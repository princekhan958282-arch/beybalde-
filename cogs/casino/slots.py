"""
slots.py  —  5-reel 3-row slot machine
Symbols by rarity. Jackpot on BEYBLADE line.
"""
import discord
from discord.ext import commands
from discord import app_commands
import random
import asyncio
from typing import Optional
from . import casino_wallet
from . import casino_premium

MIN_BET = 10
MAX_BET = 10_000

# symbol: (emoji, weight, payout_multiplier for 5-of-a-kind)
SYMBOLS = {
    "cherry":   ("🍒", 30, 2),
    "lemon":    ("🍋", 25, 3),
    "bar":      ("🎰", 18, 5),
    "seven":    ("7️⃣",  12, 10),
    "diamond":  ("💎",  8,  20),
    "crown":    ("👑",  5,  50),
    "beyblade": ("🌀",  2,  200),   # jackpot symbol
}

SYMBOL_KEYS = list(SYMBOLS.keys())
WEIGHTS     = [SYMBOLS[s][1] for s in SYMBOL_KEYS]   # index [1] = spawn weight

ROWS = 3
COLS = 5

SPIN_FRAMES = 4   # number of "spinning" animation frames


def spin_reels() -> list[list[str]]:
    """Returns ROWS x COLS grid of symbol keys."""
    grid = []
    for _ in range(ROWS):
        row = random.choices(SYMBOL_KEYS, weights=WEIGHTS, k=COLS)
        grid.append(row)
    return grid


def evaluate(grid: list[list[str]], bet: int) -> tuple[int, list[str]]:
    """
    Check middle row (row index 1) for wins.
    Returns (total_payout, list_of_win_descriptions).
    """
    middle = grid[1]
    wins   = []
    payout = 0

    # Count consecutive from left
    first   = middle[0]
    streak  = 1
    for i in range(1, COLS):
        if middle[i] == first:
            streak += 1
        else:
            break

    if streak >= 3:
        emoji, _, mult = SYMBOLS[first]
        scale = {3: 0.5, 4: 1.0, 5: 2.0}[streak]
        # 5x beyblade triggers jackpot below — skip the streak payout to avoid stacking
        if not (streak == 5 and first == "beyblade"):
            line_pay = int(bet * mult * scale)
            payout  += line_pay
            wins.append(f"{emoji}×{streak}  →  🪙 {line_pay:,}")

    # Bonus: any BEYBLADE on middle row (not part of streak)
    bey_count = middle.count("beyblade")
    if bey_count >= 1 and not (first == "beyblade" and streak >= 3):
        bonus = int(bet * bey_count * 5)
        payout += bonus
        wins.append(f"🌀 scatter ×{bey_count}  →  🪙 {bonus:,}")

    # Jackpot: all 5 BEYBLADEs on middle row
    if streak == 5 and first == "beyblade":
        jackpot = int(bet * 500)
        payout  += jackpot
        wins.append(f"🎊 **JACKPOT!**  →  🪙 {jackpot:,}")

    return payout, wins


def grid_display(grid: list[list[str]], spinning=False) -> str:
    spin_emoji = "🎲"
    lines = []
    for row in grid:
        if spinning:
            cells = [spin_emoji] * COLS
        else:
            cells = [SYMBOLS[s][0] for s in row]
        lines.append("  ".join(cells))
    return "\n".join(lines)


