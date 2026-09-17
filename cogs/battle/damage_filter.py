"""
battle/damage_filter.py
-----------------------
DamageFilter — pure damage pre-processor for one BattleSession.

Handles the four steps that happen *before* any ability fires:

  Step 1 — Tick / expire duration buffs & silences
  Step 2 — Apply mover's active ATK buffs & damage-amp stacks
  Step 3 — Invulnerability check (bypassable by ignore_invuln / true_damage)
  Step 4 — Shield absorption (bypassable by true_damage)

These steps have no knowledge of individual bey abilities.  They read and
write state through StatusManager only — they never touch ability dicts.

AbilityEngine.apply() should call DamageFilter.run() at the top of its
resolution loop, before any trigger (passive, on_take_damage, on_win, etc.)
fires.  The filter returns the adjusted (dmg_dealt, dmg_taken, logs) tuple
and a ``mover_silenced`` bool that apply() can use to gate ability triggers.

Usage
-----
    from .damage_filter import DamageFilter

    # One instance per BattleSession; pass in the same session reference.
    self.damage_filter = DamageFilter(session)

    # Inside AbilityEngine.apply():
    dmg_dealt, dmg_taken, filter_logs, mover_silenced = (
        self.damage_filter.run(
            mover_key, other_key,
            mover_blade, other_blade,
            move, dmg_dealt, dmg_taken,
            is_first_hit=is_first_hit,
        )
    )
    logs.extend(filter_logs)
    # … then proceed with steps 5–13 gated on `not mover_silenced`

Integration notes
-----------------
* DamageFilter reads all state from ``session.status`` (a
  StatusManager instance).  It never touches AbilityEngine's own dicts.
* Steps 1–4 in ability_engine.py (lines ~202–277) can be deleted once this
  module is wired in.
* The ``is_first_hit`` guard is preserved: buff ticking, ATK buff application,
  and the invuln/silence announcements all fire exactly once for multi-hit
  Specials.  Shield absorption still fires on every hit (correct — each hit
  must eat shield independently).
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

from . import button_profile
from .constants import MOVE_ATTACK, MOVE_SPECIAL, MOVE_STAMINA

if TYPE_CHECKING:
    from .session import BattleSession
    from .status_manager import StatusManager


class DamageFilter:
    """Stateless damage pre-processor.

    All mutable state lives in ``session.status_manager``.  DamageFilter
    itself holds no per-player dicts — it is safe to construct once and reuse
    across every call to ``apply()``.
    """

    def __init__(self, session: "BattleSession") -> None:
        self.session = session

    # ── convenience property ──────────────────────────────────────────────────

    @property
    def _sm(self) -> "StatusManager":
        """Return the session's StatusManager instance."""
        if not hasattr(self.session, 'status'):
            raise AttributeError(
                "BattleSession missing 'status' attribute. "
                "Ensure StatusManager is assigned to session.status."
            )
        return self.session.status

    # =========================================================================
    #  Public entry point
    # =========================================================================

    def run(
        self,
        mover_key:   str,
        other_key:   str,
        mover_blade: dict,
        other_blade: dict,
        move:        str,
        dmg_dealt:   int,
        dmg_taken:   int,
        is_first_hit: bool = True,
    ) -> tuple[int, int, list[str], bool]:
        """Run steps 1–4 of the resolution order.

        Returns
        -------
        (dmg_dealt, dmg_taken, logs, mover_silenced)

        ``mover_silenced`` — True if the mover's ability is currently sealed.
        AbilityEngine.apply() should gate all ability triggers (steps 5–13)
        on ``not mover_silenced``.
        """
        logs: list[str] = []

        # ── Step 1: Tick buffs & silences ─────────────────────────────────────
        # Guard with is_first_hit: multi-hit Specials call apply() once per hit.
        # Ticking on every hit would drain buff durations N× per round and could
        # expire a silence mid-special.
        if is_first_hit:
            self._step1_tick(mover_key, logs)

        mover_silenced = self._sm.is_silenced(mover_key)

        # Announce silence once — without this guard, a multi-hit Special would
        # emit "Ability Sealed" on every hit instead of once.
        if mover_silenced and is_first_hit:
            turns_left = self._sm.silenced_turns.get(mover_key, 0)
            logs.append(
                f"  🔇 **Ability Sealed** — {mover_blade['name']}'s ability is "
                f"suppressed! ({turns_left} turn(s) left)"
            )

        # ── Step 2: ATK buffs & damage amplification ──────────────────────────
        # FIX #9: Skip ATK buff application when damage was already zeroed
        # by invulnerability or other effects. Prevents inflating 0 damage.
        if not mover_silenced and is_first_hit and dmg_dealt > 0:
            dmg_dealt = self._step2_atk_amp(mover_key, move, dmg_dealt, logs)

        # ── Step 3: Invulnerability check ─────────────────────────────────────
        dmg_dealt = self._step3_invuln(
            mover_key, other_key, mover_blade, other_blade,
            move, dmg_dealt, is_first_hit, logs,
        )

        # ── Step 3b: Evasion check ────────────────────────────────────────────
        # Evasion is rolled per-ability; if ANY evasion ability procs, the hit
        # is fully dodged.  This runs before shield so a dodged hit never
        # touches the shield.
        if dmg_dealt > 0 and move in (MOVE_ATTACK, MOVE_SPECIAL):
            evaded, evasion_logs = self._step3b_evasion(mover_key, other_key, mover_blade, other_blade)
            logs.extend(evasion_logs)
            if evaded:
                dmg_dealt = 0

        # ── Step 4: Shield absorption ─────────────────────────────────────────
        # Shield absorbs on EVERY hit (correct behaviour); a breaking shield
        # emits its log naturally when remaining drops to 0.
        dmg_dealt = self._step4_shield(
            mover_key, other_key, other_blade,
            move, dmg_dealt, is_first_hit, logs,
        )

        # ── Step 4b: Knockout resistance (% damage reduction) ─────────────────
        # Applied after shield so the reduction only affects damage that
        # actually reaches the Bey.
        if dmg_dealt > 0:
            dmg_dealt = self._step4b_knockout_resist(other_key, other_blade, dmg_dealt, logs)

        # ── Step 4c: a wobbling defender takes more ───────────────────────────
        # The stability gradient. Sits with the other per-defender damage
        # modifiers, and reads `other_key` because it is the side being hit.
        if dmg_dealt > 0:
            dmg_dealt = self._step4c_stability_strain(
                other_key, other_blade, dmg_dealt, logs)

        # ── Step 4d: a hit knocks the DEFENDER's charge off ────────────────────
        # `other_key` is the side taking the damage here. Charging is meant to
        # be a gamble — banking power while standing still — so connecting with
        # someone mid-charge is what makes it one.
        if dmg_dealt > 0:
            self._break_charge_stacks(other_key, other_blade, logs)

        return dmg_dealt, dmg_taken, logs, mover_silenced

    def _step4c_stability_strain(self, key: str, blade: dict,
                                 dmg_dealt: int, logs: list[str]) -> int:
        """A defender low on stability takes more damage. Inert unless tiered.

        This is what turns stability from a cliff into a resource: before it,
        nothing whatsoever happened as the bar fell and then 0 killed you
        instantly, so there was no reason to spend a turn on Stamina until it
        was already too late.
        """
        try:
            eff = self.session.stability_manager.strain(key, blade)
        except Exception:                                # noqa: BLE001
            return dmg_dealt
        mult = float(eff.get("incoming_mult", 1.0) or 1.0)
        if mult <= 1.0:
            return dmg_dealt
        before = dmg_dealt
        dmg_dealt = math.ceil(dmg_dealt * mult)
        if dmg_dealt != before:
            logs.append(
                f"  🌀 **Unsteady** — {blade.get('name', '?')} is wobbling: "
                f"+{int((mult - 1) * 100)}% damage taken "
                f"({before} → **{dmg_dealt}**)!"
            )
        return dmg_dealt

    def _break_charge_stacks(self, key: str, blade: dict,
                             logs: list[str]) -> None:
        """Knock a defender's banked Charge stacks loose. Inert unless authored."""
        cfg = button_profile.charge_cfg(blade)
        if cfg["max_stacks"] <= 0 or not cfg["lost_on_hit"]:
            return
        eng = self.session.ability
        stacks = int(eng.counters.get((key, "charge_stack"), 0))
        if stacks <= 0:
            return
        eng.counters[(key, "charge_stack")] = 0
        logs.append(
            f"  💢 **Overcharge broken** — {blade.get('name', '?')} loses "
            f"{stacks} banked charge stack(s)!"
        )

    # =========================================================================
    #  Step 1 — Tick buffs & silences
    # =========================================================================

    def _step1_tick(self, mover_key: str, logs: list[str]) -> None:
        """Advance all duration counters for the mover."""
        sm = self._sm
        sm.tick_buffs(mover_key, logs)
        sm.tick_silence(mover_key, logs)
        sm.tick_universal(mover_key)

    # =========================================================================
    #  Step 2 — ATK buffs & damage amplification
    # =========================================================================

    def _step2_atk_amp(
        self,
        mover_key: str,
        move:      str,
        dmg_dealt: int,
        logs:      list[str],
    ) -> int:
        """Apply timed ATK buffs and dmg_amp stacks; returns adjusted dmg_dealt."""
        sm = self._sm

        # Timed ATK buff from active_buffs list
        atk_bonus = sm.get_buff_bonus(mover_key, "attack")
        from .purification import stat_bonus
        atk_bonus -= stat_bonus(self.session, mover_key, "attack")
        blade = self.session.blades.get(mover_key, {})
        if move == MOVE_SPECIAL and (blade.get("special_move") or {}).get("damage_formula"):
            atk_bonus = 0  # Formula already uses the live buffed ATK stat.
        if atk_bonus and move in (MOVE_ATTACK, MOVE_SPECIAL):
            dmg_dealt += atk_bonus
            logs.append(
                f"  ✨ **Active Buff** — +**{atk_bonus} ATK** from timed power-up!"
            )

        # Damage amplification stacks
        amp = sm.get_dmg_amp(mover_key)
        if amp > 0 and dmg_dealt > 0:
            bonus = math.ceil(dmg_dealt * amp)
            dmg_dealt += bonus
            logs.append(
                f"  📈 **Damage Amp** — +{int(amp * 100)}% amplification → +**{bonus} dmg**!"
            )

        # Charge stacks, cashed in.
        #
        # This lives here, with the other outgoing-damage accumulators, rather
        # than in the Attack path and _resolve_special separately: this method
        # already runs for both MOVE_ATTACK and MOVE_SPECIAL, so one site
        # covers both buttons and they cannot drift apart. It also matters
        # that run() gates this whole step behind is_first_hit — a multi-hit
        # Special must spend the bank ONCE, not once per hit, which is the
        # same trap that forced step 1's duration tick to be gated.
        dmg_dealt = self._spend_charge_stacks(mover_key, move, dmg_dealt, logs)

        return dmg_dealt

    def _spend_charge_stacks(
        self,
        mover_key: str,
        move:      str,
        dmg_dealt: int,
        logs:      list[str],
    ) -> int:
        """Convert banked Charge stacks into damage on this move, then clear.

        Inert for every blade that has not authored `button_profile.charge`:
        `max_stacks` defaults to 0, so nothing is ever banked and there is
        nothing here to spend.
        """
        if move not in (MOVE_ATTACK, MOVE_SPECIAL) or dmg_dealt <= 0:
            return dmg_dealt
        eng = self.session.ability
        blade = self.session.blades.get(mover_key)
        cfg = button_profile.charge_cfg(blade)
        if cfg["max_stacks"] <= 0 or cfg["per_stack_pct"] <= 0:
            return dmg_dealt
        stacks = int(eng.counters.get((mover_key, "charge_stack"), 0))
        if stacks <= 0:
            return dmg_dealt
        pct = cfg["per_stack_pct"] * stacks
        bonus = math.ceil(dmg_dealt * pct / 100)
        eng.counters[(mover_key, "charge_stack")] = 0
        if bonus > 0:
            dmg_dealt += bonus
            name = (blade or {}).get("name", "?")
            logs.append(
                f"  🔋 **Overcharge** — {name} releases {stacks} charge "
                f"stack(s) for +{int(pct)}% → +**{bonus} dmg**!"
            )
        return dmg_dealt

    # =========================================================================
    #  Step 3 — Invulnerability
    # =========================================================================

    def _step3_invuln(
        self,
        mover_key:   str,
        other_key:   str,
        mover_blade: dict,
        other_blade: dict,
        move:        str,
        dmg_dealt:   int,
        is_first_hit: bool,
        logs:        list[str],
    ) -> int:
        """Apply invulnerability logic; returns adjusted dmg_dealt."""
        sm = self._sm

        is_invuln     = sm.is_invulnerable(other_key)
        ignore_invuln = sm.get_duration("ignore_invuln_turns", mover_key) > 0
        is_true_dmg   = sm.get_duration("true_damage_turns",  mover_key) > 0

        # Stamina moves are never blocked by invulnerability
        if not is_invuln or move == MOVE_STAMINA:
            return dmg_dealt

        if is_true_dmg:
            # True damage pierces everything — log once per move
            if is_first_hit:
                logs.append(
                    f"  💀 **True Damage** — {mover_blade['name']} pierces invulnerability! "
                    f"Damage IGNORES Dead Armor!"
                )
            # dmg_dealt passes through unchanged
        elif ignore_invuln:
            if is_first_hit:
                logs.append(
                    f"  🔓 **Invuln Break** — {mover_blade['name']} shatters the barrier! "
                    f"Invulnerability BYPASSED!"
                )
            # dmg_dealt passes through unchanged
        else:
            if is_first_hit:
                logs.append(
                    f"  🛡️ **Dead Armor** — {other_blade['name']} is INVULNERABLE! "
                    f"All damage blocked!"
                )
            dmg_dealt = 0

        return dmg_dealt

    # =========================================================================
    #  Step 4 — Shield absorption
    # =========================================================================

    def _step4_shield(
        self,
        mover_key:   str,
        other_key:   str,
        other_blade: dict,
        move:        str,
        dmg_dealt:   int,
        is_first_hit: bool,
        logs:        list[str],
    ) -> int:
        """Absorb damage into the defender's shield; returns adjusted dmg_dealt."""
        sm = self._sm

        shield      = sm.get_shield(other_key)
        is_true_dmg = sm.get_duration("true_damage_turns", mover_key) > 0

        if shield <= 0 or dmg_dealt <= 0:
            return dmg_dealt

        if is_true_dmg:
            # True damage bypasses the shield entirely
            if is_first_hit:
                logs.append(
                    f"  💀 **True Damage** — Shield on {other_blade['name']} IGNORED!"
                )
            return dmg_dealt

        # Normal absorption — shield takes the hit
        absorbed  = sm.absorb_shield(other_key, dmg_dealt)
        dmg_dealt -= absorbed
        remaining  = sm.get_shield(other_key)
        logs.append(
            f"  🔵 **Shield** — {other_blade['name']}'s shield absorbed **{absorbed} dmg**! "
            + (f"({remaining} HP remaining)" if remaining > 0 else "Shield BROKEN!")
        )

        return dmg_dealt

    # =========================================================================
    #  Step 3b — Evasion (new)
    # =========================================================================

    def _step3b_evasion(
        self,
        mover_key:   str,
        other_key:   str,
        mover_blade: dict,
        other_blade: dict,
    ) -> tuple[bool, list[str]]:
        """Roll evasion_chance on the defender's abilities.

        Returns (evaded: bool, logs: list[str]).
        If ANY evasion ability procs, the entire hit is dodged.
        """
        logs: list[str] = []

        # Collect evasion chances from defender's abilities
        for ab in self.session.ability._get_abilities(other_blade, other_key):
            evasion_chance = ab.get("evasion_chance", 0)
            if evasion_chance > 0 and random.random() < evasion_chance:
                logs.append(
                    f"  💨 **EVADED** — {other_blade['name']} vanished into thin air! "
                    f"({ab.get('name', 'Evasion')} — {int(evasion_chance * 100)}% chance)"
                )
                return True, logs

        return False, logs

    # =========================================================================
    #  Step 4b — Knockout resistance (new)
    # =========================================================================

    def _step4b_knockout_resist(
        self,
        other_key:   str,
        other_blade: dict,
        dmg_dealt:   int,
        logs:        list[str],
    ) -> int:
        """Apply % damage reduction from knockout_resistance_pct abilities."""
        for ab in self.session.ability._get_abilities(other_blade, other_key):
            resist_pct = ab.get("knockout_resistance_pct", 0)
            if resist_pct > 0:
                reduction = int(dmg_dealt * (resist_pct / 100))
                if reduction > 0:
                    dmg_dealt = max(1, dmg_dealt - reduction)
                    logs.append(
                        f"  🛡️ **{ab.get('name', 'Knockout Resistance')}** — "
                        f"{other_blade['name']} reduced damage by {resist_pct}% "
                        f"(-{reduction} dmg) → **{dmg_dealt} dmg**!"
                    )
                break  # Only one resistance applies per hit
        return dmg_dealt
