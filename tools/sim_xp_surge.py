#!/usr/bin/env python3
"""
tools/sim_xp_surge.py — the EXP Surge, and boss EXP.

The dangerous part of an EXP multiplier is not the arithmetic, it is COVERAGE
and ORDERING.

**Coverage.** A booster that misses half the grant paths is worse than none:
the player pays and then cannot tell which of the things they did counted.
There are exactly two choke points — `database.grant_xp` (all five trainer
sites) and `bey_levels.award` (all three bey sites) — and every path is driven
through them here.

**Ordering.** Battle and story both run

    award(profile) -> update_user(profile) -> grant_xp(user_id)

with `grant_xp` RE-READING the profile. A charge-based booster decremented in
both would burn two charges for one battle, or lose the decrement entirely to
the `update_user` in between. Time-boxing sidesteps that because both
functions only READ the stamp — and this file proves the ordering is harmless
by driving the exact sequence.

Run:  python3 tools/sim_xp_surge.py
"""
import copy
import os
import sys
import time

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

from utils import xp_boost as XB                                   # noqa: E402
from utils import bey_levels as BL                                 # noqa: E402
import utils.database as DB                                        # noqa: E402

NOW = 1_700_000_000.0

print("\n── 1. the clock, on a frozen now ────────────────────────────────")
check("10x, not 11x — '1000% EXP' read as a multiplier",
      XB.XP_SURGE_MULT == 10.0, XB.XP_SURGE_MULT)
check("one hour", XB.XP_SURGE_SECONDS == 3600, XB.XP_SURGE_SECONDS)
check("400,000 coins", XB.XP_SURGE_PRICE == 400_000, XB.XP_SURGE_PRICE)

for mins, want in ((0, True), (30, True), (59, True), (59.9, True),
                   (60, False), (61, False), (10_000, False)):
    prof = {XB.K_SURGE_UNTIL: int(NOW + 3600)}
    at = NOW + mins * 60
    check(f"{mins:>6} min in: {'boosted' if want else 'expired'}",
          XB.is_active(prof, at) is want, XB.is_active(prof, at))

check("no stamp at all means no Surge", not XB.is_active({}, NOW))
check("a past stamp means no Surge",
      not XB.is_active({XB.K_SURGE_UNTIL: int(NOW - 1)}, NOW))
for junk in ("soon", None, [], {}, "", 0):
    check(f"a junk stamp {junk!r} reads as no Surge",
          not XB.is_active({XB.K_SURGE_UNTIL: junk}, NOW))

check("multiplier is 10x while live",
      XB.multiplier({XB.K_SURGE_UNTIL: int(NOW + 60)}, NOW) == 10.0)
check("...and exactly 1.0 otherwise", XB.multiplier({}, NOW) == 1.0)
live = {XB.K_SURGE_UNTIL: int(NOW + 60)}
check("apply() scales", XB.apply(137, live, NOW) == 1370,
      XB.apply(137, live, NOW))
check("apply() leaves zero alone", XB.apply(0, live, NOW) == 0)
check("apply() leaves negatives alone", XB.apply(-5, live, NOW) == -5)
check("apply() survives junk", XB.apply("x", live, NOW) == 0)

# Reading must never WRITE — this runs on the hottest path in the bot, and a
# write there would also be a write racing the two callers' own writes.
prof = {XB.K_SURGE_UNTIL: int(NOW - 1), "coins": 5}
snap = dict(prof)
XB.is_active(prof, NOW); XB.multiplier(prof, NOW); XB.apply(10, prof, NOW)
check("an expired Surge is not stripped on read — reads stay reads",
      prof == snap, prof)

print("\n── 2. buying one ────────────────────────────────────────────────")
p = {"coins": XB.XP_SURGE_PRICE}
r = XB.buy(p, NOW)
check("charges exactly 400,000", r["spent"] == XB.XP_SURGE_PRICE, r)
check("...leaving the balance at zero", p["coins"] == 0, p["coins"])
check("...and running for an hour",
      p[XB.K_SURGE_UNTIL] == int(NOW + 3600), p[XB.K_SURGE_UNTIL])
check("...reported as new, not extended", r["extended"] is False)
check("the Surge is live immediately", XB.is_active(p, NOW))

# Buying again must EXTEND. Two Surges at once is still 10x, so overlapping
# would charge 400,000 for nothing.
p["coins"] = XB.XP_SURGE_PRICE
r2 = XB.buy(p, NOW + 600)
check("buying again extends rather than overlapping", r2["extended"] is True)
check(f"...by a full hour on top of the remainder",
      p[XB.K_SURGE_UNTIL] == int(NOW + 7200), p[XB.K_SURGE_UNTIL])

# Buying after one lapses starts fresh from now, not from the stale stamp.
p2 = {"coins": XB.XP_SURGE_PRICE, XB.K_SURGE_UNTIL: int(NOW - 5000)}
r3 = XB.buy(p2, NOW)
check("buying after one lapsed starts from now, not the old stamp",
      p2[XB.K_SURGE_UNTIL] == int(NOW + 3600) and r3["extended"] is False,
      p2[XB.K_SURGE_UNTIL])

