"""Discord Components V2 host editor. All edits stay on the original message."""
import asyncio
import logging
import discord
from .drafts import DraftConfig, LABELS, OPTIONS

log = logging.getLogger(__name__)

class HostPanel(discord.ui.LayoutView):
    def __init__(self, cog, guild_id, host_id, config=None):
        super().__init__(timeout=600)
        self.cog, self.guild_id, self.host_id = cog, guild_id, host_id
        self.config = config or DraftConfig()
        self.message = None
        self.closed = False
        self.editing = None
        self.reset_pending = False
        self.note = 'Each victory is one complete PvP battle won.'
        self.lock = asyncio.Lock()
        self.build()

    async def interaction_check(self, interaction):
        from .tournament import is_tournament_admin
        authorized = interaction.user.id == self.host_id and (
            is_tournament_admin(interaction.user) or await self.cog.bot.is_owner(interaction.user))
        if not authorized:
            await interaction.response.send_message('Only the authorized host can edit this panel.', ephemeral=True)
            return False
        if self.closed or self.cog.setups.get(self.guild_id) is not self:
            await interaction.response.send_message('This setup has closed. Open a new tournament panel.', ephemeral=True)
            return False
        return True

    def build(self):
        self.clear_items()
        safe_name = discord.utils.escape_mentions(discord.utils.escape_markdown(self.config.name))
        summary = '\n'.join(f'**{label}:** {safe_name if key == "name" else self.config.display(key)}' for key, label in LABELS.items())
        card = discord.ui.Container(
            discord.ui.TextDisplay('# 🏆 Tournament Host Panel\nTournament configuration\n**Status: Draft**'),
            discord.ui.Separator(),
            accent_colour=0x5865F2)
        # Discord sections place one real Edit button beside each label/value.
        # A native select occupies a full row, so two-column selects are not used.
        for key, label in LABELS.items():
            value = safe_name if key == 'name' else self.config.display(key)
            field = discord.ui.TextDisplay(f'**{label}**\n{value}')
            if self.closed:
                card.add_item(field)
            else:
                card.add_item(discord.ui.Section(field, accessory=EditButton(key)))
        if not self.closed:
            if self.reset_pending:
                card.add_item(discord.ui.Separator())
                card.add_item(discord.ui.TextDisplay('Restore all settings to their defaults?'))
                card.add_item(discord.ui.ActionRow(PanelButton('Restore Defaults', 'reset_yes', discord.ButtonStyle.danger), PanelButton('Keep Settings', 'back')))
            elif self.editing:
                card.add_item(discord.ui.Separator())
                card.add_item(discord.ui.ActionRow(SettingSelect(self.editing, self.config)))
                card.add_item(discord.ui.ActionRow(PanelButton('Back', 'back')))
        card.add_item(discord.ui.Separator())
        card.add_item(discord.ui.TextDisplay('### Tournament Preview\n' + summary))
        card.add_item(discord.ui.TextDisplay(self.note + '\n-# Phase 1 saves drafts only. Registration and battles are unavailable.'))
        if not self.closed and not self.reset_pending and not self.editing:
            card.add_item(discord.ui.ActionRow(
                PanelButton('Confirm Setup', 'confirm', discord.ButtonStyle.success),
                PanelButton('Reset', 'reset'),
                PanelButton('Cancel Setup', 'cancel', discord.ButtonStyle.danger)))
        self.add_item(card)

    async def refresh(self, interaction):
        self.build()
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())

    def release(self):
        self.closed = True
        if self.cog.setups.get(self.guild_id) is self:
            self.cog.setups.pop(self.guild_id)
        self.stop()

    async def close(self, reason):
        async with self.lock:
            if self.closed:
                return
            self.note = reason
            self.release()
            self.build()
            if self.message:
                try:
                    await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    log.warning('Could not close tournament setup message', exc_info=True)

    async def on_timeout(self):
        await self.close('⏰ Setup expired. Unsaved edits were discarded; any previously saved draft is unchanged.')

    async def on_error(self, interaction, error, item):
        log.error('Tournament host panel failed', exc_info=(type(error), error, error.__traceback__))
        note = 'Could not complete that action. Your settings are intact; please try again.'
        if interaction.response.is_done():
            await interaction.followup.send(note, ephemeral=True)
        else:
            await interaction.response.send_message(note, ephemeral=True)

    async def act(self, interaction, action, value=None):
        async with self.lock:
            if not await self.interaction_check(interaction):
                return
            if action == 'edit':
                self.reset_pending = False
                if value in ('name', 'entry_fee'):
                    return await interaction.response.send_modal(SettingModal(self, value))
                if value not in OPTIONS:
                    raise ValueError('Unknown setting')
                self.editing = value
            elif action == 'value':
                key, raw = value
                if key not in OPTIONS or key != self.editing:
                    return await interaction.response.send_message('That editor is outdated. Select the setting again.', ephemeral=True)
                try:
                    parsed = int(raw) if isinstance(OPTIONS[key][0], int) else raw
                    self.config = self.config.updated(key, parsed)
                except ValueError as exc:
                    return await interaction.response.send_message(str(exc), ephemeral=True)
                self.editing = None
            elif action == 'reset':
                self.reset_pending = True
                self.note = 'Restore all eight settings to their defaults?'
            elif action == 'reset_yes':
                if not self.reset_pending:
                    return await interaction.response.send_message('Select Reset first.', ephemeral=True)
                self.config = DraftConfig()
                self.reset_pending = False
                self.note = 'Defaults restored.'
            elif action == 'back':
                self.editing = None
                self.reset_pending = False
                self.note = 'Each victory is one complete PvP battle won.'
            elif action == 'cancel':
                self.note = '✖️ Setup cancelled. Unsaved edits discarded; any saved draft is unchanged.'
                self.release()
            elif action == 'confirm':
                self.config.validate()
                live = self.cog.lobbies.get(self.guild_id)
                if live and not live.finished:
                    return await interaction.response.send_message('A tournament is already active in this server.', ephemeral=True)
                await interaction.response.defer()
                await asyncio.to_thread(self.cog.draft_store.save, self.guild_id, self.host_id, self.config)
                self.note = '✅ Configuration saved as a draft. Registration and battles have not started.'
                self.release()
                self.build()
                return await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())
            await self.refresh(interaction)

