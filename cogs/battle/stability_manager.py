"""
battle/stability_manager.py
---------------------------
Dedicated Stability Manager.

Every Bey has a Stability meter.  If it reaches zero the Bey is
instantly ring-out — regardless of remaining HP.

Parallels
---------
  StaminaManager  — stamina costs, regen, Special Gauge
  DefenseManager  — shield gate, counter-hit, grind debuff
  AttackManager   — attack-resolution pipeline
  StabilityManager — this file

No damage formulas, ability primitives, or Discord calls live here.
Type-advantage gating is delegated to type_system.py via
``stability_effects_active()``.

Starting Stability
------------------
  Attack  = 100
  Stamina = 100
  Balance = 100
  Defense = 150

Stability Changes Per Action
-----------------------------
  Attack hits          attacker  -10
  Attack misses (0 dmg) attacker  -4
  Defense (passive)    defender   -6
  Defense vs Stamina   defender    0   (no effect — type gate)
  Defense vs Attack    defender   -6   (when hit)
  Defense vs Defense   defender   -3   (mirror)
  Defense blocked hit  defender   +5   (no damage taken)
  Counter (you counter) enemy     -5
  Attack vs Attack clash both    -10  (TOTAL — replaces the attack-hit cost)
  Stamina move         self      +25 (halved if attacked that round)
  Special move         self        0  (specials never cost stability)

Type-Advantage Gate
-------------------
Stability deltas only apply when you hold the type advantage. This module
does not decide that — `type_system.resolve_active_bonuses` does, and this
one asks it. It used to keep its own table, which disagreed.

  Attack type  : active vs Stamina
  Stamina type : active vs Defense
  Defense type : active vs Attack
  Balance type : always active, and never suppresses the opponent
  mirrors      : neither side

Public API
----------
  StabilityManager(blades, type_mods)
      type_mods is the dict[str, TypeModifiers] already built in session.py.
      Starting stability is read from TypeModifiers.stability_start so
      Defense type (150) vs all others (100) is owned by type_system.py.

  .apply_attack_cost(key, hit: bool) -> list[str]
      Attacker stability after an attack move.
      hit=True  → -10 (dealt damage)
      hit=False → -4  (zero damage)

  .apply_defense_cost(key, context: str) -> list[str]
      Defender stability.
      context one of:
        "passive"        → -6 (default passive defence tick)
        "vs_stamina"     →  0
        "vs_attack"      → -6 (hit by attack)
        "vs_defense"     → -3 (mirror)
        "blocked"        → +5 (no damage taken)

  .apply_stamina_recovery(key) -> list[str]
      Stability for using the Stamina action → +25 (halved if attacked).

  .apply_counter_penalty(enemy_key) -> list[str]
      Stability penalty to the player who was countered → -5.

  .apply_clash_penalty(key_a, key_b) -> list[str]
      Attack vs Attack clash → -10 to both.

  .is_effects_active(your_type: str, enemy_type: str) -> bool
      Returns True when stability effects should apply for this matchup.
      Delegates to type_system.stability_effects_active().

  .check_ring_out(key) -> bool
      Returns True if stability[key] <= 0 (ring-out condition met).
"""

from __future__ import annotations

import math

from typing import TYPE_CHECKING, Optional

from cogs.abilities.type_system import resolve_active_bonuses

from . import button_profile

from .constants import (
    STABILITY_START_DEFAULT,
    STABILITY_ATTACK_HIT,
    STABILITY_ATTACK_MISS,
    STABILITY_COUNTER_PENALTY,
    STABILITY_CLASH_PENALTY,
    STABILITY_DEF_PASSIVE,
    STABILITY_DEF_VS_ATTACK,
    STABILITY_DEF_VS_DEFENSE,
    STABILITY_DEF_VS_STAMINA,
    STABILITY_DEF_BLOCKED,
    STABILITY_STAMINA_RECOVERY,
)

