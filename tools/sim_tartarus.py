#!/usr/bin/env python3
"""
tools/sim_tartarus.py — Tartarus Reaper's rework, and the special-move
machinery it needed.

Three engine gaps had to be closed to author it, and the first was a live bug:

  per-hit damage lists were AVERAGED. `damage_per_hit: [110, 70]` became 90/90,
  so a front-loaded Special silently became a flat one. Tartarus Reaper has
  carried that list all along and never once dealt 110 then 70.

  per_hit_bonus  a rider that reads live battle state — here, the stability the
                 opponent has already lost — so a finisher can hit harder the
                 more it has already rattled them.

  special_amp_stack  a cumulative PERCENTAGE Special amp. `special_boost` is
                     flat and does not compound; `bonus_damage_pct` is per-move
                     and is gone by the next Special.

The regression that matters most: **no other blade's Special may change.**

Run:  python3 tools/sim_tartarus.py
"""
import json
import math
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


from cogs.abilities.ability_engine import AbilityEngine       # noqa: E402
from cogs.battle.damage_rules import (                        # noqa: E402
    resolve_special, resolve_special_hits)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLADES = json.load(open(os.path.join(ROOT, "data", "beyblades.json"),
                        encoding="utf-8"))
TR = BLADES["Tartarus Reaper"]
SM = TR["special_move"]


class FakeStability:
    def __init__(self):
        self.stability = {"p": 100, "e": 100}
        self.max = {"p": 100, "e": 100}

    def _apply(self, key, delta):
        self.stability[key] = max(0, self.stability[key] + delta)
        return []


class FakeStatus:
    def __init__(self):
        self.active_buffs = {}
        self.ability_2_disabled = {}
        self.special_boost_flat = {}
        self.pre_special_amp = {}

    def add_buff(self, key, stat, amount, rounds):
        self.active_buffs.setdefault(key, []).append(
            {"stat": stat, "amount": amount, "rounds_left": rounds})

    def get_buff_bonus(self, key, stat):
        return sum(b["amount"] for b in self.active_buffs.get(key, [])
                   if b["stat"] == stat)

    def set_duration(self, *a, **kw):
        pass


class FakeStamina:
    def __init__(self):
        self.stamina = {"p": 5.0, "e": 5.0}
        self.max_stamina = {"p": 15.0, "e": 15.0}
        self.drain_reduction = {}


class FakeSession:
    def __init__(self):
        self.status = FakeStatus()
        self.stability_manager = FakeStability()
        self.stamina_manager = FakeStamina()
        self.blades = {"p": TR, "e": BLADES["Shadow Dragon King"]}
        self.hp = {"p": 1000, "e": 2000}
        self.max_hp_per_player = {"p": 2000, "e": 2000}
        self.max_hp = 2000
        self.last_moves = {"p": "special", "e": "attack"}


def engine():
    s = FakeSession()
    e = AbilityEngine.__new__(AbilityEngine)
    e.session = s
    e.st = s.status
    e._compiled = {}
    e.once_fired = set()
    e.modes = {}
    e.ability_2_disabled = {}
    e.debuff_immune = {}
    e.primed_bonus = {}
    e.special_boost_flat = s.status.special_boost_flat
    e.special_amp_stack = {}
    return e, s


print("\n── 1. the card matches the rework ───────────────────────────────")
names = [a["name"] for a in TR["abilities"]]
check("abilities are Soul Judgement and Grave Decree",
      names == ["Soul Judgement", "Grave Decree"], names)
check("the dead legacy `ability` block is gone", "ability" not in TR)
check("the dead legacy `ability_2` block is gone", "ability_2" not in TR)
check("special is Judgment of the Reaper",
      SM["name"] == "Judgment of the Reaper")
check("2 hits", SM["hits"] == 2)
check("authored as 110 then 50", SM["damage_per_hit"] == [110, 50],
      SM["damage_per_hit"])
check("hit 2 carries a stability rider",
      SM["per_hit_bonus"][1]["source"] == "enemy_stability_lost")
check("...at 50%", SM["per_hit_bonus"][1]["pct"] == 0.5)
check("hit 1 carries no rider", SM["per_hit_bonus"][0] is None)

print("\n── 2. per-hit lists are no longer averaged ──────────────────────")
check("Tartarus resolves to [110, 50], not [80, 80]",
      resolve_special_hits(TR) == [110, 50], resolve_special_hits(TR))