class EditButton(discord.ui.Button):
    def __init__(self, key):
        super().__init__(label='Edit', style=discord.ButtonStyle.secondary,
                         custom_id=f'tournament:edit:{key}')
        self.key = key

    async def callback(self, interaction):
        await self.view.act(interaction, 'edit', self.key)

class SettingSelect(discord.ui.Select):
    def __init__(self, key, config):
        self.key = key
        super().__init__(placeholder=LABELS[key], options=[discord.SelectOption(
            label=f'First to {v}' if key == 'victory_target' else str(v), value=str(v), default=v == getattr(config, key)) for v in OPTIONS[key]])
    async def callback(self, interaction):
        await self.view.act(interaction, 'value', (self.key, self.values[0]))

class PanelButton(discord.ui.Button):
    def __init__(self, label, action, style=discord.ButtonStyle.secondary):
        super().__init__(label=label, style=style)
        self.action = action
    async def callback(self, interaction):
        await self.view.act(interaction, self.action)

class SettingModal(discord.ui.Modal):
    def __init__(self, panel, key):
        super().__init__(title=LABELS[key], timeout=300)
        self.panel, self.key = panel, key
        self.input = discord.ui.TextInput(label=LABELS[key], default=str(getattr(panel.config, key)), max_length=100 if key == 'name' else 19)
        self.add_item(self.input)

    async def on_submit(self, interaction):
        panel = self.panel
        async with panel.lock:
            if not await panel.interaction_check(interaction):
                return
            raw = self.input.value.strip()
            try:
                if self.key == 'entry_fee' and (not raw.isascii() or not raw.isdecimal()):
                    raise ValueError('Entry fee must be a non-negative integer.')
                value = int(raw) if self.key == 'entry_fee' else raw
                config = panel.config.updated(self.key, value)
            except ValueError as exc:
                return await interaction.response.send_message(str(exc), ephemeral=True)
            await interaction.response.defer()
            panel.config = config
            panel.editing = None
            panel.build()
            # Modal is tied to the host panel message; never send a new public message.
            await panel.message.edit(view=panel, allowed_mentions=discord.AllowedMentions.none())

    async def on_error(self, interaction, error):
        await self.panel.on_error(interaction, error, self)
