"""
cogs/snapshots/clock.py — "has it been a day?", answered across restarts.

`@tasks.loop(hours=24)` is the obvious way to do a daily job and it is wrong
here: the loop's clock starts when the process starts, so a bot that restarts
every few hours would take a backup **never**. The bug is invisible — nothing
errors, there simply are no files — which is the worst kind for a backup.

So the loop ticks hourly and asks this module, which compares against a
timestamp in `community_config`: a real table, on whichever backend is live,
so the answer survives both a restart and a container rebuild.

This is the same lesson as the community recovery sweep that only ran once per
process; it is written as its own module so the suite can wind the clock
without a Discord connection.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger("beyblade_bot.snapshot.clock")

KEY = "last_snapshot_at"
EVERY = 24 * 3600.0


def _store():
    from utils import database as DB
    return DB.USER_STORE


def last_run() -> float:
    try:
        raw = _store().community_config_get(KEY)
        return float(raw) if raw else 0.0
    except Exception:                                    # noqa: BLE001
        log.exception("[snapshot] could not read the last-run stamp")
        return 0.0


def mark(now: Optional[float] = None) -> float:
    ts = time.time() if now is None else float(now)
    try:
        _store().community_config_put(KEY, str(ts))
    except Exception:                                    # noqa: BLE001
        log.exception("[snapshot] could not record the last-run stamp")
    return ts


def due(now: Optional[float] = None, every: float = EVERY) -> bool:
    """True when a snapshot is owed. Never raises.

    A store that has never run has `last_run() == 0`, so the first tick after
    deploy takes one immediately rather than waiting a day for the first copy.
    """
    ts = time.time() if now is None else float(now)
    return (ts - last_run()) >= float(every)


def next_due(now: Optional[float] = None, every: float = EVERY) -> float:
    ts = time.time() if now is None else float(now)
    return max(0.0, (last_run() + float(every)) - ts)
