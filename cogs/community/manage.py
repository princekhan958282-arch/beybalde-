"""
cogs/community/manage.py — the panels that make polls and giveaways manageable.

Why this file exists
--------------------
v1.28 shipped `PollManager.close/cancel` and `GiveawayManager.end/reroll/cancel`
fully written and fully tested, and **nothing called them**. `end` and `close`
were reachable only from the expiry timer; `reroll` and `cancel` had no caller
in the bot at all. So a giveaway with no timer never ended, a bad winner could
not be replaced, and a poll posted by mistake could not be taken down — while
the suite reported 89 green checks over all of it.

Everything here is wiring those methods to a button. `tools/sim_community.py`
now has a reachability sweep that fails if a manager method loses its last
caller, so this cannot quietly rot back.

Both panels are ephemeral and driven by the person who opened them.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord

from . import giveaways as GV
from . import guard
from . import polls as PL

log = logging.getLogger("beyblade_bot.community.manage")

COLOUR = 0x5865F2
PICKER_LIMIT = 20          # Discord allows 25 options; leave headroom


def is_staff(user) -> bool:
    from cogs.admin import actions as A
    return A.is_admin(user)


def may_manage_poll(user, poll: dict) -> bool:
    """Staff, or the person who asked the question."""
    return is_staff(user) or str(getattr(user, "id", "")) == str(
        (poll or {}).get("author_id"))


def _label(text: str, limit: int = 90) -> str:
    text = " ".join(str(text or "").split())
    return (text[:limit - 1] + "…") if len(text) > limit else (text or "—")


class RowPicker(discord.ui.Select):
    """One select listing recent rows; remembers the choice on the panel."""

    def __init__(self, panel: "ManagePanel", rows: list[dict], id_key: str,
                 label_key: str, placeholder: str) -> None:
        self.panel = panel
        self.id_key = id_key
        options = []
        for row in rows[:PICKER_LIMIT]:
            state = str(row.get("state", "?"))
            options.append(discord.SelectOption(
                label=_label(row.get(label_key)),
                value=str(row.get(id_key)),
                description=f"{state.lower()} · {row.get(id_key)}"[:100],
                default=(str(row.get(id_key)) == str(panel.selected))))
        if not options:
            # An empty select is a Discord 400 — the v96 `;inv` outage. A
            # disabled placeholder row is the shape that does not 400.
            options = [discord.SelectOption(label="nothing yet", value="-")]
        super().__init__(placeholder=placeholder, options=options, row=0,
                         disabled=not rows)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.panel.guard(interaction):
            return
        self.panel.selected = self.values[0]
        await self.panel.refresh(interaction)


class ManagePanel(discord.ui.View):
    """Shared shell: pick a row, then act on it."""

    def __init__(self, cog, invoker_id: int) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.invoker_id = int(invoker_id)
        self.selected: Optional[str] = None
        self.note = ""

    async def guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "That panel isn't yours — run the command yourself.",
                ephemeral=True)
            return False
        return True

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
            log.exception("[community] could not refresh the manage panel")

    async def on_timeout(self) -> None:
        # Without this every button answers "This interaction failed" ten
        # minutes later, which is indistinguishable from the bot being down.
        for child in self.children:
            child.disabled = True

    def rebuild(self) -> None:                           # pragma: no cover
        raise NotImplementedError

    def embed(self) -> discord.Embed:                    # pragma: no cover
        raise NotImplementedError


# ══════════════════════════════════════════════════════════════════════════════
#  Polls
# ══════════════════════════════════════════════════════════════════════════════

class PollPanel(ManagePanel):
    def __init__(self, cog, invoker_id: int) -> None:
        super().__init__(cog, invoker_id)
        self.rebuild()

    @property
    def rows(self) -> list[dict]:
        return self.cog.polls.recent(guard.main_guild_id(), PICKER_LIMIT)

    def current(self) -> Optional[dict]:
        if not self.selected:
            return None
        return self.cog.polls.get(guard.main_guild_id(), self.selected)

    def rebuild(self) -> None:
        self.clear_items()
        self.add_item(RowPicker(self, self.rows, "poll_id", "question",
                                "Pick a poll to manage…"))
        for item in (self.create, self.close_now, self.cancel_it,
                     self.results):
            self.add_item(item)

    def embed(self) -> discord.Embed:
        e = discord.Embed(title="📊  Polls", colour=COLOUR)
        poll = self.current()
        if poll:
            res = self.cog.polls.results(guard.main_guild_id(),
                                         poll["poll_id"])
            e.description = (f"**{poll.get('question')}**\n"
                             f"`{poll.get('state')}` · {res['total']} vote(s)")
        else:
            e.description = ("Create a poll, or pick one above to close or "
                             "cancel it.")
        if self.note:
            e.add_field(name="​", value=self.note, inline=False)
        return e

    async def _act(self, interaction, needs_open: bool, fn) -> None:
        if not await self.guard(interaction):
            return
        poll = self.current()
        if not poll:
            self.note = "Pick a poll first."
            return await self.refresh(interaction)
        if not may_manage_poll(interaction.user, poll):
            self.note = "Only staff or whoever asked it can do that."
            return await self.refresh(interaction)
        if needs_open and poll.get("state") != PL.OPEN:
            self.note = "That poll is already finished."
            return await self.refresh(interaction)
        await fn(poll)
        await self.refresh(interaction)

    @discord.ui.button(label="Create", emoji="📝", row=1,
                       style=discord.ButtonStyle.primary)
    async def create(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        if not await self.cog.may_poll(interaction):
            return
        await interaction.response.send_modal(PollModal(self.cog, None, self))

    @discord.ui.button(label="Close now", emoji="🔒", row=1,
                       style=discord.ButtonStyle.success)
    async def close_now(self, interaction: discord.Interaction, _b) -> None:
        async def _close(poll):
            await self.cog.finish_poll(poll["poll_id"])
            self.note = "Closed, and the results are on the post."
        await self._act(interaction, True, _close)

    @discord.ui.button(label="Cancel", emoji="🗑️", row=1,
                       style=discord.ButtonStyle.danger)
    async def cancel_it(self, interaction: discord.Interaction, _b) -> None:
        async def _cancel(poll):
            ok = self.cog.polls.cancel(guard.main_guild_id(), poll["poll_id"])
            self.note = ("Cancelled — no result published."
                         if ok else "It was already finished.")
            if ok:
                await self.cog.repost_poll(poll["poll_id"], with_view=False)
        await self._act(interaction, True, _cancel)

    @discord.ui.button(label="Results", emoji="📈", row=1,
                       style=discord.ButtonStyle.secondary)
    async def results(self, interaction: discord.Interaction, _b) -> None:
        async def _show(poll):
            res = self.cog.polls.results(guard.main_guild_id(),
                                         poll["poll_id"])
            self.note = "\n".join(
                f"{PL.NUMBERS[i]} {label} — **{res['counts'][i]}**"
                for i, label in enumerate(res["options"])) or "no votes yet"
        await self._act(interaction, False, _show)


class PollModal(discord.ui.Modal, title="New poll"):
    def __init__(self, cog, duration: Optional[int],
                 panel: Optional[PollPanel] = None) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.duration = duration
        self.panel = panel
        self.question = discord.ui.TextInput(
            label="Question", max_length=PL.MAX_QUESTION, required=True,
            placeholder="Which blade should we buff?")
        self.options = discord.ui.TextInput(
            label="Choices — one per line", style=discord.TextStyle.paragraph,
            required=True, max_length=800,
            placeholder="Valkyrie\nSpriggan\nFafnir")
        self.hours = discord.ui.TextInput(
            label="Hours until it closes (blank = no timer)",
            required=False, max_length=4, placeholder="24")
        self.add_item(self.question)
        self.add_item(self.options)
        self.add_item(self.hours)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await guard.gate(interaction):
            return
        duration = self.duration
        raw = str(self.hours.value or "").strip()
        if raw:
            try:
                duration = max(0.05, float(raw)) * 3600.0
            except (TypeError, ValueError):
                return await interaction.response.send_message(
                    "Hours has to be a number.", ephemeral=True)
        lines = [ln.strip() for ln in str(self.options.value).splitlines()
                 if ln.strip()]
        try:
            poll = self.cog.polls.create(
                interaction.guild_id, interaction.user.id,
                str(self.question.value), lines, duration=duration)
        except PL.PollError as exc:
            return await interaction.response.send_message(str(exc),
                                                           ephemeral=True)
        await interaction.response.send_message(
            embed=PL.build_embed(poll), view=PL.view_for(poll))
        message = await interaction.original_response()
        self.cog.polls.attach(interaction.guild_id, poll["poll_id"],
                              message.channel.id, message.id)


# ══════════════════════════════════════════════════════════════════════════════
#  Giveaways
# ══════════════════════════════════════════════════════════════════════════════

class GiveawayPanel(ManagePanel):
    def __init__(self, cog, invoker_id: int) -> None:
        super().__init__(cog, invoker_id)
        self.rebuild()

    @property
    def rows(self) -> list[dict]:
        return self.cog.giveaways.recent(guard.main_guild_id(), PICKER_LIMIT)

    def current(self) -> Optional[dict]:
        if not self.selected:
            return None
        return self.cog.giveaways.get(guard.main_guild_id(), self.selected)

    def rebuild(self) -> None:
        self.clear_items()
        self.add_item(RowPicker(self, self.rows, "giveaway_id", "prize",
                                "Pick a giveaway to manage…"))
        for item in (self.create, self.end_now, self.reroll, self.cancel_it):
            self.add_item(item)

    def embed(self) -> discord.Embed:
        e = discord.Embed(title="🎉  Giveaways", colour=GV.COLOUR)
        row = self.current()
        if row:
            entries = self.cog.giveaways.entries(guard.main_guild_id(),
                                                 row["giveaway_id"])
            won = row.get("winner_ids") or []
            e.description = (
                f"**{row.get('prize')}**\n`{row.get('state')}` · "
                f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} · "
                f"{int(row.get('winners') or 1)} winner(s)")
            if won:
                e.add_field(name="Drawn so far",
                            value=", ".join(f"<@{w}>" for w in won[:20]),
                            inline=False)
        else:
            e.description = ("Create one, or pick a giveaway above to end, "
                             "reroll or cancel it.")
        if self.note:
            e.add_field(name="​", value=self.note, inline=False)
        return e

    async def _act(self, interaction, fn) -> None:
        if not await self.guard(interaction):
            return
        if not is_staff(interaction.user):
            self.note = "Only staff can run giveaways."
            return await self.refresh(interaction)
        row = self.current()
        if not row:
            self.note = "Pick a giveaway first."
            return await self.refresh(interaction)
        await fn(row)
        await self.refresh(interaction)

    @discord.ui.button(label="Create", emoji="📝", row=1,
                       style=discord.ButtonStyle.primary)
    async def create(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "Only staff can run giveaways.", ephemeral=True)
        await interaction.response.send_modal(
            GiveawayModal(self.cog, 3600, self))

    @discord.ui.button(label="End now", emoji="🏁", row=1,
                       style=discord.ButtonStyle.success)
    async def end_now(self, interaction: discord.Interaction, _b) -> None:
        async def _end(row):
            out = await self.cog.finish_giveaway(row["giveaway_id"])
            if out.get("ok"):
                winners = out.get("winners") or []
                self.note = ("Ended — " + (
                    ", ".join(f"<@{w}>" for w in winners) + " won."
                    if winners else "nobody had entered."))
            else:
                self.note = out.get("why", "It was already finished.")
        await self._act(interaction, _end)

    @discord.ui.button(label="Reroll", emoji="🎲", row=1,
                       style=discord.ButtonStyle.secondary)
    async def reroll(self, interaction: discord.Interaction, _b) -> None:
        async def _reroll(row):
            out = self.cog.giveaways.reroll(guard.main_guild_id(),
                                            row["giveaway_id"], 1)
            if not out.get("ok"):
                self.note = out.get("why", "Couldn't reroll.")
                return
            winners = out["winners"]
            self.note = "Rerolled — " + ", ".join(
                f"<@{w}>" for w in winners) + " won instead."
            await self.cog.announce_winners(row["giveaway_id"], winners,
                                            rerolled=True)
        await self._act(interaction, _reroll)

    @discord.ui.button(label="Cancel", emoji="🗑️", row=1,
                       style=discord.ButtonStyle.danger)
    async def cancel_it(self, interaction: discord.Interaction, _b) -> None:
        async def _cancel(row):
            ok = self.cog.giveaways.cancel(guard.main_guild_id(),
                                           row["giveaway_id"])
            self.note = ("Cancelled — nobody was drawn."
                         if ok else "It was already finished.")
            if ok:
                await self.cog.repost_giveaway(row["giveaway_id"],
                                               with_view=False)
        await self._act(interaction, _cancel)


class GiveawayModal(discord.ui.Modal, title="New giveaway"):
    def __init__(self, cog, duration: int,
                 panel: Optional[GiveawayPanel] = None) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.duration = duration
        self.panel = panel
        self.prize = discord.ui.TextInput(
            label="Prize", max_length=GV.MAX_PRIZE, required=True,
            placeholder="Ultimate Valkyrie")
        self.winners = discord.ui.TextInput(
            label="How many winners", max_length=2, required=False,
            default="1")
        self.hours = discord.ui.TextInput(
            label="Hours it runs (blank = until you end it)",
            required=False, max_length=4, placeholder="24")
        self.add_item(self.prize)
        self.add_item(self.winners)
        self.add_item(self.hours)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await guard.gate(interaction):
            return
        try:
            count = int(str(self.winners.value or "1").strip() or 1)
        except (TypeError, ValueError):
            count = 1
        # Blank means no timer — and that is now a real option, because End now
        # exists. Before this panel it would have been a giveaway with no way
        # to finish.
        duration: Optional[float] = None
        raw = str(self.hours.value or "").strip()
        if raw:
            try:
                duration = max(0.05, float(raw)) * 3600.0
            except (TypeError, ValueError):
                return await interaction.response.send_message(
                    "Hours has to be a number.", ephemeral=True)
        try:
            row = self.cog.giveaways.create(
                interaction.guild_id, interaction.user.id,
                str(self.prize.value), duration=duration, winners=count)
        except GV.GiveawayError as exc:
            return await interaction.response.send_message(str(exc),
                                                           ephemeral=True)
        await interaction.response.send_message(
            embed=GV.build_embed(row, 0),
            view=GV.view_for(row["giveaway_id"]))
        message = await interaction.original_response()
        self.cog.giveaways.attach(interaction.guild_id, row["giveaway_id"],
                                  message.channel.id, message.id)
