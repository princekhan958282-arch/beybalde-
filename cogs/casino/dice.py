"""
dice.py  —  Dice Roll (PvE) + Dice Duel (PvP)
PvE: choose risk multiplier, bot rolls against you.
PvP: two players bet, both roll, highest wins the pot.
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
MAX_BET = 50_000
DUEL_TIMEOUT = 60   # seconds for opponent to accept

# PvE risk tiers: (label, win_chance, payout_mult)
RISK_TIERS = [
    ("🟢 Easy    — 70%",  0.70,  1.4),
    ("🟡 Medium  — 50%",  0.50,  2.0),
    ("🟠 Hard    — 30%",  0.30,  3.0),
    ("🔴 Insane  — 10%",  0.10,  9.0),
    ("💀 Yolo    — 5%",   0.05, 18.0),
]


# ── PvE Dice Roll ─────────────────────────────────────────────────────────────
class RiskSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=label,
                value=str(i),
                description=f"Win {int(chance*100)}%  →  {mult}x payout"
            )
            for i, (label, chance, mult) in enumerate(RISK_TIERS)
        ]
        super().__init__(placeholder="Choose your risk level…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: DiceView = self.view
        if interaction.user.id != view.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        view.tier_idx = int(self.values[0])
        view.roll_btn.disabled = False
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class DiceView(discord.ui.View):
    def __init__(self, player: discord.Member, bet: int):
        super().__init__(timeout=120)
        self.player   = player
        self.bet      = bet
        self.tier_idx: Optional[int] = None
        self.message: Optional[discord.Message] = None
        self.add_item(RiskSelect())

    def build_embed(self, result=None) -> discord.Embed:
        e = discord.Embed(title="🎲  Dice Roll", color=0x3498db)
        e.add_field(name="Bet", value=f"🪙 {self.bet:,}", inline=True)
        if self.tier_idx is not None:
            label, chance, mult = RISK_TIERS[self.tier_idx]
            e.add_field(name="Risk", value=label, inline=True)
            e.add_field(name="Payout", value=f"{mult}x  →  🪙 {int(self.bet*mult):,}", inline=True)
        if result:
            e.add_field(name="Your Roll",    value=f"🎲 {result['player']}", inline=True)
            e.add_field(name="Dealer Roll",  value=f"🎲 {result['dealer']}", inline=True)
            if result["won"]:
                e.add_field(name="Result", value=f"✅ Win! +🪙 {result['profit']:,}", inline=False)
                e.color = 0x2ecc71
            else:
                e.add_field(name="Result", value=f"❌ Loss  -🪙 {self.bet:,}", inline=False)
                e.color = 0xe74c3c
        else:
            e.set_footer(text="Pick risk → Roll")
        return e

    @discord.ui.button(label="🎲 Roll!", style=discord.ButtonStyle.success,
                       disabled=True, row=1)
    async def roll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        if self.tier_idx is None:
            return await interaction.response.send_message(
                "Please select a risk level first.", ephemeral=True)
        label, chance, mult = RISK_TIERS[self.tier_idx]

        # Disable buttons
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="🎲 Dice Roll", description="Rolling…", color=0xf1c40f),
            view=self
        )
        await asyncio.sleep(1.2)

        # Determine win by chance
        player_roll = random.randint(1, 100)
        win_threshold = int(chance * 100)
        won = player_roll <= win_threshold
        dealer_roll = random.randint(1, win_threshold) if won else random.randint(win_threshold+1, 100)

        if won:
            payout = int(self.bet * mult)
            profit = payout - self.bet
            await casino_wallet.credit(self.player.id, payout)
        else:
            profit = -self.bet

        result = {"player": player_roll, "dealer": dealer_roll, "won": won, "profit": profit}
        await interaction.edit_original_response(
            embed=self.build_embed(result=result), view=self)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        # Refund — player paid but timed out before picking a tier and rolling
        await casino_wallet.credit(self.player.id, self.bet)
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


# ── PvP Dice Duel ─────────────────────────────────────────────────────────────
class DiceDuelView(discord.ui.View):
    def __init__(self, challenger: discord.Member, bet: int):
        super().__init__(timeout=DUEL_TIMEOUT)
        self.challenger   = challenger
        self.opponent: Optional[discord.Member] = None
        self.bet          = bet
        self.message: Optional[discord.Message] = None
        self.accepted     = False

    def build_embed(self, rolls=None, winner=None) -> discord.Embed:
        e = discord.Embed(title="🎲  Dice Duel", color=0xe67e22)
        e.add_field(name="Challenger", value=self.challenger.mention, inline=True)
        e.add_field(name="Pot",        value=f"🪙 {self.bet*2:,}", inline=True)
        e.add_field(name="Opponent",
                    value=self.opponent.mention if self.opponent else "Waiting…",
                    inline=True)
        if rolls:
            e.add_field(name=f"{self.challenger.display_name} rolled",
                        value=f"🎲 **{rolls[0]}**", inline=True)
            e.add_field(name=f"{self.opponent.display_name} rolled",
                        value=f"🎲 **{rolls[1]}**", inline=True)
            if winner:
                e.add_field(name="Winner", value=f"🏆 {winner.mention} wins 🪙 {self.bet*2:,}!",
                            inline=False)
                e.color = 0x2ecc71
            else:
                e.add_field(name="Result", value="🤝 Tie! Bets refunded.", inline=False)
                e.color = 0x95a5a6
        else:
            e.set_footer(text=f"Click Accept to join!  •  Bet: 🪙 {self.bet:,} each")
        return e

    @discord.ui.button(label="⚔️ Accept Duel", style=discord.ButtonStyle.success, row=0)
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == self.challenger.id:
            return await interaction.response.send_message(
                "You can't duel yourself.", ephemeral=True)
        if self.accepted:
            return await interaction.response.send_message(
                "Duel already started.", ephemeral=True)
        self.accepted = True   # claim before any await so accepts can't race
        if not await casino_wallet.deduct(interaction.user.id, self.bet):
            self.accepted = False
            return await interaction.response.send_message(
                "Not enough casino coins.", ephemeral=True)

        self.opponent = interaction.user
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            embed=discord.Embed(title="🎲 Dice Duel", description="Rolling dice…", color=0xf1c40f),
            view=self
        )
        await asyncio.sleep(1.5)

        r1 = random.randint(1, 6)
        r2 = random.randint(1, 6)
        rolls = (r1, r2)

        if r1 > r2:
            winner = self.challenger
            await casino_wallet.credit(self.challenger.id, self.bet * 2)
        elif r2 > r1:
            winner = self.opponent
            await casino_wallet.credit(self.opponent.id, self.bet * 2)
        else:
            winner = None
            # Refund both on tie
            await casino_wallet.credit(self.challenger.id, self.bet)
            await casino_wallet.credit(self.opponent.id, self.bet)

        await interaction.edit_original_response(
            embed=self.build_embed(rolls=rolls, winner=winner), view=self)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        # Refund challenger — opponent never paid
        await casino_wallet.credit(self.challenger.id, self.bet)
        if self.message:
            try:
                e = self.build_embed()
                e.set_footer(text="No one accepted — bet refunded")
                await self.message.edit(embed=e, view=self)
            except Exception:
                pass


class DiceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="dice")
    async def dice(self, ctx: commands.Context, opponent: Optional[discord.Member] = None, bet: int = 0):
        """Roll against the house — pick your risk. Usage: ;dice <bet>"""
        _max_bet = await casino_premium.get_max_bet(ctx.author.id)
        if not (MIN_BET <= bet <= _max_bet):
            return await ctx.send(f"❌ Bet must be 🪙 {MIN_BET:,}–{_max_bet:,}.")
        if not await casino_wallet.deduct(ctx.author.id, bet):
            return await ctx.send("❌ Not enough casino coins.")

        view = DiceView(ctx.author, bet)
        view.message = await ctx.send(embed=view.build_embed(), view=view)
        await view.wait()

    @commands.command(name="diceduel")
    async def diceduel(self, ctx: commands.Context, opponent: Optional[discord.Member] = None, bet: int = 0):
        """Challenge someone to a dice duel! Usage: ;diceduel <bet>"""
        _max_bet = await casino_premium.get_max_bet(ctx.author.id)
        if not (MIN_BET <= bet <= _max_bet):
            return await ctx.send(f"❌ Bet must be 🪙 {MIN_BET:,}–{_max_bet:,}.")
        if not await casino_wallet.deduct(ctx.author.id, bet):
            return await ctx.send("❌ Not enough casino coins.")

        view = DiceDuelView(ctx.author, bet)
        view.message = await ctx.send(embed=view.build_embed(), view=view)
        await view.wait()


async def setup(bot):
    await bot.add_cog(DiceCog(bot))
