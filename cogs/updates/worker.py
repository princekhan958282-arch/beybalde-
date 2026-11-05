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
    """The update embed a player or server channel receives."""
    emoji, label, colour = P.PRIORITY_LABEL.get(
        P.normalise(upd.get("priority")), P.PRIORITY_LABEL[P.NORMAL])
    title = f"{emoji} {upd.get('title') or 'Beycord'}"
    e = discord.Embed(title=title[:250],
                      description=(upd.get("body") or "")[:4000],
                      colour=colour)
    if upd.get("image_url"):
        e.set_image(url=upd["image_url"])
    reward = (upd.get("audience") or {}).get("reward")
    if reward:
        r_parts = []
        if reward.get("coins"):
            r_parts.append(f"🪙 **+{int(reward['coins']):,} Coins**")
        if reward.get("exp"):
            r_parts.append(f"⚡ **+{int(reward['exp']):,} EXP**")
        if r_parts:
            e.add_field(name="🎁 Update Reward",
                        value=" · ".join(r_parts) + "\n*Tap [Claim Reward] below!*",
                        inline=False)
    foot = ["Beycord"]
    if upd.get("version"):
        foot.append(str(upd["version"]))
    if not P.overrides_optout(upd.get("priority")):
        foot.append(";notifications to change what you receive")
    e.set_footer(text=" · ".join(foot)[:2048])
    return e


class UpdateDMButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"beyupdate:(?P<uid>\d+):(?P<update>[A-Za-z0-9_]+):(?P<act>off|interested)"):
    """Persistent actions attached to every update DM."""

    def __init__(self, user_id: int, update_id: str, action: str) -> None:
        self.user_id = int(user_id)
        self.update_id = str(update_id)
        self.action = action
        if action == "off":
            label, emoji, style = "Turn Off Notifications", "🔕", discord.ButtonStyle.secondary
        else:
            label, emoji, style = "Interested", "⭐", discord.ButtonStyle.success
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, style=style,
            custom_id=f"beyupdate:{self.user_id}:{self.update_id}:{action}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["uid"]), match["update"], match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "These update buttons belong to the player who received this DM.",
                ephemeral=True)

        from utils.database import mutate_user

        if self.action == "off":
            def turn_off(profile):
                P.set_switch(profile, P.K_UPDATES, False)
                P.set_switch(profile, P.K_EVENTS, False)
            await mutate_user(self.user_id, turn_off)
            return await interaction.response.send_message(
                "🔕 Notifications are now off. You can turn them back on with `;notifications`.",
                ephemeral=True)

        def mark_interested(profile):
            interests = profile.setdefault("update_interests", [])
            if self.update_id in interests:
                return False
            interests.append(self.update_id)
            return True

        added = await mutate_user(self.user_id, mark_interested)
        await interaction.response.send_message(
            "⭐ Interest saved!" if added else "⭐ You're already marked as interested.",
            ephemeral=True)


