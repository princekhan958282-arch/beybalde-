#!/usr/bin/env python3
"""
tools/sim_heavens_ring.py — Heaven's Ring, and the three ops written for it.

Why this suite exists
---------------------
Heaven's Ring asked for four things the ability engine could not express, and
"the engine cannot do this" fails in the worst possible way here: the ability
renders perfectly on the info card, the description reads exactly as intended,
and nothing happens in the battle. 41% of the roster sat in that state until
v1.11 went looking. So every mechanic below is driven through a REAL
AbilityEngine and asserted on the number that comes out, never on the JSON.

One of the four turned out to exist already — a rule-level `chance` key, which
`_rule_fires` has always honoured. The other three were genuinely absent:

  stacking_resist   resistance that BUILDS per impact. `reduce_damage_pct` is a
                    fixed number and `stacking_buff` grants a flat stat, so
                    "7% per stack up to 5" had no representation at all.
  crit resistance   folded into the same op as `crit_per_stack`, because the
                    defender's ops run AFTER the mover's crit multiplier has
                    already been folded into the damage — without a flag that
                    survives that far, "resist critical damage" cannot know a
                    crit happened.
  shield_pct        `shield` takes an int. HP here runs from ~145 at level 1
                    into four figures at 100, so a flat number is right at
                    exactly one HP total.

And one trap worth recording: `bonus_damage_pct` silently ignores `turns`. The
first draft of Holy Bague used it for "1.5x for 4 turns" and would have boosted
only the Special that fired it. `dmg_amp` is the op that actually tracks an
expiry, and the four turns are counted here rather than trusted.

Run:  python3 tools/sim_heavens_ring.py
"""
import os
import random
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


from cogs.abilities.ability_engine import AbilityEngine      # noqa: E402
from utils.database import get_beyblade, load_beyblades      # noqa: E402

NAME = "Heaven's Ring"
HR = get_beyblade(NAME)
ALL = load_beyblades()
OWNER = 103025059165


# ── the smallest session the engine will run against ─────────────────────────

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
        return sum(a for s, a, _ in self.buffs.get(key, []) if s == stat)

    def add_dmg_amp(self, key, pct):
        self.dmg_amp_stacks[key] = self.dmg_amp_stacks.get(key, 0.0) + pct

    def get_dmg_amp(self, key):
        return self.dmg_amp_stacks.get(key, 0.0)

    def set_duration(self, *a, **kw):
        pass


class FakeSession:
    def __init__(self, hp=800):
        self.status = FakeStatus()
        self.blades = {"p": HR, "e": HR}
        self.hp = {"p": hp, "e": 1000}
        self.max_hp_per_player = {"p": 1000, "e": 1000}
        self.max_hp = 1000
        self.last_moves = {}
        self.round = 1


def engine(hp=800):
    s = FakeSession(hp)
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
    return e, s


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. the card matches the spec ─────────────────────────────────")

check("Heaven's Ring is in the roster", HR is not None)
check("rarity Ultimate", HR["rarity"] == "Ultimate", HR.get("rarity"))
check("type Defense", HR["type"] == "Defense", HR.get("type"))
for stat, want in (("hp", 145), ("attack", 88), ("defense", 171),
                   ("stamina", 112)):
    check(f"{stat} is {want}", HR["stats"][stat] == want, HR["stats"].get(stat))
# `special` was not in the spec. Set to match Inferno Viper — the only other
# Ultimate Defense blade — rather than invented, so it does not quietly become
# the strongest-Special defence blade in the game.
check("special matches the only other Ultimate Defense blade",
      HR["stats"]["special"] == get_beyblade("Inferno Viper")["stats"]["special"],
      HR["stats"]["special"])
check("the image is the one supplied",
      "1539357270574370956/Untitled_design.png" in HR["image_url"])
check("its id is unique",
      sum(1 for b in ALL.values() if b.get("id") == HR["id"]) == 1, HR["id"])

sm = HR["special_move"]
check("the Special is Holy Bague", sm["name"] == "Holy Bague", sm.get("name"))
check("2 hits x 77", sm["hits"] == 2 and sm["damage_per_hit"] == 77,
      (sm.get("hits"), sm.get("damage_per_hit")))
check("...totalling 154", sm["total_damage"] == 154, sm.get("total_damage"))
check("hits x damage really is the stated total",
      sm["hits"] * sm["damage_per_hit"] == sm["total_damage"])
check("it has flavour to print", bool(sm.get("flavour_texts")))

names = [a["name"] for a in HR["abilities"]]
check("all three abilities are on the plural list the engine reads",
      names == ["Divine", "Angelic Counter", "Holy Bague"], names)
# The recurring trap in this roster: the engine runs `abilities`, and the
# singular `ability` is display only. A blade authored with the singular alone
# has an ability that never fires.
check("the singular mirrors the first of them",
      HR["ability"]["name"] == HR["abilities"][0]["name"])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. player exclusive — every route is shut ────────────────────")

