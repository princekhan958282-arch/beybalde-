#!/usr/bin/env python3
"""
tools/add_booster_beys.py — the two X boosters and Master Diabolos.

Victory Valkyrie X and Storm Spriggan X are Epic upgrades of the Rare starters:
same identity, same ability and special by name, stronger numbers and markedly
stronger effects. They are `booster_exclusive`, so `;start` and spawns cannot
produce them — the whole point of an upgrade is that you go and get it.

Master Diabolos is a Legendary DUAL-SPIN blade. It carries two complete
statlines and two Specials, and the player chooses which one it fights in with
buttons on `;info`. Right is the aggressive mode, Left the defensive one.

Idempotent: re-running replaces these three by name rather than appending.

Run:  python3 tools/add_booster_beys.py [--check]
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "beyblades.json")

# Epic medians:      hp 101 · atk 100 · def  70 · sta  80 · spc 105
# Legendary medians: hp 116 · atk 102 · def 101 · sta 101 · spc 112
BEYS = [
    {
        "id": "BB085",
        "name": "Victory Valkyrie X",
        "rarity": "Epic",
        "type": "Attack",
        "spin_direction": "Right",
        "burst_height": "High",
        "booster_exclusive": True,
        "image_url": "https://cdn.discordapp.com/attachments/1510856884943454208/1537064111081267251/VictoryValkyrie_2-60RA.webp?ex=6a7dae01&is=6a7c5c81&hm=c05bcf547525a05d41b7c0c12094905d3dd7fb7e9976402aaca4cde1d735caab&",
        "description": (
            "The same relentless forward rush, rebuilt. Valkyrie X keeps its "
            "guard down and its foot down — the flurry lands harder, lands "
            "more often, and every impact is looking for a crit."),
        "stats": {"attack": 110, "defense": 58, "stamina": 82, "special": 96,
                  "hp": 108},
        "ability": {
            "name": "Energy Layer X",
            "trigger": "on_attack_win",
            "description": (
                "A tighter layer. Every Attack won winds on +10 Attack, up to "
                "5 stacks, and firing the Special sharpens the whole flurry to "
                "a +35% crit chance — with the crits themselves landing at "
                "1.8x instead of the usual 1.5x."),
            "rules": [
                {
                    "when": "on_attack_win",
                    "do": [{"op": "stacking_buff", "stat": "attack",
                            "per_stack": 10, "max": 5, "name": "energy_x"}],
                    "_name": "Energy Layer X",
                },
                {
                    "when": "on_special",
                    "do": [
                        {"op": "crit_chance", "value": 35},
                        {"op": "crit_damage", "value": 1.8},
                    ],
                    "_name": "Energy Layer X",
                },
            ],
        },
        "special_move": {
            "name": "Rush Launch X",
            "description": (
                "Sixteen impacts again — but each one carries half again the "
                "force, and every single one can crit."),
            "hits": 16,
            "damage_per_hit": 15,
            "total_damage": 240,
            "flavour_texts": [
                "⚔️ **Rush Launch X** — sixteen hits and not one of them soft!",
            ],
        },
    },
    {
        "id": "BB086",
        "name": "Storm Spriggan X",
        "rarity": "Epic",
        "type": "Balance",
        "spin_direction": "Right",
        "burst_height": "Mid",
        "booster_exclusive": True,
        "image_url": "https://cdn.discordapp.com/attachments/1510856884943454208/1537064123685281792/StormSpriggan_2-70M.webp?ex=6a7dae04&is=6a7c5c84&hm=a56961f6fed02a4aa575fd056f1189f9a40f5d8b8da2d2bad9efa47589de1d6d&",
        "description": (
            "The counter, perfected. Spriggan X does not merely return what it "
            "is hit with — it returns more of it, and walks away behind a "
            "guard built out of the exchange."),
        "stats": {"attack": 90, "defense": 94, "stamina": 90, "special": 102,
                  "hp": 118},
        "ability": {
            "name": "Spriggan Layer X",
            "trigger": "on_any_win",
            "description": (
                "Any exchange won mends 16 HP. Counter Break X returns 150% of "
                "the next hit taken instead of 100%, and the exchange leaves "
                "Spriggan X behind a 40 HP guard — it still takes the hit, but "
                "it is ready for the one after."),
            "rules": [
                {
                    "when": "on_any_win",
                    "do": [{"op": "heal", "value": 16}],
                    "_name": "Spriggan Layer X",
                },
                {
                    "when": "on_special",
                    "do": [
                        {"op": "set_mode", "name": "counter_break"},
                        {"op": "log",
                         "text": "🌀 **Counter Break X** — Spriggan X opens its "
                                 "guard and dares them."},
                    ],
                    "_name": "Counter Break X",
                },
                {
                    "when": "on_take_damage",
                    "if": [{"cond": "mode_is", "value": "counter_break"}],
                    "do": [
                        {"op": "reflect_pct", "value": 150},
                        {"op": "shield", "value": 40},
                        {"op": "set_mode", "name": "spent"},
                        {"op": "log",
                         "text": "🪞 **Counter Break X** — returned with "
                                 "interest!"},
                    ],
                    "_name": "Counter Break X",
                },
            ],
        },
        "special_move": {
            "name": "Counter Break X",
            "description": (
                "Spriggan X takes the exchange and gives back half again. The "
                "next hit it takes is reflected at 150% and leaves it shielded "
                "— and it still takes the hit. Armed only by the Special."),
            "hits": 1,
            "damage_per_hit": 165,
            "total_damage": 165,
            "flavour_texts": [
                "⚖️ **Counter Break X** — the whole battle, turned around!",
            ],
        },
    },
    {
        "id": "BB087",
        "name": "Master Diabolos",
        "rarity": "Legendary",
        "type": "Balance",
        "spin_direction": "Dual",
        "burst_height": "Mid",
        "dual_spin": True,
        "image_url": "https://cdn.discordapp.com/attachments/1510856884943454208/1513099327730487459/BBGT_Master_Diabolos_Generate_Beyblade.webp?ex=6a7d820f&is=6a7c308f&hm=050817ba75eecbca108e0a48d37a02f23488d76c77c7b62f455c975a995714f7",
        "description": (
            "A layer built to be flipped. Mounted one way Diabolos spins right "
            "and hunts; turned over it spins left and endures. Two blades in "
            "one shell, and the blader picks which one turns up."),
        # The default view is Right mode. `spin_modes` below is the truth, and
        # utils.spin_mode swaps the chosen one into these fields — this pair
        # exists so every reader that has never heard of dual spin still sees a
        # complete, valid blade.
        "stats": {"attack": 136, "defense": 100, "stamina": 103, "special": 120,
                  "hp": 110},
        "spin_modes": {
            "Right": {
                "label": "Right Mode — Assault",
                "spin_direction": "Right",
                "type": "Attack",
                "stats": {"attack": 136, "defense": 100, "stamina": 103,
                          "special": 120, "hp": 110},
                "special_move": {
                    "name": "Master Smash",
                    "description": (
                        "Diabolos comes down from above — one descending blow "
                        "meant to end the round where it lands."),
                    "hits": 2,
                    "damage_per_hit": [95, 60],
                    "total_damage": 155,
                    "flavour_texts": [
                        "🔥 **Master Smash** — Diabolos drops the hammer!",
                    ],
                },
            },
            "Left": {
                "label": "Left Mode — Generate",
                "spin_direction": "Left",
                "type": "Defense",
                "stats": {"attack": 118, "defense": 115, "stamina": 106,
                          "special": 112, "hp": 110},
                "special_move": {
                    "name": "Master Upper",
                    "description": (
                        "Diabolos digs in and drives upward, lifting the "
                        "opponent clean off their axis."),
                    "hits": 3,
                    "damage_per_hit": 48,
                    "total_damage": 144,
                    "flavour_texts": [
                        "🌪️ **Master Upper** — Diabolos throws them skyward!",
                    ],
                },
            },
        },
        "ability": {
            "name": "Flippable Master Layer",
            "trigger": "on_any_win",
            "description": (
                "The layer reads the exchange. Every win banks a Master stack "
                "worth +8 Attack and +8 Defence, to 4 stacks — Diabolos is "
                "rewarded for whatever it happens to be good at that round."),
            "rules": [
                {
                    "when": "on_any_win",
                    "do": [
                        {"op": "stacking_buff", "stat": "attack",
                         "per_stack": 8, "max": 4, "name": "master"},
                        {"op": "stacking_buff", "stat": "defense",
                         "per_stack": 8, "max": 4, "name": "master_def"},
                    ],
                    "_name": "Flippable Master Layer",
                },
            ],
        },
        "abilities_extra": [
            {
                "name": "Dual-Spin Capability",
                "trigger": "on_hit",
                "description": (
                    "Spinning against the grain tears at the opponent. When "
                    "Diabolos and its opponent turn in OPPOSITE directions, "
                    "every hit drains 1.5 stamina and lands 12 extra damage; "
                    "spinning the same way instead, Diabolos holds its own "
                    "spin and recovers 1 stamina a round."),
                "rules": [
                    {
                        "when": "on_attack_win",
                        "if": [{"cond": "opposite_spin"}],
                        "do": [
                            {"op": "drain_stamina", "value": 1.5},
                            {"op": "bonus_damage", "value": 12},
                        ],
                        "_name": "Dual-Spin Capability",
                    },
                    {
                        "when": "on_attack_win",
                        "if": [{"cond": "same_spin"}],
                        "do": [{"op": "gain_stamina", "value": 1}],
                        "_name": "Dual-Spin Capability",
                    },
                ],
            },
        ],
    },
]

NAMES = {b["name"] for b in BEYS}


def build(entry: dict) -> dict:
    """One blade, with `abilities` mirrored from `ability` (+ any extras).

    The engine runs `abilities` — the PLURAL list — via
    AbilityEngine._rules_for; the singular `ability` is display only. A blade
    authored with just the singular looks perfect in `;info` and its ability
    never fires once in a battle, which is why the mirroring is done here
    rather than left to be remembered per blade.
    """
    out = dict(entry)
    extra = out.pop("abilities_extra", [])
    out["abilities"] = [dict(out["ability"])] + [dict(a) for a in extra]

    # Mirror the DEFAULT spin mode onto the top level. Dozens of readers —
    # the shop, the quiz, the marketplace, ;list, the older card renderers —
    # know nothing about `spin_modes` and read `stats` / `type` /
    # `special_move` directly. Without the mirror, Master Diabolos is a blade
    # with no Special everywhere except the two paths that were taught about
    # dual spin, and it fails silently in all of them.
    modes = out.get("spin_modes") or {}
    default = modes.get("Right") or (next(iter(modes.values())) if modes else None)
    if default:
        for field in ("stats", "special_move", "type"):
            if field in default:
                out[field] = default[field]
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

    existing = {b.get("name") for b in rows}
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
        tag += " (DUAL SPIN)" if b.get("dual_spin") else ""
        print(f"{b['name']:22} {b['rarity']:10} {b['type']:8} "
              f"atk {s['attack']:3} def {s['defense']:3} sta {s['stamina']:3} "
              f"spc {s['special']:3} hp {s['hp']:3}{tag}")
        for mode, cfg in (b.get("spin_modes") or {}).items():
            ms = cfg["stats"]
            print(f"    {mode:6} {cfg['special_move']['name']:15} "
                  f"atk {ms['attack']:3} def {ms['defense']:3} "
                  f"sta {ms['stamina']:3} hp {ms['hp']:3}")
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
