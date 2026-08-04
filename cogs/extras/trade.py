"""
cogs/extras/trade.py
--------------------
Player-to-player Beyblade trading.

  ;trade @user "Your Blade" "Their Blade"
     Offer a 1-for-1 blade swap. The target must click Accept within 60s.
     Both blades are verified in each player's inventory at ACCEPT time
     (not offer time) so nothing can be sold/traded away mid-offer.

Safety:
  • Only the target can accept / decline.
  • Inventories re-checked at accept time.
  • If a traded blade was equipped, it is auto-unequipped.
  • Every completed trade appended to data/trade_log.json.
"""

from __future__ import annotations

import json
import os
import threading
import time

import discord
from discord.ext import commands

from utils.database import get_user, update_user

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOG_PATH = os.path.join(_PROJECT_ROOT, "data", "trade_log.json")
_log_lock = threading.Lock()


def _find_blade(inventory: list, name: str) -> str | None:
    """Case-insensitive inventory lookup. Returns the stored name or None."""
    low = name.lower().strip()
    for item in inventory:
        item_name = item.get("name") if isinstance(item, dict) else item
        if str(item_name).lower() == low:
            return item
    return None


def _log_trade(entry: dict) -> None:
    try:
        with _log_lock:
            data = []
            if os.path.exists(_LOG_PATH):
                try:
                    with open(_LOG_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = []
            data.append(entry)
            tmp = _LOG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, _LOG_PATH)
    except Exception:
        pass


class _TradeView(discord.ui.View):
    def __init__(self, initiator: discord.Member, target: discord.Member):
        super().__init__(timeout=60)
        self.initiator = initiator
        self.target    = target
        self.accepted: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message(
                "Only the trade target can respond to this offer.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Accept Trade", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _):
        self.accepted = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, _):
        self.accepted = False
        await interaction.response.defer()
        self.stop()


class TradeCog(commands.Cog, name="Trading"):
    """1-for-1 Beyblade trading between players."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # users currently in a pending trade — prevents double-offer races
        self._busy: set[int] = set()

    @commands.guild_only()
    @commands.command(
        name="trade",
        brief="Trade blades with another player 🤝",
        help='Usage: ;trade @user "Your Blade Name" "Their Blade Name"\n'
             "Offers a 1-for-1 swap. The other player must Accept within 60s.",
    )
    async def trade(self, ctx: commands.Context, target: discord.Member,
                    my_blade: str, their_blade: str) -> None:
        if target.bot:
            return await ctx.send("❌ You can't trade with a bot!")
        if target.id == ctx.author.id:
            return await ctx.send("❌ You can't trade with yourself!")
        if ctx.author.id in self._busy or target.id in self._busy:
            return await ctx.send("⚠️ One of you already has a pending trade.")

        # Preliminary ownership check (re-verified at accept time)
        a_prof = get_user(ctx.author.id)
        b_prof = get_user(target.id)
        a_item = _find_blade(a_prof.get("inventory", []), my_blade)
        b_item = _find_blade(b_prof.get("inventory", []), their_blade)
        if a_item is None:
            return await ctx.send(f"❌ You don't own **{my_blade}**.")
        if b_item is None:
            return await ctx.send(f"❌ {target.display_name} doesn't own **{their_blade}**.")

        a_name = a_item.get("name") if isinstance(a_item, dict) else a_item
        b_name = b_item.get("name") if isinstance(b_item, dict) else b_item

        self._busy.update({ctx.author.id, target.id})
        try:
            embed = discord.Embed(
                title="🤝 Trade Offer",
                description=(
                    f"{ctx.author.mention} offers **{a_name}**\n"
                    f"in exchange for {target.mention}'s **{b_name}**\n\n"
                    f"{target.mention}, do you accept? (60s)"
                ),
                color=discord.Color.blurple(),
            )
            view = _TradeView(ctx.author, target)
            msg  = await ctx.send(embed=embed, view=view)
            await view.wait()
            await msg.edit(view=None)

            if not view.accepted:
                return await ctx.send("❌ Trade declined or timed out.")

            # ── Re-verify + execute atomically-ish (sequential profile writes) ─
            a_prof = get_user(ctx.author.id)
            b_prof = get_user(target.id)
            a_item = _find_blade(a_prof.get("inventory", []), a_name)
            b_item = _find_blade(b_prof.get("inventory", []), b_name)
            if a_item is None or b_item is None:
                return await ctx.send(
                    "❌ Trade failed — one of the blades is no longer in the "
                    "owner's inventory."
                )

            a_prof["inventory"].remove(a_item)
            b_prof["inventory"].remove(b_item)
            a_prof["inventory"].append(b_item)
            b_prof["inventory"].append(a_item)

            # Auto-unequip if the traded blade was active
            if str(a_prof.get("active_beyblade", "")).lower() == str(a_name).lower():
                a_prof["active_beyblade"] = None
            if str(b_prof.get("active_beyblade", "")).lower() == str(b_name).lower():
                b_prof["active_beyblade"] = None

            update_user(ctx.author.id, a_prof)
            update_user(target.id, b_prof)

            _log_trade({
                "ts":       int(time.time()),
                "guild":    ctx.guild.id if ctx.guild else None,
                "a_id":     ctx.author.id, "a_gave": str(a_name),
                "b_id":     target.id,     "b_gave": str(b_name),
            })

            await ctx.send(embed=discord.Embed(
                title="✅ Trade Complete!",
                description=(
                    f"{ctx.author.mention} received **{b_name}**\n"
                    f"{target.mention} received **{a_name}**\n\n"
                    f"*(If a traded blade was equipped, it has been unequipped — "
                    f"use `;equip` to set a new active blade.)*"
                ),
                color=discord.Color.green(),
            ))
        finally:
            self._busy.discard(ctx.author.id)
            self._busy.discard(target.id)

    @trade.error
    async def trade_error(self, ctx: commands.Context, error) -> None:
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send(
                '❌ Usage: `;trade @user "Your Blade" "Their Blade"`\n'
                "Use quotes around blade names with spaces."
            )
        else:
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TradeCog(bot))