if TYPE_CHECKING:
    pass  # no circular imports needed — manager is self-contained


STABILITY_DEFAULT = STABILITY_START_DEFAULT  # fallback if type_mods entry is missing

# ── Per-context delta table ────────────────────────────────────────────────────

_ATTACK_HIT_COST   = STABILITY_ATTACK_HIT
_ATTACK_MISS_COST  = STABILITY_ATTACK_MISS

_DEFENSE_COSTS: dict[str, int] = {
    "passive":    STABILITY_DEF_PASSIVE,
    "vs_stamina": STABILITY_DEF_VS_STAMINA,
    "vs_attack":  STABILITY_DEF_VS_ATTACK,
    "vs_defense": STABILITY_DEF_VS_DEFENSE,
    "blocked":    STABILITY_DEF_BLOCKED,
}

_STAMINA_RECOVERY  = STABILITY_STAMINA_RECOVERY
_COUNTER_PENALTY   = STABILITY_COUNTER_PENALTY
_CLASH_PENALTY     = STABILITY_CLASH_PENALTY

def stability_effects_active(your_type: str, enemy_type: str) -> bool:
    """Return True when stability effects should apply for this matchup.

    THE BUG THIS REPLACES: this used to consult its own table,
    `_ACTIVE_MATCHUPS`, which disagreed with `type_system.ADVANTAGE` — the
    chart the rest of the game runs on — in three separate ways. Attack's
    effects were switched OFF against Stamina, the exact matchup Attack is
    supposed to dominate. Defence was active against everything, including
    Attack, which beats it. And every mirror was active here while
    `resolve_active_bonuses` returns (False, False) for mirrors, so the same
    fight had a type bonus suppressed and its stability effects live.

    Two charts is one chart too many. There is now one, in type_system, and
    this asks it.
    """
    active, _ = resolve_active_bonuses(your_type, enemy_type)
    return active


