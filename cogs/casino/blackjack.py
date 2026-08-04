"""
blackjack.py  —  Player vs Dealer AI
Standard Blackjack rules: Hit, Stand, Double Down, Split (pairs)
Dealer stands on soft 17.
"""
import discord
from discord.ext import commands
from discord import app_commands
import random
from typing import Optional
from . import casino_wallet
from . import casino_premium

MIN_BET = 10
MAX_BET = 50_000
SESSION_TTL = 180

SUITS  = ["♠", "♥", "♦", "♣"]
RANKS  = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
VALUES = {"A": 11, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
          "7": 7, "8": 8, "9": 9, "10": 10, "J": 10, "Q": 10, "K": 10}


def new_deck() -> list:
    d = [{"rank": r, "suit": s} for r in RANKS for s in SUITS] * 2
    random.shuffle(d)
    return d


def card_str(c: dict) -> str:
    return f"{c['rank']}{c['suit']}"


def hand_value(hand: list) -> int:
    total = sum(VALUES[c["rank"]] for c in hand)
    aces  = sum(1 for c in hand if c["rank"] == "A")
    while total > 21 and aces:
        total -= 10
        aces  -= 1
    return total


def hand_str(hand: list, hide_second=False) -> str:
    if hide_second and len(hand) >= 2:
        return f"{card_str(hand[0])}  🂠"
    return "  ".join(card_str(c) for c in hand)


def is_blackjack(hand: list) -> bool:
    return len(hand) == 2 and hand_value(hand) == 21


class BJGame:
    def __init__(self, pid: int, bet: int):
        self.pid         = pid
        self.bet         = bet
        self.deck        = new_deck()
        self.player_hand = [self.deck.pop(), self.deck.pop()]
        self.dealer_hand = [self.deck.pop(), self.deck.pop()]
        self.split_hand: Optional[list] = None
        self.active_hand = 0   # 0=main, 1=split
        self.hand_doubled = [False, False]   # per-hand double flag (0=main, 1=split)
        self.finished    = False
        self.result      = ""  # win/lose/push/blackjack/bust

    @property
    def current_hand(self) -> list:
        return self.split_hand if self.active_hand == 1 else self.player_hand

    @property
    def total_staked(self) -> int:
        """Total coins wagered across all hands (matches what was deducted)."""
        staked = self.bet * (2 if self.hand_doubled[0] else 1)
        if self.split_hand:
            staked += self.bet * (2 if self.hand_doubled[1] else 1)
        return staked

    def can_split(self) -> bool:
        h = self.player_hand
        return (len(h) == 2
                and h[0]["rank"] == h[1]["rank"]
                and self.split_hand is None)

    def can_double(self) -> bool:
        return len(self.current_hand) == 2 and not self.hand_doubled[self.active_hand]

    def dealer_play(self):
        while hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())

    def resolve(self) -> list[tuple[str, int]]:
        """Returns list of (result_label, net_coins) per hand."""
        self.dealer_play()
        dv     = hand_value(self.dealer_hand)
        dbust  = dv > 21
        hands  = [self.player_hand]
        bets   = [self.bet * (2 if self.hand_doubled[0] else 1)]
        if self.split_hand:
            hands.append(self.split_hand)
            bets.append(self.bet * (2 if self.hand_doubled[1] else 1))

        results = []
        for hand, bet in zip(hands, bets):
            pv   = hand_value(hand)
            pbj  = is_blackjack(hand)
            bust = pv > 21

            if bust:
                results.append(("Bust 💀", -bet))
            elif pbj and not is_blackjack(self.dealer_hand):
                pay = int(bet * 1.5)
                results.append(("Blackjack! 🃏", pay))
            elif dbust or pv > dv:
                results.append(("Win 🏆", bet))
            elif pv == dv:
                results.append(("Push 🤝", 0))
            else:
                results.append(("Lose 💸", -bet))
        self.finished = True
        return results


