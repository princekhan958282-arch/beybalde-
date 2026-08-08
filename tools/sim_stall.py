#!/usr/bin/env python3
"""
tools/sim_stall.py — a PvP fight must always end.

Reproduces the reported bug: an Ætherion Nemesis copy in PvP frozen at 223 HP
and refusing to die. Two independent causes, both covered here.

1. **The Shield Gate returned literal zero.** Any Attack whose raw value fell
   below `def_stat x SHIELD_GATE_RATIO` was nullified completely. A Nemesis copy
   rolls DEF 90-147, putting the gate at 54-88, while a normal Attack lands at
   `atk x LOSING_PENALTY_MULT` — so an attacker needed ~135+ ATK to deal ANY
   damage, against a roster averaging 97. Most blades literally could not
   scratch it.

2. **Attrition stopped out-pacing stamina regen.** `attrition.py` exists to stop
   exactly this stall, but its bands were tuned when the Stamina move restored a
   flat +3 with no passive regen. Once recovery scaled with the stat (up to +9)
   and passive regen became real (up to +2.15), a staller out-regenerated the
   bleed for 46-115 rounds depending on stat — and a Discord battle with a
   30-second move timer never gets there.

Run:  python3 tools/sim_stall.py
"""
import math
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


import cogs.battle.damage_rules as DR                  # noqa: E402
import cogs.battle.stamina_manager as SM               # noqa: E402
from cogs.abilities import type_system as TS           # noqa: E402
from cogs.battle.boss import boss_abilities as AB, boss_copy as BC  # noqa: E402


def regen_per_round(sta):
    """What a staller gains each round pressing the free Stamina move."""
    return (SM.STAMINA_RECOVERY_BASE + sta * SM.STAMINA_RECOVERY_PER_STAT
            + SM.STAMINA_REGEN_BASE + sta * SM.STAMINA_REGEN_PER_STAT)


def ko_round(sta, btype="balance", limit=300):
    """Round at which a pure staller finally runs the bar to zero."""
    bar = SM.max_stamina_for(sta)
    sp = bar * 0.5
    for r in range(1, limit):
        sp = min(bar, sp + regen_per_round(sta))
        sp = max(0.0, sp - TS.attrition_drain_for(btype, r, sta))
        if sp <= 0:
            return r
    return None


print("\n── 1. the Shield Gate no longer returns absolute zero ───────────")
for raw, dfn in ((10, 200), (40, 112), (1, 500), (66, 112)):
    dmg, absorbed = DR._shield_gate(raw, dfn)
    check(f"raw {raw} vs DEF {dfn} still does something", dmg >= 1, dmg)
    check("  ...and absorbed stays non-negative", absorbed >= 0, absorbed)
check("a fully-gated hit is still heavily reduced",
      DR._shield_gate(40, 112)[0] <= 40 * 0.10, DR._shield_gate(40, 112))
check("chip scales with the hit, so a big attacker isn't equal to a small one",
      DR._shield_gate(80, 500)[0] > DR._shield_gate(10, 500)[0],
      (DR._shield_gate(80, 500), DR._shield_gate(10, 500)))
check("a hit that CLEARS the gate is unchanged by this fix",
      DR._shield_gate(100, 112)[0] == max(5, 100 - math.ceil(112 * DR.SHIELD_FLAT_RATIO)))
check("zero DEF is unaffected", DR._shield_gate(50, 0)[0] == 50)
check("negative DEF cannot add damage", DR._shield_gate(50, -99)[0] == 50)

print("\n── 2. a real Nemesis copy can now be damaged ────────────────────")
rng = random.Random(7)
copies = [BC.roll_copy(AB.NEMESIS, rng) for _ in range(500)]
defs = sorted(c["stats"]["defense"] for c in copies)
print(f"       copy DEF: min {defs[0]}  median {defs[len(defs)//2]}  max {defs[-1]}")

ROSTER_MEAN_ATK = 97
blocked = 0
for c in copies:
    raw = math.ceil(ROSTER_MEAN_ATK * DR.LOSING_PENALTY_MULT)
    if DR._shield_gate(raw, c["stats"]["defense"])[0] <= 0:
        blocked += 1
check("an average-attack blade always does SOME damage to any copy",
      blocked == 0, f"{blocked}/500 still fully blocked")

worst = max(c["stats"]["defense"] for c in copies)
raw = math.ceil(ROSTER_MEAN_ATK * DR.LOSING_PENALTY_MULT)
check("even against the tankiest roll", DR._shield_gate(raw, worst)[0] >= 1,
      DR._shield_gate(raw, worst))

