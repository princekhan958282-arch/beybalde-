"""Part 1: The Unknown Challenger.

Owner flow:
    ;settings -> Horror Story -> server -> channel -> player -> SPAWN

The public encounter is visible to the whole selected channel but only the
selected target can use its buttons.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands

from cogs.admin.actions import MASTER_ID
from utils import horror_state

log = logging.getLogger("beyblade_bot.horror")

HORROR_IMAGE_URL = (
    "https://cdn.discordapp.com/attachments/1520321120123748432/"
    "1549073218004975716/8619f5904b67ec3c915a5c8563e1e64e_1.jpg"
    "?ex=6aa95e5b&is=6aa80cdb&hm=3666ae337cf73f0f0953a3c3aa1ef532ecb6e9f12dcec8d5705031e9937f4e98&"
)
PROMPT_TIMEOUT = 120.0


def _trim(text: str, n: int = 95) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


class TargetOnlyView(discord.ui.View):
    def __init__(self, cog: "HorrorCog", target_id: int, *, timeout: float = PROMPT_TIMEOUT):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.target_id = int(target_id)
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.target_id:
            return True
        await interaction.response.send_message(
            "❌ This challenge isn't for you.", ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        await self.cog.apply_unknown_curse(self.target_id, self.message)


class ForcedBattleView(TargetOnlyView):
    @discord.ui.button(label="BATTLE", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def battle(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.accept_battle(interaction, self.target_id, self)


class HorrorChallengeView(TargetOnlyView):
    @discord.ui.button(label="BATTLE", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def battle(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.accept_battle(interaction, self.target_id, self)

    @discord.ui.button(label="NO", emoji="❌", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _button: discord.ui.Button):
        horror_state.save_encounter(self.target_id, status="declined_once", declined=True)
        forced = ForcedBattleView(self.cog, self.target_id)
        forced.message = interaction.message
        self.stop()
        await interaction.response.edit_message(
            content=f"<@{self.target_id}>\n**UNKNOWN:** battle me.",
            view=forced,
        )


class ServerSelect(discord.ui.Select):
    def __init__(self, panel: "HorrorSettingsView"):
        self.panel = panel
        guilds = sorted(panel.cog.bot.guilds, key=lambda g: g.name.lower())
        options = [
            discord.SelectOption(label=_trim(g.name), value=str(g.id), description=str(g.id))
            for g in guilds[:25]
        ]
        super().__init__(
            placeholder="Select Server",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="No servers available", value="0")],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        guild = self.panel.cog.bot.get_guild(int(self.values[0]))
        if guild is None:
            return await interaction.response.send_message("❌ Server is unavailable.", ephemeral=True)
        self.panel.guild = guild
        self.panel.channel = None
        self.panel.target = None
        self.panel.rebuild()
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class ChannelSelect(discord.ui.Select):
    def __init__(self, panel: "HorrorSettingsView"):
        self.panel = panel
        guild = panel.guild
        channels = list(guild.text_channels) if guild else []
        options = [
            discord.SelectOption(label=_trim("#" + c.name), value=str(c.id))
            for c in channels[:25]
        ]
        disabled = not bool(options)
        super().__init__(
            placeholder="Select Channel" if guild else "Select a server first",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="No channels available", value="0")],
            disabled=disabled,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        if not self.panel.guild:
            return await interaction.response.send_message("❌ Select a server first.", ephemeral=True)
        channel = self.panel.guild.get_channel(int(self.values[0]))
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Channel is unavailable.", ephemeral=True)
        self.panel.channel = channel
        self.panel.rebuild()
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class PlayerSelect(discord.ui.Select):
    def __init__(self, panel: "HorrorSettingsView"):
        self.panel = panel
        guild = panel.guild
        members = []
        if guild:
            members = sorted(
                (m for m in guild.members if not m.bot),
                key=lambda m: m.display_name.lower(),
            )
        options = [
            discord.SelectOption(
                label=_trim(m.display_name),
                value=str(m.id),
                description=_trim(f"@{m.name} • {m.id}", 95),
            )
            for m in members[:25]
        ]
        disabled = not bool(options)
        super().__init__(
            placeholder="Select Player" if guild else "Select a server first",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="No players available", value="0")],
            disabled=disabled,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        if not self.panel.guild:
            return await interaction.response.send_message("❌ Select a server first.", ephemeral=True)
        member = self.panel.guild.get_member(int(self.values[0]))
        if member is None:
            return await interaction.response.send_message("❌ Player is unavailable.", ephemeral=True)
        self.panel.target = member
        self.panel.rebuild()
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class PlayerIdModal(discord.ui.Modal, title="Select Player by ID"):
    player_id = discord.ui.TextInput(label="Discord User ID", placeholder="123456789012345678")

    def __init__(self, panel: "HorrorSettingsView"):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        if not self.panel.guild:
            return await interaction.response.send_message("❌ Select a server first.", ephemeral=True)
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
                "❌ That player is not available in the selected server.", ephemeral=True
            )
        self.panel.target = member
        self.panel.rebuild()
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class HorrorSettingsView(discord.ui.View):
    def __init__(self, cog: "HorrorCog", owner: discord.abc.User):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner = owner
        self.guild: Optional[discord.Guild] = None
        self.channel: Optional[discord.TextChannel] = None
        self.target: Optional[discord.Member] = None
        self.rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == MASTER_ID:
            return True
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return False

    def rebuild(self):
        self.clear_items()
        self.add_item(ServerSelect(self))
        self.add_item(ChannelSelect(self))
        self.add_item(PlayerSelect(self))

        player_id = discord.ui.Button(
            label="Player ID", emoji="🔎", style=discord.ButtonStyle.secondary, row=3
        )
        player_id.callback = self._player_id
        self.add_item(player_id)

        spawn = discord.ui.Button(
            label="SPAWN", emoji="👁️", style=discord.ButtonStyle.danger, row=3
        )
        spawn.callback = self._spawn
        self.add_item(spawn)

        close = discord.ui.Button(
            label="Close", style=discord.ButtonStyle.secondary, row=3
        )
        close.callback = self._close
        self.add_item(close)

    def embed(self) -> discord.Embed:
        e = discord.Embed(
            title="👁️ HORROR STORY CONTROL",
            color=0x080808,
            description=(
                f"**Server:** {discord.utils.escape_markdown(self.guild.name) if self.guild else 'Not Selected'}\n"
                f"**Channel:** {self.channel.mention if self.channel else 'Not Selected'}\n"
                f"**Target:** {self.target.mention if self.target else 'Not Selected'}\n\n"
                f"**Status:** {'READY' if self.guild and self.channel and self.target else 'WAITING'}"
            ),
        )
        if self.guild and len([m for m in self.guild.members if not m.bot]) > 25:
            e.set_footer(text="Player list shows 25 members. Use Player ID for anyone else.")
        return e

    async def _player_id(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PlayerIdModal(self))

    async def _spawn(self, interaction: discord.Interaction):
        if not (self.guild and self.channel and self.target):
            return await interaction.response.send_message(
                "❌ Select a server, channel, and player first.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, message = await self.cog.spawn_encounter(self.guild, self.channel, self.target)
        await interaction.followup.send(message, ephemeral=True)

    async def _close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="Settings closed.", embed=None, view=None)


class SettingsHomeView(discord.ui.View):
    def __init__(self, cog: "HorrorCog", owner: discord.abc.User):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner = owner

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == MASTER_ID:
            return True
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return False

    @discord.ui.button(label="Horror Story", emoji="👁️", style=discord.ButtonStyle.danger)
    async def horror(self, interaction: discord.Interaction, _button: discord.ui.Button):
        panel = HorrorSettingsView(self.cog, self.owner)
        await interaction.response.edit_message(embed=panel.embed(), view=panel)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Settings closed.", embed=None, view=None)


class HorrorCog(commands.Cog, name="Horror Story"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._curse_locks: dict[int, asyncio.Lock] = {}

    @commands.command(name="settings", hidden=True)
    async def settings(self, ctx: commands.Context):
        if ctx.author.id != MASTER_ID:
            return
        embed = discord.Embed(
            title="⚙️ BEYCORD OWNER SETTINGS",
            description="Choose what you want to configure.",
            color=0x2B2D31,
        )
        await ctx.send(embed=embed, view=SettingsHomeView(self, ctx.author))

    async def spawn_encounter(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        target: discord.Member,
    ) -> tuple[bool, str]:
        if target.guild.id != guild.id or channel.guild.id != guild.id:
            return False, "❌ Server, channel, and player no longer match."

        me = guild.me
        if me is not None:
            perms = channel.permissions_for(me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                return False, "❌ Beycord cannot send embeds in that channel."

        previous = horror_state.encounter(target.id)
        if previous.get("status") in {"spawned", "declined_once"}:
            return False, "❌ That player already has an unresolved Horror encounter."

        embed = discord.Embed(color=0x050505)
        embed.set_image(url=HORROR_IMAGE_URL)

        view = HorrorChallengeView(self, target.id)
        msg = await channel.send(
            content=f"{target.mention}\n**UNKNOWN:** hey u wanna battle me?",
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        view.message = msg

        horror_state.save_encounter(
            target.id,
            status="spawned",
            declined=False,
            cursed=horror_state.is_cursed(target.id),
            guild_id=guild.id,
            channel_id=channel.id,
            message_id=msg.id,
            target_user_id=target.id,
        )
        log.info(
            "[horror] spawned target=%s guild=%s channel=%s",
            target.id, guild.id, channel.id,
        )
        return True, f"👁️ Spawned for {target.mention} in {channel.mention}."

    async def accept_battle(
        self,
        interaction: discord.Interaction,
        target_id: int,
        view: TargetOnlyView,
    ) -> None:
        # Snapshot the equipped Bey at the moment the battle is accepted.
        bey_name = None
        copy_id = None
        try:
            from cogs.battle.boss import boss_copy as bcopy
            blade, _copy = await bcopy.equipped_blade(target_id)
            if blade:
                bey_name = blade.get("name")
                copy_id = str((_copy or {}).get("id") or "") or None
        except Exception as exc:  # noqa: BLE001
            log.warning("[horror] equipped bey lookup failed for %s: %s", target_id, exc)

        if not bey_name:
            return await interaction.response.send_message(
                "❌ Equip a Beyblade first, then press **BATTLE** again.",
                ephemeral=True,
            )

        horror_state.save_encounter(
            target_id,
            status="battle_requested",
            battle_started=True,
            equipped_bey=bey_name,
            equipped_copy_id=copy_id,
        )
        view.stop()
        await interaction.response.edit_message(
            content=f"<@{target_id}>\n**UNKNOWN:** good.",
            view=None,
        )

        # Part 1's actual UNKNOWN opponent can plug into the normal battle engine
        # by listening for this event once its Bey/stats are authored.
        self.bot.dispatch(
            "horror_battle_requested",
            interaction.channel,
            interaction.user,
            bey_name,
            copy_id,
        )

    async def apply_unknown_curse(
        self,
        target_id: int,
        message: Optional[discord.Message],
    ) -> None:
        lock = self._curse_locks.setdefault(int(target_id), asyncio.Lock())
        async with lock:
            row = horror_state.encounter(target_id)
            if row.get("status") in {"battle_requested", "completed"}:
                return
            horror_state.apply_curse(target_id)
            horror_state.save_encounter(
                target_id,
                status="cursed",
                cursed=True,
            )
            if message is not None:
                try:
                    await message.edit(view=None)
                except discord.HTTPException:
                    pass
                try:
                    await message.channel.send(
                        f"<@{target_id}>\n"
                        "👁️ **CURSED**\n"
                        "**UNKNOWN:** you should have battled me.\n\n"
                        "All combat stats are reduced by **20%**.",
                        allowed_mentions=discord.AllowedMentions(
                            users=True, roles=False, everyone=False
                        ),
                    )
                except discord.HTTPException:
                    pass
            log.info("[horror] curse applied target=%s", target_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HorrorCog(bot))
