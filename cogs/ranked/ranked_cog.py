"""
ranked_cog.py — the ranked ladder's Discord surface.

    /leaderboard category:<rank|winrate|wins|streak|catches>
    /rank [user]
    /verify
    /rankadmin verify:<on|off|status>  server:<id>
    /rankadmin reset board:<...>

Prefix equivalents (`;leaderboard`, `;rank`, `;verify`, `;rankadmin`) exist for
every one of them, and the slash commands delegate to those rather than
duplicating their logic — a second copy is how the two paths drift.

All of the rules live in `utils/ranked.py`, which imports no discord. This file
only turns them into embeds.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
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

    # ── ;rankadmin ───────────────────────────────────────────────────────────
    @commands.command(name="rankadmin", aliases=["radmin"], hidden=True)
    async def rankadmin(self, ctx: commands.Context, action: str = "status",
                        value: Optional[str] = None) -> None:
        """[Owner] Configure verification and reset leaderboards."""
        if ctx.author.id != MASTER_ID:
            return                                       # silent, like the admin cog

        act = str(action or "").lower()

        if act == "status":
            cfg = RK.get_config()
            gid = cfg.get("verify_guild_id")
            g = self.bot.get_guild(int(gid)) if gid else None
            e = discord.Embed(title="🎖️ Ranked settings", colour=0x5865F2)
            e.add_field(name="Verification",
                        value="**ON**" if cfg["verify_enabled"] else "**OFF**",
                        inline=True)
            e.add_field(name="Armed",
                        value="Yes" if RK.verify_required() else
                              "No — needs a server set", inline=True)
            e.add_field(name="Server",
                        value=(f"{g.name} (`{gid}`)" if g else
                               (f"`{gid}` — bot is not in it" if gid else "not set")),
                        inline=False)
            e.add_field(name="Invite", value=cfg.get("verify_invite") or "—",
                        inline=False)
            verified = sum(1 for u in _all_users() if u.get(RK.K_VERIFIED))
            e.add_field(name="Verified players", value=f"{verified:,}", inline=True)
            e.set_footer(text=";rankadmin on | off | server <id> | invite <url> "
                              "| reset <board>")
            return await ctx.send(embed=e)

        if act in ("on", "enable"):
            cfg = RK.save_config({"verify_enabled": True})
            armed = RK.verify_required()
            return await ctx.send(
                "✅ Verification **ON**." if armed else
                "⚠️ Verification flag set, but no server is configured yet — "
                "the gate is NOT armed. Run `;rankadmin server <guild_id>`.")

        if act in ("off", "disable"):
            RK.save_config({"verify_enabled": False})
            return await ctx.send("✅ Verification **OFF** — ranked is open to all.")

        if act == "server":
            if not value or not value.strip().isdigit():
                return await ctx.send("Usage: `;rankadmin server <guild_id>`")
            gid = int(value.strip())
            g = self.bot.get_guild(gid)
            RK.save_config({"verify_guild_id": gid})
            return await ctx.send(
                f"✅ Verification server set to **{g.name}** (`{gid}`)." if g else
                f"⚠️ Set to `{gid}`, but I'm not in that server — I can't check "
                f"membership until I'm added, so `/verify` will refuse rather "
                f"than fail players.")

        if act == "invite":
            if not value:
                return await ctx.send("Usage: `;rankadmin invite <url>`")
            RK.save_config({"verify_invite": value.strip()})
            return await ctx.send(f"✅ Invite set to {value.strip()}")

        if act == "reset":
            board = (value or "").lower().strip()
            if board not in RK.RESETTABLE and board != "all":
                opts = ", ".join(f"`{k}`" for k in RK.RESETTABLE)
                return await ctx.send(
                    f"Usage: `;rankadmin reset <board>` — {opts} or `all`")
            keys = RK.reset_keys_for(board)
            view = ConfirmReset(ctx.author.id, board, keys)
            e = discord.Embed(
                title=f"⚠️ Reset the {board} leaderboard?",
                description=(
                    f"This zeroes **{', '.join(f'`{k}`' for k in keys)}** for "
                    f"**every player**. It cannot be undone.\n\n"
                    f"Coins, inventory, trainer level and bey levels are not "
                    f"touched."),
                colour=0xED4245)
            view.message = await ctx.send(embed=e, view=view)
            return

        await ctx.send("Usage: `;rankadmin status|on|off|server <id>|"
                       "invite <url>|reset <board>`")


class ConfirmReset(discord.ui.View):
    """Two-step confirm. A leaderboard wipe is irreversible and hits every
    profile, so it never happens on a single command."""

    def __init__(self, owner_id: int, board: str, keys: tuple) -> None:
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.board = board
        self.keys = keys
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.owner_id:
            await i.response.send_message("Not yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        await i.response.defer()
        changed = 0
        try:
            for uid, prof in list(load_users().items()):
                if not isinstance(prof, dict):
                    continue
                if RK.apply_reset(prof, self.board):
                    update_user(int(uid), prof)
                    changed += 1
        except Exception as exc:                         # noqa: BLE001
            log.exception("leaderboard reset failed: %s", exc)
            return await i.edit_original_response(
                embed=discord.Embed(
                    title="❌ Reset failed part-way",
                    description=f"{changed:,} profiles were already cleared. "
                                f"`{type(exc).__name__}`",
                    colour=0xED4245), view=None)
        for c in self.children:
            c.disabled = True
        await i.edit_original_response(
            embed=discord.Embed(
                title=f"✅ {self.board} leaderboard reset",
                description=f"Cleared for **{changed:,}** profiles.",
                colour=0x2ECC71), view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        for c in self.children:
            c.disabled = True
        await i.response.edit_message(
            embed=discord.Embed(title="Cancelled — nothing was reset.",
                                colour=0x99AAB5), view=self)
        self.stop()

    async def on_timeout(self) -> None:
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:                            # noqa: BLE001
                pass


class RankedCommands(commands.Cog, name="Ranked (slash)"):
    """Slash entry points, delegating to the prefix commands above."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _run(self, interaction: discord.Interaction, name: str,
                   *args, **kwargs) -> None:
        cmd = self.bot.get_command(name)
        if cmd is None:
            return await interaction.response.send_message(
                f"`{name}` isn't loaded right now.", ephemeral=True)
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.invoke(cmd, *args, **kwargs)

    # Choices are generated from the same CATEGORIES table the board sorts on,
    # so a new category cannot appear in one place and not the other.
    @app_commands.command(name="leaderboard",
                          description="Ranked leaderboards")
    @app_commands.describe(category="Which board to show")
    @app_commands.choices(category=[
        app_commands.Choice(name=f"{s['emoji']} {s['label']} — {s['describe']}"[:100],
                            value=k)
        for k, s in RK.CATEGORIES.items()
    ])
    async def s_leaderboard(self, interaction: discord.Interaction,
                            category: Optional[app_commands.Choice[str]] = None
                            ) -> None:
        await self._run(interaction, "leaderboard",
                        category=(category.value if category
                                  else RK.DEFAULT_CATEGORY))

    @app_commands.command(name="rank", description="Your ranked card")
    @app_commands.describe(user="Whose card (defaults to you)")
    async def s_rank(self, interaction: discord.Interaction,
                     user: Optional[discord.Member] = None) -> None:
        await self._run(interaction, "rank", user)

    @app_commands.command(name="verify",
                          description="Verify your account for ranked play")
    async def s_verify(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, "verify")

    rankadmin = app_commands.Group(
        name="rankadmin", description="[Owner] Ranked settings and resets")

    @rankadmin.command(name="verify", description="[Owner] Verification on/off")
    @app_commands.choices(state=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
        app_commands.Choice(name="status", value="status"),
    ])
    async def s_admin_verify(self, interaction: discord.Interaction,
                             state: app_commands.Choice[str]) -> None:
        await self._run(interaction, "rankadmin", action=state.value)

    @rankadmin.command(name="server",
                       description="[Owner] Which server players must join")
    @app_commands.describe(guild_id="The server's ID")
    async def s_admin_server(self, interaction: discord.Interaction,
                             guild_id: str) -> None:
        await self._run(interaction, "rankadmin", action="server", value=guild_id)

    @rankadmin.command(name="invite",
                       description="[Owner] The invite shown to unverified players")
    @app_commands.describe(url="Invite link")
    async def s_admin_invite(self, interaction: discord.Interaction,
                             url: str) -> None:
        await self._run(interaction, "rankadmin", action="invite", value=url)

    @rankadmin.command(name="reset", description="[Owner] Wipe a leaderboard")
    @app_commands.describe(board="Which board to clear")
    @app_commands.choices(board=[
        app_commands.Choice(name=f"{s['emoji']} {s['label']}", value=k)
        for k, s in RK.CATEGORIES.items()
    ] + [app_commands.Choice(name="⚠️ Everything", value="all")])
    async def s_admin_reset(self, interaction: discord.Interaction,
                            board: app_commands.Choice[str]) -> None:
        await self._run(interaction, "rankadmin", action="reset",
                        value=board.value)
