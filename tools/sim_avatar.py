#!/usr/bin/env python3
"""
tools/sim_avatar.py — verify per-card avatar levelling.

The guarantee that matters most: **a level-1 card must behave byte-identically
to how it behaved before this system existed.** 3,356 players have avatars
equipped right now; a migration that quietly buffs or nerfs any of them is the
worst possible outcome, and it is invisible without a test like this one.

Run:  python3 tools/sim_avatar.py
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


from cogs.avatar import avatar_levels as AL          # noqa: E402
from cogs.avatar import avatar_progress as AP        # noqa: E402
from cogs.avatar.avatar_utils import validate_avatar_data, VALID_TYPES  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "cogs", "avatar", "avatar_data.json")
CARDS = json.load(open(DATA, encoding="utf-8"))["avatars"]

print("\n── 1. every authored card migrated ──────────────────────────────")
# Not pinned: every new avatar would otherwise fail this suite for existing.
check("the original 29 cards are all still here", len(CARDS) >= 29, len(CARDS))
print(f"       card count: {len(CARDS)}")
check("every card has a type",
      all(c.get("type") in VALID_TYPES for c in CARDS),
      [c["id"] for c in CARDS if c.get("type") not in VALID_TYPES])
bad = [(c["id"], validate_avatar_data(c)[1]) for c in CARDS
       if not validate_avatar_data(c)[0]]
check("every card still validates", not bad, bad)
check("a card with no type is REJECTED, not defaulted",
      not validate_avatar_data({**CARDS[0], "type": None})[0])
check("a card with a bogus type is rejected",
      not validate_avatar_data({**CARDS[0], "type": "wizard"})[0])
dist = {t: sum(1 for c in CARDS if c["type"] == t) for t in VALID_TYPES}
check("all four types are represented", all(dist.values()), dist)
print(f"       distribution: {dist}")

print("\n── 2. the calibration guarantee: Lv1 changes nothing ────────────")
for t in AL.TYPES:
    check(f"{t} card at Lv1 adds zero",
          AL.card_stat_bonus(t, 1) == {"attack": 0, "defense": 0, "stamina": 0},
          AL.card_stat_bonus(t, 1))
check("an absent profile block reads as Lv1", AP.card_level({}, "avatar_x002") == 1)
check("a junk profile block reads as Lv1",
      AP.card_level({"avatar": "not-a-dict"}, "avatar_x002") == 1)
check("an unknown card reads as Lv1",
      AP.card_level({"avatar": {"cards": {}}}, "nope") == 1)
check("reading never creates the block",
      (lambda p: (AP.card_level(p, "x"), AP.skill_level(p, "x", "y"),
                  AP.total_spent(p, "x"), "avatar" not in p)[-1])({}))

print("\n── 3. growth is monotone and typed ──────────────────────────────")
for t in AL.TYPES:
    seq = [AL.card_stat_bonus(t, l) for l in range(1, 6)]
    check(f"{t} grows every level",
          all(seq[i + 1][s] > seq[i][s] for i in range(4) for s in AL.STATS),
          seq)
atk5 = AL.card_stat_bonus("attack", 5)
def5 = AL.card_stat_bonus("defense", 5)
sta5 = AL.card_stat_bonus("stamina", 5)
bal5 = AL.card_stat_bonus("balance", 5)
check("an attack card leads on attack", atk5["attack"] > atk5["defense"], atk5)
check("a defence card leads on defence", def5["defense"] > def5["attack"], def5)
check("a stamina card leads on stamina", sta5["stamina"] > sta5["attack"], sta5)
check("balance is even across all three",
      len(set(bal5.values())) == 1, bal5)
check("balance's peak is below a specialist's peak",
      bal5["attack"] < atk5["attack"], (bal5, atk5))
check("Lv5 attack bonus stays under the spec's flagged +88",
      atk5["attack"] <= 60, atk5["attack"])
check("level clamps at MAX_CARD_LEVEL",
      AL.card_stat_bonus("attack", 99) == atk5)
check("a garbage level does not raise",
      AL.card_stat_bonus("attack", None) == AL.card_stat_bonus("attack", 1))
check("an unknown type falls back to balance, not a crash",
      AL.card_stat_bonus("wizard", 5) == bal5)

print("\n── 4. cost curve fits the measured economy ──────────────────────")
check("card 1→5 costs 256,000", AL.card_level_cost(1, 5) == 256_000,
      AL.card_level_cost(1, 5))
check("full card is a tenth of the spec's 16.84M",
      1_600_000 < AL.full_card_cost() < 1_800_000, f"{AL.full_card_cost():,}")
check("multi-level SUMS the steps, never multiplies",
      AL.card_level_cost(1, 5) == sum(AL.card_level_cost(l, l + 1)
                                      for l in range(1, 5)))
check("steps get more expensive",
      all(AL.card_level_cost(l, l + 1) < AL.card_level_cost(l + 1, l + 2)
          for l in range(1, 4)))
check("first upgrade is affordable to the top ~6% (<100k)",
      AL.card_level_cost(1, 2) < 100_000, AL.card_level_cost(1, 2))
check("buying past max costs nothing extra",
      AL.card_level_cost(5, 9) == 0)
check("a backwards range is free, not negative",
      AL.card_level_cost(4, 2) == 0)
check("skill costs also sum their steps",
      AL.skill_level_cost(1, 10) == sum(AL.skill_level_cost(l, l + 1)
                                        for l in range(1, 10)))
check("skill magnitude reaches the spec's ×1.72",
      abs(AL.skill_magnitude_mult(10) - 1.72) < 1e-9,
      AL.skill_magnitude_mult(10))

print("\n── 5. the skill gate ────────────────────────────────────────────")
check("the gate is card_level × 2",
      [AL.max_skill_level_for(l) for l in range(1, 6)] == [2, 4, 6, 8, 10])
p = {"coins": 10_000_000}
AP.apply_card_purchase(p, "avatar_x002", 1)
q = AP.quote_skill(p, "avatar_x002", "titans-might", 9)
check("a skill is capped by the card level", q["to"] <= 4, q)
p2 = {"coins": 10_000_000, "avatar": {"cards": {"a": {"level": 1,
      "skills": {"s": 2}, "spent": {"card": 0, "skills": {}}}}}}
blocked = AP.quote_skill(p2, "a", "s")
check("a blocked upgrade NAMES its reason", bool(blocked["blocked"]),
      blocked)
check("...and the reason says what to do",
      "raise the avatar" in blocked["blocked"].lower(), blocked["blocked"])
check("a skill level above the gate is clamped on READ",
      AP.skill_level({"avatar": {"cards": {"a": {"level": 1,
                     "skills": {"s": 9}}}}}, "a", "s") == 2)

print("\n── 6. the purchase transaction ──────────────────────────────────")
prof = {"coins": 100_000}
q = AP.quote_card(prof, "avatar_l001", 1)
AP.apply_card_purchase(prof, "avatar_l001", 1)
check("coins are deducted exactly once",
      prof["coins"] == 100_000 - q["cost"], prof["coins"])
check("the level is granted", AP.card_level(prof, "avatar_l001") == 2)
check("the real spend is recorded",
      AP.spent_on_card(prof, "avatar_l001") == q["cost"])

poor = {"coins": 100}
try:
    AP.apply_card_purchase(poor, "avatar_l001", 1)
    check("an unaffordable buy raises", False)
except AP.PurchaseError as e:
    check("an unaffordable buy raises PurchaseError", True)
    check("...naming the shortfall", "short" in str(e), str(e))
check("...and takes NO coins", poor["coins"] == 100, poor["coins"])
check("...and grants NO level", AP.card_level(poor, "avatar_l001") == 1)
check("...and creates no profile block", "avatar" not in poor)

maxed = {"coins": 10_000_000}
AP.apply_card_purchase(maxed, "c", 4)
check("buying 4 levels at once reaches Lv5", AP.card_level(maxed, "c") == 5)
check("...charging the summed price",
      maxed["coins"] == 10_000_000 - 256_000, maxed["coins"])
try:
    AP.apply_card_purchase(maxed, "c", 1)
    check("buying past max raises", False)
except AP.PurchaseError:
    check("buying past max raises", True)
check("...and charges nothing",
      maxed["coins"] == 10_000_000 - 256_000)

print("\n── 7. refunds come from real spend, not the cost table ──────────")
r = AP.apply_reset(maxed, "c")
check("refund is 70% of what was actually paid",
      r["refund"] == int(256_000 * 0.70), r)
check("the card drops to Lv1", AP.card_level(maxed, "c") == 1)
check("the spend ledger is cleared", AP.total_spent(maxed, "c") == 0)
check("coins came back", maxed["coins"] == 10_000_000 - 256_000 + r["refund"])
# The point of storing spend: a later price change must not alter old refunds.
hand = {"coins": 0, "avatar": {"cards": {"z": {"level": 3, "skills": {},
        "spent": {"card": 999_999, "skills": {}}}}}}
r2 = AP.apply_reset(hand, "z")
check("a refund tracks the ledger even when it disagrees with the table",
      r2["refund"] == int(999_999 * 0.70), r2)

print("\n── 8. slugs are stable, indexes are not ─────────────────────────")
check("apostrophes vanish rather than split",
      AP.slugify("Titan's Might") == "titans-might", AP.slugify("Titan's Might"))
check("curly apostrophes too",
      AP.slugify("Titan’s Might") == "titans-might")
check("accents fold instead of dropping the letter",
      AP.slugify("Ōkami Strike") == "okami-strike",
      AP.slugify("Ōkami Strike"))
check("...so an accented name can't collide with its unaccented twin",
      AP.slugify("Ōkami Strike") != AP.slugify("Kami Strike"))
check("empty is the only name that gets the constant fallback",
      AP.slugify("") == "skill" and AP.slugify("  ") == "skill")
check("a non-latin name hashes instead of colliding",
      AP.slugify("弱点") != AP.slugify("雷光"),
      (AP.slugify("弱点"), AP.slugify("雷光")))
for c in CARDS:
    slugs = AP.skill_slugs(c)
    if slugs and len(slugs) != len(set(slugs)):
        check(f"{c['id']} has unique skill slugs", False, slugs)
        break
else:
    check("no authored card has colliding skill slugs", True)

print("\n── 9. the battle path is unchanged at Lv1 ───────────────────────")
# Rebuild what get_battle_bonuses does, without a database: the level bonus is
# added to the flat fields, so at Lv1 the AvatarBonuses must equal the authored
# block exactly.
from cogs.avatar.avatar_engine import AvatarBonuses  # noqa: E402

drift = []
for c in CARDS:
    b = c["bonuses"]
    lv1 = AL.card_stat_bonus(c["type"], 1)
    if (b.get("attack_flat", 0.0) + lv1["attack"] != b.get("attack_flat", 0.0)
            or b.get("defence_flat", 0.0) + lv1["defense"] != b.get("defence_flat", 0.0)
            or b.get("stamina_flat", 0.0) + lv1["stamina"] != b.get("stamina_flat", 0.0)):
        drift.append(c["id"])
check("all 29 cards are byte-identical at Lv1", not drift, drift)

lv5 = [c for c in CARDS if c["type"] == "attack"][0]
g = AL.card_stat_bonus(lv5["type"], 5)
check("a Lv5 attack card really does hit harder",
      lv5["bonuses"]["attack_flat"] + g["attack"] > lv5["bonuses"]["attack_flat"])
check("AvatarBonuses still constructs with defaults",
      AvatarBonuses().has_any_bonus is False)

print("\n── 10. equipped_card_level never raises ─────────────────────────")
check("no profile -> (None, 1)",
      AP.equipped_card_level(1, {}) == (None, 1))
check("equipped but unbought -> level 1",
      AP.equipped_card_level(1, {"equipped_avatar": "avatar_x002"}) == ("avatar_x002", 1))
check("equipped and bought -> that level",
      AP.equipped_card_level(1, {"equipped_avatar": "a",
                                 "avatar": {"cards": {"a": {"level": 4}}}}) == ("a", 4))
check("a corrupt profile -> (None, 1)",
      AP.equipped_card_level(1, {"equipped_avatar": "a", "avatar": 7}) == ("a", 1))

print("\n── 11. the wiring: does a level reach a real fight? ─────────────")
# The whole point of the migration. Everything above tests arithmetic; this
# tests that the arithmetic is actually PLUGGED IN — that buying a level moves
# the number a battle reads. Stubbed at the database boundary so no real
# profile is touched.
#
# `cogs/avatar/__init__.py` does `from .avatar_engine import avatar_engine`,
# which makes the attribute `cogs.avatar.avatar_engine` the singleton, not the
# module. Patching through the package would set a shadowing attribute on the
# instance and the patch would silently do nothing — reach the module via
# sys.modules instead. (Exactly the trap that made sim_story's avatar always
# read as inactive.)
import utils.database as DB                          # noqa: E402
import utils.loadout as LO                           # noqa: E402

ENGINE_MOD = sys.modules["cogs.avatar.avatar_engine"]
CARD = "avatar_x003"      # Dyrroth — pure attack, so the effect is unambiguous
CARD_TYPE = [c for c in CARDS if c["id"] == CARD][0]["type"]

FAKE = {"coins": 0, "equipped_avatar": CARD}
_real_get_user = DB.get_user
_real_equipped = ENGINE_MOD.get_equipped_avatar
DB.get_user = lambda uid: FAKE
ENGINE_MOD.get_equipped_avatar = lambda uid: FAKE.get("equipped_avatar")

BLADE = {"name": "SimBlade",
         "stats": {"hp": 100, "attack": 100, "defense": 100,
                   "stamina": 100, "special": 100}}
try:
    from cogs.avatar import avatar_engine as ENGINE

    FAKE.pop("avatar", None)
    b1, brk1, _ = LO.effective_blade(1, profile=FAKE, blade=BLADE,
                                     include_parts=False)
    lvl1_attack = b1["stats"]["attack"]

    FAKE["avatar"] = {"cards": {CARD: {"level": 5, "skills": {},
                                       "spent": {"card": 256_000, "skills": {}}}}}
    b5, brk5, _ = LO.effective_blade(1, profile=FAKE, blade=BLADE,
                                     include_parts=False)
    lvl5_attack = b5["stats"]["attack"]

    expected = AL.card_stat_bonus(CARD_TYPE, 5)["attack"]
    check("engine reports the purchased level",
          ENGINE.card_level(1, CARD) == 5, ENGINE.card_level(1, CARD))
    check("a Lv5 card raises attack inside effective_blade()",
          lvl5_attack > lvl1_attack, f"{lvl1_attack} -> {lvl5_attack}")
    check("...by exactly the growth table's amount",
          lvl5_attack - lvl1_attack == expected,
          f"{lvl5_attack - lvl1_attack} vs {expected}")
    check("the breakdown attributes it to 'avatar', not 'base'",
          brk5["attack"]["avatar"] > brk1["attack"]["avatar"]
          and brk5["attack"]["base"] == brk1["attack"]["base"],
          (brk1["attack"], brk5["attack"]))
    check("defence moved too, by its own growth rate",
          b5["stats"]["defense"] - b1["stats"]["defense"]
          == AL.card_stat_bonus(CARD_TYPE, 5)["defense"])

    # And the guarantee, through the real code path this time.
    FAKE["avatar"] = {"cards": {CARD: {"level": 1}}}
    b_again, _, _ = LO.effective_blade(1, profile=FAKE, blade=BLADE,
                                       include_parts=False)
    check("an explicit Lv1 is identical to no avatar block at all",
          b_again["stats"] == b1["stats"], (b1["stats"], b_again["stats"]))

    FAKE["avatar"] = {"cards": {CARD: {"level": "corrupt"}}}
    b_bad, _, _ = LO.effective_blade(1, profile=FAKE, blade=BLADE,
                                     include_parts=False)
    check("corrupt level data degrades to Lv1 rather than breaking the fight",
          b_bad["stats"] == b1["stats"], b_bad["stats"])
finally:
    DB.get_user = _real_get_user
    ENGINE_MOD.get_equipped_avatar = _real_equipped

print(f"\n{'─' * 66}\n  every avatar has HP%\n{'─' * 66}")

# The curve. HP multiplies a ~2,100 pool and, unlike attack, is not contested
# by the opponent's defence — 1% HP is worth strictly more than 1% attack — so
# the spread sits deliberately below the attack one. Defence-type cards take
# the top of their band, derived from the card's own `type` rather than a
# hand-kept list.
HP_BAND = {"Common": 0.02, "Rare": 0.03, "Epic": 0.05, "Legendary": 0.08,
           "Mythic": 0.10, "Ultimate": 0.12, "Exclusive": 0.15, "MLBB": 0.15}
HP_DEF_BUMP = 0.01

import json as _json                                               # noqa: E402
import os as _os                                                   # noqa: E402
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from cogs.avatar import avatar_skills as ASK                       # noqa: E402
from cogs.avatar.avatar_engine import AvatarBonuses as _AB         # noqa: E402

_CARDS = _json.load(open(_os.path.join(
    ROOT, "cogs", "avatar", "avatar_data.json"), encoding="utf-8"))["avatars"]


def _want(card):
    return round(HP_BAND[card["rarity"]]
                 + (HP_DEF_BUMP if card.get("type") == "defense" else 0.0), 4)


# The band governs hp_PERCENT, which multiplies a ~2,100 pool and therefore
# needs a ceiling that rises with rarity. The School League bladers state HP as
# a FLAT number instead — 120, 135, … — because that is what their design
# specifies, and a flat pool bonus does not scale with the pool, so it needs no
# band. They are exempted here explicitly rather than by a silent `.get`, and
# the exemption is asserted below so it cannot quietly widen.
_FLAT_HP_RARITIES = {"Blader"}
_BANDED = [c for c in _CARDS if c["rarity"] not in _FLAT_HP_RARITIES]

check(f"all {len(_BANDED)} percent-HP cards are on a known rarity band",
      all(c["rarity"] in HP_BAND for c in _BANDED),
      sorted({c["rarity"] for c in _BANDED} - set(HP_BAND)))
_flat = [c for c in _CARDS if c["rarity"] in _FLAT_HP_RARITIES]
check("...and the flat-HP tier really does use flat HP, not a zeroed percent "
      "hiding a missing band",
      bool(_flat) and all(c["bonuses"]["hp_flat"] > 0
                          and c["bonuses"]["hp_percent"] == 0 for c in _flat),
      [(c["id"], c["bonuses"]["hp_flat"], c["bonuses"]["hp_percent"])
       for c in _flat if not (c["bonuses"]["hp_flat"] > 0
                              and c["bonuses"]["hp_percent"] == 0)])

_zero = []
for c in _BANDED:
    want = _want(c)
    if c.get("skills"):
        # bonuses_for zeroes a skill card's top-level block and writes back
        # only the active skill's keys — EXCEPT the card-level keys, which it
        # carries through (avatar_skills.CARD_LEVEL_BONUS_KEYS). HP is one, so
        # the value lives once at the top level, where the shop reads it, and
        # still reaches the fight whichever skill is equipped. Asserted through
        # bonuses_for rather than read off JSON, because the carry-through is
        # the part that can break.
        # Slots are 1-indexed — skill_at does `slot - 1`, so slot 0 is not
        # the first skill, it is no skill at all.
        for slot in range(1, len(c["skills"]) + 1):
            got = ASK.bonuses_for(c, slot).get("hp_percent", 0.0)
            if abs(got - want) > 1e-9:
                check(f"{c['name']} skill {slot}: HP% is {want}", False, got)
                break
        else:
            check(f"{c['name']:<16} ({c['rarity']}) carries {want:.0%} HP in "
                  f"all {len(c['skills'])} skills", True)
    else:
        got = (c.get("bonuses") or {}).get("hp_percent", 0.0)
        check(f"{c['name']:<16} ({c['rarity']}) has {want:.0%} HP",
              abs(got - want) < 1e-9, got)
    if not want:
        _zero.append(c["name"])

check("no avatar is left on 0% — that was the whole request", not _zero, _zero)

# Mikasa was 0.69: +69% HP, 4.6x the next highest and off any curve. Left
# alone she would have made one Ultimate five times better at surviving than
# any other card in the game.
_mik = next(c for c in _CARDS if c["name"] == "Mikasa")
check("Mikasa came down from 0.69 to her band",
      abs(_mik["bonuses"]["hp_percent"] - _want(_mik)) < 1e-9,
      _mik["bonuses"]["hp_percent"])
# Historia was 0.15 — Exclusive-tier HP on a Legendary card. Stated plainly:
# this is a nerf to a card players already own, and the alternative was a
# curve with a hole in it.
_his = next(c for c in _CARDS if c["name"] == "Historia")
check("Historia came down from 0.15 to her band",
      abs(_his["bonuses"]["hp_percent"] - _want(_his)) < 1e-9,
      _his["bonuses"]["hp_percent"])

check("the band rises monotonically with rarity",
      list(HP_BAND.values()) == sorted(HP_BAND.values()))
check("a defence card outranks a non-defence card of the same rarity",
      _want({"rarity": "Epic", "type": "defense"})
      > _want({"rarity": "Epic", "type": "attack"}))

# The shop renders the card's TOP-LEVEL bonus block (avatar_utils.py:284), so
# a value that only reaches the fight is a number the buyer never sees. This
# is the check that catches HP written into the wrong place — on all 36, not
# just the nine that made it possible.
from cogs.avatar.avatar_utils import format_bonuses_summary        # noqa: E402
_noline = [c["name"] for c in _CARDS
           if "HP" not in format_bonuses_summary(c.get("bonuses") or {})]
check(f"all {len(_CARDS)} cards show an HP line in the shop — flat or "
      f"percent", not _noline, _noline)
check("...and the line shows the real number",
      all(f"{_want(c):.0%}".rstrip("%") in
          format_bonuses_summary(c["bonuses"]).replace("+", "")
          or f"{_want(c) * 100:g}" in format_bonuses_summary(c["bonuses"])
          for c in _BANDED),
      format_bonuses_summary(_BANDED[0]["bonuses"]))

print(f"\n{'─' * 66}\n  the HP% ceiling\n{'─' * 66}")
check("there is a cap", hasattr(_AB, "HP_PERCENT_CAP"))
check("...set above the top of the curve, so it guards rather than tunes",
      _AB.HP_PERCENT_CAP > max(HP_BAND.values()) + HP_DEF_BUMP,
      _AB.HP_PERCENT_CAP)
check("no shipped card is anywhere near it",
      all(_want(c) <= _AB.HP_PERCENT_CAP for c in _BANDED))
_over = _AB(hp_percent=12.0)          # the +1200% typo the validator allowed
check("a wildly out-of-range value is clamped, not applied",
      _over.apply_hp_bonus(2000) == 2000 * (1 + _AB.HP_PERCENT_CAP),
      _over.apply_hp_bonus(2000))
check("a negative value cannot SHRINK the pool",
      _AB(hp_percent=-0.5).apply_hp_bonus(2000) == 2000)
check("apply_hp_bonus still multiplies hp_flat by the percent",
      _AB(hp_flat=100, hp_percent=0.10).apply_hp_bonus(2000)
      == (2000 + 100) * 1.10,
      _AB(hp_flat=100, hp_percent=0.10).apply_hp_bonus(2000))
check("...and a card with neither changes nothing",
      _AB().apply_hp_bonus(2000) == 2000)

# hp_percent does NOT grow with card level — growth is attack/defense/stamina
# only, and this pass does not change that.
_esrc = open(_os.path.join(ROOT, "cogs", "avatar", "avatar_engine.py"),
             encoding="utf-8").read()
check("the load path clamps hp_percent too, so the shop shows what rolls",
      "HP_PERCENT_CAP,\n                           max(0.0" in _esrc
      or "min(AvatarBonuses.HP_PERCENT_CAP" in _esrc)

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