from utils.availability import obtainable                    # noqa: E402

check("nobody in general can obtain it", not obtainable(HR))
check("the named owner can", obtainable(HR, OWNER))
check("...and nobody else can", not obtainable(HR, 956773141265391676))
check("it is flagged limited", HR.get("limited") is True)
check("...and bound to exactly one id", HR.get("owner_ids") == [OWNER],
      HR.get("owner_ids"))

import cogs.economy.shop as SHOP                             # noqa: E402
import cogs.spawn.spawn as SPAWN                             # noqa: E402
from cogs.tournament.tournament import draft_pool            # noqa: E402

random.seed(11)
spawned = sum(1 for _ in range(50_000)
              if (SPAWN._pick_random_beyblade(ALL) or {}).get("name") == NAME)
check("50,000 wild spawns produce none", spawned == 0, spawned)
check("it is not in the booster pack pool",
      not any(b.get("name") == NAME for b in SHOP._load_booster_pool()))
check("...nor the booster hidden pool",
      not any(b.get("name") == NAME for b in SHOP._hidden_drop_pool()))
check("...nor a tournament draft",
      not any(b.get("name") == NAME for b in draft_pool()))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. Divine — 7% per stack, up to 5 ────────────────────────────")
# Driven through the real engine. The stack is taken BEFORE the reduction is
# applied, so the impact that grants a stack is also resisted by it — an
# ability that reads "gain resistance on impact" and then does nothing to that
# impact is the kind of thing nobody notices for four versions.

e, _ = engine()
seen = []
for _ in range(7):
    dmg, _t = e._fire("on_take_damage", "p", "e", HR, "attack", "lose", 100, 0, [])
    seen.append(dmg)
print(f"       100-damage impacts -> {seen}")
check("the first impact already resists 7%", seen[0] == 93, seen[0])
check("...the second 14%", seen[1] == 86, seen[1])
check("...the fifth 35%", seen[4] == 65, seen[4])
check("it caps at 5 stacks and stops growing",
      seen[5] == 65 and seen[6] == 65, seen[5:])
check("the counter itself stops at 5",
      e.counters[("p", "divine")] == 5, e.counters.get(("p", "divine")))

# The crit half. The defender's ops run after the mover's crit multiplier has
# been folded in, so this only works because the crit sets a flag that survives.
e2, _ = engine()
e2.last_hit_was_crit = True
crit_seen = []
for _ in range(5):
    dmg, _t = e2._fire("on_take_damage", "p", "e", HR, "attack", "lose", 100, 0, [])
    crit_seen.append(dmg)
print(f"       critical impacts     -> {crit_seen}")
check("a crit is resisted 9% at one stack (7 + 2)", crit_seen[0] == 91, crit_seen[0])
check("...and 45% at five (35 + 10)", crit_seen[4] == 55, crit_seen[4])
check("crits are resisted harder than ordinary hits at every stack",
      all(c < n for c, n in zip(crit_seen, seen)), (crit_seen, seen))

# The flag has to be per-hit, or one crit makes every later hit resist as if it
# had crit too.
e3, _ = engine()
e3.last_hit_was_crit = True
e3._fire("on_take_damage", "p", "e", HR, "attack", "lose", 100, 0, [])
e3.last_hit_was_crit = False
d2, _ = e3._fire("on_take_damage", "p", "e", HR, "attack", "lose", 100, 0, [])
check("a crit does not leak into the next hit", d2 == 86, d2)

# Stacking must never reach immunity.
e4, _ = engine()
big = [{"op": "stacking_resist", "name": "x", "per_stack": 50, "max": 10}]
for _ in range(10):
    dmg, _t = e4._run_ops({"do": big}, "T", "p", "e", "attack", 100, 0, [])
check("a runaway stack is capped short of immunity", dmg > 0, dmg)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 4. Angelic Counter — 30% to reflect 55% ──────────────────────")
# `chance` is a rule-level key `_rule_fires` has always honoured, so this
# ability needed no new op — only a check that the odds are what they claim.

random.seed(3)
TRIALS = 20_000
fired = 0
for _ in range(TRIALS):
    e5, _ = engine()
    _d, taken = e5._fire("on_take_damage", "p", "e", HR, "attack", "lose",
                         100, 0, [])
    if taken > 0:
        fired += 1
rate = fired / TRIALS
print(f"       reflected on {fired:,} of {TRIALS:,} impacts — {rate:.1%}")
# Expected 6,000, sd ~65. A +/-5sd band never flakes and still catches a rate
# that is out by even 10%.
check("it fires on about 30% of impacts", 0.285 <= rate <= 0.315, f"{rate:.3f}")

