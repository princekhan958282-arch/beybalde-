#!/usr/bin/env python3
"""Add Cure Black and Cure White to data/beyblades.json.

Both are Epic, mid-power designs with 397 total HP/ATK/DEF/STM.
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

CURE_BLACK = {
    "id": bid(),
    "name": "Cure Black",
    "rarity": "Epic",
    "type": "Attack",
    "spin_direction": "Right",
    "burst_height": "Mid",
    "image_url": "https://cdn.discordapp.com/attachments/1510856884943454208/1552923890563682434/image0.jpg?ex=6ab76093&is=6ab60f13&hm=71fd3451181c4e2a40a0cfef4319fd5f357da22ea697fc504eb44e85b8be9e81&",
    "description": "A dark-hearted Epic attacker that turns successful clashes into steady offensive momentum and recovery.",
    "stats": {"attack": 125, "defense": 87, "stamina": 80, "special": 115, "hp": 105},
    "special_move": {
        "name": "Black Cure", "hits": 1, "damage_per_hit": 115, "total_damage": 115,
        "description": "A focused dark-heart strike that pierces part of the opponent's guard and restores Cure Black after it lands.",
        "flavour_texts": ["🖤 **Black Cure** — the dark heart strikes back!"]
    },
    "abilities": [{
        "name": "Dark Heart", "trigger": "on_attack_win",
        "description": "On an Attack Win, gain +10% damage for that attack, recover 6 HP, and gain +8 ATK for 2 turns. Cooldown: 2 rounds. Black Cure restores 8 HP and ignores 8% DEF.",
        "rules": [
            {"when": "on_attack_win", "cooldown": 2, "do": [
                {"op": "dmg_amp", "value": 0.10},
                {"op": "heal", "value": 6},
                {"op": "buff", "stat": "attack", "amount": 8, "turns": 2}
            ], "_name": "Dark Heart"},
            {"when": "on_special", "cooldown": 4, "do": [
                {"op": "ignore_defense_pct", "value": 8},
                {"op": "heal", "value": 8}
            ], "_name": "Black Cure"}
        ]
    }]
}

CURE_WHITE = {
    "id": bid(),
    "name": "Cure White",
    "rarity": "Epic",
    "type": "Defense",
    "spin_direction": "Right",
    "burst_height": "Mid",
    "image_url": "https://cdn.discordapp.com/attachments/1510856884943454208/1552923900449529896/image1.jpg?ex=6ab76095&is=6ab60f15&hm=a7923c6e831e69b8cb8b2b2cfe5b540663d940361416ca5ea24faa655b809f2d&",
    "description": "A pure-hearted Epic defender built around controlled recovery and short defensive reinforcement.",
    "stats": {"attack": 82, "defense": 120, "stamina": 75, "special": 100, "hp": 120},
    "special_move": {
        "name": "White Cure", "hits": 1, "damage_per_hit": 100, "total_damage": 100,
        "description": "A cleansing defensive strike that restores HP and braces Cure White for the next incoming hit.",
        "flavour_texts": ["🤍 **White Cure** — purity becomes an unbreakable guard!"]
    },
    "abilities": [{
        "name": "Pure Heart", "trigger": "on_defense_win",
        "description": "On a Defense Win, reduce damage taken by 10% that round, recover 7 HP, and gain +8 DEF for 2 turns. Cooldown: 2 rounds. White Cure restores 12 HP and grants 10% reduction against the next incoming hit.",
        "rules": [
            {"when": "on_defense_win", "cooldown": 2, "do": [
                {"op": "reduce_damage_pct", "value": 10},
                {"op": "heal", "value": 7},
                {"op": "buff", "stat": "defense", "amount": 8, "turns": 2}
            ], "_name": "Pure Heart"},
            {"when": "on_special", "cooldown": 4, "do": [
                {"op": "heal", "value": 12},
                {"op": "shield", "value": 10}
            ], "_name": "White Cure"}
        ]
    }]
}

NEW = {"Cure Black": CURE_BLACK, "Cure White": CURE_WHITE}
clash = sorted(set(NEW) & set(doc))
if clash:
    sys.exit(f"refusing to overwrite existing blades: {clash}")

for b in NEW.values():
    st = b["stats"]
    assert st["hp"] + st["attack"] + st["defense"] + st["stamina"] == 397

doc.update(NEW)
with open(PATH, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2, ensure_ascii=False)
    fh.write("\n")

for name in NEW:
    b = doc[name]
    st = b["stats"]
    print(f"{b['id']} {name}: Epic {b['type']} — HP {st['hp']} ATK {st['attack']} DEF {st['defense']} STM {st['stamina']} = 397")
