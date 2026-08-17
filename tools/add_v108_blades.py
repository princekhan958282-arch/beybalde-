#!/usr/bin/env python3
"""Add the three v1.08 blades to data/beyblades.json.

Written as a script rather than by hand-editing a 98-entry JSON file so the
insertion is reviewable and re-runnable: it refuses to clobber an existing
entry, and it derives the next BB id instead of hard-coding one.
"""
import json
import os
import sys

ROOT = "/home/user/beybalde-"
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


NEW = {}

# ── 1. Drain Fafnir (Black Edition) ──────────────────────────────────────────
# "Everything same as normal Drain Fafnir but better ability." Taken literally:
# the stat line, spin, height, type and Special are copied from BB094 at the
# end of this file rather than retyped, so the two can never drift apart. Only
# the ability is authored, and only upward.
NEW["Drain Fafnir (Black Edition)"] = {
    "id": bid(),
    "name": "Drain Fafnir (Black Edition)",
    "rarity": "Mythic",
    "type": "Stamina",
    "spin_direction": "Left",
    "burst_height": "Mid",
    "booster_exclusive": True,
    "image_url": ("https://cdn.discordapp.com/attachments/"
                  "1510856884943454208/1538565725105561610/"
                  "b70537c94b4a53b0bfc56c3a70d06b71.jpg"
                  "?ex=6a83247e&is=6a81d2fe&hm=b55e357de44fe4256c73f119ce6894"
                  "c136bd9538d7f77137a3c87eed681d44c4&"),
    "description": (
        "The same left-spin layer in funeral black. Nothing about the shape "
        "changed — what changed is how much it keeps. Where Drain Fafnir takes "
        "a share of what hits it, the Black Edition takes the hit itself, the "
        "spin behind it, and the stamina that was going to pay for the next "
        "one."
    ),
    "abilities": [
        {
            "name": "Void Drain",
            "trigger": "on_take_damage",
            "description": (
                "Drain Spin, doubled and then some. Steals **1.2 stamina** "
                "from the attacker, heals **26** from the theft, and cuts "
                "**22%** off the hit that fed it. Every third hit taken bursts "
                "for 70 true damage — Fafnir has been full for a while by "
                "then."
            ),
            "rules": [
                {
                    "when": "on_take_damage",
                    "do": [
                        {"op": "drain_stamina", "value": 1.2},
                        {"op": "heal_per_drain", "value": 26},
                        {"op": "reduce_damage_pct", "value": 22},
                        {"op": "add_counter", "name": "void", "amount": 1,
                         "max": 3},
                        {"op": "counter_burst", "name": "void", "at": 3,
                         "damage": 70, "reset": True},
                    ],
                }
            ],
        }
    ],
}

# ── 2. Twin Nemesis ──────────────────────────────────────────────────────────
# Animal-named kit, as asked. Two heads on one axis is the whole idea, so the
# ability and the Special are both built around alternation: the blade is never
# doing one thing, it is doing the other one next.
NEW["Twin Nemesis"] = {
    "id": bid(),
    "name": "Twin Nemesis",
    "rarity": "Legendary",
    "type": "Balance",
    "spin_direction": "Right",
    "burst_height": "Mid",
    "image_url": ("https://cdn.discordapp.com/attachments/"
                  "1510856884943454208/1538565822400827452/"
                  "f61b5a67f0ec747cec4736c7111b4e44.jpg"
                  "?ex=6a832495&is=6a81d315&hm=a19f219c94f5bcb383ead467982cd3"
                  "edec3cd4b6eb2c170f06125adc047895e5&"),
    "description": (
        "Two beasts sharing one axis and refusing to share anything else. One "
        "head takes the hit, the other answers it, and the blade is never "
        "doing both at once — which is exactly why it never runs out of the "
        "one it needs."
    ),
    "stats": {
        "attack": 124,
        "defense": 121,
        "stamina": 118,
        "special": 130,
        "hp": 126,
    },
    "special_move": {
        "name": "Twin Fang Devour",
        "description": (
            "Both heads come off the axis at once and close from opposite "
            "sides. There is no gap between the two bites to step out of."
        ),
        "hits": 4,
        "damage_per_hit": 39,
        "total_damage": 156,
        "flavour_texts": [
            "🐍 **Twin Fang Devour** — both heads close at once!"
        ],
    },
    "abilities": [
        {
            "name": "Beast Alternation",
            "trigger": "on_attack_hit",
            "description": (
                "The heads trade off. Every strike takes **6 stability** and "
                "banks a Fang; at **3 Fangs** the second head bites for **85** "
                "and the count resets. Meanwhile every hit taken is answered "
                "with **12% reduction and 16 reflected** — whichever head is "
                "not attacking is guarding."
            ),
            "rules": [
                {
                    "when": "on_attack_hit",
                    "do": [
                        {"op": "enemy_lose_stability", "value": 6},
                        {"op": "add_counter", "name": "fang", "amount": 1,
                         "max": 3},
                        {"op": "counter_burst", "name": "fang", "at": 3,
                         "damage": 85, "reset": True},
                    ],
                },
                {
                    "when": "on_take_damage",
                    "do": [
                        {"op": "reduce_damage_pct", "value": 12},
                        {"op": "reflect_flat", "value": 16},
                    ],
                },
            ],
        }
    ],
}

