"""
cogs/abilities/legacy_convert.py
--------------------------------
Pure pattern-based converter: legacy flat-field ability dicts  ->  rules.

ZERO blade names here. Mapping keys off *field-name patterns only*, so any
old-format ability (including prefixed bespoke fields like `phoenix_burn_dmg`
or `solar_atk_buff`) converts mechanically. Fields with no recognised pattern
are collected in `UNMAPPED` for a migration report — they simply do nothing
until expressed as explicit rules in JSON.

Conversion model
----------------
* The ability's `trigger` becomes the rule's `when` (with legacy passive
  defender-fields split into a separate `on_defend` / `on_take_damage` rule).
* `hp_threshold` / `threshold_pct` / `*_threshold` (0–1 values) become an
  `hp_below_pct` condition attached to every produced rule.
* `once: true` -> rule `once: "battle"`.
* `chain` is passed through untouched (ChainHandler already generic).
"""
from __future__ import annotations

import re
from typing import Any

# Fields that are metadata, not effects
_META = {"name", "trigger", "description", "chain", "once", "cog_types"}

# Suffix patterns → op factory.  Each factory returns (op_dict, phase)
#   phase: "off" = mover rule, "def" = defender rule, "setup" = battle start
# `m` is the regex match; `v` the field value; `ab` the full ability dict.

def _mode_gated(op: dict, ab: dict, prefix: str) -> dict:
    """Attach a stack-count gate to a mode payoff op, if the ability declares
    one (`<prefix>_threshold` holding an integer >= 2). Values <= 1 are HP
    fractions and stay with _threshold_conds. Returns the op unchanged when
    there is no stack gate to apply."""
    thr = ab.get(f"{prefix}_threshold")
    if isinstance(thr, bool) or not isinstance(thr, (int, float)):
        return op
    if thr < 2:                     # 0-1 => HP fraction, handled elsewhere
        return op
    op["_if"] = [{"cond": "counter_at_least",
                  "name": f"{ab.get('name', 'Ability')}_stacks",
                  "value": int(thr)}]
    return op


