#!/usr/bin/env python3
"""
tools/sim_community.py — the community layer, and above all the main-server lock.

This suite exists to catch one class of failure: a community feature reaching a
server it was never meant to touch. Hiding a slash command does not prevent
that, so the lock is enforced in three places and checked in all three here —
including a sweep that walks every public method on every manager by
introspection, so a method added next month without `require_main` fails on the
day it is written rather than the day it leaks.

The second thing it exists to catch is XP farming, which is measured rather
than asserted: the suite drives real messages through the real award path with
a stubbed clock and reports what a spammer would actually earn.

Everything runs against a REAL UserStore on a temp file. The composite primary
keys are the duplicate prevention, and a fake store would prove nothing about
them.

Run:  python3 tools/sim_community.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

logging.disable(logging.CRITICAL)

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {detail}")


# ── A real store on a temp file, installed before the package is imported ────
import utils.database as DB                                       # noqa: E402
from utils.userstore import UserStore                             # noqa: E402

_TMP = tempfile.mkdtemp()
STORE = UserStore(os.path.join(_TMP, "sim.db"))
STORE.ensure_ready()
DB.USER_STORE = STORE

import discord                                                    # noqa: E402
from utils import ranked as RK                                    # noqa: E402
from cogs.community import config as C                            # noqa: E402
from cogs.community import giveaways as GV                        # noqa: E402
from cogs.community import guard                                  # noqa: E402
from cogs.community import polls as PL                            # noqa: E402
from cogs.community import store as S                             # noqa: E402
from cogs.community import xp as XP                               # noqa: E402

MAIN = 111_111_111
OTHER = 222_222_222


def seed(uid, **fields):
    profile = DB._default_profile(str(uid))
    profile.update(fields)
    STORE.put_one(str(uid), profile)
    return profile


# ── Fake discord objects ─────────────────────────────────────────────────────
class FakeRole:
    def __init__(self, rid, name="Blader", position=5, managed=False,
                 default=False, guild=None):
        self.id = int(rid)
        self.name = name
        self.position = position
        self.managed = managed
        self._default = default
        self.guild = guild

    def is_default(self):
        return self._default

    def __str__(self):
        return self.name


class FakePerms:
    def __init__(self, manage_roles=True):
        self.manage_roles = manage_roles


class FakeMe:
    def __init__(self, top_position=10, manage_roles=True):
        self.top_role = FakeRole(1, "bot", top_position)
        self.guild_permissions = FakePerms(manage_roles)


class FakeGuild:
    def __init__(self, gid=MAIN, top_position=10, manage_roles=True):
        self.id = int(gid)
        self.name = "Main"
        self.me = FakeMe(top_position, manage_roles)
        self._roles = {}

    def get_role(self, rid):
        return self._roles.get(int(rid))


class FakeMember:
    def __init__(self, uid, guild=None, roles=()):
        self.id = int(uid)
        self.bot = False
        self.guild = guild
        self.roles = list(roles)
        self.display_name = f"user{uid}"
        self.mention = f"<@{uid}>"
        self.added = []

    async def add_roles(self, role, reason=None):
        self.added.append(role.id)
        self.roles.append(role)


class FakeResponse:
    def __init__(self, box):
        self.box = box
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, **kw):
        self._done = True
        self.box.append({"content": content, **kw})

    async def send_modal(self, modal):
        self._done = True
        self.box.append({"modal": modal})

    async def edit_message(self, **kw):
        self._done = True
        self.box.append({"edit": kw})


class FakeFollowup:
    def __init__(self, box):
        self.box = box

    async def send(self, content=None, **kw):
        self.box.append({"content": content, **kw})


class FakeInteraction:
    def __init__(self, guild_id=MAIN, user_id=5001, guild=None):
        self.guild_id = guild_id
        self.guild = guild or (FakeGuild(guild_id) if guild_id else None)
        self.user = FakeMember(user_id, self.guild)
        self.channel_id = 999
        self.channel = None
        self.sent: list = []
        self.response = FakeResponse(self.sent)
        self.followup = FakeFollowup(self.sent)
        self.client = None


class FakeBot:
    def __init__(self):
        self.dispatched = []

    def get_cog(self, name):
        return None

    def get_channel(self, cid):
        return None

    def get_guild(self, gid):
        return FakeGuild(gid) if gid else None

    def dispatch(self, name, *a):
        self.dispatched.append((name, a))


def main() -> int:
    asyncio.run(suite())
    print("\n" + "=" * 66)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 66)
    return 1 if FAIL else 0


async def suite() -> None:
    # ── 1. unset means locked, everywhere ───────────────────────────────────
    print("\n── 1. with no main server, everything refuses ──────────────────")
    C.invalidate()
    guard.set_main_guild(None)
    C.invalidate()
    check("no main server is configured to begin with",
          guard.main_guild_id() is None, guard.main_guild_id())
    xpm, lvm = XP.XPManager(), XP.LevelManager()
    pm, gm = PL.PollManager(), GV.GiveawayManager()
    refused = 0
    for gid in (MAIN, OTHER, None, "123"):
        for call in (lambda: xpm.grant(gid, 1, 5),
                     lambda: pm.create(gid, 1, "q", ["a", "b"]),
                     lambda: gm.create(gid, 1, "prize"),
                     lambda: lvm.roles_for(gid, 5)):
            try:
                call()
            except guard.NotMainServer:
                refused += 1
    check("every entry point refuses in every guild while it is unset",
          refused == 16, refused)
    check("...including the guild that will later BE the main server",
          not guard.is_main(MAIN))

    # ── 2. the lock, swept by introspection ─────────────────────────────────
    print("\n── 2. the lock, on every manager method there is ───────────────")
    guard.set_main_guild(MAIN)
    C.invalidate()
    check("the main server is now set", guard.is_main(MAIN))
    check("...and a different guild is still not it", not guard.is_main(OTHER))

    # A poll and a giveaway that exist, so the guild-scoped methods have real
    # ids to be refused for rather than failing on a missing row.
    poll = pm.create(MAIN, 1, "Best blade?", ["Valkyrie", "Spriggan"],
                     duration=60)
    give = gm.create(MAIN, 1, "Ultimate Valkyrie", duration=60, winners=2)

    import inspect
    ARGS = {
        "guild_id": OTHER, "user_id": 5001, "poll_id": poll["poll_id"],
        "giveaway_id": give["giveaway_id"], "author_id": 1, "host_id": 1,
        "question": "q", "options": ["a", "b"], "prize": "p", "amount": 5,
        "choice": 0, "content": "a real message here", "level": 5,
        "message_id": 4242, "count": 1, "role": None, "member": None,
        "profile": {}, "limit": 5,
    }
    swept = leaked = 0
    for manager in (xpm, lvm, pm, gm):
        for name, fn in inspect.getmembers(manager, callable):
            if name.startswith("_"):
                continue
            sig = inspect.signature(fn)
            if "guild_id" not in sig.parameters:
                continue          # loop-facing (due/recover); no guild to check
            kwargs = {}
            for pname, param in sig.parameters.items():
                if param.default is inspect.Parameter.empty:
                    kwargs[pname] = ARGS.get(pname, 1)
            swept += 1
            try:
                out = fn(**kwargs)
                if inspect.iscoroutine(out):
                    await out
                leaked += 1
                print(f"       LEAK: {type(manager).__name__}.{name}")
            except guard.NotMainServer:
                pass
            except Exception:
                # Refused some other way (bad args) — not proof, so it counts
                # as a leak for the purposes of this sweep.
                leaked += 1
                print(f"       UNPROVEN: {type(manager).__name__}.{name}")
    check(f"all {swept} guild-taking manager methods raise NotMainServer for a "
          f"foreign guild", leaked == 0 and swept >= 20, (swept, leaked))

    # ── 3. the command layer ────────────────────────────────────────────────
    print("\n── 3. the command layer refuses before it does anything ────────")
    from cogs.community import cog as CG
    bot = FakeBot()
    cog = CG.CommunityCog(bot)
    for name in ("poll", "giveaway", "level"):
        command = getattr(CG.CommunityCog, name)
        it = FakeInteraction(guild_id=OTHER)
        await command.callback(cog, it, None)
        said = str(it.sent[0].get("content") or "")
        check(f"/{name} refuses in another server",
              "main-server" in said or "isn't available" in said, it.sent)
        check(f"...and says nothing about a modal for /{name}",
              "modal" not in it.sent[0], it.sent[0].keys())

    it = FakeInteraction(guild_id=MAIN, user_id=5001)
    await CG.CommunityCog.level.callback(cog, it, None)
    check("/level works in the main server",
          bool(it.sent and it.sent[0].get("embed")), it.sent)

    # ── 4. the event layer ──────────────────────────────────────────────────
    print("\n── 4. the event layer never awards outside the main server ─────")
    seed(6001)
    before = DB.get_user(6001).get(XP.K_XP, 0)
    check("listener_ok says no for another guild",
          not guard.listener_ok(FakeGuild(OTHER)))
    check("...and yes for the main one", guard.listener_ok(FakeGuild(MAIN)))
    try:
        xpm.award_message(OTHER, 6001, "a long enough message to count")
    except guard.NotMainServer:
        pass
    check("a message from another server earns nothing",
          DB.get_user(6001).get(XP.K_XP, 0) == before)

    # ── 5. duplicate prevention is the database's job ───────────────────────
    print("\n── 5. one vote, one entry, whatever the client does ────────────")
    out1 = pm.vote(MAIN, poll["poll_id"], 7001, 0)
    out2 = pm.vote(MAIN, poll["poll_id"], 7001, 1)
    check("a second vote from the same player is refused",
          out1["ok"] and not out2["ok"] and out2.get("already"), (out1, out2))
    check("...and the tally counts them once",
          pm.results(MAIN, poll["poll_id"])["counts"] == [1, 0],
          pm.results(MAIN, poll["poll_id"]))

    hammer = pm.create(MAIN, 1, "race", ["a", "b"], duration=60)
    wins = []
    barrier = threading.Barrier(16)

    def race():
        barrier.wait()
        wins.append(S.vote(hammer["poll_id"], 7100, "0"))

    threads = [threading.Thread(target=race) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("sixteen threads voting at once produce exactly one accepted vote",
          sum(1 for w in wins if w) == 1, wins)
    check("...and exactly one row",
          sum(pm.results(MAIN, hammer["poll_id"])["counts"]) == 1)

    e1 = gm.enter(MAIN, give["giveaway_id"], 7001)
    e2 = gm.enter(MAIN, give["giveaway_id"], 7001)
    check("a second giveaway entry is refused",
          e1["ok"] and not e2["ok"], (e1, e2))
    check("...and the entry count stays at one",
          S.entry_count(give["giveaway_id"]) == 1)

    # ── 6. restart recovery ─────────────────────────────────────────────────
    print("\n── 6. a process killed mid-close resumes ───────────────────────")
    now = time.time()
    timed = pm.create(MAIN, 1, "closes", ["a", "b"], duration=1)
    claimed = pm.due(now + 5)
    check("a poll past its deadline is claimed once",
          timed["poll_id"] in claimed, claimed)
    check("...and a second tick claims nothing",
          timed["poll_id"] not in pm.due(now + 5))
    check("the claimed poll is parked in CLOSING, not lost",
          S.get_poll(timed["poll_id"])["state"] == PL.CLOSING)
    check("recovery returns it to the queue", pm.recover() >= 1)
    check("...and it is claimable again after the restart",
          timed["poll_id"] in pm.due(now + 5))
    pm.close(MAIN, timed["poll_id"])
    frozen = S.get_poll(timed["poll_id"])
    check("closing freezes the tally into the row",
          frozen["state"] == PL.CLOSED and frozen["results"] is not None,
          frozen["state"])
    check("closing twice does not change the result",
          pm.close(MAIN, timed["poll_id"])["results"] == frozen["results"])

    # ── 7. giveaway draws and rerolls ───────────────────────────────────────
    print("\n── 7. a reroll can never re-pick a winner ──────────────────────")
    big = gm.create(MAIN, 1, "Pack", duration=1, winners=2)
    for uid in range(7200, 7208):
        gm.enter(MAIN, big["giveaway_id"], uid)
    ended = gm.end(MAIN, big["giveaway_id"])
    check("the draw picks the requested number of winners",
          len(ended["winners"]) == 2, ended["winners"])
    check("...recorded on the row, not held in memory",
          set(S.get_giveaway(big["giveaway_id"])["winner_ids"])
          == set(ended["winners"]))
    # Everything in memory is discarded here — a fresh manager is exactly what
    # the next process would have.
    fresh = GV.GiveawayManager()
    again = fresh.reroll(MAIN, big["giveaway_id"], 2)
    check("a reroll after a restart still excludes the first winners",
          not (set(again["winners"]) & set(ended["winners"])),
          (ended["winners"], again["winners"]))
    check("...and the history keeps growing",
          len(S.get_giveaway(big["giveaway_id"])["winner_ids"]) == 4,
          S.get_giveaway(big["giveaway_id"])["winner_ids"])
    fresh.reroll(MAIN, big["giveaway_id"], 8)        # drain the remaining 4
    drained = fresh.reroll(MAIN, big["giveaway_id"], 2)
    check("when everyone eligible has won, the reroll says so rather than "
          "repeating somebody", not drained["ok"], drained)
    check("...and every one of the 8 entrants won exactly once, none twice",
          sorted(S.get_giveaway(big["giveaway_id"])["winner_ids"])
          == sorted({str(u) for u in range(7200, 7208)}),
          S.get_giveaway(big["giveaway_id"])["winner_ids"])
    check("ending an ended giveaway does not draw again",
          not gm.end(MAIN, big["giveaway_id"])["ok"])

    # ── 8. anti-farming, measured ───────────────────────────────────────────
    print("\n── 8. what a spammer actually earns ────────────────────────────")
    seed(7300)
    t0 = time.time()
    earned = paid = 0
    for i in range(200):                       # 200 messages over 200 seconds
        res = xpm.award_message(MAIN, 7300, f"message number {i} about blades",
                                now=t0 + i)
        earned += res["awarded"]
        paid += 1 if res["awarded"] else 0
    check(f"200 messages over 200s pay {paid} times, not 200",
          paid == 4, paid)
    check("...which is exactly the 60s cooldown", paid == 200 // 60 + 1, paid)
    print(f"       community XP earned: {earned} over 200 messages")

    seed(7301)
    res = xpm.award_message(MAIN, 7301, "hi", now=t0)
    check("a message under the minimum length earns nothing",
          res["awarded"] == 0 and res["refused"] == "too short", res)
    xpm.award_message(MAIN, 7301, "the same sentence exactly", now=t0)
    dup = xpm.award_message(MAIN, 7301, "the same sentence exactly",
                            now=t0 + 120)
    check("repeating yourself earns nothing even after the cooldown",
          dup["awarded"] == 0 and dup["refused"] == "same message again", dup)
    fresh_text = xpm.award_message(MAIN, 7301, "something else entirely now",
                                   now=t0 + 180)
    check("...but a different sentence does", fresh_text["awarded"] > 0)

    seed(7302)
    # Anchored to a UTC midnight, because the cap is a UTC-DAY cap and 400
    # messages a minute apart span nearly seven hours. The first version of
    # this check drifted over midnight and read the reset as a breach.
    import datetime as _dt
    midnight = _dt.datetime.now(_dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    total = 0
    for i in range(300):                       # 5 hours of talking, one day
        total += xpm.award_message(MAIN, 7302, f"unique sentence {i} here",
                                   now=midnight + 60 + i * 61)["awarded"]
    check(f"the daily cap holds at {XP.DAILY_XP_CAP}",
          total == XP.DAILY_XP_CAP, total)
    tomorrow = xpm.award_message(MAIN, 7302, "first thing the next morning",
                                 now=midnight + 86400 + 60)
    check("...and resets on the UTC day boundary rather than locking the "
          "player out forever", tomorrow["awarded"] > 0, tomorrow)

    seed(7303)
    own = xpm.award_reaction(MAIN, 7303, 900, author_id=7303)
    check("reacting to your own message earns nothing",
          own["awarded"] == 0 and own["refused"] == "own message", own)
    first = xpm.award_reaction(MAIN, 7303, 901, author_id=99)
    second = xpm.award_reaction(MAIN, 7303, 901, author_id=99)
    check("reacting twice to the same message pays once",
          first["awarded"] > 0 and second["awarded"] == 0,
          (first["awarded"], second.get("refused")))

    # chat_xp, before and after — the trainer track, measured not claimed
    print("\n       trainer chat XP (cogs/economy/chat_xp.py):")
    from cogs.economy import chat_xp as CX
    from utils import bey_levels as BL
    cx = CX.ChatXPCog(bot)
    window = float(BL.XP_CHAT_COOLDOWN_S or 0)
    check("the chat XP cooldown is live rather than documented as zero",
          window > 0, window)
    paid_now = sum(1 for i in range(600)
                   if _tick(cx, 7400, t0 + i * 0.1))    # 10 msgs/sec, 60s
    per_min = paid_now * sum(BL.XP_CHAT) / 2
    print(f"       at {window}s: {paid_now} paid messages a minute "
          f"≈ {per_min:,.0f} trainer XP/min")
    cx._paid_at.clear()
    BL.XP_CHAT_COOLDOWN_S = 0
    uncapped = sum(1 for i in range(600) if _tick(cx, 7401, t0 + i * 0.1))
    print(f"       at 0s (before): {uncapped} paid messages a minute "
          f"≈ {uncapped * sum(BL.XP_CHAT) / 2:,.0f} trainer XP/min")
    BL.XP_CHAT_COOLDOWN_S = window
    check("the cooldown cuts what a spammer earns by an order of magnitude",
          paid_now * 10 <= uncapped, (paid_now, uncapped))

    # ── 9. the two tracks never touch ───────────────────────────────────────
    print("\n── 9. community XP is not trainer XP ───────────────────────────")
    seed(7500)
    before = DB.get_user(7500)
    for i in range(30):
        xpm.award_message(MAIN, 7500, f"a distinct sentence {i}",
                          now=t0 + i * 61)
    after = DB.get_user(7500)
    check("trainer xp did not move", after["xp"] == before["xp"],
          (before["xp"], after["xp"]))
    check("trainer level did not move", after["level"] == before["level"])
    check("community xp did", after[XP.K_XP] > 0, after[XP.K_XP])
    check("the community level is NOT stored under `level`, which get_user "
          "recomputes", XP.K_LEVEL != "level")
    check("...and survives a re-read", DB.get_user(7500)[XP.K_LEVEL]
          == after[XP.K_LEVEL])

    seed(7501)
    xpm.grant(MAIN, 7501, XP.xp_for_level(3))
    card = xpm.card(MAIN, 7501)
    check("the curve puts the right XP at the right level",
          card["level"] == 3, card)
    check("level 0 needs nothing and level 1 needs the constant",
          XP.xp_for_level(0) == 0
          and XP.xp_for_level(1) == XP.XP_PER_LEVEL_SQ)
    check("the curve is monotonic and capped",
          XP.level_from_xp(10 ** 12) == XP.MAX_LEVEL, XP.level_from_xp(10 ** 12))

    # ── 10. role rewards are validated before anything is granted ───────────
    print("\n── 10. level roles cannot be given out unsafely ────────────────")
    guild = FakeGuild(MAIN, top_position=10)
    ok, why = lvm.check_role(guild, FakeRole(50, "Blader", 5, guild=guild))
    check("a normal role below the bot is fine", ok, why)
    ok, why = lvm.check_role(guild, FakeRole(51, "Admin", 20, guild=guild))
    check("a role above the bot is refused, and says so",
          not ok and "above my own" in why, why)
    ok, why = lvm.check_role(guild, FakeRole(52, "Booster", 3, managed=True,
                                             guild=guild))
    check("a managed role is refused", not ok and "managed" in why, why)
    ok, why = lvm.check_role(guild, FakeRole(53, "@everyone", 0, default=True,
                                             guild=guild))
    check("@everyone is refused", not ok, why)
    noperm = FakeGuild(MAIN, manage_roles=False)
    ok, why = lvm.check_role(noperm, FakeRole(54, "Blader", 2, guild=noperm))
    check("without Manage Roles nothing is attempted, and the reason names "
          "the permission", not ok and "Manage Roles" in why, why)

    C.put(C.K_LEVEL_ROLES, {})
    good = FakeRole(60, "Blader", 5, guild=guild)
    guild._roles[60] = good
    ok, message = lvm.set_level_role(MAIN, 5, good)
    check("a valid mapping saves", ok, message)
    bad = FakeRole(61, "Boss", 30, guild=guild)
    ok, _ = lvm.set_level_role(MAIN, 6, bad)
    check("an invalid one is refused at save time, so the mapping never holds "
          "a role that cannot be granted", not ok)
    member = FakeMember(7600, guild)
    out = await lvm.apply_roles(MAIN, member, 5)
    check("reaching the level grants the role", out["added"] == [60], out)
    out2 = await lvm.apply_roles(MAIN, member, 5)
    check("...and does not grant it twice", out2["added"] == [], out2)
    check("a lower level earns nothing yet",
          lvm.roles_for(MAIN, 4) == [], lvm.roles_for(MAIN, 4))

    # ── 11. buttons survive a restart ───────────────────────────────────────
    print("\n── 11. every button is rebuildable from its own custom_id ──────")
    pid = poll["poll_id"]
    parsed = PL.parse_custom_id(f"beypoll:{pid}:3")
    check("a poll vote id round-trips",
          parsed == {"pid": pid, "idx": 3}, parsed)
    check("a malformed one is rejected rather than half-parsed",
          PL.parse_custom_id("beypoll:nope") is None)
    gid_ = give["giveaway_id"]
    check("a giveaway entry id round-trips",
          GV.parse_custom_id(f"beygive:{gid_}:enter")
          == {"gid": gid_, "act": "enter"})
    check("the generated ids stay inside the template's charset",
          PL.parse_custom_id(f"beypoll:{pid}:0") is not None
          and GV.parse_custom_id(f"beygive:{gid_}:enter") is not None)
    check("...and inside Discord's 100-character custom_id limit",
          len(f"beypoll:{pid}:0") < 100 and len(f"beygive:{gid_}:enter") < 100)

    # ── 12. the views are really built ──────────────────────────────────────
    print("\n── 12. the views build, inside a running loop ──────────────────")
    view = PL.view_for(poll)
    check("a poll view has one button per choice",
          len(view.children) == len(poll["options"]), len(view.children))
    check("...each carrying its own poll id",
          all(c.custom_id.startswith(f"beypoll:{pid}:")
              for c in view.children),
          [c.custom_id for c in view.children])
    gview = GV.view_for(gid_)
    check("a giveaway view has the entry button", len(gview.children) == 1)
    embed = PL.build_embed(poll, pm.results(MAIN, pid))
    check("the poll embed renders", isinstance(embed, discord.Embed)
          and embed.title)
    check("an open anonymous poll does not leak the tally before it closes",
          "▰" not in (PL.build_embed(poll).description or ""),
          PL.build_embed(poll).description)
    gembed = GV.build_embed(S.get_giveaway(gid_), 3)
    check("the giveaway embed renders", isinstance(gembed, discord.Embed))

    from cogs.community import panel as PN
    pview = PN.ServerView(bot, 1, lvm)
    kinds = [type(c).__name__ for c in pview.children]
    check("the /server panel builds with its selects and buttons",
          "PersonalitySelect" in kinds and "LevelRoleSelect" in kinds
          and len([k for k in kinds if k == "Button"]) == 5, kinds)
    pembed = PN.build_embed(bot, guild)
    check("...and its embed names the configured server",
          str(MAIN) in (pembed.description or ""), pembed.description)
    guard.set_main_guild(None)
    C.invalidate()
    unset_embed = PN.build_embed(bot, guild)
    check("with nothing configured the panel says so plainly",
          "No main server set" in (unset_embed.description or ""),
          unset_embed.description)
    guard.set_main_guild(MAIN)
    C.invalidate()

    # ── 13. the boards ──────────────────────────────────────────────────────
    print("\n── 13. community boards exist, and are main-server only ────────")
    check("both boards are registered",
          {"chatxp", "commlevel"} <= set(RK.CATEGORIES), list(RK.CATEGORIES))
    check("...and are declared main-server only",
          RK.MAIN_ONLY == frozenset({"chatxp", "commlevel"}), RK.MAIN_ONLY)
    rows = RK.build_board([DB.get_user(7500), DB.get_user(7302)], "chatxp", 10)
    check("the board sorts by community xp",
          rows and rows[0][1] >= rows[-1][1], [r[1] for r in rows])
    check("a player with no community xp is not on it",
          all(RK.community_xp(p) > 0 for p, _ in rows))
    check("the choice list still fits Discord's 25-choice cap",
          len(RK.CATEGORIES) <= 25, len(RK.CATEGORIES))
    src = open(os.path.join(ROOT, "cogs/ranked/ranked_cog.py")).read()
    check("the refusal is in the command that renders a board, not only in "
          "the choice list", "MAIN_ONLY" in src)

    # ── 14. the commands are registered ─────────────────────────────────────
    print("\n── 14. the surface is really there ─────────────────────────────")
    names = {c.name for c in (CG.CommunityCog.__cog_app_commands__ or [])}
    check("/server, /poll, /giveaway and /level are all registered",
          {"server", "poll", "giveaway", "level"} <= names, names)
    check("the cog listens for messages and reactions",
          hasattr(CG.CommunityCog, "community_message_xp")
          and hasattr(CG.CommunityCog, "community_reaction_xp"))
    check("the scheduler loop exists and recovers before it claims",
          hasattr(cog, "community_loop")
          and "recover" in CG.CommunityCog.community_loop.coro.__code__.co_names)
    check("/server is NOT gated on the main server — it is what sets it",
          "gate" not in CG.CommunityCog.server.callback.__code__.co_names,
          CG.CommunityCog.server.callback.__code__.co_names)


def _tick(cx, uid, when) -> bool:
    """One message through chat_xp's gate at time `when`. True = it paid."""
    if not cx.ready(uid, when):
        return False
    cx.stamp(uid, when)
    return True


if __name__ == "__main__":
    sys.exit(main())
