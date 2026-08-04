"""
keno.py  —  🔢 Keno

Pick 1-10 numbers from a 40-number board. The house draws 10.
The more of your picks that hit, the bigger the payout.
RTP ≈ 91-95% depending on how many you pick.

Usage:  ;keno <bet>            → opens the number board
        ;keno <bet> 3 7 12 29  → quick-pick specific numbers
        ;keno <bet> random 5   → auto-pick 5 random numbers
"""

import asyncio
import random
from math import comb
from typing import Optional

import discord
from discord.ext import commands

from . import casino_premium, casino_wallet

MIN_BET     = 10
BOARD_SIZE  = 40
DRAW_COUNT  = 10
MAX_PICKS   = 10

# picks -> {hits: multiplier}
PAYTABLE: dict[int, dict[int, float]] = {
    1:  {1: 3.8},
    2:  {2: 16.5},
    3:  {2: 2.5, 3: 50},
    4:  {2: 1.2, 3: 8, 4: 165},
    5:  {3: 4.5, 4: 33, 5: 700},
    6:  {3: 2.8, 4: 11, 5: 105, 6: 2000},
    7:  {3: 1.5, 4: 6.5, 5: 42, 6: 300, 7: 4500},
    8:  {4: 6, 5: 22, 6: 120, 7: 1100, 8: 11000},
    9:  {4: 4, 5: 11, 6: 47, 7: 300, 8: 2500, 9: 22000},
    10: {4: 2.5, 5: 7, 6: 25, 7: 100, 8: 700, 9: 5000, 10: 40000},
}


def _hit_chance(picks: int, hits: int) -> float:
    return (comb(picks, hits) * comb(BOARD_SIZE - picks, DRAW_COUNT - hits)
            / comb(BOARD_SIZE, DRAW_COUNT))


def _paytable_text(picks: int) -> str:
    table = PAYTABLE[picks]
    lines = []
    for hits in sorted(table, reverse=True):
        chance = _hit_chance(picks, hits) * 100
        lines.append(f"`{hits}/{picks} hits`  →  **{table[hits]:g}x**   ({chance:.3f}%)")
    return "\n".join(lines)


def _board_text(picked: set[int], drawn: Optional[set[int]] = None) -> str:
    """8 columns x 5 rows board."""
    rows = []
    for r in range(5):
        cells = []
        for c in range(8):
            n = r * 8 + c + 1
            if drawn is not None and n in drawn and n in picked:
                cells.append("🟩")      # hit
            elif drawn is not None and n in drawn:
                cells.append("🟦")      # drawn, not picked
            elif n in picked:
                cells.append("🟨")      # picked, missed
            else:
                cells.append("⬛")
        rows.append("".join(cells))
    legend = f"`{'  '.join(str(r*8+1).rjust(2) + '-' + str(r*8+8) for r in range(5))}`"
    return "\n".join(rows) + "\n" + legend