class UpdateClaimButton(discord.ui.DynamicItem[discord.ui.Button],
                        template=r"beyclaim:(?P<update>[A-Za-z0-9_]+)"):
    """Persistent claim button for update rewards."""

    def __init__(self, update_id: str) -> None:
        self.update_id = str(update_id)
        super().__init__(discord.ui.Button(
            label="Claim Reward", emoji="🎁", style=discord.ButtonStyle.success,
            custom_id=f"beyclaim:{self.update_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["update"])

    async def callback(self, interaction: discord.Interaction) -> None:
        upd = S.get_update(self.update_id)
        if not upd:
            return await interaction.response.send_message(
                "That update is no longer available.", ephemeral=True)
        aud = upd.get("audience") or {}
        reward = aud.get("reward")
        if not reward:
            return await interaction.response.send_message(
                "No reward is attached to this update.", ephemeral=True)

        claimed = S.claim_reward(self.update_id, str(interaction.user.id))
        if not claimed:
            return await interaction.response.send_message(
                "🎁 You have already claimed this update's reward!", ephemeral=True)

        from utils.database import mutate_user

        coins_gained = int(reward.get("coins", 0) or 0)
        exp_gained = int(reward.get("exp", 0) or 0)

        def grant(profile):
            if coins_gained > 0:
                profile["coins"] = profile.get("coins", 0) + coins_gained
            if exp_gained > 0:
                profile["xp"] = profile.get("xp", 0) + exp_gained

        await mutate_user(interaction.user.id, grant)

        gains = []
        if coins_gained > 0:
            gains.append(f"🪙 **{coins_gained:,} Coins**")
        if exp_gained > 0:
            gains.append(f"⚡ **{exp_gained:,} EXP**")

        await interaction.response.send_message(
            f"🎁 **Reward Claimed!** You received {' and '.join(gains) or 'your rewards'}.",
            ephemeral=True)


class UpdateRatingButton(discord.ui.DynamicItem[discord.ui.Button],
                         template=r"beyrate:(?P<update>[A-Za-z0-9_]+):(?P<val>loved|good|issues)"):
    """Persistent community feedback buttons."""

    LABELS = {
        "loved": ("Loved it", "🔥", discord.ButtonStyle.secondary),
        "good": ("Good", "⚖️", discord.ButtonStyle.secondary),
        "issues": ("Issues", "⚠️", discord.ButtonStyle.secondary),
    }

    def __init__(self, update_id: str, val: str) -> None:
        self.update_id = str(update_id)
        self.val = str(val).lower()
        label, emoji, style = self.LABELS.get(self.val, ("Feedback", "💬", discord.ButtonStyle.secondary))
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, style=style,
            custom_id=f"beyrate:{self.update_id}:{self.val}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["update"], match["val"])

    async def callback(self, interaction: discord.Interaction) -> None:
        S.record_feedback(self.update_id, str(interaction.user.id), self.val)
        summary = S.feedback_summary(self.update_id)
        label = self.LABELS.get(self.val, (self.val,))[0]
        await interaction.response.send_message(
            f"Thanks for your feedback! (Recorded: **{label}** · Total feedback: {summary.get('total', 1):,})",
            ephemeral=True)


def update_view(user_id: Optional[int], update_id: str, is_dm: bool = True) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    upd = S.get_update(update_id) or {}
    reward = (upd.get("audience") or {}).get("reward")
    if reward:
        view.add_item(UpdateClaimButton(update_id))
    view.add_item(UpdateRatingButton(update_id, "loved"))
    view.add_item(UpdateRatingButton(update_id, "good"))
    view.add_item(UpdateRatingButton(update_id, "issues"))
    if is_dm and user_id:
        view.add_item(UpdateDMButton(user_id, update_id, "off"))
    return view


def update_dm_view(user_id: int, update_id: str) -> discord.ui.View:
    return update_view(user_id, update_id, is_dm=True)


async def broadcast_guilds_async(bot, update_id: str) -> dict:
    from utils.database import get_announce_channel
    upd = S.get_update(update_id)
    if not upd:
        return {"sent": 0, "failed": 0}
    embed = build_embed(upd)
    view = update_view(None, update_id, is_dm=False)
    sent, failed = 0, 0
    for guild in getattr(bot, "guilds", []):
        cid = get_announce_channel(guild.id)
        if not cid:
            continue
        channel = guild.get_channel(cid) or bot.get_channel(cid)
        if channel:
            try:
                await channel.send(embed=embed, view=view)
                sent += 1
            except Exception:
                failed += 1
    log.info("[updates] broadcast %s to %d server channels (%d failed)", update_id, sent, failed)
    return {"sent": sent, "failed": failed}


def broadcast_guilds(bot, update_id: str) -> None:
    try:
        asyncio.create_task(broadcast_guilds_async(bot, update_id))
    except Exception:
        pass


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
            await user.send(embed=build_embed(upd),
                            view=update_dm_view(uid, upd["update_id"]))
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
