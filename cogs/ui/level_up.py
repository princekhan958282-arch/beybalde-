"""
ui/level_up.py
--------------
Listens for the `level_up` bot event and DMs the player their level-up card.

This cog does NOT pay the coins, and that is the important line in this file.
It used to: 1,000 flat per level gained, written straight to the profile. Two
things were wrong with that.

  * The event it listens for is dispatched by `cogs/economy/profile.award_xp`,
    which has no callers anywhere in the bot. So this handler has never run and
    nobody has ever been paid for a level.
  * Even once it did run, paying here means the reward is owed to whichever
    code path remembered to dispatch, rather than to the level itself.

So as of v1.23 the coins are paid inside `utils.database.grant_xp` — the single
choke point every XP grant in the game already goes through — and this file
reports what was paid. If both paid, a level would be worth double.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from utils.database import (
    xp_to_next_level,
    MAX_LEVEL,
    get_user,
)
from utils.embeds import level_badge, xp_bar
from utils.trainer_levels import level_reward, level_up_payout

# Milestone levels
_MILESTONES: dict[int, tuple[str, str]] = {
    5:         ("🌟", "You're getting the hang of this!"),
    10:        ("⚡", "Ten levels in — the payouts are getting real."),
    20:        ("🔥", "A seasoned Blader emerges!"),
    30:        ("💎", "Elite tier — very few reach here."),
    50:        ("👑", "Half-century legend. Incredible."),
    MAX_LEVEL: ("🏆", "MAXIMUM LEVEL REACHED — you are unstoppable!"),
}


def _level_color(level: int) -> int:
    if level >= MAX_LEVEL: return 0xf1c40f
    if level >= 30:        return 0x9b59b6
    if level >= 20:        return 0xe67e22
    if level >= 10:        return 0x3498db
    if level >= 5:         return 0x2ecc71
    return 0x1abc9c


class LevelUpCog(commands.Cog, name="LevelUp"):
    """Sends level-up DMs and awards coins when a player levels up."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener("on_level_up")
    async def on_level_up(
        self,
        user_id: int,
        old_level: int,
        new_level: int,
        channel: discord.abc.Messageable | None,
    ) -> None:
        # ── Guards ────────────────────────────────────────────────────────────
        if new_level <= old_level:
            return  # no actual level gain
        if old_level >= MAX_LEVEL:
            return  # already maxed before this event

        # ── Resolve user ──────────────────────────────────────────────────────
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.NotFound:
                return

        # ── Report the coins; `grant_xp` already paid them ────────────────────
        # Read, never written. The same arithmetic as the payout, so the card
        # and the balance can only ever agree.
        levels_gained = new_level - old_level
        coin_reward   = level_up_payout(old_level, new_level)
        profile       = await get_user(user_id)

        # ── Build embed data ──────────────────────────────────────────────────
        at_max    = new_level >= MAX_LEVEL
        badge     = level_badge(new_level)
        color     = _level_color(new_level)
        skipped   = levels_gained

        total_xp              = profile.get("xp", 0)
        _, xp_need, xp_prog   = xp_to_next_level(total_xp)

        milestone_emoji, milestone_text = _MILESTONES.get(
            new_level, ("⬆️", "Keep battling to grow stronger!")
        )

        skip_note = f" *(+{skipped} levels at once!)*" if skipped > 1 else ""
        title     = "🏆 MAX LEVEL REACHED!" if at_max else f"{milestone_emoji} LEVEL UP!"

        if at_max:
            xp_line   = f"`{'█' * 10}` **MAX LEVEL — {new_level}**"
            next_line = "You've reached the pinnacle of power!"
        else:
            xp_line   = xp_bar(xp_prog, xp_need)
            next_line = f"Next level: **{xp_need - xp_prog:,} XP** to go"

        next_pay = level_reward(new_level + 1)
        next_reward_line = (
            f"🪙 Level **{new_level + 1}** pays **{next_pay:,}** coins"
            if not at_max else "Nothing left to climb."
        )

        embed = discord.Embed(
            title=title,
            description=(
                f"You advanced from **Level {old_level}** → "
                f"**{badge} Level {new_level}**{skip_note}\n"
                f"──────────────────────────────────────"
            ),
            color=color,
        )
        embed.add_field(
            name="💰 Level Reward",
            value=(f"+**{coin_reward:,} coins** awarded!"
                   + (f"\n*Level {new_level} alone paid "
                      f"{level_reward(new_level):,}.*" if skipped > 1 else "")),
            inline=False,
        )
        embed.add_field(
            name="📊 Progress",
            value=f"{xp_line}\n{next_line}",
            inline=False,
        )
        embed.add_field(
            name="⬆️ Next",
            value=next_reward_line,
            inline=False,
        )
        if new_level in _MILESTONES:
            embed.add_field(
                name=f"{milestone_emoji} Milestone!",
                value=milestone_text,
                inline=False,
            )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(
            text=f"Total XP: {total_xp:,} | Balance: {profile['coins']:,} coins | ;profile to view"
        )

        # ── Send as DM only (private to the player) ───────────────────────────
        try:
            await user.send(embed=embed)
        except discord.Forbidden:
            # DMs closed — fall back to the channel if available
            if channel is not None:
                try:
                    await channel.send(
                        content=f"{user.mention} levelled up to **{badge} Level {new_level}**! "
                                f"(+{coin_reward:,} coins) — enable DMs to see full details.",
                        embed=embed,
                    )
                except Exception:
                    pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LevelUpCog(bot))
