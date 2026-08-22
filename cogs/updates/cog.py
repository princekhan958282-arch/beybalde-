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

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import prefs as P
from . import reports as R
from . import service as SV
from . import store as S
from . import targeting as T
from . import update_panel as UP
from . import worker as W

log = logging.getLogger("beyblade_bot.updates")

COLOUR = 0x5865F2


def _is_owner(user) -> bool:
    from cogs.admin import actions as A
    return getattr(user, "id", None) == A.MASTER_ID


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
            self.bot.add_dynamic_items(R.ReportButton)
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

    # ── Player preferences ───────────────────────────────────────────────────
    @commands.command(name="notifications",
                      aliases=["notify", "dms"],
                      brief="Choose which DMs you get 🔔")
    async def notifications(self, ctx: commands.Context) -> None:
        from utils.database import get_user
        profile = get_user(ctx.author.id)
        await ctx.send(embed=discord.Embed(
            title="🔔  Your notifications", colour=COLOUR,
            description=P.summary(profile)), view=PrefsView(ctx.author.id))


class PrefsToggle(discord.ui.Button):
    def __init__(self, user_id: int, key: str, label: str, emoji: str) -> None:
        from utils.database import get_user
        on = P.get(get_user(user_id), key)
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
        profile = get_user(self.user_id)
        P.set_switch(profile, self.key, not P.get(profile, self.key))
        update_user(self.user_id, profile)
        await interaction.response.edit_message(
            embed=discord.Embed(title="🔔  Your notifications", colour=COLOUR,
                                description=P.summary(get_user(self.user_id))),
            view=PrefsView(self.user_id))


class PrefsView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=180)
        self.add_item(PrefsToggle(user_id, P.K_UPDATES, "Update DMs", "📣"))
        self.add_item(PrefsToggle(user_id, P.K_EVENTS, "Event DMs", "🎉"))


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
        upd = S.get_update(draft)
        res = T.resolve(self.cog.bot, upd.get("audience") or {})
        reach = T.reachable(self.cog.bot, res["ids"])
        e = discord.Embed(
            title="👁️  Preview", colour=COLOUR,
            description=(f"**{len(res['ids']):,}** targeted · "
                         f"**{len(reach):,}** reachable right now"))
        e.add_field(name="Audience",
                    value=T.describe(upd.get("audience") or {}), inline=False)
        if res["dropped_no_beys"]:
            # Said out loud, not swallowed: this filter removes real accounts.
            e.add_field(
                name="Filtered out",
                value=(f"**{res['dropped_no_beys']:,}** of "
                       f"{res['total_before']:,} own no blades and were "
                       f"dropped."),
                inline=False)
        gap = len(res["ids"]) - len(reach)
        if gap > 0:
            e.add_field(
                name="Unreachable",
                value=(f"**{gap:,}** share no server with the bot. They stay "
                       f"queued and will record as BLOCKED."),
                inline=False)
        await interaction.followup.send(
            embeds=[e, W.build_embed(upd)], ephemeral=True)

    async def _send(self, interaction, draft) -> None:
        stats = SV.queue(self.cog.bot, draft)
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
