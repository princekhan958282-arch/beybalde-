"""
activity.py — who used the bot today, and what they ran.

What counts as activity
-----------------------
Playing: running a command, claiming a wild blade, battling. **Not chatting.**

That distinction had to be made twice, because it leaked in two directions.
Chat XP pays out on every message and every payout wrote the profile, and a
profile write stamps `last_seen` — so anyone who merely talked was counted as
somebody who uses the bot (`chat_xp.py` now writes with `touch=False`). And
claiming a wild blade is a *button*, which fires neither completion event, so
the single most obvious thing a player does was invisible until `_finish_claim`
started recording it directly.

Nothing in this bot recorded what commands people actually use. `last_seen` in
the SQLite store answers "was this player around", which is one third of the
question; "what are they doing" and "which features are dead" had no answer at
all.

Why in memory with a periodic flush, and not a row per invocation
-----------------------------------------------------------------
This bot deploys by extracting a zip over a live install and serves ~3,400
profiles from JSON. A write per command is the exact shape that froze the event
loop in `;giveallcoins`: thousands of small writes on the loop's thread. So a
command costs one dict increment here, and the file is written at most once
every `FLUSH_SECONDS` — from a worker thread, by whoever owns the listener.

Why the numbers are bounded
---------------------------
A counter keyed by user id and command name grows with the player base, and a
raid of unknown commands would grow it with no player base at all. Both keys
are capped; past the cap the counts still land, in `OVERFLOW_KEY`, so the
totals stay honest even when the names stop being individual. A tracker that
silently drops what it cannot store would report a quiet day during the busiest
one.

The day is UTC, matching `utils/ranked.py`'s daily pair tally, so "today" means
the same thing everywhere in the bot.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("beyblade_bot.activity")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTIVITY_PATH = os.path.join(BASE_DIR, "data", "activity.json")

# How often the caller is told it is worth writing the file. A crash loses at
# most this much of one day's tally, which is an acceptable price for a stat
# panel and not for anything a player owns — nothing in here is.
FLUSH_SECONDS = 120

MAX_USERS = 5000
MAX_COMMANDS = 400
# Distinct commands recorded per user before the rest of theirs are pooled.
MAX_COMMANDS_PER_USER = 60
# Servers tracked by name, and players tracked by name within each.
MAX_GUILDS = 200
MAX_USERS_PER_GUILD = 2000

OVERFLOW_KEY = "…other"
# Where counts land once a cap is hit. Zero is not a valid Discord id, so it
# can never collide with a real user or guild — and the totals stay honest
# even when the names stop being individual.
OVERFLOW_ID = 0

_day: str = ""
_counts: dict[int, dict[str, int]] = {}
_totals: dict[str, int] = {}
# guild_id -> {user_id: commands run TODAY in that server}
_guild_day: dict[int, dict[int, int]] = {}
# guild_id -> {user_id: commands run EVER in that server}
#
# Deliberately outside the daily reset. "Who uses this bot" is not a question
# about today, and a tracker that forgot every midnight would answer it with
# noise. It is the same dict, in the same file, on the same flush — the cost of
# keeping it is one more key.
_guild_life: dict[int, dict[int, int]] = {}
_dirty: bool = False
_last_flush: float = 0.0


def today(now: Optional[float] = None) -> str:
    ts = time.time() if now is None else float(now)
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def reset(now: Optional[float] = None) -> None:
    """Drop TODAY and start a fresh day. Used at rollover and by tests.

    The lifetime per-server tally deliberately survives. Clearing it here would
    make "commands ever" mean "commands since the last midnight the bot happened
    to be running through", which is a worse number than not having one.
    """
    global _day, _counts, _totals, _guild_day, _dirty
    _day = today(now)
    _counts = {}
    _totals = {}
    _guild_day = {}
    _dirty = False


def reset_all(now: Optional[float] = None) -> None:
    """Everything, lifetime included. Tests only."""
    global _guild_life
    reset(now)
    _guild_life = {}


def _rollover(now: Optional[float] = None) -> None:
    if today(now) != _day:
        reset(now)


def _bump(bucket: dict, key, cap: int) -> None:
    """Add one to `bucket[key]`, pooling into OVERFLOW_ID past `cap`."""
    if key not in bucket and len(bucket) >= cap:
        key = OVERFLOW_ID
    bucket[key] = bucket.get(key, 0) + 1


def record(user_id, command: str, *, guild_id=None,
           now: Optional[float] = None) -> None:
    """Count one command run by one player, optionally in one server.

    `guild_id` and `now` are keyword-only deliberately. `now` used to be the
    third positional argument; adding `guild_id` in front of it would have made
    every existing `record(uid, cmd, some_time)` silently record a guild id of
    1.7 billion and no timestamp. Forcing the keyword turns that into a
    TypeError at the call site instead of a wrong number in a panel.

    Never raises. This runs from an event listener in front of every command in
    the bot, which is the worst possible place for an exception: it would turn
    a bookkeeping bug into a broken bot. The whole body is guarded and a failure
    costs one missing tally.

    `guild_id` is None in DMs, where there is no server to attribute the run to.
    Those still count towards the bot-wide numbers and simply do not appear in
    any per-server breakdown — which is the truth, rather than a guess.
    """
    global _dirty
    try:
        _rollover(now)
        uid = int(user_id)
        name = str(command or "").strip().lower()[:64]
        if not name:
            return

        if guild_id is not None:
            gid = int(guild_id)
            for store, cap in ((_guild_day, MAX_GUILDS),
                               (_guild_life, MAX_GUILDS)):
                if gid not in store and len(store) >= cap:
                    bucket = store.setdefault(OVERFLOW_ID, {})
                else:
                    bucket = store.setdefault(gid, {})
                _bump(bucket, uid, MAX_USERS_PER_GUILD)

        if name in _totals or len(_totals) < MAX_COMMANDS:
            _totals[name] = _totals.get(name, 0) + 1
        else:
            _totals[OVERFLOW_KEY] = _totals.get(OVERFLOW_KEY, 0) + 1

        per = _counts.get(uid)
        if per is None:
            if len(_counts) >= MAX_USERS:
                # The user cap is reached, so this player is not tracked by
                # name — but the run still counts towards the day's total.
                per = _counts.setdefault(0, {})
            else:
                per = _counts.setdefault(uid, {})
        if name in per or len(per) < MAX_COMMANDS_PER_USER:
            per[name] = per.get(name, 0) + 1
        else:
            per[OVERFLOW_KEY] = per.get(OVERFLOW_KEY, 0) + 1
        _dirty = True
    except Exception as exc:                             # noqa: BLE001
        log.debug("activity record failed: %s", exc)


# ── reading ──────────────────────────────────────────────────────────────────

def active_users(now: Optional[float] = None) -> int:
    _rollover(now)
    return sum(1 for uid in _counts if uid)


def total_commands(now: Optional[float] = None) -> int:
    _rollover(now)
    return sum(_totals.values())


def user_commands(user_id, now: Optional[float] = None) -> dict[str, int]:
    _rollover(now)
    return dict(_counts.get(int(user_id), {}))


def top_users(limit: int = 10, now: Optional[float] = None) -> list[tuple[int, int]]:
    """[(user_id, commands run)], busiest first. The overflow bucket is not a
    player, so id 0 never appears."""
    _rollover(now)
    rows = [(uid, sum(per.values())) for uid, per in _counts.items() if uid]
    rows.sort(key=lambda r: (r[1], r[0]), reverse=True)
    return rows[:max(1, int(limit))]


def command_tally(limit: int = 15,
                  now: Optional[float] = None) -> list[tuple[str, int]]:
    _rollover(now)
    rows = sorted(_totals.items(), key=lambda r: (r[1], r[0]), reverse=True)
    return rows[:max(1, int(limit))]


def guilds_seen(now: Optional[float] = None) -> list[int]:
    """Every server with a lifetime tally, busiest first. Never the overflow."""
    _rollover(now)
    rows = [(gid, sum(per.values()))
            for gid, per in _guild_life.items() if gid]
    rows.sort(key=lambda r: (r[1], r[0]), reverse=True)
    return [gid for gid, _n in rows]


def guild_totals(guild_id, now: Optional[float] = None) -> tuple[int, int]:
    """`(commands today, commands ever)` for one server."""
    _rollover(now)
    gid = int(guild_id)
    return (sum(_guild_day.get(gid, {}).values()),
            sum(_guild_life.get(gid, {}).values()))


def top_in_guild(guild_id, limit: int = 10,
                 now: Optional[float] = None) -> list[tuple[int, int, int]]:
    """`[(user_id, today, lifetime)]` for one server, busiest first.

    Ranked on the lifetime total, because "who uses the bot here" is not a
    question about the last few hours — a player who ran fifty commands
    yesterday and none since is still one of this server's regulars.
    """
    _rollover(now)
    gid = int(guild_id)
    life = _guild_life.get(gid, {})
    day = _guild_day.get(gid, {})
    rows = [(uid, day.get(uid, 0), n) for uid, n in life.items() if uid]
    rows.sort(key=lambda r: (r[2], r[1], r[0]), reverse=True)
    return rows[:max(1, int(limit))]


def snapshot(now: Optional[float] = None) -> dict:
    _rollover(now)
    return {
        "day": _day,
        "active_users": active_users(now),
        "total_commands": total_commands(now),
        "distinct_commands": len(_totals),
        "top_users": top_users(10, now),
        "commands": command_tally(15, now),
    }


# ── persistence ──────────────────────────────────────────────────────────────

def due_for_flush(now: Optional[float] = None) -> bool:
    ts = time.time() if now is None else float(now)
    return _dirty and (ts - _last_flush) >= FLUSH_SECONDS


def flush(path: str = ACTIVITY_PATH, now: Optional[float] = None) -> bool:
    """Write the day's tally. Returns True if a file was written.

    Blocking. Call it from a worker thread — the listener that feeds this
    module runs on the event loop, and this is the only part of the module
    that touches a disk.
    """
    global _dirty, _last_flush
    _rollover(now)
    payload = {
        "day": _day,
        "counts": {str(uid): per for uid, per in _counts.items()},
        "totals": dict(_totals),
        "guild_day": {str(g): {str(u): n for u, n in per.items()}
                      for g, per in _guild_day.items()},
        "guild_life": {str(g): {str(u): n for u, n in per.items()}
                       for g, per in _guild_life.items()},
        "written_at": time.time() if now is None else float(now),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as exc:                             # noqa: BLE001
        log.warning("activity flush failed: %s", exc)
        return False
    _dirty = False
    _last_flush = time.time() if now is None else float(now)
    return True


def _nested_ints(raw) -> dict[int, dict[int, int]]:
    out: dict[int, dict[int, int]] = {}
    for outer, per in (raw or {}).items():
        if not isinstance(per, dict):
            continue
        try:
            key = int(outer)
        except (TypeError, ValueError):
            continue
        inner: dict[int, int] = {}
        for k, v in per.items():
            try:
                inner[int(k)] = int(v)
            except (TypeError, ValueError):
                continue
        out[key] = inner
    return out


def load(path: str = ACTIVITY_PATH, now: Optional[float] = None) -> bool:
    """Restore after a restart. Returns True if TODAY's tally loaded.

    Two different rules in one function, on purpose:

    * the daily numbers are only restored from a file written the same UTC day
      — the panel says "today", and a restart at 00:05 must not answer with
      yesterday;
    * the **lifetime** per-server tally is restored whatever day the file is
      from, because that is what makes it a lifetime tally rather than a very
      long day. This is the one place the "wrong day, throw it away" rule must
      not apply, and getting it backwards would silently reset every server's
      running total on the first restart after midnight.
    """
    global _day, _counts, _totals, _guild_day, _guild_life, _dirty
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        reset(now)
        return False
    except Exception as exc:                             # noqa: BLE001
        log.warning("activity load failed: %s", exc)
        reset(now)
        return False

    if not isinstance(data, dict):
        reset(now)
        return False

    # Lifetime first, and unconditionally — see the docstring.
    _guild_life = _nested_ints(data.get("guild_life"))

    if data.get("day") != today(now):
        reset(now)
        return False

    counts: dict[int, dict[str, int]] = {}
    for raw_uid, per in (data.get("counts") or {}).items():
        if not isinstance(per, dict):
            continue
        try:
            uid = int(raw_uid)
        except (TypeError, ValueError):
            continue
        counts[uid] = {str(k): int(v) for k, v in per.items()
                       if isinstance(v, (int, float))}
    _day = today(now)
    _counts = counts
    _totals = {str(k): int(v) for k, v in (data.get("totals") or {}).items()
               if isinstance(v, (int, float))}
    _guild_day = _nested_ints(data.get("guild_day"))
    _dirty = False
    return True


reset_all()