# 55% of what ACTUALLY landed, not of what was thrown. Divine runs first and
# absorbs 7% of the incoming 100, so the reflect is 55% of 93 = 52, not 55.
# That ordering is the coherent reading — you throw back what hit you, not what
# was aimed at you — and it is asserted rather than left to be noticed later as
# "the reflect is 3 short".
e6, _ = engine()
reflected = 0
random.seed(1)
for _ in range(60):
    _d, taken = e6._fire("on_take_damage", "p", "e", HR, "attack", "lose",
                         100, 0, [])
    if taken:
        reflected = taken
        break
check("...and reflects 55% of the damage that got through Divine (55% of 93)",
      reflected == 52, reflected)

# With absorption out of the way the raw 55% is visible, which is what proves
# the number above is the absorption and not a rounding accident.
e6b, _ = engine()
only_reflect = [{"op": "reflect_pct", "value": 55}]
_d, taken = e6b._run_ops({"do": only_reflect}, "Angelic Counter", "p", "e",
                         "attack", 100, 0, [])
check("...which is a clean 55 with no absorption in front of it", taken == 55,
      taken)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5. Holy Bague — shield and a 1.5x that really expires ────────")

e7, s7 = engine(hp=800)
e7._fire("on_special", "p", "e", HR, "special", "win", 0, 0, [])
check("the shield is 10% of CURRENT hp", s7.status.shields.get("p") == 80,
      s7.status.shields)
check("the amp is +50%, i.e. 1.5x", abs(s7.status.get_dmg_amp("p") - 0.5) < 1e-9,
      s7.status.get_dmg_amp("p"))

# `bonus_damage_pct` ignores `turns` entirely — the first draft used it and the
# boost would have lasted exactly one hit. Counted here rather than trusted.
alive = []
for _ in range(6):
    e7.tick_dmg_amps()
    alive.append(round(s7.status.get_dmg_amp("p"), 2))
print(f"       amp after each turn -> {alive}")
check("the boost survives turns 1-3", alive[:3] == [0.5, 0.5, 0.5], alive[:3])
check("...and is gone after the fourth", alive[3] == 0.0, alive[3])
check("...and stays gone", alive[4:] == [0.0, 0.0], alive[4:])

# A shield off a smaller pool has to be smaller — that is the whole reason the
# op is a percentage.
e8, s8 = engine(hp=200)
e8._fire("on_special", "p", "e", HR, "special", "win", 0, 0, [])
check("the shield scales with the HP pool it is taken from",
      s8.status.shields.get("p") == 20, s8.status.shields)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6. nothing in the kit is dead in a boss fight ────────────────")

from cogs.battle.boss.blade_abilities import kit_for          # noqa: E402

kit = kit_for(HR)
check("the boss translator accepts the kit", kit is not None)
check("...with nothing falling through as dormant",
      not getattr(kit, "dormant", []), getattr(kit, "dormant", None))
# The stacking resistance is translated at its FULL value (7 x 5), not its
# opening 7% — a boss fight runs long enough that the cap is always reached.
check("the stacking resistance arrives at its stacked value",
      abs(kit.reduction - 0.35) < 1e-9, kit.reduction)
check("the reflect arrives too", kit.reflect > 0, kit.reflect)
check("...and the Holy Bague amp", kit.dmg_amp > 0, kit.dmg_amp)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 7. Azeroth Veyrath is out of the wild pool ───────────────────")
# Not this blade, but the same session's second job and the same class of bug:
# clearing `booster_exclusive` in v1.11 was meant to retire it and instead
# moved it into the ORDINARY weighted pool, where its Ultimate rarity made it
# the single most common Ultimate in the game at 1 in 284.

AV = get_beyblade("Azeroth Veyrath")
check("its acquisition window is shut", not obtainable(AV))
random.seed(5)
av_hits = sum(1 for _ in range(50_000)
              if (SPAWN._pick_random_beyblade(ALL) or {}).get("name")
              == "Azeroth Veyrath")
check("50,000 wild spawns produce none of it", av_hits == 0, av_hits)
check("...it is out of the tournament draft as well",
      not any(b.get("name") == "Azeroth Veyrath" for b in draft_pool()))
check("but it still EXISTS, and its owner keeps it",
      "Azeroth Veyrath" in ALL and AV["stats"]["attack"] == 150)
# The three Ultimates that remain rollable are the ones that should be.
rollable = sorted(n for n, b in ALL.items()
                  if b.get("rarity") == "Ultimate" and obtainable(b)
                  and not b.get("booster_exclusive")
                  and not b.get("hidden_drop_one_in"))
print(f"       Ultimates still reachable from a wild spawn: {rollable}")
check("neither Veyrath nor Heaven's Ring is among them",
      "Azeroth Veyrath" not in rollable and NAME not in rollable, rollable)


print("\n" + "=" * 66)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 66)
sys.exit(1 if FAIL else 0)
