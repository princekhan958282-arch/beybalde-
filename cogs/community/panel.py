"""
cogs/community/panel.py — `/server`, the one admin surface for this layer.

Nine systems, one picker line. Everything configurable about the community
layer is reachable here: which server it runs in, the personality, whether XP
is on, where level-ups are announced, and the level -> role rewards.

Owner-gated with the same `MASTER_ID` check the rest of the bot uses, imported
from `cogs.admin.actions` rather than copied — that constant already exists in
six files and a seventh is how they drift.
"""

from __future__ import annotations

import logging

import discord

from . import config as C
from . import guard

log = logging.getLogger("beyblade_bot.community.panel")

COLOUR = 0x5865F2

PERSONALITIES = [
    ("FUNNY",    "Funny",    "jokes first, everything is a bit"),
    ("FRIENDLY", "Friendly", "warm, encouraging, no edge"),
    ("SAVAGE",   "Savage",   "sharp and teasing, still fond"),
    ("HYPE",     "Hype",     "loud, all caps, permanent finals energy"),
    ("SERIOUS",  "Serious",  "plain and businesslike"),
]


def is_owner(user) -> bool:
    from cogs.admin import actions as A
    return getattr(user, "id", None) == A.MASTER_ID


def build_embed(bot, invoker_guild=None) -> discord.Embed:
    """What `/server` shows: where the layer lives, and how it is tuned."""
    gid = guard.main_guild_id()
    guild = bot.get_guild(gid) if (bot and gid) else None
    if gid is None:
        where = ("⚠️ **No main server set.** Every community feature is off, "
                 "everywhere. Press **Set this server** to switch them on here.")
    elif guild is not None:
        where = f"✅ **{guild.name}** · `{gid}`"
    else:
        where = (f"⚠️ Set to `{gid}`, which I can't see. Community features "
                 f"are off until that's fixed.")

    e = discord.Embed(title="🏠  Community systems", colour=COLOUR,
                      description=where)
    if gid is not None:
        roles = C.level_roles()
        chan = C.get(C.K_ANNOUNCE)
        e.add_field(name="Personality",
                    value=str(C.get(C.K_PERSONALITY)).title(), inline=True)
        e.add_field(name="XP",
                    value="on" if C.get(C.K_XP_ENABLED, True) else "off",
                    inline=True)
        e.add_field(name="Level-ups",
                    value=(f"<#{chan}>" if chan else "in the channel they "
                           "levelled up in"), inline=True)
        e.add_field(
            name=f"Level roles ({len(roles)})",
            value=("\n".join(f"level **{lvl}** → <@&{rid}>"
                             for lvl, rid in sorted(roles.items())[:8])
                   or "none yet — pick a role below"),
            inline=False)
        if invoker_guild is not None and not guard.is_main(invoker_guild):
            e.set_footer(text="You're configuring the main server from "
                              "somewhere else.")
    return e


class LevelRoleModal(discord.ui.Modal, title="Level role"):
    """Which level earns the role that was just picked."""

    def __init__(self, panel: "ServerView", role: discord.Role) -> None:
        super().__init__(timeout=300)
        self.panel = panel
        self.role = role
        self.level = discord.ui.TextInput(
            label=f"Level that earns @{role.name}"[:45],
            placeholder="10", max_length=4, required=True)
        self.add_item(self.level)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            level = int(str(self.level.value).strip())
        except (TypeError, ValueError):
            return await interaction.response.send_message(
                "That isn't a number.", ephemeral=True)
        ok, message = self.panel.levels.set_level_role(
            guard.main_guild_id(), level, self.role)
        await interaction.response.send_message(message, ephemeral=True)
        if ok:
            await self.panel.refresh(interaction)


class PersonalitySelect(discord.ui.Select):
    def __init__(self, panel: "ServerView") -> None:
        current = str(C.get(C.K_PERSONALITY))
        super().__init__(
            placeholder="Personality…", row=0,
            options=[discord.SelectOption(label=label, value=key,
                                          description=blurb,
                                          default=(key == current))
                     for key, label, blurb in PERSONALITIES])
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.panel.guard(interaction):
            return
        C.put(C.K_PERSONALITY, self.values[0])
        await self.panel.refresh(interaction)


class LevelRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: "ServerView") -> None:
        super().__init__(placeholder="Add a level role…", row=1,
                         min_values=1, max_values=1)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.panel.guard(interaction):
            return
        role = self.values[0]
        ok, why = self.panel.levels.check_role(role.guild, role)
        if not ok:
            # Refused here rather than at grant time, so the mapping never
            # contains a role that cannot actually be given out.
            return await interaction.response.send_message(why, ephemeral=True)
        await interaction.response.send_modal(LevelRoleModal(self.panel, role))


class ServerView(discord.ui.View):
    """The `/server` panel. Owner only, checked on every interaction."""

    def __init__(self, bot, invoker_id: int, levels) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.invoker_id = int(invoker_id)
        self.levels = levels
        self.rebuild()

    def rebuild(self) -> None:
        self.clear_items()
        if guard.is_configured():
            self.add_item(PersonalitySelect(self))
            self.add_item(LevelRoleSelect(self))
        for item in (self.set_here, self.toggle_xp, self.announce_here,
                     self.clear_roles, self.unset):
            self.add_item(item)

    async def guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id or not is_owner(
                interaction.user):
            await interaction.response.send_message(
                "That panel isn't yours.", ephemeral=True)
            return False
        return True

    async def refresh(self, interaction: discord.Interaction) -> None:
        C.invalidate()
        self.rebuild()
        embed = build_embed(self.bot, interaction.guild)
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except Exception:                                # noqa: BLE001
            log.exception("[community] could not refresh /server")

    @discord.ui.button(label="Set this server", emoji="🏠", row=2,
                       style=discord.ButtonStyle.success)
    async def set_here(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        guard.set_main_guild(interaction.guild_id)
        await self.refresh(interaction)

    @discord.ui.button(label="XP on/off", emoji="✨", row=2,
                       style=discord.ButtonStyle.secondary)
    async def toggle_xp(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        C.put(C.K_XP_ENABLED, not C.get(C.K_XP_ENABLED, True))
        await self.refresh(interaction)

    @discord.ui.button(label="Announce level-ups here", emoji="📣", row=3,
                       style=discord.ButtonStyle.secondary)
    async def announce_here(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        C.put(C.K_ANNOUNCE, str(interaction.channel_id))
        await self.refresh(interaction)

    @discord.ui.button(label="Clear level roles", emoji="🧹", row=3,
                       style=discord.ButtonStyle.secondary)
    async def clear_roles(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        C.put(C.K_LEVEL_ROLES, {})
        await self.refresh(interaction)

    @discord.ui.button(label="Turn the whole layer off", emoji="🔒", row=4,
                       style=discord.ButtonStyle.danger)
    async def unset(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        guard.set_main_guild(None)
        await self.refresh(interaction)
