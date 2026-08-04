"""
roulette.py  —  Solo & Multiplayer Roulette
Players place bets, bot spins. Multiplayer: shared wheel, everyone sees result.
"""
import discord
from discord.ext import commands
from discord import app_commands
import random
import asyncio
from typing import Optional
from . import casino_wallet

MIN_BET      = 10
MAX_BET      = 25_000
JOIN_WINDOW  = 30    # seconds to join multiplayer round
SESSION_TTL  = 120

NUMBERS = list(range(0, 37))   # 0-36 European roulette
REDS    = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
BLACKS  = set(range(1, 37)) - REDS

BET_TYPES = {
    "red":    ("🔴 Red",       lambda n: n in REDS,              1),
    "black":  ("⚫ Black",     lambda n: n in BLACKS,            1),
    "odd":    ("🔢 Odd",       lambda n: n != 0 and n % 2 == 1,  1),
    "even":   ("🔢 Even",      lambda n: n != 0 and n % 2 == 0,  1),
    "low":    ("📉 1–18",      lambda n: 1 <= n <= 18,           1),
    "high":   ("📈 19–36",     lambda n: 19 <= n <= 36,          1),
    "dozen1": ("1st 12",       lambda n: 1 <= n <= 12,           2),
    "dozen2": ("2nd 12",       lambda n: 13 <= n <= 24,          2),
    "dozen3": ("3rd 12",       lambda n: 25 <= n <= 36,          2),
}

def num_color(n: int) -> str:
    if n == 0:  return "🟢"
    if n in REDS: return "🔴"
    return "⚫"


class BetModal(discord.ui.Modal, title="Place Your Bet"):
    amount = discord.ui.TextInput(label="Amount", placeholder="e.g. 500")
    number = discord.ui.TextInput(
        label="Straight number (0–36) or leave blank",
        required=False, placeholder="Optional"
    )

    def __init__(self, bet_type: str, view):
        super().__init__()
        self.bet_type = bet_type
        self._view    = view

    async def on_submit(self, interaction: discord.Interaction):
        # In solo mode only the host can bet
        if not self._view.multiplayer and interaction.user.id != self._view.host.id:
            return await interaction.response.send_message(
                "This is a solo game — only the host can place bets.", ephemeral=True)

        try:
            amt = int(self.amount.value.replace(",", "").strip())
        except ValueError:
            return await interaction.response.send_message("Invalid amount.", ephemeral=True)

        if not (MIN_BET <= amt <= MAX_BET):
            return await interaction.response.send_message(
                f"Bet must be 🪙 {MIN_BET:,}–{MAX_BET:,}.", ephemeral=True)

        if not await casino_wallet.deduct(interaction.user.id, amt):
            return await interaction.response.send_message(
                "Not enough casino coins.", ephemeral=True)

        bet_type = self.bet_type
        straight_num = None

        if self.number.value.strip():
            try:
                n = int(self.number.value.strip())
                if 0 <= n <= 36:
                    straight_num = n
                    bet_type = f"num_{n}"
                else:
                    await casino_wallet.credit(interaction.user.id, amt)
                    return await interaction.response.send_message(
                        "Number must be 0–36.", ephemeral=True)
            except ValueError:
                await casino_wallet.credit(interaction.user.id, amt)
                return await interaction.response.send_message(
                    "Invalid number.", ephemeral=True)
        elif self.bet_type == "num":
            # Clicked 🎯 Number but left the number field blank — nothing to resolve
            await casino_wallet.credit(interaction.user.id, amt)
            return await interaction.response.send_message(
                "Please enter a number between 0 and 36.", ephemeral=True)

        uid = interaction.user.id
        # Refund any previous bet from this player before overwriting it
        if uid in self._view.bets:
            await casino_wallet.credit(uid, self._view.bets[uid]["amount"])

        self._view.bets[uid] = {
            "amount": amt, "type": bet_type,
            "straight": straight_num, "user": interaction.user
        }
        label = f"#{straight_num}" if straight_num is not None else BET_TYPES.get(bet_type, ("?",))[0]
        await interaction.response.send_message(
            f"✅ Bet placed: 🪙 {amt:,} on **{label}**", ephemeral=True)
        await self._view.refresh_embed(interaction)