def _max_stacks(ab: dict, per_stack: int, default: int = 5) -> int:
    """How many times a stacking buff may stack.

    Two field conventions live in the data and they mean different things:
      * `max_stacks` / `<name>_max_stacks` — already a stack COUNT.
      * `max_stack`  — the total stat cap (Rage Longinus 80 = 10 × 8 ATK).
    Passing the total straight through as a count was letting stacks run to
    80/50, so the singular form is divided by the per-stack value.
    """
    for k, v in ab.items():
        if k == "max_stacks" or k.endswith("_max_stacks"):
            try:
                return max(1, int(v))
            except (TypeError, ValueError):
                pass
    if "max_stack" in ab:
        try:
            total = int(ab["max_stack"])
            if per_stack > 0:
                return max(1, total // per_stack)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return default


def _turns_for(ab: dict, base: str, default: int = 2) -> int:
    """Find a companion *_turns / *_duration field for `base`."""
    for suffix in ("_turns", "_duration"):
        for k, v in ab.items():
            if k.endswith(suffix) and base.split("_")[0] in k:
                try:
                    return max(1, int(v))
                except (TypeError, ValueError):
                    pass
    return default


_PATTERNS: list[tuple[re.Pattern, Any]] = [
    # ── percentage all-stat buffs ─────────────────────────────────────────────
    (re.compile(r"all_stats?_(bonus|boost|buff)(_pct)?$"),
     lambda m, v, ab: ({"op": "buff_all_pct", "value": float(v),
                        "turns": _turns_for(ab, "all", 2)}, "off")),

    # ── streak payoffs ────────────────────────────────────────────────────────
    # "Every 4 successful defenses, releases a burst that heals 25 HP."
    # `streak_threshold` was unmapped, so the payoff fired on EVERY trigger
    # instead of every Nth. Counts the trigger, then pays out and resets.
    (re.compile(r"^streak_heal$"),
     lambda m, v, ab: ({"op": "counter_burst",
                        "name": f"{ab.get('name','Ability')}_streak",
                        "at": max(2, int(ab.get("streak_threshold", 4) or 4)),
                        "heal": int(v),
                        "reset": True,
                        "_after": True}, "off")),
    # Non-heal streak payoffs (Wind Knight's +35 ATK / pierce every 3rd defense)
    # get the same gate, attached to the op itself.
    (re.compile(r"^streak_(atk|attack)_boost$"),
     lambda m, v, ab: ({"op": "buff", "stat": "attack", "amount": int(v),
                        "turns": _turns_for(ab, "atk", 2),
                        "_if": [{"cond": "counter_at_least",
                                 "name": f"{ab.get('name','Ability')}_streak",
                                 "value": max(2, int(ab.get("streak_threshold", 3) or 3))}]},
                       "off")),
    (re.compile(r"^streak_pierce$"),
     lambda m, v, ab: (({"op": "ignore_defense", "turns": 1,
                         "_if": [{"cond": "counter_at_least",
                                  "name": f"{ab.get('name','Ability')}_streak",
                                  "value": max(2, int(ab.get("streak_threshold", 3) or 3))}]},
                        "off") if v else (None, "off"))),

    # ── max-stack payoffs ─────────────────────────────────────────────────────
    # `max_stack_extra_hit: true` — extra Special hit once the stack counter is
    # full. Gated on the counter so it can't fire from stack 1.
    (re.compile(r"max_stack_extra_hit$"),
     lambda m, v, ab: (({"op": "bonus_special_hits", "hits": 1,
                         "_if": [{"cond": "counter_at_least",
                                  "name": f"{ab.get('name','Ability')}_stacks",
                                  "value": int(ab.get("max_stack_threshold")
                                               or _max_stacks(ab, 1))}]}, "off")
                       if v else (None, "off"))),
    # `<prefix>_burst_dmg` — burst damage when the matching stack counter fills.
    (re.compile(r"^(\w+?)_burst_dmg$"),
     lambda m, v, ab: ({"op": "counter_burst",
                        "name": f"{ab.get('name','Ability')}_stacks",
                        "at": _max_stacks(ab, 1),
                        "damage": int(v),
                        "reset": True}, "off")),

    # ── mode payoffs gated on a stack count ───────────────────────────────────
    # MUST precede the generic bonus_damage / damage_reduction patterns below.
    # `berserker_threshold: 5` is a STACK count, not an HP fraction, so
    # _threshold_conds ignored it and BERSERKER STATE's bonuses were live from
    # stack 1 — the whole "push it to its limit" payoff was free on turn one.
    (re.compile(r"^(\w+?)_(bonus_damage|damage_reduction)$"),
     lambda m, v, ab: (_mode_gated(
         {"op": "bonus_damage", "value": int(v)} if m.group(2) == "bonus_damage"
         else {"op": "reduce_damage_flat", "value": int(v)}, ab, m.group(1)),
         "off" if m.group(2) == "bonus_damage" else "def")),

    # ── previously-unmapped effects ──────────────────────────────────────────
    # Every pattern below covers a field that matched nothing and therefore did
    # nothing. They are grouped here, ahead of the generic buff/damage
    # patterns, because several would otherwise be swallowed by a looser rule.

    # "Every 3rd win erupts for N true damage" (Flare Dragon, Void Dragon,
    # Excalibur Ascendant). The streak gate is attached by the generic
    # streak_ handling in legacy_convert().
    (re.compile(r"^streak_true_damage$"),
     lambda m, v, ab: ({"op": "true_damage", "value": int(v)}, "off")),

    # Flat all-stat mode payoffs: DEMON MODE, TRIPLE FURY, Requiem's threshold
    # boost. `buff_all_pct` reads its value as a fraction, so a flat +10/+20/+30
    # had no op at all until `buff_all` was added.
    (re.compile(r"_stat_boost(_on_threshold)?$|^stat_boost_on_threshold$"),
     lambda m, v, ab: ({"op": "buff_all", "value": int(v),
                        "turns": _turns_for(ab, "mode", 99)}, "off")),

    # "30% of what lands is absorbed rather than taken" / attack-power amps
    (re.compile(r"absorb_pct$"),
     lambda m, v, ab: ({"op": "reduce_damage_pct",
                        "value": float(v) * (100 if float(v) <= 1 else 1)}, "def")),
    (re.compile(r"(atk|attack)_amp_on_threshold$"),
     lambda m, v, ab: ({"op": "bonus_damage_pct",
                        "value": float(v) * (100 if float(v) <= 1 else 1)}, "off")),
    (re.compile(r"critical_low_atk_amp$"),
     lambda m, v, ab: ({"op": "bonus_damage_pct",
                        "value": float(v) * (100 if float(v) <= 1 else 1),
                        "_if": [{"cond": "hp_below_pct", "value": 0.25}]}, "off")),

    # Cho-Z Spriggan's per-matchup counter buffs
    (re.compile(r"(atk|attack)_counter_buff$"),
     lambda m, v, ab: ({"op": "buff", "stat": "attack", "amount": int(v),
                        "turns": _turns_for(ab, "atk", 3)}, "off")),
    (re.compile(r"(def|defense)_counter_buff$"),
     lambda m, v, ab: ({"op": "buff", "stat": "defense", "amount": int(v),
                        "turns": _turns_for(ab, "def", 3)}, "off")),
    (re.compile(r"_balance_atk$"),
     lambda m, v, ab: ({"op": "buff", "stat": "attack", "amount": int(v),
                        "turns": 3,
                        "_if": [{"cond": "enemy_type_is", "value": "Balance"}]}, "off")),
    (re.compile(r"reflect_chance$"),
     lambda m, v, ab: ({"op": "reflect_pct",
                        "value": float(v) * (100 if float(v) <= 1 else 1)}, "def")),
    (re.compile(r"silence_on_\w+$"),
     lambda m, v, ab: ({"op": "silence", "turns": max(1, int(v))}, "off")),

    # Dual-mode tips that trade stats for stamina
    (re.compile(r"mode_stamina_bonus$"),
     lambda m, v, ab: ({"op": "buff", "stat": "stamina", "amount": int(v),
                        "turns": 99}, "setup")),

    # Crit riders
    (re.compile(r"^crit_bonus$|special_crit_bonus$"),
     lambda m, v, ab: ({"op": "bonus_damage", "value": int(v)}, "off")),
    (re.compile(r"crit_enemy_def_reduction$"),
     lambda m, v, ab: ({"op": "enemy_debuff", "stat": "defense",
                        "amount": int(v),
                        "turns": _turns_for(ab, "crit", 2)}, "off")),
    (re.compile(r"_atk_debuff$"),
     lambda m, v, ab: ({"op": "enemy_debuff", "stat": "attack",
                        "amount": int(v),
                        "turns": _turns_for(ab, "atk", 2)}, "off")),

    # Counter-damage riders (Defense-move payoffs)
    (re.compile(r"low_hp_counter_bonus$"),
     lambda m, v, ab: ({"op": "bonus_damage", "value": int(v),
                        "_if": [{"cond": "hp_below_pct", "value": 0.5}]}, "off")),
    (re.compile(r"left_spin_counter_bonus$"),
     lambda m, v, ab: ({"op": "bonus_damage", "value": int(v),
                        "_if": [{"cond": "my_spin_is", "value": "Left"}]}, "off")),

    # Percentage-per-stack growth ("each Conquest Stack: +4% ATK")
    (re.compile(r"stack_atk_pct$"),
     lambda m, v, ab: ({"op": "stacking_buff", "stat": "attack",
                        "per_stack_pct": float(v),
                        "max": _max_stacks(ab, 1, 10)}, "off")),
    (re.compile(r"(evolve|_)atk_stack$"),
     lambda m, v, ab: ({"op": "stacking_buff", "stat": "attack",
                        "per_stack": int(v),
                        "max": _max_stacks(ab, int(v))}, "off")),
    (re.compile(r"stack_bonus_dmg$"),
     lambda m, v, ab: ({"op": "consume_counter", "name": f"{ab.get('name','Ability')}_stacks",
                        "damage_per_stack": int(v)}, "off")),
    (re.compile(r"_release_drain$"),
     lambda m, v, ab: ({"op": "counter_burst",
                        "name": f"{ab.get('name','Ability')}_stacks",
                        "at": _max_stacks(ab, 1, 5), "damage": int(v),
                        "reset": True}, "off")),

    # Immunities
    (re.compile(r"cannot_be_debuffed$|burst_immune$"),
     lambda m, v, ab: (({"op": "debuff_immune", "value": 1}, "setup")
                       if v else (None, "off"))),

    # ATK that scales with missing HP, capped by the companion `_max` field
    (re.compile(r"missing_hp_atk_max$"),
     lambda m, v, ab: ({"op": "buff", "stat": "attack", "amount": int(v),
                        "turns": _turns_for(ab, "missing", 3),
                        "_if": [{"cond": "hp_below_pct", "value": 0.5}]}, "off")),

    # Proc chances. No companion magnitude field exists for these, so they use
    # a uniform +50% on the proc rather than an invented flat number.
    (re.compile(r"double_hit_ratio$"),
     lambda m, v, ab: ({"op": "bonus_damage_pct",
                        "value": float(v) * (100 if float(v) <= 1 else 1)}, "off")),
    (re.compile(r"extra_hit_chance$|speed_burst_chance$|chaos_bonus_chance$|"
                r"weak_point_hit_chance$|burst_chance_bonus$|"
                r"upper_attack_lift_chance$"),
     lambda m, v, ab: ({"op": "bonus_damage_pct", "value": 50.0,
                        "_chance": float(v) if float(v) <= 1 else float(v) / 100},
                       "off")),
    (re.compile(r"comeback_ko_chance$"),
     lambda m, v, ab: ({"op": "execute", "value": 40, "enemy_hp_below_pct": 0.25,
                        "_chance": float(v) if float(v) <= 1 else float(v) / 100},
                       "off")),
    (re.compile(r"afterburn_chance$"),
     lambda m, v, ab: ({"op": "drain_stamina", "value": 1.5,
                        "_chance": float(v) if float(v) <= 1 else float(v) / 100},
                       "off")),

    # ── burns / DoT ───────────────────────────────────────────────────────────
    (re.compile(r"(burn)_(dmg|damage)(_per_turn)?$"),
     lambda m, v, ab: ({"op": "burn", "dmg": int(v),
                        "turns": _turns_for(ab, "burn", 2),
                        "max_stacks": int(ab.get("max_burn_stacks",
                                          ab.get(m.group(1) + "_stacks", 3)) or 3)}, "off")),
    (re.compile(r"(curse|corruption)_(dmg|damage)(_per_turn)?$"),
     lambda m, v, ab: ({"op": "burn", "dmg": int(v),
                        "turns": _turns_for(ab, m.group(1), 2),
                        "max_stacks": int(ab.get("max_curse_stacks", 3) or 3)}, "off")),

    # ── healing / sustain ────────────────────────────────────────────────────
    (re.compile(r"heal_pct$"),
     lambda m, v, ab: ({"op": "heal_pct", "value": float(v) * (100 if float(v) <= 1 else 1)}, "off")),
    (re.compile(r"(^|_)heal(_per_win|_per_turn|_per_hit|_per_drain)?$"),
     lambda m, v, ab: ({"op": "hp_regen", "value": int(v)}, "off")
                      if m.group(2) in ("_per_turn",)
                      else ({"op": "heal", "value": int(v)}, "off")),
    (re.compile(r"revival$"),
     lambda m, v, ab: ({"op": "revive", "value": int(ab.get("revival_hp", 150) or 150)}, "setup")
                      if v else (None, "off")),
    (re.compile(r"spin_steal_pct$|lifesteal"),
     lambda m, v, ab: ({"op": "lifesteal_pct",
                        "value": float(v) * (100 if float(v) <= 1 else 1)}, "setup")),
    # `hp_drain_true` is a BOOLEAN flag marking the drain as true damage, not a
    # second drain amount — int(True) was emitting a phantom extra 1 HP steal
    # alongside the real one.
    (re.compile(r"hp_drain_true$"),
     lambda m, v, ab: (None, "off")),
    (re.compile(r"hp_drain$|(^|_)drain$"),
     lambda m, v, ab: ({"op": "steal_hp", "value": int(v)}, "off")),

    # ── shields / mitigation (defender phase) ───────────────────────────────
    (re.compile(r"(^|_)shield(_value|_vs_attack)?$"),
     lambda m, v, ab: ({"op": "shield", "value": int(v)}, "def")),
    (re.compile(r"(damage|dmg)_reduction(_pct)?$"),
     lambda m, v, ab: ({"op": "reduce_damage_pct",
                        "value": float(v) * (100 if float(v) <= 1 else 1)}, "def")
                      if (m.group(2) == "_pct" or float(v) <= 1)
                      else ({"op": "reduce_damage_flat", "value": int(v)}, "def")),
    (re.compile(r"reflect(_damage|_pct|_vs_attack)?$"),
     lambda m, v, ab: ({"op": "reflect_pct",
                        "value": float(v) * (100 if float(v) <= 1 else 1)}, "def")
                      if (m.group(1) == "_pct" or (isinstance(v, float) and v <= 1))
                      else ({"op": "reflect_flat", "value": int(v)}, "def")),
    (re.compile(r"(evasion|nullify)_chance$"),
     lambda m, v, ab: ({"op": "negate_damage",
                        "_chance": float(v) if float(v) <= 1 else float(v) / 100}, "def")),
    (re.compile(r"invulnerable_turns$"),
     lambda m, v, ab: ({"op": "invulnerable", "turns": int(v)}, "off")),
    (re.compile(r"knockback_reduction_pct$|burst_resistance"),
     lambda m, v, ab: ({"op": "reduce_damage_pct",
                        "value": min(30.0, float(v) * (100 if float(v) <= 1 else 1) / 2)}, "def")),

    # ── buffs ─────────────────────────────────────────────────────────────────
    # ORDER MATTERS: the permanent/setup variants MUST be tested before the
    # generic `*_atk_bonus` / `*_def_buff` patterns, because those use
    # `.search()` and would otherwise swallow `passive_atk_bonus` as a plain
    # 2-turn buff — which then re-applies (and stacks) on every passive round.
    (re.compile(r"passive_atk_bonus$|permanent_atk_boost$"),
     lambda m, v, ab: ({"op": "buff", "stat": "attack", "amount": int(v), "turns": 99}, "setup")),
    (re.compile(r"initial_defense_buff$|passive_def(ense)?_bonus$"),
     lambda m, v, ab: ({"op": "buff", "stat": "defense", "amount": int(v), "turns": 99}, "setup")),
    (re.compile(r"(atk|attack)_(buff|boost|bonus)(_vs_\w+)?$"),
     lambda m, v, ab: ({"op": "buff", "stat": "attack", "amount": int(v),
                        "turns": _turns_for(ab, "atk", 2)}, "off")),
    (re.compile(r"(def|defense)_(buff|boost|bonus)$"),
     lambda m, v, ab: ({"op": "buff", "stat": "defense", "amount": int(v),
                        "turns": _turns_for(ab, "def", 2)}, "off")),
    (re.compile(r"passive_dmg_reduction$"),
     lambda m, v, ab: ({"op": "reduce_damage_pct",
                        "value": float(v) * (100 if float(v) <= 1 else 1)}, "def")),
    (re.compile(r"all_stats?_(boost|bonus)(_pct)?$"),
     lambda m, v, ab: ({"op": "buff", "stat": "attack", "amount": int(v),
                        "turns": 2}, "off")),  # attack proxy; full tri-stat via rules

    # ── stacking ─────────────────────────────────────────────────────────────
    # `max_stacks` (plural) is a COUNT. `max_stack` (singular) is the total
    # stat cap — the engine's `max` is a stack count, so feeding it the total
    # let Rage Longinus reach 80 stacks × 8 = +640 ATK. Divide it out.
    # Blades also name the field after themselves (dragoon_reverse_max_stacks),
    # so any `*_max_stacks` key counts too.
    (re.compile(r"stack(ing)?_(atk|attack)(_bonus)?(_per_hit)?$|stack_per_(hit|win)$"),
     lambda m, v, ab: ({"op": "stacking_buff", "stat": "attack",
                        "per_stack": int(v),
                        "max": _max_stacks(ab, int(v))}, "off")),

    # ── control / pierce ─────────────────────────────────────────────────────
    (re.compile(r"silence_turns$"),
     lambda m, v, ab: ({"op": "silence", "turns": int(v)}, "off")),
    (re.compile(r"ignore_def(ense)?(_pct|_turns)?$"),
     lambda m, v, ab: ({"op": "ignore_defense",
                        "turns": int(v) if m.group(2) == "_turns" else 1}, "off")),
    (re.compile(r"true_damage_turns$"),
     lambda m, v, ab: ({"op": "true_damage_turns", "turns": int(v)}, "off")),
    (re.compile(r"(^|_)true_dmg$|storm_break_true_dmg$|execute_true_dmg$"),
     lambda m, v, ab: ({"op": "true_damage", "value": int(v)}, "off")),
    (re.compile(r"guaranteed_crit|crit_turns$"),
     lambda m, v, ab: ({"op": "guaranteed_crit", "turns": int(v)}, "off")),
    (re.compile(r"crit_(chance|rate)$"),
     lambda m, v, ab: ({"op": "crit_chance",
                        "value": float(v) if float(v) <= 1 else float(v) / 100}, "setup")),
    (re.compile(r"crit_damage(_bonus)?(_pct)?$"),
     lambda m, v, ab: ({"op": "crit_damage",
                        "value": float(v) if float(v) > 1 else 1.5 + float(v)}, "setup")),
    (re.compile(r"pierce(_next_attack|_turns)?$"),
     lambda m, v, ab: ({"op": "ignore_defense", "turns": int(v) if str(v).isdigit() else 1}, "off")),

    # ── damage bonuses ───────────────────────────────────────────────────────
    # A 0–1 value here is a MULTIPLIER, not a flat number — Brave Valkyrie's
    # `damage_boost: 0.4` means "+40% damage" and int() flattened it to a +0
    # bonus, i.e. the whole Last Stand payoff did nothing.
    (re.compile(r"bonus_damage$|damage_boost$|bonus_\w*_damage$|frost_break_bonus$"),
     lambda m, v, ab: ({"op": "bonus_damage_pct", "value": float(v) * 100}, "off")
                      if 0 < float(v) < 1
                      else ({"op": "bonus_damage", "value": int(v)}, "off")),
    # Smash amps are attack-flavoured: they only apply when the owner is
    # actually smashing (Attack / Special), not on Defense or Stamina rounds.
    (re.compile(r"smash_(dmg|damage)_amp(_pct)?$"),
     lambda m, v, ab: ({"op": "bonus_damage_pct",
                        "value": float(v) * 100 if float(v) <= 1 else float(v),
                        "_if": [{"cond": "move_in", "value": ["attack", "special"]}]}, "off")),
    # Generic amps resolve *per move* (bonus_damage_pct), NOT as a persistent
    # dmg_amp stack — a `passive` trigger fires every single round, so stacking
    # amp compounded without limit (+15%/round → +150% by round 10).
    (re.compile(r"(dmg|damage)_amp(_pct)?$"),
     lambda m, v, ab: ({"op": "bonus_damage_pct",
                        "value": float(v) * 100 if float(v) <= 1 else float(v)}, "off")),
    # `special_boost` is a FLAT bonus; a `_pct` field (or a 0–1 value) is a
    # multiplier on the Special instead. Sending "50% more Special damage"
    # through the flat op produced int(0.5) = +0.
    (re.compile(r"special_boost(_per_stack)?$|special_damage_bonus_pct$|condemned_special_boost$"),
     lambda m, v, ab: ({"op": "bonus_damage_pct",
                        "value": float(v) * (100 if float(v) <= 1 else 1),
                        "_if": [{"cond": "move_is", "value": "special"}]}, "off")
                      if (m.group(0).endswith("_pct") or 0 < float(v) < 1)
                      else ({"op": "special_boost", "value": int(v)}, "off")),

    # ── resources ────────────────────────────────────────────────────────────
    # float, not int — the stamina economy runs on a 0–15 scale, so Hollow
    # Deathscyther's 0.5/hit steal rounded straight down to zero.
    (re.compile(r"stamina_(steal|drain)(_per_hit)?$"),
     lambda m, v, ab: ({"op": "drain_stamina", "value": float(v)}, "off")),
    # _vs_<type> variants are CONDITIONAL — they replace the base drain against
    # that enemy type, they do NOT stack on top of it. Emit the delta with an
    # enemy_type_is condition so only the difference applies vs that type.
    (re.compile(r"stamina_(steal|drain)_vs_(\w+)$"),
     lambda m, v, ab: ({"op": "drain_stamina",
                        "value": max(0, int(v) - int(ab.get("stamina_steal",
                                                    ab.get("stamina_drain", 0)) or 0)),
                        "_if": [{"cond": "enemy_type_is", "value": m.group(2).title()}]},
                       "off")),
    # `stamina_per_turn` is always a standing regen. `stamina_gain` depends on
    # the ability's trigger: on a passive it's a per-turn trickle, but on an
    # event trigger ("gains 1 extra Stamina when it wins a defense") it is a
    # one-off. Treating the event form as a setup regen made it pay out every
    # single turn for free — and, because setup regen OVERWRITES, a second
    # ability doing this also silently wiped the first ability's real regen.
    (re.compile(r"stamina_(per_turn)$|bonus_stamina_per_turn$"),
     lambda m, v, ab: ({"op": "stamina_regen", "value": float(v)}, "setup")),
    (re.compile(r"stamina_gain(_per_hit)?$"),
     lambda m, v, ab: (({"op": "stamina_regen", "value": float(v)}, "setup")
                       if str(ab.get("trigger", "passive")) in
                          ("passive", "turn_start", "setup")
                       else ({"op": "gain_stamina", "value": float(v)}, "off"))),
    (re.compile(r"stamina_(drain|cost)_reduction(_pct)?$"),
     lambda m, v, ab: ({"op": "stamina_cost_reduction",
                        "value": float(v) if float(v) <= 1 else float(v) / 100}, "setup")),

    # ── recoil ───────────────────────────────────────────────────────────────
    # Every blade carrying `recoil_damage` describes it as damage dealt TO THE
    # ATTACKER ("its iron wings reflect the force — dealing 15 recoil damage to
    # the attacker"). The `recoil` op subtracts from the OWNER's HP, so these
    # blades were hurting themselves and the enemy took nothing. reflect_flat
    # is no good either — it needs dmg_dealt > 0, which is false on a defense
    # win — so this is a direct hit on the opponent.
    (re.compile(r"recoil_damage$"),
     lambda m, v, ab: ({"op": "true_damage", "value": int(v)}, "off")),
]

# Fields we consciously ignore (report-only): burst/knockout systems, crit
# rates, weird bespoke mechanics that need explicit rules.
UNMAPPED: dict[str, set[str]] = {}   # ability name -> set of fields


# Fields whose names mark them as the PAYOFF of an HP threshold rather than a
# baseline effect. An ability like Garuda's Eternal Flame mixes both — "reduces
# all incoming damage by 20 each phase" (always on) plus "when HP drops below
# 40%, heal 40 and gain +30 DEF" (gated). Applying the threshold ability-wide
# switched the baseline off above the threshold too, so Garuda's -20 only
# worked once it was already nearly dead.
#
# Deliberately narrow. Prefixes like `activation_` / `once_` were tried and
# reverted: on Dead Phoenix and Crimson Spriggan only ONE of several payoff
# fields carries the prefix, so treating the rest as baseline made their
# invulnerability and special boost permanent from turn 1. `surge_` is used
# only where every gated effect is prefixed, which is what makes the split
# safe — extend this list only after checking the same holds for the new
# prefix across the whole roster.
_PAYOFF_PREFIXES = ("surge_",)

# A `*_threshold` field is the GATE. `stat_boost_on_threshold` also ends in
# "_threshold" but is the EFFECT applied at the gate, so it must stay visible
# as a real field — treating it as metadata hid it from the mapper entirely.
def _is_gate_field(f: str) -> bool:
    if f in ("threshold_pct", "threshold_hp_pct"):
        return True
    return f.endswith("_threshold") and "_on_threshold" not in f


# Fields that are standing effects by their very name. A blade that "heals 8
# HP each turn" or "cannot burst" does that from round one; whatever HP gate
# the ability also carries cannot belong to them.
_BASELINE_RE = re.compile(r"(_per_turn$|^passive_|_regen$|_immune$|^cannot_)")


def _threshold_field(ab: dict) -> tuple[str, list[dict]]:
    """The ability's HP gate as ``(field_name, conds)``, or ``("", [])``.

    Any gate-shaped field counts. Values are only accepted in the 0–1 range:
    ``streak_threshold: 3`` and ``max_stack_threshold: 6`` are stack COUNTS,
    not HP fractions, and are handled by _mode_gated instead.
    """
    for f, v in ab.items():
        if not _is_gate_field(f):
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if 0 < v <= 1:
            return f, [{"cond": "hp_below_pct", "value": float(v)}]
    return "", []


def _threshold_conds(ab: dict) -> list[dict]:
    """The HP gate for this ability, or [] if it has none."""
    return _threshold_field(ab)[1]


def _effect_fields(ab: dict) -> list[str]:
    return [k for k in ab if k not in _META and not _is_gate_field(k)]


def _payoff_scope(ab: dict):
    """Predicate marking which fields the ability's HP gate applies to.

    ``None`` means "the whole ability" — every effect only exists below the
    threshold, the correct reading for a pure last-stand ability ("When HP
    falls below 40%, damage +40% and heal 20 HP").

    Scope is derived from the DATA, not from a hardcoded prefix list. The old
    list (`surge_` only) could not see that `dragon_last_stand_threshold`
    scopes `dragon_last_stand_atk` and nothing else, so Imperial Dragon's
    "each Stamina win heals 30 HP" — an always-on baseline — sat locked behind
    40% HP alongside the last stand. Spriggan Requiem's 8 HP/turn regen had
    the same problem.

    Priority, most confident first:
      1. the gate field's own stem  (`demon_mode_threshold` -> `demon_mode_*`)
      2. the legacy `surge_` prefix
      3. presence of an inherently-standing field -> everything else is payoff
    """
    fields = _effect_fields(ab)
    if not fields:
        return None

    stem = _threshold_field(ab)[0]
    if stem.endswith("_threshold"):
        stem = stem[:-len("_threshold")]
    else:
        stem = ""
    if stem and stem != "hp" and any(f.startswith(stem + "_") for f in fields):
        return lambda f, _s=stem + "_": f.startswith(_s)

    if any(f.startswith(_PAYOFF_PREFIXES) for f in fields):
        return lambda f: f.startswith(_PAYOFF_PREFIXES)

    if any(_BASELINE_RE.search(f) for f in fields):
        return lambda f: not _BASELINE_RE.search(f)

    return None


def _has_mixed_payoff(ab: dict) -> bool:
    """True when the ability has BOTH payoff-scoped and baseline fields."""
    pred = _payoff_scope(ab)
    if pred is None:
        return False
    fields = _effect_fields(ab)
    return any(pred(k) for k in fields) and any(not pred(k) for k in fields)


# A chain step and a legacy field can describe the SAME effect, in which case
# both fired and the effect landed twice — Flare Dragon and Void Dragon
# Zephyros applied their burn twice per attack win, Dead Phoenix applied
# invulnerability twice. The chain entry is the better spec: it carries `once`
# and `duration`, which a flat legacy field has no way to express. So fold that
# metadata onto the legacy op and drop the redundant chain step, instead of
# letting two uncoordinated paths apply the same effect.
_CHAIN_DUP: dict = {
    "heal_pct":           ("heal", None),
    "heal":               ("heal", None),
    "defense_boost":      ("buff", "defense"),
    "def_buff":           ("buff", "defense"),
    "attack_boost":       ("buff", "attack"),
    "atk_buff":           ("buff", "attack"),
    "damage_reduction":   ("reduce_damage_flat", None),
    "shield":             ("shield", None),
    "true_damage":        ("true_damage", None),
    "burn":               ("burn", None),
    "invulnerable":       ("invulnerable", None),
    "reflect":            ("reflect_flat", None),
    "special_boost":      ("special_boost", None),
    "special_damage_amp": ("special_boost", None),
    "guaranteed_crit":    ("guaranteed_crit", None),
    "ignore_def":         ("ignore_defense", None),
}


def _dedupe_chain(chain: list, rules: list) -> list:
    """Drop chain steps already covered by a legacy op, keeping their metadata."""
    all_ops = [o for r in rules for o in (r.get("do") or [])]
    kept: list = []
    for step in chain:
        if not isinstance(step, dict):
            kept.append(step)
            continue
        want  = _CHAIN_DUP.get(step.get("effect"))
        match = None
        if want:
            for op in all_ops:
                if op.get("op") == want[0] and (want[1] is None
                                                or op.get("stat") == want[1]):
                    match = op
                    break
        if match is None:
            kept.append(step)
            continue
        if step.get("once"):
            match["_once"] = "battle"
        dur = step.get("duration")
        if isinstance(dur, int) and dur > 0 and "turns" in match:
            if int(match["turns"]) < dur:
                match["turns"] = 99 if dur >= 99 else dur
    return kept


_BEY_TYPES = ("attack", "defense", "stamina", "balance")


def _vs_gate(field: str):
    """Split a `vs_<type>` matchup marker off a field name.

    Returns (condition | None, name_to_match). Handles both orderings used in
    the data — `vs_attack_reflect_pct` and `reflect_vs_attack` — plus
    `_vs_other`, which means "every type this ability does not name".
    """
    for t in _BEY_TYPES:
        for marker in (f"vs_{t}_", f"_vs_{t}"):
            if marker in field:
                return ({"cond": "enemy_type_is", "value": t.title()},
                        field.replace(marker, "_" if marker.endswith("_") else "")
                             .strip("_") or field)
    if "vs_other" in field:
        return ({"cond": "enemy_type_not_in", "value": []},
                field.replace("vs_other_", "").replace("_vs_other", "").strip("_"))
    return None, field


def legacy_convert(ab: dict) -> list[dict]:
    """Convert one legacy flat-field ability dict into a list of rules."""
    name    = ab.get("name", "Ability")
    trigger = ab.get("trigger", "passive")
    if trigger == "on_low_hp":
        trigger = "passive"          # low-HP expressed as condition instead
    conds   = _threshold_conds(ab)
    once    = "battle" if ab.get("once") else None
    # When baseline and payoff effects share one ability, the HP gate moves off
    # the ability and onto just the payoff ops (and those fire once per battle,
    # since "the flames surge — healing 40 HP" is a one-time event, not a heal
    # that repeats every turn the blade sits below the threshold).
    _payoff = _payoff_scope(ab)
    _mixed  = bool(conds) and _has_mixed_payoff(ab)
    _gate   = list(conds) if _mixed else []
    if _mixed:
        conds = []

    off_ops:  list[dict] = []
    def_ops:  list[dict] = []
    setup_ops: list[dict] = []
    def_chance: float | None = None

    for field, value in ab.items():
        if field in _META or not isinstance(value, (int, float, bool)):
            continue
        if value in (0, False):
            continue
        # A `vs_<type>` marker anywhere in the name means the effect only
        # applies against that type. These blades are built around reading the
        # matchup and picking ONE mode, but the marker was never parsed, so
        # every mode fired at once — Tartarus Reaper was simultaneously
        # reducing damage, reflecting, piercing defense AND draining stamina
        # against every opponent regardless of its type.
        _vs_cond, _lookup = _vs_gate(field)
        matched = False
        # ORDER MATTERS: try the FULL field name first, and only fall back to
        # the vs-stripped one if nothing matches. Some fields have a dedicated
        # pattern that already understands the marker — stamina_steal_vs_stamina
        # emits the DELTA over the base steal, because the description reads
        # "the drain doubles to 4" (replace), not "+4 on top" (stack). Stripping
        # first sent it to the generic stamina_steal pattern instead, so Fafnir
        # drained 2 + 4 = 6 against Stamina types.
        for _try in ([field, _lookup] if _lookup != field else [field]):
            for pat, factory in _PATTERNS:
                m = pat.search(_try)
                if m:
                    matched = True
                    break
            if matched:
                break
        if matched:
            for pat, factory in _PATTERNS:
                m = pat.search(_try)
                if not m:
                    continue
                try:
                    op, phase = factory(m, value, ab)
                except (TypeError, ValueError):
                    op = None
                if op:
                    ch = op.pop("_chance", None)
                    if _vs_cond and "_if" not in op:
                        op["_if"] = [_vs_cond]
                    # Every `streak_<effect>` field is part of the "every Nth
                    # time" payoff, not a per-trigger effect. Only three of
                    # them had bespoke patterns, so streak_true_damage and
                    # streak_burn_dmg fired on EVERY trigger instead of every
                    # Nth — Flare Dragon's "every 3rd burn erupts for 35 true
                    # damage" was a flat 35 every single Attack win.
                    if (field.startswith("streak_") and "_if" not in op
                            and op.get("op") != "counter_burst"):
                        op["_if"] = [{"cond": "counter_at_least",
                                      "name": f"{name}_streak",
                                      "value": max(2, int(ab.get("streak_threshold", 3) or 3))}]
                    if _gate and _payoff and _payoff(field) and "_if" not in op:
                        op["_if"]   = list(_gate)
                        op["_once"] = "battle"
                        # A once-per-battle surge that wears off in 2 turns is
                        # a contradiction — these read "for the rest of the
                        # battle" and the chain data agrees (duration 999).
                        if op.get("op") == "buff" and int(op.get("turns", 0)) < 90:
                            op["turns"] = 99
                    if phase == "def":
                        def_ops.append(op)
                        if ch is not None:
                            def_chance = ch
                    elif phase == "setup":
                        setup_ops.append(op)
                    else:
                        off_ops.append(op)
                break
        if not matched:
            UNMAPPED.setdefault(name, set()).add(field)

    # `_vs_other` means "any type this ability does not explicitly name", so it
    # can only be resolved once every field has been seen.
    _named = sorted({t.title() for t in _BEY_TYPES
                     for f in ab
                     if f"vs_{t}_" in f or f"_vs_{t}" in f})
    for _o in off_ops + def_ops + setup_ops:
        for _c in _o.get("_if") or []:
            if _c.get("cond") == "enemy_type_not_in" and not _c.get("value"):
                _c["value"] = _named or ["Attack"]

    rules: list[dict] = []
    # A streak payoff needs the trigger counted before its threshold is tested,
    # otherwise "every 4th defense" pays out on the 5th.
    _streak = next((o for o in off_ops if o.get("op") == "counter_burst"
                    and o.pop("_after", False)), None)
    _sname  = f"{name}_streak"
    _gated  = [o for o in off_ops
               if any(c.get("name") == _sname for c in (o.get("_if") or []))]
    if _streak is not None:
        off_ops.insert(off_ops.index(_streak),
                       {"op": "add_counter", "name": _streak["name"], "amount": 1})
    elif _gated:
        # Streak payoff with no counter_burst to reset it — count the trigger
        # up front and clear the counter once the payoff lands.
        off_ops.insert(0, {"op": "add_counter", "name": _sname, "amount": 1})
        _gated[-1]["_reset_streak"] = _sname
    # Ops carrying their own "_if" become standalone conditional rules
    # (e.g. stamina_steal_vs_stamina → extra drain only vs Stamina types).
    # They are collected separately and appended AFTER the unconditional base
    # rules: a counter gate ("at 6 stacks…") must be evaluated once this turn's
    # stack has already been granted, otherwise every threshold reads one turn
    # stale and "at 6 stacks" really means "on the 7th hit".
    cond_rules: list[dict] = []

    def _extract_conditional(ops: list[dict], when: str) -> None:
        # Ops sharing the SAME gate are emitted as ONE rule. Splitting them
        # per-op meant Garuda's surge heal ran first, lifted HP back over the
        # 40% line, and the +30 DEF half of the same surge then failed its own
        # gate and never applied at all.
        groups: list[tuple[list, list, str | None]] = []
        # An op may carry a proc chance of its own ("15% chance to land an
        # extra hit"). The rule-level `chance` key is the only place the engine
        # reads one, so such an op needs its own rule rather than sharing the
        # unconditional one.
        for op in [o for o in ops if "_chance" in o and "_if" not in o]:
            ops.remove(op)
            _c = op.pop("_chance")
            _oc = op.pop("_once", None)
            cr: dict = {"when": when, "do": [op], "_name": name, "chance": float(_c)}
            if _oc or once:
                cr["once"] = _oc or once
            cond_rules.append(cr)
        for op in [o for o in ops if "_if" in o]:
            ops.remove(op)
            op_if = op.pop("_if")
            _op_chance = op.pop("_chance", None)
            # An op's magnitude is not always under "value" (bonus_special_hits
            # uses "hits", counter_burst uses "damage", ignore_defense/
            # invulnerable use "turns"). Checking only "value" silently dropped
            # those ops after they had been removed from the list — look at
            # whichever magnitude key the op actually carries.
            _mag = next((op[k] for k in ("value", "hits", "damage", "amount",
                                         "per_stack", "turns", "heal", "dmg")
                         if k in op), 0)
            try:
                if float(_mag) <= 0:
                    continue  # delta is zero — base rule already covers it
            except (TypeError, ValueError):
                continue
            _op_once = op.pop("_once", None)
            if _op_chance is not None:
                cr = {"when": when, "if": op_if, "do": [op], "_name": name,
                      "chance": float(_op_chance)}
                if _op_once or once:
                    cr["once"] = _op_once or once
                cond_rules.append(cr)
                continue
            for g_if, g_ops, g_once in groups:
                if g_if == op_if:
                    g_ops.append(op)
                    break
            else:
                groups.append((op_if, [op], _op_once))
        for g_if, g_ops, g_once in groups:
            _rst = next((o.pop("_reset_streak") for o in g_ops
                         if "_reset_streak" in o), None)
            if _rst:
                g_ops.append({"op": "reset_counter", "name": _rst})
            cr: dict = {"when": when, "if": g_if, "do": g_ops, "_name": name}
            if g_once or once:
                cr["once"] = g_once or once
            cond_rules.append(cr)

    _off_when = trigger if trigger in {
        "passive", "on_attack_win", "on_defense_win", "on_stamina_win",
        "on_attack_hit", "on_hit", "on_special", "on_take_damage",
        "on_mirror", "turn_start"} else "passive"
    _extract_conditional(off_ops,   _off_when)
    _extract_conditional(def_ops,   "on_take_damage")
    _extract_conditional(setup_ops, "setup")

    if setup_ops:
        rules.append({"when": "setup", "do": setup_ops, "_name": name})
    if off_ops:
        r: dict = {"when": _off_when,
            "do": off_ops, "_name": name}
        if conds:
            r["if"] = conds
        if once:
            r["once"] = once
        rules.append(r)
    if def_ops:
        r = {"when": "on_take_damage", "do": def_ops, "_name": name}
        if conds:
            r["if"] = conds
        if def_chance is not None:
            r["chance"] = def_chance
        if once:
            r["once"] = once
        rules.append(r)

    # Conditional rules last — see the note on cond_rules above.
    rules.extend(cond_rules)

    # chain passthrough (ChainHandler is already fully generic)
    if isinstance(ab.get("chain"), list):
        _ch = _dedupe_chain(ab["chain"], rules)
        # Folding a chain step's `once` onto a legacy op only takes effect if
        # that op's rule carries it — but promoting it to the whole rule froze
        # every OTHER op sharing that rule too. Brave Valkyrie's "+40% damage
        # below 40% HP" fired exactly once per battle because the 20 HP heal
        # sitting next to it was the one-shot, and Dead Phoenix, Crimson
        # Spriggan, Guilty Longinus, Z Achilles and Garuda all had the same
        # split. So peel the one-shot ops off into their own rule and leave the
        # repeating ones repeating.
        _split: list[dict] = []
        for _r in rules:
            _ops   = _r.get("do") or []
            _once_ops, _keep = [], []
            for _o in _ops:
                (_once_ops if _o.pop("_once", None) else _keep).append(_o)
            if not _once_ops:
                continue
            if not _keep:
                _r["once"] = "battle"
                continue
            _r["do"] = _keep
            _new = {k: v for k, v in _r.items() if k != "_chain"}
            _new["do"]   = _once_ops
            _new["once"] = "battle"
            _split.append(_new)
        rules.extend(_split)
        if _ch:
            if rules:
                rules[0]["_chain"] = _ch
            else:
                rules.append({"when": trigger if trigger != "on_low_hp"
                              else "passive",
                              "do": [], "_name": name, "_chain": _ch})

    return rules
