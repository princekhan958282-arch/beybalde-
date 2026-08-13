#!/usr/bin/env python3
"""
tools/add_mythic_beys.py — Shining Shuriken and Blood Dragon.

Two Mythic Attack blades that attack in opposite ways.

**Shining Shuriken** does not want the first move. Behind 128 Defence and the
lowest HP pool in the roster it punishes contact: every normal Attack that
lands on it costs the attacker 50 HP, a point of stamina and three stability.

The counter is deliberately gated on `incoming_move_is: attack`. Under
`on_take_damage` the engine fires the rule once **per hit**, and a Special is
many hits — an ungated Counter Point would return 800 HP against a sixteen-hit
Rush Launch and read, in the JSON, exactly like the version that does not.
Firing it once per Attack, plus once when Shuriken Strike is used, is the
ability as specified.

**Blood Dragon** is the opposite: 158 Attack over 29 Defence, and an ability
that makes it stronger and more expensive at the same time. Unstable Attack
banks +10 Attack per hit to 10 stacks, and every one of those stacks also adds
0.2 stamina to the cost of every Attack and Special it makes afterwards. At
full stacks that is +100 Attack and an Attack that costs 4.2 stamina instead
of 2.2 — it wins fast or it seizes up.

Blood Claw adds Blood Dragon's full Attack stat to its 170 base and charges 3
more stamina on top of the Special's own cost.

Three ops carry this and are documented at their definitions in
cogs/abilities/ability_engine.py: `stamina_cost_increase`'s stacking flat form,
`bonus_damage_stat`, and `drain_stamina`'s `steal: false`.

Idempotent: re-running replaces these two by name rather than appending.

Run:  python3 tools/add_mythic_beys.py [--check]
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "beyblades.json")

# The counter, written once. Shining Shuriken applies it from two places —
# when it is hit, and when it fires its own Special — and the two must not
# drift apart.
COUNTER_POINT = [
    {"op": "true_damage", "value": 50},
    # steal:false — the enemy loses the point, Shuriken does not gain it.
    {"op": "drain_stamina", "value": 1, "steal": False},
    {"op": "enemy_lose_stability", "amount": 3},
]

# Mythic medians: hp 118 · atk 115 · def 80 · sta 100 · spc 125
BEYS = [
    {
        "id": "BB090",
        "name": "Shining Shuriken",
        "rarity": "Mythic",
        "type": "Attack",
        "spin_direction": "Right",
        "burst_height": "Mid",
        "image_url": "https://cdn.discordapp.com/attachments/1510856884943454208/1537309156535570472/1786592956078.png?ex=6a7e9238&is=6a7d40b8&hm=14cdb4ae8081f01b330be92bf253b14bd38c84ad8a1b362982789ebcb81e798c&",
        "description": (
            "A blade with almost nothing to lose and everything to give back. "
            "Shining Shuriken carries the thinnest HP pool in the roster "
            "behind a heavy 128 Defence, and answers contact rather than "
            "opening it — every blow that lands on it comes back sharper "
            "than it went in."),
        "stats": {"attack": 148, "defense": 128, "stamina": 77, "special": 125,
                  "hp": 69},
        "ability": {
            "name": "Counter Point",
            "trigger": "on_take_damage",
            "description": (
                "Every normal Attack that lands on Shining Shuriken is paid "
                "for: the attacker takes **50 true damage**, loses **1 "
                "stamina** and **3 stability**. It answers Attacks, not "
                "Specials — a multi-hit Special connects without waking it. "
                "Shuriken Strike fires the same counter on demand."),
            "rules": [
                {
                    "when": "on_take_damage",
                    "if": [{"cond": "incoming_move_is", "value": "attack"}],
                    "do": list(COUNTER_POINT),
                    "_name": "Counter Point",
                    "_note": "on_take_damage fires once PER HIT. Without the "
                             "incoming_move_is gate this returns 50 damage "
                             "sixteen times against a sixteen-hit Special.",
                },
            ],
        },
        "special_move": {
            "name": "Shuriken Strike",
            "description": (
                "The blade opens up and throws every edge it has at once. "
                "What survives the volley is 40% weaker for it, and takes the "
                "counter on the way out."),
            "hits": 3,
            "damage_per_hit": 45,
            "total_damage": 135,
            "flavour_texts": [
                "✴️ **SHURIKEN STRIKE** — every edge at once!",
                "🩸 The volley lands — and the counter lands with it!",
            ],
        },
        "special_rules": [
            {
                "when": "on_special",
                "do": ([{"op": "enemy_debuff_pct", "stat": "attack",
                         "value": 40, "turns": 3}] + list(COUNTER_POINT)),
                "_name": "Shuriken Strike",
            },
        ],
    },
    {
        "id": "BB091",
        "name": "Blood Dragon",
        "rarity": "Mythic",
        "type": "Attack",
        "spin_direction": "Right",
        "burst_height": "High",
        "image_url": "https://cdn.discordapp.com/attachments/1510856884943454208/1537309675605729361/Firefly.png?ex=6a7e92b4&is=6a7d4134&hm=96f0e86852fe220a3f33a1d9c73011b98c90ab388927041809b05e48f55771b0&",
        "description": (
            "158 Attack over 29 Defence, and no interest in a long fight. "
            "Blood Dragon feeds on its own momentum — every hit it lands "
            "makes the next one heavier and the one after that more "
            "expensive. It either finishes the job or grinds itself to a "
            "halt trying."),
        "stats": {"attack": 158, "defense": 29, "stamina": 81, "special": 125,
                  "hp": 111},
        "ability": {
            "name": "Unstable Attack",
            "trigger": "on_hit",
            "description": (
                "Every hit banks a stack, to a maximum of 10. Each stack is "
                "worth **+10 Attack** — and **+0.2 stamina** on every Attack "
                "and Special Blood Dragon makes from then on. At full stacks "
                "it swings with +100 Attack and pays 4.2 stamina an Attack "
                "instead of 2.2."),
            "rules": [
                {
                    "when": "on_attack_hit",
                    "do": [
                        {"op": "stacking_buff", "stat": "attack",
                         "per_stack": 10, "max": 10, "name": "unstable"},
                        {"op": "stamina_cost_increase", "flat_per_stack": 0.2,
                         "max": 10, "name": "unstable_cost",
                         "moves": ["attack", "special"]},
                    ],
                    "_name": "Unstable Attack",
                },
                {
                    "when": "on_hit",
                    "do": [
                        {"op": "stacking_buff", "stat": "attack",
                         "per_stack": 10, "max": 10, "name": "unstable"},
                        {"op": "stamina_cost_increase", "flat_per_stack": 0.2,
                         "max": 10, "name": "unstable_cost",
                         "moves": ["attack", "special"]},
                    ],
                    "_name": "Unstable Attack",
                    "_note": "on_hit is the per-Special-hit trigger and "
                             "on_attack_hit the normal-attack one; they are "
                             "disjoint, so a swing banks exactly one stack.",
                },
            ],
        },
        "special_move": {
            "name": "Blood Claw",
            "description": (
                "The claw closes on everything Blood Dragon has left. It "
                "lands for its printed damage plus its entire Attack stat, "
                "and takes three more stamina than the Special already "
                "costs."),
            "hits": 2,
            "damage_per_hit": 85,
            "total_damage": 170,
            "flavour_texts": [
                "🩸 **BLOOD CLAW** — everything it has, all at once!",
                "🐉 The claw closes — Blood Dragon spends itself to land it!",
            ],
        },
        "special_rules": [
            {
                "when": "on_special",
                "do": [
                    {"op": "bonus_damage_stat", "stat": "attack", "scale": 1.0},
                    # Negative gain_stamina IS the self-drain — there is no
                    # separate self-cost op, and the manager's surcharge only
                    # reaches per-move costs, not a one-off charge like this.
                    {"op": "gain_stamina", "value": -3},
                ],
                "_name": "Blood Claw",
            },
        ],
    },
]

NAMES = {b["name"] for b in BEYS}


def build(entry: dict) -> dict:
    """One blade, with `abilities` mirrored from `ability`.

    The engine runs `abilities` — the PLURAL list — via
    AbilityEngine._rules_for; the singular `ability` is display only. A blade
    authored with just the singular looks perfect in `;info` and its ability
    never fires once in a battle.

    `special_rules` folds into the same ability entry rather than becoming a
    second ability, so `;info` does not show an ability the blade does not
    have.
    """
    out = dict(entry)
    special_rules = out.pop("special_rules", [])
    ability = dict(out["ability"])
    ability["rules"] = list(ability.get("rules", [])) + [
        dict(r) for r in special_rules]
    out["abilities"] = [ability]
    return out


def main() -> int:
    check_only = "--check" in sys.argv
    with open(DATA, encoding="utf-8") as fh:
        doc = json.load(fh)

    if isinstance(doc, dict) and "beyblades" in doc:
        shape, blades = "wrapped", doc["beyblades"]
    elif isinstance(doc, list):
        shape, blades = "list", doc
    else:
        shape, blades = "by_name", doc
    listy = isinstance(blades, list)
    rows = blades if listy else list(blades.values())

    taken = {b.get("id") for b in rows if b.get("name") not in NAMES}
    clash = [b["id"] for b in BEYS if b["id"] in taken]
    if clash:
        print(f"ID collision: {clash}")
        return 1

    kept = [b for b in rows if b.get("name") not in NAMES]
    out = kept + [build(b) for b in BEYS]

    for b in BEYS:
        s = b["stats"]
        print(f"{b['name']:18} {b['rarity']:7} {b['type']:7} "
              f"atk {s['attack']:3} def {s['defense']:3} sta {s['stamina']:3} "
              f"spc {s['special']:3} hp {s['hp']:3}")
        sm = b["special_move"]
        print(f"    {b['ability']['name']:16} ({b['ability']['trigger']})")
        print(f"    {sm['name']:16} {sm['hits']}x{sm['damage_per_hit']} "
              f"= {sm['total_damage']}")
    print(f"\nroster {len(rows)} -> {len(out)}")

    if check_only:
        print("check only — nothing written")
        return 0

    if shape == "wrapped":
        doc["beyblades"] = out if listy else {b["name"]: b for b in out}
    elif shape == "list":
        doc = out
    else:
        doc = {b["name"]: b for b in out}

    with open(DATA, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"wrote {DATA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