class RouletteView(discord.ui.View):
    def __init__(self, host: discord.Member, multiplayer=False):
        super().__init__(timeout=SESSION_TTL)
        self.host        = host
        self.multiplayer = multiplayer
        self.bets: dict[int, dict] = {}
        self.spun        = False
        self.message: Optional[discord.Message] = None
        self.result: Optional[int] = None

    def build_embed(self, final=False) -> discord.Embed:
        mode  = "🎲 Roulette  (Multiplayer)" if self.multiplayer else "🎲 Roulette"
        color = 0x27ae60 if not final else (0x2ecc71 if any(
            self._check_win(b, self.result) for b in self.bets.values()) else 0xe74c3c)
        e = discord.Embed(title=mode, color=color)

        if final and self.result is not None:
            nc = num_color(self.result)
            e.add_field(
                name="Result",
                value=f"{nc} **{self.result}**  {'(even)' if self.result % 2 == 0 and self.result != 0 else '(odd)' if self.result != 0 else ''}",
                inline=False
            )
            if self.bets:
                lines = []
                for uid, b in self.bets.items():
                    won   = self._check_win(b, self.result)
                    mult  = self._get_mult(b)
                    net   = b["amount"] * mult if won else -b["amount"]
                    sign  = "+" if net >= 0 else ""
                    label = f"#{b['straight']}" if b["straight"] is not None else BET_TYPES.get(b["type"], ("?",))[0]
                    lines.append(
                        f"{'✅' if won else '❌'} {b['user'].display_name}  —  "
                        f"🪙 {b['amount']:,} on **{label}**  {sign}🪙 {net:,}"
                    )
                e.add_field(name="Bets", value="\n".join(lines), inline=False)
        else:
            if self.bets:
                lines = []
                for b in self.bets.values():
                    label = f"#{b['straight']}" if b["straight"] is not None else BET_TYPES.get(b["type"],("?",))[0]
                    lines.append(f"• {b['user'].display_name}  —  🪙 {b['amount']:,} on **{label}**")
                e.add_field(name="Bets Placed", value="\n".join(lines), inline=False)
            else:
                e.add_field(name="Bets", value="No bets yet. Use the buttons below!", inline=False)
            if self.multiplayer:
                e.set_footer(text=f"Spin closes in {JOIN_WINDOW}s  •  Anyone can bet")
            else:
                e.set_footer(text="Place a bet then spin")
        return e

    def _check_win(self, bet: dict, result: int) -> bool:
        t = bet["type"]
        if t.startswith("num_"):
            return result == bet["straight"]
        if t in BET_TYPES:
            return BET_TYPES[t][1](result)
        return False

    def _get_mult(self, bet: dict) -> int:
        t = bet["type"]
        if t.startswith("num_"):
            return 35
        if t in BET_TYPES:
            return BET_TYPES[t][2]
        return 1

    async def refresh_embed(self, interaction: discord.Interaction):
        try:
            if self.message:
                await self.message.edit(embed=self.build_embed())
        except Exception:
            pass

    async def _do_spin(self, interaction: discord.Interaction):
        if self.spun:
            return await interaction.response.send_message("Already spun!", ephemeral=True)
        if not self.bets:
            return await interaction.response.send_message(
                "Place at least one bet first!", ephemeral=True)
        self.spun = True
        self.spin_btn.disabled = True
        for child in self.children:
            child.disabled = True

        # Spin animation
        await interaction.response.edit_message(
            embed=discord.Embed(title="🎲 Roulette", description="🌀 Spinning…", color=0xf1c40f),
            view=self
        )
        await asyncio.sleep(2)

        self.result = random.choice(NUMBERS)

        # Resolve payouts
        for uid, b in self.bets.items():
            if self._check_win(b, self.result):
                mult = self._get_mult(b)
                await casino_wallet.credit(uid, b["amount"] * (mult + 1))
            # Losers already had bet deducted at placement

        await interaction.edit_original_response(
            embed=self.build_embed(final=True), view=self)
        self.stop()

    @discord.ui.button(label="🔴 Red", style=discord.ButtonStyle.danger, row=0)
    async def bet_red(self, i, b):
        await i.response.send_modal(BetModal("red", self))

    @discord.ui.button(label="⚫ Black", style=discord.ButtonStyle.secondary, row=0)
    async def bet_black(self, i, b):
        await i.response.send_modal(BetModal("black", self))

    @discord.ui.button(label="🔢 Odd", style=discord.ButtonStyle.primary, row=0)
    async def bet_odd(self, i, b):
        await i.response.send_modal(BetModal("odd", self))

    @discord.ui.button(label="🔢 Even", style=discord.ButtonStyle.primary, row=0)
    async def bet_even(self, i, b):
        await i.response.send_modal(BetModal("even", self))

    @discord.ui.button(label="🎯 Number", style=discord.ButtonStyle.success, row=0)
    async def bet_num(self, i, b):
        await i.response.send_modal(BetModal("num", self))

    @discord.ui.button(label="📉 Low 1–18", style=discord.ButtonStyle.secondary, row=1)
    async def bet_low(self, i, b):
        await i.response.send_modal(BetModal("low", self))

    @discord.ui.button(label="📈 High 19–36", style=discord.ButtonStyle.secondary, row=1)
    async def bet_high(self, i, b):
        await i.response.send_modal(BetModal("high", self))

    @discord.ui.button(label="1st 12", style=discord.ButtonStyle.secondary, row=1)
    async def bet_d1(self, i, b):
        await i.response.send_modal(BetModal("dozen1", self))

    @discord.ui.button(label="2nd 12", style=discord.ButtonStyle.secondary, row=1)
    async def bet_d2(self, i, b):
        await i.response.send_modal(BetModal("dozen2", self))

    @discord.ui.button(label="3rd 12", style=discord.ButtonStyle.secondary, row=1)
    async def bet_d3(self, i, b):
        await i.response.send_modal(BetModal("dozen3", self))

    @discord.ui.button(label="🌀 Spin!", style=discord.ButtonStyle.success, row=2)
    async def spin_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.multiplayer and interaction.user.id != self.host.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        if self.multiplayer and interaction.user.id != self.host.id:
            return await interaction.response.send_message(
                "Only the host can spin.", ephemeral=True)
        await self._do_spin(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        # Refund all bets on timeout
        for uid, b in self.bets.items():
            await casino_wallet.credit(uid, b["amount"])
        if self.message:
            try:
                e = self.build_embed()
                e.set_footer(text="Timed out — bets refunded")
                await self.message.edit(embed=e, view=self)
            except Exception:
                pass


class RouletteCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="roulette")
    async def roulette(self, ctx: commands.Context):
        """Solo roulette — place bets and spin. Usage: ;roulette"""
        view = RouletteView(ctx.author, multiplayer=False)
        view.message = await ctx.send(embed=view.build_embed(), view=view)
        await view.wait()

    @commands.command(name="roulette_multi", aliases=["roulette_mp", "rmulti"])
    async def roulette_multi(self, ctx: commands.Context):
        """Multiplayer roulette — everyone bets, host spins. Usage: ;roulette_multi"""
        view = RouletteView(ctx.author, multiplayer=True)
        view.message = await ctx.send(embed=view.build_embed(), view=view)
        await view.wait()


async def setup(bot):
    await bot.add_cog(RouletteCog(bot))