check("the old uniform path still averages — it is unchanged",
      resolve_special(TR)[1] == 80, resolve_special(TR)[1])
check("a scalar blade repeats its value",
      resolve_special_hits(BLADES["Bloody Longinus"]) == [128],
      resolve_special_hits(BLADES["Bloody Longinus"]))
check("a multi-hit scalar blade repeats it per hit",
      resolve_special_hits(BLADES["Rage Longinus"])
      == [resolve_special(BLADES["Rage Longinus"])[1]] * 4)
check("the table is always as long as `hits`",
      all(len(resolve_special_hits(b)) == max(1, resolve_special(b)[0])
          for b in BLADES.values() if isinstance(b, dict)))

print("\n── 3. no other blade's Special damage moved ─────────────────────")
# The regression that matters. For every blade whose damage_per_hit is NOT a
# list, the new per-hit table must be exactly the old uniform value repeated.
drift = []
for name, b in BLADES.items():
    if not isinstance(b, dict):
        continue
    raw = (b.get("special_move") or {}).get("damage_per_hit")
    if isinstance(raw, list):
        continue                      # these are the ones intended to change
    hits, per_hit, _f, _i = resolve_special(b)
    if resolve_special_hits(b) != [per_hit] * max(1, hits):
        drift.append(name)
check("every scalar-damage blade is byte-identical", not drift, drift[:5])

# Three blades carry a damage list, and all three change SHAPE — they finally
# deal what their card says. What must not change is the TOTAL: averaging and
# then re-distributing the same authored numbers is a redistribution, not a
# buff, so nobody's Special gets stronger or weaker overall.
listed = sorted(n for n, b in BLADES.items() if isinstance(b, dict)
                and isinstance((b.get("special_move") or {}).get("damage_per_hit"),
                               list))
# Not pinned to an exact list — every new blade with a per-hit list would
# otherwise fail this suite for doing nothing wrong. What matters is that each
# one preserves its total, checked per blade just below.
check("Tartarus Reaper is among the blades with a damage list",
      "Tartarus Reaper" in listed, listed)
print(f"       list blades: {listed}")
for name in listed:
    b = BLADES[name]
    hits, per_hit, _f, _i = resolve_special(b)
    before, after = per_hit * hits, sum(resolve_special_hits(b))
    check(f"{name}: total damage is unchanged ({before})", before == after,
          (before, after))
    check(f"{name}: but the shape now matches the card",
          resolve_special_hits(b) != [per_hit] * hits,
          resolve_special_hits(b))

print("\n── 4. Soul Judgement ────────────────────────────────────────────")
e, s = engine()
hp_before = s.hp["p"]
sp_before = s.stamina_manager.stamina["p"]
s.last_moves["e"] = "attack"
e._fire("on_take_damage", "p", "e", TR, "defense", "lose", 0, 100, [])
check("+30 attack from a taken Attack hit",
      s.status.get_buff_bonus("p", "attack") == 30,
      s.status.get_buff_bonus("p", "attack"))
check("+30 defense too", s.status.get_buff_bonus("p", "defense") == 30)
check("+30 stamina stat too", s.status.get_buff_bonus("p", "stamina") == 30)
check("heals 30 HP", s.hp["p"] == hp_before + 30, s.hp["p"])
check("recovers 0.3 stamina",
      abs(s.stamina_manager.stamina["p"] - (sp_before + 0.3)) < 1e-6,
      s.stamina_manager.stamina["p"])

e, s = engine()
s.last_moves["e"] = "special"
e._fire("on_take_damage", "p", "e", TR, "defense", "lose", 0, 100, [])
check("a taken SPECIAL hit triggers it too",
      s.status.get_buff_bonus("p", "attack") == 30)

e, s = engine()
s.last_moves["e"] = "defense"
e._fire("on_take_damage", "p", "e", TR, "attack", "win", 0, 0, [])
check("a hit from anything else does NOT trigger it",
      s.status.get_buff_bonus("p", "attack") == 0,
      s.status.get_buff_bonus("p", "attack"))

e, s = engine()
s.last_moves["e"] = "attack"
for _ in range(5):
    e._fire("on_take_damage", "p", "e", TR, "defense", "lose", 0, 50, [])
check("it accumulates across hits — 5 hits is +150",
      s.status.get_buff_bonus("p", "attack") == 150,
      s.status.get_buff_bonus("p", "attack"))

