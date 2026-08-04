"""
copy_abilities.py  —  making a boss copy's abilities actually fire

The problem
-----------
Boss abilities in boss_abilities.py / drakos.py are pure prose: each one is
``{"name", "emoji", "desc"}`` and nothing else. The real mechanics live inside
the boss state machines, hardcoded for the boss AI to drive.

So when a copy was made equippable, its abilities came through as description
text with ``chain: []`` and no ``rules``. Both engines skipped them:

    boss fights  blade_abilities.BladeKit reads flat tuning keys / chain
    PvP          ability_engine reads the "rules" DSL

There is nothing structured to auto-translate — a boss ability has no numbers
attached anywhere a converter could find them. So the mechanics are authored
here, once, per ability, in BOTH dialects. That keeps the two engines agreeing
about what a copy does without either of them importing the other.

Scaling
-------
Everything scales with the copy's own power fraction (`pct` from
boss_copy.describe). A Flawed copy gets a weak echo of the boss's kit and a
Flawless one gets close to the real thing, which is the whole point of grades.
Values are deliberately below the boss's own numbers: the boss reads its stance
with 2-ply lookahead, a player just holds the blade.

Awakening
---------
`awaken_chance` was rolled, stored, and described to the player, but no battle
code ever read it — the ability was pure text. It is now a real proc: a chance
each round to buff every stat for the exchange. AWAKEN_BUFF_PCT is what "full
original power for a moment" is worth.

A note on triggers
------------------
Only use triggers ability_engine.apply() actually dispatches: `passive`, the
threshold pair (`on_low_hp` / `on_high_hp` / `on_low_stamina` /
`on_high_stamina`), the move x result matrix, `on_any_win` / `on_any_loss`,
`on_attack_hit`, `on_special`, `on_mirror`, `on_defend` and `on_take_damage`.
`turn_start` and `turn_end` are listed in TRIGGERS but NOTHING in the codebase
fires them — rules hung on those are as dead as the description text this
module exists to replace. Per-round effects belong on `passive`.
"""

from typing import Optional

# Per-round proc strength for Awakening.
AWAKEN_BUFF_PCT   = 0.35     # +35% to every stat when it wakes
AWAKEN_BUFF_TURNS = 1

# A Flawed copy shouldn't get the same numbers as a Flawless one. The copy's
# power fraction (0–1 of the source blade) scales every authored value, floored
# so even the worst roll does something visible.
MIN_SCALE = 0.45


def _scale(value: float, pct: float) -> float:
    """Scale an authored value by the copy's power PERCENTAGE (0-100).

    This deliberately does not also accept a 0-1 fraction. The dual-unit
    version was ambiguous exactly where it mattered: pct=1 read as 100% while
    pct=1.5 read as 1.5% and collapsed to the floor, so a unit slip anywhere
    upstream would silently flatten every ability to MIN_SCALE instead of
    raising. describe()['pct'] is a percentage; that is the only input.
    """
    try:
        frac = float(pct) / 100.0
    except (TypeError, ValueError):
        frac = MIN_SCALE
    return value * max(MIN_SCALE, min(1.0, frac))


# ── Authored mechanics ────────────────────────────────────────────────────────
# Keyed by (boss key, ability index) — the same index boss_copy stores in
# copy["abilities"], so a copy only gets the mechanics for the abilities its
# loadout actually rolled.
#
#   flat   keys blade_abilities.BladeKit already understands (boss fights)
#   rules  ability_engine DSL (PvP)
#
# Both are written from the same reading of the ability's own description, so
# the two engines land in the same place rather than drifting.

