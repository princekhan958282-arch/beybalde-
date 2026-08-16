"""
cogs/abilities/ability_engine.py  —  Generic Rules Engine (v2)
==============================================================
A fully data-driven ability engine.  ZERO blade names appear in this file.
Every ability is expressed as *rules* in beyblades.json:

    "abilities": [
      {
        "name": "Infernal Armor",
        "description": "…",
        "rules": [
          {
            "when":   "on_take_damage",            # trigger (see TRIGGERS)
            "if":     [{"cond": "hp_below_pct", "value": 0.5}],   # optional AND-list
            "chance": 0.3,                          # optional proc chance 0–1
            "once":   "battle",                     # optional: fire once per battle
            "do":     [                             # effect ops (see OPS)
              {"op": "reflect_pct",  "value": 25},
              {"op": "shield",       "value": 40}
            ]
          }
        ]
      }
    ]

Legacy abilities (flat effect fields, no "rules" key) are auto-converted at
setup time by `legacy_convert()` — a pure *pattern* mapper with no per-blade
logic — so the whole existing roster keeps working without JSON migration.
New abilities should be written directly in rules form; ANY combination of
triggers, conditions and ops is supported without touching code.

Public API (unchanged — session.py / attack_manager.py need no edits):
  AbilityEngine(session)
  .setup(key, blade) -> list[str]
  .apply(mover_key, other_key, mover_blade, other_blade, move, matchup,
         dmg_dealt, dmg_taken, is_first_hit=True, cumulative_dmg=0,
         is_last_hit=True) -> (dmg_dealt, dmg_taken, logs)
  ._get_abilities(blade, key="") -> list[dict]
  .process_hit_proc(key, blade, okey, hit_dmg) -> (int, list[str])
  .consume_solar_flare(key, blade) -> (int, list[str])   # generic primed bonus
  .apply_stamina_regen(key) -> list[str]
  .apply_dot_tick_extras(key, blade) -> list[str]
  .needs_pre_battle_choice(key) / .resolve_pre_battle_choice(key, mode)
  attrs: guaranteed_crit_turns, special_boost_flat, post_rebirth_reflect,
         demon_mode_atk_stacks, ability_2_disabled  (shared with StatusManager)
"""
from __future__ import annotations

import math
import random
from typing import Any

from cogs.battle.constants import (
    MOVE_ATTACK, MOVE_DEFENSE, MOVE_STAMINA, MOVE_SPECIAL, MOVE_CHARGE,
)
from cogs.battle.damage_filter import DamageFilter
from .legacy_convert import legacy_convert

# ── Vocabulary ────────────────────────────────────────────────────────────────

TRIGGERS = frozenset({
    "passive",          # every round the owner acts (offensive side)
    "on_defend",        # every incoming attack attempt (defensive side)
    "on_take_damage",   # owner actually received damage this round
    # full move × result matrix
    "on_attack_win",  "on_attack_loss",  "on_attack_mirror",
    "on_defense_win", "on_defense_loss", "on_defense_mirror",
    "on_stamina_win", "on_stamina_loss", "on_stamina_mirror",
    "on_any_win", "on_any_loss",
    "on_attack_hit",    # owner's Attack landed (once per move)
    "on_hit",           # per-hit proc (each hit of Attack / multi-hit Special)
    "on_special",       # owner fired their Special (once, on first hit)
    "on_mirror",        # both picked the same move (any move)
    # threshold triggers (fire while the condition holds, once per move)
    "on_low_hp", "on_high_hp", "on_low_stamina", "on_high_stamina",
    "turn_start", "turn_end",
    "setup",
})

# default thresholds for threshold-triggers when the rule carries no own `if`
_THRESHOLD_DEFAULTS = {
    "on_low_hp":       ("hp",      "below", 0.50),
    "on_high_hp":      ("hp",      "above", 0.50),
    "on_low_stamina":  ("stamina", "below", 0.35),
    "on_high_stamina": ("stamina", "above", 0.65),
}

_MOVE_NAME = {
    MOVE_ATTACK: "attack", MOVE_DEFENSE: "defense",
    MOVE_STAMINA: "stamina", MOVE_SPECIAL: "special", MOVE_CHARGE: "charge",
}



def _avatar_resist(session, key: str, label: str):
    """Ask the defender's avatar to shrug off a status. Safe if unavailable."""
    try:
        from cogs.battle.avatar_combat import resist_status
        return resist_status(session, key, label)
    except Exception:
        return False, []


