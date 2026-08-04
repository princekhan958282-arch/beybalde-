"""
plinko.py  —  🔻 Plinko

Drop a ball down 12 rows of pegs. Where it lands decides the multiplier.
Three risk profiles. RTP ≈ 95-96% on every profile.

Usage:  ;plinko <bet> [low|medium|high]   (alias: ;drop)
        ;plinko <bet>                     → opens a risk picker
"""

import asyncio
import random
from typing import Optional

import discord
from discord.ext import commands

from . import casino_premium, casino_wallet

MIN_BET = 10
ROWS    = 12
BUCKETS = ROWS + 1          # 13 landing slots

# Multiplier ladders — symmetric, edges pay big.
RISK = {
    "low": {
        "label":  "🟢 Low",
        "colour": 0x2ecc71,
        "mults":  [5.5, 2.5, 1.5, 1.25, 1.05, 0.95, 0.6, 0.95, 1.05, 1.25, 1.5, 2.5, 5.5],
    },
    "medium": {
        "label":  "🟡 Medium",
        "colour": 0xf1c40f,
        "mults":  [29, 9, 3.5, 1.65, 1.1, 0.65, 0.4, 0.65, 1.1, 1.65, 3.5, 9, 29],
    },
    "high": {
        "label":  "🔴 High",
        "colour": 0xe74c3c,
        "mults":  [220, 33, 6.5, 1.8, 0.6, 0.2, 0.1, 0.2, 0.6, 1.8, 6.5, 33, 220],
    },
}
RISK_ALIASES = {"low": "low", "l": "low", "med": "medium", "medium": "medium",
                "m": "medium", "high": "high", "h": "high"}


def _drop_path() -> tuple[int, list[int]]:
    """Simulate the ball. Returns (bucket_index, per-row offsets)."""
    pos  = 0
    path = []
    for _ in range(ROWS):
        step = random.getrandbits(1)   # 0 = left, 1 = right
        pos += step
        path.append(pos)
    return pos, path


def _render(path: list[int], upto: int) -> str:
    """ASCII pyramid of pegs with the ball drawn at row `upto`."""
    lines = []
    for r in range(ROWS + 1):
        pegs = ["·"] * (r + 1)
        if r == upto and r < len(path) + 1:
            idx = path[r - 1] if r > 0 else 0
            idx = min(idx, r)
            pegs[idx] = "🔴"
        pad = " " * (ROWS - r)
        lines.append(pad + " ".join(pegs))
    return "```\n" + "\n".join(lines) + "\n```"


def _bucket_bar(mults: list[float], landed: Optional[int] = None) -> str:
    cells = []
    for i, m in enumerate(mults):
        txt = f"{m:g}x"
        cells.append(f"**[{txt}]**" if i == landed else f"`{txt}`")
    # split into two lines so it stays readable on phones
    mid = (len(cells) + 1) // 2
    return " ".join(cells[:mid]) + "\n" + " ".join(cells[mid:])


class RiskSelect(discord.ui.Select):
    def __init__(self):
        opts = []
        for key, cfg in RISK.items():
            m = cfg["mults"]
            opts.append(discord.SelectOption(
                label=f"{cfg['label']}  —  max {m[0]:g}x",
                value=key,
                description=f"centre {m[ROWS // 2]:g}x  •  edges {m[0]:g}x",
            ))
        super().__init__(placeholder="Choose risk level…", options=opts)

    async def callback(self, interaction: discord.Interaction):
        view: "PlinkoSetupView" = self.view
        if interaction.user.id != view.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)
        view.risk    = self.values[0]
        view.started = True
        view.stop()
        for c in view.children:
            c.disabled = True
        await interaction.response.edit_message(view=view)
        msg = await interaction.followup.send(
            embed=discord.Embed(title="🔻  Plinko", description="Dropping…",
                                color=RISK[view.risk]["colour"]),
            wait=True)
        await view.cog._run_drop(msg, view.player, view.bet, view.risk)


class PlinkoSetupView(discord.ui.View):
    def __init__(self, cog, player: discord.Member, bet: int):
        super().__init__(timeout=45)
        self.cog     = cog
        self.player  = player
        self.bet     = bet
        self.risk: Optional[str] = None
        self.started = False
        self.add_item(RiskSelect())

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True


class PlinkoResultView(discord.ui.View):
    def __init__(self, cog, player: discord.Member, bet: int, risk: str):
        super().__init__(timeout=60)
        self.cog     = cog
        self.player  = player
        self.bet     = bet
        self.risk    = risk
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="🔻  Drop Again", style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your game.", ephemeral=True)

        max_bet = await casino_premium.get_max_bet(self.player.id)
        if self.bet > max_bet:
            return await interaction.response.send_message(
                f"Your max bet is now 🪙 {max_bet:,}.", ephemeral=True)
        if not await casino_wallet.deduct(self.player.id, self.bet):
            return await interaction.response.send_message(
                "❌ Not enough casino coins.", ephemeral=True)

        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        msg = await interaction.followup.send(
            embed=discord.Embed(title="🔻  Plinko", description="Dropping…",
                                color=RISK[self.risk]["colour"]),
            wait=True)
        await self.cog._run_drop(msg, self.player, self.bet, self.risk)

    @discord.ui.button(label="📜  Paytable", style=discord.ButtonStyle.secondary)
    async def paytable(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=_paytable_embed(), ephemeral=True)

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


