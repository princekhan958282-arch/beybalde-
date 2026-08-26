"""
cogs/updates/worker.py — claim a batch, send it, record what happened.

The delivery half of the notification system, and the only thing in it that
touches Discord.

Why a worker and not a loop over the registry
---------------------------------------------
A broadcast to a few thousand players cannot be a `for` loop inside a command:
it would hold the interaction open for the length of the send, lose everything
on a restart, and hit the DM rate limit at full speed. Instead the queue lives
in a table and this walks it a batch at a time, so a send survives a reboot,
can be cancelled halfway, and paces itself.

The five states, and which of them come back
--------------------------------------------
    SENT      delivered
    BLOCKED   terminal — DMs closed, account gone, or opted out
    RETRY     transient — rate limited, a 5xx, a dropped connection
    FAILED    retried MAX_ATTEMPTS times and never got through
    PENDING   waiting; SENDING while a batch holds it

BLOCKED never retries. That is the requirement stated plainly: a player with
DMs closed is not a temporary failure, and hammering them every tick would burn
the rate limit that everyone else's delivery needs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import discord

from . import prefs as P
from . import service as SV
from . import store as S

log = logging.getLogger("beyblade_bot.updates.worker")

# ── Pacing ───────────────────────────────────────────────────────────────────
# Deliberately slow. Discord does not publish a DM rate limit, and opening a
# channel with someone the bot has never DMed is the expensive half, so the
# safe assumption is roughly two a second and the default sits well under it.
# Every value is overridable from config.json — see `tune()`.
BATCH_SIZE = 25
DELAY_BETWEEN = 0.6          # seconds between individual DMs
TICK_SECONDS = 5.0           # how often the loop looks for work
MAX_ATTEMPTS = 3             # RETRY this many times, then FAILED
BACKOFF_CAP = 300.0          # never sleep longer than this on a 429

PENDING, SENDING = "PENDING", "SENDING"
SENT, FAILED, BLOCKED, RETRY = "SENT", "FAILED", "BLOCKED", "RETRY"


def tune() -> dict:
    """Pacing from config.json's `notifications` block, with defaults.

    Read on every batch rather than cached at import, so an admin can slow a
    send down while it is running instead of restarting the bot to do it.
    """
    out = {"batch": BATCH_SIZE, "delay": DELAY_BETWEEN,
           "tick": TICK_SECONDS, "attempts": MAX_ATTEMPTS}
    try:
        from utils.database import load_config
        cfg = (load_config() or {}).get("notifications") or {}
        for key, name in (("batch", "batch_size"), ("delay", "delay_seconds"),
                          ("tick", "tick_seconds"), ("attempts",
                                                     "max_attempts")):
            if name in cfg:
                out[key] = type(out[key])(cfg[name])
    except Exception:                                    # noqa: BLE001
        pass
    out["batch"] = max(1, min(100, int(out["batch"])))
    out["delay"] = max(0.0, min(10.0, float(out["delay"])))
    out["attempts"] = max(1, min(10, int(out["attempts"])))
    return out


def build_embed(upd: dict) -> discord.Embed:
    """The DM a player actually receives."""
    emoji, label, colour = P.PRIORITY_LABEL.get(
        P.normalise(upd.get("priority")), P.PRIORITY_LABEL[P.NORMAL])
    title = f"{emoji} {upd.get('title') or 'Beycord'}"
    e = discord.Embed(title=title[:250],
                      description=(upd.get("body") or "")[:4000],
                      colour=colour)
    if upd.get("image_url"):
        e.set_image(url=upd["image_url"])
    foot = ["Beycord"]
    if upd.get("version"):
        foot.append(str(upd["version"]))
    if not P.overrides_optout(upd.get("priority")):
        # Only shown when the switch was actually consulted. Printing it on a
        # Critical notice would be advertising an opt-out that does not apply.
        foot.append(";notifications to change what you receive")
    e.set_footer(text=" · ".join(foot)[:2048])
    return e


class DeliveryWorker:
    """Walks the queue. One instance, owned by the cog."""

    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()       # one batch in flight at a time
        self._wake = asyncio.Event()
        self.last_run: Optional[float] = None
        self.sent_total = 0
        self.current: Optional[str] = None

    def wake(self) -> None:
        self._wake.set()

    # ── One delivery ─────────────────────────────────────────────────────────
    async def _send_one(self, upd: dict, user_id: str) -> tuple[str, str]:
        """`(state, note)` for one player. Never raises."""
        uid = int(user_id)

        # The preference check happens HERE, at delivery, not at enqueue.
        # A player who opts out after a broadcast is queued should still stop
        # receiving it — the queue can be minutes long.
        try:
            from utils.database import get_user
            ok, why = P.wants(await get_user(uid), event=upd.get("event"),
                              priority=upd.get("priority"))
            if not ok:
                return BLOCKED, why
        except Exception:                                # noqa: BLE001
            pass                                          # fail open: deliver

        try:
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
        except discord.NotFound:
            return BLOCKED, "no such user"
        except discord.HTTPException as exc:
            return RETRY, f"fetch failed: {exc}"

        try:
            await user.send(embed=build_embed(upd))
            return SENT, ""
        except discord.Forbidden:
            # DMs closed, or no shared server. Terminal: retrying cannot
            # change either, and trying costs everyone else throughput.
            return BLOCKED, "DMs closed"
        except discord.NotFound:
            return BLOCKED, "no such user"
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                wait = min(BACKOFF_CAP,
                           float(getattr(exc, "retry_after", 5.0) or 5.0))
                log.warning("[updates] rate limited, sleeping %.1fs", wait)
                await asyncio.sleep(wait)
                return RETRY, "rate limited"
            return RETRY, f"http {getattr(exc, 'status', '?')}"
        except Exception as exc:                         # noqa: BLE001
            return RETRY, f"{type(exc).__name__}: {exc}"

    # ── One batch ────────────────────────────────────────────────────────────
    async def run_batch(self, update_id: str) -> dict:
        cfg = tune()
        upd = S.get_update(update_id)
        if not upd:
            return {"claimed": 0}
        if upd.get("state") == SV.CANCELLED:
            return {"claimed": 0, "cancelled": True}

        claimed = S.claim(update_id, cfg["batch"])
        if not claimed:
            return {"claimed": 0}

        tally = {SENT: 0, BLOCKED: 0, RETRY: 0, FAILED: 0}
        for user_id in claimed:
            state, note = await self._send_one(upd, user_id)
            if state == RETRY:
                # Exhausted retries stop being retries. Without this a player
                # the bot can never reach would be tried forever.
                if S.attempts(update_id, user_id) + 1 >= cfg["attempts"]:
                    state = FAILED
            S.mark(update_id, user_id, state, note or None)
            tally[state] = tally.get(state, 0) + 1
            if state == SENT:
                self.sent_total += 1
            if cfg["delay"]:
                await asyncio.sleep(cfg["delay"])
        return {"claimed": len(claimed), **tally}

    # ── The loop ─────────────────────────────────────────────────────────────
    async def tick(self) -> dict:
        """One pass over everything with work left. Never raises."""
        if self._lock.locked():
            return {"busy": True}
        async with self._lock:
            self.last_run = time.time()
            done = {}
            try:
                for update_id in S.pending_updates():
                    upd = S.get_update(update_id) or {}
                    sched = upd.get("scheduled_at")
                    if sched and float(sched) > time.time():
                        continue          # not yet — the whole scheduler
                    if upd.get("state") == SV.CANCELLED:
                        continue
                    self.current = update_id
                    res = await self.run_batch(update_id)
                    done[update_id] = res
                    left = S.counts(update_id)
                    if not (left.get(PENDING) or left.get(RETRY)
                            or left.get(SENDING)):
                        S.set_update_state(update_id, SV.DONE)
            except Exception:                            # noqa: BLE001
                log.exception("[updates] worker tick failed")
                try:
                    from utils import errorlog
                    errorlog.record("updates.worker", RuntimeError("tick"))
                except Exception:                        # noqa: BLE001
                    pass
            finally:
                self.current = None
            return done

    def recover(self) -> int:
        """Un-claim anything a previous process died holding."""
        try:
            n = S.reset_stuck()
            if n:
                log.info("[updates] recovered %d stranded deliveries", n)
            return n
        except Exception:                                # noqa: BLE001
            log.exception("[updates] recovery failed")
            return 0
