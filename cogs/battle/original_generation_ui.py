"""Private, round-bound skill selection for the shared live battle panel."""
import discord
from .original_generation import COSTS


class SkillPicker(discord.ui.View):
    def __init__(self, session, key):
        super().__init__(timeout=60)
        self.session, self.key, self.round = session, key, session.round
        card = session.avatar_cards[key]
        for slot, skill in enumerate(card['skills'], 1):
            button = discord.ui.Button(label=f"{skill['name']} · {COSTS[slot-1]}⚡"[:80], row=slot-1)
            button.disabled = session.original_generation.reason(key, slot) is not None

            async def callback(interaction, slot=slot, skill=skill):
                if str(interaction.user.id) != self.key:
                    await interaction.response.send_message('This is another player’s avatar.', ephemeral=True)
                    return
                if self.round != self.session.round:
                    await interaction.response.send_message('That round has ended. Open the current skill panel.', ephemeral=True)
                    return
                error = self.session.original_generation.select(self.key, slot)
                if error:
                    await interaction.response.send_message(error, ephemeral=True)
                    return
                await interaction.response.edit_message(content=f"Selected **{skill['name']}**. Now choose your normal move.\n{skill['description']}", view=None)
            button.callback = callback
            self.add_item(button)
