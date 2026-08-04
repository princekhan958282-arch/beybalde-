"""
notifications.py — DMs and channel announcements.

Two rules worth stating:

* Channel messages use Discord's own <t:...> timestamps, which every viewer
  sees in their own timezone. Nothing we could compute beats that.
* DMs use the player's stored offset, because a DM has exactly one reader and
  "Sat 19:00 UTC+5:30" is clearer to them than a relative timestamp.

A DM can fail for reasons that are nobody's fault — closed DMs, a blocked bot,
a rate limit. Failing to notify must never wedge a tournament, so every send is
best-effort with a bounded retry and the outcome is returned rather than raised.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord

log = logging.getLogger("beyblade_bot.tournament")

MAX_ATTEMPTS = 3
BACKOFF_BASE = 1.5


async def dm(bot, user_id: int, content: str = "",
             embed: Optional[discord.Embed] = None,
             view: Optional[discord.ui.View] = None) -> bool:
    """Best-effort DM with retry. Returns whether it landed."""
    for attempt in range(MAX_ATTEMPTS):
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            if user is None:
                return False
            await user.send(content=content or None, embed=embed, view=view)
            return True
        except discord.Forbidden:
            # DMs closed. Retrying will never help, so stop immediately rather
            # than burning the budget and the rate limit.
            log.info("tournament DM refused by %s", user_id)
            return False
        except discord.HTTPException as e:
            if attempt == MAX_ATTEMPTS - 1:
                log.warning("tournament DM to %s failed: %s", user_id, e)
                return False
            await asyncio.sleep(BACKOFF_BASE ** attempt)
        except Exception as e:                      # noqa: BLE001
            log.warning("tournament DM to %s errored: %s", user_id, e)
            return False
    return False


async def announce(bot, channel_id: int, content: str = "",
                   embed: Optional[discord.Embed] = None) -> bool:
    if not channel_id:
        return False
    try:
        ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if ch is None:
            return False
        await ch.send(content=content or None, embed=embed)
        return True
    except Exception as e:                          # noqa: BLE001
        log.warning("tournament announce to %s failed: %s", channel_id, e)
        return False


DM_BATCH_SIZE = 25
DM_BATCH_DELAY = 2.0


async def dm_all(bot, user_ids, *, batch_size: int = DM_BATCH_SIZE,
                 delay: float = DM_BATCH_DELAY, progress=None,
                 **kwargs) -> dict[int, bool]:
    """Fan out DMs in batches, returning {user_id: delivered}.

    Batched rather than one big gather: an announcement to the whole player
    base is thousands of recipients, and firing every DM at once is a good way
    to get the bot rate limited — or flagged. `batch_size` go out together,
    then a short pause before the next batch.

    Ids are de-duplicated first, because the same player can easily appear in
    two tournaments' entrant lists and nobody should get the same announcement
    twice.

    A closed DM is a normal outcome, not an error: dm() returns False and the
    run continues. Anything that raises is recorded as a failure for that one
    recipient only, so one bad id can't abort a send to everyone else.
    """
    ids = list(dict.fromkeys(int(u) for u in user_ids))
    results: dict[int, bool] = {}
    if not ids:
        return results

    for start in range(0, len(ids), max(1, batch_size)):
        batch = ids[start:start + max(1, batch_size)]
        settled = await asyncio.gather(
            *(dm(bot, u, **kwargs) for u in batch), return_exceptions=True)
        for uid, outcome in zip(batch, settled):
            if isinstance(outcome, BaseException):
                log.warning("DM to %s raised: %s", uid, outcome)
                results[uid] = False
            else:
                results[uid] = bool(outcome)
        if progress is not None:
            try:
                await progress(len(results), len(ids))
            except Exception:                        # noqa: BLE001
                pass                                 # reporting must not break sending
        # No sleep after the final batch — that would just delay the reply.
        if start + batch_size < len(ids):
            await asyncio.sleep(delay)
    return results