MECHANICS: dict[tuple, dict] = {
    # ── NEMESIS ÆTHERION ──────────────────────────────────────────────────────
    ("nemesis", 0): {
        "for": "Twin Crowns",   # 👑 Twin Crowns — alternating Wrath / Judgement stance
        "flat": {"passive_atk_bonus": 18, "ignore_def_pct": 30,
                 "defense_boost": 18, "reflect_damage": 12},
        "rules": [
            {"when": "on_attack_win",
             "do": [{"op": "ignore_defense", "turns": 1},
                    {"op": "bonus_damage_pct", "value": 15}]},
            {"when": "on_defense_win",
             "do": [{"op": "reflect_flat", "value": 12},
                    {"op": "buff", "stat": "defense", "value": 18, "turns": 2}]},
        ],
    },
    ("nemesis", 1): {
        "for": "Law of Retribution",   # ⚖️ Law of Retribution — banks damage taken, spends it
        "flat": {"passive_dmg_reduction": 10, "bonus_damage": 14},
        "rules": [
            {"when": "on_take_damage",
             "do": [{"op": "stacking_buff", "name": "debt", "stat": "attack",
                     "per_stack": 6, "max": 8}]},
            {"when": "on_special",
             "do": [{"op": "bonus_damage_pct", "value": 30},
                    {"op": "log",
                     "text": "⚖️ **Law of Retribution** — the ledger comes due!"}]},
        ],
    },
    ("nemesis", 2): {
        "for": "Ætheric Ascension",   # ✨ Ætheric Ascension — wakes below 40% HP, once
        "flat": {"passive_atk_bonus": 12, "defense_boost": 12},
        "rules": [
            {"when": "on_low_hp", "once": "battle",
             "if": [{"cond": "hp_below_pct", "value": 0.4}],
             "do": [{"op": "buff_all_pct", "value": 20, "turns": 4},
                    {"op": "log",
                     "text": "✨ **Ætheric Ascension** — both crowns wake!"}]},
        ],
    },

    # ── Aetherion Drakos ──────────────────────────────────────────────────────
    ("drakos", 0): {
        "for": "Astral Ascendance",    # ⭐ Astral Ascendance — Stars build each round
        "flat": {"passive_atk_bonus": 16, "stamina_per_turn": 10},
        "rules": [
            {"when": "passive",
             "do": [{"op": "stacking_buff", "name": "stars", "stat": "attack",
                     "per_stack": 4, "max": 6}]},
            {"when": "on_take_damage",
             "do": [{"op": "reset_counter", "name": "stars"}]},
        ],
    },
    ("drakos", 1): {
        "for": "Crystalline Aegis",    # 🛡️ Crystalline Aegis — layers absorb, then shatter
        # Only ONE reduction key here. BladeKit maps knockout_resistance_pct
        # onto the same `reduction` accumulator as passive_dmg_reduction, so
        # listing both silently summed to 30% when 22% was intended.
        "flat": {"passive_dmg_reduction": 22},
        "rules": [
            {"when": "on_take_damage",
             "do": [{"op": "reduce_damage_pct", "value": 35}]},
            {"when": "passive", "chance": 0.25,
             "do": [{"op": "shield", "value": 30},
                    {"op": "log",
                     "text": "🛡️ **Crystalline Aegis** — a layer regrows."}]},
        ],
    },
}


def _awaken_rule(chance: float) -> dict:
    return {
        "when": "passive",
        "chance": max(0.0, min(1.0, float(chance or 0.0))),
        "do": [
            {"op": "buff_all_pct", "value": int(AWAKEN_BUFF_PCT * 100),
             "turns": AWAKEN_BUFF_TURNS},
            {"op": "log",
             "text": "✨ **Awakening** — the original stirs inside the shell!"},
        ],
    }


def _awaken_flat(chance: float) -> dict:
    """Boss-fight dialect. BladeKit has no proc system, so the buff is folded
    into a flat expectation: chance × strength, which is what the proc is worth
    per round on average. Overstating it here would make a copy stronger in a
    boss fight than in PvP for the same ability text."""
    expected = max(0.0, min(1.0, float(chance or 0.0))) * AWAKEN_BUFF_PCT
    return {
        "passive_atk_bonus": round(expected * 100),
        "defense_boost":     round(expected * 100),
    }