class StabilityManager:
    """Owns all stability tracking and ring-out detection for one BattleSession.

    Constructed once and stored as ``session.stability_manager``.
    All methods are synchronous.
    """

    def __init__(self, blades: dict[str, dict], type_mods: dict,
                 avatar_bonuses: Optional[dict] = None) -> None:
        self._blades = blades
        self.stability: dict[str, int] = {}
        # Ability-driven per-move stability surcharges. AbilityEngine owns the
        # stack counters; this manager owns the resolved cost because it is the
        # only authority that mutates the stability meter.
        self.cost_increase_flat: dict[str, float] = {}
        self.cost_increase_moves: dict[str, tuple[str, ...]] = {}
        # Starting stability doubles as the CEILING: recovery (stamina +25,
        # blocked +5, ability ops) can refill the meter but never overfill it.
        # Without this cap, stamina spam pushed stability to 200/100, made
        # ring-out unreachable and overflowed the panel's emoji bar.
        self.max: dict[str, int] = {}
        for key in blades:
            mod = type_mods.get(key)
            start = mod.stability_start if mod is not None else STABILITY_DEFAULT
            # The avatar's stability line, folded in here.
            #
            # `AvatarBonuses.stability_flat` / `stability_percent` and
            # `apply_stability_bonus` were written when the avatar system was
            # built and had NO CALLERS ANYWHERE — every card advertising a
            # stability bonus in the shop was advertising nothing, because the
            # only bar in the game was built from the type table alone. This
            # is the seam it should always have gone through: it raises the
            # starting value AND the ceiling together, which is what "more
            # stability" means when the two are the same number.
            av = (avatar_bonuses or {}).get(key)
            if av is not None:
                try:
                    start = int(round(av.apply_stability_bonus(float(start))))
                except Exception:                        # noqa: BLE001
                    pass
            self.stability[key] = start
            self.max[key] = start

    # =========================================================================
    #  Attack
    # =========================================================================

    def apply_attack_cost(self, key: str, hit: bool) -> list[str]:
        """Apply attacker stability cost after an attack move.

        hit=True  → dealt damage    → -10
        hit=False → zero damage     → -4
        """
        delta = _ATTACK_HIT_COST if hit else _ATTACK_MISS_COST
        return self._action_cost(key, delta, "attack")

    # =========================================================================
    #  Defense
    # =========================================================================

    def apply_defense_cost(self, key: str, context: str) -> list[str]:
        """Apply defender stability based on what just happened.

        context must be one of:
          "passive"    — passive defense tick (default)
          "vs_stamina" — no effect
          "vs_attack"  — hit by attack
          "vs_defense" — mirror matchup
          "blocked"    — successfully blocked all damage → +5
        """
        delta = _DEFENSE_COSTS.get(context, _DEFENSE_COSTS["passive"])
        if delta == 0:
            return []
        return self._action_cost(key, delta, "defense")

    # =========================================================================
    #  Stamina recovery
    # =========================================================================

    def apply_stamina_recovery(self, key: str, reduced: bool = False) -> list[str]:
        """Apply stability recovery from using the Stamina action → +25.

        ``reduced`` — True when the opponent attacked during the Stamina move;
        the recovery is halved (interrupted heal), matching the HP heal cut.
        """
        amount = _STAMINA_RECOVERY // 2 if reduced else _STAMINA_RECOVERY
        return self._apply(key, amount)

    # =========================================================================
    #  Counter penalty
    # =========================================================================

    def apply_counter_penalty(self, enemy_key: str) -> list[str]:
        """Apply stability penalty to the player who was countered → -5."""
        return self._apply(enemy_key, _COUNTER_PENALTY)

    # =========================================================================
    #  Clash penalty
    # =========================================================================

    def apply_clash_penalty(self, key_a: str, key_b: str) -> list[str]:
        """Apply Attack vs Attack clash penalty → -10 to both."""
        logs = self._action_cost(key_a, _CLASH_PENALTY, "attack")
        logs += self._action_cost(key_b, _CLASH_PENALTY, "attack")
        return logs

    # =========================================================================
    #  Type-advantage gate
    # =========================================================================

    def is_effects_active(self, your_key: str, enemy_key: str) -> bool:
        """Return True when stability effects should apply for this matchup.

        Looks up the Bey types from the internal blades dict and delegates to
        the module-level ``stability_effects_active()`` helper.
        """
        your_type  = str(self._blades.get(your_key,  {}).get("type", "")).lower()
        enemy_type = str(self._blades.get(enemy_key, {}).get("type", "")).lower()
        return stability_effects_active(your_type, enemy_type)

    # =========================================================================
    #  Ring-out check
    # =========================================================================

    def check_ring_out(self, key: str) -> bool:
        """Return True if this Bey's stability has reached zero (ring-out)."""
        return self.stability.get(key, 1) <= 0

    def pct(self, key: str) -> float:
        """Stability as a fraction of this blade's own bar.

        Starting stability doubles as the ceiling and differs by type (150 for
        Defense, 100 for everyone else), so an absolute number means two
        different things depending on who holds it — only the fraction is
        comparable. AbilityEngine._stability_pct now delegates here so the
        engine's conditions and the damage gradient read one definition.
        """
        try:
            mx = self.max.get(key) or STABILITY_DEFAULT
            return self.stability.get(key, 0) / mx
        except (TypeError, ZeroDivisionError):
            return 1.0

    def strain(self, key: str, blade: Optional[dict] = None) -> dict:
        """The active stability tier's effects, or {} when steady.

        Stability used to be a pure cliff: nothing at all happened as it fell,
        and then at exactly 0 the blade was instantly out. 99/100 and 1/100
        played identically, which made the whole bar invisible until the
        moment it ended the fight. Tiers give the descent itself meaning — and
        make the Stamina button's +25 a real defensive play rather than a heal
        with a number attached.

        Empty unless tiers are authored (`button_profile.stability.tiers`) or
        enabled globally (`STABILITY_TIERS`), so by default this changes
        nothing for anybody.
        """
        tiers = button_profile.stability_tiers(
            blade if blade is not None else self._blades.get(key))
        if not tiers:
            return {}
        frac = self.pct(key)
        # Walk LOW threshold first and take the first match, so the harshest
        # applicable band wins. Walking high-first returns the softest tier
        # the blade qualifies for — at 15% with tiers at 30% and 15% that is
        # the 30% band, which would make every lower tier unreachable.
        for threshold, effects in reversed(tiers):   # tiers are sorted high→low
            if frac <= threshold:
                return effects
        return {}

    def move_cost_increase(self, key: str, move: str) -> int:
        """Snapshot the extra Stability cost owed by key for move."""
        try:
            amount = float(self.cost_increase_flat.get(key, 0.0) or 0.0)
            allowed = self.cost_increase_moves.get(
                key, ("attack", "defense", "charge", "special"))
            if amount <= 0 or str(move).lower() not in allowed:
                return 0
            return max(0, int(round(amount)))
        except Exception:
            return 0

    def apply_move_cost_increase(
        self, key: str, move: str, amount: Optional[int] = None
    ) -> list[str]:
        """Apply an ability-authored extra Stability cost for this move.

        amount may be snapshotted before move resolution. That matters for
        stack-on-hit abilities: a stack earned by the current hit must increase
        the NEXT action cost, exactly like stamina is deducted before the hit.
        """
        try:
            cost = self.move_cost_increase(key, move) if amount is None else max(
                0, int(amount))
            if cost <= 0:
                return []
            return self._action_cost(key, -cost, move)
        except Exception:
            return []

    # =========================================================================
    #  Private helpers
    # =========================================================================

    def _action_cost(self, key: str, delta: int, move: str = "") -> list[str]:
        from .purification import action_cost
        session = getattr(self, "purification_session", None)
        if session is not None and delta < 0:
            delta = -action_cost(session, key, -delta)
        extra = getattr(self, "effect_runtime", None)
        if extra is not None and delta < 0:
            delta = -math.ceil(extra.cost(key, move, "stability", -delta))
        return self._apply(key, delta)

    def _apply(self, key: str, delta: int) -> list[str]:
        """Clamp-apply ``delta`` to ``stability[key]`` and return a log line.

        If the Bey has ``burst_resistance_pct`` in any ability, negative
        stability deltas are reduced by that percentage (only when the
        effects gate is active for this matchup).
        """
        # Check burst resistance on negative deltas only
        original_delta = delta
        if delta < 0:
            blade = self._blades.get(key, {})
            for ab in self._get_abilities_for_blade(blade):
                resist_pct = ab.get("burst_resistance_pct", 0)
                if resist_pct > 0:
                    # Reduce the loss by resist_pct
                    reduction = int(abs(delta) * (resist_pct / 100))
                    delta = min(-1, delta + reduction)  # keep at least -1
                    break

        old = self.stability.get(key, STABILITY_DEFAULT)
        cap = self.max.get(key, STABILITY_DEFAULT)
        new = max(0, min(cap, old + delta))
        self.stability[key] = new
        if new == old:
            return []                      # capped no-op → no misleading log line

        blade_name = self._blades.get(key, {}).get("name", "Unknown")
        sign = "+" if original_delta >= 0 else ""
        icon = "🔄" if original_delta >= 0 else "💢"
        log = f"  {icon} **{blade_name}** stability {sign}{original_delta}"
        if delta != original_delta:
            log += f" (reduced to {delta} by burst resistance)"
        log += f" → `{new}`"
        return [log]

    def _get_abilities_for_blade(self, blade: dict) -> list[dict]:
        """Return ability list from a blade dict (helper for burst resist)."""
        if "abilities" in blade:
            return [ab for ab in blade["abilities"] if isinstance(ab, dict)]
        ab = blade.get("ability")
        if isinstance(ab, dict) and ab:
            return [ab]
        return []
