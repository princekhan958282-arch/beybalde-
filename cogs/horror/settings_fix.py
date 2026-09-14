"""Compatibility fix for the owner-only Horror settings panel.

The original Horror cog compared every command/button interaction against one
hard-coded MASTER_ID.  If the live Discord application owner differed from
that ID, `;settings` silently returned and looked broken.  Keep MASTER_ID as a
fallback, but also trust discord.py's real application-owner check.
"""

from __future__ import annotations

import discord

from cogs.admin.actions import MASTER_ID
from . import horror as horror_module


async def _is_owner(bot, user) -> bool:
    if getattr(user, "id", None) == MASTER_ID:
        return True
    try:
        return bool(await bot.is_owner(user))
    except Exception:
        return False


async def _settings_interaction_check(self, interaction: discord.Interaction) -> bool:
    if await _is_owner(self.cog.bot, interaction.user):
        return True
    if interaction.response.is_done():
        await interaction.followup.send("❌ Owner-only settings.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Owner-only settings.", ephemeral=True)
    return False


async def _settings_callback(self, ctx) -> None:
    if not await _is_owner(self.bot, ctx.author):
        await ctx.send("❌ This settings panel is owner-only.", delete_after=15)
        return

    embed = discord.Embed(
        title="⚙️ BEYCORD OWNER SETTINGS",
        description="Choose what you want to configure.",
        color=0x2B2D31,
    )
    await ctx.send(
        embed=embed,
        view=horror_module.SettingsHomeView(self, ctx.author),
    )


async def setup(bot) -> None:
    # Patch the already-loaded Horror command/view classes in-place. This keeps
    # one canonical `;settings` command and avoids duplicate-command errors.
    horror_module.HorrorCog.settings.callback = _settings_callback
    horror_module.SettingsHomeView.interaction_check = _settings_interaction_check
    horror_module.HorrorSettingsView.interaction_check = _settings_interaction_check