def build(copy: dict, profile_src: dict, pct: float) -> list[dict]:
    """Ability dicts for a copy, carrying mechanics for BOTH engines.

    `pct` is the copy's power as a percentage of the original (describe()['pct']).
    Returns the list to put on the blade dict's "abilities" key.
    """
    src_key   = copy.get("source")
    src_abils = profile_src.get("abilities") or []
    out: list[dict] = []

    for idx in copy.get("abilities") or []:
        if idx >= len(src_abils):
            continue
        src  = src_abils[idx]
        mech = MECHANICS.get((src_key, idx))
        ab: dict = {
            "name":        src.get("name", "Ability"),
            "trigger":     "passive",
            "description": src.get("desc", ""),
            "chain":       [],
        }
        if mech:
            for key, value in (mech.get("flat") or {}).items():
                scaled = _scale(float(value), pct)
                # Flat point bonuses round to int; BladeKit ignores anything
                # that isn't a positive number.
                ab[key] = max(1, int(round(scaled)))
            rules = []
            for rule in mech.get("rules") or []:
                rules.append(_scale_rule(rule, pct))
            if rules:
                ab["rules"] = rules
        out.append(ab)

    if copy.get("awakening"):
        chance = copy.get("awaken_chance", 0.0)
        ab = {
            "name":    "Awakening",
            "trigger": "passive",
            "description": (
                f"An echo of the original still sleeps in this shell. Each round "
                f"there is a {chance * 100:.0f}% chance it wakes, restoring the "
                f"source blade's full power until the exchange ends."
            ),
            "chain": [],
            "rules": [_awaken_rule(chance)],
        }
        ab.update(_awaken_flat(chance))
        out.append(ab)

    return out


def _scale_rule(rule: dict, pct: float) -> dict:
    """Copy a rule with its numeric payload scaled to the copy's power.

    `chance`, `turns` and `max` are left alone — scaling those turns a clean
    "25% chance" into noise and makes stack caps grade-dependent in a way the
    ability text never promised.
    """
    out = dict(rule)
    scaled_ops = []
    for op in rule.get("do") or []:
        op = dict(op)
        for field in ("value", "per_stack", "per_stack_pct"):
            v = op.get(field)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                # ignore_defense/true_damage_turns use `value` as a duration
                if op.get("op") in ("ignore_defense", "true_damage_turns"):
                    continue
                op[field] = max(1, int(round(_scale(float(v), pct))))
        scaled_ops.append(op)
    out["do"] = scaled_ops
    return out


def summary(copy: dict, profile_src: dict) -> list[str]:
    """Which of this copy's abilities actually do something, for the card."""
    src_key   = copy.get("source")
    src_abils = profile_src.get("abilities") or []
    live = []
    for idx in copy.get("abilities") or []:
        if idx < len(src_abils) and (src_key, idx) in MECHANICS:
            live.append(src_abils[idx].get("name", "?"))
    if copy.get("awakening"):
        live.append("Awakening")
    return live


def verify() -> list[str]:
    """Check every MECHANICS entry still points at the ability it was written
    for, and that no boss ability is left without mechanics.

    The table is keyed by ability INDEX because that is what a copy stores in
    copy["abilities"]. Indexes are positional, so inserting or reordering an
    ability in boss_abilities.py / drakos.py would silently re-point authored
    mechanics at the wrong ability — Crystalline Aegis's damage reduction
    landing on Astral Ascendance, with nothing raising. The "for" name is the
    tripwire: a mismatch is reported instead of shipped.
    """
    from . import boss_info
    problems = []
    for (bkey, idx), mech in sorted(MECHANICS.items()):
        prof  = boss_info.REGISTRY.get(bkey)
        if prof is None:
            problems.append(f"{bkey}[{idx}]: no such boss")
            continue
        abils = prof.get("abilities") or []
        if idx >= len(abils):
            problems.append(f"{bkey}[{idx}]: index past {len(abils)} abilities")
            continue
        want, got = mech.get("for"), abils[idx].get("name")
        if want and want != got:
            problems.append(f"{bkey}[{idx}]: authored for {want!r}, now {got!r}")
    for bkey, prof in boss_info.REGISTRY.items():
        for i, a in enumerate(prof.get("abilities") or []):
            if (bkey, i) not in MECHANICS:
                problems.append(
                    f"{bkey}[{i}] {a.get('name')!r}: no mechanics — text only")
    return problems


_problems = verify()
if _problems:
    import logging
    _log = logging.getLogger("beyblade_bot")
    for _p in _problems:
        _log.warning(f"[copy_abilities] {_p}")
