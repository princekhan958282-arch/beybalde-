"""
coinflip.py  —  Coin Flip Duel (PvP)
Two players choose heads/tails. Winner takes the pot.
Also has a solo mode vs the house.
"""
import discord
from discord.ext import commands
from discord import app_commands
import random
import asyncio
from typing import Optional
from . import casino_wallet
from . import casino_premium

MIN_BET      = 10
MAX_BET      = 50_000
DUEL_TIMEOUT = 60

HEADS = "🪙 Heads"
TAILS = "🌀 Tails"


class CoinFlipSoloView(discord.ui.View):
    def __init__(self, player: discord.Member, bet: int):
        super().__init__(timeout=60)
        self.player  = player
        self.bet     = bet
        self.choice: Optional[str] = None
        self.message: Optional[discord.Message] = None

    def build_embed(self, result=None) -> discord.Embed:
        e = discord.Embed(title="🪙  Coin Flip", color=0xf1c40f)
        e.add_field(name="Bet", value=f"🪙 {self.bet:,}", inline=True)
        if self.choice:
            e.add_field(name="Your Pick", value=self.choice, inline=True)
        if result:
            landed = result["landed"]
            won    = result["won"]
            e.add_field(name="Result", value=landed, inline=True)
            if won:
                e.add_field(name="Outcome", value=f"✅ Win! +🪙 {self.bet:,}", inline=False)
                e.color = 0x2ecc71
            else:
                e.add_field(name="Outcome", value=f"❌ Loss  -🪙 {self.bet:,}", inline=False)
                e.color = 0xe74c3c
        else:
            e.set_footer(text="Pick heads or tails")
        return e

    @discord.ui.button(label="🪙 Heads", style=discord.ButtonStyle.primary, row=0)
    async def heads_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, HEADS)

    @discord.ui.button(label="🌀 Tails", style=discord.ButtonStyle.secondary, row=0)
    async def tails_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, TAILS)

    async def _pick(self, interaction: discord.Interaction, choice: str):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your flip.", ephemeral=True)
        self.choice = choice
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="🪙 Flipping…", color=0xf1c40f), view=self)
        await asyncio.sleep(1.0)

        landed = random.choice([HEADS, TAILS])
        won    = landed == choice
        if won:
            await casino_wallet.credit(self.player.id, self.bet * 2)

        await interaction.edit_original_response(
            embed=self.build_embed(result={"landed": landed, "won": won}),
            view=self
        )
        self.stop()

    async def on_timeout(self):
        await casino_wallet.credit(self.player.id, self.bet)   # refund
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class CoinFlipDuelView(discord.ui.View):
    def __init__(self, challenger: discord.Member, challenger_choice: str, bet: int):
        super().__init__(timeout=DUEL_TIMEOUT)
        self.challenger        = challenger
        self.challenger_choice = challenger_choice
        self.opponent: Optional[discord.Member] = None
        self.bet               = bet
        self.accepted          = False
        self.message: Optional[discord.Message] = None

    def build_embed(self, result=None) -> discord.Embed:
        opp_choice = TAILS if self.challenger_choice == HEADS else HEADS
        e = discord.Embed(title="🪙  Coin Flip Duel", color=0xf1c40f)
        e.add_field(name="Challenger",
                    value=f"{self.challenger.mention}\n{self.challenger_choice}", inline=True)
        e.add_field(name="Pot", value=f"🪙 {self.bet*2:,}", inline=True)
        e.add_field(name="Opponent",
                    value=f"{self.opponent.mention if self.opponent else 'Waiting…'}\n"
                          f"{opp_choice if self.opponent else '?'}", inline=True)
        if result:
            landed = result["landed"]
            winner = result["winner"]
            e.add_field(name="Coin landed on", value=f"**{landed}**", inline=False)
            if winner:
                e.add_field(name="Winner",
                            value=f"🏆 {winner.mention} wins **🪙 {self.bet*2:,}**!", inline=False)
                e.color = 0x2ecc71
            else:
                e.add_field(name="Result", value="🤝 Impossible tie — refunded", inline=False)
                e.color = 0x95a5a6
        else:
            e.set_footer(text=f"Bet: 🪙 {self.bet:,} each  •  Click Accept to take {opp_choice}")
        return e

    @discord.ui.button(label="Accept Duel", style=discord.ButtonStyle.success, row=0)
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == self.challenger.id:
            return await interaction.response.send_message(
                "You can't duel yourself.", ephemeral=True)
        # Claim the seat synchronously (no await before this) so two near-
        # simultaneous accepts can't both charge.
        if self.accepted:
            return await interaction.response.send_message(
                "Someone already accepted this duel.", ephemeral=True)
        self.accepted = True
        if not await casino_wallet.deduct(interaction.user.id, self.bet):
            self.accepted = False
            return await interaction.response.send_message(
                "Not enough casino coins.", ephemeral=True)

        self.opponent = interaction.user
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="🪙 Flipping…", color=0xf1c40f), view=self)
        await asyncio.sleep(1.2)

        landed = random.choice([HEADS, TAILS])
        if landed == self.challenger_choice:
            winner = self.challenger
            await casino_wallet.credit(self.challenger.id, self.bet * 2)
        else:
            winner = self.opponent
            await casino_wallet.credit(self.opponent.id, self.bet * 2)

        await interaction.edit_original_response(
            embed=self.build_embed(result={"landed": landed, "winner": winner}),
            view=self
        )
        self.stop()

    async def on_timeout(self):
        await casino_wallet.credit(self.challenger.id, self.bet)
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                e = self.build_embed()
                e.set_footer(text="No one accepted — bet refunded")
                await self.message.edit(embed=e, view=self)
            except Exception:
                pass


class CoinFlipCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="coinflip", aliases=["cf"])
    async def coinflip(self, ctx: commands.Context, opponent: Optional[discord.Member] = None, bet: int = 0):
        """Flip a coin vs the house or challenge someone. Usage: ;coinflip [@opponent] <bet>"""
        _max_bet = await casino_premium.get_max_bet(ctx.author.id)
        if not (MIN_BET <= bet <= _max_bet):
            return await ctx.send(f"❌ Bet must be 🪙 {MIN_BET:,}–{_max_bet:,}.")

        if opponent:
            # PvP duel
            if opponent.id == ctx.author.id:
                return await ctx.send("❌ Can't duel yourself.")
            if opponent.bot:
                return await ctx.send("❌ Can't duel a bot.")
            if not await casino_wallet.deduct(ctx.author.id, bet):
                return await ctx.send("❌ Not enough casino coins.")

            choice = random.choice([HEADS, TAILS])
            view   = CoinFlipDuelView(ctx.author, choice, bet)
            view.message = await ctx.send(
                content=f"{opponent.mention} you've been challenged!",
                embed=view.build_embed(), view=view)
            await view.wait()
        else:
            # Solo vs house
            if not await casino_wallet.deduct(ctx.author.id, bet):
                return await ctx.send("❌ Not enough casino coins.")

            view = CoinFlipSoloView(ctx.author, bet)
            view.message = await ctx.send(embed=view.build_embed(), view=view)
            await view.wait()


    @coinflip.error
    async def coinflip_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send(f"❌ Usage: `;coinflip <bet>` or `;coinflip @opponent <bet>`\nBet range: 🪙 {MIN_BET:,} – your premium limit.")
        else:
            raise error


async def setup(bot):
    await bot.add_cog(CoinFlipCog(bot))
