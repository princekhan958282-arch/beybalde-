"""
cogs/battle/skill_views.py
--------------------------
Discord UI Views for interactive Blader and Bit Beast skill selection.
Shown ephemerally to each player upon clicking the battle control buttons.
"""

from __future__ import annotations

import discord
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session import BattleSession


class BladerSkillPicker(discord.ui.View):
    """Ephemeral selection view for Blader skills."""

    def __init__(self, session: "BattleSession", key: str) -> None:
        super().__init__(timeout=60)
        self.session = session
        self.key = key
        self.round = session.round

        sm = getattr(session, "skills_manager", None) or getattr(session, "original_generation", None)
        skills = sm.get_blader_skills(key) if sm else []
        current_energy = sm.get_blader_energy(key) if sm else 0

        for skill in skills:
            slot = skill["slot"]
            cost = skill["cost"]
            name = skill["name"]
            disabled = False
            if sm:
                ok, _ = sm.can_activate_blader_skill(key, slot)
                disabled = not ok

            button = discord.ui.Button(
                label=f"{name} · {cost}⚡"[:80],
                style=discord.ButtonStyle.primary if not disabled else discord.ButtonStyle.secondary,
                disabled=disabled,
                row=slot - 1,
            )

            async def callback(interaction: discord.Interaction, slot=slot, sk=skill) -> None:
                if str(interaction.user.id) != self.key:
                    await interaction.response.send_message("❌ This is another player's skill menu.", ephemeral=True)
                    return
                if self.round != self.session.round:
                    await interaction.response.send_message("⚠️ That round has ended. Please use the current battle panel.", ephemeral=True)
                    return

                skills_mgr = getattr(self.session, "skills_manager", None) or getattr(self.session, "original_generation", None)
                if not skills_mgr:
                    await interaction.response.send_message("⚠️ Skill system unavailable.", ephemeral=True)
                    return

                ok, msg = skills_mgr.select_blader_skill(self.key, slot)
                if not ok:
                    await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
                    return

                rem = skills_mgr.get_blader_energy(self.key)
                desc = sk.get("description", "")
                await interaction.response.edit_message(
                    content=(
                        f"✅ Activated Blader Skill: **{sk['name']}** (-{sk['cost']}⚡)\n"
                        f"Remaining Blader Energy: **{rem}/100⚡**\n"
                        f"*{desc}*\n\n"
                        f"👉 Now choose your combat move on the battle panel."
                    ),
                    view=None,
                )

            button.callback = callback
            self.add_item(button)


class BitBeastSkillPicker(discord.ui.View):
    """Ephemeral selection view for Bit Beast skills."""

    def __init__(self, session: "BattleSession", key: str) -> None:
        super().__init__(timeout=60)
        self.session = session
        self.key = key
        self.round = session.round

        sm = getattr(session, "skills_manager", None) or getattr(session, "original_generation", None)
        skills = sm.get_bit_beast_skills(key) if sm else []
        current_energy = sm.get_bit_beast_energy(key) if sm else 0

        for skill in skills:
            slot = skill["slot"]
            cost = skill["cost"]
            name = skill["name"]
            disabled = False
            if sm:
                ok, _ = sm.can_activate_bit_beast_skill(key, slot)
                disabled = not ok

            button = discord.ui.Button(
                label=f"{name} · {cost}⚡"[:80],
                style=discord.ButtonStyle.success if not disabled else discord.ButtonStyle.secondary,
                disabled=disabled,
                row=slot - 1,
            )

            async def callback(interaction: discord.Interaction, slot=slot, sk=skill) -> None:
                if str(interaction.user.id) != self.key:
                    await interaction.response.send_message("❌ This is another player's skill menu.", ephemeral=True)
                    return
                if self.round != self.session.round:
                    await interaction.response.send_message("⚠️ That round has ended. Please use the current battle panel.", ephemeral=True)
                    return

                skills_mgr = getattr(self.session, "skills_manager", None) or getattr(self.session, "original_generation", None)
                if not skills_mgr:
                    await interaction.response.send_message("⚠️ Skill system unavailable.", ephemeral=True)
                    return

                ok, msg = skills_mgr.select_bit_beast_skill(self.key, slot)
                if not ok:
                    await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
                    return

                rem = skills_mgr.get_bit_beast_energy(self.key)
                desc = sk.get("description", "")
                await interaction.response.edit_message(
                    content=(
                        f"✅ Activated Bit Beast Skill: **{sk['name']}** (-{sk['cost']}⚡)\n"
                        f"Remaining Bit Beast Energy: **{rem}/100⚡**\n"
                        f"*{desc}*\n\n"
                        f"👉 Now choose your combat move on the battle panel."
                    ),
                    view=None,
                )

            button.callback = callback
            self.add_item(button)


# Legacy alias
SkillPicker = BladerSkillPicker
