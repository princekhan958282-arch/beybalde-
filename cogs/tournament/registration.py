"""Persistent public registration with 24h duration, host notifications, and start button."""
import asyncio
from dataclasses import asdict, dataclass, field
from contextlib import closing
import json
import logging
import time
import uuid

import discord
from utils.database import get_user, mutate_user
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
    created_at: float = field(default_factory=time.time)
    notified_full: bool = False

    @classmethod
    def decode(cls, payload):
        data = json.loads(payload)
        data['config'] = DraftConfig(**data['config'])
        data['config'].validate()
        data.setdefault('created_at', time.time())
        data.setdefault('notified_full', False)
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
            row = conn.execute("SELECT payload FROM registrations WHERE guild_id=? AND status IN ('open', 'closed', 'running', 'publishing')", (guild_id,)).fetchone()
        return Registration.decode(row[0]) if row else None

    def restore(self):
        with closing(self.connect()) as conn, conn:
            # A crash before storing the message ID or mid-run cannot leave a permanent reservation.
            conn.execute("UPDATE registrations SET status='cancelled' WHERE status='publishing'")
            conn.execute("UPDATE registrations SET status='cancelled' WHERE status='running'")
            rows = conn.execute("SELECT payload FROM registrations WHERE status IN ('open', 'closed')").fetchall()
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
                if time.time() >= current.created_at + 86400:
                    raise ValueError('Registration is closed: The 24-hour signup period has expired.')
                if uid in current.entrants:
                    raise ValueError('You have already joined. Use Leave to withdraw.')
                if len(current.entrants) >= current.config.slots:
                    raise ValueError('Registration is full.')
                for (payload,) in conn.execute("SELECT payload FROM registrations WHERE status IN ('open', 'closed', 'running')"):
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

        deadline_ts = int(reg.created_at + 86400)
        if reg.status == 'open':
            status_text = '**Registration Open**'
            deadline_text = f'⏳ **Registration Closes:** <t:{deadline_ts}:R> (<t:{deadline_ts}:f>)'
        elif reg.status == 'closed':
            status_text = '**Registration Closed (Waiting for Host to Start)**'
            deadline_text = '⏳ **Registration:** Closed (24h period ended — waiting for host to start).'
        elif reg.status == 'running':
            status_text = '**Tournament Running**'
            deadline_text = '⚔️ **Status:** Matches in progress!'
        elif reg.status == 'completed':
            status_text = '**Tournament Completed**'
            deadline_text = '🏆 **Status:** Tournament finished.'
        else:
            status_text = f'**Registration {reg.status.capitalize()}**'
            deadline_text = ''

        if reg.config.entry_fee > 0:
            fee_text = f'💰 **Entry Fee:** {reg.config.entry_fee:,} Beycoins (deducted upon joining, refunded if withdrawn or cancelled).'
        else:
            fee_text = '💰 **Entry Fee:** Free entry.'

        full_banner = '\n🔴 **Registration is FULL. Host has been notified to start!**' if len(reg.entrants) >= reg.config.slots else ''

        notes = [deadline_text, fee_text]
        if full_banner:
            notes.append(full_banner)
        note_str = '\n'.join(n for n in notes if n)

        card = discord.ui.Container(
            discord.ui.TextDisplay(f'# 🏆 Tournament Registration: {reg.config.name}\n{status_text}'),
            discord.ui.TextDisplay('### Tournament Preview\n' + reg.config.summary()),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f'### Players ({len(reg.entrants)}/{reg.config.slots})\n{roster}'),
            discord.ui.TextDisplay('-# ' + note_str),
            accent_colour=0x5865F2
        )

        if reg.status in ('open', 'closed'):
            join_btn = RegButton('Join', 'join', reg.token, discord.ButtonStyle.success)
            if len(reg.entrants) >= reg.config.slots or reg.status == 'closed':
                join_btn.disabled = True

            leave_btn = RegButton('Leave', 'leave', reg.token, discord.ButtonStyle.secondary)
            start_btn = RegButton('Start Tournament', 'start', reg.token, discord.ButtonStyle.primary)
            cancel_btn = RegButton('Cancel Registration', 'cancel', reg.token, discord.ButtonStyle.danger)

            card.add_item(discord.ui.ActionRow(join_btn, leave_btn, start_btn, cancel_btn))

        self.add_item(card)

    async def on_error(self, interaction, error, item):
        log.error('Registration interaction failed', exc_info=(type(error), error, error.__traceback__))
        note = 'Could not complete this action. Please try again.'
        if interaction.response.is_done():
            await interaction.followup.send(note, ephemeral=True)
        else:
            await interaction.response.send_message(note, ephemeral=True)

    async def close(self, reason: str = 'cancelled'):
        async with self.lock:
            if self.reg.status not in ('open', 'closed') or self.cog.registrations.get(self.reg.guild_id) is not self:
                return
            self.reg.status = 'cancelled'
            try:
                await asyncio.to_thread(self.cog.registration_store.write, self.reg)
            except Exception:
                self.reg.status = 'open'
                raise

            # Refund entry fees on cancellation
            if self.reg.config.entry_fee > 0:
                for uid in list(self.reg.entrants):
                    try:
                        await mutate_user(uid, lambda p: p.__setitem__('coins', int(p.get('coins', 0)) + self.reg.config.entry_fee))
                    except Exception:
                        log.exception("Failed to refund entry fee to %s on tournament cancel", uid)

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
            if self.reg.status not in ('open', 'closed') or uid not in self.reg.entrants:
                return
            await asyncio.to_thread(self.cog.registration_store.membership, self.reg, uid, False)
            self.cog._active.discard(uid)
            if self.reg.config.entry_fee > 0:
                try:
                    await mutate_user(uid, lambda p: p.__setitem__('coins', int(p.get('coins', 0)) + self.reg.config.entry_fee))
                except Exception:
                    log.exception("Failed to refund entry fee to banned user %s", uid)
            if len(self.reg.entrants) < self.reg.config.slots and self.reg.notified_full:
                self.reg.notified_full = False
                await asyncio.to_thread(self.cog.registration_store.write, self.reg)
            self.build()
            if self.message:
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def act(self, interaction, action):
        if action in ('cancel', 'cancel_yes', 'back'):
            from .tournament import is_tournament_admin
            if interaction.user.id != self.reg.host_id and not (is_tournament_admin(interaction.user) or await self.cog.bot.is_owner(interaction.user)):
                return await interaction.response.send_message('Only the authorized host or an admin can cancel registration.', ephemeral=True)
            if action == 'cancel':
                return await interaction.response.send_modal(CancelRegistrationModal(self))

        if action == 'start':
            from .tournament import is_tournament_admin
            if interaction.user.id != self.reg.host_id and not (is_tournament_admin(interaction.user) or await self.cog.bot.is_owner(interaction.user)):
                return await interaction.response.send_message('Only the authorized host or an admin can start the tournament.', ephemeral=True)
            if len(self.reg.entrants) < 2:
                return await interaction.response.send_message(f'At least 2 players are required to start the tournament (currently {len(self.reg.entrants)}).', ephemeral=True)
            async with self.lock:
                if self.reg.status not in ('open', 'closed') or self.cog.registrations.get(self.reg.guild_id) is not self:
                    return await interaction.response.send_message('This tournament is no longer awaiting start.', ephemeral=True)
                await interaction.response.defer()
                self.reg.status = 'running'
                await asyncio.to_thread(self.cog.registration_store.write, self.reg)
                self.build()
                self.stop()
                if self.message:
                    try:
                        await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException:
                        pass
                try:
                    await interaction.followup.send('🚀 Starting tournament bracket...', ephemeral=True)
                except Exception:
                    pass
                self.cog._spawn(self.cog.run_v2(self.reg, self))
                return

        async with self.lock:
            if self.reg.status != 'open' or self.cog.registrations.get(self.reg.guild_id) is not self:
                return await interaction.response.send_message('Registration is closed.', ephemeral=True)
            uid = interaction.user.id
            if interaction.guild_id != self.reg.guild_id or getattr(interaction.user, 'bot', False):
                return await interaction.response.send_message('Only players in this server may join.', ephemeral=True)
            if action not in ('join', 'leave'):
                return await interaction.response.send_message('Unknown registration action.', ephemeral=True)
            await interaction.response.defer()
            try:
                if action == 'join':
                    if time.time() >= self.reg.created_at + 86400:
                        raise ValueError('Registration is closed: The 24-hour signup period has expired.')
                    if uid in self.cog.banned:
                        raise ValueError('You are banned from tournaments.')
                    if uid in self.reg.entrants:
                        raise ValueError('You have already joined. Use Leave to withdraw.')
                    if uid in self.cog._active or self.cog._in_battle(uid):
                        raise ValueError('Finish or leave your current battle/tournament first.')
                    profile = await get_user(uid)
                    if not profile.get('inventory'):
                        raise ValueError('You need a Beyblade first — use ;start.')
                    if self.reg.config.entry_fee > 0:
                        coins = int(profile.get('coins', 0))
                        if coins < self.reg.config.entry_fee:
                            raise ValueError(f'You need at least {self.reg.config.entry_fee:,} Beycoins to join (you have {coins:,}).')
                    if self.reg.config.bey_selection == 'Equipped Beyblade':
                        from cogs.battle.boss.boss_copy import equipped_blade
                        blade, _ = await equipped_blade(uid)
                        if not blade:
                            raise ValueError('Equip a Beyblade before joining — use ;equip.')
                await asyncio.to_thread(self.cog.registration_store.membership, self.reg, uid, action == 'join')
            except ValueError as exc:
                return await interaction.followup.send(str(exc), ephemeral=True)

            if action == 'join':
                self.cog._active.add(uid)
                if self.reg.config.entry_fee > 0:
                    try:
                        await mutate_user(uid, lambda p: p.__setitem__('coins', int(p.get('coins', 0)) - self.reg.config.entry_fee))
                    except Exception:
                        log.exception("Failed to deduct entry fee for %s", uid)

                # If full, notify host automatically
                if len(self.reg.entrants) == self.reg.config.slots and not self.reg.notified_full:
                    self.reg.notified_full = True
                    await asyncio.to_thread(self.cog.registration_store.write, self.reg)
                    try:
                        channel = interaction.guild.get_channel(self.reg.channel_id) if interaction.guild else None
                        if channel is None and hasattr(self.cog.bot, 'get_channel'):
                            channel = self.cog.bot.get_channel(self.reg.channel_id)
                        if channel and hasattr(channel, 'send'):
                            coro = channel.send(
                                f"📢 <@{self.reg.host_id}>, registration for **{self.reg.config.name}** is now **FULL** "
                                f"({len(self.reg.entrants)}/{self.reg.config.slots} players)! 🎉\n"
                                f"You can now start the tournament whenever you are ready using the **Start Tournament** button on the panel above!"
                            )
                            if asyncio.iscoroutine(coro) or hasattr(coro, '__await__'):
                                await coro
                    except Exception:
                        log.exception("Failed to send tournament full notification")
            else:
                self.cog._active.discard(uid)
                if self.reg.config.entry_fee > 0:
                    try:
                        await mutate_user(uid, lambda p: p.__setitem__('coins', int(p.get('coins', 0)) + self.reg.config.entry_fee))
                    except Exception:
                        log.exception("Failed to refund entry fee for %s on leave", uid)
                if len(self.reg.entrants) < self.reg.config.slots and self.reg.notified_full:
                    self.reg.notified_full = False
                    await asyncio.to_thread(self.cog.registration_store.write, self.reg)

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
