"""Make Horror settings selection state reliable across Discord interactions.

The first implementation stored server/channel/player only on one View object.
On mobile, later component interactions can arrive after the message has been
refreshed, which made the next callback behave as if no server had been picked.
Mirror IDs into the cog and restore them whenever the panel is reconstructed.
"""

from __future__ import annotations

import discord

from . import horror as h


def _store(panel) -> None:
    state = getattr(panel.cog, "_settings_state", None)
    if state is None:
        state = {}
        panel.cog._settings_state = state
    state[int(panel.owner.id)] = {
        "guild_id": getattr(panel.guild, "id", None),
        "channel_id": getattr(panel.channel, "id", None),
        "target_id": getattr(panel.target, "id", None),
    }


def _restore(panel) -> None:
    state = getattr(panel.cog, "_settings_state", {})
    saved = state.get(int(panel.owner.id), {})
    guild = panel.cog.bot.get_guild(int(saved.get("guild_id") or 0))
    if guild is None:
        return
    panel.guild = guild
    channel = guild.get_channel(int(saved.get("channel_id") or 0))
    if isinstance(channel, discord.TextChannel):
        panel.channel = channel
    target = guild.get_member(int(saved.get("target_id") or 0))
    if target is not None and not target.bot:
        panel.target = target


_orig_view_init = h.HorrorSettingsView.__init__


def _view_init(self, cog, owner):
    _orig_view_init(self, cog, owner)
    if not hasattr(cog, "_settings_state"):
        cog._settings_state = {}
    _restore(self)
    self.rebuild()


async def _server_callback(self, interaction: discord.Interaction):
    guild = self.panel.cog.bot.get_guild(int(self.values[0]))
    if guild is None:
        return await interaction.response.send_message(
            "❌ Server is unavailable.", ephemeral=True
        )
    changed = self.panel.guild is None or self.panel.guild.id != guild.id
    self.panel.guild = guild
    if changed:
        self.panel.channel = None
        self.panel.target = None
    _store(self.panel)
    self.panel.rebuild()
    await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


async def _channel_callback(self, interaction: discord.Interaction):
    _restore(self.panel)
    if not self.panel.guild:
        return await interaction.response.send_message(
            "❌ Select a server first.", ephemeral=True
        )
    channel = self.panel.guild.get_channel(int(self.values[0]))
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message(
            "❌ Channel is unavailable.", ephemeral=True
        )
    self.panel.channel = channel
    _store(self.panel)
    self.panel.rebuild()
    await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


async def _player_callback(self, interaction: discord.Interaction):
    _restore(self.panel)
    if not self.panel.guild:
        return await interaction.response.send_message(
            "❌ Select a server first.", ephemeral=True
        )
    uid = int(self.values[0])
    member = self.panel.guild.get_member(uid)
    if member is None:
        try:
            member = await self.panel.guild.fetch_member(uid)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            member = None
    if member is None or member.bot:
        return await interaction.response.send_message(
            "❌ Player is unavailable in the selected server.", ephemeral=True
        )
    self.panel.target = member
    _store(self.panel)
    self.panel.rebuild()
    await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


async def _player_id_submit(self, interaction: discord.Interaction):
    _restore(self.panel)
    if not self.panel.guild:
        return await interaction.response.send_message(
            "❌ Select a server first.", ephemeral=True
        )
    try:
        uid = int(str(self.player_id).strip())
    except ValueError:
        return await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True)

    member = self.panel.guild.get_member(uid)
    if member is None:
        try:
            member = await self.panel.guild.fetch_member(uid)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            member = None
    if member is None or member.bot:
        return await interaction.response.send_message(
            "❌ That player is not a member of the selected server.", ephemeral=True
        )

    self.panel.target = member
    _store(self.panel)
    self.panel.rebuild()
    await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


async def _player_id_button(self, interaction: discord.Interaction):
    _restore(self)
    if not self.guild:
        return await interaction.response.send_message(
            "❌ Select a server first.", ephemeral=True
        )
    await interaction.response.send_modal(h.PlayerIdModal(self))


async def _spawn(self, interaction: discord.Interaction):
    _restore(self)
    if not (self.guild and self.channel and self.target):
        return await interaction.response.send_message(
            "❌ Select a server, channel, and player first.", ephemeral=True
        )
    await interaction.response.defer(ephemeral=True, thinking=True)
    _ok, message = await self.cog.spawn_encounter(self.guild, self.channel, self.target)
    await interaction.followup.send(message, ephemeral=True)


async def setup(bot) -> None:
    h.HorrorSettingsView.__init__ = _view_init
    h.ServerSelect.callback = _server_callback
    h.ChannelSelect.callback = _channel_callback
    h.PlayerSelect.callback = _player_callback
    h.PlayerIdModal.on_submit = _player_id_submit
    h.HorrorSettingsView._player_id = _player_id_button
    h.HorrorSettingsView._spawn = _spawn
