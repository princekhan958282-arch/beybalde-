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
        self.channel_stage = False
        self.channel_id = None
        self.published = False
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
        summary = self.config.summary()
        card = discord.ui.Container(
            discord.ui.TextDisplay('# 🏆 Tournament Host Panel\nTournament configuration\n**Status: ' + ('Registration Published' if self.published else 'Ready to Publish' if self.channel_stage else 'Draft') + '**'),
            accent_colour=0x5865F2)
        editing = self.editing or self.reset_pending or self.channel_stage
        for key in self.config.visible_keys():
            field = discord.ui.TextDisplay(f'**{LABELS[key]}**\n{discord.utils.escape_mentions(discord.utils.escape_markdown(self.config.display(key)))}')
            if self.closed or editing:
                card.add_item(field)
            else:
                card.add_item(discord.ui.Section(field, accessory=EditButton(key)))
        if not self.closed:
            if self.channel_stage:
                card.add_item(discord.ui.TextDisplay('### Select Registration Channel' +
                    (f'\nSelected: <#{self.channel_id}>' if self.channel_id else '')))
                card.add_item(discord.ui.ActionRow(RegistrationChannelSelect()))
                buttons = [PanelButton('Back to Settings', 'back'), PanelButton('Cancel Setup', 'cancel', discord.ButtonStyle.danger)]
                if self.channel_id:
                    buttons.insert(0, PanelButton('Publish Registration', 'publish', discord.ButtonStyle.success))
                card.add_item(discord.ui.ActionRow(*buttons))
            elif self.reset_pending:
                card.add_item(discord.ui.TextDisplay('Restore all settings to their defaults?'))
                card.add_item(discord.ui.ActionRow(PanelButton('Restore Defaults', 'reset_yes', discord.ButtonStyle.danger), PanelButton('Keep Settings', 'back')))
            elif self.editing:
                card.add_item(discord.ui.ActionRow(SettingSelect(self.editing, self.config)))
                card.add_item(discord.ui.ActionRow(PanelButton('Back', 'back')))
        card.add_item(discord.ui.TextDisplay('### Tournament Preview\n' + summary +
            '\n\n' + self.note + '\n-# Registration is available. Battles and fee collection are deferred.'))
        if not self.closed and not editing:
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
                self.channel_stage = False
                if value in ('name', 'entry_fee', 'avatar_level'):
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
                    if key == 'avatar_level_rule' and parsed == 'Equalized Level':
                        return await interaction.response.send_modal(SettingModal(self, 'avatar_level'))
                    self.config = self.config.updated(key, parsed)
                except ValueError as exc:
                    return await interaction.response.send_message(str(exc), ephemeral=True)
                self.editing = None
            elif action == 'reset':
                self.reset_pending = True
                self.note = 'Restore all settings to their defaults?'
            elif action == 'reset_yes':
                if not self.reset_pending:
                    return await interaction.response.send_message('Select Reset first.', ephemeral=True)
                self.config = DraftConfig()
                self.reset_pending = False
                self.note = 'Defaults restored.'
            elif action == 'back':
                self.channel_stage = False
                self.channel_id = None
                self.editing = None
                self.reset_pending = False
                self.note = 'Each victory is one complete PvP battle won.'
            elif action == 'cancel':
                self.note = '✖️ Setup cancelled. Unsaved edits discarded; any saved draft is unchanged.'
                self.release()
            elif action == 'confirm':
                self.config.validate()
                live = self.cog.lobbies.get(self.guild_id)
                if (live and not live.finished) or self.guild_id in self.cog.registrations:
                    return await interaction.response.send_message('A tournament is already active in this server.', ephemeral=True)
                await interaction.response.defer()
                await asyncio.to_thread(self.cog.draft_store.save, self.guild_id, self.host_id, self.config)
                self.note = '✅ Setup confirmed. Select a channel, then publish registration.'
                self.channel_stage = True
                self.editing = None
                self.reset_pending = False
                self.build()
                return await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())
            elif action == 'channel':
                if not self.channel_stage:
                    return await interaction.response.send_message('Confirm setup first.', ephemeral=True)
                from .registration import validate_channel
                try:
                    validate_channel(interaction.guild, interaction.guild.get_channel(value))
                except ValueError as exc:
                    return await interaction.response.send_message(str(exc), ephemeral=True)
                self.channel_id = value
            elif action == 'publish':
                if not self.channel_stage or not self.channel_id:
                    return await interaction.response.send_message('Select a registration channel first.', ephemeral=True)
                await interaction.response.defer()
                try:
                    panel = await self.cog.publish_registration(self, interaction.guild)
                except ValueError as exc:
                    return await interaction.followup.send(str(exc), ephemeral=True)
                self.published = True
                self.note = f'✅ Registration published: {panel.message.jump_url}'
                self.release()
                self.build()
                return await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())
            await self.refresh(interaction)

class RegistrationChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder='Select Registration Channel', channel_types=[discord.ChannelType.text], min_values=1, max_values=1)

    async def callback(self, interaction):
        await self.view.act(interaction, 'channel', self.values[0].id)

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
                if self.key in ('entry_fee', 'avatar_level') and (not raw.isascii() or not raw.isdecimal()):
                    raise ValueError('Enter a valid non-negative integer.')
                value = int(raw) if self.key in ('entry_fee', 'avatar_level') else raw
                config = panel.config.updated(self.key, value)
                if self.key == 'avatar_level':
                    config = config.updated('avatar_level_rule', 'Equalized Level')
            except ValueError as exc:
                return await interaction.response.send_message(str(exc), ephemeral=True)
            await interaction.response.defer()
            panel.config = config
            panel.editing = None
            panel.channel_stage = False
            panel.channel_id = None
            panel.build()
            # Modal is tied to the host panel message; never send a new public message.
            await panel.message.edit(view=panel, allowed_mentions=discord.AllowedMentions.none())

    async def on_error(self, interaction, error):
        await self.panel.on_error(interaction, error, self)
