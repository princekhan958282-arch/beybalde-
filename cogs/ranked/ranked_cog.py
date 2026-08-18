"""
ranked_cog.py — the ranked ladder's Discord surface.

    /leaderboard category:<rank|winrate|wins|streak|catches>
    /rank [user]
    /verify

Prefix equivalents (`;leaderboard`, `;rank`, `;verify`) exist for every one of
them, and the slash commands delegate to those rather than duplicating their
logic — a second copy is how the two paths drift.

The owner-only settings (`;rankadmin` and the `/rankadmin` group) left in
v1.13: they are actions in `cogs/admin/actions.py` under 🎖️ Ranked, calling
the same `utils/ranked.py` functions. A hidden group with five subcommands put
five lines in the slash picker in front of every player who could not use any
of them.

All of the rules live in `utils/ranked.py`, which imports no discord. This file
only turns them into embeds.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from utils.database import get_user, update_user, load_users
from utils import ranked as RK
from utils.ranks import RANK_TIERS, tier_for_score

log = logging.getLogger("beyblade_bot.ranked")

MASTER_ID = 956773141265391676
ENTRIES_PER_PAGE = 10
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _all_users() -> list[dict]:
    try:
        return [u for u in load_users().values() if isinstance(u, dict)]
    except Exception as exc:                             # noqa: BLE001
        log.warning("leaderboard could not read users: %s", exc)
        return []


def _display_name(bot: commands.Bot, guild: Optional[discord.Guild],
                  uid) -> str:
    """A readable name for a profile row.

    Tries the guild first, then the bot's global user cache, and only then
    falls back to the raw id — a board full of "User 1234..." is unreadable,
    and most of those users are visible to the bot somewhere.
    """
    try:
        uid_i = int(uid)
    except (TypeError, ValueError):
        return "Unknown"
    if guild:
        m = guild.get_member(uid_i)
        if m:
            return m.display_name
    u = bot.get_user(uid_i)
    return u.display_name if u else f"User {uid_i}"


class RankedCog(commands.Cog, name="Ranked"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── ;leaderboard ─────────────────────────────────────────────────────────
    @commands.command(name="leaderboard", aliases=["lb", "top"],
                      brief="Ranked leaderboards 🏆")
    async def leaderboard(self, ctx: commands.Context,
                          category: str = RK.DEFAULT_CATEGORY) -> None:
        """Ranked leaderboards. Categories: rank, winrate, wins, streak, catches."""
        key = str(category or "").lower().strip()
        if key not in RK.CATEGORIES:
            opts = ", ".join(f"`{k}`" for k in RK.CATEGORIES)
            return await ctx.send(f"❌ Unknown category `{category}`. Pick one of: {opts}")

        spec = RK.CATEGORIES[key]
        rows = RK.build_board(_all_users(), key, limit=ENTRIES_PER_PAGE)

        e = discord.Embed(
            title=f"{spec['emoji']} {spec['label']} — Top {ENTRIES_PER_PAGE}",
            colour=0xF1C40F,
        )
        if not rows:
            e.description = spec["empty"]
        else:
            lines = []
            for pos, (prof, _v) in enumerate(rows, 1):
                name = _display_name(self.bot, ctx.guild, prof.get("user_id"))
                medal = MEDALS.get(pos, f"`#{pos:>2}`")
                lines.append(f"{medal} **{name}** — {spec['format'](prof)}")
            e.description = "\n".join(lines)

        # Where the caller sits, even when they are off the bottom of the page.
        mine = RK.position_of(_all_users(), ctx.author.id, key)
        if mine and mine > ENTRIES_PER_PAGE:
            e.add_field(name="Your position", value=f"#{mine}", inline=True)
        elif not mine:
            e.add_field(name="Your position", value="Unranked", inline=True)

        foot = [spec["describe"]]
        if RK.verify_required():
            foot.append("verified players only")
        foot.append("ranked battles only")
        e.set_footer(text=" · ".join(foot))
        await ctx.send(embed=e)

    # ── ;rank ────────────────────────────────────────────────────────────────
    @commands.command(name="rank", aliases=["rankcard", "tier"],
                      brief="Your ranked card 🎖️")
    async def rank(self, ctx: commands.Context,
                   member: Optional[discord.Member] = None) -> None:
        """Ranked card: tier, score, ranked record and board positions."""
        target = member or ctx.author
        prof = get_user(target.id)
        users = _all_users()

        score = RK.rank_score(prof)
        tier = tier_for_score(score)
        nxt = next((t for t in RANK_TIERS if t[0] > score), None)

        games = RK.ranked_games(prof)
        e = discord.Embed(title=f"{tier[2]} {target.display_name} — {tier[1]}",
                          colour=tier[3])
        e.add_field(name="⭐ Rank Score", value=f"**{score:,}**", inline=True)
        e.add_field(name="🏆 Ranked W/L",
                    value=f"**{RK.ranked_wins(prof)}**W / "
                          f"**{RK.ranked_losses(prof)}**L", inline=True)
        e.add_field(name="📊 Win Rate",
                    value=(f"**{RK.win_rate(prof):.1f}%**" if games
                           else "—  *no ranked games*"), inline=True)
        e.add_field(name="🔥 Best Streak", value=f"**{RK.best_streak(prof)}**",
                    inline=True)
        e.add_field(name="🌀 Beys Caught", value=f"**{RK.beys_caught(prof):,}**",
                    inline=True)
        e.add_field(name="✅ Verified",
                    value=("Yes" if prof.get(RK.K_VERIFIED) else
                           ("No" if RK.verify_required() else "Not required")),
                    inline=True)

        placings = []
        for key, spec in RK.CATEGORIES.items():
            pos = RK.position_of(users, target.id, key)
            if pos:
                placings.append(f"{spec['emoji']} {spec['label']}: **#{pos}**")
        if placings:
            e.add_field(name="Leaderboard placings", value="\n".join(placings),
                        inline=False)

        if nxt:
            e.add_field(name=f"📈 Next: {nxt[1]}",
                        value=f"**{nxt[0] - score:,}** points to go", inline=False)
        else:
            e.add_field(name="👑 MAX RANK", value="Nothing left to climb.",
                        inline=False)

        e.set_thumbnail(url=target.display_avatar.url)
        e.set_footer(text="Casual battles do not affect any number on this card.")
        await ctx.send(embed=e)

    # ── ;verify ──────────────────────────────────────────────────────────────
    @commands.command(name="verify", brief="Verify for ranked play ✅")
    async def verify(self, ctx: commands.Context) -> None:
        """Verify by being a member of the configured server."""
        cfg = RK.get_config()
        if not RK.verify_required():
            return await ctx.send(
                "✅ Verification isn't required right now — ranked is open to "
                "everyone.")

        guild_id = int(cfg["verify_guild_id"])
        guild = self.bot.get_guild(guild_id)
        invite = cfg.get("verify_invite") or RK.DEFAULT_INVITE

        if guild is None:
            # The bot is not in the verification server, so membership cannot
            # be checked. Say so plainly rather than telling the player they
            # failed — this is a misconfiguration, not their fault.
            return await ctx.send(
                "⚠️ I can't reach the verification server, so I can't check "
                "your membership. Ask an admin to add me to it.")

        member = guild.get_member(ctx.author.id)
        if member is None:
            try:
                member = await guild.fetch_member(ctx.author.id)
            except Exception:                            # noqa: BLE001
                member = None

        if member is None:
            return await ctx.send(embed=discord.Embed(
                title="❌ Not verified yet",
                description=(f"Join **{guild.name}** and run `/verify` again:\n"
                             f"{invite}"),
                colour=0xED4245))

        prof = get_user(ctx.author.id)
        already = bool(prof.get(RK.K_VERIFIED))
        prof[RK.K_VERIFIED] = True
        update_user(ctx.author.id, prof)
        await ctx.send(embed=discord.Embed(
            title="✅ Verified" + ("" if not already else " (already)"),
            description=f"You're cleared for ranked play in **{guild.name}**.",
            colour=0x2ECC71))

    # ── ranked settings moved to /admin → 🎖️ Ranked (v1.13) ─────────────────
    #
    # `;rankadmin` (status/on/off/server/control/invite/reset) and the
    # `/rankadmin` group that delegated to it both lived here. Every one of
    # those six settings is now an action in `cogs/admin/actions.py`, which
    # calls the same `utils/ranked.py` functions — the rules never lived in
    # this file, only the embeds did.
    #
    # The leaderboard reset kept its two-step confirmation: it is a `confirm`
    # action in the registry, so the panel asks twice before wiping a board
    # for every profile.


# `/leaderboard`, `/rank` and `/verify` lived here as three separate top-level
# slash commands. Their subject is the player, so v1.14 folded them into the
# `/player` panel — see `cogs/ui/panels.py:PlayerSpec`, which invokes the
# prefix commands above. The five leaderboards became five select options
# built from `RK.CATEGORIES`, the same table the board itself sorts on, rather
# than a category string typed into a parameter.


