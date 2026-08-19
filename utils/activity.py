"""
activity.py — who used the bot today, and what they ran.

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

OVERFLOW_KEY = "…other"

_day: str = ""
_counts: dict[int, dict[str, int]] = {}
_totals: dict[str, int] = {}
_dirty: bool = False
_last_flush: float = 0.0


def today(now: Optional[float] = None) -> str:
    ts = time.time() if now is None else float(now)
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def reset(now: Optional[float] = None) -> None:
    """Drop everything and start a fresh day. Used at rollover and by tests."""
    global _day, _counts, _totals, _dirty
    _day = today(now)
    _counts = {}
    _totals = {}
    _dirty = False


def _rollover(now: Optional[float] = None) -> None:
    if today(now) != _day:
        reset(now)


def record(user_id, command: str, now: Optional[float] = None) -> None:
    """Count one command run by one player. Never raises.

    Called from an event listener in front of every command in the bot, which
    is the worst possible place for an exception: it would turn a bookkeeping
    bug into a broken bot. So the whole body is guarded and a failure costs one
    missing tally.
    """
    global _dirty
    try:
        _rollover(now)
        uid = int(user_id)
        name = str(command or "").strip().lower()[:64]
        if not name:
            return

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


def load(path: str = ACTIVITY_PATH, now: Optional[float] = None) -> bool:
    """Restore today's tally after a restart. Returns True if anything loaded.

    A file from a previous day is ignored rather than merged: the panel says
    "today", and a restart at 00:05 must not answer with yesterday.
    """
    global _day, _counts, _totals, _dirty
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

    if not isinstance(data, dict) or data.get("day") != today(now):
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
    _dirty = False
    return True


reset()
