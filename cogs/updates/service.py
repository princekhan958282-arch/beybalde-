"""
cogs/updates/service.py — the one entry point everything else uses.

    UPDATE_RELEASED · EVENT_STARTED · BALANCE_CHANGE · MAINTENANCE
    IMPORTANT_NOTICE

Admin panel or game system → **notify()** → the queue → the worker → a DM.

`notify()` writes the update row, resolves the audience, enqueues it, and
returns. It never sends anything itself — the worker owns delivery, so a caller
cannot accidentally block a command for the length of a broadcast, and a send
that is interrupted resumes from the ledger rather than from a lost coroutine.

It is also reachable without importing this package:

    bot.dispatch("beycord_notify", {...})

which is the same shape `beycord_battle_end` already uses, so a cog can fire a
notification without taking a dependency on the notification system.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from . import prefs as P
from . import store as S
from . import targeting as T

log = logging.getLogger("beyblade_bot.updates")

# Update lifecycle. DRAFT is composed but unsent; QUEUED has rows in the
# ledger; DONE means the worker found nothing left to do.
DRAFT = "DRAFT"
QUEUED = "QUEUED"
SENDING = "SENDING"
DONE = "DONE"
CANCELLED = "CANCELLED"

EVENTS = ("UPDATE_RELEASED", "EVENT_STARTED", "BALANCE_CHANGE",
          "MAINTENANCE", "IMPORTANT_NOTICE")


def create(*, event: str, title: str, body: str,
           priority: str = P.NORMAL,
           audience: Optional[dict] = None,
           image_url: Optional[str] = None,
           version: Optional[str] = None,
           scheduled_at: Optional[float] = None,
           created_by: Optional[int] = None,
           state: str = DRAFT) -> dict:
    """Write an update row. Nothing is queued or sent."""
    row = {
        "update_id": S.new_id("upd"),
        "version": version,
        "title": (title or "Untitled")[:250],
        "body": body or "",
        "priority": P.normalise(priority),
        "audience": audience or {"kind": T.ALL},
        "image_url": image_url or None,
        "event": str(event or "UPDATE_RELEASED").upper(),
        "created_by": str(created_by) if created_by else None,
        "created_at": time.time(),
        "scheduled_at": scheduled_at,
        "state": state,
    }
    S.put_update(row)
    return row


def queue(bot, update_id: str) -> dict:
    """Resolve the audience and put it on the queue.

    Idempotent by construction: enqueue is INSERT OR IGNORE against the
    ledger's composite key, so calling this twice — a double-clicked Send, a
    retry, a restart mid-send — adds nothing the second time and reports zero
    new rows.
    """
    upd = S.get_update(update_id)
    if not upd:
        raise LookupError(f"no such update: {update_id}")
    res = T.resolve(bot, upd.get("audience") or {})
    added = S.enqueue(update_id, res["ids"])
    S.set_update_state(update_id, QUEUED)
    log.info("[updates] %s queued: %d targeted, %d new rows, %d dropped "
             "(no blades)", update_id, len(res["ids"]), added,
             res["dropped_no_beys"])
    return {"queued": added, "targeted": len(res["ids"]),
            "dropped_no_beys": res["dropped_no_beys"],
            "total_before": res["total_before"]}


async def notify(bot, *, event: str, title: str, body: str,
                 priority: str = P.NORMAL,
                 audience: Optional[dict] = None,
                 image_url: Optional[str] = None,
                 version: Optional[str] = None,
                 scheduled_at: Optional[float] = None,
                 created_by: Optional[int] = None) -> str:
    """Compose, queue and wake the worker. Returns the update id."""
    row = create(event=event, title=title, body=body, priority=priority,
                 audience=audience, image_url=image_url, version=version,
                 scheduled_at=scheduled_at, created_by=created_by,
                 state=DRAFT)
    if not scheduled_at or scheduled_at <= time.time():
        queue(bot, row["update_id"])
        wake(bot)
    return row["update_id"]


async def notify_user(bot, user_id, *, event: str, title: str, body: str,
                      priority: str = P.NORMAL,
                      image_url: Optional[str] = None) -> str:
    """One player. Used by the report system to answer a reporter.

    Deliberately the same path as a broadcast rather than a bare
    `user.send()`: a reply that rides the queue inherits the retries, the rate
    limit and the ledger, so an answer to someone with DMs closed is recorded
    as BLOCKED instead of disappearing.
    """
    return await notify(bot, event=event, title=title, body=body,
                        priority=priority, image_url=image_url,
                        audience={"kind": T.USERS, "ids": [int(user_id)],
                                  "skip_no_beys": False})


def cancel(update_id: str) -> int:
    """Stop a send. Rows already delivered stay in the ledger."""
    dropped = S.drop_pending(update_id)
    S.set_update_state(update_id, CANCELLED)
    return dropped


def wake(bot) -> None:
    """Nudge the worker, if the cog is loaded. Never raises."""
    try:
        cog = bot.get_cog("Notifications")
        if cog is not None:
            cog.wake()
    except Exception:                                    # noqa: BLE001
        log.debug("[updates] could not wake the worker", exc_info=True)
