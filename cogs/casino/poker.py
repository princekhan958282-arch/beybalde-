"""
poker.py  —  Texas Hold'em Poker (2–6 players)
Betting rounds: Pre-flop, Flop, Turn, River.
Standard hand rankings. Side pots not implemented (all-in = full bet).
"""
import discord
from discord.ext import commands
from discord import app_commands
import random
import asyncio
from collections import Counter
from typing import Optional
from . import casino_wallet

MIN_BET      = 50
MAX_BET      = 25_000
BLIND        = 25     # small blind (big blind = BLIND*2)
JOIN_WINDOW  = 45     # seconds to join before game starts
ACTION_TIME  = 60     # seconds per player action

SUITS  = ["♠","♥","♦","♣"]
RANKS  = ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]
VALUES = {r: i+2 for i, r in enumerate(RANKS)}


def new_deck():
    d = [{"rank": r, "suit": s} for r in RANKS for s in SUITS]
    random.shuffle(d)
    return d


def card_str(c):
    return f"{c['rank']}{c['suit']}"


def hand_str(cards):
    return "  ".join(card_str(c) for c in cards)


# ── Hand Evaluation ───────────────────────────────────────────────────────────
HAND_NAMES = [
    "High Card", "One Pair", "Two Pair", "Three of a Kind",
    "Straight", "Flush", "Full House", "Four of a Kind",
    "Straight Flush", "Royal Flush"
]

def _vals(cards):
    return sorted([VALUES[c["rank"]] for c in cards], reverse=True)

def _suits(cards):
    return [c["suit"] for c in cards]

def evaluate_hand(hole: list, community: list) -> tuple[int, list, str]:
    """
    Returns (rank 0-9, tiebreaker list, name) for best 5-card hand
    from 7 cards (2 hole + 5 community).
    Simple evaluator — checks all 5-card combos.
    """
    from itertools import combinations
    all_cards = hole + community
    best = (-1, [], "")
    for combo in combinations(all_cards, 5):
        r, tb = _eval5(list(combo))
        if (r, tb) > (best[0], best[1]):
            best = (r, tb, HAND_NAMES[r])
    return best


def _eval5(cards):
    vals  = sorted([VALUES[c["rank"]] for c in cards], reverse=True)
    suits = [c["suit"] for c in cards]
    cnt   = Counter(vals)
    counts = sorted(cnt.values(), reverse=True)
    is_flush    = len(set(suits)) == 1
    is_straight = (len(cnt) == 5 and vals[0] - vals[4] == 4) or \
                  (sorted(vals) == [2,3,4,5,14])  # wheel

    if is_straight and is_flush:
        if sorted(vals) == [2, 3, 4, 5, 14]:
            return 8, [5]          # wheel (A-2-3-4-5) — lowest straight flush
        if vals == [14, 13, 12, 11, 10]:
            return 9, vals         # royal (10-J-Q-K-A)
        return 8, vals
    if counts[0] == 4:
        quad = [v for v,c in cnt.items() if c==4]
        kick = [v for v,c in cnt.items() if c!=4]
        return 7, quad + kick
    if counts[:2] == [3,2]:
        trip = [v for v,c in cnt.items() if c==3]
        pair = [v for v,c in cnt.items() if c==2]
        return 6, trip + pair
    if is_flush:
        return 5, vals
    if is_straight:
        return 4, [5] if vals[0]==14 and vals[1]==5 else vals
    if counts[0] == 3:
        trip = [v for v,c in cnt.items() if c==3]
        kick = sorted([v for v,c in cnt.items() if c!=3], reverse=True)
        return 3, trip + kick
    if counts[:2] == [2,2]:
        pairs = sorted([v for v,c in cnt.items() if c==2], reverse=True)
        kick  = [v for v,c in cnt.items() if c==1]
        return 2, pairs + kick
    if counts[0] == 2:
        pair = [v for v,c in cnt.items() if c==2]
        kick = sorted([v for v,c in cnt.items() if c!=2], reverse=True)
        return 1, pair + kick
    return 0, vals


# ── Game State ────────────────────────────────────────────────────────────────
class Player:
    def __init__(self, member: discord.Member, buy_in: int):
        self.member   = member
        self.chips    = buy_in
        self.hole     = []
        self.bet      = 0      # current round bet
        self.folded   = False
        self.all_in   = False

    @property
    def id(self): return self.member.id


