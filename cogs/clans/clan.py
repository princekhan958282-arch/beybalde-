"""
clan.py  —  🛡️ Clans

Retention lever: people come back for their friends, not for a bot. Clans give
players a reason to log in that isn't a daily reward.

Also a real coin sink — creating a clan costs 25,000 Beycoins, and the treasury
takes coins out of circulation until someone withdraws them.

    ;clan                      → your clan (or how to get one)
    ;clan create <tag> <name>  → found a clan
    ;clan join <name|tag>      → join an open clan
    ;clan leave                → leave
    ;clan info <name|tag>      → look at any clan
    ;clan list                 → clan leaderboard
    ;clan invite @user         → invite to an invite-only clan
    ;clan kick @user           → owner only
    ;clan deposit <amount>     → put coins in the treasury
    ;clan withdraw <amount>    → owner only
    ;clan desc <text>          → set the description
    ;clan open / ;clan closed  → toggle who can join
"""

import time
from typing import Optional

import discord
from discord.ext import commands

from utils.database import get_user, update_user
from utils.mobile_ui import MobileListView, bar as ui_bar

from . import clan_data as cd


def _member_power(user_id: int) -> dict:
    p = get_user(user_id)
    return {
        "level":      p.get("level", 0),
        "rank_score": p.get("rank_score", 0),
        "wins":       p.get("wins", 0),
        "losses":     p.get("losses", 0),
        "blades":     len(p.get("inventory", [])),
    }


def _clan_power(clan: dict) -> dict:
    """Aggregate stats. Read live so a clan can never hold stale numbers."""
    total = {"level": 0, "rank_score": 0, "wins": 0, "losses": 0, "blades": 0}
    for uid in clan.get("members", []):
        try:
            m = _member_power(uid)
        except Exception:
            continue
        for k in total:
            total[k] += m[k]
    return total