for short in (1, XB.XP_SURGE_PRICE):
    p3 = {"coins": XB.XP_SURGE_PRICE - short, "xp": 5}
    before = dict(p3)
    try:
        XB.buy(p3, NOW)
        raised = False
    except XB.SurgeError:
        raised = True
    check(f"{short:,} short: refused", raised)
    check(f"{short:,} short: the profile is byte-identical", p3 == before, p3)
try:
    XB.buy({"coins": 0}, NOW)
except XB.SurgeError as exc:
    msg = str(exc)
check("the refusal names the price and the shortfall",
      "400,000" in msg and "short" in msg.lower(), msg)

print("\n── 3. bey EXP — the choke point every path uses ─────────────────")
base = {"bey_progress": {}}
plain = BL.award(copy.deepcopy(base), "Test", 300)
surged = BL.award(dict(copy.deepcopy(base),
                       **{XB.K_SURGE_UNTIL: int(time.time() + 600)}),
                  "Test", 300)
check("unboosted grants what it was given", plain["gained"] == 300,
      plain["gained"])
check("boosted grants ten times that", surged["gained"] == 3000,
      surged["gained"])
check("...and the stored xp matches the granted figure",
      surged["xp"] == 3000, surged["xp"])
check("the reported level reflects the boosted xp",
      surged["level"] > plain["level"], (plain["level"], surged["level"]))
check("a lapsed Surge grants the plain amount",
      BL.award(dict(copy.deepcopy(base),
                    **{XB.K_SURGE_UNTIL: int(time.time() - 1)}),
               "Test", 300)["gained"] == 300)
check("award still refuses to add negative xp",
      BL.award(copy.deepcopy(base), "Test", -50)["gained"] == 0)

src = open(os.path.join(ROOT, "utils", "bey_levels.py"), encoding="utf-8").read()
check("bey_levels stays discord-free",
      not any(ln.strip().startswith(("import discord", "from discord"))
              for ln in src.splitlines()))
check("...and applies the Surge from the profile it already holds",
      "_surge(max(0, int(amount)), profile)" in src)

print("\n── 4. trainer EXP, and the chat carve-out ───────────────────────")
dsrc = open(os.path.join(ROOT, "utils", "database.py"), encoding="utf-8").read()
# Asserted from the real signature rather than by matching the source text.
# It used to match the exact two-line spelling of the `def`, so adding an
# unrelated parameter broke a check about a different one — a test failing for
# a reason that has nothing to do with what it is testing teaches you to
# ignore it.
import inspect                                          # noqa: E402
from utils.database import grant_xp as _grant_xp        # noqa: E402

_gp = inspect.signature(_grant_xp).parameters
check("grant_xp takes a boostable flag", "boostable" in _gp, list(_gp))
check("...defaulting to boosted, so a new caller is covered by default",
      _gp["boostable"].default is True)
check("...and applies the Surge inside the lock, not around it",
      dsrc.index("with _users_lock:", dsrc.index("def grant_xp"))
      < dsrc.index("_surge(xp_amount, profile)"))

csrc = open(os.path.join(ROOT, "cogs", "economy", "chat_xp.py"),
            encoding="utf-8").read()
check("chat opts its TRAINER xp out of the Surge",
      "boostable=False" in csrc)
check("...but leaves bey xp boosted — the split that was asked for",
      "BL.award(profile, blade" in csrc
      and "boostable=False" in csrc.split("grant_xp(uid,", 1)[1][:60]
      and "boostable" not in csrc.split("BL.award(profile, blade", 1)[1][:80])

# Live, through the real store.
uid = 99_000_000_000_000_001
real_get, real_put = DB.USER_STORE.get_one, DB.USER_STORE.put_one
_fake = {}
DB.USER_STORE.get_one = lambda u, **kw: copy.deepcopy(_fake.get(str(u)))
DB.USER_STORE.put_one = (lambda u, p, **kw:
                         _fake.__setitem__(str(u), copy.deepcopy(p)))