class PokerGame:
    def __init__(self, players: list[Player], buy_in: int):
        self.players   = players
        self.buy_in    = buy_in
        self.deck      = new_deck()
        self.community: list = []
        self.pot       = 0
        self.stage     = "preflop"   # preflop/flop/turn/river/showdown
        self.cur_idx   = 0
        self.min_raise = BLIND * 2
        self.round_bet = 0           # highest bet in current round
        self.acted: set = set()      # ids of players who've acted since last aggression

        # Post blinds
        if len(players) >= 2:
            self._post_blind(0, BLIND)
            self._post_blind(1, BLIND * 2)
            self.round_bet = BLIND * 2
            self.cur_idx   = 2 % len(players)

        # Deal hole cards
        for p in players:
            p.hole = [self.deck.pop(), self.deck.pop()]

    def _post_blind(self, idx: int, amount: int):
        p      = self.players[idx]
        actual = min(p.chips, amount)
        p.chips -= actual
        p.bet   += actual
        self.pot += actual

    @property
    def current_player(self) -> Player:
        return self.players[self.cur_idx]

    def active_players(self) -> list[Player]:
        return [p for p in self.players if not p.folded]

    def advance(self):
        """Move to the next player who can still act (not folded, not all-in)."""
        n = len(self.players)
        for _ in range(n):
            self.cur_idx = (self.cur_idx + 1) % n
            p = self.players[self.cur_idx]
            if not p.folded and not p.all_in:
                return

    def can_act_count(self) -> int:
        """How many active players still have chips to act with."""
        return sum(1 for p in self.active_players() if not p.all_in)

    def next_stage(self):
        stages = ["preflop", "flop", "turn", "river", "showdown"]
        idx = stages.index(self.stage)
        self.stage = stages[idx + 1]
        # Reset bets for new round
        for p in self.players:
            p.bet = 0
        self.round_bet = 0
        self.acted = set()           # nobody has acted in the new street yet
        # First to act in the new street: first player who can act
        self.cur_idx = 0
        for _ in range(len(self.players)):
            p = self.players[self.cur_idx]
            if not p.folded and not p.all_in:
                break
            self.cur_idx = (self.cur_idx + 1) % len(self.players)

        if self.stage == "flop":
            self.community += [self.deck.pop() for _ in range(3)]
        elif self.stage in ("turn", "river"):
            self.community.append(self.deck.pop())

    def is_round_over(self) -> bool:
        actives = self.active_players()
        if len(actives) <= 1:
            return True
        for p in actives:
            if p.all_in:
                continue
            # A street is only over once each player has acted AND matched the bet.
            if p.id not in self.acted:
                return False
            if p.bet != self.round_bet:
                return False
        return True

    def resolve_showdown(self) -> list[tuple[Player, str]]:
        actives = self.active_players()
        results = []
        for p in actives:
            rank, tb, name = evaluate_hand(p.hole, self.community)
            results.append((p, rank, tb, name))
        results.sort(key=lambda x: (x[1], x[2]), reverse=True)
        # Find winner(s)
        top_rank, top_tb = results[0][1], results[0][2]
        winners = [r for r in results if r[1] == top_rank and r[2] == top_tb]
        split     = self.pot // len(winners)
        remainder = self.pot % len(winners)
        out = []
        for i, w in enumerate(winners):
            payout = split + (remainder if i == 0 else 0)
            out.append((w[0], payout, w[3]))
        return out


# ── Views ─────────────────────────────────────────────────────────────────────
class PokerActionView(discord.ui.View):
    def __init__(self, game: PokerGame, update_cb):
        super().__init__(timeout=ACTION_TIME)
        self.game      = game
        self.update_cb = update_cb
        self.action    = None
        self.amount    = 0
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        g   = self.game
        cp  = g.current_player
        can_check = cp.bet == g.round_bet

        if can_check:
            self.add_item(discord.ui.Button(
                label="✅ Check", style=discord.ButtonStyle.secondary,
                custom_id="check", row=0))
        else:
            call_amt = g.round_bet - cp.bet
            self.add_item(discord.ui.Button(
                label=f"📞 Call {call_amt}", style=discord.ButtonStyle.primary,
                custom_id="call", row=0))

        self.add_item(discord.ui.Button(
            label="📈 Raise", style=discord.ButtonStyle.success,
            custom_id="raise", row=0))
        self.add_item(discord.ui.Button(
            label="❌ Fold", style=discord.ButtonStyle.danger,
            custom_id="fold", row=0))

        for child in self.children:
            child.callback = self._action_cb

    async def _action_cb(self, interaction: discord.Interaction):
        if interaction.user.id != self.game.current_player.id:
            return await interaction.response.send_message("Not your turn.", ephemeral=True)

        cid = interaction.data["custom_id"]
        cp  = self.game.current_player

        if cid == "fold":
            cp.folded = True
            self.action = "fold"
            self.game.acted.add(cp.id)
            await interaction.response.defer()

        elif cid == "check":
            self.action = "check"
            self.game.acted.add(cp.id)
            await interaction.response.defer()

        elif cid == "call":
            amt = min(self.game.round_bet - cp.bet, cp.chips)
            cp.chips    -= amt
            cp.bet      += amt
            self.game.pot += amt
            if cp.chips == 0:
                cp.all_in = True
            self.action   = "call"
            self.game.acted.add(cp.id)
            await interaction.response.defer()

        elif cid == "raise":
            await interaction.response.send_modal(RaiseModal(self))
            return

        self.stop()
        await self.update_cb(self)

    async def on_timeout(self):
        # Auto-fold on timeout. Leave self.action as None so the prompt loop
        # knows to drive the game forward from _prompt_action.
        self.game.current_player.folded = True
        self.stop()


