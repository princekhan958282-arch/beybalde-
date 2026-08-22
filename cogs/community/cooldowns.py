"""
cogs/community/cooldowns.py — the one cooldown helper for this package.

The codebase has four separate cooldown patterns already (an in-memory dict in
the boss cog, an ISO timestamp on the profile for `;daily`, a JSON side-file in
raid, a table read in reports) and discord.py's own `commands.cooldown` is used
nowhere. Rather than add a fifth shape per manager, everything here goes through
these two.

Which to use is a durability question, not a taste one:

* `Bucket` — in memory. For things where a restart resetting the timer costs
  nothing: banter frequency, per-channel chatter.
* `profile_gate` / `day_counter` — written to the player's profile. For things
  where a restart resetting the timer is an exploit: XP, roasts, submissions.
  Modelled on the UTC-day pair counter in `utils/ranked.py`, including its
  self-pruning, so old days do not accumulate on the profile forever.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional


def utc_day(now: Optional[float] = None) -> str:
    ts = time.time() if now is None else now
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class Bucket:
    """Per-key cooldown held in memory, with bounded size."""

    def __init__(self, seconds: float, *, max_keys: int = 20_000) -> None:
        self.seconds = float(seconds)
        self.max_keys = int(max_keys)
        self._at: dict = {}
        self._lock = threading.Lock()

    def ready(self, key, now: Optional[float] = None) -> bool:
        ts = time.time() if now is None else now
        with self._lock:
            last = self._at.get(key)
        return last is None or (ts - last) >= self.seconds

    def remaining(self, key, now: Optional[float] = None) -> float:
        ts = time.time() if now is None else now
        with self._lock:
            last = self._at.get(key)
        if last is None:
            return 0.0
        return max(0.0, self.seconds - (ts - last))

    def stamp(self, key, now: Optional[float] = None) -> None:
        ts = time.time() if now is None else now
        with self._lock:
            self._at[key] = ts
            if len(self._at) > self.max_keys:
                # Drop the oldest half rather than growing without bound. A
                # cooldown that forgets an idle player is harmless; one that
                # eats the heap is not.
                cutoff = sorted(self._at.values())[len(self._at) // 2]
                self._at = {k: v for k, v in self._at.items() if v >= cutoff}

    def hit(self, key, now: Optional[float] = None) -> bool:
        """Ready-and-stamp in one step. True when the caller may proceed."""
        ts = time.time() if now is None else now
        with self._lock:
            last = self._at.get(key)
            if last is not None and (ts - last) < self.seconds:
                return False
            self._at[key] = ts
        return True

    def clear(self) -> None:
        with self._lock:
            self._at.clear()


def profile_gate(profile: dict, key: str, seconds: float,
                 now: Optional[float] = None) -> tuple[bool, float]:
    """`(allowed, seconds_remaining)` from a timestamp on the profile.

    Does NOT stamp — the caller stamps after the action succeeds, so a failed
    action does not burn the cooldown.
    """
    ts = time.time() if now is None else now
    try:
        last = float(profile.get(key) or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    waited = ts - last
    if waited >= float(seconds):
        return True, 0.0
    return False, float(seconds) - waited


def stamp(profile: dict, key: str, now: Optional[float] = None) -> None:
    profile[key] = time.time() if now is None else now


def day_counter(profile: dict, key: str,
                now: Optional[float] = None) -> dict:
    """Today's counters under `key`, resetting on the UTC day boundary."""
    today = utc_day(now)
    bucket = profile.get(key)
    if not isinstance(bucket, dict) or bucket.get("date") != today:
        bucket = {"date": today}
        profile[key] = bucket
    return bucket


def bump_day(profile: dict, key: str, field: str, amount: int = 1,
             now: Optional[float] = None) -> int:
    bucket = day_counter(profile, key, now)
    try:
        total = int(bucket.get(field) or 0) + int(amount)
    except (TypeError, ValueError):
        total = int(amount)
    bucket[field] = total
    return total


def day_total(profile: dict, key: str, field: str,
              now: Optional[float] = None) -> int:
    bucket = day_counter(profile, key, now)
    try:
        return int(bucket.get(field) or 0)
    except (TypeError, ValueError):
        return 0