class NumberSelect(discord.ui.Select):
    """One dropdown per block of numbers (Discord caps options at 25)."""

    def __init__(self, lo: int, hi: int, row: int, picked: set[int]):
        self.lo, self.hi = lo, hi
        opts = [
            discord.SelectOption(label=str(n), value=str(n), default=(n in picked))
            for n in range(lo, hi + 1)
        ]
        super().__init__(
            placeholder=f"Numbers {lo}–{hi}",
            options=opts,
            min_values=0,
            max_values=min(MAX_PICKS, len(opts)),
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        view: "KenoBoardView" = self.view
        if interaction.user.id != view.player.id:
            return await interaction.response.send_message("Not your board.", ephemeral=True)

        chosen = {int(v) for v in self.values}
        # Replace only this block's selections
        view.picked = {n for n in view.picked if not (self.lo <= n <= self.hi)} | chosen

        if len(view.picked) > MAX_PICKS:
            view.picked = set(sorted(view.picked)[:MAX_PICKS])
            await interaction.response.edit_message(embed=view.build_embed(), view=view)
            return await interaction.followup.send(
                f"⚠️ Max {MAX_PICKS} numbers — extras were dropped.", ephemeral=True)

        view.refresh()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class KenoBoardView(discord.ui.View):
    def __init__(self, cog, player: discord.Member, bet: int):
        super().__init__(timeout=120)
        self.cog     = cog
        self.player  = player
        self.bet     = bet
        self.picked: set[int] = set()
        self.started = False
        self.message: Optional[discord.Message] = None
        self.refresh()

    def refresh(self):
        """Rebuild the dropdowns so current picks show as selected."""
        for child in list(self.children):
            if isinstance(child, NumberSelect):
                self.remove_item(child)
        blocks = [(1, 20, 0), (21, 40, 1)]
        for lo, hi, row in blocks:
            self.add_item(NumberSelect(lo, hi, row, self.picked))
        self.play_btn.disabled = len(self.picked) == 0

    def build_embed(self) -> discord.Embed:
        n = len(self.picked)
        e = discord.Embed(title="🔢  Keno", color=0x9b59b6)
        e.description = _board_text(self.picked)
        e.add_field(name="Bet", value=f"🪙 {self.bet:,}", inline=True)
        e.add_field(name="Picked",
                    value=(", ".join(map(str, sorted(self.picked))) if self.picked else "none"),
                    inline=True)
        if n:
            top = max(PAYTABLE[n])
            e.add_field(name="Top prize", value=f"**{PAYTABLE[n][top]:g}x** ({top}/{n})",
                        inline=True)
        e.set_footer(text=f"{self.player.display_name}  •  pick 1–{MAX_PICKS} numbers, "
                          f"{DRAW_COUNT} get drawn from {BOARD_SIZE}")
        return e

    @discord.ui.button(label="🎲  Random", style=discord.ButtonStyle.secondary, row=2)
    async def random_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your board.", ephemeral=True)
        count = len(self.picked) or 5
        self.picked = set(random.sample(range(1, BOARD_SIZE + 1), count))
        self.refresh()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="🧹  Clear", style=discord.ButtonStyle.secondary, row=2)
    async def clear_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your board.", ephemeral=True)
        self.picked = set()
        self.refresh()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶  Play", style=discord.ButtonStyle.green, row=2, disabled=True)
    async def play_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your board.", ephemeral=True)
        if not self.picked:
            return await interaction.response.send_message("Pick at least one number!",
                                                           ephemeral=True)
        self.started = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        await self.cog._resolve(interaction.message, self.player, self.bet, set(self.picked))

    @discord.ui.button(label="📜  Paytable", style=discord.ButtonStyle.secondary, row=2)
    async def paytable_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        n = len(self.picked) or 5
        e = discord.Embed(title=f"🔢  Keno Paytable — {n} pick(s)",
                          description=_paytable_text(n), color=0x9b59b6)
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if not self.started:
            await casino_wallet.credit(self.player.id, self.bet)
            self.cog._active.discard(self.player.id)
            if self.message:
                try:
                    await self.message.edit(content="⏰ Timed out — bet refunded.", view=self)
                except Exception:
                    pass


