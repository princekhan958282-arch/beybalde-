"""
cogs/snapshots/panel.py — pick a backup, see what is in it, put it back.

A restore is the most destructive button in the bot, so the shape here is
deliberate:

* **You see what you are about to apply** — date, profiles, beys, avatars,
  community levels — read from the file itself rather than its name.
* **You choose how much of it to apply.** Restoring everything rolls a player
  back wholesale, including coins they earned this morning. Restoring `beys`
  puts a lost collection back and leaves the rest of today alone.
* **The current state is saved first.** Every restore writes a `pre-restore`
  snapshot before it touches anything, so a restore you regret is itself
  undoable. That file is listed here like any other.
* **Two presses.** The first arms it and says what will happen; the second does
  it.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import discord

from utils import snapshot as SN

log = logging.getLogger("beyblade_bot.snapshot.panel")

COLOUR = 0x2ECC71
PICKER_LIMIT = 20


def _when(ts: float) -> str:
    return f"<t:{int(ts)}:R>" if ts else "—"


class SnapshotPicker(discord.ui.Select):
    def __init__(self, panel: "SnapshotPanel") -> None:
        self.panel = panel
        rows = SN.listing(panel.folder)[:PICKER_LIMIT]
        options = []
        for row in rows:
            options.append(discord.SelectOption(
                label=row["name"][:100],
                value=row["path"][-100:],
                description=f"{row['kind']} · {row['size'] // 1024 or 1} KB",
                default=(row["path"] == panel.selected)))
        if not options:
            # An empty select is a Discord 400 — the v96 `;inv` outage.
            options = [discord.SelectOption(label="no snapshots yet",
                                            value="-")]
        super().__init__(placeholder="Pick a snapshot…", options=options,
                         row=0, disabled=not rows)
        self._paths = {row["path"][-100:]: row["path"] for row in rows}

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.panel.guard(interaction):
            return
        self.panel.selected = self._paths.get(self.values[0])
        self.panel.armed = False
        await self.panel.refresh(interaction)


class SectionSelect(discord.ui.Select):
    def __init__(self, panel: "SnapshotPanel") -> None:
        self.panel = panel
        options = [discord.SelectOption(
            label="Everything", value=SN.ALL,
            description="replace each profile wholesale",
            default=(panel.sections == [SN.ALL]))]
        blurb = {
            "beys": "collections, levels, parts, boss copies",
            "avatars": "owned avatars and the equipped one",
            "community": "community XP and level",
            "economy": "coins and the casino wallet",
            "ranked": "rank score, wins, streaks",
            "story": "School League progress",
            "trainer": "trainer XP, level, quests",
        }
        for name in SN.SECTIONS:
            options.append(discord.SelectOption(
                label=name.title(), value=name,
                description=blurb.get(name, "")[:100],
                default=(name in panel.sections)))
        super().__init__(placeholder="What to restore…", options=options,
                         row=1, min_values=1, max_values=len(options))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.panel.guard(interaction):
            return
        picked = list(self.values)
        # "Everything" alongside a section is a contradiction; the broader one
        # wins rather than the panel guessing.
        self.panel.sections = [SN.ALL] if SN.ALL in picked else picked
        self.panel.armed = False
        await self.panel.refresh(interaction)


class SnapshotPanel(discord.ui.View):
    def __init__(self, bot, invoker_id: int,
                 folder: Optional[str] = None) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.invoker_id = int(invoker_id)
        self.folder = folder or SN.folder()
        self.selected: Optional[str] = None
        self.sections: list[str] = [SN.ALL]
        self.armed = False
        self.note = ""
        self.parent: Optional[discord.InteractionMessage] = None
        self.rebuild()

    # ── plumbing ─────────────────────────────────────────────────────────────
    async def guard(self, interaction: discord.Interaction) -> bool:
        from cogs.admin import actions as A
        if interaction.user.id != self.invoker_id or not A.is_admin(
                interaction.user):
            await interaction.response.send_message("That panel isn't yours.",
                                                    ephemeral=True)
            return False
        return True

    def rebuild(self) -> None:
        self.clear_items()
        self.add_item(SnapshotPicker(self))
        self.add_item(SectionSelect(self))
        for item in (self.take_now, self.download, self.restore_it,
                     self.delete_it):
            self.add_item(item)
        self.restore_it.style = (discord.ButtonStyle.danger if self.armed
                                 else discord.ButtonStyle.secondary)
        self.restore_it.label = ("Yes — restore it" if self.armed
                                 else "Restore")

    def current(self) -> Optional[dict]:
        if not self.selected or not os.path.exists(self.selected):
            return None
        try:
            return SN.read(self.selected)
        except Exception:                                # noqa: BLE001
            log.exception("[snapshot] could not read %s", self.selected)
            return None

    def embed(self) -> discord.Embed:
        from .clock import last_run, next_due
        rows = SN.listing(self.folder)
        e = discord.Embed(title="💾  Backups", colour=COLOUR)
        e.description = (
            f"**{len(rows)}** snapshot(s) in `backups/`\n"
            f"Last taken {_when(last_run())} · next in "
            f"{int(next_due() // 3600)}h")
        snap = self.current()
        if snap:
            d = SN.describe(snap)
            e.add_field(
                name=os.path.basename(self.selected or ""),
                value=(f"taken {_when(d['taken_at'])} · from `{d['backend']}`\n"
                       f"**{d['profiles']:,}** profiles · "
                       f"**{d['beys']:,}** beys · "
                       f"**{d['avatars']:,}** avatars · "
                       f"**{d['community_levelled']:,}** at community level\n"
                       f"files: {', '.join(d['files']) or 'none'}"),
                inline=False)
            e.add_field(name="Will restore",
                        value=", ".join(self.sections), inline=False)
        else:
            e.add_field(name="​",
                        value="Pick a snapshot above to see what's in it.",
                        inline=False)
        e.add_field(name="GitHub", value=self._github_status(), inline=False)
        if self.note:
            e.add_field(name="​", value=self.note, inline=False)
        return e

    def _github_status(self) -> str:
        from utils import github_backup as GB
        token, repo = GB.configured()
        if not token or not repo:
            return "⚪ not configured — local backup only"
        cog = self.bot.get_cog("Snapshots") if self.bot else None
        gh = getattr(cog, "last_github", None) if cog else None
        if not gh:
            return f"⚪ configured for `{repo}` — no push yet this run"
        if gh.get("ok"):
            return f"✅ last push OK — `{repo}`"
        return f"⚠️ last push failed — {gh.get('error', '?')}"

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.rebuild()
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=self.embed(),
                                                         view=self)
            else:
                await interaction.response.edit_message(embed=self.embed(),
                                                        view=self)
        except Exception:                                # noqa: BLE001
            log.exception("[snapshot] could not refresh the panel")

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.parent is not None:
            try:
                await self.parent.edit(view=self)
            except Exception:                            # noqa: BLE001
                pass

    # ── buttons ──────────────────────────────────────────────────────────────
    @discord.ui.button(label="Snapshot now", emoji="💾", row=2,
                       style=discord.ButtonStyle.success)
    async def take_now(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        await interaction.response.defer()
        import asyncio
        try:
            # Routed through the cog, not duplicated here, so a snapshot
            # taken from this button gets the same GitHub push as one taken
            # from the admin registry's "Take a backup now" tile — the two
            # used to diverge, and only one of them reached GitHub.
            cog = self.bot.get_cog("Snapshots") if self.bot else None
            if cog is not None:
                path = await cog.take("admin")
            else:
                path = await asyncio.to_thread(SN.write, self.folder)
                from .clock import mark
                mark()
                SN.prune(self.folder)
            self.selected = path
            self.note = (f"✅ Took a snapshot — "
                         f"`{os.path.basename(path)}`, "
                         f"{os.path.getsize(path) // 1024 or 1} KB.")
        except Exception as exc:                         # noqa: BLE001
            log.exception("[snapshot] manual snapshot failed")
            self.note = f"⚠️ Couldn't take one: `{type(exc).__name__}`."
        self.armed = False
        await self.refresh(interaction)

    @discord.ui.button(label="Download", emoji="📥", row=2,
                       style=discord.ButtonStyle.primary)
    async def download(self, interaction: discord.Interaction, _b) -> None:
        """Send the file to the admin. The cheapest off-machine copy there is.

        `backups/` lives on the same disk as the bot, so it does not survive
        the container being rebuilt. A copy sitting in your DMs does.
        """
        if not await self.guard(interaction):
            return
        target = self.selected or os.path.join(self.folder, SN.LATEST)
        if not os.path.exists(target):
            self.note = "Nothing to download yet — take a snapshot first."
            return await self.refresh(interaction)
        await interaction.response.defer()
        try:
            await interaction.followup.send(
                content=("Keep this somewhere off the machine — it is the only "
                         "copy that survives a container rebuild."),
                file=discord.File(target, filename=os.path.basename(target)),
                ephemeral=True)
            self.note = f"📥 Sent `{os.path.basename(target)}`."
        except Exception as exc:                         # noqa: BLE001
            log.exception("[snapshot] could not send the file")
            self.note = f"⚠️ Couldn't send it: `{type(exc).__name__}`."
        await self.refresh(interaction)

    @discord.ui.button(label="Restore", emoji="♻️", row=2,
                       style=discord.ButtonStyle.secondary)
    async def restore_it(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        snap = self.current()
        if not snap:
            self.note = "Pick a snapshot first."
            self.armed = False
            return await self.refresh(interaction)

        if not self.armed:
            # First press arms it and says exactly what is about to happen.
            d = SN.describe(snap)
            what = ("every field of every profile in the file"
                    if self.sections == [SN.ALL]
                    else "only " + ", ".join(self.sections))
            self.armed = True
            self.note = (f"⚠️ This will overwrite **{what}** for "
                         f"**{d['profiles']:,}** profiles.\n"
                         f"The current state is saved to `pre-restore/` first. "
                         f"Press again to go ahead.")
            return await self.refresh(interaction)

        await interaction.response.defer()
        import asyncio
        try:
            # The safety net, before anything is touched.
            safety = await asyncio.to_thread(
                SN.write, self.folder, None,
                **{"kind": SN.PRE_RESTORE_DIR, "also_latest": False})
            out = await SN.restore(snap, self.sections)
            self.note = (
                f"♻️ Restored **{out['profiles']:,}** profile(s) "
                f"({', '.join(out['sections'])})"
                + (f", files: {', '.join(out['files'])}" if out["files"] else "")
                + f".\nPrevious state saved as `{os.path.basename(safety)}` — "
                  f"select it to undo this.")
            log.warning("[snapshot] %s restored %s (%s)",
                        interaction.user.id, self.selected, out["sections"])
        except Exception as exc:                         # noqa: BLE001
            log.exception("[snapshot] restore failed")
            self.note = f"⚠️ Restore failed: `{type(exc).__name__}`."
        self.armed = False
        await self.refresh(interaction)

    @discord.ui.button(label="Delete", emoji="🗑️", row=3,
                       style=discord.ButtonStyle.secondary)
    async def delete_it(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        if not self.selected or not os.path.exists(self.selected):
            self.note = "Pick a snapshot first."
            return await self.refresh(interaction)
        name = os.path.basename(self.selected)
        try:
            os.remove(self.selected)
            self.note = f"🗑️ Deleted `{name}`."
        except OSError as exc:
            self.note = f"⚠️ Couldn't delete it: `{exc}`."
        self.selected = None
        self.armed = False
        await self.refresh(interaction)
