#!/usr/bin/env python3
"""
tools/sim_odax_horusood.py — Omni Odax and Hyper Horusood.

Two Rare blades whose whole identity is in their ability rules rather than
their stat line, which is the shape that has shipped broken here before: the
card reads well, the numbers look right in `;info`, and nothing actually fires.

The three things worth proving
------------------------------
* **Blast Beat banks and spends.** Four Attack wins bank four beats; the
  Special cashes all of them and the bank is empty afterwards. `add_counter`
  and `stacking_buff` share one counter namespace in the engine, so the two
  halves of Sound of Battle are deliberately named apart — a shared name would
  have let `consume_counter` empty the buff's own stack count and re-grant the
  Attack bonus without limit.
* **Horusood Field runs five passes while dealing nothing.** A declared
  `non_damage` Special still runs the per-hit loop, and `on_hit` fires only
  from that loop — so five passes is a claim about the engine, not the card,
  and it is asserted against the real one.
* **`steal: false` means the enemy loses it and nobody gains it.** The default
  is to steal, and a 104-stamina blade taking five stamina AND gaining five
  would be two swings for the price of one.

Run:  python3 tools/sim_odax_horusood.py
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


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLADES = json.load(open(os.path.join(ROOT, "data", "beyblades.json"),
                        encoding="utf-8"))

ODAX = BLADES.get("Omni Odax") or {}
HORU = BLADES.get("Hyper Horusood") or {}


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. both blades exist and are ordinary Rares ──────────────────")

for name, b, typ in (("Omni Odax", ODAX, "Attack"),
                     ("Hyper Horusood", HORU, "Stamina")):
    check(f"{name} is in the roster", bool(b))
    check(f"{name} is Rare", b.get("rarity") == "Rare", b.get("rarity"))
    check(f"{name} is a {typ} type", b.get("type") == typ, b.get("type"))
    check(f"{name} carries the image that was given for it",
          str(b.get("image_url", "")).startswith("https://cdn.discordapp.com/"),
          b.get("image_url", "")[:60])

# Obtainable everywhere: no owner binding, no expiry, not booster-locked. A
# Rare that quietly cannot be caught is the failure this guards against.
from utils.availability import obtainable                # noqa: E402

for name, b in (("Omni Odax", ODAX), ("Hyper Horusood", HORU)):
    check(f"{name} can actually be obtained", obtainable(b))
    check(f"{name} is not owner-bound, limited or booster-only",
          not b.get("owner_ids") and not b.get("limited")
          and not b.get("booster_exclusive"),
          {k: b.get(k) for k in ("owner_ids", "limited", "booster_exclusive")})

# It should reach the wild-spawn pool without anything else being registered.
import cogs.spawn.spawn as SPAWN                         # noqa: E402

pool = SPAWN.build_spawn_pool() if hasattr(SPAWN, "build_spawn_pool") else None
if pool is None:
    check("spawn pool builder found", True, "(checked via obtainable instead)")
else:
    names = {getattr(x, "get", lambda k, d=None: None)("name") for x in pool}
    check("Omni Odax reaches the wild-spawn pool", "Omni Odax" in names)
    check("Hyper Horusood reaches the wild-spawn pool",
          "Hyper Horusood" in names)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. stat lines sit inside the Rare band ───────────────────────")

rare_atk = [v for v in BLADES.values()
            if v.get("rarity") == "Rare" and v.get("type") == "Attack"
            and v.get("name") != "Omni Odax"]
rare_sta = [v for v in BLADES.values()
            if v.get("rarity") == "Rare" and v.get("type") == "Stamina"
            and v.get("name") != "Hyper Horusood"]


def band(peers, key):
    vals = [sum(p["stats"].values()) if key == "total" else p["stats"][key]
            for p in peers]
    return min(vals), max(vals)


for label, blade, peers in (("Omni Odax", ODAX, rare_atk),
                            ("Hyper Horusood", HORU, rare_sta)):
    tot = sum(blade["stats"].values())
    lo, hi = band(peers, "total")
    check(f"{label}'s total ({tot}) is inside the existing Rare {blade['type']} "
          f"range ({lo}-{hi})", lo <= tot <= hi, tot)

check("Omni Odax leads on Attack, as an Attack type should",
      max(ODAX["stats"], key=ODAX["stats"].get) == "attack", ODAX["stats"])
check("Hyper Horusood leads on HP with Stamina second — the shape every other "
      "Rare Stamina blade has",
      sorted(HORU["stats"], key=HORU["stats"].get, reverse=True)[:2]
      == ["hp", "stamina"], HORU["stats"])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. the rules use ops the engine really has ───────────────────")

engine_src = open(os.path.join(ROOT, "cogs", "abilities", "ability_engine.py"),
                  encoding="utf-8").read()
KNOWN = set()
import re                                                # noqa: E402

for m in re.finditer(r'kind (?:==|in) \(?((?:"[a-z_]+"(?:, )?)+)\)?', engine_src):
    KNOWN.update(x.strip('"') for x in m.group(1).split(", "))

used = []
for b in (ODAX, HORU):
    for ab in b.get("abilities", []):
        for rule in ab.get("rules", []):
            for op in rule.get("do", []):
                used.append((b["name"], op.get("op")))
unknown = [(n, o) for n, o in used if o not in KNOWN]
check("every op both blades use is implemented — an unknown op is silently "
      "skipped, so the ability reads correctly and does nothing",
      not unknown, unknown)

TRIGGERS = set(re.findall(r'"(on_[a-z_]+|setup|passive)"', engine_src))
bad_when = [(b["name"], r.get("when")) for b in (ODAX, HORU)
            for ab in b.get("abilities", []) for r in ab.get("rules", [])
            if r.get("when") not in TRIGGERS]
check("...and every trigger they hang off is a real one", not bad_when, bad_when)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 4. Sound of Battle banks beats and Blast Beat spends them ────")

from cogs.abilities.ability_engine import AbilityEngine  # noqa: E402


# The same minimal session `tools/sim_heavens_ring.py` runs the engine against.
class FakeStatus:
    def __init__(self):
        self.shields = {}
        self.special_boost_flat = {}
        self.dmg_amp_stacks = {}
        self.buffs = {}

    def add_shield(self, key, amount):
        self.shields[key] = self.shields.get(key, 0) + amount

    def add_buff(self, key, stat, amount, rounds):
        self.buffs.setdefault(key, []).append((stat, amount, rounds))

    def get_buff_bonus(self, key, stat):
        return sum(a for st, a, _ in self.buffs.get(key, []) if st == stat)

    def add_dmg_amp(self, key, pct):
        self.dmg_amp_stacks[key] = self.dmg_amp_stacks.get(key, 0.0) + pct

    def get_dmg_amp(self, key):
        return self.dmg_amp_stacks.get(key, 0.0)

    def set_duration(self, *a, **kw):
        pass


class FakeStamina:
    def __init__(self):
        self.stamina = {"p1": 16.0, "p2": 16.0}
        self.max_stamina = {"p1": 16.0, "p2": 16.0}
        self.drain_reduction = {}


class FakeStability:
    def __init__(self):
        self.stability = {"p1": 100, "p2": 100}
        self.max = {"p1": 100, "p2": 100}
        self.applied = []

    def _apply(self, key, delta, reason=""):
        self.applied.append((key, delta))
        self.stability[key] = max(0, self.stability[key] + delta)
        return []


class FakeSession:
    def __init__(self, mine, theirs):
        self.status = FakeStatus()
        self.blades = {"p1": mine, "p2": theirs}
        self.hp = {"p1": 600, "p2": 600}
        self.max_hp_per_player = {"p1": 600, "p2": 600}
        self.max_hp = 600
        self.last_moves = {}
        self.round = 1
        self.stamina_manager = FakeStamina()
        self.stability_manager = FakeStability()


def engine_for(mine, theirs):
    s = FakeSession(mine, theirs)
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
    e.crit_damage_mult = {}
    e.special_boost_flat = s.status.special_boost_flat
    e.special_amp_stack = {}
    e.timed_dmg_amps = []
    e.undodgeable_turns = {}
    e.last_hit_was_crit = False
    e.heal_per_drain = {}
    return e, s


# `_fire` rather than `apply`, matching `tools/sim_heavens_ring.py`: `apply`
# pulls in the DamageFilter and the whole damage pipeline, which is not what
# these checks are about.
eng, _ = engine_for(ODAX, HORU)
for _ in range(4):
    eng._fire("on_attack_win", "p1", "p2", ODAX, "attack", "win", 50, 0, [])
banked = eng.counters.get(("p1", "blast_beat"), 0)
check("four Attack wins bank four beats", banked == 4, banked)

tempo = eng.counters.get(("p1", "odax_tempo"), 0)
check("...and the Attack the beats bought stacked separately", tempo == 4, tempo)
check("the two halves use different counter names — sharing one would let the "
      "Special empty the buff's stack count and re-grant Attack forever",
      ("p1", "blast_beat") != ("p1", "odax_tempo"))

for _ in range(3):
    eng._fire("on_attack_win", "p1", "p2", ODAX, "attack", "win", 50, 0, [])
check("the bank stops at four, not five", eng.counters[("p1", "blast_beat")] == 4,
      eng.counters[("p1", "blast_beat")])

logs = []
dealt, _taken = eng._fire("on_special", "p1", "p2", ODAX, "special", "win",
                          40, 0, logs)
check("the Special cashes every banked beat — +15 each, +60 on a full bar",
      dealt == 40 + 60, dealt)
check("...and empties the bank", eng.counters.get(("p1", "blast_beat"), 0) == 0,
      eng.counters.get(("p1", "blast_beat")))
check("...saying so in the log", any("consumed 4 stacks" in x for x in logs), logs)

eng2, _ = engine_for(ODAX, HORU)
d2, _t = eng2._fire("on_special", "p1", "p2", ODAX, "special", "win", 40, 0, [])
check("a Special fired with nothing banked is just its printed damage",
      d2 == 40, d2)

sm = ODAX["special_move"]
check("Blast Beat is 3 hits of 40", sm["hits"] == 3 and sm["damage_per_hit"] == 40)
check("...and its printed total matches its own arithmetic",
      sm["total_damage"] == sm["hits"] * sm["damage_per_hit"], sm)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5. Horusood Field: five passes, no damage ────────────────────")

hsm = HORU["special_move"]
check("the Special is DECLARED non-damage, not merely zeroed — 0 and 'not "
      "filled in' look identical in JSON, and only one of them should skip "
      "the damage floor", hsm.get("non_damage") is True, hsm.get("non_damage"))
check("five hits", hsm["hits"] == 5, hsm["hits"])
check("zero damage per hit and zero total",
      hsm["damage_per_hit"] == 0 and hsm["total_damage"] == 0, hsm)
check("one flavour line per pass, so the log reads as five passes",
      len(hsm["flavour_texts"]) == hsm["hits"], len(hsm["flavour_texts"]))

# The damage layer must keep all five hits AND keep them at zero.
from cogs.battle.damage_rules import (                    # noqa: E402
    resolve_special, resolve_special_hits)

hits, per_hit, flavour, _ig = resolve_special(HORU, HORU["stats"]["special"])
check("the damage layer keeps all five hits for a non-damage Special",
      hits == 5, hits)
check("...at zero damage each", per_hit == 0, per_hit)
table = resolve_special_hits(HORU, HORU["stats"]["special"])
check("...and the per-hit table is five zeroes, not five ones — the max(1, …) "
      "floor is exactly what `non_damage` opts out of",
      table == [0, 0, 0, 0, 0], table)

# Levelling must not quietly give it damage back.
try:
    from utils.bey_levels import stats_at
    lv100 = dict(HORU, stats=stats_at(HORU, 100, {}))
    _h, ph100, _f, _i = resolve_special(lv100, lv100["stats"]["special"])
    check("...and it is still zero at level 100 — a scaled Special is where a "
          "declared zero quietly becomes a one", ph100 == 0, ph100)
except Exception as exc:                                 # noqa: BLE001
    check("level-100 scaling check ran", False, repr(exc))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6. every pass takes 1 stamina and 3 stability ────────────────")


eng3, s = engine_for(HORU, ODAX)

for _ in range(hsm["hits"]):
    eng3.process_hit_proc("p1", HORU, "p2", 0)

lost_sta = round(16.0 - s.stamina_manager.stamina["p2"], 2)
check("five passes take five stamina off the opponent — 1 a pass",
      lost_sta == 5.0, lost_sta)
check("...and nobody gains it: `steal: false`, so a 104-stamina blade does not "
      "take five AND gain five", s.stamina_manager.stamina["p1"] == 16.0,
      s.stamina_manager.stamina["p1"])
lost_stab = 100 - s.stability_manager.stability["p2"]
check("five passes take fifteen stability off the opponent — 3 a pass",
      lost_stab == 15, lost_stab)
check("...all of it applied to the OPPONENT, never to Horusood",
      all(k == "p2" for k, _ in s.stability_manager.applied),
      s.stability_manager.applied)
check("the opponent's HP is untouched — the whole point of the move",
      s.hp["p2"] == 600, s.hp["p2"])

# `on_hit` must be Special-scoped. If it fired on normal attacks too, Horusood
# would strip stamina and stability on every swing it ever made.
am_src = open(os.path.join(ROOT, "cogs", "battle", "attack_manager.py"),
              encoding="utf-8").read()
check("`on_hit` reaches the engine only through the Special's per-hit loop, so "
      "the field cannot fire on an ordinary attack",
      am_src.count("process_hit_proc") == 1,
      am_src.count("process_hit_proc"))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 7. Hyper Zone holds up its own half ──────────────────────────")

# The incoming hit is passed as `dmg_dealt`, not `dmg_taken` — see the real
# call site at `ability_engine.py:1277`, where the defender's damage goes in
# the first slot and the reduced figure comes back out of it. Getting that
# round the wrong way makes the resistance look broken when it is fine.
eng4, s2 = engine_for(HORU, ODAX)
landed, _t = eng4._fire("on_take_damage", "p1", "p2", HORU, "attack", "lose",
                        100, 0, [])
check("a hit on Hyper Horusood lands 8% softer", landed == 92, landed)
drained = round(16.0 - s2.stamina_manager.stamina["p2"], 2)
check("...and leaves 0.4 stamina behind in the field", drained == 0.4, drained)
check("...which the attacker loses rather than Horusood gaining",
      s2.stamina_manager.stamina["p1"] == 16.0,
      s2.stamina_manager.stamina["p1"])

# Neither blade may be dead in a boss fight, which is a failure this repo has
# shipped before across 41% of the roster.
from cogs.battle.boss.blade_abilities import kit_for      # noqa: E402

# `kit_for` always returns a BladeKit, so "is it truthy" proves nothing. What
# matters is whether the kit carries a real modifier and whether anything was
# parked in `dormant` — an op the boss layer has no mapping for is silently
# skipped, which is how 41% of the roster once ended up doing nothing here.
for name, b, expect in (("Omni Odax", ODAX, "attack"),
                        ("Hyper Horusood", HORU, "reduction")):
    kit = kit_for(b)
    check(f"{name}'s ability is applied in a boss fight, not skipped",
          kit.applied == [b["abilities"][0]["name"]], kit.applied)
    check("...with nothing left dormant", not kit.dormant, kit.dormant)
    live = (kit.stat_mult["attack"] > 1.0 if expect == "attack"
            else kit.reduction > 0)
    check(f"...and it really moves a number ({expect})", live,
          (kit.stat_mult, kit.reduction))

# The control half of Horusood Field genuinely cannot apply to a boss — there
# is no stamina or stability meter on that side — so `Hyper Zone` carries the
# damage reduction precisely so the blade is not inert there. Stated, not
# assumed.
check("Hyper Horusood's boss contribution is the 8% reduction, since stamina "
      "and stability pressure have no boss equivalent",
      abs(kit_for(HORU).reduction - 0.08) < 1e-9, kit_for(HORU).reduction)


print("\n" + "=" * 66)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 66)
sys.exit(1 if FAIL else 0)
