"""
wheel.py  —  🎡 Wheel of Fortune

Spin a weighted wheel. One spin, one multiplier, instant result.
RTP ≈ 95.6%

Usage:  ;wheel <bet>          (aliases: ;spin, ;wof)
"""

import asyncio
import random
from typing import Optional

import discord
from discord.ext import commands

from . import casino_premium, casino_wallet

MIN_BET = 10

# (multiplier, weight, emoji, colour)
SEGMENTS = [
    (0.0,  47.5, "💀", 0xe74c3c),
    (0.4,  15.0, "🟥", 0xc0392b),
    (1.0,  15.0, "🟨", 0xf1c40f),
    (1.5,  10.0, "🟩", 0x2ecc71),
    (2.0,   7.5, "🟦", 0x3498db),
    (3.0,   4.5, "🟪", 0x9b59b6),
    (5.0,   2.3, "🟧", 0xe67e22),
    (10.0,  1.0, "⭐", 0xf39c12),
    (50.0,  0.25, "💎", 0x1abc9c),
]

_MULTS   = [s[0] for s in SEGMENTS]
_WEIGHTS = [s[1] for s in SEGMENTS]
_TOTAL_W = sum(_WEIGHTS)

# Visual ring used for the spin animation
_RING = "".join(s[2] for s in SEGMENTS)


def _spin() -> tuple[float, str, int]:
    idx = random.choices(range(len(SEGMENTS)), weights=_WEIGHTS, k=1)[0]
    mult, _w, emoji, colour = SEGMENTS[idx]
    return mult, emoji, colour


def _paytable_text() -> str:
    lines = []
    for mult, w, emoji, _c in SEGMENTS:
        chance = w / _TOTAL_W * 100
        label = "LOSE" if mult == 0 else f"{mult:g}x"
        lines.append(f"{emoji} `{label:>5}`  —  {chance:5.2f}%")
    return "\n".join(lines)


class WheelView(discord.ui.View):
    """Post-result view: lets the player re-spin the same bet."""

    def __init__(self, cog, player: discord.Member, bet: int):
        super().__init__(timeout=60)
        self.cog    = cog
        self.player = player
        self.bet    = bet
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="🎡  Spin Again", style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your wheel.", ephemeral=True)

        max_bet = await casino_premium.get_max_bet(self.player.id)
        if self.bet > max_bet:
            return await interaction.response.send_message(
                f"Your max bet is now 🪙 {max_bet:,}.", ephemeral=True)
        if not await casino_wallet.deduct(self.player.id, self.bet):
            return await interaction.response.send_message(
                "❌ Not enough casino coins.", ephemeral=True)

        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

        msg = await interaction.followup.send(embed=_spinning_embed(self.player, self.bet), wait=True)
        await self.cog._run_spin(msg, self.player, self.bet)

    @discord.ui.button(label="📜  Paytable", style=discord.ButtonStyle.secondary)
    async def paytable(self, interaction: discord.Interaction, button: discord.ui.Button):
        e = discord.Embed(title="🎡  Wheel Paytable", description=_paytable_text(),
                          color=0x9b59b6)
        e.set_footer(text="Return to player ≈ 95.6%")
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


def _spinning_embed(player: discord.Member, bet: int, frame: int = 0) -> discord.Embed:
    ring = _RING[frame % len(_RING):] + _RING[:frame % len(_RING)]
    e = discord.Embed(
        title="🎡  Wheel of Fortune",
        description=f"**Spinning…**\n\n> {ring}\n>  ⬆",
        color=0x9b59b6,
    )
    e.add_field(name="Bet", value=f"🪙 {bet:,}", inline=True)
    e.set_footer(text=player.display_name)
    return e


class WheelCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active: set[int] = set()

    async def _run_spin(self, message: discord.Message,
                        player: discord.Member, bet: int):
        """Animate then resolve. Bet must already be deducted."""
        try:
            for frame in range(1, 5):
                await asyncio.sleep(0.55)
                try:
                    await message.edit(embed=_spinning_embed(player, bet, frame * 3))
                except discord.DiscordException:
                    break

            mult, emoji, colour = _spin()
            payout = int(bet * mult)
            profit = payout - bet

            if payout > 0:
                await casino_wallet.credit(player.id, payout)

            bal = await casino_wallet.get_balance(player.id)

            if mult == 0:
                title, desc = "💀  Bust!", f"The wheel landed on **LOSE**."
            elif mult >= 10:
                title, desc = "💎  JACKPOT!", f"The wheel landed on **{mult:g}x**!"
            elif profit >= 0:
                title, desc = "🎉  Winner!", f"The wheel landed on **{mult:g}x**!"
            else:
                title, desc = "😬  Partial Refund", f"The wheel landed on **{mult:g}x**."

            e = discord.Embed(
                title=title,
                description=f"{desc}\n\n> {emoji} **{('LOSE' if mult == 0 else f'{mult:g}x')}** {emoji}",
                color=colour,
            )
            e.add_field(name="Bet",     value=f"🪙 {bet:,}",    inline=True)
            e.add_field(name="Payout",  value=f"🪙 {payout:,}", inline=True)
            e.add_field(name="Profit",
                        value=f"{'+' if profit >= 0 else ''}🪙 {profit:,}", inline=True)
            e.add_field(name="Balance", value=f"🪙 {bal:,}",    inline=False)
            e.set_footer(text=f"{player.display_name}  •  Wheel of Fortune")

            view = WheelView(self, player, bet)
            try:
                await message.edit(embed=e, view=view)
                view.message = message
            except discord.DiscordException:
                pass
        finally:
            self._active.discard(player.id)

    @commands.command(name="wheel", aliases=["wof", "spinwheel"])
    async def wheel(self, ctx: commands.Context, bet: int = 0):
        """🎡 Spin the wheel of fortune. Usage: ;wheel <bet>"""
        pid = ctx.author.id

        if pid in self._active:
            return await ctx.send("❌ You already have a wheel spinning!")

        max_bet = await casino_premium.get_max_bet(pid)
        if not (MIN_BET <= bet <= max_bet):
            return await ctx.send(f"❌ Bet must be 🪙 {MIN_BET:,}–{max_bet:,}.")

        if not await casino_wallet.deduct(pid, bet):
            return await ctx.send("❌ Not enough casino coins.")

        self._active.add(pid)
        try:
            msg = await ctx.send(embed=_spinning_embed(ctx.author, bet))
        except discord.DiscordException:
            await casino_wallet.credit(pid, bet)
            self._active.discard(pid)
            return

        await self._run_spin(msg, ctx.author, bet)

    @commands.command(name="wheelpaytable", aliases=["wheelinfo"])
    async def wheel_paytable(self, ctx: commands.Context):
        """Show the wheel paytable."""
        e = discord.Embed(title="🎡  Wheel Paytable", description=_paytable_text(),
                          color=0x9b59b6)
        e.set_footer(text="Return to player ≈ 95.6%  •  ;wheel <bet>")
        await ctx.send(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(WheelCog(bot))