class KenoResultView(discord.ui.View):
    def __init__(self, cog, player: discord.Member, bet: int, picks: set[int]):
        super().__init__(timeout=60)
        self.cog    = cog
        self.player = player
        self.bet    = bet
        self.picks  = picks
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="🔁  Same Numbers", style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        max_bet = await casino_premium.get_max_bet(self.player.id)
        if self.bet > max_bet:
            return await interaction.response.send_message(
                f"Your max bet is now 🪙 {max_bet:,}.", ephemeral=True)
        if self.player.id in self.cog._active:
            return await interaction.response.send_message("Finish your current draw first.",
                                                           ephemeral=True)
        if not await casino_wallet.deduct(self.player.id, self.bet):
            return await interaction.response.send_message("❌ Not enough casino coins.",
                                                           ephemeral=True)
        self.cog._active.add(self.player.id)
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        msg = await interaction.followup.send(
            embed=discord.Embed(title="🔢  Keno", description="Drawing…", color=0x9b59b6),
            wait=True)
        await self.cog._resolve(msg, self.player, self.bet, set(self.picks))

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class KenoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active: set[int] = set()

    async def _resolve(self, message: discord.Message, player: discord.Member,
                       bet: int, picks: set[int]):
        """Draw, animate, pay. Bet must already be deducted."""
        try:
            drawn_list = random.sample(range(1, BOARD_SIZE + 1), DRAW_COUNT)

            # Reveal in three chunks for a bit of tension
            revealed: set[int] = set()
            for chunk in (drawn_list[:4], drawn_list[4:7], drawn_list[7:]):
                revealed |= set(chunk)
                e = discord.Embed(title="🔢  Keno — Drawing…", color=0x3498db)
                e.description = _board_text(picks, revealed)
                e.add_field(name="Drawn", value=f"{len(revealed)}/{DRAW_COUNT}", inline=True)
                e.add_field(name="Hits so far",
                            value=str(len(picks & revealed)), inline=True)
                try:
                    await message.edit(embed=e, view=None)
                except discord.DiscordException:
                    break
                await asyncio.sleep(0.9)

            drawn  = set(drawn_list)
            hits   = picks & drawn
            n      = len(picks)
            mult   = PAYTABLE[n].get(len(hits), 0)
            payout = int(bet * mult)
            profit = payout - bet

            if payout > 0:
                await casino_wallet.credit(player.id, payout)
            bal = await casino_wallet.get_balance(player.id)

            if mult >= 50:
                title, colour = "💎  MASSIVE HIT!", 0x1abc9c
            elif profit > 0:
                title, colour = "🎉  Winner!", 0x2ecc71
            elif profit == 0:
                title, colour = "➖  Push", 0x95a5a6
            else:
                title, colour = "📉  No luck", 0xe74c3c

            e = discord.Embed(title=f"🔢  Keno  —  {title}", color=colour)
            e.description = _board_text(picks, drawn)
            e.add_field(name="Your picks",
                        value=", ".join(map(str, sorted(picks))), inline=False)
            e.add_field(name="Drawn",
                        value=", ".join(map(str, sorted(drawn))), inline=False)
            e.add_field(name="Hits", value=f"**{len(hits)}/{n}**", inline=True)
            e.add_field(name="Multiplier", value=f"**{mult:g}x**", inline=True)
            e.add_field(name="Payout", value=f"🪙 {payout:,}", inline=True)
            e.add_field(name="Profit",
                        value=f"{'+' if profit >= 0 else ''}🪙 {profit:,}", inline=True)
            e.add_field(name="Balance", value=f"🪙 {bal:,}", inline=True)
            e.set_footer(text="🟩 hit   🟦 drawn   🟨 your miss")

            view = KenoResultView(self, player, bet, picks)
            try:
                await message.edit(embed=e, view=view)
                view.message = message
            except discord.DiscordException:
                pass
        finally:
            self._active.discard(player.id)

    @commands.command(name="keno")
    async def keno(self, ctx: commands.Context, bet: int = 0, *args: str):
        """🔢 Pick numbers, the house draws 10. Usage: ;keno <bet> [numbers…]"""
        pid = ctx.author.id

        if pid in self._active:
            return await ctx.send("❌ You already have a Keno card in play!")

        max_bet = await casino_premium.get_max_bet(pid)
        if not (MIN_BET <= bet <= max_bet):
            return await ctx.send(f"❌ Bet must be 🪙 {MIN_BET:,}–{max_bet:,}.")

        # ── Parse optional quick-pick ─────────────────────────────────────────
        picks: Optional[set[int]] = None
        if args:
            first = args[0].lower()
            if first in ("random", "rand", "r", "quick", "qp"):
                count = 5
                if len(args) > 1 and args[1].isdigit():
                    count = int(args[1])
                if not (1 <= count <= MAX_PICKS):
                    return await ctx.send(f"❌ Pick count must be 1–{MAX_PICKS}.")
                picks = set(random.sample(range(1, BOARD_SIZE + 1), count))
            else:
                nums = set()
                for a in args:
                    if not a.isdigit():
                        return await ctx.send(
                            f"❌ `{a}` isn't a number. Use `;keno {bet} 3 7 12` "
                            f"or `;keno {bet} random 5`.")
                    v = int(a)
                    if not (1 <= v <= BOARD_SIZE):
                        return await ctx.send(f"❌ Numbers must be 1–{BOARD_SIZE}.")
                    nums.add(v)
                if not (1 <= len(nums) <= MAX_PICKS):
                    return await ctx.send(f"❌ Pick between 1 and {MAX_PICKS} numbers.")
                picks = nums

        if not await casino_wallet.deduct(pid, bet):
            return await ctx.send("❌ Not enough casino coins.")

        self._active.add(pid)

        if picks:
            msg = await ctx.send(embed=discord.Embed(
                title="🔢  Keno", description="Drawing…", color=0x9b59b6))
            return await self._resolve(msg, ctx.author, bet, picks)

        view = KenoBoardView(self, ctx.author, bet)
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @commands.command(name="kenopaytable", aliases=["kenoinfo"])
    async def keno_paytable(self, ctx: commands.Context, picks: int = 5):
        """Show the Keno paytable for a given pick count."""
        if picks not in PAYTABLE:
            return await ctx.send(f"❌ Pick count must be 1–{MAX_PICKS}.")
        e = discord.Embed(title=f"🔢  Keno Paytable — {picks} pick(s)",
                          description=_paytable_text(picks), color=0x9b59b6)
        e.set_footer(text=f"{DRAW_COUNT} drawn from {BOARD_SIZE}  •  ;kenopaytable <picks>")
        await ctx.send(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(KenoCog(bot))
