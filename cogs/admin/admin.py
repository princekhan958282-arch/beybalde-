"""
cogs/admin.py
-------------
Master control commands — only usable by the bot owner (MASTER_ID).
ALL commands in this cog are hidden=True so they never appear in ;help or ;info.
Non-masters get a silent failure — the commands appear not to exist.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from utils.database import get_user, update_user, get_beyblade, load_beyblades, add_beyblade_to_inventory, load_users
from utils.ranks import rank_name_for

logger = logging.getLogger("beyblade_bot.admin")

# ── Master owner Discord ID ───────────────────────────────────────────────────
MASTER_ID = 956773141265391676


def is_master():
    async def predicate(ctx: commands.Context) -> bool:
        return ctx.author.id == MASTER_ID
    return commands.check(predicate)


class AdminCog(commands.Cog, name="Admin"):
    """Hidden master control cog."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._spawn_loop_task: asyncio.Task | None = None  # active auto-spawn task

    async def cog_check(self, ctx: commands.Context) -> bool:
        # Block every command in this cog for non-masters
        return ctx.author.id == MASTER_ID

    # ── ;sync ─────────────────────────────────────────────────────────────────
    @commands.command(name="sync", hidden=True)
    @is_master()
    async def sync(self, ctx: commands.Context, scope: str = "guild") -> None:
        """Register slash commands.

        `;sync`        — mirror the globals into this server (instant)
        `;sync global` — everywhere (Discord can take up to an hour)
        `;sync clean`  — drop commands this server has that the bot doesn't
        `;sync purge`  — delete every guild copy, leaving only the globals

        Worth knowing: the plain guild sync writes a COPY of every command into
        this server, and nothing removes entries from that copy later. Delete a
        command from the bot and its guild copy stays behind, shadowing
        whatever replaces it. `clean` is the fix for that; it also runs on
        every boot.
        """
        from utils.command_sync import prune_guild, purge_guild, reconcile
        mode = (scope or "guild").lower()
        try:
            if mode.startswith("purge"):
                n = await purge_guild(self.bot, ctx.guild)
                return await ctx.send(
                    f"🧹 Cleared this server's command copies. "
                    f"The {n} global command(s) still apply.")

            if mode.startswith("clean"):
                keep = {c.name for c in self.bot.tree.get_commands()}
                removed = await prune_guild(self.bot, ctx.guild, keep)
                return await ctx.send(
                    f"🧹 Removed **{len(removed)}** stale command(s): "
                    + ", ".join(f"`/{r}`" for r in removed)
                    if removed else "✅ Nothing stale in this server.")

            if mode.startswith("g") and mode != "guild":
                report = await reconcile(self.bot, guilds=[ctx.guild])
                pruned = sum(len(v) for v in report["pruned"].values())
                return await ctx.send(
                    f"🔁 Synced **{report['synced']}** command(s) globally"
                    + (f", removed **{pruned}** stale here." if pruned else "."))

            self.bot.tree.copy_global_to(guild=ctx.guild)
            cmds = await self.bot.tree.sync(guild=ctx.guild)
            await ctx.send(f"🔁 Synced **{len(cmds)}** slash command(s) "
                           f"to this server.")
        except Exception as exc:
            await ctx.send(f"❌ Sync failed: `{type(exc).__name__}: {exc}`")

    # ── ;givecoins @user <amount> ─────────────────────────────────────────────
    @commands.command(name="givecoins", hidden=True)
    @is_master()
    async def givecoins(self, ctx: commands.Context, member: discord.Member, amount: int) -> None:
        profile = get_user(member.id)
        profile["coins"] = max(0, profile.get("coins", 0) + amount)
        update_user(member.id, profile)
        await ctx.send(
            f"✅ Gave **{amount:,} coins** to {member.mention}. "
            f"Balance: **{profile['coins']:,}**",
            delete_after=10,
        )

    # ── ;removecoin @user <amount> ────────────────────────────────────────────
    @commands.command(name="removecoin", hidden=True)
    @is_master()
    async def removecoin(self, ctx: commands.Context, member: discord.Member, amount: int) -> None:
        profile = get_user(member.id)
        profile["coins"] = max(0, profile.get("coins", 0) - amount)
        update_user(member.id, profile)
        await ctx.send(
            f"✅ Removed **{amount:,} coins** from {member.mention}. "
            f"Balance: **{profile['coins']:,}**",
            delete_after=10,
        )

    # ── ;givebey @user "Blade Name" ───────────────────────────────────────────
    @commands.command(name="givebey", hidden=True)
    @is_master()
    async def givebey(self, ctx: commands.Context, member: discord.Member, *, blade_name: str) -> None:
        blade_name = blade_name.strip('"').strip("'")
        blade = get_beyblade(blade_name)
        if not blade:
            return await ctx.send(f"❌ **{blade_name}** not found in database.", delete_after=10)
        canonical_name = blade["name"]
        profile = get_user(member.id)
        if canonical_name not in profile.get("inventory", []):
            profile.setdefault("inventory", []).append(canonical_name)
        update_user(member.id, profile)
        await ctx.send(f"✅ Gave **{canonical_name}** to {member.mention}.", delete_after=10)

    # ── ;resetplayer @user ────────────────────────────────────────────────────
    @commands.command(name="resetplayer", hidden=True)
    @is_master()
    async def resetplayer(self, ctx: commands.Context, member: discord.Member) -> None:
        fresh = {
            "user_id":         str(member.id),
            "active_beyblade": None,
            "inventory":       [],
            "coins":           0,
            "rank_score":      0,
            "wins":            0,
            "losses":          0,
            "xp":              0,
            "level":           1,
            "parts":           [],
            "last_daily":      None,
        }
        update_user(member.id, fresh)
        await ctx.send(f"✅ **{member.display_name}**'s profile has been fully reset.", delete_after=10)

    # ── ;addpart @user "Part Name" ────────────────────────────────────────────
    @commands.command(name="addpart", hidden=True)
    @is_master()
    async def addpart(self, ctx: commands.Context, member: discord.Member, *, part_name: str) -> None:
        part_name = part_name.strip('"').strip("'")
        profile = get_user(member.id)
        profile.setdefault("parts", [])
        if part_name not in profile["parts"]:
            profile["parts"].append(part_name)
        update_user(member.id, profile)
        await ctx.send(f"✅ Added part **{part_name}** to {member.mention}.", delete_after=10)

    # ── ;setrank @user <rank_number> ──────────────────────────────────────────
    @commands.command(name="setrank", hidden=True)
    @is_master()
    async def setrank(self, ctx: commands.Context, member: discord.Member, rank: int) -> None:
        profile = get_user(member.id)
        profile["rank_override"] = rank
        update_user(member.id, profile)
        rname = rank_name_for(rank)
        await ctx.send(f"✅ Set {member.mention}'s rank to **#{rank}** ({rname}).", delete_after=10)

    # ── ;carddebug — why isn't the info card deploying? ──────────────────────
    @commands.command(name="carddebug", hidden=True)
    @is_master()
    async def carddebug(self, ctx: commands.Context) -> None:
        """Render a test card and report exactly which engine served it,
        or why Playwright is failing on this panel."""
        import platform, shutil
        from utils import info_card
        from utils.database import get_beyblade, load_beyblades

        blade = get_beyblade("Dynamite Belial") or \
                next(iter(load_beyblades().values()), None)

        info_card.clear_cache()          # force a real render, not a cache hit
        t0 = __import__("time").time()
        buf = await info_card.render_info_card(blade)
        dt = (__import__("time").time() - t0) * 1000

        lines = [
            f"**engine:** `{info_card.last_engine}`  ·  {dt:.0f} ms",
            f"**python:** `{platform.python_version()}`  ·  "
            f"**free /tmp:** `{shutil.disk_usage('/tmp').free // 2**20} MB`",
        ]
        try:
            import playwright
            lines.append("**playwright pkg:** installed")
        except ImportError:
            lines.append("**playwright pkg:** ❌ NOT INSTALLED — "
                         "`pip install playwright && playwright install chromium`")
        if info_card.last_playwright_error:
            err = info_card.last_playwright_error[:600]
            lines.append(f"**playwright error:**\n```\n{err}\n```")
            if "browser" in err.lower() and "executable" in err.lower():
                lines.append("→ Chromium binary missing: run "
                             "`python -m playwright install chromium` on the panel.")
            elif "shared librar" in err.lower() or ".so" in err.lower():
                lines.append("→ System libs missing (common on Pterodactyl): the bot "
                             "will keep using the Pillow card. To get the HTML card, "
                             "switch to a container image with Chromium deps.")
        if buf is None:
            return await ctx.send("❌ Both engines failed.\n" + "\n".join(lines))
        import discord as _d
        await ctx.send("\n".join(lines), file=_d.File(buf, filename="carddebug.png"))

    # ── ;reload ───────────────────────────────────────────────────────────────
    @commands.command(name="reload", hidden=True)
    @is_master()
    async def reload_cogs(self, ctx: commands.Context) -> None:
        results = []
        for cog in list(self.bot.extensions.keys()):
            try:
                await self.bot.reload_extension(cog)
                results.append(f"✅ `{cog}`")
            except Exception as exc:
                results.append(f"❌ `{cog}`: {exc}")
        await ctx.send("**Reload results:**\n" + "\n".join(results), delete_after=20)

    # ── ;battlereset [guild_id] ───────────────────────────────────────────────
    @commands.command(name="battlereset", hidden=True)
    @is_master()
    async def battlereset(self, ctx: commands.Context, guild_id: int = None) -> None:
        """[Admin] Force-clear all active battles (or a specific guild's)."""
        battle_cog = self.bot.get_cog("Battle")
        if not battle_cog:
            return await ctx.send("❌ BattleCog not loaded.", delete_after=10)

        if guild_id:
            # Remove all entries belonging to sessions in that guild
            to_remove = [
                uid for uid, session in battle_cog.active_battles.items()
                if getattr(session, "channel", None)
                and getattr(session.channel, "guild", None)
                and session.channel.guild.id == guild_id
            ]
            for uid in to_remove:
                battle_cog.active_battles.pop(uid, None)
            await ctx.send(
                f"✅ Cleared **{len(to_remove)}** battle slot(s) for guild `{guild_id}`.",
                delete_after=10,
            )
        else:
            count = len(battle_cog.active_battles)
            battle_cog.active_battles.clear()
            await ctx.send(f"✅ Cleared **{count}** active battle slot(s) globally.", delete_after=10)

    # ── ;adminspawn [#channel] ────────────────────────────────────────────────
    @commands.command(name="adminspawn", hidden=True)
    @is_master()
    async def adminspawn(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel = None,
        *,
        rarity: str = None,
    ) -> None:
        """[Admin] Force a spawn in any channel, optionally for a specific rarity.

        Usage:
          ;adminspawn                       → spawn in current channel (normal weights)
          ;adminspawn #channel              → spawn in target channel
          ;adminspawn #channel Exclusive    → spawn an Exclusive in target channel
          ;adminspawn . Legendary           → spawn Legendary in current channel
        """
        spawn_cog = self.bot.get_cog("SpawnCog")
        if not spawn_cog:
            return await ctx.send("❌ SpawnCog not loaded.", delete_after=10)

        target = channel or ctx.channel
        exclusive_only = rarity is not None and rarity.strip().title() == "Exclusive"

        if rarity and not exclusive_only:
            # Inject a rarity-filtered spawn by temporarily monkey-patching weights
            # Simple approach: just pass it through exclusive_only for Exclusive,
            # otherwise do a normal spawn and note the rarity filter isn't supported yet.
            await ctx.send(
                f"⚠️ Rarity filter `{rarity}` not yet supported — doing a normal spawn.",
                delete_after=8,
            )

        await spawn_cog._do_spawn(target, exclusive_only=exclusive_only)
        if target != ctx.channel:
            await ctx.send(f"✅ Spawned in {target.mention}.", delete_after=8)

    # ── ;clearspawn [guild_id] ────────────────────────────────────────────────
    @commands.command(name="clearspawn", hidden=True)
    @is_master()
    async def clearspawn(self, ctx: commands.Context, guild_id: int = None) -> None:
        """[Admin] Clear all active spawns for a guild without awarding them."""
        spawn_cog = self.bot.get_cog("SpawnCog")
        if not spawn_cog:
            return await ctx.send("❌ SpawnCog not loaded.", delete_after=10)

        gid   = guild_id or (ctx.guild.id if ctx.guild else None)
        if gid is None:
            return await ctx.send("❌ Provide a guild_id when using this in DMs.", delete_after=10)
        state = spawn_cog.spawn_states.get(gid)
        if not state or not state.get("active"):
            return await ctx.send("❌ No active spawn found for that guild.", delete_after=10)

        async with state["_lock"]:
            names = [s["bey"].get("name", "Unknown") for s in state["active"]]
            state["active"] = []
        await ctx.send(
            f"✅ Cleared **{len(names)}** active spawn(s) for guild `{gid}`: "
            f"{', '.join(f'**{n}**' for n in names)}",
            delete_after=10,
        )

    # ── ;givexp @user <amount> ────────────────────────────────────────────────
    @commands.command(name="givexp", hidden=True)
    @is_master()
    async def givexp(self, ctx: commands.Context, member: discord.Member, amount: int) -> None:
        """[Admin] Give (or with a negative amount, remove) XP. Never goes below 0."""
        profile = get_user(member.id)
        profile["xp"] = max(0, profile.get("xp", 0) + amount)
        update_user(member.id, profile)
        await ctx.send(
            f"✅ Gave **{amount:,} XP** to {member.mention}. Total: **{profile['xp']:,}**",
            delete_after=10,
        )

    # ── ;setcoins @user <amount> ──────────────────────────────────────────────
    @commands.command(name="setcoins", hidden=True)
    @is_master()
    async def setcoins(self, ctx: commands.Context, member: discord.Member, amount: int) -> None:
        """[Admin] Set a user's coins to an exact value."""
        profile = get_user(member.id)
        profile["coins"] = max(0, amount)
        update_user(member.id, profile)
        await ctx.send(
            f"✅ Set {member.mention}'s coins to **{profile['coins']:,}**.",
            delete_after=10,
        )

    # ── ;removebey @user "Blade Name" ─────────────────────────────────────────
    @commands.command(name="removebey", hidden=True)
    @is_master()
    async def removebey(self, ctx: commands.Context, member: discord.Member, *, blade_name: str) -> None:
        """[Admin] Remove a Beyblade from a user's inventory."""
        blade_name = blade_name.strip('"').strip("'")
        profile    = get_user(member.id)
        inventory  = profile.get("inventory", [])
        if blade_name not in inventory:
            return await ctx.send(
                f"❌ **{blade_name}** is not in {member.mention}'s inventory.", delete_after=10
            )
        inventory.remove(blade_name)
        if profile.get("active_beyblade") == blade_name:
            profile["active_beyblade"] = None
        profile["inventory"] = inventory
        update_user(member.id, profile)
        await ctx.send(f"✅ Removed **{blade_name}** from {member.mention}'s inventory.", delete_after=10)

    # ── ;listbattles ──────────────────────────────────────────────────────────
    @commands.command(name="listbattles", hidden=True)
    @is_master()
    async def listbattles(self, ctx: commands.Context) -> None:
        """[Admin] List all currently active battles."""
        battle_cog = self.bot.get_cog("Battle")
        if not battle_cog:
            return await ctx.send("❌ BattleCog not loaded.", delete_after=10)

        battles = battle_cog.active_battles
        if not battles:
            return await ctx.send("✅ No active battles.", delete_after=10)

        seen_sessions = set()
        lines = []
        for uid, session in battles.items():
            sid = id(session)
            if sid in seen_sessions:
                continue
            seen_sessions.add(sid)
            p1 = getattr(session, "p1", None)
            p2 = getattr(session, "p2", None)
            ch = getattr(session, "channel", None)
            lines.append(
                f"• {getattr(p1, 'display_name', uid)} vs {getattr(p2, 'display_name', '?')} "
                f"in #{getattr(ch, 'name', '?')}"
            )

        await ctx.send(
            f"**Active Battles ({len(lines)}):**\n" + "\n".join(lines),
            delete_after=20,
        )


    # ── ;giveallcoins ─────────────────────────────────────────────────────────
    @commands.command(name="giveallcoins", hidden=True)
    @is_master()
    async def giveallcoins(self, ctx: commands.Context, amount: int) -> None:
        """[Admin] Give coins to every user. Usage: ;giveallcoins <amount>"""
        if amount <= 0:
            return await ctx.send("❌ Amount must be positive.", delete_after=10)

        # Bulk update in ONE load→mutate→save cycle. The old per-user
        # update_user() loop rewrote the entire users file once per player
        # (3000+ full-file writes) and froze the event loop for minutes.
        from utils.database import _users_lock, save_users

        def _bulk() -> int:
            with _users_lock:
                users = load_users()
                for profile in users.values():
                    profile["coins"] = profile.get("coins", 0) + amount
                save_users(users)
                return len(users)

        count = await asyncio.to_thread(_bulk)

        await ctx.send(
            f"✅ Gave **{amount:,} coins** to all **{count}** users!",
            delete_after=15,
        )

    # ── ;spawnloop <hours> [#channel] ────────────────────────────────────────
    # ── ;spawnloop stop ───────────────────────────────────────────────────────
    @commands.group(name="spawnloop", hidden=True, invoke_without_command=True)
    @is_master()
    async def spawnloop(
        self,
        ctx: commands.Context,
        duration_hours: float = 1.0,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """[Admin] Auto-spawn beyblades at a random 3-5 min interval.

        Usage:
          ;spawnloop <hours> [#channel]
          ;spawnloop stop

        Examples:
          ;spawnloop 2              -> 2 hrs, random 3-5 min, current channel
          ;spawnloop 2 #spawns      -> 2 hrs, random 3-5 min, in #spawns
          ;spawnloop stop
        """
        import random

        spawn_cog = self.bot.get_cog("SpawnCog")
        if not spawn_cog:
            return await ctx.send("❌ SpawnCog not loaded.", delete_after=10)

        if not (0.1 <= duration_hours <= 24):
            return await ctx.send(
                "❌ Hours must be between `0.1` and `24`.", delete_after=10
            )

        # Stop any existing loop first
        if self._spawn_loop_task and not self._spawn_loop_task.done():
            self._spawn_loop_task.cancel()
            await ctx.send("⚠️ Previous spawn loop cancelled — starting a new one.", delete_after=8)

        target_channel = channel or ctx.channel
        deadline_secs  = duration_hours * 3600

        await ctx.send(
            f"✅ **Spawn loop started!**\n"
            f"📍 Channel  : {target_channel.mention}\n"
            f"⏱ Interval : **random 3–5 min** per spawn\n"
            f"⏳ Duration : **{duration_hours:g} hr(s)**\n"
            f"Use `;spawnloop stop` to cancel early.",
            delete_after=30,
        )
        logger.info(
            "Spawn loop started by %s — channel=%s random-interval=3-5m duration=%gh",
            ctx.author, target_channel, duration_hours,
        )

        async def _loop() -> None:
            elapsed   = 0.0
            spawn_num = 0
            while elapsed < deadline_secs:
                try:
                    await spawn_cog._do_spawn(target_channel)
                    spawn_num += 1
                    logger.info(
                        "Spawn loop: spawn #%d in #%s (elapsed %.0fs / %.0fs)",
                        spawn_num, target_channel.name, elapsed, deadline_secs,
                    )
                except Exception as exc:
                    logger.error("Spawn loop error on spawn #%d: %s", spawn_num + 1, exc)

                wait_secs = random.randint(3, 5) * 60
                elapsed  += wait_secs
                if elapsed < deadline_secs:
                    await asyncio.sleep(wait_secs)

            try:
                await ctx.send(
                    f"✅ Spawn loop **finished** — completed **{spawn_num}** spawns in {target_channel.mention}.",
                    delete_after=30,
                )
            except Exception:
                pass

        self._spawn_loop_task = asyncio.create_task(_loop())

    @spawnloop.command(name="stop", hidden=True)
    @is_master()
    async def spawnloop_stop(self, ctx: commands.Context) -> None:
        """[Admin] Stop the running auto-spawn loop."""
        if self._spawn_loop_task and not self._spawn_loop_task.done():
            self._spawn_loop_task.cancel()
            self._spawn_loop_task = None
            await ctx.send("✅ Spawn loop **stopped**.", delete_after=10)
        else:
            await ctx.send("❌ No spawn loop is currently running.", delete_after=10)

    @commands.command(name="version", aliases=["build", "ver"], hidden=True)
    @is_master()
    async def version(self, ctx: commands.Context) -> None:
        """[Admin] Which build is running, and do all the modules agree?

        The question this answers is "did my upload actually land?" — the
        panel can extract a zip partially, leaving new cogs calling old utils.
        """
        from utils.buildinfo import selfcheck
        from utils import database as db
        rep = selfcheck(verbose=False)

        e = discord.Embed(
            title=f"🧬 Beycord {rep['version']}",
            colour=0x57F287 if rep["ok"] else 0xED4245)
        e.add_field(name="Python", value=rep["python"], inline=True)
        e.add_field(name="discord.py", value=rep["discord_version"] or "?",
                    inline=True)
        e.add_field(name="DB backend", value=getattr(db, "BACKEND", "?"),
                    inline=True)

        # Which commit the auto-updater last installed. This is the other half
        # of "did my upload land?": VERSION above is what the RUNNING code says
        # it is, this is what is on DISK. They disagree when an update has been
        # downloaded but the bot hasn't been restarted yet.
        try:
            from utils import updater
            st = updater.status()
            if not st:
                # No state file at all means check_and_apply() has not run on
                # this host — almost always because the installed code predates
                # the updater, which is a chicken-and-egg the bot can't solve
                # for itself.
                value = ("**Never run on this host.**\n"
                         "Either `utils/updater.py` isn't installed (upload the "
                         "current build once by hand), or the bot hasn't been "
                         "restarted since it was.")
            else:
                lines = []
                if st.get("sha"):
                    lines.append(f"Installed: `{st['sha'][:7]}` on "
                                 f"`{st.get('branch', '?')}`")
                    lines.append(f"{(st.get('message') or '')[:70]}")
                if st.get("last_outcome"):
                    lines.append(f"Last check: **{st['last_outcome']}** "
                                 f"({st.get('last_check', '?')})")
                if st.get("last_detail"):
                    lines.append(f"*{st['last_detail'][:150]}*")
                value = "\n".join(lines) or "No detail recorded."
            e.add_field(name="📥 Auto-update", value=value, inline=False)
        except Exception:                            # noqa: BLE001
            pass

        if rep["parity_gaps"]:
            e.add_field(
                name="❌ Store mismatch",
                value=("MySQLStore is missing: "
                       + ", ".join(f"`{m}`" for m in rep["parity_gaps"][:8])
                       + "\n**utils/ didn't update — re-upload the whole zip.**"),
                inline=False)
        if rep["stale_pyc"]:
            e.add_field(
                name="❌ Stale __pycache__",
                value=("\n".join(f"`{p}`" for p in rep["stale_pyc"][:6])
                       + "\nDelete every `__pycache__` folder and restart."),
                inline=False)
        if rep["ok"]:
            e.add_field(name="Self-check", value="✅ All modules agree.",
                        inline=False)
        await ctx.send(embed=e)

    @commands.command(name="servers", aliases=["guilds", "serverlist"], hidden=True)
    @is_master()
    async def servers(self, ctx: commands.Context) -> None:
        """[Admin] Every server this bot is in.

        bot.guilds is the whole answer — it is the cached list of every guild
        the bot is a member of. Two things to know: it is only populated after
        on_ready (before that it is empty, which is why this is a command and
        not a startup print), and it needs the `guilds` intent, which is on by
        default. For member counts to be accurate you also need the `members`
        intent enabled in the Developer Portal.
        """
        guilds = sorted(self.bot.guilds, key=lambda g: g.member_count or 0,
                        reverse=True)
        if not guilds:
            return await ctx.send("Not in any servers (or still connecting).")

        total = sum(g.member_count or 0 for g in guilds)
        lines = [
            f"`{i:>3}.` **{g.name}** — `{g.id}` · {g.member_count or 0:,} members"
            for i, g in enumerate(guilds, 1)
        ]

        # Embed descriptions cap at 4096 characters, so page rather than let a
        # long list fail the send outright once the bot is in many servers.
        LIMIT = 3900
        pages, buf = [], ""
        for line in lines:
            # A guild name can be 100 characters, so a line stays well under the
            # limit in practice — but truncate anyway: a single oversized line
            # used to be emitted as its own over-4096 page, which Discord
            # rejects outright, and it also pushed out an empty page first.
            if len(line) > LIMIT:
                line = line[:LIMIT - 1] + "\u2026"
            if buf and len(buf) + len(line) + 1 > LIMIT:
                pages.append(buf); buf = ""
            buf += line + "\n"
        if buf:
            pages.append(buf)

        for n, page in enumerate(pages, 1):
            e = discord.Embed(
                title=f"🌐 Servers ({len(guilds)})"
                      + (f" — page {n}/{len(pages)}" if len(pages) > 1 else ""),
                description=page,
                colour=0x5865F2,
            )
            if n == len(pages):
                e.set_footer(text=f"{total:,} members across {len(guilds)} servers")
            await ctx.send(embed=e)

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        # Stay completely silent for non-masters
        if isinstance(error, (commands.CheckFailure, commands.MissingRequiredArgument,
                               commands.MemberNotFound, commands.BadArgument)):
            return
        logger.error(f"Admin error: {error}", exc_info=error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
