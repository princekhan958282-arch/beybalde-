"""
wallet_card.py  —  💳 The unified balance card

There are two separate currencies and they used to live in two separate
commands: `;bal` showed Beycoins, `;casinobal` showed casino coins, and nothing
showed the exchange rate or your current tax. So the one question people
actually ask — "how much do I have?" — took two commands and a mental note.

This is one card with both, plus the things you'd immediately want next:

  * both daily bonuses, with a live countdown when they're not ready
  * the exchange rate and *your* tax (premium passes lower it)
  * one button that claims everything that's claimable

Commands:
    ;bal / ;balance / ;coins / ;wallet   → the card
    ;bal @user                           → someone else's balances
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands

from cogs.casino import casino_premium, casino_wallet
from utils.database import get_user, update_user

# Kept in sync with cogs/economy/shop.py — the Beycoin daily lives there.
BEYCOIN_DAILY_COOLDOWN_H = 4


def _beycoin_daily_ready(profile: dict) -> tuple[bool, Optional[int]]:
    """(ready, unix_timestamp_when_ready). Timestamp is None when ready now."""
    last_s = profile.get("last_daily")
    if not last_s:
        return True, None
    try:
        last = datetime.fromisoformat(last_s)
    except (TypeError, ValueError):
        return True, None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    ready_at = last + timedelta(hours=BEYCOIN_DAILY_COOLDOWN_H)
    now = datetime.now(timezone.utc)
    if now >= ready_at:
        return True, None
    return False, int(ready_at.timestamp())


async def _casino_daily_ready(user_id: int) -> bool:
    """Casino daily resets on a UTC day boundary."""
    try:
        data = await casino_wallet._load_async()
        last = data.get(str(user_id), {}).get("last_daily", 0)
        return last != int(time.time() // 86400)
    except Exception:
        return True


async def build_wallet_embed(target: discord.abc.User) -> discord.Embed:
    """The card itself. Two inline fields so a phone lays them out side by side."""
    profile   = get_user(target.id)
    beycoins  = profile.get("coins", 0)
    casino    = await casino_wallet.get_balance(target.id)
    tax       = await casino_premium.get_exchange_tax(target.id)
    max_bet   = await casino_premium.get_max_bet(target.id)
    prem      = await casino_premium.get_premium(target.id)
    c_daily   = await casino_premium.get_daily_bonus(target.id)

    bey_ready, bey_at = _beycoin_daily_ready(profile)
    cas_ready         = await _casino_daily_ready(target.id)

    e = discord.Embed(
        title=f"💳  {target.display_name}'s Wallet",
        color=0xf1c40f,
    )
    e.set_thumbnail(url=target.display_avatar.url)

    e.add_field(name="🪙 Beycoins",
                value=f"**{beycoins:,}**", inline=True)
    e.add_field(name="🎰 Casino Coins",
                value=f"**{casino:,}**", inline=True)

    daily_bits = []
    daily_bits.append("🪙 `;daily` — **ready**" if bey_ready
                      else f"🪙 `;daily` — <t:{bey_at}:R>")
    daily_bits.append(f"🎰 `;casinodaily` — **ready** (+{c_daily:,})" if cas_ready
                      else "🎰 `;casinodaily` — tomorrow")
    e.add_field(name="🎁 Dailies", value="\n".join(daily_bits), inline=False)

    tier = casino_premium.PACKS[prem["key"]]["display"] if prem else "No pass"
    e.add_field(
        name="💱 Exchange",
        value=(f"100 🪙 → 60 🎰  ·  sell tax **{tax:.0%}**"
               + ("  — tax free!" if tax == 0 else "")
               + f"\n{tier} · max bet 🎰 {max_bet:,}"),
        inline=False,
    )
    e.set_footer(text="`;casinoexchange buy|sell <amount>` to move coins between wallets")
    return e


class WalletView(discord.ui.View):
    """Claim buttons live here so the card is also the place you act."""

    def __init__(self, owner: discord.abc.User, shop_ctx: commands.Context):
        super().__init__(timeout=120)
        self.owner    = owner
        self.ctx      = shop_ctx
        self.message: Optional[discord.Message] = None

    def _owns(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner.id

    @discord.ui.button(label="Claim dailies", emoji="🎁",
                       style=discord.ButtonStyle.success)
    async def claim(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self._owns(interaction):
            return await interaction.response.send_message(
                "That's not your wallet — run `;bal` yourself!", ephemeral=True)

        await interaction.response.defer()
        claimed = []

        # ── Casino daily: the wallet module owns this one outright ───────────
        got, amount = await casino_wallet.claim_daily(self.owner.id)
        if got:
            claimed.append(f"🎰 **+{amount:,}** casino coins")

        # ── Beycoin daily: reuse the shop cog so the reward roll and the
        #    cooldown stay defined in exactly one place ─────────────────────
        profile = get_user(self.owner.id)
        ready, _at = _beycoin_daily_ready(profile)
        if ready:
            shop = interaction.client.get_cog("Shop") or interaction.client.get_cog("ShopCog")
            cmd  = getattr(shop, "daily", None) if shop else None
            if cmd is not None and self.ctx is not None:
                try:
                    await cmd(self.ctx)
                    claimed.append("🪙 Beycoin daily claimed")
                except Exception:
                    claimed.append("🪙 run `;daily` for your Beycoins")
            else:
                claimed.append("🪙 run `;daily` for your Beycoins")

        if not claimed:
            await interaction.followup.send(
                "⏰ Both dailies are already claimed — check the card for timers.",
                ephemeral=True)
        else:
            await interaction.followup.send("\n".join(claimed), ephemeral=True)

        try:
            await self.message.edit(embed=await build_wallet_embed(self.owner),
                                    view=self)
        except Exception:
            pass

    @discord.ui.button(label="Exchange", emoji="💱",
                       style=discord.ButtonStyle.secondary)
    async def exchange(self, interaction: discord.Interaction, _: discord.ui.Button):
        tax  = await casino_premium.get_exchange_tax(interaction.user.id)
        base = casino_premium.BASE_EXCHANGE_TAX
        e = discord.Embed(
            title="💱  Moving Coins Between Wallets",
            description=(
                "**Buy** `;casinoexchange buy <beycoins>`\n"
                "100 🪙 Beycoins → 60 🎰 casino coins\n\n"
                "**Sell** `;casinoexchange sell <casino coins>`\n"
                f"Your tax right now: **{tax:.0%}** (base {base:.0%})"
            ),
            color=0x9b59b6,
        )
        if tax > 0:
            e.add_field(
                name="👑 Lower it",
                value=("Pro **6%** · Elite **3%** · Legend **0%**\n"
                       "`;premium info`"),
                inline=False,
            )
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="Casino", emoji="🎰",
                       style=discord.ButtonStyle.secondary)
    async def casino(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "Run **`;casino`** to open the lobby — 19 games in one menu.",
            ephemeral=True)

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class WalletCog(commands.Cog, name="Wallet"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="bal",
                      aliases=["balance", "wallet", "coins", "money", "purse"])
    async def bal(self, ctx: commands.Context, member: discord.Member = None):
        """💳 Both balances, both dailies, and your exchange tax in one card."""
        target = member or ctx.author
        embed  = await build_wallet_embed(target)

        if target.id != ctx.author.id:
            embed.set_footer(text=f"Viewed by {ctx.author.display_name}")
            return await ctx.send(embed=embed)

        view = WalletView(ctx.author, ctx)
        view.message = await ctx.send(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(WalletCog(bot))
