"""
cogs/codes/builder.py — pick a code's rewards instead of typing a spec.

`;code create coins:5000 blade:Dranzer` still works — this is a second door
onto the exact same `parse_rewards`/`create_code` pair in `redeem.py`, not a
parallel implementation. Every "add a reward" step here builds one
`kind:value` token and feeds it through `parse_rewards`, so a reward added
by clicking through this picker and one typed in the admin text box are
validated and stored identically — there is exactly one place that decides
what a reward means.

Discord's Select options top out at 25, and there are 100+ Beyblades and 45+
avatars — nowhere near listable that way. So this is a category picker
(Select) feeding a text Modal per category, fuzzy-matched on submit the same
way `;giveavatar` and `blade:` already are, rather than a dropdown of names.
"""

from __future__ import annotations

import discord

from . import redeem as R


class _AddModal(discord.ui.Modal):
    """One text field, fed straight through `parse_rewards` as `kind:<value>`."""

    def __init__(self, view: "CodeBuilderView", kind: str, title: str,
                field_label: str, placeholder: str = "") -> None:
        super().__init__(title=title)
        self.view_ref = view
        self.kind = kind
        self.field = discord.ui.TextInput(
            label=field_label, placeholder=placeholder, max_length=100)
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = str(self.field.value or "").strip()
        rewards, err = R.parse_rewards(f"{self.kind}:{value}")
        if err:
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        self.view_ref.rewards.extend(rewards)
        self.view_ref.note_line = f"✅ Added {R.describe(rewards)}."
        await self.view_ref.refresh(interaction)


class _BossBeyModal(discord.ui.Modal, title="Add a boss bey copy"):
    def __init__(self, view: "CodeBuilderView") -> None:
        super().__init__()
        self.view_ref = view
        self.name = discord.ui.TextInput(
            label="Boss name",
            placeholder="Drakos, Argus, Lionheart, or Nemesis")
        self.grade = discord.ui.TextInput(
            label="Grade (optional — blank rolls normally)",
            placeholder="Flawed / Standard / Refined / Pristine / Flawless / Perfect",
            required=False, max_length=20)
        self.add_item(self.name)
        self.add_item(self.grade)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        spec = f"bossbey:{str(self.name.value or '').strip()}"
        grade = str(self.grade.value or "").strip()
        if grade:
            spec += f":{grade}"
        rewards, err = R.parse_rewards(spec)
        if err:
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        self.view_ref.rewards.extend(rewards)
        self.view_ref.note_line = f"✅ Added {R.describe(rewards)}."
        await self.view_ref.refresh(interaction)


class _MetaModal(discord.ui.Modal, title="Code details"):
    def __init__(self, view: "CodeBuilderView") -> None:
        super().__init__()
        self.view_ref = view
        self.uses = discord.ui.TextInput(
            label="Max uses (0 = unlimited)", placeholder="0",
            required=False, max_length=10)
        self.days = discord.ui.TextInput(
            label="Expires in days (0 = never)", placeholder="0",
            required=False, max_length=10)
        self.note = discord.ui.TextInput(
            label="Note (optional)", required=False, max_length=200,
            style=discord.TextStyle.paragraph)
        self.add_item(self.uses)
        self.add_item(self.days)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        uses_raw = str(self.uses.value or "").strip()
        days_raw = str(self.days.value or "").strip()
        self.view_ref.uses = int(uses_raw) if uses_raw.isdigit() else 0
        self.view_ref.days = int(days_raw) if days_raw.isdigit() else 0
        self.view_ref.note = str(self.note.value or "").strip()
        self.view_ref.note_line = "✅ Details updated."
        await self.view_ref.refresh(interaction)


