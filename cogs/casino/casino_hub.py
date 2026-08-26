"""
casino_hub.py  —  Casino wallet commands + daily bonus + leaderboard
/casino balance   — check your coins
/casino daily     — claim 500 free coins once per day
/casino give      — admin: give coins to a player
/casino leaderboard — top 10 richest casino players
"""

import discord
from discord.ext import commands
from . import casino_wallet
from . import casino_premium
from utils import database

ADMIN_ROLE = "Casino Admin"

# ── Exchange constants ────────────────────────────────────────────────────────
BEYCOIN_PER_EXCHANGE = 100   # Beycoins spent per exchange unit
CASINO_PER_EXCHANGE  = 60    # casino coins received per 100 Beycoins
CASINO_TAX           = casino_premium.BASE_EXCHANGE_TAX  # 10% base sell tax
# Buy:  100 Beycoins → 60 casino coins
# Sell: 60 casino coins → 90 Beycoins (10% tax)
# Premium passes lower the sell tax: Pro 6% | Elite 3% | Legend 0%


class CasinoHub(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # The `/casino` group lived here — seven subcommands, and Discord lists
    # them FLAT in the picker. v1.14 replaced it with one `/casino` command
    # opening a panel; see `cogs/ui/panels.py:CasinoSpec`, which invokes the
    # prefix commands below and hands "Play a game" to the casino's own
    # CasinoLobbyView rather than rebuilding it.

    # `/casino balance`, `daily`, `leaderboard` and `exchange` lived here as
    # four more flat picker lines, each a second implementation of the prefix
    # command directly below. The panel invokes those prefix commands, so
    # there is now one implementation of each rather than two.

    # ── Prefix wallet commands ────────────────────────────────────────────────
    @commands.command(name="casinobal", aliases=["cbal", "casinobalance"])
    async def prefix_balance(self, ctx: commands.Context):
        """Check your casino coin balance."""
        bal = await casino_wallet.get_balance(ctx.author.id)
        e = discord.Embed(
            title="🎰 Casino Wallet",
            description=f"{ctx.author.mention}\n**🪙 {bal:,}** casino coins",
            color=0xf1c40f
        )
        await ctx.send(embed=e)

    @commands.command(name="casinodaily", aliases=["cdaily"])
    async def prefix_daily(self, ctx: commands.Context):
        """Claim your daily 500 casino coins."""
        claimed, amount = await casino_wallet.claim_daily(ctx.author.id)
        if claimed:
            bal = await casino_wallet.get_balance(ctx.author.id)
            e = discord.Embed(
                title="🎁 Daily Bonus!",
                description=f"You claimed **🪙 {amount:,}** casino coins!\nBalance: 🪙 {bal:,}",
                color=0x2ecc71
            )
        else:
            e = discord.Embed(
                title="⏰ Already Claimed",
                description="Come back tomorrow for your next bonus!",
                color=0xe74c3c
            )
        await ctx.send(embed=e)

    @commands.command(name="casinoleaderboard", aliases=["clb", "casinolb"])
    async def prefix_leaderboard(self, ctx: commands.Context):
        """Top 10 casino coin holders."""
        import json
        async with casino_wallet._lock:
            try:
                with open(casino_wallet.WALLET_FILE) as f:
                    data = json.load(f)
            except Exception:
                data = {}

        sorted_players = sorted(
            data.items(), key=lambda x: x[1].get("balance", 0), reverse=True
        )[:10]

        if not sorted_players:
            return await ctx.send("No casino data yet.")

        medals = ["🥇","🥈","🥉"] + ["🏅"]*7
        lines = []
        for i, (uid, d) in enumerate(sorted_players):
            member = ctx.guild.get_member(int(uid))
            name   = member.display_name if member else f"User {uid}"
            lines.append(f"{medals[i]} {name}  —  🪙 {d.get('balance',0):,}")

        e = discord.Embed(
            title="🎰 Casino Leaderboard",
            description="\n".join(lines),
            color=0xf1c40f
        )
        await ctx.send(embed=e)

    @commands.command(name="casinogive", aliases=["cgive"])
    @commands.has_permissions(administrator=True)
    async def prefix_give(self, ctx: commands.Context, player: discord.Member, amount: int):
        """[Admin] Give casino coins to a player."""
        if amount <= 0:
            return await ctx.send("❌ Amount must be positive.")
        await casino_wallet.credit(player.id, amount)
        bal = await casino_wallet.get_balance(player.id)
        await ctx.send(f"✅ Gave 🪙 {amount:,} to {player.mention}. Their balance: 🪙 {bal:,}")

    @commands.command(name="casinotake", aliases=["ctake"])
    @commands.has_permissions(administrator=True)
    async def prefix_take(self, ctx: commands.Context, player: discord.Member, amount: int):
        """[Admin] Remove casino coins from a player."""
        bal    = await casino_wallet.get_balance(player.id)
        actual = min(amount, bal)
        await casino_wallet.set_balance(player.id, bal - actual)
        await ctx.send(f"✅ Removed 🪙 {actual:,} from {player.mention}. Their balance: 🪙 {bal-actual:,}")

    # ── Prefix exchange command ───────────────────────────────────────────────
    @commands.command(name="casinoexchange", aliases=["cexchange", "casino_exchange"])
    async def prefix_exchange(self, ctx: commands.Context, direction: str = None, amount: int = None):
        """Exchange Beycoins ↔ casino coins.
        Usage:
          ;casinoexchange buy <beycoins>   — spend Beycoins, get casino coins
          ;casinoexchange sell <casino>    — sell casino coins, get Beycoins
        Rate: 100 Beycoins = 60 casino coins (10% tax on sell, 0% with Legend pass)
        """
        if direction is None or amount is None or direction.lower() not in ("buy", "sell"):
            e = discord.Embed(
                title="💱 Casino Exchange",
                description=(
                    "**Buy:**  `;casinoexchange buy <beycoins>`\n"
                    "→ Spend Beycoins, receive casino coins\n"
                    "→ Rate: **100 Beycoins = 60 casino coins**\n\n"
                    "**Sell:** `;casinoexchange sell <casino coins>`\n"
                    "→ Sell casino coins, receive Beycoins\n"
                    f"→ **{casino_premium.BASE_EXCHANGE_TAX:.0%}** tax applied on return\n"
                    "→ Premium passes lower it: Pro **6%** | Elite **3%** | Legend **0%**\n\n"
                    "*Aliases: `;cexchange`, `;casino_exchange`*"
                ),
                color=0x9b59b6
            )
            return await ctx.send(embed=e)

        if amount <= 0:
            return await ctx.send("❌ Amount must be positive.")

        uid = ctx.author.id
        direction = direction.lower()

        if direction == "buy":
            if amount % BEYCOIN_PER_EXCHANGE != 0:
                return await ctx.send(f"❌ Beycoins must be a multiple of {BEYCOIN_PER_EXCHANGE}. (e.g. 100, 200, 60000)")

            casino_gain = (amount // BEYCOIN_PER_EXCHANGE) * CASINO_PER_EXCHANGE
            profile = await database.get_user(uid)
            if profile.get("coins", 0) < amount:
                return await ctx.send(
                    f"❌ Not enough Beycoins. Need 🪙 {amount:,} but you have 🪙 {profile.get('coins', 0):,}.")

            profile["coins"] -= amount
            await database.update_user(uid, profile)
            await casino_wallet.credit(uid, casino_gain)

            casino_bal = await casino_wallet.get_balance(uid)
            e = discord.Embed(title="💱 Exchange Complete", color=0x2ecc71)
            e.add_field(name="Spent",    value=f"🪙 {amount:,} Beycoins",          inline=True)
            e.add_field(name="Received", value=f"🎰 {casino_gain:,} casino coins", inline=True)
            e.add_field(name="Rate",     value="100 Beycoins = 60 casino coins",   inline=False)
            e.add_field(name="Casino balance",  value=f"🎰 {casino_bal:,}",        inline=True)
            e.add_field(name="Beycoin balance", value=f"🪙 {profile['coins']:,}",  inline=True)
            await ctx.send(embed=e)

        else:  # sell
            casino_bal = await casino_wallet.get_balance(uid)
            if casino_bal < amount:
                return await ctx.send(
                    f"❌ Not enough casino coins. Have 🎰 {casino_bal:,}, need 🎰 {amount:,}.")

            tax_rate       = await casino_premium.get_exchange_tax(uid)
            beycoin_gross  = (amount // CASINO_PER_EXCHANGE) * BEYCOIN_PER_EXCHANGE
            tax_taken      = int(beycoin_gross * tax_rate)
            beycoin_return = beycoin_gross - tax_taken

            await casino_wallet.deduct(uid, amount)
            profile = await database.get_user(uid)
            profile["coins"] += beycoin_return
            await database.update_user(uid, profile)

            prem = await casino_premium.get_premium(uid)
            tax_label = f"Tax ({tax_rate:.0%})"
            tax_value = (f"🪙 {tax_taken:,} Beycoins" if tax_taken else "🪙 0 — **tax free!**")
            if prem:
                tax_value += f"\n{casino_premium.PACKS[prem['key']]['display']} pass active"
            else:
                tax_value += "\n*Buy a premium pass to cut this down to 0%.*"

            casino_bal_new = await casino_wallet.get_balance(uid)
            e = discord.Embed(title="💱 Exchange Complete", color=0x2ecc71)
            e.add_field(name="Sold",      value=f"🎰 {amount:,} casino coins",     inline=True)
            e.add_field(name="Received",  value=f"🪙 {beycoin_return:,} Beycoins", inline=True)
            e.add_field(name=tax_label,   value=tax_value,                        inline=False)
            e.add_field(name="Casino balance",  value=f"🎰 {casino_bal_new:,}",    inline=True)
            e.add_field(name="Beycoin balance", value=f"🪙 {profile['coins']:,}",  inline=True)
            await ctx.send(embed=e)

    # `/casino menu`, `/casino play <game>` and `/casino games` lived here,
    # along with a GAMES table mapping each slash value to its prefix command.
    # All three are covered by the panel's "Play a game", which opens the
    # casino's own CasinoLobbyView — a game select, a bet modal and wallet
    # buttons that already existed. A second game table here was one more
    # thing to remember to update whenever a game is added.


async def setup(bot):
    await bot.add_cog(CasinoHub(bot))
