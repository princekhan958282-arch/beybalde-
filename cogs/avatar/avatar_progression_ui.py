"""Owner-only, expiring avatar upgrade controls."""
import asyncio
from weakref import WeakValueDictionary
import logging
import random
import discord
from utils.database import get_user, mutate_user
from . import avatar_config as C, avatar_progress as AP, avatar_levels as AL
from . import avatar_collection as AC, avatar_skills as AS
from .avatar_shop import AvatarSkillsView
from .avatar_utils import build_avatar_embed

log = logging.getLogger(__name__)
_locks = WeakValueDictionary()


def user_lock(uid):
    lock = _locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _locks[uid] = lock
    return lock


def bar(now, maximum):
    return '▰' * now + '▱' * (maximum - now)


def progression_embed(profile, card, slot=1):
    aid = card['id']
    q = AP.quote_card(profile, aid)
    star = AC.stars(profile, aid)
    e = discord.Embed(title=f"{card['name']} · {star}★", colour=0xF1C40F)
    gain = AL.card_stat_bonus(card.get('type'), q['from'])
    sg = AC.stat_bonus(profile, card)
    e.add_field(name=f"Level {q['from']}/{C.MAX_CARD_LEVEL}",
                value=f"{bar(q['from'], C.MAX_CARD_LEVEL)}\n" +
                ', '.join(f"+{gain[s] + sg[s]} {s.upper()}" for s in gain) +
                ('\nMAX' if q['maxed'] else f"\nNext level: {q['cost']:,} coins"), inline=False)
    e.add_field(name='Copies', value=f"Spare copies: {AC.spare_copies(profile, aid)}\nThe owned/equipped card is never a feeding copy.", inline=False)
    if star == C.SAFE_STARS:
        st = AC.stages(profile, aid)
        e.add_field(name=f'Stages {st}/{C.STAGE_COUNT}', value=f"{bar(st, C.STAGE_COUNT)}\nFeed stage: {C.STAGE_COPY_COST} spare copy", inline=False)
    if star < C.MAX_STARS:
        target = star + 1
        e.add_field(name=f'Next: {target}★', value=f"{C.STAR_COPY_COST[target]} spare copies · {C.STAR_SUCCESS[target]:.0%} success", inline=False)
    else:
        e.add_field(name='Stars', value='MAX · 7★', inline=False)
    for i, skill in enumerate(card.get('skills', []), 1):
        slug = AP.slugify(skill['name'])
        sq = AP.quote_skill(profile, aid, slug)
        if card.get('active_battle_skills'):
            now = C.EMPOWER_PERCENT_STEP * (sq['from'] - 1)
            nxt = C.EMPOWER_PERCENT_STEP * (sq['to'] - 1)
            preview = f"Activation ATK/DEF boost: {now:.0%} → {nxt:.0%}, {C.EMPOWER_ROUNDS} rounds"
        else:
            preview = f"Numeric effect magnitude: ×{AL.skill_magnitude_mult(sq['from']):.2f} → ×{AL.skill_magnitude_mult(sq['to']):.2f}\nSelected skill stat bonus: +{C.SKILL_STAT_GAIN['attack'] * (sq['from'] - 1)} → +{C.SKILL_STAT_GAIN['attack'] * (sq['to'] - 1)} ATK/DEF/STM"
        e.add_field(name=f"{'✅ ' if i == slot else ''}{skill['name']} · Lv{sq['from']}/{C.MAX_SKILL_LEVEL}",
                    value=preview + '\n' + (sq['blocked'] or f"Next: {sq['cost']:,} coins"), inline=False)
    e.set_footer(text=f"Balance: {int(profile.get('coins', 0)):,} coins · Choose a skill to level it")
    return e


class OwnedView(discord.ui.View):
    def __init__(self, owner, timeout):
        super().__init__(timeout=timeout)
        self.owner = owner
        self.message = None
        self.closed = False

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner:
            await interaction.response.send_message('Only the player who opened this view can use it.', ephemeral=True)
            return False
        if self.closed:
            await interaction.response.send_message('This view is closed. Open the avatar again.', ephemeral=True)
            return False
        return True

    def close(self):
        self.closed = True
        for item in self.children:
            item.disabled = True
        self.stop()

    async def on_timeout(self):
        self.close()
        if self.message:
            try:
                await self.message.edit(content='Upgrade cancelled: confirmation timed out. No cost.', view=self)
            except discord.HTTPException:
                pass


