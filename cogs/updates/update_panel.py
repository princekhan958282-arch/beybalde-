"""
cogs/updates/update_panel.py — `/update`, as one picker line.

Create · Preview · Send · Status · Cancel · History

Six operations behind one command rather than six subcommands, because Discord
lists a group's subcommands FLAT: `/update create`, `/update send` and four
more would be six lines in every player's picker for a feature only the owner
can run. v1.14 removed the `/player` group for exactly that reason.

The panel keeps the DRAFT id per admin, so Create → Preview → Send is a
conversation rather than six commands that each need the id retyped.
"""

from __future__ import annotations

import logging

import discord

from . import prefs as P
from . import service as SV
from . import store as S
from . import targeting as T

log = logging.getLogger("beyblade_bot.updates.panel")

COLOUR = 0x5865F2

# The draft each admin is working on. In memory on purpose: it is a UI cursor,
# not data. The update itself is in the table the moment Create finishes, so a
# restart costs the admin a click and nothing else.
_drafts: dict[int, str] = {}


def draft_for(user_id: int) -> str | None:
    return _drafts.get(int(user_id))


def set_draft(user_id: int, update_id: str) -> None:
    _drafts[int(user_id)] = update_id


class ComposeModal(discord.ui.Modal, title="📣 New update"):
    def __init__(self, panel) -> None:
        super().__init__(timeout=900)
        self.panel = panel
        from utils.buildinfo import VERSION
        try:
            from cogs.admin.actions import update_note
            drafted = update_note()
        except Exception:                                # noqa: BLE001
            drafted = f"Beycord is now on **{VERSION}**."
        self.title_in = discord.ui.TextInput(
            label="Title", max_length=140, required=True,
            placeholder=f"Beycord {VERSION}")
        self.body_in = discord.ui.TextInput(
            label="What changed?", style=discord.TextStyle.paragraph,
            max_length=1800, required=True, default=drafted)
        self.image_in = discord.ui.TextInput(
            label="Image URL (optional)", required=False, max_length=400)
        for item in (self.title_in, self.body_in, self.image_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        row = SV.create(
            event="UPDATE_RELEASED",
            title=str(self.title_in.value).strip(),
            body=str(self.body_in.value).strip(),
            image_url=(str(self.image_in.value).strip() or None),
            priority=P.NORMAL,
            audience={"kind": T.ALL, "skip_no_beys": True},
            created_by=interaction.user.id)
        set_draft(interaction.user.id, row["update_id"])
        from utils.buildinfo import VERSION
        S.put_update({**row, "version": VERSION})
        await interaction.response.send_message(
            embed=_draft_card(row["update_id"]),
            view=ComposeView(row["update_id"]), ephemeral=True)


class PrioritySelect(discord.ui.Select):
    def __init__(self, update_id: str) -> None:
        upd = S.get_update(update_id) or {}
        cur = P.normalise(upd.get("priority"))
        opts = []
        for p in P.PRIORITIES:
            emoji, label, _c = P.PRIORITY_LABEL[p]
            note = ("always delivered, ignores opt-out"
                    if P.overrides_optout(p) else "respects the player's switch")
            opts.append(discord.SelectOption(
                label=label, value=p, emoji=emoji, description=note,
                default=(p == cur)))
        super().__init__(placeholder="Priority…", options=opts, row=0)
        self.update_id = update_id

    async def callback(self, interaction: discord.Interaction) -> None:
        upd = S.get_update(self.update_id)
        if upd:
            S.put_update({**upd, "priority": self.values[0]})
        await interaction.response.edit_message(
            embed=_draft_card(self.update_id),
            view=ComposeView(self.update_id))


class AudienceSelect(discord.ui.Select):
    def __init__(self, update_id: str) -> None:
        upd = S.get_update(update_id) or {}
        cur = (upd.get("audience") or {}).get("kind", T.ALL)
        opts = []
        for kind in (T.ALL, T.ACTIVE, T.NEW):
            emoji, label = T.LABEL[kind]
            opts.append(discord.SelectOption(
                label=label, value=kind, emoji=emoji,
                default=(kind == cur)))
        super().__init__(placeholder="Who gets it…", options=opts, row=1)
        self.update_id = update_id

    async def callback(self, interaction: discord.Interaction) -> None:
        upd = S.get_update(self.update_id)
        if upd:
            aud = dict(upd.get("audience") or {})
            aud["kind"] = self.values[0]
            aud.setdefault("skip_no_beys", True)
            S.put_update({**upd, "audience": aud})
        await interaction.response.edit_message(
            embed=_draft_card(self.update_id),
            view=ComposeView(self.update_id))


class SkipNoBeysButton(discord.ui.Button):
    def __init__(self, update_id: str) -> None:
        upd = S.get_update(update_id) or {}
        on = (upd.get("audience") or {}).get("skip_no_beys", True)
        super().__init__(
            label=("Skipping players with no blades" if on
                   else "Including players with no blades"),
            emoji=("🚫" if on else "🌍"), row=2,
            style=(discord.ButtonStyle.secondary if on
                   else discord.ButtonStyle.primary))
        self.update_id = update_id

    async def callback(self, interaction: discord.Interaction) -> None:
        upd = S.get_update(self.update_id)
        if upd:
            aud = dict(upd.get("audience") or {})
            aud["skip_no_beys"] = not aud.get("skip_no_beys", True)
            S.put_update({**upd, "audience": aud})
        await interaction.response.edit_message(
            embed=_draft_card(self.update_id),
            view=ComposeView(self.update_id))


class ComposeView(discord.ui.View):
    def __init__(self, update_id: str) -> None:
        super().__init__(timeout=900)
        self.add_item(PrioritySelect(update_id))
        self.add_item(AudienceSelect(update_id))
        self.add_item(SkipNoBeysButton(update_id))


def _draft_card(update_id: str) -> discord.Embed:
    upd = S.get_update(update_id) or {}
    emoji, label, colour = P.PRIORITY_LABEL.get(
        P.normalise(upd.get("priority")), P.PRIORITY_LABEL[P.NORMAL])
    e = discord.Embed(title=f"📝 Draft · {upd.get('title', '?')}"[:250],
                      description=(upd.get("body") or "")[:1000],
                      colour=colour)
    e.add_field(name="Priority", value=f"{emoji} {label}", inline=True)
    e.add_field(name="Audience",
                value=T.describe(upd.get("audience") or {}), inline=False)
    e.set_footer(text=f"{update_id} · Preview it before you send.")
    return e