class RewardKindSelect(discord.ui.Select):
    # (kind, option label, modal field label, placeholder)
    KINDS = [
        ("coins",   "🪙 Beycoins",     "Amount",         "e.g. 5000"),
        ("casino",  "🎰 Casino coins", "Amount",         "e.g. 2000"),
        ("blade",   "🌀 Beyblade",     "Beyblade name",  "e.g. Dranzer"),
        ("avatar",  "🖼️ Avatar card",  "Avatar name",    "e.g. Yuki"),
        ("premium", "👑 Premium pass", "pro / elite / legend", "pro"),
    ]

    def __init__(self, view: "CodeBuilderView") -> None:
        options = [discord.SelectOption(label=label, value=kind)
                  for kind, label, _f, _p in self.KINDS]
        options.append(discord.SelectOption(label="👹 Boss bey copy", value="bossbey"))
        super().__init__(placeholder="➕ Add a reward…", options=options, row=0)
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.view_ref.guard(interaction):
            return
        kind = self.values[0]
        if kind == "bossbey":
            return await interaction.response.send_modal(_BossBeyModal(self.view_ref))
        _kind, label, field_label, placeholder = next(
            row for row in self.KINDS if row[0] == kind)
        modal = _AddModal(self.view_ref, kind, f"Add {label}", field_label, placeholder)
        await interaction.response.send_modal(modal)


class CodeBuilderView(discord.ui.View):
    def __init__(self, bot, invoker_id: int) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.invoker_id = int(invoker_id)
        self.rewards: list[dict] = []
        self.uses = 0
        self.days = 0
        self.note = ""
        self.note_line = ""
        self.rebuild()

    async def guard(self, interaction: discord.Interaction) -> bool:
        from cogs.admin import actions as A
        if interaction.user.id != self.invoker_id or not A.is_admin(interaction.user):
            await interaction.response.send_message("That panel isn't yours.",
                                                    ephemeral=True)
            return False
        return True

    def rebuild(self) -> None:
        self.clear_items()
        self.add_item(RewardKindSelect(self))
        for item in (self.details, self.clear_rewards, self.create_code_btn):
            self.add_item(item)

    def embed(self) -> discord.Embed:
        e = discord.Embed(title="🎟️  Build a gift code", colour=0x9B59B6)
        e.add_field(name="Rewards so far",
                    value=R.describe(self.rewards) if self.rewards
                    else "none yet — pick one below",
                    inline=False)
        e.add_field(name="Uses", value=("unlimited" if not self.uses else str(self.uses)),
                    inline=True)
        e.add_field(name="Expires", value=("never" if not self.days else f"in {self.days}d"),
                    inline=True)
        if self.note:
            e.add_field(name="Note", value=self.note, inline=False)
        if self.note_line:
            e.add_field(name="​", value=self.note_line, inline=False)
        return e

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.rebuild()
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=self.embed(), view=self)
            else:
                await interaction.response.edit_message(embed=self.embed(), view=self)
        except Exception:                                    # noqa: BLE001
            pass

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Details (uses / expiry / note)", emoji="📝", row=1,
                       style=discord.ButtonStyle.secondary)
    async def details(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        await interaction.response.send_modal(_MetaModal(self))

    @discord.ui.button(label="Clear rewards", emoji="🗑️", row=1,
                       style=discord.ButtonStyle.danger)
    async def clear_rewards(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        self.rewards = []
        self.note_line = "🗑️ Cleared."
        await self.refresh(interaction)

    @discord.ui.button(label="Create code", emoji="✅", row=1,
                       style=discord.ButtonStyle.success)
    async def create_code_btn(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        if not self.rewards:
            self.note_line = "❌ Add at least one reward first."
            return await self.refresh(interaction)

        key, entry = R.create_code(self.rewards, self.uses, self.days, self.note,
                                   self.invoker_id)
        e = discord.Embed(
            title="🎟️  Code created", colour=0x2ECC71,
            description=f"## `{R._pretty(key)}`\n\n{R.describe(entry['rewards'])}")
        e.add_field(name="Uses", value=("unlimited" if not self.uses else str(self.uses)),
                    inline=True)
        e.add_field(name="Expires",
                    value=("never" if not entry["expires"]
                          else f"<t:{int(entry['expires'])}:R>"),
                    inline=True)
        if self.note:
            e.add_field(name="Note", value=self.note, inline=False)
        e.set_footer(text="Players claim it with ;redeem <code>")
        for child in self.children:
            child.disabled = True
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=e, view=self)
            else:
                await interaction.response.edit_message(embed=e, view=self)
        except Exception:                                    # noqa: BLE001
            pass
        self.stop()
