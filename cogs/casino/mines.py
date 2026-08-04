import discord
from discord.ext import commands
from . import casino_wallet
from . import casino_premium
from discord import app_commands
import random
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────
GRID_ROWS   = 4
GRID_COLS   = 5
GRID_SIZE   = GRID_ROWS * GRID_COLS   # 20 tiles
MIN_BET     = 10
MAX_BET     = 50_000
MINE_COUNTS = [1, 2, 3, 5, 7, 10, 15]
SESSION_TTL = 300   # seconds before idle forfeit

HIDDEN = "\U0001f7e6"   # 🟦
SAFE   = "\U0001f48e"   # 💎
MINE_E = "\U0001f4a5"   # 💥


# ── Multiplier math ───────────────────────────────────────────────────────────
def _cashout_multiplier(mines: int, revealed: int) -> float:
    """
    Cumulative multiplier after `revealed` safe picks on a 20-tile grid.
    Each pick: mult *= (tiles_left / safe_left) * 0.97   (3% house edge total)
    """
    safe_total = GRID_SIZE - mines
    mult = 1.0
    for i in range(revealed):
        safe_left  = safe_total - i
        tiles_left = GRID_SIZE - i
        if safe_left <= 0:
            break
        mult *= (tiles_left / safe_left) * 0.97
    return round(mult, 3)


def _next_multiplier(mines: int, revealed: int) -> float:
    """Multiplier the NEXT safe tile would bring the total to."""
    return _cashout_multiplier(mines, revealed + 1)


# ── Game state ────────────────────────────────────────────────────────────────
class MinesGame:
    def __init__(self, player_id: int, bet: int, mine_count: int):
        self.player_id   = player_id
        self.bet         = bet
        self.mine_count  = mine_count
        self.revealed    = 0
        self.cashed_out  = False
        self.dead        = False

        positions            = list(range(GRID_SIZE))
        random.shuffle(positions)
        self.mine_positions  = set(positions[:mine_count])
        self.revealed_tiles: dict[int, bool] = {}   # idx → True=safe / False=mine

    @property
    def current_multiplier(self) -> float:
        return _cashout_multiplier(self.mine_count, self.revealed)

    @property
    def next_mult(self) -> float:
        return _next_multiplier(self.mine_count, self.revealed)

    @property
    def winnings(self) -> int:
        return max(0, int(self.bet * self.current_multiplier))

    @property
    def profit(self) -> int:
        return self.winnings - self.bet

    def reveal(self, idx: int) -> bool:
        """Returns True=safe, False=mine. Marks dead on mine."""
        is_safe = idx not in self.mine_positions
        self.revealed_tiles[idx] = is_safe
        if is_safe:
            self.revealed += 1
        else:
            self.dead = True
            for m in self.mine_positions:
                if m not in self.revealed_tiles:
                    self.revealed_tiles[m] = False
        return is_safe

    @property
    def all_safe_revealed(self) -> bool:
        return self.revealed == GRID_SIZE - self.mine_count