class AbilityEngine:
    """Generic, data-driven ability interpreter. One instance per BattleSession."""

    # =========================================================================
    #  Construction / state
    # =========================================================================

    def __init__(self, session: Any):
        self.session = session
        self.st      = session.status                 # StatusManager
        self.damage_filter = DamageFilter(session)

        # ── Generic rule state (the ONLY ability memory that exists) ─────────
        self.counters:   dict[tuple[str, str], int] = {}   # (key, name) -> value
        self.once_fired: set[tuple[str, int]]       = set()  # (key, rule_id)
        self.modes:      dict[str, str]             = {}   # key -> mode name
        self.primed_bonus: dict[str, int]           = {}   # one-shot dmg bonus
        self.revive_pool:  dict[str, int]           = {}   # key -> revive HP
        self.lifesteal_pct: dict[str, float]        = {}   # key -> pct of dmg healed
        self.regen_per_turn: dict[str, int]         = {}   # key -> stamina/turn
        self.hp_regen_per_turn: dict[str, int]      = {}   # key -> hp/turn
        self.heal_per_drain: dict[str, int]         = {}   # key -> HP healed per stamina drained
        self.crit_chance_bonus: dict[str, float]    = {}   # key -> engine-side crit prob
        self.crit_damage_mult:  dict[str, float]    = {}   # key -> crit multiplier (default 1.5)
        self.extra_special_hits: dict[str, int]     = {}   # key -> bonus hits on MOVE_SPECIAL
        self.debuff_immune: dict[str, bool]         = {}   # key -> ignores enemy debuffs

        # ── Compiled rules cache: blade name -> list[(rule_id, rule)] ────────
        self._compiled: dict[str, list[tuple[int, dict]]] = {}

        # ── External-compat attrs — SHARE StatusManager's dicts so snapshots,
        #    embeds and attack_manager all see one source of truth ────────────
        self.guaranteed_crit_turns = self.st.guaranteed_crit_turns
        self.special_boost_flat    = self.st.special_boost_flat
        # Cumulative percentage Special amp, grown by the `special_amp_stack`
        # op and read by AttackManager._resolve_special. Lives on the engine
        # rather than StatusManager because it is not a timed buff — it holds
        # for the whole battle and never ticks down.
        self.special_amp_stack: dict[str, float] = {}
        # [key, amount, rounds_left] for dmg_amp grants that expire. Swept by
        # tick_dmg_amps() at end of round.
        self.timed_dmg_amps: list[list] = []
        # key -> rounds during which this attacker's hits cannot be dodged.
        # Ticked down alongside the other durations at end of round.
        self.undodgeable_turns: dict[str, int] = {}
        # key -> [multiplier, rounds_left] while an `ability_amp` window is
        # open. It scales ONLY ops and passives that opt in with
        # `"ampable": true`, so a blade's Special can amplify its own kit
        # without silently amplifying anything else the player happens to
        # carry. Swept by tick_dmg_amps() with the rest.
        self.ability_amp: dict[str, list] = {}
        # key -> [percent, rounds_left]. Opened by `reflect_pct_turns` and
        # consumed in _fire_defensive on whatever hit actually lands.
        self.reflect_windows: dict[str, list] = {}
        self.post_rebirth_reflect  = self.st.post_rebirth_reflect
        self.demon_mode_atk_stacks = self.st.demon_mode_atk_stacks
        self.ability_2_disabled    = self.st.ability_2_disabled
        # HUD-tag compat (session.py battle panel reads these off the engine)
        self.rage_stacks           = self.st.rage_stacks
        self.solar_flare_ready     = self.st.solar_flare_ready
        self.revival_used          = self.st.revival_used
        self.dead_armor_triggered  = self.st.dead_armor_triggered
        self.attack_streak         = self.st.attack_streak
        self.limit_break_bonus     = self.st.limit_break_bonus
        self.stamina_steal_bonus   = self.st.stamina_steal_bonus
        self.deflect_active        = self.st.deflect_active
        self.shatter_stacks        = self.st.shatter_stacks
        self.demon_mode_active     = self.st.demon_mode_active
        self.soul_hunt_streak      = self.st.soul_hunt_streak

    # =========================================================================
    #  Rule compilation
    # =========================================================================

    def _get_abilities(self, blade: dict, key: str = "") -> list[dict]:
        """All ability dicts of a blade (respecting disabled flags)."""
        abs_ = blade.get("abilities") or []
        if key and self.ability_2_disabled.get(key) and len(abs_) > 1:
            return [abs_[0]] + list(abs_[2:])
        return list(abs_)

    def _rules_for(self, blade: dict, key: str = "") -> list[tuple[int, dict]]:
        """Compiled (rule_id, rule) list for a blade — cached per blade form.

        The cache key carries the active mode, not just the name. A two-form
        blade whose modes have DIFFERENT ability lists is one blade with one
        name, so a name-only key would let whichever form compiled first serve
        both — and both players in a mirror match can be holding the same
        blade in opposite forms.
        """
        name = blade.get("name", "?")
        mode = blade.get("active_spin_mode")
        if mode:
            name = f"{name}#{mode}"
        if name not in self._compiled:
            rules: list[dict] = []
            # `_ab_index` was never written by either path, so the
            # ability_2_disabled filter below matched nothing and the whole
            # "disable the enemy's 2nd ability" mechanic was inert. Tag each
            # rule with the index of the ability it came from, and give
            # rules-form abilities the `_name` that legacy_convert already
            # supplies so their log lines name the ability, not the blade.
            for _i, ab in enumerate(blade.get("abilities") or []):
                if isinstance(ab.get("rules"), list):
                    _src = [dict(r) for r in ab["rules"] if isinstance(r, dict)]
                    for _r in _src:
                        _r.setdefault("_name", ab.get("name", name))
                else:                       # legacy flat-field ability
                    _src = legacy_convert(ab)
                for _r in _src:
                    _r["_ab_index"] = _i
                rules.extend(_src)
            self._compiled[name] = list(enumerate(rules))
        out = self._compiled[name]
        # honour ability_2 disable for rule sets too (rules tagged with _ab_index)
        if key and self.ability_2_disabled.get(key):
            out = [(i, r) for i, r in out if r.get("_ab_index") != 1]
        return out

    # =========================================================================
    #  Condition evaluation
    # =========================================================================

    def _hp_pct(self, key: str) -> float:
        max_hp = self.session.max_hp_per_player.get(key) or self.session.max_hp or 1
        return self.session.hp.get(key, 0) / max_hp

    def _stamina_pct(self, key: str) -> float:
        try:
            sm  = self.session.stamina_manager
            cur = sm.stamina.get(key, 0)
            mx  = getattr(sm, "max_stamina", {}).get(key, 100) or 100
            return cur / mx
        except Exception:
            return 1.0

    def _check(self, cond: dict, key: str, okey: str, move: str, matchup: str) -> bool:
        c = cond.get("cond")
        v = cond.get("value")
        if c == "hp_below_pct":        return self._hp_pct(key)  <  float(v)
        if c == "hp_above_pct":        return self._hp_pct(key)  >= float(v)
        if c == "enemy_hp_below_pct":  return self._hp_pct(okey) <  float(v)
        if c == "enemy_hp_above_pct":  return self._hp_pct(okey) >= float(v)
        if c == "move_is":             return move == v
        if c == "incoming_move_is":
            # The same read as `move_is`, named for the defender's phase.
            #
            # `_fire_defensive` passes the ATTACKER'S move as `move`, so a rule
            # under `on_take_damage` guarded by `move_is: attack` is correct
            # and reads like it means the opposite. A counter gated on the
            # wrong one of those does not fail loudly — it fires on every hit
            # of a sixteen-hit Special instead of once, which is a very
            # different ability from the one written down.
            return move == v
        if c == "move_in":
            return move in [str(x) for x in (v if isinstance(v, list) else [v])]
        if c == "enemy_move_is":
            # Fails CLOSED when the enemy's move is unknown. The old fallback
            # returned True, which made the condition inert — an ability gated
            # on "the enemy is defending" fired against every move — and it was
            # invisible because BattleSession never set last_moves at all, so
            # the guard was hit on every single evaluation.
            return getattr(self.session, "last_moves", {}).get(okey) == v
        if c in ("bey_level_at_least", "bey_level_below"):
            # The wielder's BEY level, so an ability can awaken at level 100.
            # session.bey_levels is written by BattleSession from the same
            # utils.bey_levels lookup effective_blade uses. Absent (an older
            # session, a harness) it reads as level 1, which means an
            # "at_least" gate fails closed and a "below" gate passes — the
            # un-awakened form, which is the safe default.
            try:
                lvl = int((getattr(self.session, "bey_levels", {}) or {})
                          .get(key, 1) or 1)
            except (TypeError, ValueError):
                lvl = 1
            try:
                threshold = int(v)
            except (TypeError, ValueError):
                return False
            return lvl >= threshold if c == "bey_level_at_least" else lvl < threshold
        if c == "round_at_least":
            # How far into the fight we are. There was no way to express "after
            # five rounds" at all — every existing gate reads HP, stamina, a
            # counter or a move, none of which is time. Reads the session's own
            # round counter and fails CLOSED (round 0) when it is absent, so a
            # harness without one cannot accidentally satisfy the condition.
            try:
                return int(getattr(self.session, "round", 0) or 0) >= int(v)
            except (TypeError, ValueError):
                return False
        if c == "round_below":
            try:
                return int(getattr(self.session, "round", 0) or 0) < int(v)
            except (TypeError, ValueError):
                return False
        if c == "matchup_is":          return matchup == v
        if c == "mode_is":             return self.modes.get(key) == v
        if c == "counter_at_least":
            return self.counters.get((key, cond.get("name", "")), 0) >= int(v)
        if c == "counter_below":
            return self.counters.get((key, cond.get("name", "")), 0) < int(v)
        if c == "stamina_below_pct":
            try:
                sm  = self.session.stamina_manager
                cur = sm.stamina.get(key, 0)
                mx  = getattr(sm, "max_stamina", {}).get(key, 100) or 100
                return (cur / mx) < float(v)
            except Exception:
                return False
        if c == "enemy_stamina_below":
            # Absolute threshold: true while the ENEMY's current stamina is
            # below <value> (e.g. 1.5 = can no longer afford an Attack).
            try:
                sm = self.session.stamina_manager
                return sm.stamina.get(okey, 0) < float(v)
            except Exception:
                return False
        if c == "enemy_spin_is":
            # spin_direction was display-only until now. Same-spin contact
            # behaves nothing like opposite-spin contact, so abilities built
            # around rotation need to read it.
            _sp = str((self.session.blades.get(okey) or {}).get(
                "spin_direction", "Right")).strip().lower()
            return _sp == str(v).strip().lower()
        if c == "my_spin_is":
            _sp = str((self.session.blades.get(key) or {}).get(
                "spin_direction", "Right")).strip().lower()
            return _sp == str(v).strip().lower()
        if c in ("opposite_spin", "same_spin"):
            # "Are we turning against each other?" cannot be written with
            # my_spin_is + enemy_spin_is, because the `if` list is ANDed and
            # this needs an OR — right-vs-left OR left-vs-right. Written as
            # rule pairs it is four rules per ability and easy to get half
            # right, so it is one condition instead.
            #
            # A Dual blade counts as neither: it has no fixed rotation until a
            # spin mode is chosen, and `utils.spin_mode` resolves it to a real
            # direction before the session ever sees it.
            mine = str((self.session.blades.get(key) or {}).get(
                "spin_direction", "Right")).strip().lower()
            theirs = str((self.session.blades.get(okey) or {}).get(
                "spin_direction", "Right")).strip().lower()
            if mine not in ("left", "right") or theirs not in ("left", "right"):
                return False
            return (mine != theirs) if c == "opposite_spin" else (mine == theirs)
        if c == "enemy_type_not_in":
            _et = str((self.session.blades.get(okey) or {}).get("type", "")).lower()
            return _et not in [str(x).lower()
                               for x in (v if isinstance(v, list) else [v])]
        if c == "enemy_type_is":
            return (self.session.blades.get(okey, {}).get("type", "")).lower() == str(v).lower()
        if c == "my_type_is":
            return (self.session.blades.get(key, {}).get("type", "")).lower() == str(v).lower()
        return False  # unknown condition blocks (fail-closed: a typo'd cond
                      # should never make an ability fire unconditionally)

    def _rule_fires(self, rid: int, rule: dict, when: str, key: str, okey: str,
                    move: str, matchup: str) -> bool:
        if rule.get("when") != when:
            return False
        if rule.get("once") == "battle" and (key, rid) in self.once_fired:
            return False
        # threshold triggers: if the rule has no own conditions, enforce defaults
        thr = _THRESHOLD_DEFAULTS.get(when)
        if thr and not rule.get("if"):
            res, side, cut = thr
            pct = self._hp_pct(key) if res == "hp" else self._stamina_pct(key)
            if side == "below" and not pct < cut:
                return False
            if side == "above" and not pct >= cut:
                return False
        for cond in rule.get("if") or []:
            if not self._check(cond, key, okey, move, matchup):
                return False
        ch = rule.get("chance")
        if ch is not None and random.random() >= float(ch):
            return False
        if rule.get("once") == "battle":
            self.once_fired.add((key, rid))
        return True

    # =========================================================================
    #  Effect ops  —  each op mutates state and/or adjusts (dmg_dealt, dmg_taken)
    # =========================================================================

    def _heal(self, key: str, amount: int, logs: list[str], label: str) -> None:
        if amount <= 0:
            return
        max_hp = self.session.max_hp_per_player.get(key) or self.session.max_hp
        before = self.session.hp.get(key, 0)
        self.session.hp[key] = min(max_hp, before + amount)
        gained = self.session.hp[key] - before
        if gained > 0:
            logs.append(f"💚 **{label}** — healed **{gained}** HP!")

    def amp_mult(self, key: str) -> float:
        """The live `ability_amp` multiplier for this player, or 1.0.

        Read by `_run_ops` for opt-in ops and by the two passive readers
        (partial pierce, counter bonus) so one window scales the whole kit
        rather than only the parts that happen to run through an op.

        **Dormant on the round it is granted.** A Special that grants an amp
        fires its `on_special` rule on the FIRST hit, so without this the
        remaining hits of that same Special are already amplified — the move
        pays its own reward, and "+X for the next N turns" quietly becomes
        "+X right now as well". The granting round is recorded and skipped.
        """
        entry = (getattr(self, "ability_amp", None) or {}).get(key)
        try:
            if not entry or entry[1] <= 0:
                return 1.0
            if len(entry) > 2 and entry[2] == int(
                    getattr(self.session, "round", 0) or 0):
                return 1.0
            return float(entry[0])
        except (TypeError, ValueError, IndexError):      # noqa: BLE001
            return 1.0

    def _amped(self, key: str, op: dict, val):
        """Scale an op's value if it opted in AND a window is open.

        Opt-in rather than a name whitelist: the blade that grants the amp is
        the blade that decides what doubles, so an amp can never reach across
        into an unrelated ability the same player is carrying.
        """
        if not op.get("ampable"):
            return val
        mult = self.amp_mult(key)
        if mult == 1.0:
            return val
        try:
            return type(val)(val * mult) if isinstance(val, int) else val * mult
        except (TypeError, ValueError):                  # noqa: BLE001
            return val

    def _run_ops(self, rule: dict, ab_name: str, key: str, okey: str,
                 move: str, dmg_dealt: int, dmg_taken: int,
                 logs: list[str]) -> tuple[int, int]:
        for op in rule.get("do") or []:
            kind = op.get("op")
            val  = self._amped(key, op, op.get("value", 0))

            # ── outgoing damage ──────────────────────────────────────────────
            if kind == "bonus_damage":
                dmg_dealt += int(val)
                logs.append(f"⚡ **{ab_name}** — +{int(val)} bonus damage!")
            elif kind == "bonus_damage_pct":
                add = math.ceil(dmg_dealt * float(val) / 100)
                if add > 0:
                    dmg_dealt += add
                    logs.append(f"⚡ **{ab_name}** — +{add} damage ({val}%)!")
            elif kind == "damage_boost":
                # Dynamic scaling:  bonus_mult = scale × ratio(based_on)
                based = op.get("based_on", "missing_hp")
                scale = float(op.get("scale", val or 0.5))
                if   based == "missing_hp":      ratio = 1 - self._hp_pct(key)
                elif based == "hp":              ratio = self._hp_pct(key)
                elif based == "enemy_missing_hp": ratio = 1 - self._hp_pct(okey)
                elif based == "missing_stamina": ratio = 1 - self._stamina_pct(key)
                elif based == "stamina":         ratio = self._stamina_pct(key)
                else:                            ratio = 1.0
                add = math.ceil(dmg_dealt * scale * max(0.0, min(1.0, ratio)))
                if add > 0:
                    dmg_dealt += add
                    logs.append(f"📊 **{ab_name}** — +{add} damage "
                                f"(scales with {based.replace('_',' ')})!")
            elif kind == "crit_chance":
                p = float(val)
                p = p if p <= 1 else p / 100
                self.crit_chance_bonus[key] = max(self.crit_chance_bonus.get(key, 0.0), p)
            elif kind == "crit_damage":
                m = float(val)
                self.crit_damage_mult[key] = m if m > 1 else 1.5 + m
            elif kind == "true_damage":
                self.session.hp[okey] = self.session.hp.get(okey, 0) - int(val)
                logs.append(f"💥 **{ab_name}** — {int(val)} TRUE damage!")
            elif kind == "bonus_special_hits":
                # Extra hit(s) on the next Special. attack_manager reads this
                # off the engine when it resolves MOVE_SPECIAL.
                n = int(op.get("hits", val or 1))
                if n > 0 and self.extra_special_hits.get(key, 0) < n:
                    self.extra_special_hits[key] = n
                    logs.append(f"⚔️ **{ab_name}** — MAX STACKS! "
                                f"Special Move gains {n} extra hit"
                                f"{'s' if n > 1 else ''}!")
            elif kind == "counter_burst":
                # Fire a burst once a counter reaches `at`, then reset the
                # counter so it can charge again. Any permanent buffs already
                # granted by those stacks are deliberately kept.
                cname = op.get("name", "stacks")
                at    = int(op.get("at", 3))
                cur   = self.counters.get((key, cname), 0)
                if at > 0 and cur >= at:
                    dmg = int(op.get("damage", val or 0))
                    if dmg > 0:
                        self.session.hp[okey] = self.session.hp.get(okey, 0) - dmg
                        logs.append(f"🐉 **{ab_name}** — BURST! {dmg} TRUE damage!")
                    heal = int(op.get("heal", 0))
                    if heal > 0:
                        self._heal(key, heal, logs, ab_name)
                    if op.get("reset", True):
                        self.counters[(key, cname)] = 0
                        logs.append(f"♻️ **{ab_name}** — stacks reset.")
            elif kind == "execute":
                thr = float(op.get("enemy_hp_below_pct", 0.25))
                if self._hp_pct(okey) < thr:
                    self.session.hp[okey] = self.session.hp.get(okey, 0) - int(val)
                    logs.append(f"☠️ **{ab_name}** — execute! {int(val)} TRUE damage!")
            elif kind == "recoil":
                self.session.hp[key] = self.session.hp.get(key, 0) - int(val)
                logs.append(f"🩸 **{ab_name}** — {int(val)} recoil damage.")
            elif kind == "prime_bonus":
                self.primed_bonus[key] = self.primed_bonus.get(key, 0) + int(val)
                logs.append(f"🔆 **{ab_name}** — next Special primed (+{int(val)})!")

            # ── incoming damage / defense ────────────────────────────────────
            elif kind == "reduce_damage_pct":
                cut = math.ceil(dmg_dealt * float(val) / 100)
                dmg_dealt = max(0, dmg_dealt - cut)
                logs.append(f"🛡️ **{ab_name}** — damage reduced by {int(val)}%!")
            elif kind == "reduce_damage_flat":
                dmg_dealt = max(0, dmg_dealt - int(val))
                logs.append(f"🛡️ **{ab_name}** — damage reduced by {int(val)}!")
            elif kind == "negate_damage":
                if dmg_dealt > 0:
                    logs.append(f"💨 **{ab_name}** — attack EVADED!")
                dmg_dealt = 0
            elif kind == "reflect_pct":
                ref = math.ceil(dmg_dealt * float(val) / 100)
                if ref > 0:
                    dmg_taken += ref
                    logs.append(f"🪞 **{ab_name}** — reflected {ref} damage!")
            elif kind == "reflect_flat":
                if dmg_dealt > 0 and int(val) > 0:
                    dmg_taken += int(val)
                    logs.append(f"🪞 **{ab_name}** — reflected {int(val)} damage!")
            elif kind == "shield":
                self.st.add_shield(key, int(val))
                logs.append(f"🛡️ **{ab_name}** — gained a {int(val)} HP shield!")

            # ── sustain ──────────────────────────────────────────────────────
            elif kind == "heal":
                self._heal(key, int(val), logs, ab_name)
            elif kind == "heal_pct":
                max_hp = self.session.max_hp_per_player.get(key) or self.session.max_hp
                self._heal(key, math.ceil(max_hp * float(val) / 100), logs, ab_name)
            elif kind == "lifesteal_pct":
                self.lifesteal_pct[key] = max(self.lifesteal_pct.get(key, 0.0), float(val))
            elif kind == "revive":
                self.revive_pool[key] = max(self.revive_pool.get(key, 0), int(val))
            elif kind == "hp_regen":
                self.hp_regen_per_turn[key] = int(val)
            elif kind == "lose_stability":
                # Wobble. Used by same-spin matchups where the blade can't bite
                # into the opponent and loses its footing instead.
                try:
                    stm = getattr(self.session, "stability_manager", None)
                    amt = int(val)
                    if stm is not None and amt > 0:
                        logs.extend(stm._apply(key, -amt) or [])
                except Exception:
                    pass
            elif kind == "gain_stability":
                # The mirror of `lose_stability`. That op guards on `amt > 0`
                # and negates, so it cannot express a heal at all — a blade
                # that steadies itself as it lands hits had no way to say so.
                try:
                    stm = getattr(self.session, "stability_manager", None)
                    amt = float(val)
                    if stm is not None and amt > 0:
                        logs.extend(stm._apply(key, amt) or [])
                except Exception:                        # noqa: BLE001
                    pass
            elif kind == "reflect_pct_turns":
                pct   = float(val or 0)
                turns = int(op.get("turns", 1) or 1)
                if pct > 0 and turns > 0:
                    prev = self.reflect_windows.get(key)
                    # Refresh, never stack — see ability_amp for the same call.
                    if prev and prev[0] >= pct:
                        prev[1] = max(int(prev[1]), turns)
                    else:
                        self.reflect_windows[key] = [pct, turns]
                    logs.append(f"🪞 **{ab_name}** — counter stance up: "
                                f"{int(pct)}% returned for {turns} turn(s)!")
            elif kind == "ability_amp":
                # Amplify this blade's OWN opted-in ability numbers for N
                # turns. `turns` is required in spirit: an amp with no expiry
                # is a permanent stat line wearing a Special's clothes.
                mult  = float(val or 2.0)
                turns = int(op.get("turns", 0) or 0)
                if turns > 0 and mult > 1.0:
                    now = int(getattr(self.session, "round", 0) or 0)
                    # `turns + 1`, because the granting round is dormant (see
                    # amp_mult) and is then decremented by this round's own
                    # tick — so "5 turns" is five LIVE turns, not four.
                    prev = self.ability_amp.get(key)
                    # Re-firing refreshes rather than stacks. Two overlapping
                    # windows multiplying to 4x is not what "double" means,
                    # and it is what a player would try first.
                    if prev and prev[0] >= mult:
                        # Extend, but KEEP the original grant round. Stamping
                        # `now` here would re-arm the dormancy on an already
                        # live window, so casting the Special a second time
                        # would switch your own amp off for that round.
                        prev[1] = max(int(prev[1]), turns + 1)
                    else:
                        self.ability_amp[key] = [mult, turns + 1, now]
                    logs.append(f"⚜️ **{ab_name}** — its kit is AMPLIFIED "
                                f"×{mult:g} for {turns} turn(s)!")
            elif kind == "buff_all_pct":
                # +X% to every stat, resolved against the blade's own base
                # stats. The flat `buff` op can't express this: the legacy
                # field holds a fraction (0.08) and int() flattened it to 0,
                # so the bonus silently did nothing.
                try:
                    pct   = float(val)
                    pct   = pct if pct <= 1 else pct / 100
                    base  = (self.session.blades.get(key) or {}).get("stats") or {}
                    turns = int(op.get("turns", 2))
                    parts = []
                    for stat in ("attack", "defense", "stamina"):
                        amt = int(round(float(base.get(stat, 0)) * pct))
                        if amt > 0:
                            self.st.add_buff(key, stat, amt, turns)
                            parts.append(f"{stat[:3].upper()} +{amt}")
                    if parts:
                        logs.append(f"⚖️ **{ab_name}** — {', '.join(parts)} "
                                    f"({int(pct * 100)}% all stats)!")
                except Exception:
                    pass
            elif kind == "buff_all":
                # Flat +N to every stat. `buff_all_pct` could not express this:
                # it treats its value as a fraction, so a mode granting a flat
                # "+10 all stats" (Belial's DEMON MODE, Kerbeus' TRIPLE FURY)
                # had no op to map onto and was dropped entirely.
                amt   = int(val)
                turns = int(op.get("turns", 2))
                if amt:
                    for stat in ("attack", "defense", "stamina"):
                        self.st.add_buff(key, stat, amt, turns)
                    logs.append(f"⚖️ **{ab_name}** — all stats {amt:+d}"
                                + (" (permanent)!" if turns >= 99
                                   else f" for {turns} turns!"))
            elif kind == "debuff_immune":
                self.debuff_immune[key] = True
                logs.append(f"🛡️ **{ab_name}** — immune to debuffs!")
            elif kind == "stamina_regen":
                # float, not int — the stamina economy runs on a 0–15 scale
                # where a 0.5/turn trickle is a meaningful value and int()
                # would silently round it away to nothing.
                # ACCUMULATE: a blade can have two abilities that each grant
                # regen (Garuda: 2/turn from one, more from another). A plain
                # assignment let whichever ran last erase the other.
                self.regen_per_turn[key] = (
                    self.regen_per_turn.get(key, 0.0) + float(val))

            # ── buffs / debuffs (via StatusManager => visible in embeds) ─────
            elif kind == "buff":
                self.st.add_buff(key, op.get("stat", "attack"), int(op.get("amount", val)),
                                 int(op.get("turns", 2)))
                _t   = int(op.get("turns", 2))
                _amt = int(op.get("amount", val))
                # Debuffs are real (Penta Sword Mode trades DEF and STA for
                # ATK), so sign the number instead of always prefixing "+" —
                # that printed "Defense +-20".
                logs.append(f"{'📈' if _amt >= 0 else '📉'} **{ab_name}** — "
                            f"{op.get('stat','attack').title()} {_amt:+d} "
                            + ("(permanent)!" if _t >= 99 else f"for {_t} turns!"))
            elif kind == "enemy_debuff":
                if self.debuff_immune.get(okey):
                    logs.append(f"🛡️ **{ab_name}** — enemy is immune to debuffs!")
                else:
                    self.st.add_buff(okey, op.get("stat", "attack"),
                                     -abs(int(op.get("amount", val))), int(op.get("turns", 2)))
                    logs.append(f"📉 **{ab_name}** — enemy {op.get('stat','attack')} "
                                f"-{abs(int(op.get('amount', val)))}!")
            elif kind == "enemy_lose_stability":
                # Stability damage dealt TO the opponent — the burst pressure a
                # blade applies rather than the wobble it suffers.
                #
                # `lose_stability` is self-inflicted by design (same-spin
                # matchups where the blade cannot bite and loses its own
                # footing), so there was no way to express "this hit knocks 5
                # stability off the enemy" at all. Routed through the same
                # StabilityManager._apply, so burst resistance, the ring-out
                # check and the log line all behave exactly as they do for any
                # other stability change.
                try:
                    stm = getattr(self.session, "stability_manager", None)
                    amt = int(op.get("amount", val))
                    if stm is not None and amt > 0:
                        logs.extend(stm._apply(okey, -amt) or [])
                except Exception:
                    pass
            elif kind == "enemy_debuff_pct":
                # Enemy loses X% of one stat, resolved against THEIR base stat.
                #
                # `enemy_debuff` is flat, which cannot express "−20% defence":
                # a flat 20 is a fifth of a 100-defence blade but a thirteenth
                # of a levelled 250-defence one, so the ability would quietly
                # weaken as the game went on. Mirrors buff_all_pct, on the
                # other side and with the sign flipped.
                if self.debuff_immune.get(okey):
                    logs.append(f"🛡️ **{ab_name}** — enemy is immune to debuffs!")
                else:
                    try:
                        pct = float(val)
                        pct = pct if pct <= 1 else pct / 100
                        stat = op.get("stat", "defense")
                        base = (self.session.blades.get(okey) or {}).get("stats") or {}
                        amt = int(round(float(base.get(stat, 0)) * pct))
                        if amt > 0:
                            self.st.add_buff(okey, stat, -amt,
                                             int(op.get("turns", 2)))
                            logs.append(f"📉 **{ab_name}** — enemy {stat} "
                                        f"-{amt} ({int(pct * 100)}%)!")
                    except Exception:
                        pass
            elif kind == "stacking_buff":
                cname = op.get("name", f"{ab_name}_stacks")
                mx    = int(op.get("max", 99))
                cur   = self.counters.get((key, cname), 0)
                if cur < mx:
                    stat = op.get("stat", "attack")
                    # A stack can be worth a PERCENTAGE of the blade's own base
                    # stat ("each Conquest Stack: +4% ATK"). int(0.04) is 0, so
                    # those blades stacked nothing at all.
                    pct  = op.get("per_stack_pct")
                    if pct is not None:
                        pct  = float(pct)
                        pct  = pct if pct <= 1 else pct / 100
                        base = (self.session.blades.get(key) or {}).get("stats") or {}
                        per  = int(round(float(base.get(stat, 0)) * pct))
                    else:
                        per = int(op.get("per_stack", val))
                    self.counters[(key, cname)] = cur + 1
                    if per:
                        self.st.add_buff(key, stat, per, 99)
                    logs.append(f"🔺 **{ab_name}** — stack {cur+1}/{mx} "
                                f"(+{per} {stat})!")
            elif kind == "dmg_amp":
                # `turns` makes the amp TEMPORARY. add_dmg_amp is a permanent
                # accumulator with no expiry, so "+25% for 5 turns" could only
                # be written as "+25% forever" — an Overdrive that never ends
                # is not an Overdrive. The expiry is tracked here and swept by
                # tick_dmg_amps() at end of round.
                amt = float(val)
                self.st.add_dmg_amp(key, amt)
                turns = int(op.get("turns", 0) or 0)
                if turns > 0:
                    self.timed_dmg_amps.append([key, amt, turns])
                    logs.append(f"🔥 **{ab_name}** — damage amplified "
                                f"{int(amt * 100)}% for {turns} turn(s)!")
                else:
                    logs.append(f"🔥 **{ab_name}** — damage amplified {int(amt*100)}%!")
            elif kind == "special_boost":
                self.special_boost_flat[key] = self.special_boost_flat.get(key, 0) + int(val)
                logs.append(f"✨ **{ab_name}** — Special +{int(val)} damage!")
            elif kind == "special_amp_stack":
                # A PERCENTAGE Special amp that accumulates across the battle.
                #
                # `special_boost` is flat and `bonus_damage_pct` is per-move, so
                # "the Special hits 50% harder every time it is used" had no
                # home: a flat bonus does not compound and a per-move one is
                # gone by the next Special. Stored on the engine and read by
                # attack_manager when it resolves a Special.
                try:
                    pct = float(val)
                    pct = pct if pct <= 1 else pct / 100
                except (TypeError, ValueError):
                    pct = 0.0
                cap = float(op.get("max", 4.0))          # +400% ceiling
                cur = self.special_amp_stack.get(key, 0.0)
                new = min(cap, cur + pct)
                if new > cur:
                    self.special_amp_stack[key] = new
                    logs.append(f"💀 **{ab_name}** — Special damage "
                                f"+{int(new * 100)}% (stacking)!")
            elif kind == "guaranteed_crit":
                self.guaranteed_crit_turns[key] = max(
                    self.guaranteed_crit_turns.get(key, 0), int(op.get("turns", val or 1)))
                logs.append(f"🎯 **{ab_name}** — guaranteed CRIT!")

            # ── control ──────────────────────────────────────────────────────
            elif kind == "silence":
                _resisted, _rl = _avatar_resist(self.session, okey, "silence")
                logs.extend(_rl)
                if not _resisted:
                    self.st.silence(okey, int(op.get("turns", val or 1)))
                    logs.append(f"🤐 **{ab_name}** — enemy silenced "
                                f"{int(op.get('turns', val or 1))} turn(s)!")
            elif kind == "ignore_defense":
                self.st.set_duration("ignore_defense_turns", key, int(op.get("turns", val or 1)))
                logs.append(f"🗡️ **{ab_name}** — attacks pierce defense!")
            elif kind == "undodgeable":
                # An attack that simply cannot be avoided. Dodge is the one
                # defensive layer that zeroes a hit outright, so an ability
                # that "cannot be interrupted" has to be able to say so —
                # ignore_defense only gets past mitigation, not past a dodge.
                turns = int(op.get("turns", val or 1))
                self.undodgeable_turns[key] = max(
                    self.undodgeable_turns.get(key, 0), turns)
                logs.append(f"🚫 **{ab_name}** — attacks cannot be dodged "
                            f"for {turns} turn(s)!")
            elif kind == "true_damage_turns":
                self.st.set_duration("true_damage_turns", key, int(op.get("turns", val or 1)))
                logs.append(f"💢 **{ab_name}** — attacks deal TRUE damage!")
            elif kind == "invulnerable":
                self.st.set_invulnerable(key, int(op.get("turns", val or 1)))
                logs.append(f"🌟 **{ab_name}** — INVULNERABLE "
                            f"{int(op.get('turns', val or 1))} turn(s)!")

            # ── DoT ──────────────────────────────────────────────────────────
            elif kind == "burn":
                _resisted, _rl = _avatar_resist(self.session, okey, "burn")
                logs.extend(_rl)
                if _resisted:
                    continue
                b_logs = self.st.apply_burn(okey, {
                    "name":                 ab_name,
                    "burn_damage_per_turn": int(op.get("dmg", val)),
                    "burn_duration":        int(op.get("turns", 2)),
                    "max_burn_stacks":      int(op.get("max_stacks", 3)),
                })
                logs.extend(b_logs)

            # ── resources ────────────────────────────────────────────────────
            elif kind == "drain_stamina":
                try:
                    sm   = self.session.stamina_manager
                    # Target's stamina-drain resistance softens the steal.
                    red  = getattr(sm, "drain_reduction", {}).get(okey, 0.0)
                    want = round(float(val) * (1.0 - min(0.9, max(0.0, red))), 2)
                    take = round(min(want, sm.stamina.get(okey, 0)), 2)
                    cap  = getattr(sm, "max_stamina", {}).get(key, 15) or 15
                    sm.stamina[okey] = round(max(0.0, sm.stamina.get(okey, 0) - take), 2)
                    # `steal: false` — the enemy loses it and nobody gains it.
                    # A counter that knocks stamina loose is not the same
                    # ability as one that feeds on it, and handing the drained
                    # points to a low-stamina blade is a quiet buff nobody
                    # asked for.
                    if op.get("steal", True):
                        sm.stamina[key] = round(
                            min(float(cap), sm.stamina.get(key, 0) + take), 2)
                    if take > 0:
                        logs.append(f"🌀 **{ab_name}** — drained {take:g} stamina!")
                        hpd = self.heal_per_drain.get(key, 0)
                        if hpd > 0:
                            heal   = math.ceil(take * hpd)
                            mx_hp  = self.session.max_hp_per_player.get(
                                key, getattr(self.session, "max_hp", 600))
                            before = self.session.hp.get(key, 0)
                            self.session.hp[key] = min(mx_hp, before + heal)
                            gained = self.session.hp[key] - before
                            if gained > 0:
                                logs.append(f"☀️ **Solar Siphon** — absorbed "
                                            f"**+{gained} HP** from the drain!")
                except Exception:
                    pass
            elif kind == "stamina_cost_reduction":
                # Aerodynamic / low-friction blades: every stamina cost they pay
                # and every drain they suffer is cut by <value> (0–1 fraction).
                try:
                    pct = float(val)
                    pct = pct if pct <= 1 else pct / 100
                    pct = min(0.9, max(0.0, pct))
                    sm  = self.session.stamina_manager
                    if not hasattr(sm, "drain_reduction"):
                        sm.drain_reduction = {}
                    sm.drain_reduction[key] = max(sm.drain_reduction.get(key, 0.0), pct)
                    if pct > 0:
                        logs.append(f"🍃 **{ab_name}** — stamina loss reduced "
                                    f"by {int(pct * 100)}%!")
                except Exception:
                    pass
            elif kind == "stamina_cost_increase":
                # The drawback half of a power ability: this blade's OWN moves
                # cost more stamina. `stamina_cost_reduction` clamps to 0–0.9
                # and cannot express this — a negative value there reads as
                # zero and the drawback silently does not exist.
                #
                # `moves` names which actions pay the surcharge (default
                # attack + special). The Stamina recovery move costs nothing
                # to begin with, so it is never affected either way.
                try:
                    sm = self.session.stamina_manager
                    for _attr in ("cost_increase", "cost_increase_flat",
                                  "cost_increase_moves"):
                        if not hasattr(sm, _attr):
                            setattr(sm, _attr, {})
                    moves = op.get("moves")
                    if moves:
                        sm.cost_increase_moves[key] = tuple(
                            str(m).lower() for m in moves)
                    elif key not in sm.cost_increase_moves:
                        sm.cost_increase_moves[key] = ("attack", "special")

                    # `flat_per_stack` is the STACKING form: each firing adds
                    # another flat point of cost, up to `max` stacks. It keeps
                    # its own counter rather than reading the one a
                    # `stacking_buff` in the same rule keeps, so neither op can
                    # be reordered, renamed or removed without the other still
                    # being correct on its own.
                    per = op.get("flat_per_stack")
                    if per is not None:
                        cname = op.get("name", f"{ab_name}_cost")
                        mx    = int(op.get("max", 99))
                        cur   = self.counters.get((key, cname), 0)
                        if cur < mx:
                            self.counters[(key, cname)] = cur + 1
                            sm.cost_increase_flat[key] = round(
                                (cur + 1) * float(per), 2)
                            logs.append(
                                f"🩸 **{ab_name}** — stack {cur+1}/{mx}: "
                                f"attacks now cost "
                                f"+{sm.cost_increase_flat[key]:g} stamina!")
                    else:
                        pct = float(val)
                        pct = pct if pct <= 1 else pct / 100
                        pct = min(3.0, max(0.0, pct))
                        sm.cost_increase[key] = max(
                            sm.cost_increase.get(key, 0.0), pct)
                        if pct > 0:
                            logs.append(f"🩸 **{ab_name}** — attacks cost "
                                        f"{int(pct * 100)}% more stamina!")
                except Exception:
                    pass
            elif kind == "bonus_damage_stat":
                # Damage equal to a share of one of the MOVER'S OWN stats.
                #
                # Nothing else could express "this Special also deals your full
                # Attack": `bonus_damage` is a printed constant that stops
                # meaning anything by level 100, `bonus_damage_pct` scales off
                # the damage already dealt, and `damage_boost` scales off an
                # hp/stamina ratio. This reads the effective stat — levels,
                # parts and avatar folded in — from session.battle_stats.
                #
                # Deliberately NOT the buffed stat. Temporary buffs already
                # raise ordinary damage, so counting them here would pay the
                # same stacks out twice on one move.
                try:
                    stat  = op.get("stat", "attack")
                    scale = float(op.get("scale", val if val is not None else 1.0))
                    scale = scale if scale <= 10 else scale / 100
                    eff   = (getattr(self.session, "battle_stats", {}) or {}).get(key)
                    base  = (eff or {}).get(stat)
                    if base is None:
                        base = ((self.session.blades.get(key) or {})
                                .get("stats") or {}).get(stat, 0)
                    add = int(round(float(base) * scale))
                    if add > 0:
                        dmg_dealt += add
                        logs.append(f"🩸 **{ab_name}** — +{add} damage from "
                                    f"its full {stat.title()}!")
                except Exception:
                    pass
            elif kind == "heal_per_drain":
                # Register: every stamina point drained by this player also
                # heals them <val> HP (applied inside drain_stamina).
                self.heal_per_drain[key] = int(val)
                logs.append(f"☀️ **{ab_name}** — drained stamina now restores "
                            f"{int(val)} HP per point!")
            elif kind == "gain_stamina":
                try:
                    sm  = self.session.stamina_manager
                    cap = getattr(sm, "max_stamina", {}).get(key, 15) or 15
                    cur = sm.stamina.get(key, 0)
                    # float, not int — the stamina scale is 0–15, so a 0.5
                    # gain is meaningful and int() silently zeroed it.
                    amt = float(val)
                    new = max(0.0, min(float(cap), cur + amt))
                    gained = round(new - cur, 2)
                    sm.stamina[key] = round(new, 2)
                    if amt >= 0:
                        if gained > 0:
                            logs.append(f"🔋 **{ab_name}** — +{gained:g} stamina!")
                    else:
                        logs.append(f"🪫 **{ab_name}** — {amt:g} stamina!")
                except Exception:
                    pass
            elif kind == "steal_hp":
                take = min(int(val), max(0, self.session.hp.get(okey, 0)))
                self.session.hp[okey] = self.session.hp.get(okey, 0) - take
                self._heal(key, take, logs, ab_name)

            # ── counters / modes / chains ────────────────────────────────────
            elif kind in ("add_counter", "stack_gain"):
                cname = op.get("name", "stacks")
                mx    = int(op.get("max", 999))
                self.counters[(key, cname)] = min(mx, self.counters.get((key, cname), 0)
                                                  + int(op.get("amount", 1)))
            elif kind in ("consume_counter", "stack_consume"):
                cname = op.get("name", "stacks")
                need  = op.get("amount")          # None = consume all
                cur   = self.counters.get((key, cname), 0)
                take  = cur if need is None else min(cur, int(need))
                self.counters[(key, cname)] = cur - take
                per   = int(op.get("damage_per_stack", 0))
                if per and take:
                    dmg_dealt += per * take
                    logs.append(f"💠 **{ab_name}** — consumed {take} stacks "
                                f"(+{per*take} damage)!")
            elif kind == "reset_counter":
                self.counters[(key, op.get("name", "stacks"))] = 0
            elif kind == "status_apply":            # doc alias: routes to burn/buff
                stype = op.get("status", "burn")
                if stype == "burn":
                    logs.extend(self.st.apply_burn(okey, {
                        "name": ab_name,
                        "burn_damage_per_turn": int(op.get("dmg", val)),
                        "burn_duration": int(op.get("turns", 2)),
                        "max_burn_stacks": int(op.get("max_stacks", 3)),
                    }))
                elif not self.debuff_immune.get(okey):
                    self.st.add_buff(okey, stype, -abs(int(op.get("amount", val))),
                                     int(op.get("turns", 2)))
            elif kind == "set_mode":
                self.modes[key] = str(op.get("name", val))
                logs.append(f"🔁 **{ab_name}** — switched to {self.modes[key]}!")
            elif kind == "queue_chain":
                self.session.chain_handler.queue(key, op.get("steps", []))
            elif kind == "disable_ability_2":
                self.ability_2_disabled[okey] = True
                logs.append(f"🚫 **{ab_name}** — enemy's 2nd ability disabled!")
            elif kind == "log":
                logs.append(str(op.get("text", "")))
            # unknown op: skip silently (forward-compat for new ops)

        return dmg_dealt, dmg_taken

    # =========================================================================
    #  Trigger dispatch
    # =========================================================================

    def _fire(self, when: str, key: str, okey: str, blade: dict, move: str,
              matchup: str, dmg_dealt: int, dmg_taken: int,
              logs: list[str]) -> tuple[int, int]:
        for rid, rule in self._rules_for(blade, key):
            if self._rule_fires(rid, rule, when, key, okey, move, matchup):
                ab_name = rule.get("_name", blade.get("name", "Ability"))
                dmg_dealt, dmg_taken = self._run_ops(
                    rule, ab_name, key, okey, move, dmg_dealt, dmg_taken, logs)
                # legacy chain passthrough
                if rule.get("_chain"):
                    self.session.chain_handler.queue(key, rule["_chain"])
        return dmg_dealt, dmg_taken

    # =========================================================================
    #  Public entry points (same contract as the old engine)
    # =========================================================================

    def setup(self, key: str, blade: dict) -> list[str]:
        """Battle-start: compile rules and fire the 'setup' trigger."""
        logs: list[str] = []
        for rid, rule in self._rules_for(blade, key):
            if rule.get("when") == "setup" and self._rule_fires(
                    rid, rule, "setup", key, self._other_key(key), "", ""):
                _, _ = self._run_ops(rule, rule.get("_name", "Ability"),
                                     key, self._other_key(key), "", 0, 0, logs)
        return logs

    def apply(
        self,
        mover_key:   str,
        other_key:   str,
        mover_blade: dict,
        other_blade: dict,
        move:        str,
        matchup:     str,
        dmg_dealt:   int,
        dmg_taken:   int,
        is_first_hit: bool = True,
        cumulative_dmg: int = 0,
        is_last_hit: bool = True,
    ) -> tuple[int, int, list[str]]:
        """Route one move through the full generic trigger pipeline."""
        logs: list[str] = []

        # Steps 1–4: buffs tick, ATK buffs & amp, invuln, shields (unchanged)
        dmg_dealt, dmg_taken, f_logs, mover_silenced = self.damage_filter.run(
            mover_key, other_key, mover_blade, other_blade,
            move, dmg_dealt, dmg_taken, is_first_hit=is_first_hit,
        )
        logs.extend(f_logs)

        evaded = dmg_dealt == 0 and any("EVADED" in l or "vanished" in l for l in f_logs)

        if not mover_silenced and not evaded:
            # Mover offensive triggers
            dmg_dealt, dmg_taken = self._fire("passive", mover_key, other_key,
                                              mover_blade, move, matchup,
                                              dmg_dealt, dmg_taken, logs)
            # threshold triggers (fire while condition holds)
            for thr_trg in ("on_low_hp", "on_high_hp",
                            "on_low_stamina", "on_high_stamina"):
                dmg_dealt, dmg_taken = self._fire(thr_trg, mover_key, other_key,
                                                  mover_blade, move, matchup,
                                                  dmg_dealt, dmg_taken, logs)
            # full move × result matrix: on_attack_win / on_defense_loss / …
            mname = _MOVE_NAME.get(move)
            # `lose_grind` is what calc_damage returns when Defense loses to
            # Stamina — it is a loss. It was missing from this map, so `res`
            # came back None and BOTH on_defense_loss and on_any_loss were
            # skipped for that matchup. Across a 600-battle fuzz run,
            # on_defense_loss fired exactly zero times.
            res   = {"win": "win", "lose": "loss", "lose_grind": "loss",
                     "mirror": "mirror"}.get(matchup)
            if mname and res:
                dmg_dealt, dmg_taken = self._fire(f"on_{mname}_{res}", mover_key,
                                                  other_key, mover_blade, move,
                                                  matchup, dmg_dealt, dmg_taken, logs)
            if matchup == "win":
                dmg_dealt, dmg_taken = self._fire("on_any_win", mover_key,
                                                  other_key, mover_blade, move,
                                                  matchup, dmg_dealt, dmg_taken, logs)
            elif matchup in ("lose", "lose_grind"):
                dmg_dealt, dmg_taken = self._fire("on_any_loss", mover_key,
                                                  other_key, mover_blade, move,
                                                  matchup, dmg_dealt, dmg_taken, logs)
            if move == MOVE_ATTACK and is_first_hit:
                # Deliberately ONLY on_attack_hit here. `on_hit` is the
                # per-Special-hit trigger and stays confined to
                # process_hit_proc — keeping them separate is what stops a
                # multi-hit Special from applying an attack-side effect once
                # per hit.
                dmg_dealt, dmg_taken = self._fire("on_attack_hit", mover_key,
                                                  other_key, mover_blade, move,
                                                  matchup, dmg_dealt, dmg_taken, logs)
            if move == MOVE_SPECIAL and is_first_hit:
                dmg_dealt, dmg_taken = self._fire("on_special", mover_key,
                                                  other_key, mover_blade, move,
                                                  matchup, dmg_dealt, dmg_taken, logs)
                # consume any primed one-shot bonus
                primed = self.primed_bonus.pop(mover_key, 0)
                if primed:
                    dmg_dealt += primed
                    logs.append(f"🔆 Primed energy released — +{primed} damage!")
            if matchup == "mirror":
                dmg_dealt, dmg_taken = self._fire("on_mirror", mover_key,
                                                  other_key, mover_blade, move,
                                                  matchup, dmg_dealt, dmg_taken, logs)

            # engine-side ability crit (crit_chance / crit_damage ops)
            p = self.crit_chance_bonus.get(mover_key, 0.0)
            if p > 0 and dmg_dealt > 0 and matchup == "win" and is_first_hit \
                    and random.random() < min(0.95, p):
                mult = self.crit_damage_mult.get(mover_key, 1.5)
                dmg_dealt = math.ceil(dmg_dealt * mult)
                logs.append(f"🎯 **CRITICAL!** — damage ×{mult:g}!")

        # Defender reactive triggers (blocked only by evasion, not mover silence)
        if not evaded and dmg_dealt > 0:
            dmg_dealt, dmg_taken = self._fire_defensive(
                other_key, mover_key, other_blade, move, matchup,
                dmg_dealt, dmg_taken, logs)

        # Lifesteal (generic, set by ops)
        ls = self.lifesteal_pct.get(mover_key, 0.0)
        if ls and dmg_dealt > 0:
            self._heal(mover_key, math.ceil(dmg_dealt * ls / 100), logs, "Lifesteal")

        # Chain resolution once per move
        if is_first_hit and not mover_silenced:
            logs.extend(self.session.chain_handler.resolve(
                mover_key, mover_blade, other_key, dmg_dealt))

        # Revival check — once, after the full move is projected
        if is_last_hit:
            logs.extend(self._check_revive(other_key, other_blade,
                                           dmg_dealt + cumulative_dmg))

        return dmg_dealt, dmg_taken, logs

    def _fire_defensive(self, dkey: str, akey: str, dblade: dict, move: str,
                        matchup: str, dmg_dealt: int, dmg_taken: int,
                        logs: list[str]) -> tuple[int, int]:
        """Defender-phase rules: on_defend (any attempt) + on_take_damage."""
        dmg_dealt, dmg_taken = self._fire("on_defend", dkey, akey, dblade,
                                          move, matchup, dmg_dealt, dmg_taken, logs)

        # Windowed reflect, opened by the `reflect_pct_turns` op. The plain
        # `reflect_pct` op can only fire from a rule that is already running,
        # which cannot express "whoever hits me over the next N turns eats a
        # counter" — the rule would have to know in advance that it was going
        # to be attacked. Applied here, on the real incoming hit, so the
        # counter is a percentage of what actually landed.
        win = (getattr(self, "reflect_windows", None) or {}).get(dkey)
        if win and dmg_dealt > 0 and win[1] > 0:
            try:
                ref = math.ceil(dmg_dealt * float(win[0]) / 100)
            except (TypeError, ValueError):              # noqa: BLE001
                ref = 0
            if ref > 0:
                dmg_taken += ref
                logs.append(f"  🪞 **Counter Stance** — {int(win[0])}% of that "
                            f"hit comes straight back ({ref})!")

        if dmg_dealt > 0:
            dmg_dealt, dmg_taken = self._fire("on_take_damage", dkey, akey, dblade,
                                              move, matchup, dmg_dealt, dmg_taken, logs)
        return dmg_dealt, dmg_taken

    def _check_revive(self, key: str, blade: dict, incoming: int) -> list[str]:
        logs: list[str] = []
        if incoming <= 0:
            return logs
        # Revival is armed by the 'revive' op; fires when HP would hit 0.
        if self.session.hp.get(key, 0) - incoming <= 0 and not self.st.revival_used.get(key):
            hp = self.revive_pool.get(key, 0)
            if hp > 0:
                self.st.revival_used[key] = True
                # counteract the lethal blow: restore to `hp` after damage lands
                self.session.hp[key] = incoming + hp
                logs.append(f"⚡ **{blade.get('name','?')}** REFUSES to fall — revived with {hp} HP!")
        return logs

    def tick_dmg_amps(self) -> list[str]:
        """Expire timed dmg_amp grants and undodgeable windows.

        Called once per round by the session.
        """
        logs: list[str] = []
        # getattr rather than direct access: this is called every round from
        # the session, and an engine built by an older path (or a harness) must
        # not take a whole battle down over a missing bookkeeping dict.
        undodgeable = getattr(self, "undodgeable_turns", None)
        if undodgeable:
            for key, turns in list(undodgeable.items()):
                left = int(turns) - 1
                if left > 0:
                    undodgeable[key] = left
                else:
                    undodgeable.pop(key, None)
        windows = getattr(self, "reflect_windows", None)
        if windows:
            for key, entry in list(windows.items()):
                try:
                    entry[1] = int(entry[1]) - 1
                except (TypeError, ValueError, IndexError):   # noqa: BLE001
                    windows.pop(key, None)
                    continue
                if entry[1] <= 0:
                    windows.pop(key, None)
                    logs.append("  ⏳ The counter stance drops.")

        # `ability_amp` windows expire here too — one sweep, one place, so a
        # new timed mechanism cannot be added without an expiry by accident.
        amps = getattr(self, "ability_amp", None)
        if amps:
            for key, entry in list(amps.items()):
                try:
                    entry[1] = int(entry[1]) - 1
                except (TypeError, ValueError, IndexError):   # noqa: BLE001
                    amps.pop(key, None)
                    continue
                if entry[1] <= 0:
                    amps.pop(key, None)
                    logs.append("  ⏳ The amplified kit settles back down.")
        if not getattr(self, "timed_dmg_amps", None):
            return logs
        still: list[list] = []
        for entry in self.timed_dmg_amps:
            entry[2] -= 1
            if entry[2] > 0:
                still.append(entry)
            else:
                # Subtract exactly what was granted. add_dmg_amp is a running
                # total shared with permanent grants, so it must never be
                # zeroed wholesale — a blade with both would lose the permanent
                # one too.
                self.st.dmg_amp_stacks[entry[0]] = max(
                    0.0, self.st.dmg_amp_stacks.get(entry[0], 0.0) - entry[1])
                logs.append(f"  ⏳ Overdrive fades — damage amp "
                            f"-{int(entry[1] * 100)}%.")
        self.timed_dmg_amps = still
        return logs

    # ── Per-hit proc (multi-hit specials / attack) ────────────────────────────
    def process_hit_proc(self, key: str, blade: dict, okey: str,
                         hit_dmg: int) -> tuple[int, list[str]]:
        logs: list[str] = []
        # Only `on_hit` — `on_attack_hit` is the normal-attack trigger and must
        # not fire here, or an attack-side effect would apply once per hit of a
        # multi-hit Special.
        hit_dmg, _ = self._fire("on_hit", key, okey, blade, MOVE_SPECIAL, "win",
                                hit_dmg, 0, logs)
        return hit_dmg, logs

    # ── Generic primed one-shot bonus (old solar-flare contract) ─────────────
    def consume_solar_flare(self, key: str, blade: dict) -> tuple[int, list[str]]:
        bonus = self.primed_bonus.pop(key, 0)
        if bonus:
            return bonus, [f"🔆 Primed energy released — +{bonus} damage!"]
        return 0, []

    # ── Per-round ticks (called by session) ──────────────────────────────────
    def apply_stamina_regen(self, key: str) -> list[str]:
        logs: list[str] = []
        amt = self.regen_per_turn.get(key, 0)
        if amt:
            try:
                sm  = self.session.stamina_manager
                cap = getattr(sm, "max_stamina", {}).get(key, 15) or 15
                sm.stamina[key] = round(min(float(cap),
                                            sm.stamina.get(key, 0) + amt), 2)
                logs.append(f"🔋 Regenerated {amt:g} stamina.")
            except Exception:
                pass
        return logs

    def apply_dot_tick_extras(self, key: str, blade: dict) -> list[str]:
        logs: list[str] = []
        # generic HP regen op + turn_start rules
        amt = self.hp_regen_per_turn.get(key, 0)
        if amt:
            self._heal(key, amt, logs, blade.get("name", "Regen"))
        _, _ = self._fire("turn_start", key, self._other_key(key), blade,
                          "", "", 0, 0, logs)
        _, _ = self._fire("turn_end", key, self._other_key(key), blade,
                          "", "", 0, 0, logs)
        return logs

    # ── Pre-battle choice stubs (mode blades declare via rules/UI later) ─────
    def needs_pre_battle_choice(self, key: str) -> bool:
        return False

    # ── Compat helpers used by defense_manager ───────────────────────────────
    def _get_buf_bonus(self, key: str, stat: str) -> int:
        """Current buff bonus for a stat (delegates to StatusManager)."""
        return self.st.get_buff_bonus(key, stat)

    def get_shatter_defense_reduction(self, key: str, ab: dict) -> int:
        """Defense reduction from accumulated shatter stacks (legacy field
        shape ``ab["shatter"]`` still honoured; stacks live in StatusManager)."""
        stacks = self.shatter_stacks.get(key, 0)
        if stacks <= 0:
            return 0
        shat = ab.get("shatter", {}) if isinstance(ab, dict) else {}
        per  = shat.get("defense_reduction_per_stack", 5)
        cap  = shat.get("max_defense_reduction", 50)
        return min(stacks * per, cap)

    # ── Passive properties read straight off the blade ────────────────────────
    #
    # These two are read from the ability list at the moment they matter rather
    # than granted by a trigger, for the same reason `get_shatter_defense_
    # reduction` is: they describe what the blade IS in this form, not
    # something that happens to it. Routing them through a trigger would make
    # them depend on whether the owner's ability fired before or after the
    # damage it is supposed to modify — an ordering question with no good
    # answer inside a single round.

    def _passive_pct(self, key: str, blade: dict, want_op: str) -> float:
        """Sum the values of every `want_op` on this blade, amp included."""
        total = 0.0
        for _rid, rule in self._compiled_for(blade):
            for op in rule.get("do") or []:
                if op.get("op") != want_op:
                    continue
                try:
                    total += float(self._amped(key, op, op.get("value", 0)))
                except (TypeError, ValueError):          # noqa: BLE001
                    continue
        return total

    def pierce_pct(self, key: str, blade: dict) -> float:
        """How much of the defender's DEF this blade ignores, 0–100.

        Deliberately separate from `ignore_defense`, which zeroes DEF outright
        AND nullifies the opponent's counter. Partial pierce does neither of
        those things — it shaves the stat and leaves the counter alone, so a
        50% pierce is half of a full pierce rather than most of one.
        """
        return max(0.0, min(100.0, self._passive_pct(key, blade,
                                                     "ignore_defense_pct")))

    def counter_damage_pct(self, key: str, blade: dict) -> float:
        """Extra percent this blade adds to a counter it lands."""
        return max(0.0, self._passive_pct(key, blade, "counter_damage_pct"))

    def _compiled_for(self, blade: dict) -> list:
        """Compiled rules for a blade, tolerating anything unexpected."""
        try:
            return self._rules_for(blade or {}) or []
        except Exception:                                # noqa: BLE001
            return []

    def resolve_pre_battle_choice(self, key: str, mode: str) -> list[str]:
        self.modes[key] = mode
        return [f"🔁 Mode set: {mode}"]

    # ── misc ──────────────────────────────────────────────────────────────────
    def _other_key(self, key: str) -> str:
        for k in self.session.hp:
            if k != key:
                return k
        return key
