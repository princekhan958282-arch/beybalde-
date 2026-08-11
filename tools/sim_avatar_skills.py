#!/usr/bin/env python3
"""
tools/sim_avatar_skills.py — one skill per battle, and the 100-energy pool.

Five things have to hold or the system is worse than not shipping it:

  1. The split is LOSSLESS. Every non-zero bonus on a signature card is owned
     by exactly one skill — nothing silently deleted, nothing double-counted.
  2. The price ladder is MONOTONIC. Slot 3 costs three times slot 1, so slot 3
     must be worth more. Six of nine cards were authored the other way round.
  3. CASUAL never touches the pool and RANKED never refills — not between
     rounds, and not at match open. Casual used to refill, which meant one free
     fight restocked a ranked resource and made both recovery and the paid
     refill pointless. That inversion is the easiest one to reintroduce.
  4. RECOVERY is +25 every 5 minutes, settled on read, frozen mid-match, and
     never loses a partial tick — a player who checks their energy constantly
     must regenerate at exactly the same rate as one who never looks.
  5. The 27 avatars with no skills are untouched — same bonuses, no energy, no
     pick, no behaviour change at all.

Plus the audit that started this: every skill's described effect must have a
mechanical binding that can actually fire.

Run:  python3 tools/sim_avatar_skills.py
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


from cogs.avatar import avatar_skills as AS                    # noqa: E402
from cogs.avatar.avatar_engine import avatar_engine, AvatarBonuses  # noqa: E402

avatar_engine.load()
ALL = avatar_engine.get_all_avatars()
CARDS = {a["name"]: a for a in ALL}
SIGNATURE = [a for a in ALL if a.get("skills")]
PLAIN = [a for a in ALL if not a.get("skills")]

print("\n── 1. the rules as stated ───────────────────────────────────────")
check("the pool is 100", AS.MAX_ENERGY == 100, AS.MAX_ENERGY)
check("slot 1 costs 25", AS.skill_cost(1) == 25, AS.skill_cost(1))
check("slot 2 costs 50", AS.skill_cost(2) == 50, AS.skill_cost(2))
check("slot 3 costs 75", AS.skill_cost(3) == 75, AS.skill_cost(3))
check("slot 0 and slot 4 cost nothing and grant nothing",
      AS.skill_cost(0) == 0 and AS.skill_cost(4) == 0)
# 100 energy buys four cheap skills, two mid ones, or one expensive one.
check("a full pool affords slot 1 four times", AS.uses_affordable(1) == 4)
check("...slot 2 twice", AS.uses_affordable(2) == 2)
check("...slot 3 once", AS.uses_affordable(3) == 1)

print("\n── 2. nine signature cards, three skills each ───────────────────")
check("exactly nine cards carry skills", len(SIGNATURE) == 9,
      [a["name"] for a in SIGNATURE])
check("the other 27 carry none", len(PLAIN) == 27, len(PLAIN))
for av in SIGNATURE:
    check(f"{av['name']} has 3 skills", len(av["skills"]) == 3,
          len(av["skills"]))
    check(f"{av['name']}: every skill owns a bonus slice",
          all(s.get("bonuses") for s in av["skills"]),
          [s["name"] for s in av["skills"] if not s.get("bonuses")])

print("\n── 3. the split is lossless ─────────────────────────────────────")
for av in SIGNATURE:
    live = {k for k, v in av["bonuses"].items() if v}
    claimed: dict[str, int] = {}
    for sk in av["skills"]:
        for k in sk["bonuses"]:
            claimed[k] = claimed.get(k, 0) + 1
    check(f"{av['name']}: nothing lost", not (live - set(claimed)),
          sorted(live - set(claimed)))
    check(f"{av['name']}: nothing invented", not (set(claimed) - live),
          sorted(set(claimed) - live))
    check(f"{av['name']}: nothing counted twice",
          not [k for k, n in claimed.items() if n > 1],
          [k for k, n in claimed.items() if n > 1])
    # Values must match the card, not merely the key names.
    for sk in av["skills"]:
        bad = {k: (v, av["bonuses"].get(k)) for k, v in sk["bonuses"].items()
               if av["bonuses"].get(k) != v}
        check(f"{av['name']} / {sk['name']}: values match the card",
              not bad, bad)

print("\n── 4. the price ladder is monotonic ─────────────────────────────")
# Scored by the same weights the migration used, so this test fails if someone
# edits a card into an inverted ladder later.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from split_avatar_skills import score                          # noqa: E402

for av in SIGNATURE:
    scores = [score(s["bonuses"]) for s in av["skills"]]
    check(f"{av['name']}: weakest → strongest",
          scores == sorted(scores),
          list(zip([s["name"] for s in av["skills"]], scores)))
    check(f"{av['name']}: the 75⚡ pick beats the 25⚡ one",
          scores[2] > scores[0], scores)

print("\n── 5. bonuses_for narrows to exactly one skill ──────────────────")
argus = CARDS["Argus"]
b1 = AS.bonuses_for(argus, 1)
b2 = AS.bonuses_for(argus, 2)
b3 = AS.bonuses_for(argus, 3)
check("Argus slot 1 = Titan's Might only",
      b1["attack_percent"] == 0.30 and not b1["crit_percent"]
      and not b1["immortal_rounds"], b1)
check("Argus slot 2 = Piercing Gaze only",
      b2["crit_percent"] == 0.40 and b2["gauge_on_crit"] == 50
      and not b2["attack_percent"], b2)
check("Argus slot 3 = Deathless only",
      b3["immortal_rounds"] == 2 and not b3["attack_percent"]
      and not b3["crit_percent"], b3)
check("the shape is preserved — same keys as the card",
      set(b1) == set(argus["bonuses"]),
      set(argus["bonuses"]) ^ set(b1))
check("booleans stay booleans, not zeros",
      AS.bonuses_for(CARDS["Eudora"], 1)["multi_hit_extra_hits"] is False,
      AS.bonuses_for(CARDS["Eudora"], 1)["multi_hit_extra_hits"])
check("slot 3 of Eudora turns the boolean back on",
      AS.bonuses_for(CARDS["Eudora"], 3)["multi_hit_extra_hits"] is True)

print("\n── 6. cards WITHOUT skills are untouched ────────────────────────")
for av in PLAIN:
    same = all(AS.bonuses_for(av, slot) == av["bonuses"] for slot in (0, 1, 2, 3))
    check(f"{av['name']}: full bonuses whatever the slot", same)
check("...and they report no active slot",
      all(AS.active_slot({}, av) == 0 for av in PLAIN))
check("...so nothing charges them energy",
      all(AS.begin_battle({}, av, ranked=r)["cost"] == 0
          for av in PLAIN[:5] for r in (False, True)))

print("\n── 7. choosing, and the default ─────────────────────────────────")
check("an untouched profile defaults to slot 1 — the cheapest",
      AS.chosen_slot({}, "avatar_x002") == 1)
prof = {"avatar_skill": {"avatar_x002": 3}}
check("a stored pick is honoured", AS.chosen_slot(prof, "avatar_x002") == 3)
check("a junk pick falls back to slot 1",
      AS.chosen_slot({"avatar_skill": {"avatar_x002": "banana"}},
                     "avatar_x002") == 1)
check("an out-of-range pick falls back to slot 1",
      AS.chosen_slot({"avatar_skill": {"avatar_x002": 9}}, "avatar_x002") == 1)
check("a pick past the end of a short card clamps to its last skill",
      AS.active_slot({"avatar_skill": {"x": 3}},
                     {"id": "x", "bonuses": {}, "skills": [{"name": "a"}]}) == 1)

print("\n── 8. casual never touches the pool ────────────────────────────")
# Casual used to REFILL the pool, which meant a single casual battle restocked
# a ranked resource for free — so nobody would ever wait for recovery or pay
# for a refill. Casual is still free; it just does not write the pool now.
p = {"avatar_skill": {"avatar_x002": 3}, AS.K_ENERGY: 10}
first = AS.begin_battle(p, argus, ranked=False)
check("casual grants the chosen skill", first["slot"] == 3, first)
check("...at a drained pool", AS.energy(p) == 10, AS.energy(p))
check("...charging nothing", first["cost"] == 0, first)
check("...and reporting no change", first["energy_after"] == 10, first)
AS.end_battle(p, ranked=False)
check("ending a casual battle does not refill", AS.energy(p) == 10, AS.energy(p))
check("...and drops the lock", AS.K_LOCKED not in p, p)

p = {"avatar_skill": {"avatar_x002": 3}, AS.K_ENERGY: 0}
for _ in range(20):
    r = AS.begin_battle(p, argus, ranked=False)
    AS.end_battle(p, ranked=False)
check("20 casual battles on an EMPTY pool all grant the skill",
      r["slot"] == 3 and r["afforded"], r)
check("...and the pool is still empty", AS.energy(p) == 0, AS.energy(p))
check("...so casual can never be used to farm ranked energy", AS.energy(p) == 0)

print("\n── 9. ranked spends, and brings what it has ─────────────────────")
# The rule as stated: energy carries the whole match, and the match does not
# top you up on the way in.
p = {"avatar_skill": {"avatar_x002": 2}}      # 50 energy a round
r1 = AS.begin_battle(p, argus, ranked=True)
AS.end_battle(p, ranked=True)
check("round 1 spends 50, leaving 50", AS.energy(p) == 50, AS.energy(p))
check("...and the round end did NOT refill", AS.energy(p) != 100)
r2 = AS.begin_battle(p, argus, ranked=True)
AS.end_battle(p, ranked=True)
check("round 2 spends the last 50", AS.energy(p) == 0, AS.energy(p))
r3 = AS.begin_battle(p, argus, ranked=True)
check("round 3 cannot afford the skill", not r3["afforded"], r3)
check("...so no skill is active", r3["slot"] == 0 and AS.active_slot(p, argus) == 0)
check("...and nothing was charged for it", AS.energy(p) == 0, AS.energy(p))
AS.end_battle(p, ranked=True)
AS.end_match(p)
check("the match ending does NOT refill", AS.energy(p) == 0, AS.energy(p))
check("...and clears the in-match flag", not AS.in_ranked_match(p), p)

# Opening a match part-drained is the whole point: you fight on what recovery
# has given back. This is the assertion that would have caught the old
# end_match_for()-at-match-open refill.
p = {"avatar_skill": {"avatar_x002": 2}, AS.K_ENERGY: 50}
a = AS.begin_battle(p, argus, ranked=True)
check("a match opened at 50 is NOT topped up to 100",
      a["energy_before"] == 50, a)
check("...round 1 still affords the 50 skill", a["afforded"] and a["slot"] == 2)
AS.end_battle(p, ranked=True)
b = AS.begin_battle(p, argus, ranked=True)
check("...and round 2 cannot", not b["afforded"], b)
AS.end_match(p)

# A 75 skill is a once-per-match play; a 25 one lasts four rounds.
for slot, expected in ((1, 4), (2, 2), (3, 1)):
    p = {"avatar_skill": {"avatar_x002": slot}}
    got = 0
    for _ in range(9):                        # MAX_ROUNDS in the ranked driver
        if AS.begin_battle(p, argus, ranked=True)["afforded"]:
            got += 1
        AS.end_battle(p, ranked=True)
    check(f"slot {slot} fires {expected}x across a whole ranked match",
          got == expected, got)
    AS.end_match(p)

print("\n── 10. a casual battle mid-match must not disturb the budget ────")
p = {"avatar_skill": {"avatar_x002": 2}}
AS.begin_battle(p, argus, ranked=True)
AS.end_battle(p, ranked=True)
mid = AS.energy(p)
AS.begin_battle(p, argus, ranked=False)       # a stray casual fight
AS.end_battle(p, ranked=False)
check("the ranked budget is byte-identical after a casual battle",
      AS.energy(p) == mid, (mid, AS.energy(p)))
check("...and is not 100", AS.energy(p) != 100, AS.energy(p))
AS.end_match(p)

print("\n── 10b. recovery: +25 every 5 minutes ───────────────────────────")
MIN = 60.0
T0 = 1_000_000.0
check("a tick is 5 minutes", AS.ENERGY_REGEN_SECONDS == 300,
      AS.ENERGY_REGEN_SECONDS)
check("a tick is worth 25", AS.ENERGY_REGEN_AMOUNT == 25,
      AS.ENERGY_REGEN_AMOUNT)
check("a full pool therefore takes 20 minutes",
      (AS.MAX_ENERGY // AS.ENERGY_REGEN_AMOUNT) * AS.ENERGY_REGEN_SECONDS
      == 20 * 60)

for mins, expect in ((0, 0), (4, 0), (5, 25), (12, 50), (20, 100), (60, 100)):
    p = {AS.K_ENERGY: 0, AS.K_ENERGY_TS: T0}
    gained = AS.accrue(p, T0 + mins * MIN)
    check(f"{mins:>2} min drained -> {expect} energy",
          AS.energy(p) == expect, (gained, AS.energy(p)))

# The remainder must survive. Settling advances the clock by WHOLE ticks, so a
# player who checks their energy every minute still regenerates — advancing the
# stamp to `now` would reset the partial tick on every read and they would
# never gain anything.
p = {AS.K_ENERGY: 0, AS.K_ENERGY_TS: T0}
AS.accrue(p, T0 + 7 * MIN)
check("7 min gives one tick", AS.energy(p) == 25, AS.energy(p))
AS.accrue(p, T0 + 10 * MIN)
check("...and 3 min later the second lands", AS.energy(p) == 50, AS.energy(p))

p = {AS.K_ENERGY: 0, AS.K_ENERGY_TS: T0}
for i in range(1, 6 * 60):                    # a read every 10 seconds
    AS.accrue(p, T0 + i * 10.0)
check("reading every 10s for an hour still fills the pool",
      AS.energy(p) == 100, AS.energy(p))

# Nothing is banked above the cap.
p = {AS.K_ENERGY: 100, AS.K_ENERGY_TS: T0}
AS.accrue(p, T0 + 600 * MIN)
check("time at full is not banked", AS.energy(p) == 100)
p[AS.K_ENERGY] = 0
AS.accrue(p, T0 + 600 * MIN + 60)
check("...so draining does not instantly refund it", AS.energy(p) == 0,
      AS.energy(p))

# A profile that has never battled must not be handed decades of accrual.
p = {}
AS.accrue(p, T0)
check("a fresh profile settles from now, not from the epoch",
      AS.energy(p) == 100, AS.energy(p))
p = {AS.K_ENERGY: 0}
AS.accrue(p, T0)
check("a drained profile with no stamp gains nothing yet",
      AS.energy(p) == 0, AS.energy(p))

# A clock that jumps backwards must not credit anything.
p = {AS.K_ENERGY: 0, AS.K_ENERGY_TS: T0 + 3600}
AS.accrue(p, T0)
check("a stamp from the future credits nothing", AS.energy(p) == 0,
      AS.energy(p))

print("\n── 10c. recovery is FROZEN during a ranked match ────────────────")
p = {"avatar_skill": {"avatar_x002": 2}, AS.K_ENERGY: 0, AS.K_ENERGY_TS: T0}
AS.begin_battle(p, argus, ranked=True, now=T0)
check("the match flag is set", AS.in_ranked_match(p))
AS.accrue(p, T0 + 600 * MIN)
check("ten hours mid-match gains nothing", AS.energy(p) == 0, AS.energy(p))
AS.end_match(p, now=T0 + 600 * MIN)
check("...and the match ending does not back-credit it",
      AS.energy(p) == 0, AS.energy(p))
AS.accrue(p, T0 + 605 * MIN)
check("recovery resumes after the match", AS.energy(p) == 25, AS.energy(p))

check("next_tick_at is None while frozen",
      AS.next_tick_at({AS.K_ENERGY: 0, AS.K_MATCH: "1"}) is None)
check("full_at is None while frozen",
      AS.full_at({AS.K_ENERGY: 0, AS.K_MATCH: "1"}) is None)
check("both are None at a full pool",
      AS.next_tick_at({AS.K_ENERGY: 100}) is None
      and AS.full_at({AS.K_ENERGY: 100}) is None)
p = {AS.K_ENERGY: 50, AS.K_ENERGY_TS: T0}
check("next_tick_at is one tick after the stamp",
      AS.next_tick_at(p, T0) == T0 + 300, AS.next_tick_at(p, T0))
check("full_at needs two more ticks from 50",
      AS.full_at(p, T0) == T0 + 600, AS.full_at(p, T0))
p = {AS.K_ENERGY: 60, AS.K_ENERGY_TS: T0}
check("...and rounds UP for a partial tick (60 -> 100 is 2 ticks)",
      AS.full_at(p, T0) == T0 + 600, AS.full_at(p, T0))

print("\n── 10d. the 40,000 coin refill ──────────────────────────────────")
check("the price is 40,000", AS.ENERGY_REFILL_PRICE == 40_000,
      AS.ENERGY_REFILL_PRICE)

p = {AS.K_ENERGY: 0, AS.K_ENERGY_TS: T0, "coins": 100_000}
out = AS.buy_refill(p, now=T0)
check("a refill fills the pool", AS.energy(p) == 100, AS.energy(p))
check("...and charges exactly 40,000", p["coins"] == 60_000, p["coins"])
check("...and reports the move", out["from"] == 0 and out["to"] == 100, out)

def refused(profile, **kw):
    """True when buy_refill raised AND took no coins.

    Coins are the property that matters: buy_refill settles recovery before it
    decides, so energy CAN legitimately move on a refused call (that is how
    "recovery already filled you, there is nothing to sell" is reached). What
    must never happen is money leaving.
    """
    before = int(profile.get("coins", 0))
    try:
        AS.buy_refill(profile, **kw)
    except AS.RefillError:
        return int(profile.get("coins", 0)) == before
    return False

check("refused when already full",
      refused({AS.K_ENERGY: 100, "coins": 100_000}, now=T0))
check("refused mid-match",
      refused({AS.K_ENERGY: 0, AS.K_ENERGY_TS: T0, AS.K_MATCH: "1",
               "coins": 100_000}, now=T0))
# One coin short must leave BOTH the coins and the energy alone — the caller
# runs this inside mutate_user, where the raise abandons the whole write.
_short = {AS.K_ENERGY: 0, AS.K_ENERGY_TS: T0, "coins": 39_999}
check("refused one coin short, with nothing taken",
      refused(_short, now=T0))
check("...and no energy handed out either",
      AS.energy(_short) == 0 and _short["coins"] == 39_999, _short)
check("...and exactly on the price it succeeds",
      not refused({AS.K_ENERGY: 0, AS.K_ENERGY_TS: T0, "coins": 40_000},
                  now=T0))

# Buying must not leave a stale clock that instantly re-credits. A pool with
# six minutes on it has one tick pending; the refill has to consume that clock,
# not leave it sitting there to pay out again a second later.
p = {AS.K_ENERGY: 0, AS.K_ENERGY_TS: T0 - 6 * MIN, "coins": 100_000}
AS.buy_refill(p, now=T0)
check("buying from a part-accrued pool still fills it", AS.energy(p) == 100)
p[AS.K_ENERGY] = 0
AS.accrue(p, T0 + 60)
check("...and re-stamps the clock, so nothing re-credits",
      AS.energy(p) == 0, AS.energy(p))

# Enough accrued time to fill on its own means there is nothing to sell.
check("refused when recovery has already filled the pool",
      refused({AS.K_ENERGY: 0, AS.K_ENERGY_TS: T0 - 600 * MIN,
               "coins": 100_000}, now=T0))

print("\n── 11. the audit: every skill can actually fire ─────────────────")
# Skills describe effects; effects need a binding the engine reads. A skill
# whose only bonus key is never consumed is a card that lies.
combat_src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "cogs", "battle", "avatar_combat.py"),
    encoding="utf-8").read()
FIELDS = set(AvatarBonuses.__dataclass_fields__)
for av in SIGNATURE:
    for sk in av["skills"]:
        unknown = [k for k in sk["bonuses"] if k not in FIELDS]
        check(f"{av['name']} / {sk['name']}: every key is a real bonus field",
              not unknown, unknown)

# The specific bug this release fixes: a counter that can never be rolled.
# absorb_incoming used to roll it only inside the dodge branch, so a card with
# counter_chance and no dodge_chance advertised an effect that never happened.
broken = [a["name"] for a in ALL
          if a["bonuses"].get("counter_chance", 0) > 0
          and not a["bonuses"].get("dodge_chance", 0)]
check("cards that counter without dodging still exist (Freya, Miku, Sukuna, Gen)",
      sorted(broken) == ["Freya", "Gen", "Miku", "Sukuna"], sorted(broken))
check("...and the block path now rolls a counter for them",
      combat_src.count("roll_counter()") == 2, combat_src.count("roll_counter()"))
check("...inside the resistance branch, not only the dodge one",
      "resistance absorbs" in combat_src
      and combat_src.index("roll_counter()")
          < combat_src.index("resistance absorbs")
          < combat_src.rindex("roll_counter()"))

print("\n── 12. dodge is displayed at the value that actually rolls ──────")
from cogs.avatar.avatar_utils import format_bonuses_summary     # noqa: E402

over = [a for a in ALL if a["bonuses"].get("dodge_chance", 0) > AvatarBonuses.DODGE_CAP]
check("some cards are authored above the cap", len(over) >= 10, len(over))
lying = []
for a in over:
    # Only the Dodge line, not the whole summary: Abyssal Warden counters at
    # 18% AND dodges at 18%, so a substring search over every line reports a
    # lie that isn't there.
    dodge_line = next(
        (ln for ln in format_bonuses_summary(a["bonuses"]).splitlines()
         if "Dodge Chance" in ln), "")
    if f"+{int(a['bonuses']['dodge_chance'] * 100)}%" in dodge_line:
        lying.append((a["name"], dodge_line))
check("none of them advertise the uncapped number", not lying, lying[:5])
check("...they advertise the 5% the engine rolls",
      all("**Dodge Chance**: +5%" in format_bonuses_summary(a["bonuses"])
          for a in over), [a["name"] for a in over[:3]])

print("\n── 13. the engine reads the pick end to end ─────────────────────")
# get_battle_bonuses is the single choke point every consumer already uses, so
# patching the profile lookup is enough to prove PvP, boss, Story and both card
# renderers all see the same narrowed bonuses.
import utils.database as DB                                     # noqa: E402

_real_get_user = DB.get_user
_fake: dict = {}
DB.get_user = lambda uid: _fake                                 # noqa: E731
_real_equipped = avatar_engine.get_equipped_avatar_id
avatar_engine.get_equipped_avatar_id = lambda uid: "avatar_x002"

try:
    for slot, field, value in ((1, "attack_percent", 0.30),
                               (2, "crit_percent", 0.40),
                               (3, "immortal_rounds", 2)):
        _fake.clear()
        _fake.update({"avatar_skill": {"avatar_x002": slot}})
        got = avatar_engine.get_battle_bonuses(1)
        check(f"slot {slot}: {field} arrives at the engine",
              getattr(got, field) == value, getattr(got, field))
        others = [f for f in ("attack_percent", "crit_percent", "immortal_rounds")
                  if f != field and getattr(got, f)]
        check(f"slot {slot}: the other two skills are OFF", not others, others)

    # Out of energy mid-ranked-match: no skill at all, but the card's level
    # bonus is bought with coins and must survive.
    _fake.clear()
    _fake.update({"avatar_skill": {"avatar_x002": 3},
                  AS.K_LOCKED: 0, AS.K_ENERGY: 10})
    drained = avatar_engine.get_battle_bonuses(1)
    check("a drained player gets no skill",
          not drained.attack_percent and not drained.crit_percent
          and not drained.immortal_rounds, drained)
    check("...and the fight can still start (a real bonuses object)",
          isinstance(drained, AvatarBonuses))
finally:
    DB.get_user = _real_get_user
    avatar_engine.get_equipped_avatar_id = _real_equipped

print("\n── 13b. the pre-battle picker asks the right people ─────────────")
# The View itself needs a gateway, but the decision of WHO gets asked is pure
# and is the part that must not regress: a prompt in a fight where nobody has
# anything to pick would appear in the overwhelming majority of battles.
from cogs.battle import skill_prompt as SP                      # noqa: E402


class _FakeMember:
    def __init__(self, uid, name="P"):
        self.id = uid
        self.display_name = name


_equipped_by_id: dict = {}
_real_equipped2 = avatar_engine.get_equipped_avatar_id
avatar_engine.get_equipped_avatar_id = lambda uid: _equipped_by_id.get(int(uid))

try:
    a, b = _FakeMember(1, "A"), _FakeMember(2, "B")

    _equipped_by_id.clear()
    check("nobody with an avatar -> nobody is asked",
          SP.participants((a, b)) == [])

    # A plain card (no skills) must not summon the prompt either — this is the
    # 27-of-36 case, i.e. almost every battle.
    plain = next(x for x in ALL if not x.get("skills"))
    _equipped_by_id.update({1: plain["id"], 2: plain["id"]})
    check("two skill-less avatars -> still nobody",
          SP.participants((a, b)) == [], SP.participants((a, b)))

    _equipped_by_id.update({1: "avatar_x002", 2: plain["id"]})
    got = SP.participants((a, b))
    check("one signature avatar -> exactly one participant", len(got) == 1, got)
    check("...and it is the right player and card",
          got[0][0].id == 1 and got[0][1]["id"] == "avatar_x002")

    _equipped_by_id.update({1: "avatar_x002", 2: "avatar_mlbb001"})
    check("both on signature cards -> both asked",
          len(SP.participants((a, b))) == 2)

    # An id that no longer resolves (a card removed from the data) must drop
    # that player rather than raise — a battle must still start.
    _equipped_by_id.update({1: "avatar_does_not_exist", 2: "avatar_x002"})
    got = SP.participants((a, b))
    check("a dangling avatar id drops that player, does not raise",
          len(got) == 1 and got[0][0].id == 2, got)
finally:
    avatar_engine.get_equipped_avatar_id = _real_equipped2

check("the picker has a timeout and it is not zero",
      SP.SKILL_PROMPT_SECONDS > 0, SP.SKILL_PROMPT_SECONDS)

# The components must actually CONSTRUCT. This suite used to test only the pure
# helpers, and shipped a picker that raised AttributeError the instant anyone
# pressed the button: `self.parent = ...` collides with discord.ui.Item.parent,
# a read-only property. It raised while building the argument to
# send_message, so the interaction was never acknowledged and Discord said
# "The application did not respond" — with the traceback in a logger nobody
# was reading. Building every view for every skilled card is what catches that
# whole class of bug, and it needs no gateway.
built, broke = 0, []
for av in SIGNATURE:
    for ranked in (False, True):
        for pool in (0, 100):
            try:
                prompt = SP.SkillPromptView.__new__(SP.SkillPromptView)
                prompt.picked = {}
                SP._SkillSelectView(prompt, _FakeMember(1, "A"), av, pool, ranked)
                built += 1
            except Exception as exc:                 # noqa: BLE001
                broke.append((av["name"], ranked, pool, repr(exc)[:80]))
check(f"the skill dropdown builds for every card ({built} combinations)",
      not broke, broke[:3])

# And the public prompt itself, with the profile lookups stubbed.
_real_get_user3 = DB.get_user
_real_eq3 = avatar_engine.get_equipped_avatar_id
DB.get_user = lambda uid: {}
try:
    for av in SIGNATURE:
        avatar_engine.get_equipped_avatar_id = lambda uid, _a=av: _a["id"]
        try:
            v = SP.SkillPromptView([(_FakeMember(1, "A"), av)], False)
            v._status()
            v._card_embed(_FakeMember(1, "A"), av)
        except Exception as exc:                     # noqa: BLE001
            broke.append((av["name"], "prompt", repr(exc)[:80]))
    check("the public prompt and its embeds build for every card",
          not broke, broke[:3])
finally:
    DB.get_user = _real_get_user3
    avatar_engine.get_equipped_avatar_id = _real_eq3

# Discord rejects an embed field with an empty value, and a select option over
# 100 chars, with a 400 at SEND time — too late to catch by hand.
long = [(av["name"], sk["name"]) for av in SIGNATURE
        for sk in av["skills"] if not str(sk.get("description", "")).strip()]
check("no skill has an empty description (an empty embed field 400s)",
      not long, long)
# Timing out must leave the standing pick alone. participants() is read-only
# and on_timeout only disables buttons — nothing in the module writes a choice
# except the Select callback, which is the assertion that keeps it that way.
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "cogs", "battle", "skill_prompt.py"), encoding="utf-8").read()
check("only the Select callback ever stores a pick",
      src.count("AS.set_choice") == 1, src.count("AS.set_choice"))
check("...and on_timeout does not touch it",
      "set_choice" not in src[src.index("async def on_timeout"):])

print("\n── 14. nothing else moved ───────────────────────────────────────")
check("the roster is still 36 cards", len(ALL) == 36, len(ALL))
check("the MLBB banner is still six",
      len([a for a in ALL if a["rarity"] == "MLBB"]) == 6)
check("every card still builds an AvatarBonuses",
      not [a["name"] for a in ALL
           if not isinstance(AvatarBonuses(
               **{k: v for k, v in a["bonuses"].items()
                  if k in AvatarBonuses.__dataclass_fields__}), AvatarBonuses)])
check("the split migration is idempotent",
      os.system(f"{sys.executable} tools/split_avatar_skills.py --check "
                f"> /dev/null 2>&1") == 0)
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "cogs", "avatar", "avatar_data.json"), encoding="utf-8") as fh:
    check("avatar_data.json is still valid JSON", bool(json.load(fh)))

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
