"""
cogs/defense_manager.py
-----------------------
Dedicated Defense Manager.

Centralises every piece of defense-resolution logic:
  • Shield Gate (blocking weak hits entirely)
  • Flat damage reduction
  • Deficit bleed (stat gap amplifier)
  • Counter-hit calculation and application
  • Grind debuff tracking, ticking, and duration calculation
  • Defense-buff pre-processing (from AbilityEngine buffs)
  • Defense-pierce tracking (ignore_defense_turns)
  • Shatter-stack defense reduction (Dynamite Belial)

Parallels
---------
  StaminaManager  — stamina costs, regen, special gauge
  AttackManager   — attack pipeline and damage application
  DefenseManager  — defense pipeline (this file)

No damage formulas, ability primitives, or Discord calls live here.

Public API
----------
  DefenseManager(session)

  .tick_grind(key)  → list[str]
      Called at round-start. Applies grind debuff tick for one player.

  .apply_grind(key, def_stat, target_key)  → list[str]
      Called after matchup resolves to "lose_grind".
      Sets grind turns on the Stamina user.

  .preprocess_defender_stats(mkey, okey, mblade, oblade, ostats) → (dict, list[str])
      Adjusts raw defender stats for pierce / shatter / def-buff before
      calc_damage runs. Mirrors the logic previously inline in AttackManager.

  .calc_shield_gate(raw_hit, def_stat) → (mitigated, absorbed)
  .calc_deficit_bleed(raw_hit, atk_stat, def_stat) → int
  .calc_counter_hit(absorbed, def_stat) → int
  .calc_grind_duration(def_stat) → int
      Pure math helpers — thin wrappers over damage_rules internals so
      callers never import from damage_rules directly.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .damage_rules import (
    _shield_gate,
    _deficit_bleed,
    _counter_hit,
    _grind_duration,
    SHIELD_GATE_RATIO,
    SHIELD_FLAT_RATIO,
    COUNTER_ABSORB_RATIO,
    COUNTER_STAT_RATIO,
    COUNTER_MIN,
    DEFICIT_DIVISOR,
    GRIND_LEVEL_DIVISOR,
)

if TYPE_CHECKING:
    from .session import BattleSession


class DefenseManager:
    """Owns all defense-side resolution for one BattleSession.

    Constructed once and stored as ``session.defense_manager``.
    """

    def __init__(self, session: "BattleSession") -> None:
        self.session = session
        # grind_turns[key] = rounds remaining of the Grind debuff on that player
        self.grind_turns: dict[str, int] = {k: 0 for k in session.hp.keys()}

    # =========================================================================
    #  Grind debuff
    # =========================================================================

    def tick_grind(self, key: str) -> list[str]:
        """Apply one round of the Grind debuff tick to ``key``.

        Deducts 1 Stamina and decrements the counter.
        Returns log lines (empty if no active grind).
        """
        if self.grind_turns.get(key, 0) <= 0:
            return []

        sm = self.session.stamina_manager
        sm.stamina[key] = max(0.0, sm.stamina.get(key, 0.0) - 1.0)
        self.grind_turns[key] -= 1

        blade_name = self.session.blades.get(key, {}).get("name", "Unknown")
        return [
            f"  🪨 **Grind** — {blade_name} loses "
            f"**1 Stamina** from the defense grind! "
            f"({self.grind_turns[key]} round(s) remaining)"
        ]

    def apply_grind(self, defender_key: str, def_stat: int, stamina_user_key: str) -> list[str]:
        """Set the Grind debuff on ``stamina_user_key`` after Defense lost to Stamina.

        Only sets the debuff if the new duration is longer than whatever is
        already running (so stacking doesn't reset a longer existing grind).
        Returns log lines.
        """
        turns = self.calc_grind_duration(def_stat)
        self.grind_turns[stamina_user_key] = max(
            self.grind_turns.get(stamina_user_key, 0), turns
        )
        defender_name = self.session.blades.get(defender_key, {}).get("name", "Unknown")
        stamina_name  = self.session.blades.get(stamina_user_key, {}).get("name", "Unknown")
        return [
            f"  🪨 **Grind!** {defender_name} saps {stamina_name}'s spin — "
            f"**-1 Stamina/round** for **{turns}** round(s)!"
        ]

    # =========================================================================
    #  Defender stat pre-processing
    # =========================================================================

    def preprocess_defender_stats(
        self,
        mkey:   str,
        okey:   str,
        mblade: dict,
        oblade: dict,
        ostats: dict,
    ) -> tuple[dict, list[str]]:
        """Adjust defender stats before calc_damage runs.

        Handles (in order):
          1. Defense-pierce turns granted by ability effects
          2. Active defense buffs on the defender
          3. Shatter-stack defense reduction (Dynamite Belial)

        Returns ``(adjusted_stats, log_lines)``.
        Calling code should replace its local ``ostats`` with the returned dict.
        """
        logs   = []
        ab_eng = self.session.ability

        # 1. Defense-pierce: attacker's ability zeroes out defender DEF
        pierce_turns = self.session.status.get_duration("ignore_defense_turns", mkey)
        if pierce_turns > 0:
            ostats = dict(ostats)
            # Save the real DEF BEFORE zeroing so deficit_bleed isn't
            # inflated by treating the opponent as having 0 DEF.
            ostats["_real_defense"] = ostats.get("defense", 50)
            ostats["defense"]       = 0
            logs.append(
                f"  🎯 **Defense Pierce** — {mblade['name']} ignores all defense!"
            )
            d = self.session.status.ignore_defense_turns
            d[mkey] = max(0, pierce_turns - 1)
            return ostats, logs   # pierce already zeroed DEF; skip buff/shatter

        # 2. Active defense buff on the defender
        def_buf = ab_eng._get_buf_bonus(okey, "defense")
        if def_buf:
            ostats = dict(ostats)
            base_def = ostats.get("defense", 50)
            ostats["defense"] = base_def + def_buf

        # 3. Shatter-stack defense reduction (Dynamite Belial)
        for _mab in ab_eng._get_abilities(mblade):
            if _mab.get("trigger") == "on_attack_or_special":
                shatter_red = ab_eng.get_shatter_defense_reduction(mkey, _mab)  # mkey = attacker who built shatter stacks
                if shatter_red:
                    ostats = dict(ostats)
                    ostats["defense"] = max(0, ostats.get("defense", 50) - shatter_red)
                    logs.append(
                        f"  🪨 **Shatter** — {oblade['name']} defense reduced by "
                        f"**{shatter_red}** from accumulated Shatter stacks!"
                    )
                break   # only one shatter ability per blade

        return ostats, logs

    # =========================================================================
    #  Stability costs for defense moves
    # =========================================================================

    def apply_stability_costs(
        self,
        key:           str,
        move:          str,
        enemy_move:    str,
        enemy_matchup: str,
        incoming_dmg:  int,
    ) -> list[str]:
        """Apply stability delta to a player who used Defense this round.

        Called from session.__resolve_round_body after damage is resolved so
        we know whether the hit landed and what the matchup was.

        ``enemy_matchup`` MUST be the matchup returned by resolve_pair for the
        OPPONENT (attacker), NOT this defender's own matchup string.
        For example, when evaluating k1 (defender), pass ``matchup_p2``
        (the result of P2's attack resolution against P1).

        Attacker matchup strings map to stability contexts as follows:
          "win"        → attacker's hit got through   → defender context: vs_attack  (-6)
          "lose"       → attacker was blocked         → defender context: blocked    (+5)
          "mirror"     → defense vs defense           → defender context: vs_defense (-3)
          "lose_grind" → attacker (Stamina) beat def  → defender context: vs_stamina  (0)

        ``incoming_dmg`` is the damage dealt BY THE OPPONENT to this defender
        (i.e. dmg_p2 when evaluating k1, dmg_p1 when evaluating k2).
        It is used as a secondary signal: if the attacker nominally "won" but
        incoming_dmg is 0 (full Shield Gate block), we treat it as "blocked".
        """
        from .constants import MOVE_DEFENSE, MOVE_STAMINA
        if move != MOVE_DEFENSE:
            return []

        stab = self.session.stability_manager
        if not stab.is_effects_active(key, self._get_enemy_key(key)):
            return []

        # enemy_matchup is from the OPPONENT (attacker) perspective:
        #   "win"        → attacker hit this defender   → damage got through
        #   "lose"       → attacker was blocked         → defender absorbed all
        #   "mirror"     → defense vs defense
        #   "lose_grind" → Stamina beat Defense (attacker is Stamina user)
        #
        # Bug fix: previously this received the DEFENDER's own matchup string
        # instead of the enemy's, causing all context lookups to be inverted.
        # Stamina-vs-Defense resulted in "win" on Stamina's side, which was
        # mistakenly mapped to "vs_attack" (-6) instead of "vs_stamina" (0).
        if enemy_move == MOVE_STAMINA:
            # Stamina beats Defense — defender loses no stability
            context = "vs_stamina"
        elif enemy_matchup == "mirror":
            # Defense vs Defense mirror clash
            context = "vs_defense"
        elif enemy_matchup == "lose":
            # Attacker was fully blocked — defender gains stability
            context = "blocked"
        elif enemy_matchup == "win":
            # Attacker's hit got through — defender takes stability hit
            # If damage was fully nullified post-calc (e.g. shield), treat as blocked
            context = "blocked" if incoming_dmg == 0 else "vs_attack"
        else:
            context = "passive"

        return stab.apply_defense_cost(key, context)

    def _get_enemy_key(self, key: str) -> str:
        """Return the other player's key from the session.

        Uses ``session.blades`` (never shrinks) rather than ``session.hp``
        so that this still returns the correct enemy key even when the
        opponent's HP reaches 0 mid-round — avoiding a false self-match that
        would make is_effects_active(key, key) always return True.
        """
        for k in self.session.blades:
            if k != key:
                return k
        return key

    # =========================================================================
    #  Pure math helpers (thin wrappers over damage_rules internals)
    # =========================================================================

    @staticmethod
    def calc_shield_gate(raw_hit: int, def_stat: int) -> tuple[int, int]:
        """Apply Shield Gate + flat reduction.

        Returns ``(damage_after_mitigation, absorbed_amount)``.

        Hits below ``def_stat × SHIELD_GATE_RATIO`` are fully blocked.
        Survivors are reduced by a flat ``def_stat × SHIELD_FLAT_RATIO`` amount.
        ``absorbed_amount`` feeds into :meth:`calc_counter_hit`.
        """
        return _shield_gate(raw_hit, def_stat)

    @staticmethod
    def calc_deficit_bleed(raw_hit: int, atk_stat: int, def_stat: int) -> int:
        """Amplify raw_hit when attacker ATK exceeds defender DEF.

        Multiplier = 1 + (max(0, atk - def) / DEFICIT_DIVISOR).
        Applied *after* Shield Gate so defense still does its primary job.
        """
        return _deficit_bleed(raw_hit, atk_stat, def_stat)

    @staticmethod
    def calc_counter_hit(absorbed: int, def_stat: int) -> int:
        """Calculate the counter-hit damage reflected by a successful Defense.

        counter = max(COUNTER_MIN,
                      ceil(absorbed × COUNTER_ABSORB_RATIO
                           + def_stat × COUNTER_STAT_RATIO))
        """
        return _counter_hit(absorbed, def_stat)

    @staticmethod
    def calc_grind_duration(def_stat: int) -> int:
        """Return the number of rounds the Grind debuff lasts.

        Duration = 1 + floor(def_stat / GRIND_LEVEL_DIVISOR).
        """
        return _grind_duration(def_stat)

    # =========================================================================
    #  Convenience constants (readable from outside without importing damage_rules)
    # =========================================================================

    SHIELD_GATE_RATIO    = SHIELD_GATE_RATIO
    SHIELD_FLAT_RATIO    = SHIELD_FLAT_RATIO
    COUNTER_ABSORB_RATIO = COUNTER_ABSORB_RATIO
    COUNTER_STAT_RATIO   = COUNTER_STAT_RATIO
    COUNTER_MIN          = COUNTER_MIN
    DEFICIT_DIVISOR      = DEFICIT_DIVISOR
    GRIND_LEVEL_DIVISOR  = GRIND_LEVEL_DIVISOR
