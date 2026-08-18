#!/usr/bin/env python3
"""
tools/sim_dead_phoenix.py — the 1-in-1000 Mythic, and the pools it must not be in.

What this covers
----------------
Dead Phoenix was a **Rare** — and already the strongest one at 335
attack+defence+stamina. v1.15 buffs it well past every blade in the game, moves
it to Mythic, and puts it behind its own 1-in-1000 drop.

The interesting part is not the buff, it is the rate. "1 in 1000" is only true
if every OTHER way of getting the blade is closed, and this bot has four
independent random-blade pools that have each leaked at least once:

    wild spawns          cogs/spawn/spawn.py
    booster packs        cogs/economy/shop.py
    tournament drafts    cogs/tournament/tournament.py
    the booster hidden roll

`draft_pool`'s own docstring records the last leak: keeping only
`if b.get("name")` made Ultimate Valkyrie — a 1-in-10,000,000 blade —
draftable at roughly **1 in 578**, and `sim_ultimate_valkyrie.py` stayed green
because it scoped its proof to the spawn and shop pools. So this suite checks
all four, by construction rather than by reading the code.

The other thing worth asserting is the heal. The old ability declared
`heal_pct: 0.0429` AND `activation_heal: 30`, and `legacy_convert` resolved that
pair to a **flat 30** — healing that never scaled with level. At base HP that
was ~24%; at level 100, against a four-figure pool, it was nothing. The rewrite
is checked at the point the engine reads it, not in the JSON.

Run:  python3 tools/sim_dead_phoenix.py
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


from utils.database import get_beyblade, load_beyblades          # noqa: E402

NAME = "Dead Phoenix"
BP = get_beyblade(NAME)
ALL = load_beyblades()


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. the stat line ─────────────────────────────────────────────")
# +53 to every stat, then attack −10 and defence −20.

WANT = {"attack": 121, "defense": 189, "stamina": 154, "special": 178, "hp": 176}
for stat, value in WANT.items():
    check(f"{stat} is {value}", BP["stats"][stat] == value, BP["stats"][stat])

ads = sum(BP["stats"][s] for s in ("attack", "defense", "stamina"))
check("attack+defence+stamina is 464", ads == 464, ads)

# It is meant to be the strongest blade in the game now — asserted, so that if
# something later out-scales it that is a decision somebody makes on purpose.
others = {n: sum(b["stats"][s] for s in ("attack", "defense", "stamina"))
          for n, b in ALL.items() if n != NAME and b.get("stats")}
best_other = max(others.values())
check("...which is the highest total in the roster",
      ads > best_other, f"{ads} vs {best_other} "
                        f"({max(others, key=others.get)})")

# Still clearly a Defense blade — but the gap NARROWED, and that is worth
# recording rather than glossing. The nerf took 10 off attack and 20 off
# defence, so net the blade gained +43 attack against +33 defence: the
# defence-to-attack ratio goes 2.00 (156/78) to 1.56 (189/121). It is still a
# wall, just a slightly less lopsided one than it was.
check("defence still comfortably exceeds attack",
      BP["stats"]["defense"] > BP["stats"]["attack"] * 1.5,
      f'{BP["stats"]["defense"]} vs {BP["stats"]["attack"]}')
check("...though by a narrower ratio than before, which the nerf is why",
      BP["stats"]["defense"] / BP["stats"]["attack"] < 156 / 78,
      round(BP["stats"]["defense"] / BP["stats"]["attack"], 2))
check("defence is still its highest stat",
      BP["stats"]["defense"] == max(BP["stats"].values()), BP["stats"])
check("it is still typed Defense", BP["type"] == "Defense", BP["type"])


# ── what the buff is actually worth once levelled ────────────────────────────
# Defence was already brushing STAT_CAP at level 100 before the change, so most
# of the +33 is absorbed there. This is not a bug — it is what a cap does — but
# it means the defence buff is a low-and-mid-level buff, and that should be on
# the record rather than discovered later by a player at max level.
from utils import bey_levels as BL                                # noqa: E402

_old = dict(BP)
_old["stats"] = dict(BP["stats"], attack=78, defense=156, stamina=101)
_o, _n = BL.stats_at(_old, 100), BL.stats_at(BP, 100)
print(f"       Lv100  atk {_o['attack']}->{_n['attack']}  "
      f"def {_o['defense']}->{_n['defense']}  "
      f"sta {_o['stamina']}->{_n['stamina']}   (cap {BL.STAT_CAP})")
check("attack keeps its full +43 at level 100",
      _n["attack"] - _o["attack"] == 43, _n["attack"] - _o["attack"])
check("stamina keeps its full +53", _n["stamina"] - _o["stamina"] == 53,
      _n["stamina"] - _o["stamina"])
check("defence is capped at level 100, so most of its +33 is absorbed there — "
      "the defence buff is a low-and-mid-level buff",
      _n["defense"] == BL.STAT_CAP and _n["defense"] - _o["defense"] < 33,
      (_o["defense"], _n["defense"]))
check("...and it is a real buff below the cap — +33 at level 1",
      BP["stats"]["defense"] - 156 == 33)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. rarity and its own drop rate ──────────────────────────────")

check("rarity is Mythic", BP["rarity"] == "Mythic", BP["rarity"])
check("it carries a hidden 1-in-1000", BP.get("hidden_drop_one_in") == 1000,
      BP.get("hidden_drop_one_in"))

# The request was scoped to this blade. Changing the TIER weight instead would
# have made Mythic rarer than Ultimate — the tier above it — and dragged the
# other 16 Mythics down with it.
import cogs.spawn.spawn as SPAWN                                 # noqa: E402

check("the Mythic tier weight is untouched",
      SPAWN.RARITY_WEIGHTS["Mythic"] == 2, SPAWN.RARITY_WEIGHTS["Mythic"])
check("...so Ultimate is still the rarest tier",
      SPAWN.RARITY_WEIGHTS["Ultimate"] < SPAWN.RARITY_WEIGHTS["Mythic"],
      SPAWN.RARITY_WEIGHTS)
check("...and the ladder still descends",
      [SPAWN.RARITY_WEIGHTS[t] for t in
       ("Common", "Rare", "Epic", "Legendary", "Mythic", "Ultimate")]
      == sorted((SPAWN.RARITY_WEIGHTS[t] for t in
                 ("Common", "Rare", "Epic", "Legendary", "Mythic", "Ultimate")),
                reverse=True))

others_mythic = [n for n, b in ALL.items()
                 if b.get("rarity") == "Mythic" and n != NAME]
check(f"the other {len(others_mythic)} Mythics keep the ordinary tier route",
      all(not ALL[n].get("hidden_drop_one_in") for n in others_mythic),
      [n for n in others_mythic if ALL[n].get("hidden_drop_one_in")])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. the ability, as the ENGINE reads it ───────────────────────")
# Not as the JSON declares it: the old form declared a percentage heal and a
# flat one together, and the converter silently kept the flat 30.

from cogs.abilities.legacy_convert import legacy_convert         # noqa: E402

ab = BP["abilities"][0]
rules = ab.get("rules") or legacy_convert(ab)
check("there is exactly one rule", len(rules) == 1, len(rules))
rule = rules[0]
ops = {op["op"]: op for op in rule["do"]}

check("it fires passively", rule["when"] == "passive", rule.get("when"))
check("...once per battle", rule.get("once") == "battle", rule.get("once"))
check("...below 43% HP",
      rule["if"][0]["cond"] == "hp_below_pct"
      and abs(rule["if"][0]["value"] - 0.4286) < 1e-6, rule.get("if"))

check("invulnerable is 3 turns, up from 2",
      ops["invulnerable"]["turns"] == 3, ops.get("invulnerable"))
check("the Dead Stinger boost is +120, up from +60",
      ops["special_boost"]["value"] == 120, ops.get("special_boost"))

# The heal is the one that was quietly broken.
check("the heal is a PERCENTAGE, so it scales with level",
      "heal_pct" in ops, sorted(ops))
check("...at 25% of max HP", ops["heal_pct"]["value"] == 25, ops.get("heal_pct"))
check("...and there is no flat heal left to shadow it",
      "heal" not in ops, sorted(ops))
for legacy_key in ("chain", "hp_threshold", "invulnerable_turns",
                   "activation_heal", "special_boost"):
    check(f"the legacy `{legacy_key}` field is gone", legacy_key not in ab)
check("`ability` and `abilities[0]` still agree",
      BP["ability"] == BP["abilities"][0])

# What players are told has to match what runs. The old text said "300 or
# below", a threshold from an HP scale this blade has not used for versions.
desc = ab["description"]
check("the description names the real trigger", "43%" in desc, desc)
check("...the real invulnerability", "3 turns" in desc, desc)
check("...the real heal", "25%" in desc, desc)
check("...the real boost", "+120" in desc, desc)
check("...and no longer claims a flat 300 HP threshold", "300" not in desc, desc)

# The engine has to actually accept it — a rule it cannot parse is a rule that
# silently does nothing, which is how 41% of the roster ended up with dead kits.
import cogs.battle.boss.blade_abilities as BA                    # noqa: E402

kit = BA.kit_for(BP) if hasattr(BA, "kit_for") else None
check("the boss-fight translator reads the new shape without raising",
      kit is not None or not hasattr(BA, "kit_for"))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 4. every other route to the blade is closed ──────────────────")
# "1 in 1000" is only true if this is the ONLY way to get one.

pool_hits = 0
for _ in range(20_000):
    got = SPAWN._pick_random_beyblade(ALL)
    if got and got.get("name") == NAME:
        pool_hits += 1
check("20,000 weighted spawn rolls never produce it", pool_hits == 0, pool_hits)

import cogs.economy.shop as SHOP                                 # noqa: E402

shop_pool = SHOP._booster_pool() if hasattr(SHOP, "_booster_pool") else []
check("it is not in the booster pack pool",
      not any(b.get("name") == NAME for b in shop_pool))
check("...and not in the booster HIDDEN pool either — that one is gated on "
      "`booster_exclusive`, which this blade is not",
      not any(b.get("name") == NAME for b in SHOP._hidden_drop_pool()))
check("...because it is not booster_exclusive",
      not BP.get("booster_exclusive"))

from cogs.tournament.tournament import draft_pool                # noqa: E402

check("it cannot be dealt in a tournament draft",
      not any(b.get("name") == NAME for b in draft_pool()))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5. the rate itself, simulated ────────────────────────────────")
# Seeded so this is reproducible rather than flaky.

random.seed(20260818)
TRIALS = 200_000
hits = sum(1 for _ in range(TRIALS)
           if (SPAWN._roll_hidden_spawn(ALL) or {}).get("name") == NAME)
rate = hits / TRIALS
print(f"       {hits} hits in {TRIALS:,} spawns — 1 in {TRIALS / hits:,.0f}"
      if hits else "       0 hits")
# Expected 200 hits, sd ~14. A ±5sd band is wide enough never to flake and
# tight enough to catch a rate that is out by even 40%.
check("the observed rate sits on 1-in-1000", 130 <= hits <= 270, hits)
# Ultimate Valkyrie also rolls here, at 1-in-10,000,000. Each candidate gets
# its OWN independent test, so adding a second hidden blade must not move the
# first one's odds — that is the property `_roll_hidden_spawn` promises, and
# the reason a 1-in-1000 blade can share the mechanism with a 1-in-10,000,000
# one without either being diluted.
sharing = sorted(n for n, b in ALL.items()
                 if b.get("hidden_drop_one_in") and not b.get("booster_exclusive"))
print(f"       sharing the spawn hidden roll: {', '.join(sharing)}")

random.seed(20260818)
solo = {NAME: ALL[NAME]}
solo_hits = sum(1 for _ in range(TRIALS)
                if (SPAWN._roll_hidden_spawn(solo) or {}).get("name") == NAME)
check("its rate is the same with the other hidden blade removed — the rolls "
      "are independent, not a shared draw",
      abs(solo_hits - hits) < 70, (hits, solo_hits))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6. the 18 existing owners are not disturbed ──────────────────")
# The rarity change is retroactive by nature — a blade already in an inventory
# is stored by NAME, so it simply becomes a Mythic in place.

from utils.database import load_users                            # noqa: E402

owners = [uid for uid, p in (load_users() or {}).items()
          if isinstance(p, dict) and NAME in (p.get("inventory") or [])]
print(f"       {len(owners)} profile(s) currently hold one")
check("inventories store the blade by name, so nothing needs migrating",
      all(isinstance(n, str)
          for uid in owners[:5]
          for n in (load_users()[uid].get("inventory") or [])))

from cogs.spawn.spawn import BEY_QUICKSELL_VALUE                 # noqa: E402

check("their quick-sell value follows the new rarity",
      BEY_QUICKSELL_VALUE["Mythic"] == 10_000,
      BEY_QUICKSELL_VALUE.get("Mythic"))


print("\n" + "=" * 66)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 66)
sys.exit(1 if FAIL else 0)