class BJView(discord.ui.View):
    def __init__(self, player: discord.Member, game: BJGame):
        super().__init__(timeout=SESSION_TTL)
        self.player  = player
        self.game    = game
        self.message: Optional[discord.Message] = None
        self._update_buttons()

    def _update_buttons(self):
        g = self.game
        self.hit_btn.disabled    = g.finished
        self.stand_btn.disabled  = g.finished
        self.double_btn.disabled = g.finished or not g.can_double()
        self.split_btn.disabled  = g.finished or not g.can_split()

    def build_embed(self, results=None) -> discord.Embed:
        g   = self.game
        pv  = hand_value(g.player_hand)
        done = g.finished or results is not None

        color = 0x2b2d31
        if results:
            total_net = sum(r[1] for r in results)
            color = 0x2ecc71 if total_net > 0 else 0xe74c3c if total_net < 0 else 0x95a5a6

        e = discord.Embed(title="🃏  Blackjack", color=color)

        dealer_val = hand_value(g.dealer_hand) if done else hand_value([g.dealer_hand[0]])
        e.add_field(
            name=f"Dealer  [{hand_value(g.dealer_hand) if done else '?'}]",
            value=hand_str(g.dealer_hand, hide_second=not done),
            inline=False
        )
        e.add_field(
            name=f"You  [{pv}]{'  💥 BUST' if pv > 21 else '  🃏 BJ' if is_blackjack(g.player_hand) else ''}",
            value=hand_str(g.player_hand),
            inline=False
        )
        if g.split_hand:
            sv = hand_value(g.split_hand)
            e.add_field(
                name=f"Split Hand  [{sv}]{'  💥' if sv > 21 else ''}",
                value=hand_str(g.split_hand),
                inline=False
            )

        if results:
            out = []
            for label, net in results:
                sign = "+" if net >= 0 else ""
                out.append(f"{label}  —  {sign}🪙 {net:,}")
            e.add_field(name="Result", value="\n".join(out), inline=False)
        else:
            e.add_field(name="Bet", value=f"🪙 {g.bet:,}", inline=True)
            if any(g.hand_doubled):
                e.add_field(name="Wagered", value=f"🪙 {g.total_staked:,}", inline=True)

        e.set_footer(text=f"{self.player.display_name}  •  Dealer stands on soft 17")
        return e

    async def _finish(self, interaction: discord.Interaction):
        results = self.game.resolve()
        self._update_buttons()
        for child in self.children:
            child.disabled = True

        net = sum(r[1] for r in results)
        # Return = everything wagered (across all hands) plus net result.
        # net == -total_staked on a full loss -> gross 0; push -> stake back; win -> stake+profit.
        gross_return = self.game.total_staked + net
        if gross_return > 0:
            await casino_wallet.credit(self.player.id, gross_return)

        await interaction.response.edit_message(
            embed=self.build_embed(results=results), view=self)
        self.stop()

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, row=0)
    async def hit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        self.game.current_hand.append(self.game.deck.pop())
        pv = hand_value(self.game.current_hand)

        if pv > 21:
            # Bust current hand
            if self.game.active_hand == 0 and self.game.split_hand:
                self.game.active_hand = 1  # move to split
                self._update_buttons()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)
            else:
                await self._finish(interaction)
        elif pv == 21:
            if self.game.active_hand == 0 and self.game.split_hand:
                self.game.active_hand = 1
                self._update_buttons()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)
            else:
                await self._finish(interaction)
        else:
            self._update_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, row=0)
    async def stand_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        if self.game.active_hand == 0 and self.game.split_hand:
            self.game.active_hand = 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        else:
            await self._finish(interaction)

    @discord.ui.button(label="Double Down", style=discord.ButtonStyle.success, row=0)
    async def double_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        if not self.game.can_double():
            return await interaction.response.send_message(
                "Can't double down right now.", ephemeral=True)
        if not await casino_wallet.deduct(self.player.id, self.game.bet):
            return await interaction.response.send_message(
                "Not enough coins to double down.", ephemeral=True)
        self.game.hand_doubled[self.game.active_hand] = True
        self.game.current_hand.append(self.game.deck.pop())

        # Doubling takes exactly one card, then the hand is done.
        # If we just doubled the main hand and a split hand is waiting, move to it.
        if self.game.active_hand == 0 and self.game.split_hand:
            self.game.active_hand = 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        else:
            await self._finish(interaction)

    @discord.ui.button(label="Split", style=discord.ButtonStyle.danger, row=0)
    async def split_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        if not await casino_wallet.deduct(self.player.id, self.game.bet):
            return await interaction.response.send_message(
                "Not enough coins to split.", ephemeral=True)
        card = self.game.player_hand.pop()
        self.game.split_hand = [card, self.game.deck.pop()]
        self.game.player_hand.append(self.game.deck.pop())
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        self.game.finished = True
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                e = self.build_embed()
                e.description = "⏰ Session timed out — bet forfeited."
                await self.message.edit(embed=e, view=self)
            except Exception:
                pass
        self.stop()


class BlackjackCog(commands.Cog):
    def __init__(self, bot):
        self.bot     = bot
        self._active: set[int] = set()

    @commands.command(name="blackjack", aliases=["bj"])
    async def blackjack(self, ctx: commands.Context, opponent: Optional[discord.Member] = None, bet: int = 0):
        """Play Blackjack against the dealer. Usage: ;blackjack <bet>"""
        pid = ctx.author.id
        if pid in self._active:
            return await ctx.send("❌ You have an active Blackjack game!")
        _max_bet = await casino_premium.get_max_bet(ctx.author.id)
        if not (MIN_BET <= bet <= _max_bet):
            return await ctx.send(f"❌ Bet must be 🪙 {MIN_BET:,}–{_max_bet:,}.")
        if not await casino_wallet.deduct(pid, bet):
            return await ctx.send("❌ Not enough casino coins.")

        self._active.add(pid)
        game  = BJGame(pid, bet)

        # Instant blackjack check
        if is_blackjack(game.player_hand):
            results = game.resolve()
            net     = sum(r[1] for r in results)
            gross_return = game.total_staked + net
            if gross_return > 0:
                await casino_wallet.credit(pid, gross_return)
            view  = BJView(ctx.author, game)
            for c in view.children:
                c.disabled = True
            self._active.discard(pid)
            return await ctx.send(embed=view.build_embed(results=results), view=view)

        view  = BJView(ctx.author, game)
        msg   = await ctx.send(embed=view.build_embed(), view=view)
        view.message = msg
        await view.wait()
        self._active.discard(pid)


async def setup(bot):
    await bot.add_cog(BlackjackCog(bot))