class ClanCog(commands.Cog, name="Clans"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Helpers ──────────────────────────────────────────────────────────────
    async def _require_clan(self, ctx) -> Optional[dict]:
        clan = cd.clan_of(ctx.author.id)
        if clan is None:
            await ctx.send("❌ You're not in a clan. `;clan list` to find one, "
                           "or `;clan create <tag> <name>` to start your own.")
            return None
        return clan

    def _clan_embed(self, clan: dict) -> discord.Embed:
        """Compact card. Two inline fields per row (three squash on a phone),
        and the roster lives behind a button instead of dumping 15 mentions."""
        power = _clan_power(clan)
        door = "🟢 Open" if clan.get("open", True) else "🔒 Invite only"
        desc = (clan.get("description") or "").strip()[:120]

        e = discord.Embed(
            title=f"🛡️  [{clan['tag']}] {clan['name']}",
            description=(
                (f"*{desc}*\n" if desc else "")
                + f"👑 <@{clan['owner_id']}> · {door}"
            ),
            color=0x9b59b6,
        )
        # Exactly two inline fields — Discord mobile lays those out side by side.
        e.add_field(
            name="Clan",
            value=(f"👥 {len(clan['members'])}/{cd.MAX_MEMBERS}\n"
                   f"🪙 {clan.get('treasury', 0):,}"),
            inline=True,
        )
        e.add_field(
            name="Combat",
            value=(f"🏆 {power['rank_score']:,} pts\n"
                   f"⚔ {power['wins']}W/{power['losses']}L"),
            inline=True,
        )
        e.set_footer(text=f"{power['blades']:,} blades  •  "
                          f"{time.strftime('%b %Y', time.gmtime(clan['created_at']))}")
        return e

    async def _send_clan(self, ctx, clan: dict):
        view = ClanCardView(ctx.author, clan, self)
        view.message = await ctx.send(embed=self._clan_embed(clan), view=view)

    # ── ;clan ────────────────────────────────────────────────────────────────
    @commands.group(name="clan", aliases=["guild", "team"],
                    invoke_without_command=True)
    async def clan(self, ctx: commands.Context):
        """🛡️ Your clan."""
        clan = cd.clan_of(ctx.author.id)
        if clan:
            return await self._send_clan(ctx, clan)

        e = discord.Embed(
            title="🛡️  Clans",
            description=(
                "You're not in a clan yet.\n\n"
                f"**Join one** — `;clan list` to browse, then `;clan join <tag>`\n"
                f"**Start one** — `;clan create <tag> <name>` "
                f"(costs 🪙 {cd.CREATE_COST:,})\n\n"
                "Clans give you a shared treasury, a combined leaderboard, and "
                "a tag next to your name."
            ),
            color=0x9b59b6,
        )
        await ctx.send(embed=e)

    # ── create ───────────────────────────────────────────────────────────────
    @clan.command(name="create", aliases=["found", "new"])
    async def clan_create(self, ctx: commands.Context, tag: str = None, *, name: str = None):
        if not tag or not name:
            return await ctx.send(
                "❌ Usage: `;clan create <tag> <name>`\n"
                "Example: `;clan create DRGN Dragon Riders`")

        if cd.clan_of(ctx.author.id):
            return await ctx.send("❌ You're already in a clan — `;clan leave` first.")

        for err in (cd.validate_tag(tag), cd.validate_name(name)):
            if err:
                return await ctx.send(f"❌ {err}")
        taken = cd.name_taken(name, tag)
        if taken:
            return await ctx.send(f"❌ {taken}")

        profile = get_user(ctx.author.id)
        if profile.get("coins", 0) < cd.CREATE_COST:
            return await ctx.send(
                f"❌ Founding a clan costs 🪙 **{cd.CREATE_COST:,}** — "
                f"you have 🪙 {profile.get('coins', 0):,}.")

        profile["coins"] -= cd.CREATE_COST
        update_user(ctx.author.id, profile)

        try:
            clan = cd.create_clan(ctx.author.id, name, tag)
        except Exception as exc:
            profile["coins"] += cd.CREATE_COST      # refund on failure
            update_user(ctx.author.id, profile)
            return await ctx.send(f"❌ Couldn't create the clan: `{exc}`")

        e = self._clan_embed(clan)
        e.title = f"🎉  [{clan['tag']}] {clan['name']} founded!"
        view = ClanCardView(ctx.author, clan, self)
        view.message = await ctx.send(
            f"{ctx.author.mention} spent 🪙 {cd.CREATE_COST:,} to found a clan.",
            embed=e, view=view)

    # ── join / leave ─────────────────────────────────────────────────────────
    @clan.command(name="join")
    async def clan_join(self, ctx: commands.Context, *, query: str = None):
        if not query:
            return await ctx.send("❌ Usage: `;clan join <name or tag>`")
        clan = cd.find_by_name(query)
        if clan is None:
            return await ctx.send(f"❌ No clan called **{query}**. Try `;clan list`.")
        ok, msg = cd.add_member(clan["id"], ctx.author.id)
        await ctx.send(("✅ " if ok else "❌ ") + msg)

    @clan.command(name="leave", aliases=["quit"])
    async def clan_leave(self, ctx: commands.Context):
        ok, msg, _clan = cd.remove_member(ctx.author.id)
        await ctx.send(("✅ " if ok else "❌ ") + msg)

    # ── info / list ──────────────────────────────────────────────────────────
    @clan.command(name="info", aliases=["show"])
    async def clan_info(self, ctx: commands.Context, *, query: str = None):
        if not query:
            clan = cd.clan_of(ctx.author.id)
            if clan is None:
                return await ctx.send("❌ Usage: `;clan info <name or tag>`")
        else:
            clan = cd.find_by_name(query)
            if clan is None:
                return await ctx.send(f"❌ No clan called **{query}**.")
        await self._send_clan(ctx, clan)

    @clan.command(name="list", aliases=["lb", "top", "leaderboard"])
    async def clan_list(self, ctx: commands.Context):
        clans = cd.all_clans()
        if not clans:
            return await ctx.send(
                f"No clans exist yet — be the first!\n"
                f"`;clan create <tag> <name>` (🪙 {cd.CREATE_COST:,})")

        scored = sorted(
            ((_clan_power(c)["rank_score"], c) for c in clans),
            key=lambda x: -x[0],
        )
        medals = ["🥇", "🥈", "🥉"]

        def render(item, idx):
            score, c = item
            badge = medals[idx] if idx < 3 else f"**{idx + 1}.**"
            door  = "🟢" if c.get("open", True) else "🔒"
            return (f"{badge} {door} **[{c['tag']}] {c['name']}**\n"
                    f"　{len(c['members'])}/{cd.MAX_MEMBERS} · {score:,} pts · "
                    f"🪙 {c.get('treasury', 0):,}")

        def option_of(item):
            score, c = item
            return (f"[{c['tag']}] {c['name']}",
                    f"{len(c['members'])} members · {score:,} pts",
                    "🛡️")

        async def detail(interaction, item):
            _score, c = item
            fresh = cd.get_clan(c["id"]) or c
            await interaction.response.send_message(
                embed=self._clan_embed(fresh), ephemeral=True)

        view = MobileListView(
            owner=ctx.author,
            title="🛡️  Clan Leaderboard",
            items=scored,
            render=render,
            option_of=option_of,
            detail=detail,
            detail_placeholder="🔍 Open a clan…",
            colour=0xf1c40f,
            footer="🟢 open  🔒 invite only",
        )
        view.message = await ctx.send(embed=view.embed(), view=view)

    # ── invite / kick ────────────────────────────────────────────────────────
    @clan.command(name="invite")
    async def clan_invite(self, ctx: commands.Context, member: discord.Member = None):
        if member is None:
            return await ctx.send("❌ Usage: `;clan invite @user`")
        clan = await self._require_clan(ctx)
        if clan is None:
            return
        ok, msg = cd.invite_member(clan["id"], ctx.author.id, member.id)
        await ctx.send(("✅ " if ok else "❌ ") + msg)

    @clan.command(name="kick")
    async def clan_kick(self, ctx: commands.Context, member: discord.Member = None):
        if member is None:
            return await ctx.send("❌ Usage: `;clan kick @user`")
        clan = await self._require_clan(ctx)
        if clan is None:
            return
        ok, msg = cd.kick_member(clan["id"], ctx.author.id, member.id)
        await ctx.send(("✅ " if ok else "❌ ") + msg)

    # ── treasury ─────────────────────────────────────────────────────────────
    @clan.command(name="deposit", aliases=["dep"])
    async def clan_deposit(self, ctx: commands.Context, amount: int = 0):
        if amount <= 0:
            return await ctx.send("❌ Usage: `;clan deposit <amount>`")
        clan = await self._require_clan(ctx)
        if clan is None:
            return

        profile = get_user(ctx.author.id)
        if profile.get("coins", 0) < amount:
            return await ctx.send(
                f"❌ You only have 🪙 {profile.get('coins', 0):,}.")

        profile["coins"] -= amount
        update_user(ctx.author.id, profile)

        ok, new_balance = cd.update_treasury(clan["id"], amount)
        if not ok:
            profile["coins"] += amount              # refund on failure
            update_user(ctx.author.id, profile)
            return await ctx.send("❌ Couldn't update the treasury — nothing was taken.")

        await ctx.send(
            f"✅ {ctx.author.mention} deposited 🪙 **{amount:,}** into "
            f"**[{clan['tag']}]**.\nTreasury: 🪙 **{new_balance:,}**")

    @clan.command(name="withdraw", aliases=["wd"])
    async def clan_withdraw(self, ctx: commands.Context, amount: int = 0):
        if amount <= 0:
            return await ctx.send("❌ Usage: `;clan withdraw <amount>`")
        clan = await self._require_clan(ctx)
        if clan is None:
            return
        if clan["owner_id"] != ctx.author.id:
            return await ctx.send("❌ Only the clan owner can withdraw.")

        ok, new_balance = cd.update_treasury(clan["id"], -amount)
        if not ok:
            return await ctx.send(
                f"❌ Treasury only has 🪙 {clan.get('treasury', 0):,}.")

        profile = get_user(ctx.author.id)
        profile["coins"] = profile.get("coins", 0) + amount
        update_user(ctx.author.id, profile)

        await ctx.send(
            f"✅ Withdrew 🪙 **{amount:,}** from **[{clan['tag']}]**.\n"
            f"Treasury: 🪙 **{new_balance:,}**")

    # ── settings ─────────────────────────────────────────────────────────────
    @clan.command(name="desc", aliases=["description", "motto"])
    async def clan_desc(self, ctx: commands.Context, *, text: str = ""):
        clan = await self._require_clan(ctx)
        if clan is None:
            return
        if clan["owner_id"] != ctx.author.id:
            return await ctx.send("❌ Only the clan owner can set the description.")
        text = text.strip()[:200]
        cd.set_field(clan["id"], "description", text)
        await ctx.send("✅ Description updated." if text else "✅ Description cleared.")

    @clan.command(name="open")
    async def clan_open(self, ctx: commands.Context):
        clan = await self._require_clan(ctx)
        if clan is None:
            return
        if clan["owner_id"] != ctx.author.id:
            return await ctx.send("❌ Only the clan owner can change this.")
        cd.set_field(clan["id"], "open", True)
        await ctx.send("🟢 Clan is now **open** — anyone can join.")

    @clan.command(name="closed", aliases=["close", "private"])
    async def clan_closed(self, ctx: commands.Context):
        clan = await self._require_clan(ctx)
        if clan is None:
            return
        if clan["owner_id"] != ctx.author.id:
            return await ctx.send("❌ Only the clan owner can change this.")
        cd.set_field(clan["id"], "open", False)
        await ctx.send("🔒 Clan is now **invite only** — use `;clan invite @user`.")


class ClanCardView(discord.ui.View):
    """Keeps the clan card short — roster and stats open on demand."""

    def __init__(self, owner, clan: dict, cog: "ClanCog"):
        super().__init__(timeout=180)
        self.owner = owner
        self.clan  = clan
        self.cog   = cog
        self.message = None

    @discord.ui.button(label="Roster", emoji="👥",
                       style=discord.ButtonStyle.secondary)
    async def roster(self, interaction: discord.Interaction, _: discord.ui.Button):
        clan = cd.get_clan(self.clan["id"]) or self.clan
        members = clan.get("members", [])

        rows = []
        for uid in members:
            try:
                stats = _member_power(uid)
            except Exception:
                stats = {"level": 0, "rank_score": 0, "wins": 0,
                         "losses": 0, "blades": 0}
            rows.append((uid, stats))
        rows.sort(key=lambda r: -r[1]["rank_score"])

        def render(item, idx):
            uid, st = item
            crown = "👑" if uid == clan["owner_id"] else "　"
            return (f"{crown} **{idx + 1}.** <@{uid}>\n"
                    f"　L{st['level']} · {st['rank_score']:,} pts · "
                    f"{st['wins']}W · {st['blades']} blades")

        view = MobileListView(
            owner=interaction.user,
            title=f"👥  [{clan['tag']}] Roster",
            items=rows,
            render=render,
            colour=0x9b59b6,
            footer="sorted by rank score",
            empty="No members.",
        )
        await interaction.response.send_message(
            embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(label="Treasury", emoji="🪙",
                       style=discord.ButtonStyle.secondary)
    async def treasury(self, interaction: discord.Interaction, _: discord.ui.Button):
        clan = cd.get_clan(self.clan["id"]) or self.clan
        e = discord.Embed(
            title=f"🪙  [{clan['tag']}] Treasury",
            description=f"**🪙 {clan.get('treasury', 0):,}**",
            color=0xf1c40f,
        )
        e.add_field(
            name="How it works",
            value=("`;clan deposit <amount>` — anyone can add\n"
                   "`;clan withdraw <amount>` — owner only\n"
                   "Clan wars stake from here"),
            inline=False,
        )
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ClanCog(bot))