class StarConfirm(OwnedView):
    def __init__(self, owner, card, profile, parent):
        super().__init__(owner, C.CONFIRM_TIMEOUT)
        self.card, self.parent = card, parent
        self.target = AC.stars(profile, card['id']) + 1
        risky = self.target > C.SAFE_STARS
        self.confirm.label = '⚠️ I Understand, Try It' if risky else 'Confirm Star Up'
        self.confirm.style = discord.ButtonStyle.danger if risky else discord.ButtonStyle.success

    def embed(self, profile):
        risky = self.target > C.SAFE_STARS
        text = (f"You are trying to upgrade {self.card['name']} to {self.target}★.\n"
                f"Success chance: {C.STAR_SUCCESS[self.target]:.0%}\n")
        if risky:
            text += 'If it FAILS, you will PERMANENTLY LOSE this avatar. It cannot be recovered.\n'
        text += (f"Copies that will be used: {C.STAR_COPY_COST[self.target]}\n"
                 f"Copies owned: {AC.spare_copies(profile, self.card['id'])}")
        if self.target == C.MAX_STARS:
            text += '\nThis is the highest and riskiest tier.'
        return discord.Embed(title='⚠️ WARNING: Risky Upgrade ⚠️' if risky else 'Confirm Star Up',
                             description=text, colour=0xED4245 if risky else 0xF1C40F)

    @discord.ui.button(label='Confirm', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        # No await before closing: queued clicks cannot start a second attempt.
        if self.closed:
            return await interaction.response.send_message('This attempt was already handled.', ephemeral=True)
        self.close()
        await interaction.response.edit_message(view=self)
        try:
            async with user_lock(self.owner):
                result = await mutate_user(self.owner, lambda p: AC.upgrade_star(
                    p, self.card['id'], self.target, random.random))
        except AP.PurchaseError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            log.exception('Avatar star transaction failed')
            await interaction.followup.send('Upgrade could not be saved. Open the avatar to check its current state.', ephemeral=True)
            return
        success = result['success']
        e = discord.Embed(title='✨ STAR UPGRADE SUCCESS! ✨' if success else '💔 Avatar permanently lost',
                          description=(f"{self.card['name']} reached **{self.target}★**!" if success else
                                       f"{self.card['name']} was permanently removed. It cannot be recovered.") +
                          f"\nCopies consumed: {result['copies']}", colour=0x2ECC71 if success else 0xED4245)
        await interaction.edit_original_response(embed=e, view=self)
        await self.parent.refresh_message()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.close()
        await interaction.response.edit_message(content='Cancelled. No copies consumed.', view=self)


class SkillSelect(discord.ui.Select):
    def __init__(self, card, slot):
        super().__init__(placeholder='Choose skill to level up', options=[
            discord.SelectOption(label=s['name'], value=str(i), default=i == slot)
            for i, s in enumerate(card['skills'], 1)])

    async def callback(self, interaction):
        self.view.slot = int(self.values[0])
        await interaction.response.defer()
        await self.view.refresh_message()


class ProgressionView(AvatarSkillsView):
    interaction_check = OwnedView.interaction_check
    close = OwnedView.close

    def __init__(self, owner, card, profile, *, bot=None):
        super().__init__(card, timeout=C.PROFILE_TIMEOUT, details_embed=build_avatar_embed(
            card, owned=True, level=AP.card_level(profile, card['id'])))
        self.owner, self.message, self.closed, self.busy = owner, None, False, False
        self.bot = bot
        self.card, self.slot = card, 1
        if card.get('skills'):
            self.add_item(SkillSelect(card, self.slot))
        self.configure(profile)

    def configure(self, profile):
        aid = self.card['id']
        self.skill_levels = AP.card_entry(profile, aid).get('skills', {})
        self.active_slot = AS.chosen_slot(profile, aid) if self.card.get('skills') else 0
        self.details_embed = build_avatar_embed(self.card, owned=True,
            equipped=profile.get('equipped_avatar') == aid,
            level=AP.card_level(profile, aid), skill_levels=self.skill_levels,
            active_skill_slot=self.active_slot)
        owned = aid in profile.get('avatar_inventory', [])
        q = AP.quote_card(profile, aid)
        self.level_up.disabled = not owned or q['maxed'] or q['coins'] < q['cost']
        skills = self.card.get('skills', [])
        sq = AP.quote_skill(profile, aid, AP.slugify(skills[self.slot - 1]['name'])) if skills else None
        self.skill_up.disabled = not owned or not sq or bool(sq['blocked']) or sq['coins'] < sq['cost']
        star, stage = AC.stars(profile, aid), AC.stages(profile, aid)
        self.star_up.label = f'Try {star + 1}★' if star >= C.SAFE_STARS and star < C.MAX_STARS else 'Star Up'
        self.star_up.style = discord.ButtonStyle.danger if star >= C.SAFE_STARS else discord.ButtonStyle.success
        self.star_up.disabled = (not owned or star >= C.MAX_STARS or
            (star == C.SAFE_STARS and stage < C.STAGE_COUNT) or
            AC.spare_copies(profile, aid) < C.STAR_COPY_COST.get(star + 1, 0))
        self.feed_stage.disabled = not owned or star != C.SAFE_STARS or stage >= C.STAGE_COUNT or AC.spare_copies(profile, aid) < C.STAGE_COPY_COST

    async def on_timeout(self):
        self.close()
        if self.message:
            try:
                await self.message.edit(content='View expired. Open ;ainfo again.', view=self)
            except discord.HTTPException:
                pass

    async def refresh_message(self):
        p = await get_user(self.owner)
        self.configure(p)
        if self.message:
            owned = self.card['id'] in p.get('avatar_inventory', [])
            e = progression_embed(p, self.card, self.slot) if owned else discord.Embed(
                title='Avatar lost', description='This avatar is no longer in your inventory.', colour=0xED4245)
            attachments = []
            if owned:
                try:
                    from utils.avatar_info_card import render_avatar_info_card, resolve_avatar_image_url
                    card = dict(self.card)
                    card['image'] = await resolve_avatar_image_url(self.bot, card.get('image'))
                    buf = await asyncio.to_thread(render_avatar_info_card, card, owned=True,
                        equipped=p.get('equipped_avatar') == card['id'],
                        level=AP.card_level(p, card['id']), stars=AC.stars(p, card['id']),
                        skill_levels=self.skill_levels, active_skill_slot=self.active_slot)
                    if buf is not None:
                        attachments.append(discord.File(buf, filename='ainfo.jpg'))
                except Exception:
                    log.exception('Avatar card refresh failed; using progression embed')
            await self.message.edit(embed=e, view=self, attachments=attachments)

    async def purchase(self, interaction, kind):
        if self.busy:
            return await interaction.response.send_message('An upgrade is already in progress.', ephemeral=True)
        self.busy = True
        try:
            await self._purchase(interaction, kind)
        finally:
            self.busy = False

    async def _purchase(self, interaction, kind):
        await interaction.response.defer(ephemeral=True)
        aid, slot = self.card['id'], self.slot
        p = await get_user(self.owner)
        expected = (AP.card_level(p, aid) if kind == 'card' else
                    AP.skill_level(p, aid, AP.slugify(self.card['skills'][slot - 1]['name'])))
        def apply(profile):
            AC.require_owned(profile, aid)
            if kind == 'card':
                if AP.card_level(profile, aid) != expected:
                    raise AP.PurchaseError('Avatar level changed. Refresh the view.')
                return AP.apply_card_purchase(profile, aid)
            slug = AP.slugify(self.card['skills'][slot - 1]['name'])
            if AP.skill_level(profile, aid, slug) != expected:
                raise AP.PurchaseError('Skill level changed. Refresh the view.')
            return AP.apply_skill_purchase(profile, aid, slug)
        await self.apply(interaction, apply, deferred=True)

    async def apply(self, interaction, fn, *, deferred=False):
        if not deferred:
            await interaction.response.defer(ephemeral=True)
        try:
            async with user_lock(self.owner):
                await mutate_user(self.owner, fn)
        except AP.PurchaseError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            log.exception('Avatar progression failed')
            await interaction.followup.send('Could not save the upgrade. Reopen the avatar to check its state.', ephemeral=True)
        else:
            await interaction.followup.send('Upgrade saved.', ephemeral=True)
        await self.refresh_message()

    @discord.ui.button(label='Level Up', style=discord.ButtonStyle.primary)
    async def level_up(self, interaction, button):
        await self.purchase(interaction, 'card')

    @discord.ui.button(label='Skill Level Up', style=discord.ButtonStyle.primary)
    async def skill_up(self, interaction, button):
        await self.purchase(interaction, 'skill')

    @discord.ui.button(label='Star Up', style=discord.ButtonStyle.success)
    async def star_up(self, interaction, button):
        p = await get_user(self.owner)
        try:
            AC.require_owned(p, self.card['id'])
            if AC.stars(p, self.card['id']) >= C.MAX_STARS:
                raise AP.PurchaseError('This avatar is already at 7★.')
        except AP.PurchaseError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        view = StarConfirm(self.owner, self.card, p, self)
        await interaction.response.send_message(embed=view.embed(p), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @discord.ui.button(label='Feed Stage', style=discord.ButtonStyle.secondary)
    async def feed_stage(self, interaction, button):
        if self.busy:
            return await interaction.response.send_message('An upgrade is already in progress.', ephemeral=True)
        self.busy = True
        try:
            await interaction.response.defer(ephemeral=True)
            p = await get_user(self.owner)
            expected = AC.stages(p, self.card['id'])
            await self.apply(interaction, lambda p: AC.feed(p, self.card['id'], expected), deferred=True)
        finally:
            self.busy = False
