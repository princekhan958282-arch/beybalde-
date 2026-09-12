"""
Top.gg vote rewards.

Slash command:
    /vote

A verified active Top.gg vote grants:
    - 20,000 Beycoins
    - 3 hours of EXP Surge

The claim is protected by Top.gg verification plus the exact v1 vote creation
timestamp, so one vote can only pay once. The reminder uses Top.gg's own vote
expiry timestamp and is stored durably in the player's profile.

TOPGG_API_TOKEN must contain the API token from the bot's Top.gg dashboard.
The current Top.gg v1 vote-status endpoint is used so the bot can identify the
exact vote by its creation timestamp and reward it once without requiring this
Discord process to expose a public webhook server.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.database import load_users, mutate_user
from utils.secrets import get as get_secret
from utils.xp_boost import K_SURGE_UNTIL

log = logging.getLogger("beyblade_bot.vote")

BOT_ID = 1423193032810823820
VOTE_URL = f"https://top.gg/bot/{BOT_ID}/vote"
TOPGG_V1_CHECK_URL = "https://top.gg/api/v1/projects/@me/votes/{user_id}"

COIN_REWARD = 20_000
SURGE_REWARD_SECONDS = 3 * 60 * 60
CLAIM_WINDOW_SECONDS = 12 * 60 * 60
REMINDER_POLL_SECONDS = 5 * 60

K_LAST_CLAIM = "topgg_last_claim"
K_LAST_VOTE_CREATED = "topgg_last_vote_created_at"
K_REMINDER_AT = "topgg_vote_reminder_at"
K_REMINDER_SENT = "topgg_vote_reminder_sent"

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)


class VoteServiceError(RuntimeError):
    """Top.gg could not be queried safely."""


def _now() -> int:
    return int(time.time())


def _parse_topgg_time(value: str) -> int:
    """Parse Top.gg ISO-8601 timestamps into UTC Unix seconds."""
    try:
        text = str(value or "").strip()
        if not text:
            raise ValueError("empty timestamp")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError) as exc:
        raise VoteServiceError("Top.gg returned an invalid vote timestamp.") from exc


def _claim(
    profile: dict[str, Any],
    now: int,
    vote_created_at: str,
    vote_expires_at: int,
) -> dict[str, int | bool]:
    """Grant exactly one reward for one Top.gg vote.

    v1 gives us the vote's creation timestamp. That is the idempotency key:
    re-running /vote for the same active vote cannot pay twice, while a new
    vote can be rewarded immediately even if the previous vote was claimed
    several hours after it was cast.
    """
    last_vote = str(profile.get(K_LAST_VOTE_CREATED, "") or "")
    if last_vote and last_vote == vote_created_at:
        return {
            "granted": False,
            "next_claim": vote_expires_at,
            "coins": int(profile.get("coins", 0) or 0),
            "surge_until": int(profile.get(K_SURGE_UNTIL, 0) or 0),
        }

    # Migration guard for rewards claimed by the old v0 implementation. Until
    # that old 12-hour window expires we cannot prove whether the currently
    # active v1 vote is the already-paid vote, so refuse once rather than pay
    # a duplicate. After the window expires, v1 timestamps take over forever.
    if not last_vote:
        last_claim = int(profile.get(K_LAST_CLAIM, 0) or 0)
        if last_claim and now < last_claim + CLAIM_WINDOW_SECONDS:
            return {
                "granted": False,
                "next_claim": last_claim + CLAIM_WINDOW_SECONDS,
                "coins": int(profile.get("coins", 0) or 0),
                "surge_until": int(profile.get(K_SURGE_UNTIL, 0) or 0),
            }

    coins = int(profile.get("coins", 0) or 0) + COIN_REWARD
    existing_surge = int(profile.get(K_SURGE_UNTIL, 0) or 0)
    surge_base = max(now, existing_surge)
    surge_until = surge_base + SURGE_REWARD_SECONDS

    profile["coins"] = coins
    profile[K_SURGE_UNTIL] = surge_until
    profile[K_LAST_CLAIM] = now
    profile[K_LAST_VOTE_CREATED] = vote_created_at
    profile[K_REMINDER_AT] = vote_expires_at
    profile[K_REMINDER_SENT] = False

    return {
        "granted": True,
        "next_claim": vote_expires_at,
        "coins": coins,
        "surge_until": surge_until,
    }


def _mark_reminder_sent(profile: dict[str, Any], expected_at: int) -> bool:
    """Mark exactly the reminder we are about to deliver.

    The timestamp comparison prevents an old worker iteration from marking a
    newer reminder as sent if the user claims again while the worker is running.
    """
    reminder_at = int(profile.get(K_REMINDER_AT, 0) or 0)
    if reminder_at != expected_at or bool(profile.get(K_REMINDER_SENT, False)):
        return False
    profile[K_REMINDER_SENT] = True
    return True


class VoteCog(commands.Cog, name="Vote"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None
        self.reminder_worker.start()

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT)

    async def cog_unload(self) -> None:
        self.reminder_worker.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    async def _vote_status(self, user_id: int) -> dict[str, Any] | None:
        """Return the user's active Top.gg vote, or None if there isn't one."""
        token = get_secret("TOPGG_API_TOKEN") or get_secret("TOPGG_TOKEN")
        if not token:
            raise VoteServiceError(
                "TOPGG_API_TOKEN is not configured on the bot host.")

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT)

        token = str(token).strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()

        url = TOPGG_V1_CHECK_URL.format(user_id=int(user_id))
        try:
            async with self._session.get(
                url,
                params={"source": "discord"},
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if response.status == 200:
                    try:
                        data = await response.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError) as exc:
                        raise VoteServiceError(
                            "Top.gg returned an unreadable vote response.") from exc
                    if not isinstance(data, dict):
                        raise VoteServiceError(
                            "Top.gg returned an invalid vote response.")
                    created_at = str(data.get("created_at", "") or "")
                    expires_raw = str(data.get("expires_at", "") or "")
                    if not created_at or not expires_raw:
                        raise VoteServiceError(
                            "Top.gg returned an incomplete vote record.")

                    expires_at = _parse_topgg_time(expires_raw)
                    if expires_at <= _now():
                        return None

                    return {
                        "created_at": created_at,
                        "expires_at": expires_at,
                        "weight": int(data.get("weight", 1) or 1),
                    }

                # v1 explicitly returns 404 when the user has no current vote.
                if response.status == 404:
                    return None

                body = (await response.text())[:300]
                if response.status in (401, 403):
                    log.error("Top.gg v1 vote check rejected API token: %s", body)
                    raise VoteServiceError(
                        "Top.gg authentication failed. The bot owner needs a "
                        "current v1 project API token.")

                if response.status == 429:
                    raise VoteServiceError(
                        "Top.gg is rate-limiting vote checks. Try again shortly.")

                log.warning("Top.gg v1 vote check failed: HTTP %s %s",
                            response.status, body)
                raise VoteServiceError(
                    "Top.gg could not verify the vote right now.")
        except asyncio.TimeoutError as exc:
            raise VoteServiceError(
                "Top.gg took too long to respond. Try again shortly.") from exc
        except aiohttp.ClientError as exc:
            log.warning("Top.gg network error: %s", exc)
            raise VoteServiceError(
                "Top.gg could not be reached right now.") from exc

    @staticmethod
    def _vote_view() -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Vote on Top.gg",
            emoji="🗳️",
            style=discord.ButtonStyle.link,
            url=VOTE_URL,
        ))
        return view

    @app_commands.command(
        name="vote",
        description="Vote for Beycord and claim 20k Beycoins + 3h EXP Surge",
    )
    async def vote(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)

        try:
            vote_status = await self._vote_status(interaction.user.id)
        except VoteServiceError as exc:
            embed = discord.Embed(
                title="🗳️ Vote for Beycord",
                description=(
                    f"{exc}\n\nYou can still vote now. Once Top.gg verification "
                    "is available, run **/vote** again to claim the reward."
                ),
                color=discord.Color.orange(),
            )
            await interaction.followup.send(
                embed=embed, view=self._vote_view())
            return

        if vote_status is None:
            embed = discord.Embed(
                title="🗳️ Vote for Beycord",
                description=(
                    "Top.gg does not show an active vote from you yet.\n\n"
                    f"Vote and then run **/vote** again to claim:\n"
                    f"🪙 **{COIN_REWARD:,} Beycoins**\n"
                    "⚡ **3 hours EXP Surge**"
                ),
                color=discord.Color.blurple(),
            )
            await interaction.followup.send(
                embed=embed, view=self._vote_view())
            return

        now = _now()
        result = await mutate_user(
            interaction.user.id,
            lambda p: _claim(
                p,
                now,
                str(vote_status["created_at"]),
                int(vote_status["expires_at"]),
            ),
        )

        if not result["granted"]:
            await interaction.followup.send(
                f"✅ Your vote is verified, but this exact vote reward was already "
                f"claimed. You can vote again <t:{result['next_claim']}:R>.",
                view=self._vote_view(),
            )
            return

        embed = discord.Embed(
            title="✅ Vote verified — reward claimed",
            description=(
                f"🪙 **+{COIN_REWARD:,} Beycoins**\n"
                "⚡ **+3 hours EXP Surge**\n\n"
                f"Next vote reminder: <t:{result['next_claim']}:R>"
            ),
            color=discord.Color.green(),
        )
        embed.add_field(
            name="New Beycoin balance",
            value=f"🪙 **{int(result['coins']):,}**",
            inline=True,
        )
        embed.add_field(
            name="EXP Surge active until",
            value=f"<t:{int(result['surge_until'])}:R>",
            inline=True,
        )
        await interaction.followup.send(
            embed=embed, view=self._vote_view())

    @tasks.loop(seconds=REMINDER_POLL_SECONDS)
    async def reminder_worker(self) -> None:
        now = _now()

        try:
            profiles = await asyncio.to_thread(load_users)
        except Exception:
            log.exception("Could not load profiles for vote reminders")
            return

        due: list[tuple[int, int]] = []
        for raw_uid, profile in profiles.items():
            try:
                uid = int(raw_uid)
                reminder_at = int(profile.get(K_REMINDER_AT, 0) or 0)
            except (TypeError, ValueError, AttributeError):
                continue

            if (
                reminder_at > 0
                and reminder_at <= now
                and not bool(profile.get(K_REMINDER_SENT, False))
            ):
                due.append((uid, reminder_at))

        for uid, reminder_at in due:
            try:
                marked = await mutate_user(
                    uid,
                    lambda p, at=reminder_at: _mark_reminder_sent(p, at),
                    touch=False,
                )
                if not marked:
                    continue

                user = self.bot.get_user(uid)
                if user is None:
                    user = await self.bot.fetch_user(uid)

                embed = discord.Embed(
                    title="🗳️ You can vote for Beycord again",
                    description=(
                        f"Your vote reward is ready again:\n"
                        f"🪙 **{COIN_REWARD:,} Beycoins**\n"
                        "⚡ **3 hours EXP Surge**\n\n"
                        "Vote on Top.gg, then run **/vote** to claim it."
                    ),
                    color=discord.Color.blurple(),
                )
                await user.send(embed=embed, view=self._vote_view())
            except discord.Forbidden:
                log.debug("Vote reminder DM blocked by user %s", uid)
            except discord.NotFound:
                log.debug("Vote reminder user %s no longer exists", uid)
            except Exception:
                log.exception("Vote reminder failed for user %s", uid)

    @reminder_worker.before_loop
    async def before_reminder_worker(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoteCog(bot))
