"""
Top.gg vote rewards.

Slash command:
    /vote

A verified active Top.gg vote grants:
    - 20,000 Beycoins
    - 3 hours of EXP Surge

The claim is protected by both Top.gg verification and a durable 12-hour
per-user claim window. The reminder is also durable: after a successful claim,
the bot stores the next reminder timestamp in the player's profile and a
background loop sends one DM when that timestamp becomes due.

TOPGG_API_TOKEN must contain the API token from the bot's Top.gg dashboard.
The legacy vote-check endpoint remains supported by Top.gg and is intentionally
used here because it answers the one thing this command needs without requiring
this Discord bot process to expose a public webhook server.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.database import get_user, load_users, mutate_user
from utils.secrets import get as get_secret
from utils.xp_boost import K_SURGE_UNTIL

log = logging.getLogger("beyblade_bot.vote")

BOT_ID = 1423193032810823820
VOTE_URL = f"https://top.gg/bot/{BOT_ID}/vote"
TOPGG_CHECK_URL = f"https://top.gg/api/bots/{BOT_ID}/check"

COIN_REWARD = 20_000
SURGE_REWARD_SECONDS = 3 * 60 * 60
CLAIM_WINDOW_SECONDS = 12 * 60 * 60
REMINDER_POLL_SECONDS = 5 * 60

K_LAST_CLAIM = "topgg_last_claim"
K_REMINDER_AT = "topgg_vote_reminder_at"
K_REMINDER_SENT = "topgg_vote_reminder_sent"

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)


class VoteServiceError(RuntimeError):
    """Top.gg could not be queried safely."""


def _now() -> int:
    return int(time.time())


def _claim(profile: dict[str, Any], now: int) -> dict[str, int | bool]:
    """Grant a vote reward atomically inside database.mutate_user.

    A second concurrent /vote can still reach this function after the API check,
    so duplicate prevention must happen here under the user-store lock rather
    than only in the command handler.
    """
    last_claim = int(profile.get(K_LAST_CLAIM, 0) or 0)
    next_claim = last_claim + CLAIM_WINDOW_SECONDS
    if last_claim and now < next_claim:
        return {
            "granted": False,
            "next_claim": next_claim,
            "coins": int(profile.get("coins", 0) or 0),
            "surge_until": int(profile.get(K_SURGE_UNTIL, 0) or 0),
        }

    coins = int(profile.get("coins", 0) or 0) + COIN_REWARD
    existing_surge = int(profile.get(K_SURGE_UNTIL, 0) or 0)
    surge_base = max(now, existing_surge)
    surge_until = surge_base + SURGE_REWARD_SECONDS
    reminder_at = now + CLAIM_WINDOW_SECONDS

    profile["coins"] = coins
    profile[K_SURGE_UNTIL] = surge_until
    profile[K_LAST_CLAIM] = now
    profile[K_REMINDER_AT] = reminder_at
    profile[K_REMINDER_SENT] = False

    return {
        "granted": True,
        "next_claim": reminder_at,
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

    async def _has_voted(self, user_id: int) -> bool:
        token = get_secret("TOPGG_API_TOKEN") or get_secret("TOPGG_TOKEN")
        if not token:
            raise VoteServiceError(
                "TOPGG_API_TOKEN is not configured on the bot host.")

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT)

        try:
            async with self._session.get(
                TOPGG_CHECK_URL,
                params={"userId": str(int(user_id))},
                headers={"Authorization": token},
            ) as response:
                if response.status == 200:
                    data = await response.json(content_type=None)
                    return bool(data.get("voted", 0))

                body = (await response.text())[:300]
                if response.status in (401, 403):
                    log.error("Top.gg vote check rejected API token: %s", body)
                    raise VoteServiceError(
                        "Top.gg authentication failed. The bot owner needs to "
                        "refresh TOPGG_API_TOKEN.")

                if response.status == 429:
                    raise VoteServiceError(
                        "Top.gg is rate-limiting vote checks. Try again shortly.")

                log.warning("Top.gg vote check failed: HTTP %s %s",
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
        await interaction.response.defer(ephemeral=True, thinking=True)

        profile = await get_user(interaction.user.id)
        now = _now()
        last_claim = int(profile.get(K_LAST_CLAIM, 0) or 0)
        next_claim = last_claim + CLAIM_WINDOW_SECONDS

        if last_claim and now < next_claim:
            embed = discord.Embed(
                title="🗳️ Vote reward already claimed",
                description=(
                    f"Your next reward can be claimed <t:{next_claim}:R>.\n\n"
                    f"Reward: **{COIN_REWARD:,} Beycoins** + "
                    "**3 hours EXP Surge**."
                ),
                color=discord.Color.orange(),
            )
            await interaction.followup.send(
                embed=embed, view=self._vote_view(), ephemeral=True)
            return

        try:
            voted = await self._has_voted(interaction.user.id)
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
                embed=embed, view=self._vote_view(), ephemeral=True)
            return

        if not voted:
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
                embed=embed, view=self._vote_view(), ephemeral=True)
            return

        result = await mutate_user(
            interaction.user.id,
            lambda p: _claim(p, now),
        )

        if not result["granted"]:
            await interaction.followup.send(
                f"✅ Your vote is verified, but this vote reward was already "
                f"claimed. Next claim: <t:{result['next_claim']}:R>.",
                view=self._vote_view(),
                ephemeral=True,
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
            embed=embed, view=self._vote_view(), ephemeral=True)

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
