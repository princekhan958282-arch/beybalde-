"""
battle/ui.py
------------
Discord UI components for the battle system.

ChallengeView  — accept/decline challenge prompt sent to the opponent.

The per-round control panel (_InChannelControlPanel) lives in session.py
because it holds a direct reference to BattleSession and would create a
circular import if placed here.
"""

import discord
from discord import ui


class ChallengeView(ui.View):
    """Accept / Decline view shown to the challenged player."""

    def __init__(self, challenger: discord.Member, opponent: discord.Member):
        super().__init__(timeout=30)
        self.challenger = challenger
        self.opponent   = opponent
        self.accepted   = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message(
                "⚠️ Only the challenged player can respond!", ephemeral=True
            )
            return False
        return True

    @ui.button(label="⚔ Honor Challenge", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: ui.Button) -> None:
        self.accepted = True
        self.stop()
        await interaction.response.defer()

    @ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: ui.Button) -> None:
        self.stop()
        await interaction.response.send_message(
            f"🚫 {self.opponent.display_name} declined the battle."
        )
