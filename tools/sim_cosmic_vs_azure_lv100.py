#!/usr/bin/env python3
"""Lv100 diagnostic: Cosmic Phoenix vs Azure Drakonyx.

This is deliberately a balance diagnostic, not a balance change.  It fixes the
most important limitation of the individual blade sims: those sessions keep
raw Lv1 stats.  Here both cards are materialised through the same stats_at()
pipeline used by the level system before AbilityEngine/AttackManager see them.

Run: python3 tools/sim_cosmic_vs_azure_lv100.py
"""
import copy
import os
import sys
import types as _t

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.abilities import type_system as TS
from cogs.abilities.ability_engine import AbilityEngine
from cogs.battle.attack_manager import AttackManager
from cogs.battle.stability_manager import StabilityManager
from cogs.battle.stamina_manager import StaminaManager
from cogs.battle.status_manager import StatusManager
from cogs.core.constants import MOVE_ATTACK, MOVE_SPECIAL
from utils.database import get_beyblade
from utils import bey_levels as BL

LEVEL = 100
CP0 = get_beyblade("Cosmic Phoenix")
AD0 = get_beyblade("Azure Drakonyx")


def at100(card):
    b = copy.deepcopy(card)
    b["stats"] = BL.stats_at(card, LEVEL, {})
    return b


class Session:
    def __init__(self, cosmic_first=True):
        cp, ad = at100(CP0), at100(AD0)
        self.blades = {"c": cp, "a": ad}
        self.battle_stats = {k: dict(v["stats"]) for k, v in self.blades.items()}
        self.bey_levels = {"c": LEVEL, "a": LEVEL}
        self.max_hp_per_player = {k: int(v["stats"]["hp"]) for k, v in self.blades.items()}
        self.max_hp = max(self.max_hp_per_player.values())
        self.hp = dict(self.max_hp_per_player)
        self.last_moves, self.moves, self.stat_mult = {}, {}, {}
        self.round = 1
        self.status = StatusManager(self)
        self.status_manager = self.status
        self.stamina_manager = StaminaManager(self.blades)
        self.type_mods = {k: TS.TypeModifiers(v, stats=self.battle_stats[k]) for k, v in self.blades.items()}
        self.stability_manager = StabilityManager(self.blades, self.type_mods)
        self.chain_handler = _t.SimpleNamespace(resolve=lambda *a, **k: [])
        self.ability = AbilityEngine(self)
        for k in self.blades:
            self.ability.setup(k, self.blades[k])


def am(s):
    x = AttackManager.__new__(AttackManager)
    x.session = s
    return x


def raw_attack(s, who, foe, raw=100):
    s.moves = {who: MOVE_ATTACK, foe: MOVE_ATTACK}
    out, _, logs = s.ability.apply(who, foe, s.blades[who], s.blades[foe], MOVE_ATTACK,
                                   "win", raw, 0, is_first_hit=True,
                                   cumulative_dmg=0, is_last_hit=True)
    s.hp[foe] = max(0, s.hp[foe] - out)
    return out, logs


def special(s, who, foe):
    total, logs = am(s)._resolve_special(who, foe, MOVE_SPECIAL,
                                         s.blades[who], s.blades[foe], [])
    s.hp[foe] = max(0, s.hp[foe] - total)
    return total, logs


def main():
    cp, ad = at100(CP0), at100(AD0)
    print("=== TRUE LV100 STATS ===")
    print("Cosmic Phoenix :", cp["stats"])
    print("Azure Drakonyx :", ad["stats"])

    # Ability throughput under identical raw Attack input: exposes Rampage.
    s = Session()
    print("\n=== IDENTICAL 100-RAW ATTACK PROC TEST ===")
    for i in range(1, 7):
        d, _ = raw_attack(s, "a", "c", 100)
        print(f"Azure attack {i}: {d} ability-adjusted damage")

    # Fresh session: compare first specials at Lv100. Cosmic's first Nova must
    # also prove Astral Shift is actually online after the cast.
    s = Session()
    cp_sp, cp_logs = special(s, "c", "a")
    print("\n=== FIRST SPECIAL ===")
    print("Cosmic Phoenix Nova:", cp_sp)
    print("Cosmic form after Nova:", s.blades["c"].get("name"), s.blades["c"].get("type"))
    print("Cosmic persistent bonuses:", {
        "attack": s.ability._get_buf_bonus("c", "attack"),
        "defense": s.ability._get_buf_bonus("c", "defense"),
        "stamina": s.ability._get_buf_bonus("c", "stamina"),
    })

    s2 = Session()
    ad_sp_full, _ = special(s2, "a", "c")
    print("Azure special vs full-HP Cosmic:", ad_sp_full)

    # Azure execute window (<40% target HP).
    s3 = Session()
    s3.hp["c"] = max(1, int(s3.max_hp_per_player["c"] * 0.35))
    ad_sp_low, ad_low_logs = special(s3, "a", "c")
    print("Azure special vs Cosmic at 35% HP:", ad_sp_low)
    print("Azure low-HP rider/pierce logged:", any("25%" in x or "+30%" in x or "cuts through" in x for x in ad_low_logs))

    # Rebirth diagnostics at the *real* Lv100 HP pools.
    s4 = Session()
    s4.hp["c"] = 1
    raw_attack(s4, "a", "c", s4.max_hp_per_player["c"] + 500)
    print("\n=== SURVIVAL ===")
    print("Cosmic HP after lethal / Cosmic Rebirth:", s4.hp["c"], "/", s4.max_hp_per_player["c"])
    print("Cosmic revive used:", bool(s4.status.revival_used.get("c")))

    s5 = Session()
    s5.hp["a"] = max(1, int(s5.max_hp_per_player["a"] * 0.25))
    before = s5.hp["a"]
    raw_attack(s5, "a", "c", 0)  # threshold trigger is evaluated for mover
    print("Azure HP at 25% before/after Azure Rebirth:", before, "->", s5.hp["a"])

    print("\n=== DIAGNOSTIC ===")
    print("This script intentionally reports mechanics rather than declaring a winner.")
    print("If Cosmic is behind, compare: pre-Nova pressure, Azure Rampage cadence,"
          " Azure execute-window special, and Cosmic's post-Shift DEF penalty.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
