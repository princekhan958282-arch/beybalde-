#!/usr/bin/env python3
"""
tools/sim_avatar_skills.py — one skill per battle, and the 100-energy pool.

Four things have to hold or the system is worse than not shipping it:

  1. The split is LOSSLESS. Every non-zero bonus on a signature card is owned
     by exactly one skill — nothing silently deleted, nothing double-counted.
  2. The price ladder is MONOTONIC. Slot 3 costs three times slot 1, so slot 3
     must be worth more. Six of nine cards were authored the other way round.
  3. Ranked energy does NOT refill between rounds, and casual energy does. This
     is the whole rule, and it is the one an off-by-one would quietly invert.
  4. The 27 avatars with no skills are untouched — same bonuses, no energy, no
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

print("\n── 8. casual refills, every battle ──────────────────────────────")
p = {}
first = AS.begin_battle(p, argus, ranked=False)
check("slot 1 costs 25 of the 100", first["energy_after"] == 75, first)
check("...and locks the slot it granted", p[AS.K_LOCKED] == 1, p)
AS.end_battle(p, ranked=False)
check("casual refills at the end of the fight", AS.energy(p) == 100, p)
check("...and drops the lock", AS.K_LOCKED not in p, p)

p = {"avatar_skill": {"avatar_x002": 3}}
for i in range(5):
    r = AS.begin_battle(p, argus, ranked=False)
    AS.end_battle(p, ranked=False)
check("a 75⚡ skill is affordable every casual battle",
      r["afforded"] and r["slot"] == 3, r)

print("\n── 9. ranked does NOT refill until the match ends ───────────────")
# The rule as the user stated it: energy carries the whole match.
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
check("the match ending refills", AS.energy(p) == 100, AS.energy(p))
check("...and clears the in-match flag", not AS.in_ranked_match(p), p)

# A 75⚡ skill is a once-per-match play; a 25⚡ one lasts four rounds.
for slot, expected in ((1, 4), (2, 2), (3, 1)):
    p = {"avatar_skill": {"avatar_x002": slot}}
    got = 0
    for _ in range(9):                        # MAX_ROUNDS in the ranked driver
        if AS.begin_battle(p, argus, ranked=True)["afforded"]:
            got += 1
        AS.end_battle(p, ranked=True)
    check(f"slot {slot} fires {expected}× across a whole ranked match",
          got == expected, got)

print("\n── 10. a casual battle mid-match must not refill the budget ─────")
p = {"avatar_skill": {"avatar_x002": 2}}
AS.begin_battle(p, argus, ranked=True)
AS.end_battle(p, ranked=True)
mid = AS.energy(p)
AS.begin_battle(p, argus, ranked=False)       # a stray casual fight
AS.end_battle(p, ranked=False)
check("the ranked budget survives a casual battle in between",
      AS.energy(p) < mid or AS.energy(p) == mid - AS.skill_cost(2),
      (mid, AS.energy(p)))
check("...because the in-match flag suppresses the refill",
      AS.energy(p) != 100, AS.energy(p))
AS.end_match(p)

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
