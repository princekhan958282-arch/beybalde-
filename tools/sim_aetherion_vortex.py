#!/usr/bin/env python3
"""
tools/sim_aetherion_vortex.py — Aetherion Vortex, and the primitives it needed.

Two more engine gaps, both about things the rule language simply could not say:

  round_at_least / round_below   elapsed time. Every existing condition reads
                                 HP, stamina, a counter or a move — none of them
                                 is "after five rounds".
  dmg_amp turns=N                a TEMPORARY damage amp. add_dmg_amp is a
                                 permanent accumulator, so "+25% for 5 turns"
                                 could only be written as "+25% forever", and an
                                 Overdrive that never ends is not an Overdrive.

Plus a second `per_hit_bonus` source, `own_stamina_above`, for the Adaptive
Punish hit.

Run:  python3 tools/sim_aetherion_vortex.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {detail}")


from cogs.abilities.ability_engine import AbilityEngine        # noqa: E402
from cogs.battle.damage_rules import (                         # noqa: E402
    resolve_special, resolve_special_hits)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLADES = json.load(open(os.path.join(ROOT, "data", "beyblades.json"),
                        encoding="utf-8"))
AV = BLADES["Aetherion Vortex"]
SM = AV["special_move"]


class FakeStatus:
    def __init__(self):
        self.active_buffs = {}
        self.ability_2_disabled = {}
        self.special_boost_flat = {}
        self.dmg_amp_stacks = {}
        self.crit_bonus = {}

    def add_buff(self, key, stat, amount, rounds):
        self.active_buffs.setdefault(key, []).append(
            {"stat": stat, "amount": amount, "rounds_left": rounds})

    def get_buff_bonus(self, key, stat):
        return sum(b["amount"] for b in self.active_buffs.get(key, [])
                   if b["stat"] == stat)

    def add_dmg_amp(self, key, pct):
        self.dmg_amp_stacks[key] = self.dmg_amp_stacks.get(key, 0.0) + pct

    def get_dmg_amp(self, key):
        return self.dmg_amp_stacks.get(key, 0.0)

    def set_duration(self, *a, **kw):
        pass


class FakeStamina:
    def __init__(self):
        self.stamina = {"p": 5.0, "e": 5.0}
        self.max_stamina = {"p": 15.0, "e": 15.0}


class FakeSession:
    def __init__(self):
        self.status = FakeStatus()
        self.stamina_manager = FakeStamina()
        self.blades = {"p": AV, "e": BLADES["Shadow Dragon King"]}
        self.hp = {"p": 1000, "e": 2000}
        self.max_hp_per_player = {"p": 2000, "e": 2000}
        self.max_hp = 2000
        self.last_moves = {"p": "attack", "e": "attack"}
        self.round = 1


def engine():
    s = FakeSession()
    e = AbilityEngine.__new__(AbilityEngine)
    e.session = s
    e.st = s.status
    e._compiled = {}
    e.once_fired = set()
    e.modes = {}
    e.counters = {}
    e.ability_2_disabled = {}
    e.debuff_immune = {}
    e.primed_bonus = {}
    e.crit_chance_bonus = {}
    e.special_boost_flat = s.status.special_boost_flat
    e.special_amp_stack = {}
    e.timed_dmg_amps = []
    e.undodgeable_turns = {}
    return e, s


print("\n── 1. the card matches the spec ─────────────────────────────────")
check("Aetherion Vortex exists", AV is not None)
check("rarity Ultimate", AV["rarity"] == "Ultimate", AV["rarity"])
check("type Balance", AV["type"] == "Balance")
check("spin Left", AV["spin_direction"] == "Left")
check("attack 121", AV["stats"]["attack"] == 121)
check("defense 121", AV["stats"]["defense"] == 121)
check("stamina 111", AV["stats"]["stamina"] == 111)
check("hp 111", AV["stats"]["hp"] == 111)
check("image is the one supplied",
      "1535977406840438944/Firefly_1.png" in AV["image_url"])
check("special is Vortex Judgment: Cataclysm Break",
      SM["name"] == "Vortex Judgment: Cataclysm Break")
check("hit 1 is 70, hit 2 is 60", SM["damage_per_hit"] == [70, 60])
check("no id collides",
      sum(1 for b in BLADES.values()
          if isinstance(b, dict) and b.get("id") == AV["id"]) == 1)

print("\n── 2. it takes Azeroth Veyrath's booster slot ───────────────────")
pool = sorted(n for n, b in BLADES.items()
              if isinstance(b, dict) and b.get("booster_exclusive"))
check("Aetherion Vortex is in the booster pool", "Aetherion Vortex" in pool)
check("Azeroth Veyrath is NOT", "Azeroth Veyrath" not in pool, pool)
# The claim this makes is about the SWAP, and the two checks above already
# prove it: Vortex is in, Veyrath is out. Pinning the pool's absolute size made
# that claim depend on every future booster too, and it has since broken on the
# X boosters, which were a deliberate addition and nothing to do with Vortex.
check(f"the pool has not shrunk ({len(pool)} blades)", len(pool) >= 8, pool)
check("Azeroth Veyrath still EXISTS — owners keep it",
      "Azeroth Veyrath" in BLADES)
check("...and is still fully playable",
      BLADES["Azeroth Veyrath"].get("stats", {}).get("attack") == 150)

# This check used to read "it just stops dropping", and that was false for four
# versions. Clearing `booster_exclusive` does not retire a blade — it moves it
# from the booster-only pool into the ORDINARY weighted one, where Veyrath's
# Ultimate rarity made it the single most common Ultimate in the game at
# 1 in 284. Retiring it needs the availability gate, so that is what is
# asserted now: the claim, not a proxy for it.
import random as _r                                              # noqa: E402

import cogs.spawn.spawn as _SPAWN                                # noqa: E402
from utils.availability import obtainable as _obtainable         # noqa: E402

check("its acquisition window is shut", not _obtainable(BLADES["Azeroth Veyrath"]))
_r.seed(7)
_hits = sum(1 for _ in range(50_000)
            if (_SPAWN._pick_random_beyblade(BLADES) or {}).get("name")
            == "Azeroth Veyrath")
check("...so 50,000 wild spawns produce none of it", _hits == 0, _hits)
check("...and it is out of the tournament draft too",
      not any(b.get("name") == "Azeroth Veyrath"
              for b in __import__("cogs.tournament.tournament", fromlist=["x"])
              .draft_pool()))

print("\n── 3. Aether Drain — the stack engine ───────────────────────────")
e, s = engine()
hp0 = s.hp["p"]
e._fire("on_attack_hit", "p", "e", AV, "attack", "win", 100, 0, [])
check("one hit is one stack", e.counters.get(("p", "aether_drain")) == 1,
      e.counters)
check("+3 attack", s.status.get_buff_bonus("p", "attack") == 3)
check("+3 defense", s.status.get_buff_bonus("p", "defense") == 3)
check("+5 HP", s.hp["p"] == hp0 + 5, s.hp["p"])

e, s = engine()
for _ in range(5):
    e._fire("on_attack_hit", "p", "e", AV, "attack", "win", 100, 0, [])
check("5 hits is +15 attack", s.status.get_buff_bonus("p", "attack") == 15,
      s.status.get_buff_bonus("p", "attack"))
check("...and +15 defense", s.status.get_buff_bonus("p", "defense") == 15)
check("...and +25 HP", s.hp["p"] == 1000 + 25, s.hp["p"])
check("no Overdrive before 10 stacks", not e.modes.get("p"), e.modes)
check("no damage amp yet", s.status.get_dmg_amp("p") == 0.0)

print("\n── 4. ...and Overdrive at 10 ────────────────────────────────────")
e, s = engine()
for _ in range(10):
    e._fire("on_attack_hit", "p", "e", AV, "attack", "win", 100, 0, [])
check("Overdrive mode is set", e.modes.get("p") == "Overdrive", e.modes)
check("+25% damage amp", abs(s.status.get_dmg_amp("p") - 0.25) < 1e-9,
      s.status.get_dmg_amp("p"))
check("stacks reset to 0", e.counters.get(("p", "aether_drain"), 0) == 0,
      e.counters)
check("the amp is registered as TIMED, not permanent",
      len(e.timed_dmg_amps) == 1, e.timed_dmg_amps)
check("...for 5 turns", e.timed_dmg_amps[0][2] == 5, e.timed_dmg_amps)

for turn in range(4):
    e.tick_dmg_amps()
    check(f"still amplified after {turn + 1} turn(s)",
          s.status.get_dmg_amp("p") > 0, s.status.get_dmg_amp("p"))
e.tick_dmg_amps()
check("the amp expires on the 5th tick", s.status.get_dmg_amp("p") == 0.0,
      s.status.get_dmg_amp("p"))
check("...and the entry is dropped", not e.timed_dmg_amps)

e, s = engine()
s.status.add_dmg_amp("p", 0.5)          # a permanent amp from elsewhere
e._run_ops({"do": [{"op": "dmg_amp", "value": 0.25, "turns": 1}]},
           "T", "p", "e", "attack", 0, 0, [])
e.tick_dmg_amps()
check("expiring a timed amp does not eat a permanent one",
      abs(s.status.get_dmg_amp("p") - 0.5) < 1e-9, s.status.get_dmg_amp("p"))

print("\n── 5. Tyrant's Decree — a stack every 5 rounds, max 4 ───────────")
e, s = engine()
for rnd in range(1, 26):
    s.round = rnd
    e._fire("turn_start", "p", "e", AV, "attack", "win", 0, 0, [])
    if rnd == 4:
        check("nothing at round 4", s.status.get_buff_bonus("p", "attack") == 0,
              s.status.get_buff_bonus("p", "attack"))
    if rnd == 5:
        check("stack 1 lands at round 5",
              s.status.get_buff_bonus("p", "attack") > 0)
        check("...worth 8% of base attack",
              s.status.get_buff_bonus("p", "attack")
              == int(round(AV["stats"]["attack"] * 0.08)),
              s.status.get_buff_bonus("p", "attack"))
        check("...and +10% special damage",
              abs(e.special_amp_stack.get("p", 0) - 0.10) < 1e-9,
              e.special_amp_stack)
    if rnd == 9:
        check("no Execution Bonus before 2 stacks",
              not e.crit_chance_bonus.get("p"), e.crit_chance_bonus)
    if rnd == 10:
        check("stack 2 lands at round 10",
              abs(e.special_amp_stack.get("p", 0) - 0.20) < 1e-9,
              e.special_amp_stack)
        check("Execution Bonus unlocks with it", e.crit_chance_bonus.get("p", 0) > 0,
              e.crit_chance_bonus)

check("4 stacks by round 25", abs(e.special_amp_stack["p"] - 0.40) < 1e-9,
      e.special_amp_stack)
check("...and no more — the cap holds",
      e.special_amp_stack["p"] <= 0.40)
check("all-stat buff is 4 x 8% = 32% of base",
      s.status.get_buff_bonus("p", "attack")
      == 4 * int(round(AV["stats"]["attack"] * 0.08)),
      s.status.get_buff_bonus("p", "attack"))
check("each stack applied exactly once — one entry per stack per stat",
      len([b for b in s.status.active_buffs["p"] if b["stat"] == "attack"]) == 4,
      len([b for b in s.status.active_buffs["p"] if b["stat"] == "attack"]))

print("\n── 6. the round condition itself ────────────────────────────────")
e, s = engine()
s.round = 3
check("round_at_least is False below the threshold",
      not e._check({"cond": "round_at_least", "value": 5}, "p", "e", "attack", "w"))
s.round = 5
check("...True at it",
      e._check({"cond": "round_at_least", "value": 5}, "p", "e", "attack", "w"))
s.round = 99
check("...and above it",
      e._check({"cond": "round_at_least", "value": 5}, "p", "e", "attack", "w"))
check("round_below is its complement",
      e._check({"cond": "round_below", "value": 100}, "p", "e", "attack", "w"))
del s.round
check("a session with no round counter fails CLOSED",
      not e._check({"cond": "round_at_least", "value": 1}, "p", "e", "attack", "w"))

print("\n── 7. Adaptive Punish — hit 2 scales with YOUR stamina ──────────")
rider = SM["per_hit_bonus"][1]
check("hit 1 has no rider", SM["per_hit_bonus"][0] is None)
check("the rider reads own stamina", rider["source"] == "own_stamina_above")
check("threshold is 7", rider["threshold"] == 7)
check("50 per stack", rider["per"] == 50)
check("capped at 3 stacks", rider["max_stacks"] == 3)


def punish(stamina):
    """The rider, exactly as attack_manager computes it."""
    import math as _m
    stacks = max(0, min(rider["max_stacks"],
                        int(_m.floor(stamina - rider["threshold"]))))
    return stacks * rider["per"]


check("7 stamina is nothing — the threshold is exclusive", punish(7) == 0)
check("7.9 is still nothing", punish(7.9) == 0)
check("the worked example: 8 stamina = 1 stack = +50", punish(8) == 50)
check("9 = 2 stacks = +100", punish(9) == 100)
check("10 = 3 stacks = +150", punish(10) == 150)
check("15 is still 3 stacks — the cap holds", punish(15) == 150)
check("below the threshold cannot go negative", punish(0) == 0 and punish(3) == 0)
check("so hit 2 runs 60 to 210",
      SM["damage_per_hit"][1] + punish(0) == 60
      and SM["damage_per_hit"][1] + punish(10) == 210)

asrc = open(os.path.join(ROOT, "cogs", "battle", "attack_manager.py"),
            encoding="utf-8").read()
check("attack_manager implements the source", "own_stamina_above" in asrc)
check("...reading the live bar, not the stat",
      "stamina_manager.stamina.get(mkey" in asrc)

print("\n── 8. the roster is intact ──────────────────────────────────────")
# A floor, not an equality. Pinning the exact count means every blade added
# afterwards fails a suite that has nothing to do with it — this one has now
# broken on Void Longinus, Aetherion Vortex and the four starters in turn.
check(f"the roster has not shrunk ({len(BLADES)} blades)",
      len(BLADES) >= 80, len(BLADES))
check("Aetherion's Special resolves to its authored shape",
      resolve_special_hits(AV) == [70, 60], resolve_special_hits(AV))
drift = []
for name, b in BLADES.items():
    if not isinstance(b, dict):
        continue
    raw = (b.get("special_move") or {}).get("damage_per_hit")
    if isinstance(raw, list):
        continue
    hits, per_hit, _f, _i = resolve_special(b)
    if resolve_special_hits(b) != [per_hit] * max(1, hits):
        drift.append(name)
check("every scalar-damage blade is still byte-identical", not drift, drift[:4])

broken = []
for name, blade in BLADES.items():
    if not isinstance(blade, dict):
        continue
    try:
        ee, _ = engine()
        ee._rules_for(blade, "p")
        resolve_special_hits(blade)
    except Exception as exc:                             # noqa: BLE001
        broken.append((name, repr(exc)[:60]))
check("every blade compiles and resolves", not broken, broken[:4])
ids = [b["id"] for b in BLADES.values() if isinstance(b, dict) and "id" in b]
check("no duplicate ids", len(ids) == len(set(ids)))

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
