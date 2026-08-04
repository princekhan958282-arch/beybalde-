"""
casino_hub.py  —  Casino wallet commands + daily bonus + leaderboard
/casino balance   — check your coins
/casino daily     — claim 500 free coins once per day
/casino give      — admin: give coins to a player
/casino leaderboard — top 10 richest casino players
"""
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands
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

    casino = app_commands.Group(name="casino", description="Casino wallet & utilities")

    @casino.command(name="balance", description="Check your casino coin balance.")
    async def balance(self, interaction: discord.Interaction):
        bal = await casino_wallet.get_balance(interaction.user.id)
        e   = discord.Embed(
            title="🎰 Casino Wallet",
            description=f"{interaction.user.mention}\n**🪙 {bal:,}** casino coins",
            color=0xf1c40f
        )
        await interaction.response.send_message(embed=e, ephemeral=True)

    @casino.command(name="daily", description="Claim your daily 500 casino coins.")
    async def daily(self, interaction: discord.Interaction):
        claimed, amount = await casino_wallet.claim_daily(interaction.user.id)
        if claimed:
            bal = await casino_wallet.get_balance(interaction.user.id)
            e   = discord.Embed(
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
        await interaction.response.send_message(embed=e, ephemeral=True)

    @casino.command(name="leaderboard", description="Top 10 casino coin holders.")
    async def leaderboard(self, interaction: discord.Interaction):
        import json
        # Read under the wallet lock to avoid a partially-written file.
        # Use the module's own WALLET_FILE — a second hardcoded relative path
        # here would read a different (empty) file whenever the bot is started
        # from a directory other than the project root.
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
            return await interaction.response.send_message(
                "No casino data yet.", ephemeral=True)

        lines = []
        medals = ["🥇","🥈","🥉"] + ["🏅"]*7
        for i, (uid, d) in enumerate(sorted_players):
            try:
                member = interaction.guild.get_member(int(uid))
                name   = member.display_name if member else f"User {uid}"
            except Exception:
                name = f"User {uid}"
            lines.append(f"{medals[i]} {name}  —  🪙 {d.get('balance',0):,}")

        e = discord.Embed(
            title="🎰 Casino Leaderboard",
            description="\n".join(lines),
            color=0xf1c40f
        )
        await interaction.response.send_message(embed=e)

    @casino.command(name="exchange", description="Exchange between Beycoins and casino coins.")
    @app_commands.describe(
        direction="Which way to exchange",
        amount="How many coins to exchange"
    )
    @app_commands.choices(direction=[
        app_commands.Choice(name="Beycoins → Casino  (100 : 60)", value="buy"),
        app_commands.Choice(name="Casino → Beycoins  (10% tax, lower with premium)", value="sell"),
    ])
    async def exchange(self, interaction: discord.Interaction,
                       direction: str, amount: int):
        if amount <= 0:
            return await interaction.response.send_message(
                "Amount must be positive.", ephemeral=True)

        uid = interaction.user.id

        if direction == "buy":
            # amount = Beycoins to spend (must be multiple of 100)
            if amount % BEYCOIN_PER_EXCHANGE != 0:
                return await interaction.response.send_message(
                    f"Amount must be a multiple of {BEYCOIN_PER_EXCHANGE}.", ephemeral=True)

            casino_gain  = (amount // BEYCOIN_PER_EXCHANGE) * CASINO_PER_EXCHANGE
            profile = database.get_user(uid)
            if profile.get("coins", 0) < amount:
                return await interaction.response.send_message(
                    f"Not enough Beycoins. Need 🪙 {amount:,} but you have 🪙 {profile.get('coins',0):,}.",
                    ephemeral=True)

            profile["coins"] -= amount
            database.update_user(uid, profile)
            await casino_wallet.credit(uid, casino_gain)

            casino_bal = await casino_wallet.get_balance(uid)
            e = discord.Embed(title="💱 Exchange Complete", color=0x2ecc71)
            e.add_field(name="Spent",    value=f"🪙 {amount:,} Beycoins",      inline=True)
            e.add_field(name="Received", value=f"🎰 {casino_gain:,} casino coins", inline=True)
            e.add_field(name="Rate",     value=f"100 Beycoins = 60 casino coins", inline=False)
            e.add_field(name="Casino balance",  value=f"🎰 {casino_bal:,}",       inline=True)
            e.add_field(name="Beycoin balance", value=f"🪙 {profile['coins']:,}", inline=True)

        else:  # sell
            # amount = casino coins to sell
            casino_bal = await casino_wallet.get_balance(uid)
            if casino_bal < amount:
                return await interaction.response.send_message(
                    f"Not enough casino coins. Have 🎰 {casino_bal:,}, need 🎰 {amount:,}.",
                    ephemeral=True)

            tax_rate       = await casino_premium.get_exchange_tax(uid)
            beycoin_gross  = (amount // CASINO_PER_EXCHANGE) * BEYCOIN_PER_EXCHANGE
            tax_taken      = int(beycoin_gross * tax_rate)
            beycoin_return = beycoin_gross - tax_taken

            await casino_wallet.deduct(uid, amount)
            profile = database.get_user(uid)
            profile["coins"] += beycoin_return
            database.update_user(uid, profile)

            prem = await casino_premium.get_premium(uid)
            tax_label = f"Tax ({tax_rate:.0%})"
            tax_value = (f"🪙 {tax_taken:,} Beycoins" if tax_taken else "🪙 0 — **tax free!**")
            if prem:
                tax_value += f"\n{casino_premium.PACKS[prem['key']]['display']} pass active"
            else:
                tax_value += f"\n*Buy a premium pass to cut this down to 0%.*"

            casino_bal_new = await casino_wallet.get_balance(uid)
            e = discord.Embed(title="💱 Exchange Complete", color=0x2ecc71)
            e.add_field(name="Sold",      value=f"🎰 {amount:,} casino coins",     inline=True)
            e.add_field(name="Received",  value=f"🪙 {beycoin_return:,} Beycoins", inline=True)
            e.add_field(name=tax_label,   value=tax_value,                        inline=False)
            e.add_field(name="Casino balance",  value=f"🎰 {casino_bal_new:,}",    inline=True)
            e.add_field(name="Beycoin balance", value=f"🪙 {profile['coins']:,}",  inline=True)

        await interaction.response.send_message(embed=e, ephemeral=True)

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
            profile = database.get_user(uid)
            if profile.get("coins", 0) < amount:
                return await ctx.send(
                    f"❌ Not enough Beycoins. Need 🪙 {amount:,} but you have 🪙 {profile.get('coins', 0):,}.")

            profile["coins"] -= amount
            database.update_user(uid, profile)
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
            profile = database.get_user(uid)
            profile["coins"] += beycoin_return
            database.update_user(uid, profile)

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

    # Every game the casino has, as {slash value: (label, prefix command)}.
    # One `play` subcommand with autocomplete rather than one subcommand per
    # game: Discord caps a group at 25 subcommands and there are more games
    # than that, so per-game subcommands would stop working the moment another
    # is added. Autocomplete also searches, which a flat list of 30 does not.
    GAMES = {
        "blackjack": ("🃏 Blackjack", "blackjack"),
        "coinflip": ("🪙 Coinflip", "coinflip"),
        "slots": ("🎰 Slots", "slots"),
        "roulette": ("🎡 Roulette", "roulette"),
        "crash": ("📈 Crash", "crash"),
        "mines": ("💣 Mines", "mines"),
        "minesmp": ("💣 Mines (multiplayer)", "minesmp"),
        "tower": ("🗼 Tower", "tower"),
        "dice": ("🎲 Dice", "dice"),
        "diceduel": ("🎲 Dice duel", "diceduel"),
        "higherlower": ("🔼 Higher or lower", "higherlower"),
        "wheel": ("🎡 Wheel", "wheel"),
        "plinko": ("🔻 Plinko", "plinko"),
        "keno": ("🔢 Keno", "keno"),
        "videopoker": ("🃏 Video poker", "videopoker"),
        "poker": ("♠️ Poker", "poker"),
        "beyrace": ("🏁 Bey race", "beyrace"),
        "auction": ("🔨 Auction", "auction"),
    }

    @casino.command(name="menu",
                    description="Open the casino lobby and pick a game.")
    async def menu(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        cog = self.bot.get_cog("CasinoMenu") or self.bot.get_cog("Casino Menu")
        cmd = self.bot.get_command("casinomenu") or self.bot.get_command("casino")
        if cmd is None:
            return await interaction.response.send_message(
                "The casino lobby isn't loaded right now.", ephemeral=True)
        await ctx.invoke(cmd)

    @casino.command(name="play", description="Launch a casino game.")
    @app_commands.describe(game="Which game to play",
                           bet="Casino coins to wager (games that need one)")
    async def play(self, interaction: discord.Interaction, game: str,
                   bet: Optional[int] = None):
        entry = self.GAMES.get(game.lower())
        if entry is None:
            return await interaction.response.send_message(
                f"Unknown game `{game}` — pick one from the list.",
                ephemeral=True)
        label, prefix_name = entry
        cmd = self.bot.get_command(prefix_name)
        if cmd is None:
            return await interaction.response.send_message(
                f"{label} isn't loaded right now.", ephemeral=True)
        if bet is not None and bet <= 0:
            return await interaction.response.send_message(
                "Bet has to be a positive number.", ephemeral=True)

        # Reuse the prefix command rather than reimplementing each game — the
        # games own their own validation, cooldowns and views, and duplicating
        # that here is how the two paths drift apart.
        ctx = await commands.Context.from_interaction(interaction)
        try:
            if bet is None:
                await ctx.invoke(cmd)
            else:
                await ctx.invoke(cmd, bet)
        except TypeError:
            # Game takes no bet (or a different signature) — run it bare.
            await ctx.invoke(cmd)

    @play.autocomplete("game")
    async def play_autocomplete(self, interaction: discord.Interaction,
                                current: str):
        cur = (current or "").lower()
        out = [app_commands.Choice(name=label, value=value)
               for value, (label, _) in self.GAMES.items()
               if cur in value or cur in label.lower()]
        return out[:25]          # Discord shows at most 25 choices

    @casino.command(name="games", description="List all available casino games.")
    async def games(self, interaction: discord.Interaction):
        e = discord.Embed(
            title="🎰  Casino Games",
            description="Tip: run **`;casino`** to browse and launch any of these "
                        "from one menu — no typing required.",
            color=0x9b59b6,
        )
        e.add_field(
            name="🆕 New",
            value=(
                "`;wheel` — one spin, bust to 50x\n"
                "`;plinko` — 12 rows of pegs, edges up to 220x\n"
                "`;keno` — pick 10 of 40, house draws 10\n"
                "`;videopoker` — Jacks or Better, royal pays 400x\n"
                "`;beyrace` — back a blade, five sets of odds\n"
                "`;minesmp` — shared-grid Mines, last one standing wins the pot"
            ),
            inline=False,
        )
        e.add_field(
            name="🃏 Single Player",
            value=(
                "`;blackjack` — vs dealer AI, hit/stand/double/split\n"
                "`;slots` — 5-reel slot machine, Beyblade jackpot\n"
                "`;roulette` — bet on numbers, colors, groups\n"
                "`;dice` — pick your risk multiplier\n"
                "`;mines` — reveal tiles, avoid bombs, cash out\n"
                "`;tower` — climb floors vs RNG enemies"
            ),
            inline=False,
        )
        e.add_field(
            name="⚔️ Multiplayer",
            value=(
                "`;crash` — shared channel round, everyone rides one multiplier\n"
                "`;minesmp` — shared-grid Mines, last blade standing\n"
                "`;poker` — Texas Hold'em, 2–6 players\n"
                "`;roulette_multi` — shared wheel, everyone bets\n"
                "`;diceduel` — two players, highest roll wins\n"
                "`;coinflip` — heads or tails duel\n"
                "`;higherlower` — card duel, first to 3 wins\n"
                "`;auction` — seller-initiated bidding"
            ),
            inline=False,
        )
        e.add_field(
            name="💰 Wallet & Passes",
            value=(
                "`;casino` — the lobby (all games in one menu)\n"
                "`/casino balance` — check coins\n"
                "`/casino daily` — claim your daily bonus\n"
                "`/casino exchange` — swap Beycoins ↔ casino coins "
                f"({casino_premium.BASE_EXCHANGE_TAX:.0%} sell tax)\n"
                "`;premium info` — passes cut the sell tax to 0%"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=e)


async def setup(bot):
    await bot.add_cog(CasinoHub(bot))
