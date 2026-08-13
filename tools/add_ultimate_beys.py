#!/usr/bin/env python3
"""
tools/add_ultimate_beys.py — Ultimate Valkyrie, and its Black Edition.

Ultimate Valkyrie is the top of the Valkyrie line: 165 Attack behind a 40
Defence. It hits harder than anything else in the roster and it cannot afford
to be hit back, which is the whole design.

Its ability, ULTIMATE BLADE, is deliberately two-sided. It is an offence
ability — every landed hit carries a percentage bonus and the crit rate is
raised — but until the blade is mastered, swinging that hard costs extra
stamina on Attack and Special. At **bey level 100** the surcharge is gone and
only the offence remains. That is expressed with the `bey_level_below`
condition (the same mechanism Void Longinus uses to awaken) gating a
`stamina_cost_increase` op, rather than by two separate blades.

`stamina_cost_increase` is new for this blade — see the note in
cogs/abilities/ability_engine.py. The existing `stamina_cost_reduction`
clamps to 0.0–0.9 and can only make costs *cheaper*, so it cannot express a
drawback at all: a negative value there reads as zero and the ability would
have looked correct in `;info` while doing nothing in a battle.

The Black Edition is the same blade with the same ability name and stronger
numbers on every part of it — a bigger hit bonus, a higher crit rate, harder
crits, and a noticeably lighter stamina surcharge. It is booster-exclusive AND
carries `hidden_drop_one_in`, which takes it out of the weighted booster pool
entirely and gives it an independent per-pack roll instead. See
cogs/economy/shop.py for how that is rolled and why the number is never shown.

Idempotent: re-running replaces these two by name rather than appending.

Run:  python3 tools/add_ultimate_beys.py [--check]
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "beyblades.json")

# Both blades share one statline — the user asked for the Black Edition to be
# "same stats", so the difference between them is entirely in the effects.
ULT_STATS = {"attack": 165, "defense": 40, "stamina": 117, "special": 165,
             "hp": 134}

BEYS = [
    {
        "id": "BB088",
        "name": "Ultimate Valkyrie",
        "rarity": "Ultimate",
        "type": "Attack",
        "spin_direction": "Right",
        "burst_height": "High",
        "image_url": "https://cdn.discordapp.com/attachments/1510856884943454208/1537151067119427594/1786554299801.png?ex=6a7dfefd&is=6a7cad7d&hm=691e6d6cbdc6728e5f6a104e43719939724adefeff80b71d6a03521f7f40d90b&",
        "description": (
            "The Valkyrie line taken as far as it goes. Every gram of it is "
            "pointed forward — 165 Attack over a 40 Defence, a blade that has "
            "no answer to being hit and does not intend to need one. Swung "
            "unmastered it burns through its own spin to do it."),
        "stats": dict(ULT_STATS),
        "ability": {
            "name": "Ultimate Blade",
            "trigger": "on_attack_hit",
            "description": (
                "Pure offence. Every Attack that lands hits for +20%, every "
                "hit of the Special for +15%, and the crit rate is raised by "
                "15%. The cost: until Ultimate Valkyrie is mastered, Attack "
                "and Special each drain **35% more stamina**. At **bey level "
                "100** the blade is fully awakened and that surcharge is "
                "removed — the offence stays."),
            "rules": [
                {
                    "when": "on_attack_hit",
                    "do": [{"op": "bonus_damage_pct", "value": 20}],
                    "_name": "Ultimate Blade",
                },
                {
                    "when": "on_hit",
                    "do": [{"op": "bonus_damage_pct", "value": 15}],
                    "_name": "Ultimate Blade",
                    "_note": "on_hit is the per-Special-hit trigger; "
                             "on_attack_hit is the normal-attack one. They are "
                             "disjoint, so this does not stack with the rule "
                             "above on a single swing.",
                },
                {
                    "when": "setup",
                    "do": [{"op": "crit_chance", "value": 15}],
                    "_name": "Ultimate Blade",
                },
                {
                    "when": "setup",
                    "if": [{"cond": "bey_level_below", "value": 100}],
                    "do": [{"op": "stamina_cost_increase", "value": 35,
                            "moves": ["attack", "special"]}],
                    "_name": "Ultimate Blade — unmastered",
                    "_note": "The drawback, and ONLY the drawback, is gated on "
                             "level. At 100 this rule stops firing and the "
                             "offence rules above are untouched.",
                },
            ],
        },
        "special_move": {
            "name": "Ultimate V",
            "description": (
                "Five cuts along the same line, carved into the shape of a V. "
                "The guard is not gone through — it is gone past."),
            "hits": 5,
            "damage_per_hit": 37,
            "total_damage": 185,
            "flavour_texts": [
                "⚡ **ULTIMATE V** — five cuts down the same line!",
                "🗡️ The V closes — there is nothing left to guard with!",
            ],
        },
        "special_rules": [
            {
                "when": "on_special",
                "do": [{"op": "ignore_defense", "turns": 1}],
                "_name": "Ultimate V",
            },
        ],
    },
    {
        "id": "BB089",
        "name": "Ultimate Valkyrie (Black Edition)",
        "rarity": "Ultimate",
        "type": "Attack",
        "spin_direction": "Right",
        "burst_height": "High",
        "booster_exclusive": True,
        # Not a weight — an independent 1-in-N roll, and it is never shown to
        # a player. shop.py reads this key; nothing renders it.
        "hidden_drop_one_in": 5_000_000,
        "image_url": "https://cdn.discordapp.com/attachments/1510856884943454208/1537064143088124014/1786534758260.png?ex=6a7e56c8&is=6a7d0548&hm=58e0e3a8f054595fb7e4fbc2c806720073e55da8fdf4f0acb70a55192c01df99&",
        "description": (
            "The same blade, finished in black. Identical on the scales and "
            "not remotely identical in the stadium — the layer is tuned so "
            "hard that it barely pays for the privilege. Almost nobody has "
            "one."),
        "stats": dict(ULT_STATS),
        "ability": {
            "name": "Ultimate Blade",
            "trigger": "on_attack_hit",
            "description": (
                "The same offence, sharpened. Every Attack that lands hits "
                "for +28%, every hit of the Special for +22%, the crit rate "
                "is raised by 22% and crits land at 1.8x instead of 1.5x. The "
                "black layer is efficient with it too: the unmastered stamina "
                "surcharge on Attack and Special is only **20%**, and it is "
                "still removed entirely at **bey level 100**."),
            "rules": [
                {
                    "when": "on_attack_hit",
                    "do": [{"op": "bonus_damage_pct", "value": 28}],
                    "_name": "Ultimate Blade",
                },
                {
                    "when": "on_hit",
                    "do": [{"op": "bonus_damage_pct", "value": 22}],
                    "_name": "Ultimate Blade",
                },
                {
                    "when": "setup",
                    "do": [
                        {"op": "crit_chance", "value": 22},
                        {"op": "crit_damage", "value": 1.8},
                    ],
                    "_name": "Ultimate Blade",
                },
                {
                    "when": "setup",
                    "if": [{"cond": "bey_level_below", "value": 100}],
                    "do": [{"op": "stamina_cost_increase", "value": 20,
                            "moves": ["attack", "special"]}],
                    "_name": "Ultimate Blade — unmastered",
                },
            ],
        },
        "special_move": {
            "name": "Ultimate Wing V",
            "description": (
                "The V opens into a wing. Five cuts that go past the guard "
                "and cannot be stepped out of."),
            "hits": 5,
            "damage_per_hit": 38,
            "total_damage": 190,
            "flavour_texts": [
                "🖤 **ULTIMATE WING V** — the black wing opens!",
                "⚡ Five cuts, no guard, nowhere to go!",
            ],
        },
        "special_rules": [
            {
                "when": "on_special",
                "do": [
                    {"op": "ignore_defense", "turns": 1},
                    {"op": "undodgeable", "turns": 1},
                ],
                "_name": "Ultimate Wing V",
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

    `special_rules` is folded into the same ability entry rather than becoming
    a second ability, because it is the Special's own behaviour and should not
    show up in `;info` as an ability the blade does not have.
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
        tag = " (booster only)" if b.get("booster_exclusive") else ""
        if b.get("hidden_drop_one_in"):
            tag += f" (hidden 1-in-{b['hidden_drop_one_in']:,})"
        print(f"{b['name']:34} {b['rarity']:9} {b['type']:7} "
              f"atk {s['attack']:3} def {s['defense']:3} sta {s['stamina']:3} "
              f"spc {s['special']:3} hp {s['hp']:3}{tag}")
        sm = b["special_move"]
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