try:
    _fake[str(uid)] = {"user_id": str(uid), "xp": 0, "level": 0, "coins": 0}
    DB.grant_xp(uid, 100)
    check("no Surge: a battle win grants the flat 100",
          _fake[str(uid)]["xp"] == 100, _fake[str(uid)]["xp"])

    _fake[str(uid)] = {"user_id": str(uid), "xp": 0, "level": 0, "coins": 0,
                       XB.K_SURGE_UNTIL: int(time.time() + 600)}
    DB.grant_xp(uid, 100)
    check("Surge: the same win grants 1,000",
          _fake[str(uid)]["xp"] == 1000, _fake[str(uid)]["xp"])

    _fake[str(uid)] = {"user_id": str(uid), "xp": 0, "level": 0, "coins": 0,
                       XB.K_SURGE_UNTIL: int(time.time() + 600)}
    DB.grant_xp(uid, 70, boostable=False)
    check("Surge + boostable=False: chat still grants the plain 70",
          _fake[str(uid)]["xp"] == 70, _fake[str(uid)]["xp"])

    # THE ORDERING. award mutates the dict in hand; update_user persists it;
    # grant_xp then RE-READS. A charge-based design double-spends here.
    _fake[str(uid)] = {"user_id": str(uid), "xp": 0, "level": 0, "coins": 0,
                       "bey_progress": {},
                       XB.K_SURGE_UNTIL: int(time.time() + 600)}
    prof = DB.get_user(uid)
    gain = BL.award(prof, "Test", 300)
    DB.update_user(uid, prof)
    lvl, total, _up = DB.grant_xp(uid, 100)
    stored = _fake[str(uid)]
    check("award -> update_user -> grant_xp: bey xp survives the re-read",
          stored["bey_progress"]["Test"]["xp"] == 3000,
          stored["bey_progress"]["Test"]["xp"])
    check("...and trainer xp is boosted exactly once",
          stored["xp"] == 1000, stored["xp"])
    check("...and the Surge stamp is untouched by either",
          stored[XB.K_SURGE_UNTIL] > time.time(), stored.get(XB.K_SURGE_UNTIL))
    check("the reported bey gain matches what was stored",
          gain["gained"] == 3000, gain["gained"])

    # Ten rounds of the same sequence: nothing drifts, nothing is spent twice.
    _fake[str(uid)] = {"user_id": str(uid), "xp": 0, "level": 0, "coins": 0,
                       "bey_progress": {},
                       XB.K_SURGE_UNTIL: int(time.time() + 600)}
    for _ in range(10):
        prof = DB.get_user(uid)
        BL.award(prof, "Test", 300)
        DB.update_user(uid, prof)
        DB.grant_xp(uid, 100)
    check("ten battles: bey xp is exactly 10 x 3,000",
          _fake[str(uid)]["bey_progress"]["Test"]["xp"] == 30_000,
          _fake[str(uid)]["bey_progress"]["Test"]["xp"])
    check("...and trainer xp exactly 10 x 1,000",
          _fake[str(uid)]["xp"] == 10_000, _fake[str(uid)]["xp"])
finally:
    DB.USER_STORE.get_one, DB.USER_STORE.put_one = real_get, real_put

print("\n── 5. bosses grant EXP at all now ───────────────────────────────")
import cogs.battle.boss.boss_battle as BB                          # noqa: E402
from cogs.battle.boss import boss_tiers as BT                      # noqa: E402

check("a boss win is worth trainer EXP", BB.BOSS_TRAINER_XP > 0,
      BB.BOSS_TRAINER_XP)
check("...and bey EXP", BB.BOSS_BEY_XP > 0, BB.BOSS_BEY_XP)
check("a boss is worth more than a PvP win (100 trainer)",
      BB.BOSS_TRAINER_XP > 100, BB.BOSS_TRAINER_XP)
check("...and more bey EXP than one too (300-459)",
      BB.BOSS_BEY_XP > 459, BB.BOSS_BEY_XP)
vals = [BT.scale_reward(BB.BOSS_BEY_XP, k) for k in BT.ORDERED]
check(f"boss EXP scales with the difficulty tier: {vals}",
      all(b > a for a, b in zip(vals, vals[1:])), vals)

bsrc = open(os.path.join(ROOT, "cogs", "battle", "boss", "boss_battle.py"),
            encoding="utf-8").read()
fin = bsrc.split("        for member in fight.party:", 1)[1][:2600]
check("bey EXP lands BEFORE the update_user that persists it",
      fin.index("bcopy_levels.award") < fin.index("update_user(member.id"))
check("...and trainer EXP AFTER it, because grant_xp re-reads",
      fin.index("update_user(member.id") < fin.index("grant_xp(member.id"))
check("a boss copy is skipped, as everywhere else",
      'profile.get("active_copy")' in fin)
check("an EXP failure cannot lose the coins that were already granted",
      fin.count("except Exception") >= 2)
check("the win embed reports EXP", "📈 Experience" in bsrc)
check("...from the granted figure, not the constant",
      "gain['gained']" in bsrc)

print("\n── 6. the command ───────────────────────────────────────────────")
ssrc = open(os.path.join(ROOT, "cogs", "economy", "shop.py"),
            encoding="utf-8").read()
check("there is a ;surge command", 'name="surge"' in ssrc)
check("...not called 'booster', which already means the gacha pack",
      'name="booster"' in ssrc and '"expsurge"' in ssrc)
check("it shows the status before asking for money", '"Status"' in ssrc)
check("it renders the deadline as a live timestamp", "<t:{until}:R>" in ssrc)
check("it explains the chat carve-out",
      "Trainer EXP from chat stays" in ssrc)
check("buying goes through the locked transaction", "XB.buy_for(" in ssrc)
hsrc = open(os.path.join(ROOT, "cogs", "ui", "help_cog.py"),
            encoding="utf-8").read()
check("it is in help", ";surge" in hsrc)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
