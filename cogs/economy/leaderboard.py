"""
cogs/leaderboard.py
-------------------
Global leaderboard and personal rank cards.

Commands
--------
  ;leaderboard   — paginated global leaderboard (sorted by rank score)
  ;rank [@user]  — personal rank card with tier, progress bar, win rate
"""

from __future__ import annotations

import json
import logging

import discord
from discord.ext import commands
from discord import ui

from utils.database import get_user, update_user, load_users, USERS_PATH
from utils.ranks import (
    RANK_TIERS, tier_for_score, rank_score_for,
    WIN_SCORE, LOSS_SCORE,
)
from utils.embeds import level_badge

logger = logging.getLogger("beyblade_bot.leaderboard")

ENTRIES_PER_PAGE = 10


def _load_all_users() -> list[dict]:
    try:
        return list(load_users().values())
    except Exception:
        return []


def _progress_bar(score: int, tier_min: int, next_min: int, length: int = 10) -> str:
    if next_min <= tier_min:
        return "█" * length + " MAX"
    progress = (score - tier_min) / (next_min - tier_min)
    filled   = min(length, int(progress * length))
    empty    = length - filled
    pct      = min(100, int(progress * 100))
    return f"{'█' * filled}{'░' * empty} {pct}%"


# ── Paginator ─────────────────────────────────────────────────────────────────

class LeaderboardView(ui.View):
    def __init__(self, pages: list[discord.Embed], author_id: int):
        super().__init__(timeout=60)
        self.pages     = pages
        self.page      = 0
        self.author_id = author_id
        self._refresh()

    def _refresh(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= len(self.pages) - 1

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message("Only the command caller can flip pages.", ephemeral=True)
            return False
        return True

    @ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, i: discord.Interaction, _: ui.Button):
        self.page -= 1
        self._refresh()
        await i.response.edit_message(embed=self.pages[self.page], view=self)

    @ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, i: discord.Interaction, _: ui.Button):
        self.page += 1
        self._refresh()
        await i.response.edit_message(embed=self.pages[self.page], view=self)


# ══════════════════════════════════════════════════════════════════════════════
#  Leaderboard Cog
# ══════════════════════════════════════════════════════════════════════════════

class LeaderboardCog(commands.Cog, name="Leaderboard"):
    """Global leaderboard and personal rank cards."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── ;leaderboard ──────────────────────────────────────────────────────────

    @commands.command(
        name="leaderboard", aliases=["lb", "top"],
        help=(
            "Show the global Beyblade leaderboard.\n\n"
            "Players are ranked by Rank Score.\n"
            "Wins give +25 pts, losses cost -10 pts.\n"
            "Use the buttons to flip pages."
        ),
        brief="Global leaderboard 🏆",
    )
    async def leaderboard(self, ctx: commands.Context) -> None:
        users = _load_all_users()
        if not users:
            return await ctx.send("❌ No players found.")

        users.sort(
            key=lambda u: (u.get("rank_score", 0), u.get("wins", 0)),
            reverse=True,
        )

        chunks     = [users[i:i + ENTRIES_PER_PAGE] for i in range(0, len(users), ENTRIES_PER_PAGE)]
        total_pages = len(chunks)
        pages: list[discord.Embed] = []

        for page_idx, chunk in enumerate(chunks):
            embed = discord.Embed(title="🏆 Global Leaderboard", color=discord.Color.gold())
            lines = []
            for pos, profile in enumerate(chunk, start=page_idx * ENTRIES_PER_PAGE + 1):
                uid    = int(profile.get("user_id", 0))
                score  = profile.get("rank_score", 0)
                wins   = profile.get("wins", 0)
                losses = profile.get("losses", 0)
                tier   = tier_for_score(score)
                medal  = {1: "🥇", 2: "🥈", 3: "🥉"}.get(pos, f"`#{pos}`")
                member = ctx.guild.get_member(uid) if ctx.guild else None
                name   = member.display_name if member else f"User {uid}"
                lines.append(
                    f"{medal} **{name}** — {tier[2]} {tier[1]}\n"
                    f"  Score: **{score:,}** | W: {wins} / L: {losses}"
                )
            embed.description = "\n".join(lines) or "No players yet."
            embed.set_footer(
                text=f"Page {page_idx+1}/{total_pages} | +{WIN_SCORE} per win / -{LOSS_SCORE} per loss"
            )
            pages.append(embed)

        await ctx.send(embed=pages[0], view=LeaderboardView(pages, ctx.author.id))

    # ── ;rank [@user] ─────────────────────────────────────────────────────────

    @commands.command(
        name="rank", aliases=["rankcard", "tier"],
        help="View your rank card with tier, score, and progress to the next rank.\n\n;rank @user — check another player.",
        brief="View your rank card 🎖️",
    )
    async def rank(self, ctx: commands.Context, member: discord.Member = None) -> None:
        target  = member or ctx.author
        profile = get_user(target.id)
        score   = rank_score_for(profile)

        # Find current and next tier
        current_idx = 0
        for i, t in enumerate(RANK_TIERS):
            if score >= t[0]:
                current_idx = i

        current_tier = RANK_TIERS[current_idx]
        next_tier    = RANK_TIERS[current_idx + 1] if current_idx + 1 < len(RANK_TIERS) else None

        tier_min = current_tier[0]
        next_min = next_tier[0] if next_tier else tier_min
        bar      = _progress_bar(score, tier_min, next_min)

        wins   = profile.get("wins", 0)
        losses = profile.get("losses", 0)
        total  = wins + losses
        wr     = f"{wins/total*100:.1f}%" if total else "N/A"

        embed = discord.Embed(
            title=f"{current_tier[2]} {target.display_name}'s Rank Card",
            color=current_tier[3],
        )
        embed.add_field(name="🎖️ Tier",      value=f"**{current_tier[1]}**",              inline=True)
        embed.add_field(name="⭐ Score",      value=f"**{score:,}**",                      inline=True)
        embed.add_field(name="📊 Win Rate",   value=wr,                                    inline=True)
        embed.add_field(name="🏆 Wins",       value=str(wins),                             inline=True)
        embed.add_field(name="💀 Losses",     value=str(losses),                           inline=True)
        embed.add_field(name="💰 Coins",      value=f"{profile.get('coins', 0):,}",        inline=True)
        embed.add_field(name="🌀 Equipped",   value=profile.get("active_beyblade") or "None", inline=True)

        if next_tier:
            pts_needed = next_min - score
            embed.add_field(
                name=f"📈 Progress → {next_tier[1]}",
                value=f"`{bar}`\n**{pts_needed:,}** points needed",
                inline=False,
            )
        else:
            embed.add_field(
                name="👑 MAX RANK ACHIEVED",
                value="`" + "█" * 10 + "` 100%",
                inline=False,
            )

        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=f"+{WIN_SCORE} pts per win | -{LOSS_SCORE} pts per loss")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LeaderboardCog(bot))
