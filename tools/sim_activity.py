#!/usr/bin/env python3
"""
tools/sim_activity.py — the day tracker behind "who played the bot today".

What this covers
----------------
Nothing in this bot recorded what commands people run, so v1.18 added
`utils/activity.py`: one dict increment per completed command, flushed to a
file every couple of minutes. Cheap to write is exactly what makes it easy to
get wrong, and three of those ways are silent:

* **Unbounded growth.** A counter keyed by user id and command name grows with
  the player base, and a burst of unknown names grows it with no player base at
  all. Both keys are capped — and the counts past the cap must still land, or
  the tracker reports a quiet day during the busiest one.
* **A day that never ends.** The tally is "today". If nothing rolls it over at
  UTC midnight, the panel keeps adding yesterday's numbers to today's and
  nothing ever looks wrong — it just slowly becomes a lifetime total wearing
  the word "today".
* **A restart eating the day.** The bot deploys by extracting a zip over a live
  install, so restarts happen mid-day. Reloading yesterday's file would be
  worse than losing today's: it would answer the question with the wrong day.

And one that is not silent at all: this runs on the event loop in front of
every command in the bot, so it must never raise.

Run:  python3 tools/sim_activity.py
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

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


from utils import activity as ACT                       # noqa: E402


def _raises_typeerror(fn) -> bool:
    try:
        fn()
    except TypeError:
        return True
    except Exception:                                    # noqa: BLE001
        return False
    return False

# Two fixed instants, one either side of a UTC midnight, so nothing here
# depends on when the suite is run.
DAY1 = datetime(2026, 3, 14, 22, 30, tzinfo=timezone.utc).timestamp()
DAY1_LATE = datetime(2026, 3, 14, 23, 59, tzinfo=timezone.utc).timestamp()
DAY2 = datetime(2026, 3, 15, 0, 1, tzinfo=timezone.utc).timestamp()


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. it counts, per player and per command ─────────────────────")

ACT.reset(DAY1)
for _ in range(3):
    ACT.record(111, "battle", now=DAY1)
ACT.record(111, "daily", now=DAY1)
ACT.record(222, "battle", now=DAY1)

check("the day is the UTC date", ACT.today(DAY1) == "2026-03-14", ACT.today(DAY1))
check("two players were active", ACT.active_users(DAY1) == 2,
      ACT.active_users(DAY1))
check("five commands ran", ACT.total_commands(DAY1) == 5,
      ACT.total_commands(DAY1))
check("per player: what THEY ran",
      ACT.user_commands(111, DAY1) == {"battle": 3, "daily": 1},
      ACT.user_commands(111, DAY1))
check("busiest player first", ACT.top_users(10, DAY1)[0] == (111, 4),
      ACT.top_users(10, DAY1))
check("the command tally is across everybody, not per player",
      dict(ACT.command_tally(10, DAY1)) == {"battle": 4, "daily": 1},
      ACT.command_tally(10, DAY1))
check("...busiest command first", ACT.command_tally(10, DAY1)[0][0] == "battle")

check("a snapshot answers all three questions at once",
      set(ACT.snapshot(DAY1)) >=
      {"day", "active_users", "total_commands", "top_users", "commands"},
      sorted(ACT.snapshot(DAY1)))

# The two surfaces are counted apart on purpose: `/player` invoking `;profile`
# does NOT dispatch command_completion, so `/player` is one run, not two.
ACT.reset(DAY1)
ACT.record(111, "profile", now=DAY1)
ACT.record(111, "/player", now=DAY1)
check("the slash surface counts under its own name",
      ACT.user_commands(111, DAY1) == {"profile": 1, "/player": 1},
      ACT.user_commands(111, DAY1))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. names are normalised, junk is refused ─────────────────────")

ACT.reset(DAY1)
ACT.record(111, "  BATTLE  ", now=DAY1)
ACT.record(111, "battle", now=DAY1)
check("case and whitespace are one command, not three",
      ACT.user_commands(111, DAY1) == {"battle": 2},
      ACT.user_commands(111, DAY1))

ACT.record(111, "", now=DAY1)
ACT.record(111, "   ", now=DAY1)
ACT.record(111, None, now=DAY1)
check("an empty command name is not counted", ACT.total_commands(DAY1) == 2,
      ACT.total_commands(DAY1))

ACT.record(111, "x" * 500, now=DAY1)
check("a long name is clamped rather than stored whole",
      max(len(k) for k, _ in ACT.command_tally(50, DAY1)) == 64,
      sorted(len(k) for k, _ in ACT.command_tally(50, DAY1)))

before = ACT.total_commands(DAY1)
ACT.record("not-an-id", "battle", now=DAY1)
check("a bad user id cannot raise in front of every command in the bot",
      ACT.total_commands(DAY1) >= before)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. it rolls over at UTC midnight ─────────────────────────────")

ACT.reset(DAY1)
ACT.record(111, "battle", now=DAY1)
ACT.record(111, "battle", now=DAY1_LATE)
check("two minutes to midnight is still today",
      ACT.total_commands(DAY1_LATE) == 2, ACT.total_commands(DAY1_LATE))

ACT.record(222, "daily", now=DAY2)
check("one minute past is a new day", ACT.today(DAY2) == "2026-03-15")
check("...and yesterday's tally is gone, not added to",
      ACT.total_commands(DAY2) == 1, ACT.total_commands(DAY2))
check("...including yesterday's players",
      ACT.user_commands(111, DAY2) == {}, ACT.user_commands(111, DAY2))
check("a READ alone rolls the day over — the panel must not be shown "
      "yesterday because nobody has run anything yet today",
      ACT.snapshot(DAY2)["day"] == "2026-03-15")


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 4. it cannot grow without bound ──────────────────────────────")

ACT.reset(DAY1)
for i in range(ACT.MAX_USERS + 250):
    ACT.record(1000 + i, "battle", now=DAY1)
check(f"at most {ACT.MAX_USERS} players are tracked by name",
      ACT.active_users(DAY1) <= ACT.MAX_USERS, ACT.active_users(DAY1))
check("...but every run past the cap still counts towards the total",
      ACT.total_commands(DAY1) == ACT.MAX_USERS + 250,
      ACT.total_commands(DAY1))
check("the overflow bucket is not reported as a player",
      all(uid for uid, _ in ACT.top_users(50, DAY1)))

ACT.reset(DAY1)
for i in range(ACT.MAX_COMMANDS + 100):
    ACT.record(111, f"cmd{i}", now=DAY1)
check(f"at most {ACT.MAX_COMMANDS} distinct commands are named",
      len(ACT._totals) <= ACT.MAX_COMMANDS + 1, len(ACT._totals))
check("...and the rest are pooled, not dropped",
      ACT.total_commands(DAY1) == ACT.MAX_COMMANDS + 100,
      ACT.total_commands(DAY1))
check("one player cannot hold unbounded command names either",
      len(ACT.user_commands(111, DAY1)) <= ACT.MAX_COMMANDS_PER_USER + 1,
      len(ACT.user_commands(111, DAY1)))
check("...and that player's own total is still right",
      sum(ACT.user_commands(111, DAY1).values()) == ACT.MAX_COMMANDS + 100,
      sum(ACT.user_commands(111, DAY1).values()))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5. a restart keeps today and refuses yesterday ───────────────")

tmp = os.path.join(tempfile.mkdtemp(), "activity.json")

ACT.reset(DAY1)
ACT.record(111, "battle", now=DAY1)
ACT.record(222, "daily", now=DAY1)
check("a flush writes a file", ACT.flush(tmp, DAY1) and os.path.exists(tmp))

ACT.reset(DAY1)
check("...the tracker really was empty before the load",
      ACT.total_commands(DAY1) == 0)
check("a reload restores today", ACT.load(tmp, DAY1) is True)
check("...with the same numbers", ACT.total_commands(DAY1) == 2,
      ACT.total_commands(DAY1))
check("...and the same per-player breakdown",
      ACT.user_commands(111, DAY1) == {"battle": 1},
      ACT.user_commands(111, DAY1))

check("a file from another day is refused, not merged",
      ACT.load(tmp, DAY2) is False)
check("...leaving today empty rather than wrong",
      ACT.total_commands(DAY2) == 0, ACT.total_commands(DAY2))

with open(tmp, "w", encoding="utf-8") as f:
    f.write("{not json at all")
check("a corrupt file does not take the bot down", ACT.load(tmp, DAY1) is False)
check("...and leaves a usable tracker behind", ACT.total_commands(DAY1) == 0)
ACT.record(111, "battle", now=DAY1)
check("...that still counts", ACT.total_commands(DAY1) == 1)

check("a missing file is not an error", ACT.load(tmp + ".nope", DAY1) is False)

ACT.reset(DAY1)
check("nothing to write means nothing is due", ACT.due_for_flush(DAY1) is False)
ACT.record(111, "battle", now=DAY1)
check("a write is due once there is something to write and the interval has "
      "passed", ACT.due_for_flush(DAY1 + ACT.FLUSH_SECONDS + 1) is True)
check("...but not one second after the last one",
      ACT.due_for_flush(DAY1) is False)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5b. per server, today and lifetime ───────────────────────────")
# "Top 10 players who use the bot, for each server" could not be answered at
# all before v1.19: `record()` took a user and a command and threw the guild
# away, even though both listeners in app.py had it in hand.

ACT.reset_all(DAY1)
for _ in range(5):
    ACT.record(111, "battle", guild_id=100, now=DAY1)
for _ in range(2):
    ACT.record(222, "daily", guild_id=100, now=DAY1)
ACT.record(333, "bal", guild_id=200, now=DAY1)
ACT.record(444, "bal", now=DAY1)                      # a DM — no server

check("both servers are known", ACT.guilds_seen(DAY1) == [100, 200],
      ACT.guilds_seen(DAY1))
check("...busiest first", ACT.guilds_seen(DAY1)[0] == 100)
check("a server's totals count only its own runs",
      ACT.guild_totals(100, DAY1) == (7, 7), ACT.guild_totals(100, DAY1))
check("...and the other server is unaffected",
      ACT.guild_totals(200, DAY1) == (1, 1), ACT.guild_totals(200, DAY1))
check("the top ten is per server, busiest first",
      [uid for uid, _d, _l in ACT.top_in_guild(100, 10, DAY1)] == [111, 222],
      ACT.top_in_guild(100, 10, DAY1))
check("...carrying today AND lifetime for each player",
      ACT.top_in_guild(100, 10, DAY1)[0] == (111, 5, 5),
      ACT.top_in_guild(100, 10, DAY1)[0])
check("a DM has no server to attribute to, so it appears in no breakdown — "
      "which is the truth rather than a guess",
      444 not in [u for g in ACT.guilds_seen(DAY1)
                  for u, _d, _l in ACT.top_in_guild(g, 10 ** 6, DAY1)])
check("...but it still counts bot-wide", ACT.total_commands(DAY1) == 9,
      ACT.total_commands(DAY1))


print("\n── 5c. lifetime outlives the day, and the restart ───────────────")
# The one place the "a file from another day is discarded" rule must NOT
# apply. Getting this backwards would silently reset every server's running
# total on the first restart after midnight — and look completely normal.

ACT.reset_all(DAY1)
for _ in range(4):
    ACT.record(111, "battle", guild_id=100, now=DAY1)
check("day one: four today, four ever", ACT.guild_totals(100, DAY1) == (4, 4))

ACT.record(111, "battle", guild_id=100, now=DAY2)
check("across midnight, today resets but the lifetime keeps counting",
      ACT.guild_totals(100, DAY2) == (1, 5), ACT.guild_totals(100, DAY2))

tmp2 = os.path.join(tempfile.mkdtemp(), "activity.json")
ACT.reset_all(DAY1)
for _ in range(4):
    ACT.record(111, "battle", guild_id=100, now=DAY1)
ACT.flush(tmp2, DAY1)

ACT.reset_all(DAY2)
check("a file from yesterday still does not restore today",
      ACT.load(tmp2, DAY2) is False)
check("...but the lifetime tally comes back, because it is not a daily number",
      ACT.guild_totals(100, DAY2) == (0, 4), ACT.guild_totals(100, DAY2))

ACT.reset_all(DAY1)
ACT.flush(tmp2, DAY1) if False else None
ACT.reset_all(DAY1)
for _ in range(4):
    ACT.record(111, "battle", guild_id=100, now=DAY1)
ACT.flush(tmp2, DAY1)
ACT.reset_all(DAY1)
check("a same-day restart restores both", ACT.load(tmp2, DAY1) is True
      and ACT.guild_totals(100, DAY1) == (4, 4),
      ACT.guild_totals(100, DAY1))


print("\n── 5d. the per-server tally is bounded too ──────────────────────")

ACT.reset_all(DAY1)
for i in range(ACT.MAX_GUILDS + 50):
    ACT.record(111, "battle", guild_id=1000 + i, now=DAY1)
check(f"at most {ACT.MAX_GUILDS} servers are tracked by name",
      len(ACT.guilds_seen(DAY1)) <= ACT.MAX_GUILDS, len(ACT.guilds_seen(DAY1)))
check("...and the overflow bucket is not reported as a server",
      all(g for g in ACT.guilds_seen(DAY1)))

ACT.reset_all(DAY1)
for i in range(ACT.MAX_USERS_PER_GUILD + 60):
    ACT.record(2000 + i, "battle", guild_id=100, now=DAY1)
check(f"at most {ACT.MAX_USERS_PER_GUILD} players per server are named",
      len(ACT.top_in_guild(100, 10 ** 6, DAY1)) <= ACT.MAX_USERS_PER_GUILD,
      len(ACT.top_in_guild(100, 10 ** 6, DAY1)))
check("...but every run past the cap still counts in the server's total",
      ACT.guild_totals(100, DAY1)[1] == ACT.MAX_USERS_PER_GUILD + 60,
      ACT.guild_totals(100, DAY1))

check("`now` and `guild_id` are keyword-only — `now` used to be the third "
      "positional, and a guild id silently landing there would be a wrong "
      "number rather than an error",
      _raises_typeerror(lambda: ACT.record(1, "x", 12345)))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5e. activity means PLAYING, not talking ──────────────────────")
# The reported bug: the audit said people were active when all they had done
# was chat. `chat_xp.py` pays 50-90 XP on every message, every payout wrote the
# profile, and `put_one` stamps `last_seen` on every write — which is the field
# every "active players" number counts. Nobody intended chatting to register
# as playing; it came in through the write path.

import inspect                                          # noqa: E402
import cogs.economy.chat_xp as CHAT                      # noqa: E402
from utils import database as DB                         # noqa: E402

chat_src = inspect.getsource(CHAT)
check("chat XP grants XP without marking the player as having used the bot",
      "touch=False" in chat_src, chat_src.count("touch=False"))
check("...on both writes — the trainer grant AND the bey grant",
      chat_src.count("touch=False") >= 2, chat_src.count("touch=False"))
check("the write path can express that at all",
      "touch" in inspect.signature(DB.update_user).parameters
      and "touch" in inspect.signature(DB.grant_xp).parameters)
_touch = inspect.signature(DB.update_user).parameters.get("touch")
check("...and still stamps by default, so a real command marks you active",
      _touch is not None and _touch.default is True, _touch)

# Claiming a wild blade is a BUTTON. A button is a component interaction and
# fires neither `on_command_completion` nor `on_app_command_completion`, so
# catching a blade — one of the things "who played today" is most obviously
# about — was invisible to the tracker entirely.
import cogs.spawn.spawn as SPAWN                         # noqa: E402

spawn_src = inspect.getsource(SPAWN)
check("claiming a blade records activity, because the button that does it "
      "fires no command-completion event",
      "activity.record(user.id, via" in spawn_src)
check("...and `;claim` passes via=None, so typing the command is not counted "
      "twice for the same catch", "via=None" in spawn_src)
check("the claim path takes the flag",
      "via" in inspect.signature(SPAWN.SpawnCog._finish_claim).parameters,
      list(inspect.signature(SPAWN.SpawnCog._finish_claim).parameters))


print("\n── 5f. the trainer level cap ────────────────────────────────────")

from utils import trainer_levels as TL                   # noqa: E402
from utils import profile_card as PC                     # noqa: E402

check("the cap is 9,999", TL.MAX_LEVEL == 9999, TL.MAX_LEVEL)
check("the curve is unchanged, so nobody re-levels — 164,820 XP is still "
      "level 57, exactly as it is in the live registry",
      TL.level_from_xp(164820) == 57, TL.level_from_xp(164820))
check("level 100 still costs 500,000 XP", TL.xp_for_level(100) == 500_000)
check("the cap really binds", TL.level_from_xp(10 ** 15) == 9999)
check("negative XP does not raise", TL.level_from_xp(-5) == 0)

check("the database re-exports the same cap, not a second copy",
      DB.MAX_LEVEL is TL.MAX_LEVEL)
check("the profile card reads the same curve — it used to carry its own cap "
      "and its own arithmetic, which is two answers to 'what level is this "
      "player' with one of them printed on the card",
      PC.MAX_LEVEL == TL.MAX_LEVEL and PC._level_from_xp(164820)[0] == 57,
      (PC.MAX_LEVEL, PC._level_from_xp(164820)))
check("...at every boundary, not just one",
      all(PC._level_from_xp(x)[0] == TL.level_from_xp(x)
          for x in (0, 49, 50, 199, 200, 499_999, 500_000, 10 ** 9)))

# The consequence a cap raise would otherwise have had — and the reason it can
# no longer have it at all. Trainer level fed a flat stat multiplier until
# v1.23 (+2% per 10 levels, capped at +20%); unbounded, `(level // 10) * 0.02`
# at a 9,999 cap is +1998%, and one player would end every battle in the game
# on the first hit. The cap is gone because the bonus is gone.
DB.get_user = lambda uid: {"level": uid}
check("trainer level no longer multiplies any stat, at any level",
      all(DB.get_stat_multiplier(lv) == 1.0 for lv in (1, 10, 50, 100, 9999)),
      [DB.get_stat_multiplier(lv) for lv in (1, 10, 50, 100, 9999)])
check("...and the two constants that drove it are gone, not zeroed",
      not hasattr(DB, "STAT_BONUS_PER_10") and not hasattr(DB, "STAT_BONUS_MAX"))
check("the level cap is therefore free to be anything",
      TL.MAX_LEVEL == 9999)

# What a level is worth now.
check("level 2 pays 200, level 3 pays 300 — the level number x 100",
      [TL.level_reward(n) for n in (1, 2, 3, 10, 100)]
      == [100, 200, 300, 1000, 10000],
      [TL.level_reward(n) for n in (1, 2, 3, 10, 100)])
check("level 0 pays nothing — a new profile starts there",
      TL.level_reward(0) == 0 and TL.level_reward(-5) == 0)
check("a multi-level jump pays each level it crossed, not a flat rate",
      TL.level_up_payout(3, 6) == 400 + 500 + 600,
      TL.level_up_payout(3, 6))
check("...so a big XP drop is worth exactly what the slow climb was",
      TL.level_up_payout(0, 20)
      == sum(TL.level_up_payout(n, n + 1) for n in range(20)))
check("no level movement pays nothing",
      TL.level_up_payout(5, 5) == 0 and TL.level_up_payout(5, 3) == 0)
check("the payout is one coin per XP — the reward curve is the XP curve's "
      "derivative, so no level is a better deal than any other",
      all(abs(TL.level_reward(n)
              - (TL.xp_for_level(n) - TL.xp_for_level(n - 1))) <= 50
          for n in (2, 10, 100, 5000)))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5g. the coins are actually paid ──────────────────────────────")
#
# A reward table nobody calls is worth nothing, and that is not hypothetical
# here: the coins used to be paid by `cogs/ui/level_up.py`, listening for an
# event dispatched only by `cogs.economy.profile.award_xp` — a function with no
# callers anywhere in the bot. So the whole reward had never once fired. These
# checks run the REAL `grant_xp` against an in-memory store and read the
# balance back.

_orig_get_user, _orig_store = DB.get_user, DB.USER_STORE


class _MemStore:
    def __init__(self):
        self.data = {}

    def get_one(self, uid):
        v = self.data.get(str(uid))
        return dict(v) if v is not None else None

    def put_one(self, uid, prof, touch=True):
        self.data[str(uid)] = dict(prof)

    def has(self, uid):
        return str(uid) in self.data


DB.USER_STORE = _MemStore()
DB.get_user = _orig_get_user
UID = 700000000000000001
DB.USER_STORE.put_one(UID, {"user_id": str(UID), "xp": 0, "level": 0,
                            "coins": 0, "quests": {}})

lvl, xp, up = DB.grant_xp(UID, TL.xp_for_level(1), boostable=False)
check("one grant to level 1 pays 100",
      (lvl, DB.USER_STORE.get_one(UID)["coins"]) == (1, 100),
      (lvl, DB.USER_STORE.get_one(UID)["coins"]))
check("...and reports the level-up", up is True)

DB.grant_xp(UID, TL.xp_for_level(2) - TL.xp_for_level(1), boostable=False)
check("the next level pays 200, not another 100",
      DB.USER_STORE.get_one(UID)["coins"] == 300,
      DB.USER_STORE.get_one(UID)["coins"])

before = DB.USER_STORE.get_one(UID)["coins"]
_l, _x, up2 = DB.grant_xp(UID, 1, boostable=False)
check("XP that crosses no level pays nothing",
      DB.USER_STORE.get_one(UID)["coins"] == before and up2 is False)

DB.USER_STORE.put_one(UID, {"user_id": str(UID), "xp": 0, "level": 0,
                            "coins": 0, "quests": {}})
DB.grant_xp(UID, TL.xp_for_level(6), boostable=False)
check("a single grant that jumps 0 to 6 pays all six levels",
      DB.USER_STORE.get_one(UID)["coins"] == TL.level_up_payout(0, 6) == 2100,
      DB.USER_STORE.get_one(UID)["coins"])

# The stale-`level`-field trap: pay from the XP, never from the stored number.
DB.USER_STORE.put_one(UID, {"user_id": str(UID), "xp": TL.xp_for_level(5),
                            "level": 0, "coins": 0, "quests": {}})
DB.grant_xp(UID, 0, boostable=False)
check("a profile whose stored `level` disagrees with its XP is not re-paid "
      "for levels it already has",
      DB.USER_STORE.get_one(UID)["coins"] == 0,
      DB.USER_STORE.get_one(UID)["coins"])

check("level_up.py no longer writes coins of its own — two payers would "
      "double every level",
      "update_user" not in open(
          os.path.join(os.path.dirname(os.path.dirname(
              os.path.abspath(__file__))), "cogs", "ui", "level_up.py"),
          encoding="utf-8").read())

DB.get_user, DB.USER_STORE = _orig_get_user, _orig_store


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6. it is wired to something that fires ───────────────────────")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def code(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


app_src = code("app.py")
check("a prefix listener records completions",
      "async def on_command_completion" in app_src)
check("...and a slash one, because a panel's ctx.invoke never dispatches "
      "command_completion", "async def on_app_command_completion" in app_src)
check("completion, not invocation — a refused command is not a use",
      "on_command_error" in app_src and "on_command_invoke" not in app_src)
check("the disk write is off the event loop",
      "asyncio.to_thread(activity.flush)" in app_src)
check("today's tally survives a restart within the day",
      "activity" in app_src and "_activity.load" in app_src)

import cogs.admin.actions as ADMIN                      # noqa: E402

check("the report takes a server filter", 
      "server" in ADMIN.REGISTRY["audit_activity"].needs,
      ADMIN.REGISTRY["audit_activity"].needs)
check("...which is optional — 'all servers' is a real answer, so the action "
      "never refuses to run for want of a selection",
      not ADMIN.missing_params(
          ADMIN.REGISTRY["audit_activity"],
          ADMIN.ActionCtx(bot=None, invoker_id=1)))
check("the listeners pass the guild they already have",
      "guild_id=guild_id" in app_src and "interaction.guild_id" in app_src)
check("the admin panel can show it",
      "audit_activity" in ADMIN.REGISTRY and "audit_who" in ADMIN.REGISTRY,
      sorted(k for k in ADMIN.REGISTRY if "activ" in k or k == "audit_who"))
check("...under Audit, where the other reports live",
      ADMIN.REGISTRY["audit_activity"].category == "audit")

from utils.database import USER_STORE                   # noqa: E402

check("the store can name who was seen, not merely count them",
      hasattr(USER_STORE, "active_users_since"))
check("...and the count it already had is still there",
      hasattr(USER_STORE, "active_since"))


print("\n" + "=" * 66)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 66)
sys.exit(1 if FAIL else 0)
