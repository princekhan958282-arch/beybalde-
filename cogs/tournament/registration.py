"""Persistent public registration. Deliberately does not launch the legacy runner."""
import asyncio
from dataclasses import asdict, dataclass, field
from contextlib import closing
import json
import logging
import uuid

import discord
from utils.database import get_user
from .drafts import DraftConfig

log = logging.getLogger(__name__)

@dataclass
class Registration:
    guild_id: int
    host_id: int
    channel_id: int
    config: DraftConfig
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    message_id: int = 0
    entrants: list[int] = field(default_factory=list)
    status: str = 'publishing'

    @classmethod
    def decode(cls, payload):
        data = json.loads(payload)
        data['config'] = DraftConfig(**data['config'])
        data['config'].validate()
        return cls(**data)

class RegistrationStore:
    def __init__(self, drafts):
        self.drafts = drafts

    def connect(self):
        conn = self.drafts._connect()
        conn.execute('CREATE TABLE IF NOT EXISTS registrations (guild_id INTEGER PRIMARY KEY, token TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)')
        return conn

    def begin(self, reg):
        reg.config.validate()
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT status FROM registrations WHERE guild_id=?', (reg.guild_id,)).fetchone()
            if row and row[0] in ('open', 'publishing'):
                raise ValueError('Registration is already open in this server.')
            conn.execute('INSERT OR REPLACE INTO registrations VALUES (?, ?, ?, ?)',
                         (reg.guild_id, reg.token, reg.status, json.dumps(asdict(reg))))

    def write(self, reg):
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute('UPDATE registrations SET status=?, payload=? WHERE guild_id=? AND token=?',
                                 (reg.status, json.dumps(asdict(reg)), reg.guild_id, reg.token))
            if cursor.rowcount != 1:
                raise ValueError('This registration is no longer current.')

    def current(self, guild_id):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT payload FROM registrations WHERE guild_id=? AND status IN ('open', 'publishing')", (guild_id,)).fetchone()
        return Registration.decode(row[0]) if row else None

    def restore(self):
        with closing(self.connect()) as conn, conn:
            # A crash before storing the message ID cannot leave a permanent reservation.
            conn.execute("UPDATE registrations SET status='cancelled' WHERE status='publishing'")
            rows = conn.execute("SELECT payload FROM registrations WHERE status='open'").fetchall()
        return [Registration.decode(row[0]) for row in rows]

    def membership(self, reg, uid, join):
        """Update from the durable record; serialize joins across all guilds."""
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT payload FROM registrations WHERE guild_id=? AND token=? AND status='open'", (reg.guild_id, reg.token)).fetchone()
            if not row:
                raise ValueError('Registration is closed.')
            current = Registration.decode(row[0])
            if join:
                if uid in current.entrants:
                    raise ValueError('You have already joined. Use Leave to withdraw.')
                if len(current.entrants) >= current.config.slots:
                    raise ValueError('Registration is full.')
                for (payload,) in conn.execute("SELECT payload FROM registrations WHERE status='open'"):
                    if uid in Registration.decode(payload).entrants:
                        raise ValueError('You are already registered in another tournament.')
                current.entrants.append(uid)
            else:
                if uid not in current.entrants:
                    raise ValueError('You have not joined this tournament.')
                current.entrants.remove(uid)
            conn.execute('UPDATE registrations SET payload=? WHERE guild_id=? AND token=?',
                         (json.dumps(asdict(current)), reg.guild_id, reg.token))
        reg.entrants = current.entrants


def validate_channel(guild, channel):
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
        raise ValueError('Select a text channel in this server.')
    if guild.me is None:
        raise ValueError('Bot membership could not be resolved. Try again.')
    perms = channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.read_message_history):
        raise ValueError('The bot needs View Channel, Send Messages and Read Message History in that channel.')
    return channel

