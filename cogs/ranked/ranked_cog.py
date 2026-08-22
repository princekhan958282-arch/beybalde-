"""
ranked_cog.py — the ranked ladder's Discord surface.

    ;leaderboard <category>
    ;rank [user]

The slash surfaces delegate to those prefix commands rather than duplicating
their logic — a second copy is how the two paths drift.

The owner-only settings (`;rankadmin` and the `/rankadmin` group) left in
v1.13: they are actions in `cogs/admin/actions.py` under 🎖️ Ranked, calling
the same `utils/ranked.py` functions. A hidden group with five subcommands put
five lines in the slash picker in front of every player who could not use any
of them.

All of the rules live in `utils/ranked.py`, which imports no discord. This file
only turns them into embeds.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands

from utils.database import get_user, load_users
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


async def _all_users_async() -> list[dict]:
    """The registry, read on a worker thread.

    `load_users()` deserialises every profile — 3,400 JSON blobs, each with a
    full inventory. Doing that on the event loop freezes every other player in
    every server for the duration, which is the same shape that froze the bot
    in `;giveallcoins`. The boards are the one place that genuinely needs all
    the rows, so the read stays and moves off the loop instead.
    """
    return await asyncio.to_thread(_all_users)


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
        """The boards. Categories come from `RK.CATEGORIES`, which is also
        what `/leaderboard` builds its select from — one table, so a new board
        appears in both places or in neither."""
        key = str(category or "").lower().strip()
        if key not in RK.CATEGORIES:
            opts = ", ".join(f"`{k}`" for k in RK.CATEGORIES)
            return await ctx.send(f"❌ Unknown category `{category}`. Pick one of: {opts}")

        # The community boards belong to the main server. The slash choices are
        # built once at registration, so the option exists everywhere whatever
        # the table says — the refusal has to happen HERE, where a board is
        # actually rendered, rather than by hiding the choice.
        if key in getattr(RK, "MAIN_ONLY", ()):
            try:
                from cogs.community import guard as _guard
                allowed = _guard.is_main(ctx.guild)
            except Exception:                            # noqa: BLE001
                allowed = False
            if not allowed:
                return await ctx.send(
                    "🔒 That board is main-server only.")

        spec = RK.CATEGORIES[key]
        # Read once. This used to call `_all_users()` again below for "Your
        # position", deserialising all 3,400 profiles a second time to answer
        # a question the first read already had the data for.
        users = await _all_users_async()
        rows = RK.build_board(users, key, limit=ENTRIES_PER_PAGE)

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
        mine = RK.position_of(users, ctx.author.id, key)
        if mine and mine > ENTRIES_PER_PAGE:
            e.add_field(name="Your position", value=f"#{mine}", inline=True)
        elif not mine:
            e.add_field(name="Your position", value="Unranked", inline=True)

        foot = [spec["describe"], "ranked battles only"]
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

        # The read and all seven board placings on one worker thread. Each
        # placing sorts the whole registry, and the read parses every profile
        # in it — together that is the most expensive thing a player can ask
        # for, and it used to happen on the event loop.
        placings = await asyncio.to_thread(
            lambda: RK.placings(_all_users(), target.id))

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
        if placings:
            e.add_field(name="Leaderboard placings",
                        value="\n".join(
                            f"{RK.CATEGORIES[k]['emoji']} "
                            f"{RK.CATEGORIES[k]['label']}: **#{pos}**"
                            for k, pos in placings.items()),
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

    # ── ;verify removed in v1.18 ────────────────────────────────────────────
    #
    # Verification asked a player to join a configured server before ranked
    # would count them. It defaulted to off, no install ever turned it on, and
    # it cost a command, a profile key and a filter inside `build_board`.
    # Ranked is open to everyone; `utils/ranked.py` no longer has a gate to
    # ask about. Live profiles keep their `ranked_verified` field — nothing
    # reads it.

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
# slash commands. v1.14 folded them into the `/player` panel; v1.18 moved the
# boards back out into their own `/leaderboard` panel — see
# `cogs/ui/panels.py:LeaderboardSpec` — because a board is about everyone and
# `/player` is about one player. Both panels invoke the prefix commands above.
# The board options are built from `RK.CATEGORIES`, the same table the board
# itself sorts on, rather than a category string typed into a parameter.


