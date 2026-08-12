#!/usr/bin/env python3
"""
tools/add_starter_beys.py — the four anime starter blades.

Adds Rising Ragnaruk, King Kerbeus, Storm Spriggan and Victory Valkyrie to
data/beyblades.json, one per type, all Rare, all pitched at STARTER power.

"Starter" is the constraint that decides every number here. These are the only
blades `;start` hands out, so they set a new player's first impression of the
game AND become the thing every other blade has to look better than. Their
stats therefore sit at the bottom of the Rare band — below the Rare median on
every line — while their abilities carry the anime identity. A starter should
feel characterful and be outgrown, not be a blade you keep because it happens
to be strong.

Idempotent: re-running replaces these four by name rather than appending, so
tuning is a one-line edit and a re-run.

Run:  python3 tools/add_starter_beys.py [--check]
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "beyblades.json")

# Rare band, measured across the 15 existing Rare blades:
#   hp 87-131 (med 111) · atk 47-114 (med 102) · def 31-156 (med 80)
#   sta 66-121 (med 91) · special 75-125 (med 90)
# Every line below is at or under the median on purpose.
STARTERS = [
    {
        "id": "BB081",
        "name": "Rising Ragnaruk",
        "rarity": "Rare",
        "type": "Stamina",
        "spin_direction": "right",
        "burst_height": "Mid",
        "image_url": "https://cdn.discordapp.com/attachments/1510856884943454208/1537063768641769513/Beyblade_Ragnaruk.webp?ex=6a7dadaf&is=6a7c5c2f&hm=34878829e4e89a3a81aff8407ed9c4e0b97e62ae95bbd64b9c615befb63a8a59&",
        "description": (
            "A heavyweight that wins by refusing to stop. Ragnaruk converts "
            "every exchange it survives into more spin, grinding opponents "
            "down long after a faster blade would have burned out."),
        "stats": {"attack": 62, "defense": 74, "stamina": 98, "special": 74,
                  "hp": 108},
        "ability": {
            "name": "Centrifugal Force",
            "trigger": "on_stamina_win",
            "description": (
                "Every Stamina exchange Ragnaruk wins feeds its rotation — "
                "recovering 10 HP and packing on +5 Defence, stacking up to "
                "4 times. The longer the battle runs, the harder it is to "
                "move."),
            "rules": [
                {
                    "when": "on_stamina_win",
                    "do": [
                        {"op": "heal", "value": 10},
                        {"op": "stacking_buff", "stat": "defense",
                         "per_stack": 5, "max": 4, "name": "centrifugal"},
                    ],
                    "_name": "Centrifugal Force",
                },
            ],
        },
        "special_move": {
            "name": "Roktavor Zone",
            "description": (
                "Ragnaruk plants itself and floods the stadium floor — the "
                "zone drags the opponent's spin away and feeds it back."),
            "hits": 1,
            "damage_per_hit": 78,
            "total_damage": 78,
            "flavour_texts": [
                "🌀 **Roktavor Zone** — the stadium belongs to Ragnaruk!",
            ],
        },
    },
    {
        "id": "BB082",
        "name": "King Kerbeus",
        "rarity": "Rare",
        "type": "Defense",
        "spin_direction": "right",
        "burst_height": "Low",
        "image_url": "https://cdn.discordapp.com/attachments/1510856884943454208/1537063778384875570/Beyblade_Kerbeus.webp?ex=6a7dadb2&is=6a7c5c32&hm=e9ba3ac3ce651e5cac041a6c6107e36de829442db7612b54278c911f1e87216f&",
        "description": (
            "The three-headed guardian. Kerbeus does not dodge and does not "
            "chase — it plants itself in the centre and makes every attacker "
            "pay for the ground they take."),
        "stats": {"attack": 58, "defense": 96, "stamina": 76, "special": 72,
                  "hp": 104},
        "ability": {
            "name": "Chain Blade",
            "trigger": "on_defend",
            "description": (
                "Kerbeus braces behind a 12 HP guard every time it is "
                "attacked, and the chains snap back for 10 damage at whoever "
                "swung."),
            "rules": [
                {
                    "when": "on_defend",
                    "do": [{"op": "shield", "value": 12}],
                    "_name": "Chain Blade",
                },
                # on_take_damage, not on_defense_win. `reflect_flat` adds to
                # the ATTACKER's damage and is guarded by `dmg_dealt > 0`, so
                # it is a defender-side op: fired as a mover trigger on a
                # defence win there is no incoming damage to answer and it can
                # never do anything. This is the trigger where a reflect means
                # what its name says.
                {
                    "when": "on_take_damage",
                    "do": [{"op": "reflect_flat", "value": 10}],
                    "_name": "Chain Blade",
                },
            ],
        },
        "special_move": {
            "name": "Chain Launch",
            "description": (
                "The chains go taut and Kerbeus hauls the opponent in — two "
                "grinding impacts that leave them badly off balance."),
            "hits": 2,
            "damage_per_hit": 39,
            "total_damage": 78,
            "flavour_texts": [
                "⛓️ **Chain Launch** — Kerbeus drags them into the wall!",
            ],
        },
    },
    {
        "id": "BB083",
        "name": "Storm Spriggan",
        "rarity": "Rare",
        "type": "Balance",
        "spin_direction": "right",
        "burst_height": "Mid",
        "image_url": "https://cdn.discordapp.com/attachments/1510856884943454208/1537063801269129236/Beyblade_Spriggan.webp?ex=6a7dadb7&is=6a7c5c37&hm=c8e29a46b9ad89e23ef2829d6cd0496c725e17e51b7f3b4f2d6e78427ba03d15&",
        "description": (
            "The blade that answers. Storm Spriggan gives ground on purpose, "
            "then turns whatever it was hit with straight back — a style that "
            "costs it as much as the opponent."),
        "stats": {"attack": 74, "defense": 78, "stamina": 78, "special": 82,
                  "hp": 104},
        "ability": {
            "name": "Spriggan Layer",
            "trigger": "on_any_win",
            "description": (
                "A layer built for either direction. Any exchange Spriggan "
                "wins mends 8 HP — it stays in the fight long enough to find "
                "its counter."),
            "rules": [
                {
                    "when": "on_any_win",
                    "do": [{"op": "heal", "value": 8}],
                    "_name": "Spriggan Layer",
                },
                # Counter Break's stance, armed by the Special and spent on the
                # next hit taken. Modelled as a mode rather than a timer
                # because the engine's set_mode has no duration — reflecting
                # once and disarming makes it a genuine counter instead of a
                # permanent aura, and bounds it without a new op.
                {
                    "when": "on_special",
                    "do": [
                        {"op": "set_mode", "name": "counter_break"},
                        {"op": "log",
                         "text": "🌀 **Counter Break** — Spriggan opens its "
                                 "guard and waits."},
                    ],
                    "_name": "Counter Break",
                },
                {
                    "when": "on_take_damage",
                    "if": [{"cond": "mode_is", "value": "counter_break"}],
                    "do": [
                        {"op": "reflect_pct", "value": 100},
                        {"op": "set_mode", "name": "spent"},
                        {"op": "log",
                         "text": "🪞 **Counter Break** — every point of it goes "
                                 "back the way it came!"},
                    ],
                    "_name": "Counter Break",
                },
            ],
        },
        "special_move": {
            "name": "Counter Break",
            "description": (
                "Spriggan absorbs the exchange and returns it whole. The next "
                "hit it takes is reflected 100% back at the attacker — and "
                "Spriggan still takes it. Armed only by the Special."),
            "hits": 1,
            "damage_per_hit": 130,
            "total_damage": 130,
            "flavour_texts": [
                "⚖️ **Counter Break** — Spriggan turns the battle around!",
            ],
        },
    },
    {
        "id": "BB084",
        "name": "Victory Valkyrie",
        "rarity": "Rare",
        "type": "Attack",
        "spin_direction": "right",
        "burst_height": "High",
        "image_url": "https://cdn.discordapp.com/attachments/1510856884943454208/1537063819661283448/Beyblade_Valkyrie.webp?ex=6a7dadbb&is=6a7c5c3b&hm=8b7cae23341f9cac532f42182fe4140bc2a030bb5f6585b2ecf2b32952c88700&",
        "description": (
            "All forward momentum and no brakes. Valkyrie trades its guard "
            "for speed and wins by never letting the opponent set their feet."),
        "stats": {"attack": 92, "defense": 48, "stamina": 70, "special": 76,
                  "hp": 96},
        "ability": {
            "name": "Energy Layer",
            "trigger": "on_attack_win",
            "description": (
                "Every Attack Valkyrie wins winds the layer tighter: +6 Attack "
                "per stack, up to 4. Firing the Special sharpens the whole "
                "flurry — +20% crit chance on every hit of Rush Launch."),
            "rules": [
                {
                    "when": "on_attack_win",
                    "do": [{"op": "stacking_buff", "stat": "attack",
                            "per_stack": 6, "max": 4, "name": "energy"}],
                    "_name": "Energy Layer",
                },
                {
                    "when": "on_special",
                    "do": [{"op": "crit_chance", "value": 20}],
                    "_name": "Energy Layer",
                },
            ],
        },
        "special_move": {
            "name": "Rush Launch",
            "description": (
                "Sixteen impacts in one pass — ten damage apiece, every one of "
                "them able to crit."),
            "hits": 16,
            "damage_per_hit": 10,
            "total_damage": 160,
            "flavour_texts": [
                "⚔️ **Rush Launch** — sixteen hits, no pause!",
            ],
        },
    },
]

NAMES = {b["name"] for b in STARTERS}


def main() -> int:
    check_only = "--check" in sys.argv
    with open(DATA, encoding="utf-8") as fh:
        doc = json.load(fh)

    # data/beyblades.json is a dict keyed by blade NAME at the top level, but
    # a "beyblades" wrapper and a plain list both exist elsewhere in the repo's
    # history. Detect which, and write back the SAME shape — a mismatch here
    # silently rewrites the whole roster into a nested key and loses every
    # blade to every reader.
    if isinstance(doc, dict) and "beyblades" in doc:
        shape, blades = "wrapped", doc["beyblades"]
    elif isinstance(doc, list):
        shape, blades = "list", doc
    else:
        shape, blades = "by_name", doc
    listy = isinstance(blades, list)
    rows = blades if listy else list(blades.values())

    existing = {b.get("name") for b in rows}
    taken_ids = {b.get("id") for b in rows}
    clash = [b["id"] for b in STARTERS
             if b["id"] in taken_ids and b["name"] not in existing]
    if clash:
        print(f"ID collision: {clash} already used by another blade")
        return 1

    kept = [b for b in rows if b.get("name") not in NAMES]

    # The engine runs `abilities` — the PLURAL list — via
    # AbilityEngine._rules_for. The singular `ability` is display only: ;info
    # and the info card read it, and nothing else does. Every working blade in
    # the roster carries both, with abilities[0] mirroring ability.
    #
    # Authoring only the singular key produces a blade that looks completely
    # correct in ;info and whose ability never fires a single time in a
    # battle. That is exactly what happened here before the tests caught it,
    # so the mirroring is done in code rather than left to be remembered.
    out = kept + [dict(b, abilities=[dict(b["ability"])]) for b in STARTERS]

    print(f"{'blade':20} {'type':8} {'atk':>4} {'def':>4} {'sta':>4} "
          f"{'spc':>4} {'hp':>4}   special")
    for b in STARTERS:
        s = b["stats"]
        sm = b["special_move"]
        print(f"{b['name']:20} {b['type']:8} {s['attack']:4} {s['defense']:4} "
              f"{s['stamina']:4} {s['special']:4} {s['hp']:4}   "
              f"{sm['name']} ({sm['hits']}x{sm['damage_per_hit']})")
    print(f"\nroster {len(rows)} -> {len(out)}")

    if check_only:
        print("check only — nothing written")
        return 0

    if shape == "wrapped":
        doc["beyblades"] = out if listy else {b["name"]: b for b in out}
    elif shape == "list":
        doc = out
    else:                                    # by_name — the shape on disk today
        doc = {b["name"]: b for b in out}

    with open(DATA, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"wrote {DATA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