# ── 3. Surge Xcalibur ────────────────────────────────────────────────────────
# The brief: "ability change everything into attack", 1 stamina heal = 59
# damage, defence counters at 20, and a Special that banks 30 stability on its
# first use and cashes it for 230 on the next.
#
# Read as one idea rather than three: Xcalibur has no defensive options at all,
# because every defensive option has been rewritten as an attack. The Stamina
# move stops restoring and becomes 59 damage; the Defend move stops soaking and
# counters 20% harder off the normal defence-counter calculation. The stat line
# says the same thing — 47 Defence on a Mythic is not an oversight, it is the
# ability printed in the numbers.
NEW["Surge Xcalibur"] = {
    "id": bid(),
    "name": "Surge Xcalibur",
    "rarity": "Mythic",
    "type": "Attack",
    "spin_direction": "Right",
    "burst_height": "High",
    "image_url": ("https://cdn.discordapp.com/attachments/"
                  "1510856884943454208/1538565731757592627/"
                  "675efa73338f2bfa315c44ee0da84cb6.jpg"
                  "?ex=6a83247f&is=6a81d2ff&hm=37d4b281b1238a0bbfbed0faf00dcc"
                  "7dc2610a0521928ad57c76222b974fdd3f&"),
    "description": (
        "A sword that has forgotten how to be anything else. Surge Xcalibur "
        "cannot recover and cannot guard — the recovery comes out as a strike "
        "and the guard comes out as a counter, because the blade has no other "
        "gear to shift into. 47 Defence is not a weakness it carries; it is "
        "the weight it threw away."
    ),
    "stats": {
        "attack": 158,
        "defense": 47,
        "stamina": 92,
        "special": 152,
        "hp": 112,
    },
    "special_move": {
        "name": "Excalibur Surge",
        "description": (
            "The first surge does not go for health at all — it goes for the "
            "footing, and takes 30 stability out from under the opponent. The "
            "next one collects."
        ),
        "hits": 1,
        "damage_per_hit": 34,
        "total_damage": 34,
        "flavour_texts": [
            "⚡ **Excalibur Surge** — the ground goes first!"
        ],
    },
    "abilities": [
        {
            "name": "Surge Overwrite",
            "trigger": "on_special",
            "description": (
                "Every defensive option is rewritten as an attack.\n"
                "• **Recover** stops restoring and lands **59 damage** "
                "instead.\n"
                "• **Defend** stops soaking and counters **20% harder**, off "
                "the normal defence-counter calculation.\n"
                "• The **Special** takes **30 stability** and banks it. The "
                "NEXT Special cashes that bank for **230 damage** and banks "
                "again, so from the second one onward every Special collects."
            ),
            "rules": [
                {
                    # FIRST, deliberately. The bank is added by the rule below,
                    # in the same trigger pass — so if this one ran second it
                    # would cash a charge that was placed a microsecond ago and
                    # the very first Special would pay out 230. Ordering is the
                    # whole mechanism here.
                    "when": "on_special",
                    "if": [{"cond": "counter_at_least", "value": 1,
                            "name": "surge_charge"}],
                    "do": [
                        {"op": "consume_counter", "name": "surge_charge",
                         "amount": 1, "damage_per_stack": 230}
                    ],
                    "_name": "Surge Overwrite",
                },
                {
                    "when": "on_special",
                    "do": [
                        {"op": "enemy_lose_stability", "value": 30},
                        {"op": "add_counter", "name": "surge_charge",
                         "amount": 1, "max": 1},
                    ],
                    "_name": "Surge Overwrite",
                },
                {
                    # The Recover move, converted. Placed on all three stamina
                    # outcomes because the conversion is not a reward for
                    # winning the exchange — the resource is spent either way.
                    "when": "on_stamina_win",
                    "do": [{"op": "bonus_damage", "value": 59}],
                    "_name": "Surge Overwrite",
                },
                {
                    "when": "on_stamina_mirror",
                    "do": [{"op": "bonus_damage", "value": 59}],
                    "_name": "Surge Overwrite",
                },
                {
                    "when": "on_stamina_loss",
                    "do": [{"op": "bonus_damage", "value": 59}],
                    "_name": "Surge Overwrite",
                },
                {
                    "when": "setup",
                    "do": [{"op": "counter_damage_pct", "value": 20}],
                    "_name": "Surge Overwrite",
                },
            ],
        }
    ],
}

# Copy the Black Edition's shared half from the blade it is an edition OF,
# rather than retyping it. Two copies of one stat line is one copy too many the
# first time either changes.
base = doc["Drain Fafnir"]
be = NEW["Drain Fafnir (Black Edition)"]
be["stats"] = json.loads(json.dumps(base["stats"]))
be["special_move"] = json.loads(json.dumps(base["special_move"]))
be["special_move"]["flavour_texts"] = [
    "🖤 **Nothing Break** — the black layer gives nothing back!"
]

clash = sorted(set(NEW) & set(doc))
if clash:
    sys.exit(f"refusing to overwrite existing blades: {clash}")

doc.update(NEW)
with open(PATH, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2, ensure_ascii=False)
    fh.write("\n")

for name, b in NEW.items():
    st = b["stats"]
    print(f"{b['id']}  {name:<32} {b['rarity']:<10} {b['type']:<8} "
          f"A{st['attack']} D{st['defense']} S{st['stamina']} "
          f"SP{st['special']} HP{st['hp']}")
print(f"\nroster: {len(doc)} blades")
