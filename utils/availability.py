"""
utils/availability.py — is this content obtainable, and by whom?

Two independent gates, shared by blades (`data/beyblades.json`) and avatars
(`cogs/avatar/avatar_data.json`) because both needed the same answer and only
one of them had half of it written.

**Limited-time** — `limited: true` plus an optional `available_until`
(ISO-8601, or a unix timestamp). Null or absent means the window is open-ended:
flagged as limited for display, but not yet closing. This vocabulary comes from
`avatar_engine.avatar_is_available`, which implemented it correctly and was
then never called from anywhere — so no limited avatar has ever actually
expired. That function now delegates here, and the avatar pack roll finally
consults it.

**Owner-bound** — `owner_ids: [123, 456]`. Content made for specific people.
Absent means anyone; present means *only* those ids, on every acquisition
route, forever.

Both gates cover ACQUISITION only. A player who already owns a limited blade
keeps it when the window shuts — taking something out of an inventory because
a date passed is a different and much worse feature.

Two deliberate failure modes, both fail OPEN (available):

  * An unparseable `available_until` returns available. The alternative is
    that one typo in a data file silently deletes content from the game with
    no error anywhere.
  * An `owner_ids` that is not a list of ints is ignored rather than locking
    everyone out.

Deliberately discord-free and dependency-free: `utils/bey_levels.py` and the
`tools/sim_*.py` harnesses import modules like this one, and neither may pull
in discord.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger("beyblade_bot.availability")


def _as_epoch(value) -> float | None:
    """`available_until` as a unix timestamp, or None if it doesn't parse."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        text = str(value).strip().replace("Z", "+00:00")
        end = datetime.fromisoformat(text)
        if end.tzinfo is None:
            # A bare date/time in the data file means UTC, not "whatever the
            # host's clock is set to" — panels are rarely on UTC.
            end = end.replace(tzinfo=timezone.utc)
        return end.timestamp()
    except (ValueError, TypeError):
        log.warning("[availability] unparseable available_until: %r", value)
        return None


def is_limited(entry: dict) -> bool:
    """Does this carry a limited-time flag at all (window open or shut)?"""
    return bool((entry or {}).get("limited"))


def expires_at(entry: dict) -> float | None:
    """Unix timestamp this closes at, or None for open-ended / not limited."""
    if not is_limited(entry):
        return None
    return _as_epoch((entry or {}).get("available_until"))


def is_available(entry: dict, now: float | None = None) -> bool:
    """Is this obtainable right now?

    Owner-bound content is never "available" in the general sense — use
    `owned_by` for that. This answers the time question only, so a caller that
    cares about both must ask both.
    """
    end = expires_at(entry or {})
    if end is None:
        return True
    return (time.time() if now is None else float(now)) <= end


def owner_ids(entry: dict) -> list[int]:
    """The ids allowed to own this, or [] meaning anyone."""
    raw = (entry or {}).get("owner_ids")
    if not raw:
        return []
    try:
        return [int(x) for x in raw]
    except (TypeError, ValueError):
        log.warning("[availability] unusable owner_ids: %r", raw)
        return []


def is_owner_bound(entry: dict) -> bool:
    return bool(owner_ids(entry))


def owned_by(entry: dict, user_id) -> bool:
    """May this user obtain it? True for unbound content."""
    ids = owner_ids(entry)
    if not ids:
        return True
    try:
        return int(user_id) in ids
    except (TypeError, ValueError):
        return False


def obtainable(entry: dict, user_id=None, now: float | None = None) -> bool:
    """Both gates at once — the question every acquisition route is asking.

    `user_id=None` means "could anybody get this", which is what a pool
    builder with no player in hand needs; owner-bound content answers False
    there, because it must never land in a shared pool.
    """
    if not is_available(entry, now):
        return False
    if is_owner_bound(entry):
        return user_id is not None and owned_by(entry, user_id)
    return True
