"""Owner-only, expiring avatar upgrade controls."""
import asyncio
from weakref import WeakValueDictionary
import logging
import random
import time
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
    e.add_field(name='Category', value=AC.category(card), inline=False)
    e.add_field(name='Permanent star bonuses',
                value=', '.join(f"+{sg[k]} {k.upper()}" for k in sg), inline=False)
    e.add_field(name='Card-level allocation',
                value=', '.join(f"+{gain[k]} {k.upper()}" for k in gain) +
                      f" · {sum(gain.values())} total points", inline=False)
    from .avatar_scaling import scaled_card, selected_stats
    scaled = scaled_card(profile, card)
    active = AS.active_slot(profile, scaled)
    block = AS.bonuses_for(scaled, active)
    selected = selected_stats(scaled, active)
    totals = []
    for stat, key in [('attack', 'attack'), ('defense', 'defence'), ('stamina', 'stamina')]:
        flat = block.get(key + '_flat', 0) + gain[stat] + sg[stat] + selected[stat]
        pct = block.get(key + '_percent', 0)
        totals.append(f"{stat.upper()}: +{flat:g} flat, +{pct:.0%}")
    e.add_field(name='Total avatar stats (current skill selection)', value='\n'.join(totals), inline=False)
    fed = AC.stages(profile, aid)
    if star < C.MAX_STARS:
        target = star + 1
        required = C.STAR_COPY_COST[target]
        chance = C.STAR_SUCCESS.get(target)
        e.add_field(name=f'Feeding progress for {target}★',
                    value=f"{fed}/{required} same-category cards fed · {max(0, required-fed)} still required", inline=False)
        risk = ('\nFailure permanently destroys this avatar. Fed cards are never refunded.'
                if target > C.SAFE_STARS else '\nGuaranteed success.')
        e.add_field(name=f'Next: {target}★', value=(f"{chance:.0%} success" + risk if chance is not None
                    else 'Success probability pending approval · Upgrade and feeding locked.'), inline=False)
    else:
        e.add_field(name='Stars', value='MAX · 15★', inline=False)
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
        self.deadline = time.monotonic() + timeout

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner:
            await interaction.response.send_message('Only the player who opened this view can use it.', ephemeral=True)
            return False
        if self.closed or time.monotonic() >= self.deadline:
            self.close()
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
        self.generation = AP.card_entry(profile, card['id']).get('generation')
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
                 f"Already fed: {AC.stages(profile, self.card['id'])}. No additional cards or currency charged.")
        if self.target == C.MAX_STARS:
            text += '\nThis is the highest and riskiest tier.'
        return discord.Embed(title='⚠️ WARNING: Risky Upgrade ⚠️' if risky else 'Confirm Star Up',
                             description=text, colour=0xED4245 if risky else 0xF1C40F)

    @discord.ui.button(label='Confirm', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        # No await before closing: queued clicks cannot start a second attempt.
        if self.closed or time.monotonic() >= self.deadline:
            self.close()
            return await interaction.response.send_message('This attempt was already handled.', ephemeral=True)
        self.close()
        await interaction.response.edit_message(view=self)
        try:
            async with user_lock(self.owner):
                result = await mutate_user(self.owner, lambda p: AC.upgrade_star(
                    p, self.card['id'], self.target, random.random, generation=self.generation))
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
        self.deadline = time.monotonic() + C.PROFILE_TIMEOUT
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
        required = C.STAR_COPY_COST.get(star + 1, 0)
        approved = star + 1 in C.STAR_SUCCESS
        self.star_up.disabled = not owned or not approved or stage < required
        from .avatar_engine import avatar_engine
        catalog = {c['id']: c for c in avatar_engine.get_all_avatars()}
        self.feed_stage.disabled = (not owned or not approved or stage >= required or
                                   not AC.eligible_materials(profile, aid, catalog))
        if not owned:
            self.close()

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
                        skill_levels=self.skill_levels, active_skill_slot=self.active_slot,
                        feeding_progress=AC.stages(p, card['id']))
                    if buf is not None:
                        attachments.append(discord.File(buf, filename='ainfo.jpg'))
                        # Keep V4 progression details alongside the unchanged image template.
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
        generation = AP.card_entry(p, aid).get('generation')
        expected = (AP.card_level(p, aid) if kind == 'card' else
                    AP.skill_level(p, aid, AP.slugify(self.card['skills'][slot - 1]['name'])))
        def apply(profile):
            AC.require_owned(profile, aid)
            if AP.card_entry(profile, aid).get('generation') != generation:
                raise AP.PurchaseError('This is a different copy. Open the avatar again.')
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
                raise AP.PurchaseError('This avatar is already at 15★.')
        except AP.PurchaseError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        target = AC.stars(p, self.card['id']) + 1
        if target not in C.STAR_SUCCESS:
            return await interaction.response.send_message('Success rate pending approval.', ephemeral=True)
        if AC.stages(p, self.card['id']) < C.STAR_COPY_COST[target]:
            return await interaction.response.send_message('Finish feeding before Star Up.', ephemeral=True)
        view = StarConfirm(self.owner, self.card, p, self)
        await interaction.response.send_message(embed=view.embed(p), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @discord.ui.button(label='Feed Stage', style=discord.ButtonStyle.secondary)
    async def feed_stage(self, interaction, button):
        from .avatar_engine import avatar_engine
        p = await get_user(self.owner)
        aid = self.card['id']
        catalog = {c['id']: c for c in avatar_engine.get_all_avatars()}
        try:
            AC.require_owned(p, aid)
            if AC.stars(p, aid) + 1 not in C.STAR_SUCCESS:
                raise AP.PurchaseError('Next success rate is pending approval.')
            materials = AC.eligible_materials(p, aid, catalog)
            if not materials:
                raise AP.PurchaseError('No eligible same-category spare cards.')
        except AP.PurchaseError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        selection = FeedSelection(self.owner, self, p, catalog, materials)
        await interaction.response.send_message(
            content='Choose a spare card to feed, then enter the quantity. Feeding permanently consumes those copies.',
            view=selection, ephemeral=True)
        selection.message = await interaction.original_response()


class FeedAmount(discord.ui.Modal, title='Feed same-category copies'):
    quantity = discord.ui.TextInput(label='Number of spare copies', default='1', max_length=3)

    def __init__(self, selection, material):
        super().__init__(timeout=C.CONFIRM_TIMEOUT)
        self.selection, self.material = selection, material

    async def on_submit(self, interaction):
        v = self.selection
        if interaction.user.id != v.owner or v.closed or time.monotonic() >= v.deadline:
            return await interaction.response.send_message('This feeding selection is closed.', ephemeral=True)
        try:
            count = int(self.quantity.value)
        except ValueError:
            return await interaction.response.send_message('Enter a positive whole number.', ephemeral=True)
        v.close()
        await v.parent.apply(interaction, lambda p: AC.feed(p, v.parent.card['id'], v.expected,
            {self.material: count}, v.catalog, expected_star=v.star, generation=v.generation))
        if v.message:
            await v.message.edit(content='Feeding selection handled. Open Feed Stage again to feed more.', view=v)


class FeedSelect(discord.ui.Select):
    def __init__(self, selection):
        items = list(selection.materials.items())[selection.page * 25:(selection.page + 1) * 25]
        super().__init__(placeholder='Select feeding card', options=[discord.SelectOption(
            label=selection.catalog[aid]['name'][:100], value=aid,
            description=f'{count} spare copies') for aid, count in items])

    async def callback(self, interaction):
        await interaction.response.send_modal(FeedAmount(self.view, self.values[0]))


class FeedSelection(OwnedView):
    def __init__(self, owner, parent, profile, catalog, materials):
        super().__init__(owner, C.PROFILE_TIMEOUT)
        self.parent, self.catalog, self.materials = parent, catalog, materials
        aid = parent.card['id']
        self.expected, self.star = AC.stages(profile, aid), AC.stars(profile, aid)
        self.generation = AP.card_entry(profile, aid).get('generation')
        self.page = 0
        self.menu()

    def menu(self):
        for child in list(self.children):
            if isinstance(child, FeedSelect):
                self.remove_item(child)
        self.add_item(FeedSelect(self))
        self.previous.disabled = self.page == 0
        self.next_page.disabled = (self.page + 1) * 25 >= len(self.materials)

    @discord.ui.button(label='Previous', style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page = max(0, self.page - 1)
        self.menu()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label='Next', style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        self.page = min((len(self.materials) - 1) // 25, self.page + 1)
        self.menu()
        await interaction.response.edit_message(view=self)
