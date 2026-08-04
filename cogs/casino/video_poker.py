"""
video_poker.py  —  🃏 Video Poker (Jacks or Better)

Solo 5-card draw against a fixed paytable. Deal, hold the cards you want,
draw replacements once, get paid.

Distinct from ;poker (which is multiplayer Texas Hold'em).

Usage:  ;videopoker <bet>     (aliases: ;vp, ;jacks)
"""

import random
from collections import Counter
from typing import Optional

import discord
from discord.ext import commands

from . import casino_premium, casino_wallet

MIN_BET = 10
HAND_SIZE = 5

SUITS = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
RED_SUITS = {"H", "D"}
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}

# hand key -> (display name, multiplier)  — 8/5 Jacks or Better
PAYTABLE = [
    ("royal_flush",    "Royal Flush",     400),
    ("straight_flush", "Straight Flush",  50),
    ("four_kind",      "Four of a Kind",  25),
    ("full_house",     "Full House",      8),
    ("flush",          "Flush",           5),
    ("straight",       "Straight",        4),
    ("three_kind",     "Three of a Kind", 3),
    ("two_pair",       "Two Pair",        2),
    ("jacks_better",   "Jacks or Better", 1),
]
PAY_LOOKUP = {k: (name, mult) for k, name, mult in PAYTABLE}


class Card:
    __slots__ = ("rank", "suit")

    def __init__(self, rank: str, suit: str):
        self.rank = rank
        self.suit = suit

    @property
    def value(self) -> int:
        return RANK_VALUE[self.rank]

    def __str__(self) -> str:
        return f"{self.rank}{SUITS[self.suit]}"


def _new_deck() -> list[Card]:
    deck = [Card(r, s) for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


def evaluate(hand: list[Card]) -> Optional[str]:
    """Return a paytable key, or None for a losing hand."""
    values = sorted(c.value for c in hand)
    suits  = {c.suit for c in hand}
    counts = Counter(c.value for c in hand)
    shape  = sorted(counts.values(), reverse=True)

    is_flush = len(suits) == 1

    distinct = sorted(set(values))
    is_straight = False
    high = 0
    if len(distinct) == 5:
        if distinct[4] - distinct[0] == 4:
            is_straight, high = True, distinct[4]
        elif distinct == [2, 3, 4, 5, 14]:      # wheel: A-2-3-4-5
            is_straight, high = True, 5

    if is_straight and is_flush:
        return "royal_flush" if high == 14 else "straight_flush"
    if shape[0] == 4:
        return "four_kind"
    if shape == [3, 2]:
        return "full_house"
    if is_flush:
        return "flush"
    if is_straight:
        return "straight"
    if shape[0] == 3:
        return "three_kind"
    if shape == [2, 2, 1]:
        return "two_pair"
    if shape[0] == 2:
        pair_value = next(v for v, n in counts.items() if n == 2)
        if pair_value >= RANK_VALUE["J"]:
            return "jacks_better"
    return None


def _hand_text(hand: list[Card], holds: set[int], revealed: bool = True) -> str:
    cells = []
    for i, c in enumerate(hand):
        face = str(c) if revealed else "🂠"
        mark = "🔒" if i in holds else "　"
        cells.append(f"`{face:>4}`\n{mark}")
    return "   ".join(cells)


def _paytable_embed(bet: int = 0) -> discord.Embed:
    lines = []
    for key, name, mult in PAYTABLE:
        payout = f"  →  🪙 {int(bet * mult):,}" if bet else ""
        lines.append(f"**{name}** — `{mult}x`{payout}")
    e = discord.Embed(title="🃏  Video Poker Paytable",
                      description="\n".join(lines), color=0x9b59b6)
    e.set_footer(text="Jacks or Better  •  8/5 paytable  •  one draw")
    return e


class HoldButton(discord.ui.Button):
    def __init__(self, idx: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="HOLD", row=1)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        view: "VideoPokerView" = self.view
        if interaction.user.id != view.player.id:
            return await interaction.response.send_message("Not your hand.", ephemeral=True)
        if view.finished:
            return await interaction.response.defer()

        if self.idx in view.holds:
            view.holds.discard(self.idx)
            self.style = discord.ButtonStyle.secondary
            self.label = "HOLD"
        else:
            view.holds.add(self.idx)
            self.style = discord.ButtonStyle.success
            self.label = "HELD"
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class VideoPokerView(discord.ui.View):
    def __init__(self, cog, player: discord.Member, bet: int):
        super().__init__(timeout=120)
        self.cog      = cog
        self.player   = player
        self.bet      = bet
        self.deck     = _new_deck()
        self.hand     = [self.deck.pop() for _ in range(HAND_SIZE)]
        self.holds: set[int] = set()
        self.finished = False
        self.message: Optional[discord.Message] = None

        for i in range(HAND_SIZE):
            self.add_item(HoldButton(i))

    # ── UI ────────────────────────────────────────────────────────────────────
    def build_embed(self, result_key: Optional[str] = None,
                    payout: int = 0, balance: int = 0) -> discord.Embed:
        if self.finished:
            if result_key:
                name, mult = PAY_LOOKUP[result_key]
                colour = 0x1abc9c if mult >= 25 else 0x2ecc71
                title  = f"🃏  {name}!"
                desc   = f"**{mult}x** — you won 🪙 **{payout:,}**"
            else:
                colour = 0xe74c3c
                title  = "🃏  No Win"
                desc   = f"Nothing paid. Lost 🪙 **{self.bet:,}**"
        else:
            colour = 0x9b59b6
            title  = "🃏  Video Poker — Jacks or Better"
            desc   = "Tap **HOLD** under the cards you want to keep, then press **Draw**."

        e = discord.Embed(title=title, description=desc, color=colour)
        e.add_field(name="Your hand", value=_hand_text(self.hand, self.holds), inline=False)
        e.add_field(name="Bet", value=f"🪙 {self.bet:,}", inline=True)
        if self.finished:
            profit = payout - self.bet
            e.add_field(name="Profit",
                        value=f"{'+' if profit >= 0 else ''}🪙 {profit:,}", inline=True)
            e.add_field(name="Balance", value=f"🪙 {balance:,}", inline=True)
        else:
            e.add_field(name="Holding", value=str(len(self.holds)), inline=True)
            best = evaluate(self.hand)
            e.add_field(name="Right now",
                        value=(PAY_LOOKUP[best][0] if best else "nothing"), inline=True)
        e.set_footer(text=f"{self.player.display_name}  •  ;vppaytable for full payouts")
        return e

    @discord.ui.button(label="🎴  Draw", style=discord.ButtonStyle.primary, row=0)
    async def draw_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your hand.", ephemeral=True)
        if self.finished:
            return await interaction.response.defer()

        for i in range(HAND_SIZE):
            if i not in self.holds:
                self.hand[i] = self.deck.pop()

        await self._finish(interaction)

    @discord.ui.button(label="🔒  Hold All", style=discord.ButtonStyle.secondary, row=0)
    async def hold_all_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your hand.", ephemeral=True)
        if self.finished:
            return await interaction.response.defer()
        self.holds = set(range(HAND_SIZE))
        for c in self.children:
            if isinstance(c, HoldButton):
                c.style = discord.ButtonStyle.success
                c.label = "HELD"
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="📜  Paytable", style=discord.ButtonStyle.secondary, row=0)
    async def paytable_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=_paytable_embed(self.bet), ephemeral=True)

    async def _finish(self, interaction: discord.Interaction):
        self.finished = True
        key    = evaluate(self.hand)
        mult   = PAY_LOOKUP[key][1] if key else 0
        payout = int(self.bet * mult)
        if payout > 0:
            await casino_wallet.credit(self.player.id, payout)
        bal = await casino_wallet.get_balance(self.player.id)

        for c in self.children:
            c.disabled = True

        replay = ReplayView(self.cog, self.player, self.bet)
        await interaction.response.edit_message(
            embed=self.build_embed(key, payout, bal), view=replay)
        try:
            replay.message = await interaction.original_response()
        except Exception:
            pass
        self.cog._active.discard(self.player.id)
        self.stop()

    async def on_timeout(self):
        if self.finished:
            return
        # Auto-draw so the player isn't punished for going idle
        for i in range(HAND_SIZE):
            if i not in self.holds:
                self.hand[i] = self.deck.pop()
        self.finished = True
        key    = evaluate(self.hand)
        mult   = PAY_LOOKUP[key][1] if key else 0
        payout = int(self.bet * mult)
        if payout > 0:
            await casino_wallet.credit(self.player.id, payout)
        bal = await casino_wallet.get_balance(self.player.id)
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                e = self.build_embed(key, payout, bal)
                e.set_footer(text="Timed out — hand auto-drawn")
                await self.message.edit(embed=e, view=self)
            except Exception:
                pass
        self.cog._active.discard(self.player.id)


