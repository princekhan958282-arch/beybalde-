"""
tower_climb.py  —  PvE Tower Climb
Fight through floors vs increasingly hard RNG stat checks.
Each floor cleared = multiplier rises. Cash out between floors or risk going higher.
Fall on a failed floor = lose the bet.
"""
import discord
from discord.ext import commands
from discord import app_commands
import random
import asyncio
from typing import Optional
from . import casino_wallet
from . import casino_premium

MIN_BET     = 10
MAX_BET     = 50_000
SESSION_TTL = 300

# (floor_name, win_chance, flavor)
FLOORS = [
    ("Floor 1  — Slime Den",          0.90, "🟢"),
    ("Floor 2  — Goblin Horde",       0.82, "🟢"),
    ("Floor 3  — Stone Golem",        0.74, "🟡"),
    ("Floor 4  — Shadow Wolves",      0.66, "🟡"),
    ("Floor 5  — Dark Knight",        0.58, "🟠"),
    ("Floor 6  — Wyvern Rider",       0.50, "🟠"),
    ("Floor 7  — Demon Warlord",      0.42, "🔴"),
    ("Floor 8  — Chaos Titan",        0.34, "🔴"),
    ("Floor 9  — Void Dragon",        0.24, "💀"),
    ("Floor 10 — ☠️ THE BEYGOD",       0.12, "💀"),
]

def floor_multiplier(floor_idx: int) -> float:
    """Cumulative multiplier for clearing floor_idx (0-indexed)."""
    mult = 1.0
    for i in range(floor_idx + 1):
        chance = FLOORS[i][1]
        mult  *= (1 / chance) * 0.95   # 5% house edge per floor
    return round(mult, 2)


class TowerGame:
    def __init__(self, pid: int, bet: int):
        self.pid        = pid
        self.bet        = bet
        self.floor      = 0     # next floor to attempt (0-indexed)
        self.cleared    = 0     # floors successfully cleared
        self.alive      = True
        self.cashed_out = False

    @property
    def current_mult(self) -> float:
        if self.cleared == 0:
            return 1.0
        return floor_multiplier(self.cleared - 1)

    @property
    def next_mult(self) -> float:
        return floor_multiplier(self.floor)

    @property
    def winnings(self) -> int:
        return int(self.bet * self.current_mult)

    def attempt_floor(self) -> bool:
        chance = FLOORS[self.floor][1]
        won    = random.random() < chance
        if won:
            self.cleared += 1
            self.floor   += 1
        else:
            self.alive   = False
        return won

    @property
    def at_top(self) -> bool:
        return self.floor >= len(FLOORS)