class RaiseModal(discord.ui.Modal, title="Raise Amount"):
    amount = discord.ui.TextInput(label="Raise to (total bet)", placeholder="e.g. 200")

    def __init__(self, action_view: PokerActionView):
        super().__init__()
        self._av = action_view

    async def on_submit(self, interaction: discord.Interaction):
        # If the action view already resolved (timed out / another action
        # landed), this raise is stale — ignore it so we don't corrupt chips,
        # the pot, or double-advance the game.
        if self._av.is_finished():
            return await interaction.response.send_message(
                "That action already expired.", ephemeral=True)

        try:
            amt = int(self.amount.value.replace(",","").strip())
        except ValueError:
            return await interaction.response.send_message("Invalid amount.", ephemeral=True)

        g  = self._av.game
        cp = g.current_player
        if amt <= g.round_bet:
            return await interaction.response.send_message(
                f"Raise must exceed {g.round_bet}.", ephemeral=True)
        if amt > cp.chips + cp.bet:
            amt = cp.chips + cp.bet   # all-in

        diff        = amt - cp.bet
        cp.chips   -= diff
        cp.bet      = amt
        g.pot      += diff
        g.round_bet = amt
        if cp.chips == 0:
            cp.all_in = True
        g.acted = {cp.id}   # raise resets acted — everyone else must respond
        self._av.action = "raise"
        self._av.amount = amt
        self._av.stop()
        await interaction.response.defer()
        await self._av.update_cb(interaction)


class PokerLobbyView(discord.ui.View):
    def __init__(self, host: discord.Member, buy_in: int):
        super().__init__(timeout=JOIN_WINDOW)
        self.host        = host
        self.buy_in      = buy_in
        self.joined: list[discord.Member] = [host]
        self.message: Optional[discord.Message] = None
        self.started     = False

    def build_embed(self) -> discord.Embed:
        e = discord.Embed(title="♠️  Poker Table", color=0x27ae60)
        e.add_field(name="Buy-in",   value=f"🪙 {self.buy_in:,}", inline=True)
        e.add_field(name="Players",  value=f"{len(self.joined)}/6", inline=True)
        e.add_field(name="Blinds",   value=f"🪙 {BLIND}/{BLIND*2}", inline=True)
        names = "\n".join(f"• {p.display_name}" for p in self.joined)
        e.add_field(name="Seated", value=names, inline=False)
        e.set_footer(text=f"Game starts when host clicks Start ({JOIN_WINDOW}s window)")
        return e

    @discord.ui.button(label="🪑 Join Table", style=discord.ButtonStyle.primary, row=0)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if any(p.id == interaction.user.id for p in self.joined):
            return await interaction.response.send_message("Already seated.", ephemeral=True)
        if len(self.joined) >= 6:
            return await interaction.response.send_message("Table full!", ephemeral=True)
        if not await casino_wallet.deduct(interaction.user.id, self.buy_in):
            return await interaction.response.send_message(
                "Not enough casino coins.", ephemeral=True)
        self.joined.append(interaction.user)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶ Start Game", style=discord.ButtonStyle.success, row=0)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            return await interaction.response.send_message("Only host can start.", ephemeral=True)
        if len(self.joined) < 2:
            return await interaction.response.send_message(
                "Need at least 2 players.", ephemeral=True)
        self.started = True
        self.stop()
        await interaction.response.defer()

    async def on_timeout(self):
        if not self.started:
            # Refund all
            for p in self.joined:
                await casino_wallet.credit(p.id, self.buy_in)
            for child in self.children:
                child.disabled = True
            if self.message:
                try:
                    e = self.build_embed()
                    e.set_footer(text="Timed out — buy-ins refunded")
                    await self.message.edit(embed=e, view=self)
                except Exception:
                    pass
        self.stop()


