"""
audit.py  —  🔍 Economy & funnel audit (master only)

Built after a registry snapshot showed:
  * 3,131 of 3,356 rows (93%) completely empty
  * 46 accounts had ever fought a battle
  * one wallet held 20.06M of the 41.35M total coin supply (48.5%)

These commands make that visible on demand instead of needing a script.

    ;audit                → economy + funnel overview
    ;audit wallets        → top wallets, with concentration warnings
    ;audit funnel         → how many rows reach each stage
    ;audit user @member   → one account's full picture
    ;audit backup         → dump the SQLite store back to JSON
"""

import os
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands

from utils import database as _db
from utils.database import USER_STORE, USERS_PATH, get_user
from utils.mobile_ui import MobileListView

MASTER_ID = 956773141265391676

# A single wallet holding more than this share of the supply is worth a look.
CONCENTRATION_WARN = 0.20


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "—"


def _ts(value) -> str:
    if not value:
        return "unknown"
    try:
        return f"<t:{int(value)}:R>"
    except Exception:
        return "unknown"


class AuditCog(commands.Cog, name="Audit"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        return ctx.author.id == MASTER_ID

    @commands.group(name="audit", invoke_without_command=True, hidden=True)
    async def audit(self, ctx: commands.Context):
        """🔍 Economy and population overview."""
        s   = USER_STORE.stats()
        pct = USER_STORE.coin_percentiles()
        now = time.time()

        total  = s["total"] or 1
        supply = s["coin_supply"] or 0

        wallets = USER_STORE.top_wallets(1)
        top_share = (wallets[0]["coins"] / supply) if wallets and supply else 0

        e = discord.Embed(
            title="🔍  Economy Audit",
            color=0xe74c3c if top_share >= CONCENTRATION_WARN else 0x2ecc71,
        )
        e.add_field(
            name="Population",
            value=(f"Rows: **{s['total']:,}**\n"
                   f"Empty (ghosts): **{s['ghosts']:,}**  ({_pct(s['ghosts'], total)})\n"
                   f"Real players: **{s['total'] - s['ghosts']:,}**"),
            inline=True,
        )
        e.add_field(
            name="Engagement",
            value=(f"Ever battled: **{s['battlers']:,}**  ({_pct(s['battlers'], total)})\n"
                   f"Own a blade: **{s['collectors']:,}**  ({_pct(s['collectors'], total)})\n"
                   f"Active 7d: **{USER_STORE.active_since(now - 7 * 86400):,}**"),
            inline=True,
        )
        e.add_field(
            name="Coin supply",
            value=(f"Total: 🪙 **{supply:,}**\n"
                   f"Median: 🪙 {pct.get('p50', 0):,}  ·  "
                   f"p90: 🪙 {pct.get('p90', 0):,}  ·  "
                   f"p99: 🪙 {pct.get('p99', 0):,}\n"
                   f"Richest holds **{top_share * 100:.1f}%** of everything"),
            inline=False,
        )
        if top_share >= CONCENTRATION_WARN:
            e.add_field(
                name="⚠️ Concentration warning",
                value=(f"One wallet holds {top_share * 100:.1f}% of the supply. "
                       f"Check `;audit wallets` — if that isn't you, the "
                       f"leaderboard and casino balance are both meaningless "
                       f"until you find the source."),
                inline=False,
            )
        e.set_footer(text="Sub-commands: wallets · funnel · user · backup")
        await ctx.send(embed=e)

    @audit.command(name="wallets")
    async def audit_wallets(self, ctx: commands.Context, limit: int = 20):
        """Top wallets with age and activity, to spot anything unearned."""
        limit  = max(1, min(50, limit))
        rows   = USER_STORE.top_wallets(limit)
        supply = USER_STORE.stats()["coin_supply"] or 1

        def render(r, idx):
            share   = r["coins"] / supply * 100
            battles = r["wins"] + r["losses"]
            # Big balance with almost no gameplay behind it is the tell.
            flag = " 🚩" if (share >= 5 and battles < 5 and r["inv_count"] < 5) else ""
            return (f"**{idx + 1}.** <@{r['user_id']}> — 🪙 {r['coins']:,} "
                    f"({share:.1f}%){flag}\n"
                    f"　L{r['level']} · {r['wins']}W/{r['losses']}L · "
                    f"{r['inv_count']} blades · {_ts(r['last_seen'])}")

        def option_of(r):
            return (f"{r['coins']:,} coins",
                    f"L{r['level']} · {r['wins']}W · {r['inv_count']} blades",
                    "💰")

        async def detail(interaction, r):
            member = ctx.guild.get_member(int(r["user_id"])) if ctx.guild else None
            name = member.display_name if member else f"ID {r['user_id']}"
            e = discord.Embed(title=f"💰  {name}", color=0xf1c40f)
            e.add_field(name="Coins", value=f"🪙 {r['coins']:,}", inline=True)
            e.add_field(name="Share",
                        value=f"{r['coins'] / supply * 100:.2f}%", inline=True)
            e.add_field(name="Level", value=str(r["level"]), inline=True)
            e.add_field(name="Record",
                        value=f"{r['wins']}W/{r['losses']}L", inline=True)
            e.add_field(name="Blades", value=str(r["inv_count"]), inline=True)
            e.add_field(name="Last seen", value=_ts(r["last_seen"]), inline=True)
            e.set_footer(text=f"ID {r['user_id']}  •  ;audit user @them for more")
            await interaction.response.send_message(embed=e, ephemeral=True)

        view = MobileListView(
            owner=ctx.author,
            title="💰  Top Wallets",
            items=rows,
            render=render,
            option_of=option_of,
            detail=detail,
            detail_placeholder="🔍 Inspect a wallet…",
            colour=0xf1c40f,
            footer="🚩 big balance, little gameplay",
        )
        view.message = await ctx.send(embed=view.embed(), view=view)

    @audit.command(name="funnel")
    async def audit_funnel(self, ctx: commands.Context):
        """Where new players drop off."""
        s     = USER_STORE.stats()
        total = s["total"] or 1
        stages = [
            ("Touched the bot",  s["total"]),
            ("Owns a blade",     s["collectors"]),
            ("Fought a battle",  s["battlers"]),
            ("Active last 7d",   USER_STORE.active_since(time.time() - 7 * 86400)),
        ]

        lines = []
        prev = None
        for label, n in stages:
            bar  = "█" * max(0, min(20, round(n / total * 20)))
            drop = "" if prev is None or prev == 0 else f"  ↓ {(1 - n / prev) * 100:.0f}% drop"
            lines.append(f"`{bar:<20}` **{n:,}** — {label}{drop}")
            prev = n

        e = discord.Embed(
            title="📉  New Player Funnel",
            description="\n".join(lines),
            color=0x3498db,
        )
        e.add_field(
            name="Read this as",
            value=("The biggest gap is where to spend your next update. "
                   "If most rows never get a blade, the fix is onboarding "
                   "(`;start`), not more content."),
            inline=False,
        )
        await ctx.send(embed=e)

    @audit.command(name="user")
    async def audit_user(self, ctx: commands.Context, member: discord.Member):
        """Full picture for one account."""
        p = get_user(member.id)
        e = discord.Embed(title=f"🔍  {member.display_name}", color=0x9b59b6)
        e.add_field(name="Coins",   value=f"🪙 {p.get('coins', 0):,}", inline=True)
        e.add_field(name="Level",   value=str(p.get("level", 0)),      inline=True)
        e.add_field(name="XP",      value=f"{p.get('xp', 0):,}",       inline=True)
        e.add_field(name="Record",
                    value=f"{p.get('wins', 0)}W / {p.get('losses', 0)}L", inline=True)
        e.add_field(name="Rank",    value=f"{p.get('rank_score', 0):,}", inline=True)
        e.add_field(name="Blades",  value=str(len(p.get("inventory", []))), inline=True)
        e.add_field(name="Active blade",
                    value=str(p.get("active_beyblade") or "none"), inline=False)

        battles = p.get("wins", 0) + p.get("losses", 0)
        earned_est = battles * 200
        if p.get("coins", 0) > max(50_000, earned_est * 20):
            e.add_field(
                name="⚠️ Note",
                value=(f"Balance is far above what {battles} battles would "
                       f"normally produce. Worth checking against admin grants "
                       f"and casino history."),
                inline=False,
            )
        e.set_footer(text=f"ID {member.id}")
        await ctx.send(embed=e)

    @audit.command(name="db", aliases=["database", "backend"])
    async def audit_db(self, ctx: commands.Context, action: str = "status"):
        """Which storage backend is live, and move data to MySQL."""
        action = (action or "status").lower()

        if action in ("status", "info"):
            e = discord.Embed(title="🗄️  Storage Backend",
                              color=0x2ecc71 if _db.BACKEND == "mysql" else 0x3498db)
            e.add_field(name="Active", value=f"**{_db.BACKEND.upper()}**", inline=True)
            e.add_field(name="Profiles",
                        value=f"{_db.USER_STORE.count():,}", inline=True)
            try:
                from utils import mysql_store as ms
                from utils import secrets as _sec
                url = _sec.get("MYSQL_URL")
                if not url:
                    d = _sec.diagnose()
                    # Say exactly where it looked and what was there, so a
                    # missing key is a two-second fix instead of a guess.
                    detail = (
                        "**MYSQL_URL not found.** Checked:\n"
                        f"• env vars → {', '.join(d['env_keys']) or 'none set'}\n"
                        f"• `config_local.py` → "
                        + ("exists" if d["local_exists"] else "**missing**")
                        + (", loaded" if d["local_loaded"] else ", **not loadable**")
                        + "\n"
                        f"• keys it defines → "
                        + (", ".join(f"`{k}`" for k in d["local_keys"]) or "*none*")
                        + f"\n\nPath: `{d['local_path']}`\n"
                        "Add `MYSQL_URL = \"mysql://...\"` there and **restart** — "
                        "the file is read once at boot."
                    )
                elif not ms.driver_available():
                    detail = "PyMySQL not installed — `pip install PyMySQL`"
                else:
                    cfg = ms.parse_url(url) or {}
                    ok, msg = (_db.MYSQL_STORE.probe() if _db.MYSQL_STORE
                               else ms.MySQLStore(url).probe())
                    detail = (f"`{cfg.get('host')}/{cfg.get('database')}`\n"
                              f"{'✅' if ok else '❌'} {msg}")
            except Exception as exc:
                detail = f"check failed: {exc}"
            e.add_field(name="MySQL", value=detail, inline=False)
            e.add_field(name="SQLite (always present)",
                        value=f"{_db.SQLITE_STORE.count():,} profiles", inline=False)
            e.set_footer(text=";audit db migrate  ·  ;audit db verify")
            return await ctx.send(embed=e)

        if action == "migrate":
            if _db.MYSQL_STORE is None:
                return await ctx.send(
                    "❌ MySQL isn't connected. `;audit db status` shows why.")
            async with ctx.typing():
                from utils import mysql_store as ms
                rep = ms.migrate(_db.SQLITE_STORE,
                                 os.path.join(_db.BASE_DIR, "data"),
                                 _db.MYSQL_STORE, force=False)
            e = discord.Embed(title="🗄️  Migration", color=0x2ecc71)
            e.add_field(name="Profiles",
                        value=(f"{rep['users']:,}"
                               + (" (already present, skipped)" if rep["skipped"] else "")),
                        inline=False)
            if rep["kv"]:
                e.add_field(name="Other stores",
                            value="\n".join(f"`{k}` — {v}" for k, v in rep["kv"].items()),
                            inline=False)
            if rep["errors"]:
                e.add_field(name="⚠️ Errors", value="\n".join(rep["errors"])[:1000],
                            inline=False)
                e.color = 0xe67e22
            e.set_footer(text="Local files are untouched — safe to run again")
            return await ctx.send(embed=e)

        if action == "verify":
            if _db.MYSQL_STORE is None:
                return await ctx.send("❌ MySQL isn't connected.")
            async with ctx.typing():
                from utils import mysql_store as ms
                v = ms.verify(_db.SQLITE_STORE, _db.MYSQL_STORE)
            ok = v.get("mismatched", 1) == 0 and not v.get("error")
            e = discord.Embed(title="🗄️  Verification",
                              color=0x2ecc71 if ok else 0xe74c3c)
            e.add_field(name="Local rows", value=f"{v.get('source_rows',0):,}", inline=True)
            e.add_field(name="MySQL rows", value=f"{v.get('mysql_rows',0):,}", inline=True)
            e.add_field(name="Sampled",
                        value=f"{v.get('checked',0)} checked · "
                              f"{v.get('mismatched',0)} mismatched", inline=False)
            if v.get("error"):
                e.add_field(name="Error", value=str(v["error"])[:1000], inline=False)
            return await ctx.send(embed=e)

        await ctx.send("Usage: `;audit db status|migrate|verify`")

    @audit.command(name="backup")
    async def audit_backup(self, ctx: commands.Context):
        """Dump the SQLite store back out to JSON."""
        path = USERS_PATH.replace(".json", f".backup.{int(time.time())}.json")
        try:
            n = USER_STORE.export_json(path)
        except Exception as exc:
            return await ctx.send(f"❌ Backup failed: `{exc}`")
        USER_STORE.checkpoint()
        await ctx.send(
            f"✅ Exported **{n:,}** profiles to `{path.split('/')[-1]}`.\n"
            f"WAL checkpointed — `users.db` is safe to copy on its own."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AuditCog(bot))
