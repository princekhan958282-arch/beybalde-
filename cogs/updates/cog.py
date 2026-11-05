"""
cogs/updates/cog.py — the Discord surface, and the worker's home.

    /update        the six-operation panel (owner only)
    /bugs          report a bug, from any server
    /suggest       suggest an improvement
    ;notifications the player's two switches

The delivery loop lives here because a `tasks.loop` needs a cog to hang off.
Everything it does is in `worker.py`; this owns the schedule and the lifecycle.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import prefs as P
from . import reports as R
from . import service as SV
from . import store as S
from . import update_panel as UP
from . import worker as W

log = logging.getLogger("beyblade_bot.updates")

COLOUR = 0x5865F2


def _is_owner(user) -> bool:
    from cogs.admin import actions as A
    uid = getattr(user, "id", None)
    admin_ids = getattr(A, "ADMIN_IDS", {A.MASTER_ID})
    return uid in admin_ids or uid == A.MASTER_ID


# ── Public /whatsnew and /patchnotes ─────────────────────────────────────────
class WhatsNewVersionSelect(discord.ui.Select):
    def __init__(self, updates: list[dict], selected_id: str) -> None:
        opts = []
        for upd in updates[:25]:
            uid = upd["update_id"]
            ver = upd.get("version") or "v1.0"
            title = upd.get("title") or "Update"
            label = f"{ver} · {title}"[:100]
            opts.append(discord.SelectOption(
                label=label,
                value=uid,
                default=(uid == selected_id),
                description=str(upd.get("event", "UPDATE_RELEASED"))[:100]
            ))
        super().__init__(placeholder="Select an update version…", options=opts, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view.selected_id = self.values[0]
        self.view.rebuild()
        await interaction.response.edit_message(
            embed=self.view.build_embed(), view=self.view)


class WhatsNewCategoryButton(discord.ui.Button):
    def __init__(self, cat_key: str, label: str, emoji: str, active: bool) -> None:
        super().__init__(
            label=label, emoji=emoji,
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
            row=1)
        self.cat_key = cat_key

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view.filter_cat = self.cat_key
        self.view.rebuild()
        await interaction.response.edit_message(
            embed=self.view.build_embed(), view=self.view)


class WhatsNewView(discord.ui.View):
    def __init__(self, bot: commands.Bot, selected_id: Optional[str] = None) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.updates = S.list_updates(25)
        self.selected_id = selected_id or (self.updates[0]["update_id"] if self.updates else None)
        self.filter_cat = "ALL"
        self.rebuild()

    def rebuild(self) -> None:
        self.clear_items()
        if self.updates:
            self.add_item(WhatsNewVersionSelect(self.updates, self.selected_id))
        for key, lbl, em in (("ALL", "All", "📜"), ("FEATURES", "Features", "✨"),
                             ("BALANCE", "Balance", "⚖️"), ("FIXES", "Fixes", "🐛")):
            self.add_item(WhatsNewCategoryButton(key, lbl, em, self.filter_cat == key))
        upd = next((u for u in self.updates if u["update_id"] == self.selected_id), None)
        if upd:
            rew = (upd.get("audience") or {}).get("reward")
            if rew:
                self.add_item(W.UpdateClaimButton(upd["update_id"]))

    def build_embed(self) -> discord.Embed:
        if not self.updates:
            return discord.Embed(
                title="📣 Beycord Updates",
                description="*No updates posted yet!*",
                colour=COLOUR)
        upd = next((u for u in self.updates if u["update_id"] == self.selected_id), self.updates[0])
        e = W.build_embed(upd)
        e.title = f"📣 {upd.get('title') or 'Beycord Update'}"
        summary = S.feedback_summary(upd["update_id"])
        if summary.get("total", 0) > 0:
            e.add_field(
                name="Community Feedback",
                value=f"🔥 {summary.get('loved', 0)} · ⚖️ {summary.get('good', 0)} · ⚠️ {summary.get('issues', 0)}",
                inline=True)
        return e


class NotificationCog(commands.Cog, name="Notifications"):
    """Update DMs out, player reports in."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.worker = W.DeliveryWorker(bot)
        self._recovered = False

    async def cog_load(self) -> None:
        # Registered ONCE, for every report post that already exists. Without
        # this a button pressed after a restart does nothing at all — the view
        # it belonged to died with the previous process.
        try:
            self.bot.add_dynamic_items(R.ReportButton, W.UpdateDMButton, W.UpdateClaimButton, W.UpdateRatingButton)
        except Exception:                                # noqa: BLE001
            log.exception("[updates] could not register report buttons")
        self.delivery_loop.start()

    async def cog_unload(self) -> None:
        self.delivery_loop.cancel()

    def wake(self) -> None:
        self.worker.wake()

    # ── The worker's schedule ────────────────────────────────────────────────
    @tasks.loop(seconds=W.TICK_SECONDS)
    async def delivery_loop(self) -> None:
        if not self._recovered:
            # Anything a previous process died holding goes back on the queue
            # before the first batch of this one is claimed.
            self.worker.recover()
            self._recovered = True
        await self.worker.tick()

    @delivery_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ── Anyone can trigger a notification without importing us ───────────────
    @commands.Cog.listener("on_beycord_notify")
    async def on_beycord_notify(self, payload: dict) -> None:
        """`bot.dispatch("beycord_notify", {...})` — the decoupled entry point.

        Same shape as `beycord_battle_end`, so a cog can announce something
        without taking a dependency on this package.
        """
        try:
            await SV.notify(
                self.bot,
                event=payload.get("event", "UPDATE_RELEASED"),
                title=payload.get("title", "Beycord"),
                body=payload.get("body", ""),
                priority=payload.get("priority", P.NORMAL),
                audience=payload.get("audience"),
                image_url=payload.get("image_url"),
                version=payload.get("version"))
        except Exception:                                # noqa: BLE001
            log.exception("[updates] beycord_notify failed")

    # ── /update ──────────────────────────────────────────────────────────────
    @app_commands.command(
        name="update", description="Compose and send an update DM (owner)")
    async def update(self, interaction: discord.Interaction) -> None:
        if not _is_owner(interaction.user):
            return await interaction.response.send_message(
                "That one isn't yours.", ephemeral=True)
        await interaction.response.send_message(
            embed=self._menu_embed(interaction.user),
            view=UpdateMenu(self), ephemeral=True)

    def _menu_embed(self, user) -> discord.Embed:
        e = discord.Embed(
            title="📣  Update centre", colour=COLOUR,
            description="Compose an update, check who it reaches, then send.")
        draft = UP.draft_for(user.id)
        if draft:
            upd = S.get_update(draft) or {}
            e.add_field(name="Working on",
                        value=f"**{upd.get('title', '?')}**\n`{draft}`",
                        inline=False)
        w = self.worker
        e.set_footer(text=(f"worker: {w.sent_total} sent this run"
                           + (f" · busy on {w.current}" if w.current else "")))
        return e

    # ── /bugs and /suggest ───────────────────────────────────────────────────
    @app_commands.command(name="bugs",
                          description="Report a bug to the Beycord team")
    async def bugs(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(R.ReportModal(R.BUG))

    @app_commands.command(name="suggest",
                          description="Suggest an improvement to Beycord")
    async def suggest(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(R.ReportModal(R.IDEA))

    # ── Where the reports go ─────────────────────────────────────────────────
    @commands.command(name="reportchannel",
                      aliases=["bugchannel", "reportshere"],
                      hidden=True)
    async def reportchannel(self, ctx: commands.Context,
                            target: str = "") -> None:
        """`;reportchannel` — show it, set it here, or turn it off.

        The panel route works, but it is four clicks deep in a category named
        Announce, and a setting nobody can find is a setting nobody has set —
        which is how reports end up going nowhere.
        """
        from cogs.admin import actions as A
        if not A.is_admin(ctx.author):
            return
        from utils.database import set_report_channel
        arg = (target or "").strip().lower()
        if arg in ("off", "none", "clear", "unset"):
            set_report_channel(None)
            return await ctx.send(
                "🚫 Reports channel cleared. `/bugs` and `/suggest` will DM the "
                "owner instead — they are never dropped.")
        if arg in ("here", "this", "set"):
            set_report_channel(ctx.channel.id)
            return await ctx.send(
                f"✅ `/bugs` and `/suggest` from **every** server now land in "
                f"{ctx.channel.mention}.")
        await ctx.send(embed=reports_embed(self.bot),
                       view=ReportsView(self.bot))

    # ── Player preferences ───────────────────────────────────────────────────
    @commands.command(name="notifications",
                      aliases=["notify", "dms"],
                      brief="Choose which DMs you get 🔔")
    async def notifications(self, ctx: commands.Context) -> None:
        from utils.database import get_user
        profile = await get_user(ctx.author.id)
        await ctx.send(embed=discord.Embed(
            title="🔔  Your notifications", colour=COLOUR,
            description=P.summary(profile)),
            view=await PrefsView.create(ctx.author.id))


# ── Update Channel Configuration ─────────────────────────────────────────
    @commands.command(name="updatechannel",
                      aliases=["patchchannel", "announceto"],
                      brief="Set or clear the server's update announcement channel 📢")
    @commands.has_permissions(manage_guild=True)
    async def updatechannel(self, ctx: commands.Context, target: str = "") -> None:
        if not ctx.guild:
            return await ctx.send("This command must be run in a server.")
        from utils.database import get_announce_channel, set_announce_channel
        arg = (target or "").strip().lower()
        if arg in ("off", "none", "clear", "unset"):
            set_announce_channel(ctx.guild.id, None)
            return await ctx.send("🚫 Update channel cleared for this server.")
        if arg in ("here", "this", "set") or not target:
            set_announce_channel(ctx.guild.id, ctx.channel.id)
            return await ctx.send(f"📢 Server update announcements will now be posted in {ctx.channel.mention}!")
        try:
            ch = await commands.TextChannelConverter().convert(ctx, target)
            set_announce_channel(ctx.guild.id, ch.id)
            return await ctx.send(f"📢 Server update announcements will now be posted in {ch.mention}!")
        except Exception:
            cid = get_announce_channel(ctx.guild.id)
            ch = ctx.guild.get_channel(cid) if cid else None
            status = f"currently set to {ch.mention}" if ch else "not set yet"
            await ctx.send(f"📢 Update channel is {status}. Use `;updatechannel here` to set it to this channel.")

    @app_commands.command(name="updatechannel", description="Set or clear the server's update announcement channel")
    @app_commands.describe(channel="Channel to receive game update announcements", clear="Set to True to clear the channel")
    async def slash_updatechannel(self, interaction: discord.Interaction,
                                  channel: Optional[discord.TextChannel] = None,
                                  clear: bool = False) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        if not interaction.user.guild_permissions.manage_guild and not _is_owner(interaction.user):
            return await interaction.response.send_message("You need `Manage Server` permission to configure this.", ephemeral=True)
        from utils.database import get_announce_channel, set_announce_channel
        if clear:
            set_announce_channel(interaction.guild_id, None)
            return await interaction.response.send_message("🚫 Update channel cleared for this server.", ephemeral=True)
        if channel:
            set_announce_channel(interaction.guild_id, channel.id)
            return await interaction.response.send_message(f"📢 Server updates will now post in {channel.mention}!", ephemeral=True)
        cid = get_announce_channel(interaction.guild_id)
        ch = interaction.guild.get_channel(cid) if cid else None
        status = f"currently {ch.mention}" if ch else "not set yet"
        await interaction.response.send_message(f"📢 Update channel is {status}. Pick a channel to set it.", ephemeral=True)

    # ── Public /whatsnew and /patchnotes ─────────────────────────────────────
    @app_commands.command(name="whatsnew", description="Browse recent Beycord updates and patch notes")
    async def whatsnew(self, interaction: discord.Interaction) -> None:
        view = WhatsNewView(self.bot)
        await interaction.response.send_message(embed=view.build_embed(), view=view)

    @app_commands.command(name="patchnotes", description="Browse Beycord release notes and version history")
    async def patchnotes(self, interaction: discord.Interaction) -> None:
        view = WhatsNewView(self.bot)
        await interaction.response.send_message(embed=view.build_embed(), view=view)

    @commands.command(name="whatsnew", aliases=["patchnotes", "changelog"], brief="View recent updates 📣")
    async def prefix_whatsnew(self, ctx: commands.Context) -> None:
        view = WhatsNewView(self.bot)
        await ctx.send(embed=view.build_embed(), view=view)


def reports_embed(bot) -> discord.Embed:
    """Where reports land, said plainly, with the open count."""
    from utils.database import get_report_channel
    cid = get_report_channel()
    ch = bot.get_channel(cid) if cid else None
    if ch is not None:
        where = f"✅ {ch.mention} — in **{getattr(ch.guild, 'name', '?')}**"
    elif cid:
        where = (f"⚠️ Set to `{cid}`, which this bot cannot see. Reports are "
                 f"DMing the owner instead.")
    else:
        where = ("⚠️ **Not set.** Reports are DMing the owner instead, so "
                 "nothing is lost — but a channel is easier to work through.")
    e = discord.Embed(title="📥  Bug reports & suggestions", colour=COLOUR,
                      description=where)
    try:
        rows = S.open_reports(50)
        e.add_field(name="Open", value=f"**{len(rows)}** waiting",
                    inline=True)
    except Exception:                                    # noqa: BLE001
        pass
    e.set_footer(text="Reports from every server land in the one channel.")
    return e


class ReportsView(discord.ui.View):
    """One click to set the destination, one to clear it."""

    def __init__(self, bot) -> None:
        super().__init__(timeout=180)
        self.bot = bot

    async def _guard(self, interaction) -> bool:
        from cogs.admin import actions as A
        if A.is_admin(interaction.user):
            return True
        await interaction.response.send_message("Staff only.", ephemeral=True)
        return False

    @discord.ui.button(label="Send reports here", emoji="📥",
                       style=discord.ButtonStyle.success)
    async def here(self, interaction: discord.Interaction, _b) -> None:
        if not await self._guard(interaction):
            return
        from utils.database import set_report_channel
        set_report_channel(interaction.channel_id)
        await interaction.response.edit_message(
            embed=reports_embed(self.bot), view=ReportsView(self.bot))

    @discord.ui.button(label="Turn off", emoji="🚫",
                       style=discord.ButtonStyle.secondary)
    async def off(self, interaction: discord.Interaction, _b) -> None:
        if not await self._guard(interaction):
            return
        from utils.database import set_report_channel
        set_report_channel(None)
        await interaction.response.edit_message(
            embed=reports_embed(self.bot), view=ReportsView(self.bot))


class PrefsToggle(discord.ui.Button):
    def __init__(self, user_id: int, key: str, label: str, emoji: str,
                on: bool) -> None:
        # `on` is passed in rather than read here: `get_user` is `async def`
        # now (BUG-02) and a Button `__init__` cannot await — see
        # `PrefsView.create`, the async factory that resolves it up front.
        super().__init__(label=f"{label}: {'on' if on else 'off'}",
                         emoji=emoji,
                         style=(discord.ButtonStyle.success if on
                                else discord.ButtonStyle.secondary))
        self.user_id = int(user_id)
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "Those aren't your settings — run `;notifications` yourself.",
                ephemeral=True)
        from utils.database import get_user, update_user
        profile = await get_user(self.user_id)
        P.set_switch(profile, self.key, not P.get(profile, self.key))
        await update_user(self.user_id, profile)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🔔  Your notifications", colour=COLOUR,
                description=P.summary(await get_user(self.user_id))),
            view=await PrefsView.create(self.user_id))