# ── Poker Cog ─────────────────────────────────────────────────────────────────
class PokerCog(commands.Cog):
    def __init__(self, bot):
        self.bot     = bot
        self._active: set[int] = set()   # hosts currently running a table

    @commands.command(name="poker")
    async def poker(self, ctx: commands.Context, buy_in: int = 0):
        """Texas Hold'em poker table (2–6 players). Usage: ;poker <buy_in>"""
        pid = ctx.author.id
        if pid in self._active:
            return await ctx.send("❌ You're already hosting a Poker table!")
        if not (MIN_BET <= buy_in <= MAX_BET):
            return await ctx.send(f"❌ Buy-in must be 🪙 {MIN_BET:,}–{MAX_BET:,}.")
        if not await casino_wallet.deduct(pid, buy_in):
            return await ctx.send("❌ Not enough casino coins.")

        self._active.add(pid)
        lobby = PokerLobbyView(ctx.author, buy_in)
        lobby.message = await ctx.send(embed=lobby.build_embed(), view=lobby)
        await lobby.wait()

        if not lobby.started or len(lobby.joined) < 2:
            self._active.discard(pid)
            return

        # Build player objects
        players = [Player(m, buy_in) for m in lobby.joined]
        game    = PokerGame(players, buy_in)

        await self._run_game(ctx, game, lobby.message)
        self._active.discard(pid)

    async def _run_game(self, ctx, game: PokerGame, msg: discord.Message):
        """Main game loop."""

        async def update_cb(interaction_or_none):
            # Advance to next player or next stage
            if len(game.active_players()) <= 1:
                await self._showdown(game, msg)
                return

            if game.is_round_over():
                if game.stage == "river":
                    await self._showdown(game, msg)
                    return
                game.next_stage()

            else:
                game.advance()

            await self._prompt_action(game, msg, update_cb)

        await self._prompt_action(game, msg, update_cb)

    async def _prompt_action(self, game: PokerGame, msg: discord.Message, update_cb):
        cp   = game.current_player
        comm = hand_str(game.community) if game.community else "*(none yet)*"

        e = discord.Embed(title=f"♠️  Poker  —  {game.stage.capitalize()}", color=0x27ae60)
        e.add_field(name="Community", value=comm, inline=False)
        e.add_field(name="Pot",       value=f"🪙 {game.pot:,}", inline=True)
        e.add_field(name="To act",    value=cp.member.mention, inline=True)
        e.add_field(name="Current bet", value=f"🪙 {game.round_bet:,}", inline=True)

        status = []
        for p in game.players:
            tag = "❌" if p.folded else "✅"
            status.append(f"{tag} {p.member.display_name}  chips: 🪙 {p.chips:,}  bet: {p.bet:,}")
        e.add_field(name="Players", value="\n".join(status), inline=False)

        # Send hole cards to current player via DM (best practice for poker)
        try:
            await cp.member.send(
                f"♠️ Your hole cards: **{hand_str(cp.hole)}**\n"
                f"Community: {comm}\nPot: 🪙 {game.pot:,}"
            )
        except Exception:
            pass   # DMs disabled — they can see community only

        view = PokerActionView(game, update_cb)
        try:
            await msg.edit(embed=e, view=view)
        except Exception:
            pass
        await view.wait()

        if view.action is None:
            # Timed out — auto-fold already handled in on_timeout; advance turn
            game.advance()
            await update_cb(None)

    async def _showdown(self, game: PokerGame, msg: discord.Message):
        winners = game.resolve_showdown()
        actives = game.active_players()

        e = discord.Embed(title="♠️  Showdown!", color=0xf1c40f)
        comm = hand_str(game.community) if game.community else "*(none)*"
        e.add_field(name="Community", value=comm, inline=False)

        hands_text = []
        for p in actives:
            _, _, name = evaluate_hand(p.hole, game.community)
            hands_text.append(f"{p.member.display_name}: {hand_str(p.hole)}  —  *{name}*")
        e.add_field(name="Hands", value="\n".join(hands_text), inline=False)

        win_text = []
        for w_player, split, hand_name in winners:
            await casino_wallet.credit(w_player.id, split)
            win_text.append(
                f"🏆 {w_player.member.display_name}  —  {hand_name}  →  🪙 {split:,}")
        e.add_field(name="Winners", value="\n".join(win_text), inline=False)

        # Return remaining chips to players
        for p in game.players:
            if p.chips > 0:
                await casino_wallet.credit(p.id, p.chips)

        try:
            await msg.edit(embed=e, view=None)
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(PokerCog(bot))