class SlotsView(discord.ui.View):
    def __init__(self, player: discord.Member, bet: int):
        super().__init__(timeout=120)
        self.player  = player
        self.bet     = bet
        self.message: Optional[discord.Message] = None
        self.spinning = False
        self._first_spin = True   # first spin bet already deducted by command
        self.total_won   = 0      # cumulative payout received
        self.spins_done  = 0      # number of spins completed

    def build_embed(self, grid=None, wins=None, payout=None, spinning=False) -> discord.Embed:
        e = discord.Embed(title="🎰  Slots", color=0x9b59b6)
        e.add_field(name="Bet", value=f"🪙 {self.bet:,}", inline=True)

        if spinning or grid is None:
            display = grid_display([[""]*COLS]*ROWS, spinning=True)
            e.add_field(name="\u200b", value=f"```\n{display}\n```", inline=False)
            e.set_footer(text="Spinning…")
        else:
            display = grid_display(grid)
            e.add_field(name="\u200b", value=f"```\n{display}\n```", inline=False)
            if wins:
                e.add_field(name="Wins", value="\n".join(wins), inline=False)
                e.add_field(name="Total Payout", value=f"🪙 {payout:,} (+{payout-self.bet:,})", inline=True)
                e.color = 0x2ecc71
            else:
                e.add_field(name="Result", value="No win. Try again!", inline=False)
                e.color = 0xe74c3c
            e.set_footer(text=f"▲ Middle row is the win line  •  {self.player.display_name}")
        return e

    @discord.ui.button(label="🎰  Spin!", style=discord.ButtonStyle.primary, row=0)
    async def spin_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        if self.spinning:
            return await interaction.response.defer()

        # First spin: bet already deducted at command level.
        # Re-spins: deduct now.
        if self._first_spin:
            self._first_spin = False
        else:
            if not await casino_wallet.deduct(self.player.id, self.bet):
                return await interaction.response.send_message(
                    "Not enough casino coins.", ephemeral=True)

        self.spinning    = True
        button.disabled  = True
        self.quit_btn.disabled = True

        await interaction.response.edit_message(
            embed=self.build_embed(spinning=True), view=self)

        # Animate spinning frames
        for _ in range(SPIN_FRAMES):
            await asyncio.sleep(0.6)
            try:
                await interaction.edit_original_response(
                    embed=self.build_embed(spinning=True))
            except Exception:
                pass

        grid    = spin_reels()
        payout, wins = evaluate(grid, self.bet)

        self.spins_done += 1
        if payout > 0:
            await casino_wallet.credit(self.player.id, payout)
            self.total_won += payout

        self.spinning       = False
        button.disabled     = False
        self.quit_btn.disabled = False

        await interaction.edit_original_response(
            embed=self.build_embed(grid=grid, wins=wins, payout=payout),
            view=self)

    @discord.ui.button(label="Quit", style=discord.ButtonStyle.secondary, row=0)
    async def quit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        for child in self.children:
            child.disabled = True
        net  = self.total_won - (self.bet * self.spins_done) if self.spins_done > 0 else 0
        sign = "+" if net >= 0 else ""
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        await interaction.followup.send(
            f"🎰 **{self.player.display_name}** cashed out after **{self.spins_done}** spin(s)!\n"
            f"Total won: 🪙 {self.total_won:,}  |  Net: {sign}🪙 {net:,}"
        )
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        # Refund the pre-deducted bet if the player never spun
        if self._first_spin:
            await casino_wallet.credit(self.player.id, self.bet)
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class SlotsCog(commands.Cog):
    def __init__(self, bot):
        self.bot    = bot
        self._active: set[int] = set()

    @commands.command(name="slots")
    async def slots(self, ctx: commands.Context, opponent: Optional[discord.Member] = None, bet: int = 0):
        """Spin the slot machine! Usage: ;slots <bet>"""
        pid = ctx.author.id
        if pid in self._active:
            return await ctx.send("❌ You already have an active Slots session!")
        _max_bet = await casino_premium.get_max_bet(ctx.author.id)
        if not (MIN_BET <= bet <= _max_bet):
            return await ctx.send(f"❌ Bet must be 🪙 {MIN_BET:,}–{_max_bet:,}.")
        if not await casino_wallet.deduct(pid, bet):
            return await ctx.send("❌ Not enough casino coins.")

        self._active.add(pid)
        view = SlotsView(ctx.author, bet)
        view.message = await ctx.send(embed=view.build_embed(), view=view)
        await view.wait()
        self._active.discard(pid)


async def setup(bot):
    await bot.add_cog(SlotsCog(bot))
