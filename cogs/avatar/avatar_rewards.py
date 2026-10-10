"""Durable, individually claimable avatar rewards. No duplicate auto-refunds."""
import asyncio
import logging
import time
from uuid import uuid4

import discord
from discord.ext import commands, tasks
from . import avatar_collection as AC, avatar_config as C, avatar_progress as AP

log = logging.getLogger(__name__)


def enqueue(profile, aid, *, reward_id=None, sell_value=None, channel_id=None, now=None):
    from .avatar_engine import avatar_engine
    card = avatar_engine.get_avatar(aid)
    if not card:
        raise AP.PurchaseError('Avatar reward definition unavailable.')
    rewards = profile.setdefault('avatar_rewards', {})
    rid = str(reward_id or uuid4().hex)
    if rid in rewards:
        return dict(rewards[rid])
    stamp = time.time() if now is None else now
    # Preserve explicit catalog prices. Pack callers supply their existing
    # rarity/pack refund amount as the manual sell value. Other rewards with
    # no authored sell value remain zero; do not invent new economy values.
    value = card.get('sell_value', card.get('sell_price', 0)) if sell_value is None else sell_value
    reward = {'id': rid, 'avatar_id': aid, 'name': card['name'],
              'category': AC.category(card), 'sell_value': max(0, int(value)),
              'state': 'pending', 'created_at': stamp,
              'expires_at': stamp + C.REWARD_TIMEOUT, 'channel_id': channel_id,
              'message_id': None}
    rewards[rid] = reward
    return dict(reward)


def decide(profile, rid, choice, *, now=None):
    if choice not in ('keep', 'sell'):
        raise AP.PurchaseError('Unknown reward decision.')
    reward = profile.get('avatar_rewards', {}).get(rid)
    if not reward:
        raise AP.PurchaseError('Reward unavailable.')
    if reward['state'] != 'pending':
        return dict(reward)  # Idempotent, including concurrent Keep/Sell.
    stamp = time.time() if now is None else now
    if stamp >= reward['expires_at']:
        choice = 'keep'
    if choice == 'keep':
        AC.grant(profile, reward['avatar_id'])
    else:
        profile['coins'] = int(profile.get('coins', 0)) + reward['sell_value']
    reward.update(state=choice, resolved_at=stamp)
    return dict(reward)


def reward_embed(profile, reward):
    copies = AC.spare_copies(profile, reward['avatar_id']) + int(
        reward['avatar_id'] in profile.get('avatar_inventory', []))
    return discord.Embed(title='NEW AVATAR OBTAINED!', colour=0xF1C40F,
        description=(f"Avatar: **{reward['name']}**\nCategory: **{reward['category']}**\n"
                     f"Owned Copies: **{copies}**\nSell Value: **{reward['sell_value']:,} Beycoins**\n\n"
                     'Would you like to keep or sell this avatar?\n'
                     f"No decision by <t:{int(reward['expires_at'])}:R> → Keep."))


class RewardView(discord.ui.View):
    def __init__(self, owner, reward):
        super().__init__(timeout=None)
        self.owner, self.reward = owner, reward
        for choice, label, style in [('keep', 'KEEP AVATAR', discord.ButtonStyle.success),
                                     ('sell', 'SELL AVATAR', discord.ButtonStyle.secondary)]:
            button = discord.ui.Button(label=label, style=style,
                custom_id=f"avatar_reward:{reward['id']}:{choice}")
            async def callback(interaction, choice=choice):
                from utils.database import mutate_user
                result = await mutate_user(self.owner, lambda p: decide(p, self.reward['id'], choice))
                for child in self.children:
                    child.disabled = True
                await interaction.response.edit_message(
                    embed=discord.Embed(title=f"Avatar {result['state']}", colour=0x2ECC71,
                        description=f"{result['name']} · " + (
                            f"{result['sell_value']:,} Beycoins awarded." if result['state'] == 'sell'
                            else 'Saved to your collection. No duplicate refund.')),
                    view=self)
                self.stop()
            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner:
            await interaction.response.send_message('Only the receiving player can decide.', ephemeral=True)
            return False
        return True


async def present(bot, owner, reward, destination=None):
    """Send or reattach a saved decision; failures leave it recoverable."""
    from utils.database import get_user, mutate_user
    if destination is None:
        if reward.get('channel_id'):
            destination = bot.get_channel(int(reward['channel_id'])) or await bot.fetch_channel(int(reward['channel_id']))
        else:
            user = bot.get_user(owner) or await bot.fetch_user(owner)
            destination = user.dm_channel or await user.create_dm()
    view = RewardView(owner, reward)
    if reward.get('message_id'):
        bot.add_view(view, message_id=int(reward['message_id']))
        return
    profile = await get_user(owner)
    message = await destination.send(content=f'<@{owner}>', embed=reward_embed(profile, reward), view=view)
    def record(p):
        row = p['avatar_rewards'][reward['id']]
        row.update(message_id=message.id, channel_id=message.channel.id)
    await mutate_user(owner, record, touch=False)


class AvatarRewards(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.attached = set()

    async def cog_load(self):
        self.recover.start()

    def cog_unload(self):
        self.recover.cancel()

    @tasks.loop(seconds=C.REWARD_POLL_SECONDS)
    async def recover(self):
        from utils import database as DB
        try:
            profiles = await asyncio.to_thread(DB.USER_STORE.load_all)
        except Exception:
            log.exception('Avatar reward scan failed; retrying next interval')
            return
        for uid, profile in profiles.items():
            for rid, reward in profile.get('avatar_rewards', {}).items():
                if reward['state'] != 'pending':
                    continue
                try:
                    if time.time() >= reward['expires_at']:
                        result = await DB.mutate_user(int(uid), lambda p: decide(p, rid, 'keep'), touch=False)
                        if reward.get('message_id') and reward.get('channel_id'):
                            channel = self.bot.get_channel(int(reward['channel_id'])) or await self.bot.fetch_channel(int(reward['channel_id']))
                            message = channel.get_partial_message(int(reward['message_id']))
                            await message.edit(embed=discord.Embed(title=f"Avatar {result['state']}",
                                description=f"{result['name']} · Decision saved.", colour=0x2ECC71), view=None)
                    elif (uid, rid) not in self.attached:
                        await present(self.bot, int(uid), reward)
                        self.attached.add((uid, rid))
                except Exception:
                    log.exception('Could not recover avatar reward %s for %s', rid, uid)

    @recover.before_loop
    async def before_recover(self):
        await self.bot.wait_until_ready()

    @commands.command(name='avatarrewards')
    async def pending(self, ctx):
        """Recover pending avatar decisions if their original message is unavailable."""
        from utils.database import get_user
        profile = await get_user(ctx.author.id)
        rows = [r for r in profile.get('avatar_rewards', {}).values() if r['state'] == 'pending']
        if not rows:
            return await ctx.send('No pending avatar rewards.')
        for reward in rows:
            await ctx.send(embed=reward_embed(profile, reward), view=RewardView(ctx.author.id, reward))