# ── Mine select dropdown ──────────────────────────────────────────────────────
class MineCountSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for n in MINE_COUNTS:
            safe = GRID_SIZE - n
            first = round((GRID_SIZE / safe) * 0.97, 3)
            risk  = "🟢 Low" if n <= 2 else "🟡 Medium" if n <= 7 else "🔴 High"
            options.append(discord.SelectOption(
                label=f"{n} mines  —  {first}x first tile",
                value=str(n),
                description=f"{risk} risk  |  {safe} safe tiles"
            ))
        super().__init__(placeholder="Choose mine count…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: MinesSetupView = self.view
        if interaction.user.id != view.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        view.mine_count = int(self.values[0])
        view.start_btn.disabled = False
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class MinesSetupView(discord.ui.View):
    def __init__(self, player: discord.Member, bet: int, cog=None):
        super().__init__(timeout=60)
        self.player     = player
        self.bet        = bet
        self.cog        = cog
        self.mine_count: Optional[int] = None
        self.started    = False   # True once the grid game actually launches
        self.add_item(MineCountSelect())

    def build_embed(self) -> discord.Embed:
        e = discord.Embed(title="💣  Mines", color=0x2b2d31)
        e.add_field(name="Bet", value=f"🪙 {self.bet:,}", inline=True)
        if self.mine_count:
            safe = GRID_SIZE - self.mine_count
            e.add_field(name="Mines", value=str(self.mine_count), inline=True)
            e.add_field(name="Safe tiles", value=str(safe), inline=True)
        e.set_footer(text="Select mine count → Start Game")
        return e

    @discord.ui.button(label="▶  Start Game", style=discord.ButtonStyle.green,
                       disabled=True, row=1)
    async def start_btn(self, interaction: discord.Interaction,
                        button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        self.started = True
        self.stop()
        game = MinesGame(self.player.id, self.bet, self.mine_count)
        view = MinesGridView(self.player, game, cog=self.cog)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)
        view.message = await interaction.original_response()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


# ── Grid tile button ──────────────────────────────────────────────────────────
class TileButton(discord.ui.Button):
    def __init__(self, idx: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="\u200b",     # invisible label — keeps button square
            row=idx // GRID_COLS,
        )
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        view: MinesGridView = self.view
        if interaction.user.id != view.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        if view.game.dead or view.game.cashed_out:
            return await interaction.response.defer()
        if self.disabled:
            return await interaction.response.defer()

        safe = view.game.reveal(self.idx)

        if safe:
            self.style    = discord.ButtonStyle.success
            self.emoji    = discord.PartialEmoji.from_str(SAFE)
            self.label    = ""
            self.disabled = True
        else:
            self.style    = discord.ButtonStyle.danger
            self.emoji    = discord.PartialEmoji.from_str(MINE_E)
            self.label    = ""
            self.disabled = True

        if view.game.dead:
            # Reveal every mine on the board (reveal() pre-marks them in
            # revealed_tiles, so style by mine_positions, not by that dict).
            for child in view.children:
                if isinstance(child, TileButton):
                    if child.idx in view.game.mine_positions:
                        child.style = discord.ButtonStyle.danger
                        child.emoji = discord.PartialEmoji.from_str(MINE_E)
                        child.label = ""
                    child.disabled = True
            view.cashout_btn.disabled = True
            # bet already deducted at game start — nothing to do on loss
            await interaction.response.edit_message(embed=view.build_embed(lost=True), view=view)
            if view.cog:
                view.cog._active.discard(view.player.id)
            view.stop()

        elif view.game.all_safe_revealed:
            # Perfect run
            view.game.cashed_out = True
            view._lock_all()
            await casino_wallet.credit(view.player.id, view.game.winnings)
            await interaction.response.edit_message(embed=view.build_embed(perfect=True), view=view)
            if view.cog:
                view.cog._active.discard(view.player.id)
            view.stop()

        else:
            await interaction.response.edit_message(embed=view.build_embed(), view=view)


# ── Grid view (4 rows tiles + 1 row controls) ─────────────────────────────────
class MinesGridView(discord.ui.View):
    def __init__(self, player: discord.Member, game: MinesGame, cog=None):
        super().__init__(timeout=SESSION_TTL)
        self.player  = player
        self.game    = game
        self.cog     = cog   # reference so callbacks can clear _active
        self.message: Optional[discord.Message] = None

        for idx in range(GRID_SIZE):   # 20 tiles → rows 0-3
            self.add_item(TileButton(idx))
        # row 4 gets the cash-out button (added via decorator below)

    @discord.ui.button(label="💰  Cash Out", style=discord.ButtonStyle.primary, row=4)
    async def cashout_btn(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        if self.game.dead or self.game.cashed_out:
            return await interaction.response.defer()
        if self.game.revealed == 0:
            return await interaction.response.send_message(
                "Reveal at least one tile first!", ephemeral=True)

        self.game.cashed_out = True
        self._lock_all()
        await casino_wallet.credit(self.player.id, self.game.winnings)
        await interaction.response.edit_message(embed=self.build_embed(won=True), view=self)
        await interaction.followup.send(
            f"💰 **{self.player.display_name}** cashed out with **{self.game.revealed}** tile(s) revealed!\n"
            f"Won: 🪙 {self.game.winnings:,}  |  Profit: +🪙 {self.game.profit:,}"
        )
        if self.cog:
            self.cog._active.discard(self.player.id)
        self.stop()

    def _lock_all(self):
        for child in self.children:
            child.disabled = True

    def build_embed(self, lost=False, won=False, perfect=False) -> discord.Embed:
        g = self.game

        if lost:
            color = 0xe74c3c
            title = "💥  Mine Hit!"
            desc  = f"You lost **🪙 {g.bet:,}**"
        elif perfect:
            color = 0xf1c40f
            title = "🏆  Perfect Run!"
            desc  = f"All safe tiles cleared!\nWon **🪙 {g.winnings:,}** (+{g.profit:,})"
        elif won:
            color = 0x2ecc71
            title = "💰  Cashed Out!"
            desc  = f"Walked away with **🪙 {g.winnings:,}** (+{g.profit:,})"
        else:
            color = 0xf1c40f
            title = "💣  Mines"
            desc  = (
                f"Bet: **🪙 {g.bet:,}**\n"
                f"Cash out now: **🪙 {g.winnings:,}**"
                + (f" (+{g.profit:,})" if g.revealed > 0 else "")
            )

        e = discord.Embed(title=title, description=desc, color=color)

        row1 = []
        row2 = []

        if not lost and not won and not perfect:
            row1 += [
                ("Multiplier",  f"{g.current_multiplier:.3f}x"),
                ("Next tile",   f"{g.next_mult:.3f}x"),
            ]
            row2 += [
                ("Tiles safe",  str(g.revealed)),
                ("Mines",       str(g.mine_count)),
            ]
        else:
            row1 += [
                ("Multiplier",  f"{g.current_multiplier:.3f}x"),
                ("Tiles safe",  str(g.revealed)),
                ("Mines",       str(g.mine_count)),
            ]

        for name, val in row1 + row2:
            e.add_field(name=name, value=val, inline=True)

        e.set_footer(text=f"{self.player.display_name}  •  4×5 grid  •  {GRID_SIZE - g.mine_count} safe tiles")
        return e

    async def on_timeout(self):
        if not self.game.dead and not self.game.cashed_out:
            self.game.dead = True
            self._lock_all()
            # bet already deducted at game start — nothing extra to do on timeout
            if self.message:
                try:
                    e = self.build_embed(lost=True)
                    e.set_footer(text="Session timed out — bet forfeited")
                    await self.message.edit(embed=e, view=self)
                except Exception:
                    pass
        if self.cog:
            self.cog._active.discard(self.player.id)


# ── Cog ───────────────────────────────────────────────────────────────────────
class MinesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot           = bot
        self._active: set[int] = set()   # player IDs currently in a game

    @commands.command(name="mines")
    async def mines(self, ctx: commands.Context, opponent: Optional[discord.Member] = None, bet: int = 0):
        """Reveal tiles, avoid mines, cash out before you explode. Usage: ;mines <bet>"""
        pid = ctx.author.id

        if pid in self._active:
            return await ctx.send("❌ You already have an active Mines session!")

        _max_bet = await casino_premium.get_max_bet(ctx.author.id)
        if not (MIN_BET <= bet <= _max_bet):
            return await ctx.send(f"❌ Bet must be 🪙 {MIN_BET:,}–{_max_bet:,}.")

        if not await casino_wallet.can_afford(pid, bet):
            return await ctx.send("❌ Not enough casino coins.")
        await casino_wallet.deduct(pid, bet)

        self._active.add(pid)

        view  = MinesSetupView(ctx.author, bet, cog=self)
        embed = view.build_embed()
        view.message = await ctx.send(embed=embed, view=view)

        await view.wait()   # waits for Start or timeout

        # If the grid game never launched (timed out at the mine-select screen,
        # even if a count was picked), refund and release the active lock.
        if not view.started:
            await casino_wallet.credit(pid, bet)
            self._active.discard(pid)
        # Otherwise the grid view is now running; it clears _active via on_timeout or stop callbacks.


async def setup(bot: commands.Bot):
    await bot.add_cog(MinesCog(bot))
