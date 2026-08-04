"""
higher_lower.py  —  PvP Higher or Lower
Shared deck. Each round both players flip a card.
Higher card wins the round. First to 3 round wins takes the pot.
Ties replay that round.
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
JOIN_TIMEOUT = 60
ROUND_PAUSE  = 2.0

SUITS  = ["♠", "♥", "♦", "♣"]
RANKS  = ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]
VALUES = {r: i+2 for i, r in enumerate(RANKS)}   # 2=2 … A=14

WINS_NEEDED = 3


def new_deck():
    d = [{"rank": r, "suit": s} for r in RANKS for s in SUITS]
    random.shuffle(d)
    return d


def card_str(c):
    return f"{c['rank']}{c['suit']}"


class HLGame:
    def __init__(self, p1: discord.Member, p2: discord.Member, bet: int):
        self.p1      = p1
        self.p2      = p2
        self.bet     = bet
        self.deck    = new_deck()
        self.wins    = {p1.id: 0, p2.id: 0}
        self.rounds: list[dict] = []
        self.winner: Optional[discord.Member] = None

    def play_round(self) -> dict:
        if len(self.deck) < 2:
            # Reshuffle on extreme tie streaks (astronomically rare)
            self.deck = new_deck()
        c1 = self.deck.pop()
        c2 = self.deck.pop()
        v1 = VALUES[c1["rank"]]
        v2 = VALUES[c2["rank"]]
        if v1 > v2:
            rw = self.p1
            self.wins[self.p1.id] += 1
        elif v2 > v1:
            rw = self.p2
            self.wins[self.p2.id] += 1
        else:
            rw = None   # tie

        r = {"c1": c1, "c2": c2, "winner": rw}
        self.rounds.append(r)

        if self.wins[self.p1.id] >= WINS_NEEDED:
            self.winner = self.p1
        elif self.wins[self.p2.id] >= WINS_NEEDED:
            self.winner = self.p2
        return r


class HLView(discord.ui.View):
    def __init__(self, game: HLGame):
        super().__init__(timeout=300)
        self.game    = game
        self.message: Optional[discord.Message] = None

    def build_embed(self, latest_round=None) -> discord.Embed:
        g   = self.game
        w1  = g.wins[g.p1.id]
        w2  = g.wins[g.p2.id]

        if g.winner:
            color = 0x2ecc71
            title = f"🏆 {g.winner.display_name} wins the duel!"
        else:
            color = 0x3498db
            title = "🃏  Higher or Lower"

        e = discord.Embed(title=title, color=color)
        e.add_field(name=g.p1.display_name, value=f"Wins: **{w1}**", inline=True)
        e.add_field(name="First to 3", value=f"🪙 {g.bet*2:,}", inline=True)
        e.add_field(name=g.p2.display_name, value=f"Wins: **{w2}**", inline=True)

        if latest_round:
            c1, c2 = latest_round["c1"], latest_round["c2"]
            rw     = latest_round["winner"]
            tie    = rw is None
            e.add_field(
                name="Last Round",
                value=(f"{g.p1.display_name}: **{card_str(c1)}**  vs  "
                       f"{g.p2.display_name}: **{card_str(c2)}**\n"
                       f"{'🤝 Tie — replay' if tie else f'✅ {rw.display_name} wins the round'}"),
                inline=False
            )

        if g.winner:
            pot    = g.bet * 2
            e.add_field(
                name="Payout",
                value=f"🏆 {g.winner.mention} receives 🪙 {pot:,}",
                inline=False
            )
        elif not latest_round:
            e.set_footer(text=f"First to {WINS_NEEDED} round wins takes 🪙 {g.bet*2:,}!")

        return e

    @discord.ui.button(label="🃏 Flip Cards", style=discord.ButtonStyle.primary, row=0)
    async def flip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        if interaction.user.id not in (g.p1.id, g.p2.id):
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        if g.winner:
            return await interaction.response.defer()

        button.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="🃏 Flipping…", color=0xf1c40f), view=self)
        await asyncio.sleep(ROUND_PAUSE)

        r = g.play_round()

        if g.winner:
            button.disabled = True
            await casino_wallet.credit(g.winner.id, g.bet * 2)
            await interaction.edit_original_response(
                embed=self.build_embed(latest_round=r), view=self)
            self.stop()
        else:
            button.disabled = False
            await interaction.edit_original_response(
                embed=self.build_embed(latest_round=r), view=self)

    async def on_timeout(self):
        # Refund both if game didn't finish
        if not self.game.winner:
            await casino_wallet.credit(self.game.p1.id, self.game.bet)
            await casino_wallet.credit(self.game.p2.id, self.game.bet)
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class HLJoinView(discord.ui.View):
    def __init__(self, challenger: discord.Member, bet: int):
        super().__init__(timeout=JOIN_TIMEOUT)
        self.challenger = challenger
        self.bet        = bet
        self.opponent: Optional[discord.Member] = None
        self.claimed    = False
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="⚔️ Join Game", style=discord.ButtonStyle.success, row=0)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == self.challenger.id:
            return await interaction.response.send_message(
                "You can't play against yourself.", ephemeral=True)
        if self.claimed:
            return await interaction.response.send_message(
                "Someone already joined this game.", ephemeral=True)
        self.claimed = True   # claim before any await so joins can't race
        if not await casino_wallet.deduct(interaction.user.id, self.bet):
            self.claimed = False
            return await interaction.response.send_message(
                "Not enough casino coins.", ephemeral=True)

        self.opponent = interaction.user
        self.stop()
        button.disabled = True

        game = HLGame(self.challenger, self.opponent, self.bet)
        view = HLView(game)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)
        view.message = await interaction.original_response()
        await view.wait()

    async def on_timeout(self):
        await casino_wallet.credit(self.challenger.id, self.bet)
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                e = discord.Embed(
                    title="🃏 Higher or Lower",
                    description="No one joined — bet refunded.",
                    color=0x95a5a6
                )
                await self.message.edit(embed=e, view=self)
            except Exception:
                pass


class HigherLowerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="higherlower", aliases=["hl"])
    async def higherlower(self, ctx: commands.Context, opponent: Optional[discord.Member] = None, bet: int = 0):
        """Card duel — flip cards, highest wins the round. First to 3! Usage: ;higherlower <bet>"""
        _max_bet = await casino_premium.get_max_bet(ctx.author.id)
        if not (MIN_BET <= bet <= _max_bet):
            return await ctx.send(f"❌ Bet must be 🪙 {MIN_BET:,}–{_max_bet:,}.")
        if not await casino_wallet.deduct(ctx.author.id, bet):
            return await ctx.send("❌ Not enough casino coins.")

        view = HLJoinView(ctx.author, bet)
        e    = discord.Embed(
            title="🃏  Higher or Lower",
            description=(f"{ctx.author.mention} wants to duel!\n"
                         f"Bet: 🪙 {bet:,} each  •  First to 3 round wins takes 🪙 {bet*2:,}"),
            color=0x3498db
        )
        view.message = await ctx.send(embed=e, view=view)
        await view.wait()


async def setup(bot):
    await bot.add_cog(HigherLowerCog(bot))