class PrefsView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=180)
        self.user_id = int(user_id)

    @classmethod
    async def create(cls, user_id: int) -> "PrefsView":
        from utils.database import get_user
        profile = await get_user(user_id)
        view = cls(user_id)
        view.add_item(PrefsToggle(user_id, P.K_UPDATES, "Update DMs", "📣",
                                  P.get(profile, P.K_UPDATES)))
        view.add_item(PrefsToggle(user_id, P.K_EVENTS, "Event DMs", "🎉",
                                  P.get(profile, P.K_EVENTS)))
        return view


class UpdateMenu(discord.ui.View):
    """Create · Preview · Send · Status · Cancel · History."""

    def __init__(self, cog: NotificationCog) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.add_item(UpdateActions(cog))


class UpdateActions(discord.ui.Select):
    OPTIONS = [
        ("create",  "Create",  "compose a new update",           "📝"),
        ("preview", "Preview", "see the DM and who it reaches",  "👁️"),
        ("send",    "Send",    "queue it for delivery",          "🚀"),
        ("status",  "Status",  "how the current send is going",  "📊"),
        ("cancel",  "Cancel",  "stop a send in flight",          "🛑"),
        ("history", "History", "the last updates and their totals", "🗂️"),
        ("reports", "Reports", "where /bugs and /suggest land",     "📥"),
    ]

    def __init__(self, cog: NotificationCog) -> None:
        super().__init__(placeholder="Pick an action…", options=[
            discord.SelectOption(label=l, value=k, description=d, emoji=e)
            for k, l, d, e in self.OPTIONS])
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_owner(interaction.user):
            return await interaction.response.send_message(
                "That one isn't yours.", ephemeral=True)
        action = self.values[0]
        if action == "create":
            return await interaction.response.send_modal(
                UP.ComposeModal(self))

        await interaction.response.defer(ephemeral=True, thinking=True)
        draft = UP.draft_for(interaction.user.id)
        if action in ("preview", "send", "status", "cancel") and not draft:
            return await interaction.followup.send(
                "No draft yet — pick **📝 Create** first.", ephemeral=True)

        try:
            fn = getattr(self, f"_{action}")
            await fn(interaction, draft)
        except Exception:                                # noqa: BLE001
            log.exception("[updates] /update %s failed", action)
            await interaction.followup.send(
                f"⚠️ `{action}` failed — it's in the error log.",
                ephemeral=True)

    async def _preview(self, interaction, draft) -> None:
        # Shared with the Preview button on the compose screen itself —
        # see update_panel.preview_embeds — so there is one implementation of
        # what a preview actually shows, not two that can drift apart.
        embeds = UP.preview_embeds(self.cog.bot, draft)
        await interaction.followup.send(embeds=embeds, ephemeral=True)

    async def _send(self, interaction, draft) -> None:
        # Shared with the Send button on the compose screen — update_panel.py
        # is where "queue it" lives now; this is just the phrasing for
        # reaching it via the Select instead of the compose screen's button.
        stats = UP.send_stats(self.cog.bot, draft)
        self.cog.wake()
        await interaction.followup.send(
            f"🚀 Queued **{stats['queued']:,}** new deliveries "
            f"(of {stats['targeted']:,} targeted"
            + (f", {stats['dropped_no_beys']:,} dropped for owning no blades"
               if stats["dropped_no_beys"] else "")
            + ").\nThe worker is running — **📊 Status** to watch it.",
            ephemeral=True)

    async def _status(self, interaction, draft) -> None:
        c = S.counts(draft)
        upd = S.get_update(draft) or {}
        total = sum(c.values()) or 1
        rows = "\n".join(
            f"`{k:<8}` {v:>6,}  {'▰' * max(1, round(20 * v / total))}"
            for k, v in sorted(c.items(), key=lambda kv: -kv[1])) or "nothing queued"
        e = discord.Embed(title=f"📊  {upd.get('title', draft)}"[:250],
                          description=rows, colour=COLOUR)
        e.set_footer(text=f"state {upd.get('state', '?')} · "
                          f"{self.cog.worker.sent_total} sent this run")
        await interaction.followup.send(embed=e, ephemeral=True)

    async def _cancel(self, interaction, draft) -> None:
        dropped = SV.cancel(draft)
        await interaction.followup.send(
            f"🛑 Cancelled. **{dropped:,}** undelivered rows dropped; "
            f"anything already sent stays on the record.", ephemeral=True)

    async def _reports(self, interaction, draft) -> None:
        # Not gated on a draft: this is the reports destination, not an update.
        await interaction.followup.send(
            embed=reports_embed(self.cog.bot), view=ReportsView(self.cog.bot),
            ephemeral=True)

    async def _history(self, interaction, draft) -> None:
        rows = S.list_updates(8)
        if not rows:
            return await interaction.followup.send("Nothing sent yet.",
                                                   ephemeral=True)
        e = discord.Embed(title="🗂️  Recent updates", colour=COLOUR)
        for r in rows:
            c = S.counts(r["update_id"])
            emoji, label, _c = P.PRIORITY_LABEL.get(
                P.normalise(r.get("priority")), P.PRIORITY_LABEL[P.NORMAL])
            e.add_field(
                name=f"{emoji} {r.get('title', '?')}"[:256],
                value=(f"`{r['update_id']}` · {r.get('state', '?')}\n"
                       + (" · ".join(f"{k} {v}" for k, v in sorted(c.items()))
                          or "never queued")),
                inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NotificationCog(bot))