class TowerView(discord.ui.View):
    def __init__(self, player: discord.Member, game: TowerGame):
        super().__init__(timeout=SESSION_TTL)
        self.player  = player
        self.game    = game
        self.message: Optional[discord.Message] = None
        self._last_result: Optional[str] = None

    def build_embed(self) -> discord.Embed:
        g = self.game
        if not g.alive:
            color = 0xe74c3c
            title = f"💀 Fell on {FLOORS[g.floor][0]}"
            desc  = f"Lost **🪙 {g.bet:,}**"
        elif g.cashed_out or g.at_top:
            color = 0x2ecc71
            title = "🏆 Tower Cleared!" if g.at_top else "💰 Cashed Out!"
            profit = g.winnings - g.bet
            desc   = f"Won **🪙 {g.winnings:,}** (+{profit:,})"
        else:
            color = 0x3498db
            title = "🗼  Tower Climb"
            profit = g.winnings - g.bet
            desc   = (
                f"Cash out: **🪙 {g.winnings:,}**"
                + (f" (+{profit:,})" if g.cleared > 0 else " (no floors cleared yet)")
            )

        e = discord.Embed(title=title, description=desc, color=color)

        # Floor progress
        lines = []
        for i, (name, chance, dot) in enumerate(FLOORS):
            if i < g.cleared:
                lines.append(f"✅ {name}")
            elif i == g.floor and g.alive and not g.cashed_out:
                lines.append(f"⚔️ **{name}**  ← ({int(chance*100)}%)")
            else:
                lines.append(f"{'💀' if not g.alive and i == g.floor else dot} {name}")

        e.add_field(name="Tower", value="\n".join(lines[:6]), inline=True)
        if len(lines) > 6:
            e.add_field(name="\u200b", value="\n".join(lines[6:]), inline=True)

        if not g.alive or g.cashed_out or g.at_top:
            pass
        else:
            e.add_field(
                name="Next floor multiplier",
                value=f"{g.next_mult:.2f}x  →  🪙 {int(g.bet * g.next_mult):,}",
                inline=False
            )

        e.set_footer(text=f"Bet: 🪙 {g.bet:,}  •  {self.player.display_name}")
        return e

    @discord.ui.button(label="⚔️ Fight!", style=discord.ButtonStyle.danger, row=0)
    async def fight_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your tower.", ephemeral=True)
        if not self.game.alive or self.game.cashed_out or self.game.at_top:
            return await interaction.response.defer()

        floor_name = FLOORS[self.game.floor][0]
        button.disabled           = True
        self.cashout_btn.disabled = True

        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"⚔️ Fighting {floor_name}…",
                color=0xf1c40f
            ),
            view=self
        )
        await asyncio.sleep(1.5)

        won = self.game.attempt_floor()

        if not won:
            # Dead — disable all
            for child in self.children:
                child.disabled = True
            # Bet already deducted at game start
            await interaction.edit_original_response(embed=self.build_embed(), view=self)
            self.stop()
        elif self.game.at_top:
            # Cleared all floors!
            self.game.cashed_out = True
            await casino_wallet.credit(self.player.id, self.game.winnings)
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(embed=self.build_embed(), view=self)
            self.stop()
        else:
            button.disabled           = False
            self.cashout_btn.disabled = False
            await interaction.edit_original_response(embed=self.build_embed(), view=self)

    @discord.ui.button(label="💰 Cash Out", style=discord.ButtonStyle.success, row=0)
    async def cashout_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your tower.", ephemeral=True)
        if not self.game.alive or self.game.cashed_out:
            return await interaction.response.defer()
        if self.game.cleared == 0:
            return await interaction.response.send_message(
                "Clear at least one floor before cashing out!", ephemeral=True)

        self.game.cashed_out = True
        await casino_wallet.credit(self.player.id, self.game.winnings)
        profit = self.game.winnings - self.game.bet
        sign   = "+" if profit >= 0 else ""
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        await interaction.followup.send(
            f"🏰 **{self.player.display_name}** cashed out on floor **{self.game.cleared}**!\n"
            f"Won: 🪙 {self.game.winnings:,}  |  Profit: {sign}🪙 {profit:,}"
        )
        self.stop()

    async def on_timeout(self):
        if self.game.alive and not self.game.cashed_out:
            # Forfeit — bet already deducted
            self.game.alive = False
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                e = self.build_embed()
                e.set_footer(text="Session timed out — bet forfeited")
                await self.message.edit(embed=e, view=self)
            except Exception:
                pass


class TowerCog(commands.Cog):
    def __init__(self, bot):
        self.bot     = bot
        self._active: set[int] = set()

    @commands.command(name="tower")
    async def tower(self, ctx: commands.Context, opponent: Optional[discord.Member] = None, bet: int = 0):
        """Climb the tower floor by floor — cash out or fall trying! Usage: ;tower <bet>"""
        pid = ctx.author.id
        if pid in self._active:
            return await ctx.send("❌ You have an active Tower session!")
        _max_bet = await casino_premium.get_max_bet(ctx.author.id)
        if not (MIN_BET <= bet <= _max_bet):
            return await ctx.send(f"❌ Bet must be 🪙 {MIN_BET:,}–{_max_bet:,}.")
        if not await casino_wallet.deduct(pid, bet):
            return await ctx.send("❌ Not enough casino coins.")

        self._active.add(pid)
        game = TowerGame(pid, bet)
        view = TowerView(ctx.author, game)
        view.message = await ctx.send(embed=view.build_embed(), view=view)
        await view.wait()
        self._active.discard(pid)


async def setup(bot):
    await bot.add_cog(TowerCog(bot))
