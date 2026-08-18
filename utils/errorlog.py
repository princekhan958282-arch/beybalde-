"""
errorlog.py — the last N exceptions, where an admin can actually read them.

Why this exists
---------------
This bot raises exceptions in 21 places that are logged and nowhere else. There
is no `logs/` directory, nothing is written to disk, and nothing surfaces in
Discord. Every incident in this project so far was found by a player reporting
it, not by the bot saying anything:

  * a boss battle froze mid-fight for weeks, because `argus.special_damage`
    returned the wrong shape and the traceback went to a log nobody reads;
  * the host filled its disk, which silently broke the auto-updater and would
    have broken every PNG render, and the first sign of it was a screenshot.

Both were one `;admin errors` away from being obvious.

Why in memory and not a file
----------------------------
The failure this is most likely to be reporting is a full disk. A logger that
answers "what went wrong?" by writing to the disk that just filled up is a
logger that goes silent exactly when it matters. A ring buffer costs nothing,
survives as long as the process, and cannot itself fail.

The trade is real and worth stating: a restart loses the history. That is the
correct trade for a bot redeployed from a phone — the alternative is a log file
that grows unbounded on a host with a fixed disk allowance, which is the
problem, not the solution.
"""

from __future__ import annotations

import logging
import time
import traceback
from collections import deque
from typing import Optional

# 50 is chosen against the failure mode, not by taste: one broken interaction
# usually raises once per press, so a player mashing a dead button produces a
# handful of entries, not hundreds. Deep enough to hold a whole incident,
# shallow enough that the oldest entry is still from today.
MAX_ENTRIES = 50

_entries: deque = deque(maxlen=MAX_ENTRIES)


class _RingHandler(logging.Handler):
    """Captures anything logged at ERROR or above that carries an exception."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.ERROR:
                return
            exc = ""
            if record.exc_info:
                exc = "".join(traceback.format_exception(*record.exc_info))
            _entries.append({
                "when":   time.time(),
                "logger": record.name,
                "msg":    record.getMessage()[:400],
                "trace":  exc[-1500:],
            })
        except Exception:                                # noqa: BLE001
            # A logging handler that raises breaks logging itself, which would
            # take down whatever was being logged about. Never propagate.
            pass


_handler: Optional[_RingHandler] = None


def install() -> None:
    """Attach to the root logger. Idempotent — safe to call on every boot."""
    global _handler
    if _handler is not None:
        return
    _handler = _RingHandler()
    _handler.setLevel(logging.ERROR)
    logging.getLogger().addHandler(_handler)


def record(where: str, exc: BaseException) -> None:
    """Record an exception directly, for code that catches without logging."""
    _entries.append({
        "when":   time.time(),
        "logger": where,
        "msg":    f"{type(exc).__name__}: {exc}"[:400],
        "trace":  "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__))[-1500:],
    })


def recent(limit: int = 10) -> list[dict]:
    """The most recent entries, newest first."""
    return list(_entries)[-limit:][::-1]


def count() -> int:
    return len(_entries)


def clear() -> None:
    _entries.clear()