print("\n── 5. Grave Decree — the standing 15% ───────────────────────────")
e, s = engine()
for _round in range(8):
    e._fire("passive", "p", "e", TR, "attack", "win", 0, 0, [])
want = int(round(TR["stats"]["attack"] * 0.15))
check("+15% attack after 8 rounds, applied ONCE",
      s.status.get_buff_bonus("p", "attack") == want,
      (s.status.get_buff_bonus("p", "attack"), want))
check("...one buff entry per stat, not eight",
      len(s.status.active_buffs.get("p", [])) == 3,
      len(s.status.active_buffs.get("p", [])))

print("\n── 6. Grave Decree — the 4-turn 15%→30% window ──────────────────")
e, s = engine()
e._fire("passive", "p", "e", TR, "attack", "win", 0, 0, [])
standing = s.status.get_buff_bonus("p", "attack")
e._fire("on_special", "p", "e", TR, "special", "win", 0, 0, [])
boosted = s.status.get_buff_bonus("p", "attack")
check("firing the Special doubles the decree to 30%",
      boosted == standing * 2, (standing, boosted))
window = [b for b in s.status.active_buffs["p"]
          if b["stat"] == "attack" and b["rounds_left"] == 4]
check("...for exactly 4 turns", len(window) == 1,
      [b["rounds_left"] for b in s.status.active_buffs["p"]])
for _tick in range(4):
    for b in list(s.status.active_buffs["p"]):
        b["rounds_left"] -= 1
        if b["rounds_left"] <= 0:
            s.status.active_buffs["p"].remove(b)
check("and settles back to 15%, not to zero",
      s.status.get_buff_bonus("p", "attack") == standing,
      s.status.get_buff_bonus("p", "attack"))

print("\n── 7. Grave Decree — +50% Special damage per use ────────────────")
e, s = engine()
check("no amp before the first Special", e.special_amp_stack.get("p", 0) == 0)
for use in range(1, 5):
    e._fire("on_special", "p", "e", TR, "special", "win", 0, 0, [])
    check(f"use {use} -> +{use * 50}%",
          abs(e.special_amp_stack["p"] - 0.50 * use) < 1e-9,
          e.special_amp_stack["p"])
for _ in range(20):
    e._fire("on_special", "p", "e", TR, "special", "win", 0, 0, [])
check("the stack is capped, so it cannot run away",
      e.special_amp_stack["p"] <= 4.0, e.special_amp_stack["p"])
check("the amp persists — it is not a timed buff",
      e.special_amp_stack["p"] > 0)

print("\n── 8. hit 2 scales with the stability already lost ──────────────")


def hit2(stability_lost):
    """What the rider adds, exactly as attack_manager computes it."""
    start, pct = 100.0, SM["per_hit_bonus"][1]["pct"]
    now = start - stability_lost
    return int(math.floor(max(0.0, start - now) * pct))


check("the worked example: 50 lost -> 50 + 25 = 75",
      SM["damage_per_hit"][1] + hit2(50) == 75,
      SM["damage_per_hit"][1] + hit2(50))
check("nothing lost -> the base 50", SM["damage_per_hit"][1] + hit2(0) == 50)
check("100 lost -> 50 + 50 = 100", SM["damage_per_hit"][1] + hit2(100) == 100)
check("it scales smoothly, not in steps",
      [hit2(x) for x in (10, 20, 30)] == [5, 10, 15])
check("hit 1 is untouched by the rider", SM["damage_per_hit"][0] == 110)

asrc = open(os.path.join(ROOT, "cogs", "battle", "attack_manager.py"),
            encoding="utf-8").read()
check("the rider reads the stability manager's own baseline",
      "stab.max.get(okey" in asrc)
check("...so a type starting above 100 is not read as having lost stability",
      "START_STABILITY" not in asrc)
check("the rider only fires for the declared source",
      'spec.get("source") != "enemy_stability_lost"' in asrc)
check("the amp is read once and applied per hit, not squared",
      "_amp = 1.0" in asrc and "base_for_hit * _amp" in asrc)

print("\n── 9. the roster is intact ──────────────────────────────────────")
check("the roster is not smaller than when this was written",
      len(BLADES) >= 79, len(BLADES))
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
check("every blade compiles and resolves its Special", not broken, broken[:4])

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
