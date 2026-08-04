"""
cogs/chain_handler.py
---------------------
ChainHandler — owns the ability-chain queue and resolves chain steps.

Design Principles
-----------------
  1. AbilityEngine is the caller. ChainHandler holds zero references to
     AbilityEngine — it only knows about BattleSession and StatusManager.
     This means ability_engine.py can delegate to chain_handler without
     creating a circular dependency.

  2. All state mutations (buffs, shields, silences, etc.) go through
     StatusManager, so the chain step effects are automatically visible
     in snapshots and embeds.

  3. Step lifecycle:
       once=True  — fires at most once; marked _fired=True and kept in
                    the queue so it stays "exhausted" and doesn't re-fire.
       once=False — consumed on each pass and discarded; the originating
                    trigger must re-queue it each round if it should recur.

Supported Effects
-----------------
  crit, heal_pct, activate_mode, attack_boost, shield, silence,
  true_damage, dmg_amp, ignore_def, all_stats_boost, full_stat_boost,
  disable_ability_2, damage_reduction, defense_boost, guaranteed_crit,
  special_boost, invulnerable, reflect, burn, special_damage_amp

Supported Conditions
--------------------
  always, hp_below, next_attack, stack_threshold, on_special_use,
  pre_special  (consumed by AbilityEngine._on_special before this pass)

Public API
----------
  ChainHandler(session)

  .queue(key, steps)
      Append chain steps to the pending queue for *key*.

  .resolve(key, blade, okey, dmg_dealt) -> list[str]
      Evaluate and fire all pending steps whose condition is met.
      Returns log lines.  Call once per round, inside the silence guard.

  .drain_pre_special(key) -> list[dict]
      Pop and return all steps with condition == "pre_special" that
      haven't fired yet.  Called by AbilityEngine._on_special.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .constants import BASE_HP

if TYPE_CHECKING:
    from .session import BattleSession


class ChainHandler:
    """Owns the ability-chain queue and resolves each step for one BattleSession."""

    def __init__(self, session: "BattleSession") -> None:
        self.session = session

    # ------------------------------------------------------------------
    #  Internal helpers
    # ------------------------------------------------------------------

    @property
    def _sm(self):
        """Convenience reference to the session's StatusManager."""
        return self.session.status

    def _other_key(self, key: str) -> str:
        keys = list(self.session.hp.keys())
        return keys[1] if key == keys[0] else keys[0]

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def queue(self, key: str, steps: list[dict]) -> None:
        """Append *steps* to the pending chain queue for *key*.

        Each step dict shape::

            {
              "condition": "hp_below" | "always" | "next_attack"
                         | "stack_threshold" | "on_special_use"
                         | "pre_special",
              "threshold": 0.30,        # float ratio (hp_below) or int (stack_threshold)
              "effect":    "crit",      # see module docstring
              "value":     40,          # meaning depends on effect
              "duration":  1,           # turns (for timed effects)
              "once":      True,        # fire at most once per match
              "_fired":    False,       # internal: set True after firing
            }
        """
        self._sm.pending_chains.setdefault(key, []).extend(steps)

    def drain_pre_special(self, key: str) -> list[dict]:
        """Pop all un-fired pre_special steps from the queue and return them.

        Called by AbilityEngine._on_special *before* the normal resolve pass so
        that same-turn special-damage amps are applied before damage is computed.
        once=False steps are discarded (caller re-queues on next trigger).
        once=True steps are marked _fired and returned to the queue exhausted.
        """
        pending = self._sm.pending_chains.get(key, [])
        pre: list[dict] = []
        surviving: list[dict] = []

        for step in pending:
            if step.get("condition") != "pre_special":
                surviving.append(step)
                continue
            if step.get("once") and step.get("_fired"):
                surviving.append(step)
                continue
            pre.append(step)
            if step.get("once"):
                step["_fired"] = True
                surviving.append(step)

        self._sm.pending_chains[key] = surviving
        return pre

    def resolve(
        self,
        key:       str,
        blade:     dict,
        okey:      str,
        dmg_dealt: int,
    ) -> list[str]:
        """Evaluate all pending chain steps for *key* and fire those whose
        condition is met.

        Returns log lines.
        Must be called inside the silence guard — silenced players must not
        resolve queued chains.
        Must be called with is_first_hit=True guard in the caller — multi-hit
        specials should not fire chains on every hit.
        """
        sm   = self._sm
        s    = self.session
        logs: list[str] = []

        steps = sm.pending_chains.get(key, [])
        if not steps:
            return logs

        hp_value = max(0, s.hp.get(key, 0))
        hp_ratio = hp_value / BASE_HP if BASE_HP else 1.0

        # ── First ability name for log labels ─────────────────────────────────
        abilities = blade.get("abilities")
        if abilities and isinstance(abilities, list):
            abilities_list = [ab for ab in abilities if isinstance(ab, dict)]
        else:
            abilities_list = []
            if isinstance(blade.get("ability"), dict):
                abilities_list = [blade["ability"]]

        ab_name = abilities_list[0].get("name", blade.get("name", "???")) if abilities_list else blade.get("name", "???")

        # ── Separate exhausted once-steps from live candidates ────────────────
        surviving:  list[dict] = []
        to_process: list[dict] = []

        for step in steps:
            if step.get("once") and step.get("_fired"):
                surviving.append(step)
            else:
                to_process.append(step)

        # Rebuild queue with only exhausted once-steps; once=False are discarded
        sm.pending_chains[key] = surviving

        # ── Evaluate each live step ───────────────────────────────────────────
        for step in to_process:
            condition = step.get("condition", "always")
            met = self._check_condition(condition, step, key, hp_ratio)
            if not met:
                continue

            if step.get("once"):
                step["_fired"] = True
                sm.pending_chains[key].append(step)

            logs.extend(
                self._apply_effect(step, key, okey, blade, ab_name, dmg_dealt)
            )

        return logs

    # ------------------------------------------------------------------
    #  Condition evaluation
    # ------------------------------------------------------------------

    def _check_condition(
        self,
        condition: str,
        step:      dict,
        key:       str,
        hp_ratio:  float,
    ) -> bool:
        if condition == "always":
            return True
        if condition == "hp_below":
            return hp_ratio < step.get("threshold", 0.30)
        if condition in ("next_attack", "on_special_use"):
            return True
        if condition == "stack_threshold":
            thresh = step.get("threshold", 5)
            return self.session.ability.demon_mode_atk_stacks.get(key, 0) >= thresh
        return False

    # ------------------------------------------------------------------
    #  Effect application
    # ------------------------------------------------------------------

    def _apply_effect(
        self,
        step:      dict,
        key:       str,
        okey:      str,
        blade:     dict,
        ab_name:   str,
        dmg_dealt: int,
    ) -> list[str]:
        logs  = []
        sm    = self._sm
        s     = self.session
        effect = step.get("effect", "")
        value  = step.get("value", 0)
        dur    = step.get("duration", 1)

        # Guard: if either player key is no longer in the session HP dict
        # (e.g. disconnected mid-battle), skip silently to prevent KeyError.
        if key not in s.hp or okey not in s.hp:
            return logs

        if effect == "crit":
            bonus = int(value)
            s.hp[okey] = max(0, s.hp.get(okey, 0) - bonus)
            logs.append(
                f"  ⛓️ **{ab_name} Chain** — Forced CRIT! +**{bonus} true dmg**!"
            )

        elif effect == "heal_pct":
            heal = math.ceil(BASE_HP * value)
            s.hp[key] = min(BASE_HP, s.hp.get(key, 0) + heal)
            logs.append(f"  ⛓️ **{ab_name} Chain** — Chain heal: +**{heal} HP**!")

        elif effect in ("activate_mode", "attack_boost"):
            sm.add_buff(key, "attack", int(value), dur)
            logs.append(
                f"  ⛓️ **{ab_name} Chain** — MODE ACTIVATED! "
                f"+{int(value)} ATK for {dur} round(s)!"
            )

        elif effect == "shield":
            sm.add_shield(key, int(value))
            logs.append(
                f"  ⛓️ **{ab_name} Chain** — Shield granted: **{int(value)} HP**!"
            )

        elif effect == "silence":
            sm.silence(okey, dur)
            logs.append(
                f"  ⛓️ **{ab_name} Chain** — Enemy ability SEALED for **{dur} turn(s)**!"
            )

        elif effect == "true_damage":
            sm.set_duration("true_damage_turns", key, dur)
            logs.append(
                f"  ⛓️ **{ab_name} Chain** — TRUE DAMAGE mode for {dur} turn(s)!"
            )

        elif effect == "dmg_amp":
            sm.add_dmg_amp(key, float(value))
            logs.append(
                f"  ⛓️ **{ab_name} Chain** — Damage amplified +{int(value * 100)}%!"
            )

        elif effect == "ignore_def":
            sm.set_duration("ignore_defense_turns", key, dur)
            logs.append(
                f"  ⛓️ **{ab_name} Chain** — Defense IGNORED for {dur} turn(s)!"
            )

        elif effect in ("all_stats_boost", "full_stat_boost"):
            # Prefer battle-start modified stats (parts/avatar/level) so
            # percentage boosts scale with the player's real stats.
            b_stats = (getattr(s, "battle_stats", {}) or {}).get(key) \
                or (s.blades.get(key, {}).get("stats", {}) if hasattr(s, 'blades') else {})
            if effect == "all_stats_boost":
                atk_gain = math.ceil(b_stats.get("attack", 0) * value)
                def_gain = math.ceil(b_stats.get("defense", 0) * value)
            else:
                atk_gain = def_gain = int(value)
            if atk_gain:
                sm.add_buff(key, "attack", atk_gain, 999)
            if def_gain:
                sm.add_buff(key, "defense", def_gain, 999)
            if effect == "all_stats_boost":
                logs.append(
                    f"  ⛓️ **{ab_name} Chain** — ALL STATS ×{int(value * 100) + 100}%! "
                    f"(+{atk_gain} ATK, +{def_gain} DEF permanently)"
                )
            else:
                logs.append(
                    f"  👹 **{ab_name} Chain** — ⚠️ DEMON MODE ACTIVATED! "
                    f"All stats +**{atk_gain}** permanently!"
                )

        elif effect == "disable_ability_2":
            self.session.ability.ability_2_disabled[key] = True
            logs.append(
                f"  ⛓️ **{ab_name} Chain** — Secondary ability permanently DISABLED!"
            )

        elif effect == "damage_reduction":
            sm.add_buff(key, "defense", int(value), dur)
            logs.append(
                f"  ⛓️ **{ab_name} Chain** — Damage reduction activated! "
                f"+**{int(value)} DEF** for {dur} round(s)!"
            )

        elif effect == "defense_boost":
            sm.add_buff(key, "defense", int(value), dur)
            logs.append(
                f"  ⛓️ **{ab_name} Chain** — Defense surged! "
                f"+**{int(value)} DEF** for {dur} round(s)!"
            )

        elif effect == "guaranteed_crit":
            self.session.ability.guaranteed_crit_turns[key] = (
                self.session.ability.guaranteed_crit_turns.get(key, 0) + dur
            )
            logs.append(
                f"  ⛓️ **{ab_name} Chain** — ☠️ CONDEMNED MODE! "
                f"Guaranteed crits for **{dur} attack(s)**!"
            )

        elif effect == "special_boost":
            self.session.ability.special_boost_flat[key] = (
                self.session.ability.special_boost_flat.get(key, 0) + int(value)
            )
            logs.append(
                f"  ⛓️ **{ab_name} Chain** — Special Move empowered! "
                f"+**{int(value)} Special dmg** permanently!"
            )

        elif effect == "invulnerable":
            sm.set_invulnerable(key, max(sm.invulnerable_turns.get(key, 0), dur))
            logs.append(
                f"  ⛓️ **{ab_name} Chain** — 🛡️ INVULNERABLE! "
                f"All damage nullified for **{dur} turn(s)**!"
            )

        elif effect == "reflect":
            self.session.ability.post_rebirth_reflect[key] = max(
                self.session.ability.post_rebirth_reflect.get(key, 0), int(value)
            )
            logs.append(
                f"  ⛓️ **{ab_name} Chain** — 🔥 Armor superheated! "
                f"Reflects **{int(value)} dmg** on every hit!"
            )

        elif effect == "burn":
            burn_ab = {
                "name":                 ab_name,
                "burn_damage_per_turn": int(value),
                "burn_duration":        dur,
                "max_burn_stacks":      3,
            }
            if hasattr(sm, 'apply_burn'):
                logs.extend(sm.apply_burn(okey, burn_ab))
            else:
                logs.append(f"  ⚠️ **{ab_name} Chain** — Burn effect not available (apply_burn missing)!")

        elif effect == "special_damage_amp":
            pre_special_amp = getattr(sm, 'pre_special_amp', {})
            pre_special_amp[key] = pre_special_amp.get(key, 0.0) + float(value)
            sm.pre_special_amp = pre_special_amp  # unconditional write — always persist the amp
            logs.append(
                f"  🌩️ **{ab_name} Chain** — Dark Weather surges! "
                f"Special Move damage +**{int(value * 100)}%** this activation!"
            )

        else:
            # Unknown effect — log warning for debugging
            logs.append(
                f"  ⚠️ **{ab_name} Chain** — Unknown effect '{effect}' skipped!"
            )

        return logs