class RegistrationPanel(discord.ui.LayoutView):
    def __init__(self, cog, registration):
        super().__init__(timeout=None)
        self.cog, self.reg = cog, registration
        self.message = None
        self.lock = asyncio.Lock()
        self.build()

    def build(self):
        self.clear_items()
        reg = self.reg
        roster = '\n'.join(f'{n}. <@{uid}>' for n, uid in enumerate(reg.entrants, 1)) or 'Nobody yet — press Join.'
        note = 'Battles are not available yet. Entry fees are not charged during registration.'
        card = discord.ui.Container(
            discord.ui.TextDisplay('# 🏆 Tournament Registration\n' + ('**Registration Open**' if reg.status == 'open' else '**Registration Closed**')),
            discord.ui.TextDisplay('### Tournament Preview\n' + reg.config.summary()),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f'### Players ({len(reg.entrants)}/{reg.config.slots})\n{roster}'),
            discord.ui.TextDisplay('-# ' + note), accent_colour=0x5865F2)
        if reg.status == 'open':
            card.add_item(discord.ui.ActionRow(RegButton('Join', 'join', reg.token, discord.ButtonStyle.success), RegButton('Leave', 'leave', reg.token), RegButton('Cancel Registration', 'cancel', reg.token, discord.ButtonStyle.danger)))
        self.add_item(card)

    async def on_error(self, interaction, error, item):
        log.error('Registration interaction failed', exc_info=(type(error), error, error.__traceback__))
        note = 'Could not complete this action. Please try again.'
        if interaction.response.is_done():
            await interaction.followup.send(note, ephemeral=True)
        else:
            await interaction.response.send_message(note, ephemeral=True)

    async def close(self):
        async with self.lock:
            if self.reg.status != 'open' or self.cog.registrations.get(self.reg.guild_id) is not self:
                return
            self.reg.status = 'cancelled'
            try:
                await asyncio.to_thread(self.cog.registration_store.write, self.reg)
            except Exception:
                self.reg.status = 'open'
                raise
            for uid in self.reg.entrants:
                self.cog._active.discard(uid)
            if self.cog.registrations.get(self.reg.guild_id) is self:
                self.cog.registrations.pop(self.reg.guild_id)
            self.build()
            self.stop()
            if self.message:
                try:
                    await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    log.warning('Could not edit closed registration', exc_info=True)

    async def remove_banned(self, uid):
        async with self.lock:
            if self.reg.status != 'open' or uid not in self.reg.entrants:
                return
            await asyncio.to_thread(self.cog.registration_store.membership, self.reg, uid, False)
            self.cog._active.discard(uid)
            self.build()
            if self.message:
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def act(self, interaction, action):
        if action in ('cancel', 'cancel_yes', 'back'):
            from .tournament import is_tournament_admin
            if interaction.user.id != self.reg.host_id or not (is_tournament_admin(interaction.user) or await self.cog.bot.is_owner(interaction.user)):
                return await interaction.response.send_message('Only the authorized host can cancel registration.', ephemeral=True)
        async with self.lock:
            if self.reg.status != 'open' or self.cog.registrations.get(self.reg.guild_id) is not self:
                return await interaction.response.send_message('Registration is closed.', ephemeral=True)
            uid = interaction.user.id
            if interaction.guild_id != self.reg.guild_id or getattr(interaction.user, 'bot', False):
                return await interaction.response.send_message('Only players in this server may join.', ephemeral=True)
            if action == 'cancel':
                return await interaction.response.send_modal(CancelRegistrationModal(self))
            if action not in ('join', 'leave'):
                return await interaction.response.send_message('Unknown registration action.', ephemeral=True)
            await interaction.response.defer()
            try:
                if action == 'join':
                    if uid in self.cog.banned:
                        raise ValueError('You are banned from tournaments.')
                    if uid in self.reg.entrants:
                        raise ValueError('You have already joined. Use Leave to withdraw.')
                    if uid in self.cog._active or self.cog._in_battle(uid):
                        raise ValueError('Finish or leave your current battle/tournament first.')
                    profile = await get_user(uid)
                    if not profile.get('inventory'):
                        raise ValueError('You need a Beyblade first — use ;start.')
                    if self.reg.config.bey_selection == 'Equipped Beyblade':
                        from cogs.battle.boss.boss_copy import equipped_blade
                        blade, _ = await equipped_blade(uid)
                        if not blade:
                            raise ValueError('Equip a Beyblade before joining — use ;equip.')
                    # V2 equipment/random/avatars are policies only until battle support lands.
                await asyncio.to_thread(self.cog.registration_store.membership, self.reg, uid, action == 'join')
            except ValueError as exc:
                return await interaction.followup.send(str(exc), ephemeral=True)
            if action == 'join':
                self.cog._active.add(uid)
            else:
                self.cog._active.discard(uid)
            self.build()
            await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())

class CancelRegistrationModal(discord.ui.Modal):
    def __init__(self, panel):
        super().__init__(title='Cancel Registration', timeout=120)
        self.panel = panel
        self.confirmation = discord.ui.TextInput(label='Type CANCEL to release all players', max_length=6)
        self.add_item(self.confirmation)

    async def on_submit(self, interaction):
        from .tournament import is_tournament_admin
        if interaction.user.id != self.panel.reg.host_id or not (is_tournament_admin(interaction.user) or await self.panel.cog.bot.is_owner(interaction.user)):
            return await interaction.response.send_message('Only the authorized host may cancel.', ephemeral=True)
        if self.confirmation.value.strip() != 'CANCEL':
            return await interaction.response.send_message('Registration remains open. Type CANCEL to confirm.', ephemeral=True)
        await interaction.response.defer()
        await self.panel.close()

    async def on_error(self, interaction, error):
        await self.panel.on_error(interaction, error, self)

class RegButton(discord.ui.Button):
    def __init__(self, label, action, token, style=discord.ButtonStyle.secondary):
        super().__init__(label=label, style=style, custom_id=f'tournament:registration:{token}:{action}')
        self.action = action

    async def callback(self, interaction):
        await self.view.act(interaction, self.action)
