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

# Two fixed instants, one either side of a UTC midnight, so nothing here
# depends on when the suite is run.
DAY1 = datetime(2026, 3, 14, 22, 30, tzinfo=timezone.utc).timestamp()
DAY1_LATE = datetime(2026, 3, 14, 23, 59, tzinfo=timezone.utc).timestamp()
DAY2 = datetime(2026, 3, 15, 0, 1, tzinfo=timezone.utc).timestamp()


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. it counts, per player and per command ─────────────────────")

ACT.reset(DAY1)
for _ in range(3):
    ACT.record(111, "battle", DAY1)
ACT.record(111, "daily", DAY1)
ACT.record(222, "battle", DAY1)

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
ACT.record(111, "profile", DAY1)
ACT.record(111, "/player", DAY1)
check("the slash surface counts under its own name",
      ACT.user_commands(111, DAY1) == {"profile": 1, "/player": 1},
      ACT.user_commands(111, DAY1))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. names are normalised, junk is refused ─────────────────────")

ACT.reset(DAY1)
ACT.record(111, "  BATTLE  ", DAY1)
ACT.record(111, "battle", DAY1)
check("case and whitespace are one command, not three",
      ACT.user_commands(111, DAY1) == {"battle": 2},
      ACT.user_commands(111, DAY1))

ACT.record(111, "", DAY1)
ACT.record(111, "   ", DAY1)
ACT.record(111, None, DAY1)
check("an empty command name is not counted", ACT.total_commands(DAY1) == 2,
      ACT.total_commands(DAY1))

ACT.record(111, "x" * 500, DAY1)
check("a long name is clamped rather than stored whole",
      max(len(k) for k, _ in ACT.command_tally(50, DAY1)) == 64,
      sorted(len(k) for k, _ in ACT.command_tally(50, DAY1)))

before = ACT.total_commands(DAY1)
ACT.record("not-an-id", "battle", DAY1)
check("a bad user id cannot raise in front of every command in the bot",
      ACT.total_commands(DAY1) >= before)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. it rolls over at UTC midnight ─────────────────────────────")

ACT.reset(DAY1)
ACT.record(111, "battle", DAY1)
ACT.record(111, "battle", DAY1_LATE)
check("two minutes to midnight is still today",
      ACT.total_commands(DAY1_LATE) == 2, ACT.total_commands(DAY1_LATE))

ACT.record(222, "daily", DAY2)
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
    ACT.record(1000 + i, "battle", DAY1)
check(f"at most {ACT.MAX_USERS} players are tracked by name",
      ACT.active_users(DAY1) <= ACT.MAX_USERS, ACT.active_users(DAY1))
check("...but every run past the cap still counts towards the total",
      ACT.total_commands(DAY1) == ACT.MAX_USERS + 250,
      ACT.total_commands(DAY1))
check("the overflow bucket is not reported as a player",
      all(uid for uid, _ in ACT.top_users(50, DAY1)))

ACT.reset(DAY1)
for i in range(ACT.MAX_COMMANDS + 100):
    ACT.record(111, f"cmd{i}", DAY1)
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
ACT.record(111, "battle", DAY1)
ACT.record(222, "daily", DAY1)
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
ACT.record(111, "battle", DAY1)
check("...that still counts", ACT.total_commands(DAY1) == 1)

check("a missing file is not an error", ACT.load(tmp + ".nope", DAY1) is False)

ACT.reset(DAY1)
check("nothing to write means nothing is due", ACT.due_for_flush(DAY1) is False)
ACT.record(111, "battle", DAY1)
check("a write is due once there is something to write and the interval has "
      "passed", ACT.due_for_flush(DAY1 + ACT.FLUSH_SECONDS + 1) is True)
check("...but not one second after the last one",
      ACT.due_for_flush(DAY1) is False)


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
