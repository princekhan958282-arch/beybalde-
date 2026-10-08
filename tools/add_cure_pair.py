#!/usr/bin/env python3
"""Add Cure Black and Cure White to data/beyblades.json.

Both are Epic, mid-power designs with 397 total HP/ATK/DEF/STM.
"""

def authored_open(*args, **kwargs):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from utils.character_registry import authored_open as open_registry
    return open_registry(*args, **kwargs)

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "beys")

with authored_open('bey', encoding="utf-8") as fh:
    doc = json.load(fh)

nums = [int(b["id"][2:]) for b in doc.values()
        if str(b.get("id", "")).startswith("BB") and b["id"][2:].isdigit()]
nxt = max(nums) + 1

def bid():
    global nxt
    out = f"BB{nxt:03d}"
    nxt += 1
    return out

CURE_BLACK = {'id': 'BB124',
 'name': 'Cure Black',
 'rarity': 'Epic',
 'type': 'Attack',
 'spin_direction': 'Right',
 'burst_height': 'Mid',
 'image_url': 'https://cdn.discordapp.com/attachments/1510856884943454208/1552923890563682434/image0.jpg?ex=6ab76093&is=6ab60f13&hm=71fd3451181c4e2a40a0cfef4319fd5f357da22ea697fc504eb44e85b8be9e81&',
 'description': "An Epic support Bey that strengthens teammates' attack in boss battles.",
 'stats': {'attack': 125, 'defense': 87, 'stamina': 80, 'special': 115, 'hp': 105},
 'special_move': {'name': 'Black Cure',
                  'hits': 1,
                  'damage_per_hit': 115,
                  'total_damage': 115,
                  'description': "A focused dark-heart strike that pierces part of the opponent's "
                                 'guard and restores Cure Black after it lands.',
                  'flavour_texts': ['🖤 **Black Cure** — the dark heart strikes back!']},
 'abilities': [{'name': 'Dark Heart Support',
                'trigger': 'passive',
                'description': 'In co-op boss battles, living Cure Black grants other teammates '
                               '+30% Attack. Ends when Cure Black is defeated; duplicate auras do '
                               'not stack. No team buff in solo or PvP. Black Cure restores 8 HP '
                               'and ignores 8% DEF.',
                'boss_support': {'stat': 'attack', 'percent': 30},
                'rules': [{'when': 'on_special',
                           'cooldown': 4,
                           'do': [{'op': 'ignore_defense_pct', 'value': 8},
                                  {'op': 'heal', 'value': 8}],
                           '_name': 'Black Cure'}]}]}
CURE_BLACK["id"] = bid()

CURE_WHITE = {'id': 'BB125',
 'name': 'Cure White',
 'rarity': 'Epic',
 'type': 'Defense',
 'spin_direction': 'Right',
 'burst_height': 'Mid',
 'image_url': 'https://cdn.discordapp.com/attachments/1510856884943454208/1552923900449529896/image1.jpg?ex=6ab76095&is=6ab60f15&hm=a7923c6e831e69b8cb8b2b2cfe5b540663d940361416ca5ea24faa655b809f2d&',
 'description': "An Epic support Bey that strengthens teammates' defense in boss battles.",
 'stats': {'attack': 82, 'defense': 120, 'stamina': 75, 'special': 100, 'hp': 120},
 'special_move': {'name': 'White Cure',
                  'hits': 1,
                  'damage_per_hit': 100,
                  'total_damage': 100,
                  'description': 'A cleansing defensive strike that restores HP and braces Cure '
                                 'White for the next incoming hit.',
                  'flavour_texts': ['🤍 **White Cure** — purity becomes an unbreakable guard!']},
 'abilities': [{'name': 'Pure Heart Support',
                'trigger': 'passive',
                'description': 'In co-op boss battles, living Cure White grants other teammates '
                               '+30% Defense. Ends when Cure White is defeated; duplicate auras do '
                               'not stack. No team buff in solo or PvP. White Cure restores 12 HP '
                               'and grants 10% reduction against the next incoming hit.',
                'boss_support': {'stat': 'defense', 'percent': 30},
                'rules': [{'when': 'on_special',
                           'cooldown': 4,
                           'do': [{'op': 'heal', 'value': 12}, {'op': 'shield', 'value': 10}],
                           '_name': 'White Cure'}]}]}
CURE_WHITE["id"] = bid()

NEW = {"Cure Black": CURE_BLACK, "Cure White": CURE_WHITE}
clash = sorted(set(NEW) & set(doc))
if clash:
    sys.exit(f"refusing to overwrite existing blades: {clash}")

for b in NEW.values():
    st = b["stats"]
    assert st["hp"] + st["attack"] + st["defense"] + st["stamina"] == 397

doc.update(NEW)
with authored_open('bey', "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2, ensure_ascii=False)
    fh.write("\n")

for name in NEW:
    b = doc[name]
    st = b["stats"]
    print(f"{b['id']} {name}: Epic {b['type']} — HP {st['hp']} ATK {st['attack']} DEF {st['defense']} STM {st['stamina']} = 397")