print("\n── 3. attrition out-paces the stamina engine again ──────────────")
print(f"       {'sta':>5} {'regen/rd':>9} {'balance KO':>11} {'stamina KO':>11}")
rows = []
for sta in (50, 112, 200, 306, 500):
    b = ko_round(sta, "balance")
    s = ko_round(sta, "stamina")
    rows.append((sta, b, s))
    print(f"       {sta:>5} {regen_per_round(sta):>9.2f} {str(b):>11} {str(s):>11}")

check("every stamina stat reaches a KO", all(b and s for _, b, s in rows), rows)
check("a balance blade's stall ends within 35 rounds",
      all(b <= 35 for _, b, _ in rows), [(s, b) for s, b, _ in rows])
check("a stamina blade's stall ends within 45 rounds",
      all(s <= 45 for _, _, s in rows), [(st, s) for st, _, s in rows])
check("stamina types genuinely outlast others — identity preserved",
      all(s > b for _, b, s in rows), rows)
check("a bigger stamina stat no longer means a MUCH longer stall",
      max(b for _, b, _ in rows) - min(b for _, b, _ in rows) <= 6,
      [b for _, b, _ in rows])

# The regression guard. This is the check that would have caught the original
# break: attrition must beat regen, whatever either side is re-tuned to.
worst_gap = 0
for sta in range(0, 501, 10):
    need = regen_per_round(sta)
    for r in range(TS.ATTRITION_START, 200):
        if TS.attrition_drain_for("stamina", r, sta) > need:
            worst_gap = max(worst_gap, r)
            break
    else:
        worst_gap = 999
check("attrition out-drains regen by round 40 across the WHOLE stat range",
      worst_gap <= 40, f"worst crossover at round {worst_gap}")

print("\n── 4. attrition's own shape is intact ───────────────────────────")
check("nothing bleeds before ATTRITION_START",
      TS.attrition_drain_for("balance", TS.ATTRITION_START, 300) == 0.0)
check("the bleed only ever grows",
      all(TS.attrition_drain_for("balance", r, 100)
          <= TS.attrition_drain_for("balance", r + 1, 100)
          for r in range(1, 80)))
check("the final band is open-ended, so it always resolves",
      TS.attrition_drain_for("balance", 500, 0)
      > TS.attrition_drain_for("balance", 100, 0))
check("stamina types still take half",
      abs(TS.attrition_drain_for("stamina", 40, 0)
          - TS.attrition_drain_for("balance", 40, 0) * TS.ATTRITION_STAMINA_MULT) < 0.02)
check("omitting sta_stat reproduces the old flat behaviour",
      TS.attrition_drain_for("balance", 40)
      == TS.attrition_drain_for("balance", 40, 0.0))
check("a negative stat cannot reduce the bleed below base",
      TS.attrition_drain_for("balance", 40, -500)
      == TS.attrition_drain_for("balance", 40, 0))

print("\n── 5. the reported scenario, end to end ─────────────────────────")
# Average blade attacking a median Nemesis copy that holds Defense and heals.
copy_def = defs[len(defs) // 2]
copy_sta = sorted(c["stats"]["stamina"] for c in copies)[len(copies) // 2]
hp, cap = 2000, 2000
bar = SM.max_stamina_for(copy_sta)
sp = bar * 0.5
raw = math.ceil(ROSTER_MEAN_ATK * DR.LOSING_PENALTY_MULT)
frozen_at, rounds = None, 0
for r in range(1, 200):
    rounds = r
    chip, _ = DR._shield_gate(raw, copy_def)
    hp = max(0, hp - chip)
    # The copy presses Stamina: free, heals, tops the bar back up.
    ratio = hp / cap
    mult = 1.0 + max(0.0, (0.40 - ratio) / 0.40) * 0.2
    hp = min(cap, hp + max(SM.STAMINA_HEAL_MIN,
                           math.ceil(copy_sta * SM.STAMINA_HEAL_RATIO * mult)))
    sp = min(bar, sp + regen_per_round(copy_sta))
    sp = max(0.0, sp - TS.attrition_drain_for("balance", r, copy_sta))
    if sp <= 0:
        frozen_at = None
        break
    frozen_at = hp
check("the fight terminates instead of freezing",
      frozen_at is None, f"still alive at {frozen_at} HP after {rounds} rounds")
check("...and does so in a playable number of rounds", rounds <= 35, rounds)
print(f"       stamina KO on round {rounds} (copy DEF {copy_def}, STA {copy_sta})")

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