def _paytable_embed() -> discord.Embed:
    e = discord.Embed(title="🔻  Plinko Paytable", color=0x9b59b6)
    for key, cfg in RISK.items():
        e.add_field(name=cfg["label"], value=_bucket_bar(cfg["mults"]), inline=False)
    e.set_footer(text=f"{ROWS} rows  •  {BUCKETS} slots  •  RTP ≈ 95-96%")
    return e


class PlinkoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active: set[int] = set()

    async def _run_drop(self, message: discord.Message, player: discord.Member,
                        bet: int, risk: str):
        """Animate the drop and pay out. Bet must already be deducted."""
        cfg   = RISK[risk]
        mults = cfg["mults"]
        try:
            bucket, path = _drop_path()

            # Animate a few frames (not every row — keeps rate limits happy)
            for row in (3, 7, 11):
                e = discord.Embed(title="🔻  Plinko", color=cfg["colour"])
                e.description = _render(path, row)
                e.add_field(name="Bet",  value=f"🪙 {bet:,}",     inline=True)
                e.add_field(name="Risk", value=cfg["label"],      inline=True)
                try:
                    await message.edit(embed=e)
                except discord.DiscordException:
                    break
                await asyncio.sleep(0.6)

            mult   = mults[bucket]
            payout = int(bet * mult)
            profit = payout - bet
            if payout > 0:
                await casino_wallet.credit(player.id, payout)
            bal = await casino_wallet.get_balance(player.id)

            if mult >= 10:
                title, colour = "💎  HUGE HIT!", 0x1abc9c
            elif profit > 0:
                title, colour = "🎉  Win!", 0x2ecc71
            elif profit == 0:
                title, colour = "➖  Push", 0x95a5a6
            else:
                title, colour = "📉  Loss", 0xe74c3c

            e = discord.Embed(title=f"🔻  Plinko  —  {title}", color=colour)
            e.description = _render(path, ROWS) + "\n" + _bucket_bar(mults, bucket)
            e.add_field(name="Landed",  value=f"**{mult:g}x**",   inline=True)
            e.add_field(name="Risk",    value=cfg["label"],       inline=True)
            e.add_field(name="Bet",     value=f"🪙 {bet:,}",      inline=True)
            e.add_field(name="Payout",  value=f"🪙 {payout:,}",   inline=True)
            e.add_field(name="Profit",
                        value=f"{'+' if profit >= 0 else ''}🪙 {profit:,}", inline=True)
            e.add_field(name="Balance", value=f"🪙 {bal:,}",      inline=True)
            e.set_footer(text=f"{player.display_name}  •  slot {bucket + 1}/{BUCKETS}")

            view = PlinkoResultView(self, player, bet, risk)
            try:
                await message.edit(embed=e, view=view)
                view.message = message
            except discord.DiscordException:
                pass
        finally:
            self._active.discard(player.id)

    @commands.command(name="plinko", aliases=["drop"])
    async def plinko(self, ctx: commands.Context, bet: int = 0, risk: str = None):
        """🔻 Drop a ball through the pegs. Usage: ;plinko <bet> [low|medium|high]"""
        pid = ctx.author.id

        if pid in self._active:
            return await ctx.send("❌ You already have a ball in play!")

        max_bet = await casino_premium.get_max_bet(pid)
        if not (MIN_BET <= bet <= max_bet):
            return await ctx.send(f"❌ Bet must be 🪙 {MIN_BET:,}–{max_bet:,}.")

        risk_key = RISK_ALIASES.get((risk or "").lower())
        if risk is not None and risk_key is None:
            return await ctx.send("❌ Risk must be `low`, `medium` or `high`.")

        if not await casino_wallet.deduct(pid, bet):
            return await ctx.send("❌ Not enough casino coins.")

        self._active.add(pid)

        if risk_key:
            msg = await ctx.send(embed=discord.Embed(
                title="🔻  Plinko", description="Dropping…",
                color=RISK[risk_key]["colour"]))
            return await self._run_drop(msg, ctx.author, bet, risk_key)

        # No risk given — show the picker
        view = PlinkoSetupView(self, ctx.author, bet)
        e = discord.Embed(title="🔻  Plinko", color=0x9b59b6,
                          description=f"Bet: **🪙 {bet:,}**\nPick a risk level to drop.")
        e.add_field(name="Rows", value=str(ROWS), inline=True)
        e.add_field(name="Slots", value=str(BUCKETS), inline=True)
        msg = await ctx.send(embed=e, view=view)
        await view.wait()

        if not view.started:
            await casino_wallet.credit(pid, bet)
            self._active.discard(pid)
            try:
                await msg.edit(content="⏰ Timed out — bet refunded.", view=view)
            except Exception:
                pass

    @commands.command(name="plinkopaytable", aliases=["plinkoinfo"])
    async def plinko_paytable(self, ctx: commands.Context):
        """Show the Plinko paytable."""
        await ctx.send(embed=_paytable_embed())


async def setup(bot: commands.Bot):
    await bot.add_cog(PlinkoCog(bot))