class ReplayView(discord.ui.View):
    def __init__(self, cog, player: discord.Member, bet: int):
        super().__init__(timeout=60)
        self.cog    = cog
        self.player = player
        self.bet    = bet
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="🔁  Deal Again", style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        if self.player.id in self.cog._active:
            return await interaction.response.send_message("Finish your current hand first.",
                                                           ephemeral=True)
        max_bet = await casino_premium.get_max_bet(self.player.id)
        if self.bet > max_bet:
            return await interaction.response.send_message(
                f"Your max bet is now 🪙 {max_bet:,}.", ephemeral=True)
        if not await casino_wallet.deduct(self.player.id, self.bet):
            return await interaction.response.send_message("❌ Not enough casino coins.",
                                                           ephemeral=True)
        self.cog._active.add(self.player.id)
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

        view = VideoPokerView(self.cog, self.player, self.bet)
        view.message = await interaction.followup.send(
            embed=view.build_embed(), view=view, wait=True)

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class VideoPokerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active: set[int] = set()

    @commands.command(name="videopoker", aliases=["vp", "jacks"])
    async def videopoker(self, ctx: commands.Context, bet: int = 0):
        """🃏 Jacks or Better video poker. Usage: ;videopoker <bet>"""
        pid = ctx.author.id

        if pid in self._active:
            return await ctx.send("❌ You already have a hand in play!")

        max_bet = await casino_premium.get_max_bet(pid)
        if not (MIN_BET <= bet <= max_bet):
            return await ctx.send(f"❌ Bet must be 🪙 {MIN_BET:,}–{max_bet:,}.")

        if not await casino_wallet.deduct(pid, bet):
            return await ctx.send("❌ Not enough casino coins.")

        self._active.add(pid)
        view = VideoPokerView(self, ctx.author, bet)
        try:
            view.message = await ctx.send(embed=view.build_embed(), view=view)
        except discord.DiscordException:
            await casino_wallet.credit(pid, bet)
            self._active.discard(pid)

    @commands.command(name="vppaytable", aliases=["videopokerinfo"])
    async def vp_paytable(self, ctx: commands.Context, bet: int = 0):
        """Show the video poker paytable."""
        await ctx.send(embed=_paytable_embed(max(0, bet)))


async def setup(bot: commands.Bot):
    await bot.add_cog(VideoPokerCog(bot))
