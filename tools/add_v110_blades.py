#!/usr/bin/env python3
"""Add the Deep Caynox pair and rework Surge Xcalibur, in data/beyblades.json.

A script rather than a hand-edit of a 101-entry JSON file, so the change is
reviewable and re-runnable. It derives the next BB id, refuses to clobber an
existing blade, and copies the ELT's shared half from the base blade rather
than retyping it.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "beyblades.json")

with open(PATH, encoding="utf-8") as fh:
    doc = json.load(fh)

nums = [int(b["id"][2:]) for b in doc.values()
        if str(b.get("id", "")).startswith("BB") and b["id"][2:].isdigit()]
nxt = max(nums) + 1


def bid():
    global nxt
    out = f"BB{nxt:03d}"
    nxt += 1
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 1. Deep Caynox — Stamina, Legendary, 50 / 101 / 121
#
# 50 Attack is the lowest on any Legendary in the roster, and that is the blade:
# it is not supposed to win by swinging. `Switch Strike` is what makes 50 Attack
# playable — Caynox strikes with its STAMINA instead, which is the one stat it
# has 121 of. Without that the card would just be worse than everything around
# it, and "low attack" would read as a mistake rather than as a design.
# ══════════════════════════════════════════════════════════════════════════════
CAYNOX = {
    "id": bid(),
    "name": "Deep Caynox",
    "rarity": "Legendary",
    "type": "Stamina",
    "spin_direction": "Right",
    "burst_height": "Low",
    "image_url": ("https://cdn.discordapp.com/attachments/"
                  "1510856884943454208/1538565792738705418/"
                  "bdf3baf43eaaeeedf925c71e47277adb.jpg"
                  "?ex=6a83cd4e&is=6a827bce&hm=dff89f7e27d9f0e011f541a1d7a19c"
                  "e0e175b117cc5f07f29e91ac421017f69e&"),
    "description": (
        "It sits so low the stadium floor barely knows it is there. Deep "
        "Caynox has the weakest strike of any Legendary and does not need a "
        "better one — it puts its spin behind the blow instead of its edge, "
        "and outlasts anything that tries to out-hit it."
    ),
    "stats": {
        "attack": 50,
        "defense": 101,
        "stamina": 121,
        "special": 116,
        "hp": 134,
    },
    "special_move": {
        "name": "Levitation Launch",
        # THE point of this move: it deals nothing. See `non_damage` in
        # damage_rules.resolve_special for why it has to be declared rather
        # than authored as a 0 — every other path floors per-hit damage at 1.
        "non_damage": True,
        "hits": 1,
        "damage_per_hit": 0,
        "total_damage": 0,
        "description": (
            "Caynox lifts clear of the floor entirely and stops touching "
            "anything. No damage, none intended — it comes back down with its "
            "spin restored and its footing back."
        ),
        "flavour_texts": [
            "🌀 **Levitation Launch** — Caynox leaves the floor entirely!"
        ],
    },
    "abilities": [
        {
            "name": "Switch Strike",
            "trigger": "on_attack_hit",
            "description": (
                "Caynox switches which stat does the hitting: every Attack "
                "adds **45% of its Stamina** as damage, which on a 121-Stamina "
                "blade is worth more than its whole Attack stat. It also "
                "drains **0.5 stamina** from whoever hits it and cuts **10%** "
                "off that hit.\n"
                "**Levitation Launch** deals no damage at all — instead it "
                "restores **26 HP**, **2 stamina** and **20 stability**, and "
                "leaves a **60 HP shield** with **+45 Defence for 2 turns**."
            ),
            "rules": [
                {
                    "when": "on_attack_hit",
                    "do": [
                        {"op": "bonus_damage_stat", "stat": "stamina",
                         "scale": 0.45}
                    ],
                },
                {
                    "when": "on_take_damage",
                    "do": [
                        {"op": "drain_stamina", "value": 0.5},
                        {"op": "reduce_damage_pct", "value": 10},
                    ],
                },
                {
                    # Everything Levitation Launch does lives here, because the
                    # move itself deals nothing. A non-damage Special with no
                    # ability rules would be a button that does nothing at all.
                    "when": "on_special",
                    "do": [
                        {"op": "heal", "value": 26},
                        {"op": "gain_stamina", "value": 2},
                        {"op": "gain_stability", "value": 20},
                        # A DURABLE guard, not `reduce_damage_pct`. That op
                        # takes no `turns` — it only shaves the hit currently
                        # being resolved, and on your own Special turn there is
                        # no incoming hit for it to shave. It would have been a
                        # line of JSON that did nothing at all.
                        {"op": "buff", "stat": "defense", "amount": 45,
                         "turns": 2},
                        {"op": "shield", "value": 60},
                    ],
                },
            ],
        }
    ],
}

# ══════════════════════════════════════════════════════════════════════════════
# 2. Deep Caynox ELT — booster, Legendary, same stats, better ability
#
# Same treatment as Drain Fafnir (Black Edition): the stat line and the Special
# are COPIED from the base blade at build time rather than retyped, so the two
# cannot drift apart. Only the ability is authored, and only upward.
# ══════════════════════════════════════════════════════════════════════════════
CAYNOX_ELT = {
    "id": bid(),
    "name": "Deep Caynox ELT",
    "rarity": "Legendary",
    "type": "Stamina",
    "spin_direction": "Right",
    "burst_height": "Low",
    "booster_exclusive": True,
    "image_url": ("https://cdn.discordapp.com/attachments/"
                  "1510856884943454208/1538764149973328022/"
                  "Deep_Caynox_ELT.png"
                  "?ex=6a83dd4a&is=6a828bca&hm=befef1edcea93c16ed3175891933997"
                  "3399ab08d8caf3ad502ca41107ac49a21&"),
    "description": (
        "The same low disc, tuned past the point the standard release stops "
        "at. ELT does not switch which stat strikes — it stops distinguishing "
        "between them. Every point of spin is a point of edge, and the "
        "levitation comes back down heavier than it left."
    ),
    "abilities": [
        {
            "name": "Extreme Switch Strike",
            "trigger": "on_attack_hit",
            "description": (
                "Switch Strike, taken further. Every Attack adds **70% of its "
                "Stamina** as damage; it drains **0.9 stamina** from whoever "
                "hits it and cuts **18%** off that hit, and every third hit "
                "taken answers for **60**.\n"
                "**Levitation Launch** restores **44 HP**, **3 stamina** and "
                "**30 stability**, leaves a **110 HP shield** with **+70 "
                "Defence for 3 turns**, and puts **+25 Attack** behind the "
                "next two strikes."
            ),
            "rules": [
                {
                    "when": "on_attack_hit",
                    "do": [
                        {"op": "bonus_damage_stat", "stat": "stamina",
                         "scale": 0.70}
                    ],
                },
                {
                    "when": "on_take_damage",
                    "do": [
                        {"op": "drain_stamina", "value": 0.9},
                        {"op": "reduce_damage_pct", "value": 18},
                        {"op": "add_counter", "name": "elt", "amount": 1,
                         "max": 3},
                        {"op": "counter_burst", "name": "elt", "at": 3,
                         "damage": 60, "reset": True},
                    ],
                },
                {
                    "when": "on_special",
                    "do": [
                        {"op": "heal", "value": 44},
                        {"op": "gain_stamina", "value": 3},
                        {"op": "gain_stability", "value": 30},
                        {"op": "buff", "stat": "defense", "amount": 70,
                         "turns": 3},
                        {"op": "shield", "value": 110},
                        {"op": "buff", "stat": "attack", "amount": 25,
                         "turns": 2},
                    ],
                },
            ],
        }
    ],
}

NEW = {"Deep Caynox": CAYNOX, "Deep Caynox ELT": CAYNOX_ELT}

clash = sorted(set(NEW) & set(doc))
if clash:
    sys.exit(f"refusing to overwrite existing blades: {clash}")

# The ELT's shared half, copied from the base rather than retyped.
CAYNOX_ELT["stats"] = json.loads(json.dumps(CAYNOX["stats"]))
CAYNOX_ELT["special_move"] = json.loads(json.dumps(CAYNOX["special_move"]))
CAYNOX_ELT["special_move"]["flavour_texts"] = [
    "🌀 **Levitation Launch** — ELT does not come back down the same!"
]

doc.update(NEW)

# ══════════════════════════════════════════════════════════════════════════════
# 3. Surge Xcalibur → Surge Xcalius, and Excalibur Surge → Triple Saber
#
# Safe as a straight rename: measured against the live store, the blade is owned
# by 0 players and equipped by 0, because it shipped yesterday. A rename of
# anything anyone HELD would need a profile migration — inventories store the
# name, not the id.
#
# "3 hit, everything will be same, 10 stability each hit": the 30 stability the
# Special used to take in one lump becomes 10 per hit across three, and the
# total damage is held at exactly 34 with an explicit per-hit list rather than
# rounded to 3x11 or 3x12. Everything else about the blade — the stats, the
# 230-damage bank, the converted Recover and Defend — is untouched.
# ══════════════════════════════════════════════════════════════════════════════
OLD, NEWNAME = "Surge Xcalibur", "Surge Xcalius"
if OLD in doc:
    blade = doc.pop(OLD)
    blade["name"] = NEWNAME
    doc[NEWNAME] = blade
elif NEWNAME not in doc:
    sys.exit(f"neither {OLD!r} nor {NEWNAME!r} is in the roster")

sx = doc[NEWNAME]
sx["special_move"].update({
    "name": "Triple Saber",
    "hits": 3,
    # A LIST, not a scalar: 34 // 3 is 11 and 3x11 is 33, so a scalar would
    # quietly shave a point off a Special the brief said to leave alone.
    "damage_per_hit": [12, 11, 11],
    "total_damage": 34,
    "description": (
        "Three sabers out of one edge, each one aimed at the footing rather "
        "than the blade. Thirty points of stability leave the stadium across "
        "the set."
    ),
    "flavour_texts": [
        "⚔️ **Triple Saber** — three cuts, and none of them at the blade!"
    ],
})
sx["description"] = sx["description"].replace("Surge Xcalibur", NEWNAME)

for ab in sx["abilities"]:
    ab["description"] = ab["description"].replace(
        "• The **Special** takes **30 stability** and banks it.",
        "• The **Special** lands **3 sabers at 10 stability each** and banks "
        "the 30.")
    for rule in ab["rules"]:
        do = rule.get("do") or []
        # The stability moves from on_special (fires ONCE, on the first hit) to
        # on_hit (the per-Special-hit trigger), so "10 each" is three separate
        # 10s rather than one 30 wearing a different label.
        if rule.get("when") == "on_special" and any(
                op["op"] == "enemy_lose_stability" for op in do):
            rule["do"] = [op for op in do
                          if op["op"] != "enemy_lose_stability"]
    ab["rules"].append({
        "when": "on_hit",
        "do": [{"op": "enemy_lose_stability", "value": 10}],
        "_name": "Surge Overwrite",
        "_note": "on_hit is the per-Special-hit trigger — 3 hits x 10 = the "
                 "same 30 the single-hit version took.",
    })

with open(PATH, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2, ensure_ascii=False)
    fh.write("\n")

for name in (*NEW, NEWNAME):
    b = doc[name]
    st = b["stats"]
    sm = b["special_move"]
    print(f"{b['id']}  {name:<20} {b['rarity']:<10} {b['type']:<8} "
          f"A{st['attack']} D{st['defense']} S{st['stamina']} "
          f"SP{st['special']} HP{st['hp']}  ·  {sm['name']} "
          f"{sm['hits']}x{sm.get('damage_per_hit')}")
print(f"\nroster: {len(doc)} blades")
