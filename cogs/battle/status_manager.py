"""
cogs/status_manager.py
----------------------
StatusManager — the single source of truth for ALL per-player status
effects, stacks, and duration counters in a BattleSession.

Design Principles
-----------------
  1. No ability *logic* lives here.  This module is a structured data
     store plus tick helpers.  The "what does this effect do" question
     is answered by AbilityEngine; the "does this effect exist, and
     for how long" question is answered here.

  2. Every dict that was previously scattered across AbilityEngine.__init__
     (rage_stacks, burn_stacks, silenced_turns, etc.) now lives here.
     AbilityEngine delegates all reads and writes through this object.

  3. Tick methods (tick_buffs, tick_silences, tick_universal_mods,
     tick_dot) are called by the decision_engine at the correct phase
     of each round — AbilityEngine no longer controls when ticking happens.

  4. The public surface is intentional:
       • get / set / clear helpers for each logical group
       • tick_* methods advance durations and return log lines
       • snapshot() returns a read-only dict for debugging / embeds

Public API
----------
  StatusManager(session)

  # ── Buff management ──────────────────────────────────────────────────────
  .add_buff(key, stat, amount, rounds)
  .get_buff_bonus(key, stat) -> int
  .clear_buffs(key, stat)           — remove all buffs of a given stat
  .tick_buffs(key, logs)            — advance rounds_left, append expiry lines

  # ── Silence ──────────────────────────────────────────────────────────────
  .silence(key, turns)
  .is_silenced(key) -> bool
  .tick_silence(key, logs)          — decrement, append "lifted" line if 0

  # ── Scalar duration effects (ignore_defense, true_damage, etc.) ─────────
  .set_duration(effect, key, turns) — max-merge
  .get_duration(effect, key) -> int
  .tick_universal(key)              — decrement ignore_invuln, true_damage

  # ── Shield ───────────────────────────────────────────────────────────────
  .add_shield(key, amount)
  .absorb_shield(key, damage) -> int   — returns absorbed amount, mutates shield
  .get_shield(key) -> int

  # ── Invulnerability ───────────────────────────────────────────────────────
  .set_invulnerable(key, turns)
  .is_invulnerable(key) -> bool
  .decrement_invulnerable(key)

  # ── Damage amp stacks ─────────────────────────────────────────────────────
  .add_dmg_amp(key, pct_float)
  .get_dmg_amp(key) -> float

  # ── Burn / DoT ────────────────────────────────────────────────────────────
  .apply_burn(key, ab) -> list[str]
  .tick_burn(key, blade) -> list[str]        — deals damage, decrements duration

  # ── Ability-specific counters (read/write via attributes) ────────────────
  rage_stacks, solar_flare_ready, revival_used, attack_streak,
  limit_break_bonus, special_boost_flat, dead_armor_triggered,
  stamina_steal_bonus, post_rebirth_reflect, deflect_active,
  pending_chains, on_hit_burst_counters, on_hit_enemy_amp_pending,
  shatter_stacks, shatter_triggered, demon_mode_atk_stacks,
  demon_mode_active, soul_hunt_streak, ability_2_disabled,
  guaranteed_crit_turns, pre_special_amp

  # ── Misc ─────────────────────────────────────────────────────────────────
  .snapshot(key) -> dict            — all effect values for one player
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from .constants import BASE_HP

if TYPE_CHECKING:
    from .session import BattleSession


class StatusManager:
    """Central store for all per-player battle-effect state.

    One instance per BattleSession, constructed before AbilityEngine so that
    AbilityEngine can be refactored to delegate here incrementally.
    """

    # ── Duration-based effect keys recognised by set_duration / get_duration ──
    _DURATION_KEYS = frozenset({
        "ignore_defense_turns",
        "ignore_invuln_turns",
        "true_damage_turns",
        "post_special_convert_turns",
        "post_special_lock_turns",
    })

    def __init__(self, session: "BattleSession") -> None:
        self.session = session

        # ── Timed / stacking buff list ────────────────────────────────────────
        self.active_buffs: dict[str, list[dict]] = {}

        # ── Silence ───────────────────────────────────────────────────────────
        self.silenced_turns: dict[str, int] = {}

        # ── Duration counters (scalar) ────────────────────────────────────────
        self.ignore_defense_turns: dict[str, int]   = {}
        self.ignore_invuln_turns:  dict[str, int]   = {}
        self.true_damage_turns:    dict[str, int]   = {}

        # ── Shield ────────────────────────────────────────────────────────────
        self.shield_hp: dict[str, int] = {}

        # ── Invulnerability ───────────────────────────────────────────────────
        self.invulnerable_turns: dict[str, int] = {}

        # ── Damage amplification ──────────────────────────────────────────────
        self.dmg_amp_stacks: dict[str, float] = {}

        # ── Burn / DoT ────────────────────────────────────────────────────────
        self.burn_stacks:   dict[str, int] = {}
        self.burn_dmg:      dict[str, int] = {}
        self.burn_duration: dict[str, int] = {}

        # ── Rage / on_take_damage stacks ──────────────────────────────────────
        self.rage_stacks: dict[str, int] = {}

        # ── Solar Flare (Prominence Valkyrie) ─────────────────────────────────
        self.solar_flare_ready: dict[str, bool] = {}

        # ── Revival / rebirth ─────────────────────────────────────────────────
        self.revival_used:          dict[str, bool] = {}
        self.dead_armor_triggered:  dict[str, bool] = {}

        # ── Attack streak (Strike Valtryek) ───────────────────────────────────
        self.attack_streak: dict[str, int] = {}

        # ── Limit Break bonus (flat ATK accumulator) ──────────────────────────
        self.limit_break_bonus: dict[str, int] = {}

        # ── Special move flat boost (Dead Armor shed, chains) ─────────────────
        self.special_boost_flat: dict[str, int] = {}

        # ── Stamina steal bonus ───────────────────────────────────────────────
        self.stamina_steal_bonus: dict[str, int] = {}

        # ── Post-rebirth reflect (Dead Phoenix fix 5) ─────────────────────────
        self.post_rebirth_reflect: dict[str, int] = {}

        # ── Deflect passive (Z Achilles fix 8) ───────────────────────────────
        self.deflect_active: dict[str, bool] = {}
        self.deflect_duration: dict[str, int] = {}  # NEW: track deflect duration

        # ── Ability chain queue ───────────────────────────────────────────────
        self.pending_chains: dict[str, list] = {}

        # ── on_hit burst state (Reckless Fury / Bloody Longinus) ─────────────
        self.on_hit_burst_counters:      dict[str, int]  = {}
        self.on_hit_enemy_amp_pending:   dict[str, bool] = {}

        # ── Shatter stacks (Dynamite Belial) ──────────────────────────────────
        self.shatter_stacks:    dict[str, int]  = {}
        self.shatter_triggered: dict[str, bool] = {}

        # ── Demon Mode (Dynamite Belial) ──────────────────────────────────────
        self.demon_mode_atk_stacks: dict[str, int]  = {}
        self.demon_mode_active:     dict[str, bool] = {}

        # ── Soul Hunt streak (Hollow Deathscyther) ────────────────────────────
        self.soul_hunt_streak: dict[str, int] = {}

        # ── Shadow Dragon King ────────────────────────────────────────────────
        self.ability_2_disabled: dict[str, bool] = {}

        # ── Guaranteed crit (Guilty Longinus — Condemned Mode) ───────────────
        self.guaranteed_crit_turns: dict[str, int] = {}

        # ── Pre-special damage amp (Shadow Dragon King — Dark Weather) ────────
        self.pre_special_amp: dict[str, float] = {}

        # ── Ronin Dragoon — reverse stacks ───────────────────────────────────
        self.dragoon_reverse_stacks: dict[str, int] = {}

        # ── Imperial Dragon — stamina-win streak counter ──────────────────────
        self.imperial_streak: dict[str, int] = {}

        # ── Imperial Dragon — Last Stand (one-time trigger) ───────────────────
        self.dragon_last_stand_used: dict[str, bool] = {}

    # =========================================================================
    #  Buff management
    # =========================================================================

    def add_buff(self, key: str, stat: str, amount: int, rounds: int) -> None:
        """Add a timed stat buff for *key*."""
        if rounds <= 0:
            return  # Reject 0-round buffs
        self.active_buffs.setdefault(key, []).append(
            {"stat": stat, "amount": amount, "rounds_left": rounds}
        )

    def get_buff_bonus(self, key: str, stat: str) -> int:
        """Sum of all active buff amounts for *stat* on *key*."""
        return sum(
            b["amount"]
            for b in self.active_buffs.get(key, [])
            if b["stat"] == stat
        )

    def clear_buffs(self, key: str, stat: str) -> None:
        """Remove all timed buffs of *stat* from *key*."""
        self.active_buffs[key] = [
            b for b in self.active_buffs.get(key, []) if b["stat"] != stat
        ]

    def tick_buffs(self, key: str, logs: list[str]) -> None:
        """Advance all buff durations for *key*; append expiry messages."""
        alive = []
        for buf in self.active_buffs.get(key, []):
            buf["rounds_left"] -= 1
            if buf["rounds_left"] > 0:
                alive.append(buf)
            else:
                logs.append(
                    f"  ⏳ Buff expired: **{buf['stat'].upper()} +{buf['amount']}** worn off."
                )
        self.active_buffs[key] = alive

        # Also tick deflect duration
        if self.deflect_duration.get(key, 0) > 0:
            self.deflect_duration[key] -= 1
            if self.deflect_duration[key] <= 0:
                self.deflect_active[key] = False
                blade_name = self.session.blades.get(key, {}).get("name", key)
                logs.append(f"  🛡️ **Deflect** faded on {blade_name}.")

    # =========================================================================
    #  Silence
    # =========================================================================

    def silence(self, key: str, turns: int) -> None:
        """Apply (or extend) a silence on *key*."""
        self.silenced_turns[key] = self.silenced_turns.get(key, 0) + turns

    def is_silenced(self, key: str) -> bool:
        return self.silenced_turns.get(key, 0) > 0

    def tick_silence(self, key: str, logs: list[str]) -> None:
        """Decrement silence counter; append a "lifted" message when it expires."""
        if self.silenced_turns.get(key, 0) > 0:
            self.silenced_turns[key] -= 1
            if self.silenced_turns[key] == 0:
                blade_name = self.session.blades.get(key, {}).get("name", key)
                logs.append(
                    f"  🔊 **Silence lifted** — {blade_name}'s ability is restored!"
                )

    # =========================================================================
    #  Scalar duration effects
    # =========================================================================

    def set_duration(self, effect: str, key: str, turns: int) -> None:
        """Set a duration effect to max(current, turns)."""
        if effect not in self._DURATION_KEYS:
            raise ValueError(f"StatusManager has no duration effect '{effect}'")
        d = getattr(self, effect)
        d[key] = max(d.get(key, 0), turns)

    def get_duration(self, effect: str, key: str) -> int:
        if effect not in self._DURATION_KEYS:
            return 0
        d = getattr(self, effect)
        return d.get(key, 0)

    def tick_universal(self, key: str) -> None:
        """Decrement ignore_invuln and true_damage duration counters."""
        for d in (self.ignore_invuln_turns, self.true_damage_turns):
            if d.get(key, 0) > 0:
                d[key] = max(0, d[key] - 1)

    # =========================================================================
    #  Shield
    # =========================================================================

    def add_shield(self, key: str, amount: int) -> None:
        self.shield_hp[key] = self.shield_hp.get(key, 0) + amount

    def get_shield(self, key: str) -> int:
        return self.shield_hp.get(key, 0)

    def absorb_shield(self, key: str, damage: int) -> int:
        """Subtract as much damage as possible from shield; returns absorbed amount."""
        if damage < 0:
            return 0  # Negative damage does not increase shield
        current = self.shield_hp.get(key, 0)
        absorbed = min(current, damage)
        self.shield_hp[key] = current - absorbed
        return absorbed

    # =========================================================================
    #  Invulnerability
    # =========================================================================

    def set_type(self, key: str, type_str: str) -> None:
        """Record the active move-type label for a player (e.g. 'Attack', 'Defense').
        Used by Infinity Achilles mode switching; no mechanical effect in StatusManager itself.
        """
        if not hasattr(self, 'active_type'):
            self.active_type: dict[str, str] = {}
        self.active_type[key] = type_str

    def set_invulnerable(self, key: str, turns: int) -> None:
        self.invulnerable_turns[key] = turns

    def is_invulnerable(self, key: str) -> bool:
        return self.invulnerable_turns.get(key, 0) > 0

    def decrement_invulnerable(self, key: str) -> None:
        if self.invulnerable_turns.get(key, 0) > 0:
            self.invulnerable_turns[key] = max(0, self.invulnerable_turns[key] - 1)

    # =========================================================================
    #  Damage amplification
    # =========================================================================

    def add_dmg_amp(self, key: str, pct: float) -> None:
        self.dmg_amp_stacks[key] = self.dmg_amp_stacks.get(key, 0.0) + pct

    def get_dmg_amp(self, key: str) -> float:
        return self.dmg_amp_stacks.get(key, 0.0)

    # =========================================================================
    #  Burn / DoT
    # =========================================================================

    def apply_burn(self, target_key: str, ab: dict) -> list[str]:
        """Apply or stack a burn from an ability dict onto *target_key*."""
        logs       = []
        per_turn   = ab.get("burn_damage_per_turn", 0)
        duration   = ab.get("burn_duration", 0)
        max_stacks = ab.get("max_burn_stacks", 1)

        if not per_turn or not duration:
            return logs

        current = self.burn_stacks.get(target_key, 0)
        if current < max_stacks:
            self.burn_stacks[target_key]   = current + 1
            self.burn_dmg[target_key]      = per_turn
            self.burn_duration[target_key] = duration
            logs.append(
                f"  🔥 **{ab.get('name', 'Burn')}** — Burn applied! "
                f"**{per_turn} dmg/turn** for **{duration} turn(s)** "
                f"(stack {current + 1}/{max_stacks})!"
            )
        else:
            self.burn_duration[target_key] = max(
                self.burn_duration.get(target_key, 0), duration
            )
            logs.append(
                f"  🔥 **{ab.get('name', 'Burn')}** — Burn refreshed! "
                f"(max {max_stacks} stack(s) already active)"
            )
        return logs

    def tick_burn(self, key: str, blade: dict) -> list[str]:
        """Deal burn damage to *key* and decrement duration."""
        logs     = []
        stacks   = self.burn_stacks.get(key, 0)
        duration = self.burn_duration.get(key, 0)
        dmg_ps   = self.burn_dmg.get(key, 0)

        if stacks > 0 and duration > 0 and dmg_ps > 0:
            total = stacks * dmg_ps
            self.session.hp[key] = max(0, self.session.hp[key] - total)
            self.burn_duration[key] = duration - 1
            blade_name = blade.get("name", key)
            logs.append(
                f"  🔥 **Burn** — {blade_name} takes **{total} burn dmg** "
                f"({stacks} stack(s) × {dmg_ps}/turn, {self.burn_duration[key]} turn(s) left)!"
            )
            if self.burn_duration[key] <= 0:
                self.burn_stacks[key] = 0
                self.burn_dmg[key]    = 0
                logs.append(f"  🔥 **Burn** fades on {blade_name}.")

        return logs

    # =========================================================================
    #  Snapshot — for embeds and debugging
    # =========================================================================

    def snapshot(self, key: str) -> dict[str, Any]:
        """Return a read-only dict of every effect value for *key*."""
        return {
            "active_buffs":         [dict(b) for b in self.active_buffs.get(key, [])],
            "silenced_turns":       self.silenced_turns.get(key, 0),
            "ignore_defense_turns": self.ignore_defense_turns.get(key, 0),
            "ignore_invuln_turns":  self.ignore_invuln_turns.get(key, 0),
            "true_damage_turns":    self.true_damage_turns.get(key, 0),
            "shield_hp":            self.shield_hp.get(key, 0),
            "invulnerable_turns":   self.invulnerable_turns.get(key, 0),
            "dmg_amp_stacks":       self.dmg_amp_stacks.get(key, 0.0),
            "burn_stacks":          self.burn_stacks.get(key, 0),
            "burn_dmg":             self.burn_dmg.get(key, 0),
            "burn_duration":        self.burn_duration.get(key, 0),
            "rage_stacks":          self.rage_stacks.get(key, 0),
            "solar_flare_ready":    self.solar_flare_ready.get(key, False),
            "revival_used":         self.revival_used.get(key, False),
            "dead_armor_triggered": self.dead_armor_triggered.get(key, False),
            "attack_streak":        self.attack_streak.get(key, 0),
            "limit_break_bonus":    self.limit_break_bonus.get(key, 0),
            "special_boost_flat":   self.special_boost_flat.get(key, 0),
            "post_rebirth_reflect": self.post_rebirth_reflect.get(key, 0),
            "deflect_active":       self.deflect_active.get(key, False),
            "deflect_duration":     self.deflect_duration.get(key, 0),
            "shatter_stacks":       self.shatter_stacks.get(key, 0),
            "shatter_triggered":    self.shatter_triggered.get(key, False),
            "demon_mode_atk_stacks":self.demon_mode_atk_stacks.get(key, 0),
            "demon_mode_active":    self.demon_mode_active.get(key, False),
            "soul_hunt_streak":     self.soul_hunt_streak.get(key, 0),
            "ability_2_disabled":   self.ability_2_disabled.get(key, False),
            "guaranteed_crit_turns": (
                self.session.ability.guaranteed_crit_turns.get(key, 0)
                if hasattr(self.session, "ability")
                else self.guaranteed_crit_turns.get(key, 0)
            ),
            "pre_special_amp":      self.pre_special_amp.get(key, 0.0),
            "pending_chains":       [dict(s) for s in self.pending_chains.get(key, [])],
            "dragoon_reverse_stacks": self.dragoon_reverse_stacks.get(key, 0),
            "imperial_streak":        self.imperial_streak.get(key, 0),
            "dragon_last_stand_used": self.dragon_last_stand_used.get(key, False),
        }
