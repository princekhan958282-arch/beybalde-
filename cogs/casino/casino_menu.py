"""
casino_menu.py  —  🎰 The Casino

One place to see every game and launch it without typing a command.

    ;casino            → the lobby
    ;casino <bet>      → lobby with the bet pre-filled
    /casinomenu        → same thing as a slash command

The lobby shows your wallet, your premium tier and your live exchange tax,
then lets you browse games by category, read what each one does, and hit
Play. Every game is still available as its own prefix command — this is
purely a friendlier front door.
"""

import inspect
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from . import casino_premium, casino_wallet

log = logging.getLogger("beyblade_bot")

MIN_BET = 10

# ── Invocation styles ─────────────────────────────────────────────────────────
#   ctx_opp_bet     await cog.cmd(ctx, None, bet)
#   ctx_bet         await cog.cmd(ctx, bet)
#   ctx_only        await cog.cmd(ctx)
#   app_bet         await cmd.callback(cog, interaction, bet)
#   manual          can't be launched from the menu — show usage instead

GAMES: list[dict] = [
    # ── Solo ──────────────────────────────────────────────────────────────────
    dict(key="mines", name="Mines", emoji="💣", cat="solo", cog="MinesCog",
         attr="mines", style="ctx_opp_bet", cmd=";mines <bet>",
         blurb="Reveal tiles on a 4×5 grid without hitting a bomb. "
               "Every safe tile raises your multiplier — cash out whenever you want.",
         tag="Cash-out"),
    dict(key="blackjack", name="Blackjack", emoji="🃏", cat="solo", cog="BlackjackCog",
         attr="blackjack", style="ctx_opp_bet", cmd=";blackjack <bet>",
         blurb="Beat the dealer to 21. Hit, stand, double down or split.",
         tag="Card game"),
    dict(key="videopoker", name="Video Poker", emoji="🎴", cat="solo", cog="VideoPokerCog",
         attr="videopoker", style="ctx_bet", cmd=";videopoker <bet>",
         blurb="Jacks or Better. Get five cards, hold the ones you like, "
               "draw once. Royal flush pays 400x.",
         tag="NEW", new=True),
    dict(key="slots", name="Slots", emoji="🎰", cat="solo", cog="SlotsCog",
         attr="slots", style="ctx_opp_bet", cmd=";slots <bet>",
         blurb="Five-reel Beyblade slot machine with a themed jackpot.",
         tag="Instant"),
    dict(key="wheel", name="Wheel of Fortune", emoji="🎡", cat="solo", cog="WheelCog",
         attr="wheel", style="ctx_bet", cmd=";wheel <bet>",
         blurb="One spin, one multiplier. Nine segments from bust all the way "
               "up to a 50x diamond.",
         tag="NEW", new=True),
    dict(key="plinko", name="Plinko", emoji="🔻", cat="solo", cog="PlinkoCog",
         attr="plinko", style="ctx_bet", cmd=";plinko <bet> [low|medium|high]",
         blurb="Drop a ball through 12 rows of pegs into one of 13 slots. "
               "Three risk profiles — high risk edges pay 220x.",
         tag="NEW", new=True),
    dict(key="keno", name="Keno", emoji="🔢", cat="solo", cog="KenoCog",
         attr="keno", style="ctx_bet", cmd=";keno <bet>",
         blurb="Pick up to 10 numbers from a board of 40. The house draws 10. "
               "Hit all ten and it pays 40,000x.",
         tag="NEW", new=True),
    dict(key="beyrace", name="Bey Race", emoji="🏁", cat="solo", cog="BeyRaceCog",
         attr="beyrace", style="ctx_bet", cmd=";beyrace <bet>",
         blurb="Five wild Beyblades, five sets of odds. Back one and watch "
               "the stadium sort it out.",
         tag="NEW", new=True),
    dict(key="crash", name="Crash", emoji="📈", cat="multi", cog="CrashCog",
         attr="crash_prefix", style="ctx_bet", cmd=";crash <bet>",
         blurb="Shared round for the whole channel. The multiplier climbs — "
               "everyone cashes out on their own nerve. Anything still riding "
               "when it crashes is gone.",
         tag="Whole channel"),
    dict(key="tower", name="Tower Climb", emoji="🗼", cat="solo", cog="TowerCog",
         attr="tower", style="ctx_opp_bet", cmd=";tower <bet>",
         blurb="Fight your way up the tower. Each floor cleared raises the "
               "pot — bank it or push your luck.",
         tag="Cash-out"),
    dict(key="dice", name="Dice", emoji="🎲", cat="solo", cog="DiceCog",
         attr="dice", style="ctx_opp_bet", cmd=";dice <bet>",
         blurb="Pick your risk multiplier and roll.",
         tag="Instant"),
    dict(key="roulette", name="Roulette", emoji="🔴", cat="solo", cog="RouletteCog",
         attr="roulette", style="ctx_only", cmd=";roulette",
         blurb="Bet on numbers, colours, odds/evens, dozens or halves, "
               "then spin the wheel.",
         needs_bet=False, tag="Table"),

    # ── Multiplayer ───────────────────────────────────────────────────────────
    dict(key="minesmp", name="Mines: Last Standing", emoji="💣", cat="multi",
         cog="MinesMultiCog", attr="minesmp", style="ctx_bet",
         cmd=";minesmp <entry> [mines]",
         blurb="One shared grid, 2–8 players, turns rotate. Reveal a tile or "
               "fold for half your entry. Hit a mine and you're out — your "
               "entry stays in the pot. Last one standing takes it all.",
         bet_label="Entry", tag="NEW • 2–8 players", new=True),
    dict(key="poker", name="Texas Hold'em", emoji="♠️", cat="multi", cog="PokerCog",
         attr="poker", style="ctx_bet", cmd=";poker <buy-in>",
         blurb="Full Texas Hold'em table for 2–6 players. You host, others join.",
         bet_label="Buy-in", tag="2–6 players"),
    dict(key="roulette_multi", name="Roulette (Table)", emoji="🎡", cat="multi",
         cog="RouletteCog", attr="roulette_multi", style="ctx_only",
         cmd=";roulette_multi",
         blurb="Shared wheel — everyone places their own bets, host spins.",
         needs_bet=False, tag="Party"),
    dict(key="diceduel", name="Dice Duel", emoji="⚔️", cat="multi", cog="DiceCog",
         attr="diceduel", style="ctx_opp_bet", cmd=";diceduel @user <bet>",
         blurb="Two players, one roll each, highest wins the pot.",
         tag="1v1"),
    dict(key="coinflip", name="Coinflip", emoji="🪙", cat="multi", cog="CoinFlipCog",
         attr="coinflip", style="ctx_opp_bet", cmd=";coinflip @user <bet>",
         blurb="Heads or tails duel for the whole pot.",
         tag="1v1"),
    dict(key="higherlower", name="Higher / Lower", emoji="🔼", cat="multi",
         cog="HigherLowerCog", attr="higherlower", style="ctx_opp_bet",
         cmd=";higherlower @user <bet>",
         blurb="Card duel — call higher or lower, first to three points wins.",
         tag="1v1"),
    dict(key="auction", name="Auction House", emoji="🔨", cat="multi", cog="AuctionCog",
         attr="auction", style="manual",
         cmd=";auction <item> <start_bid> [minutes] [description]",
         blurb="Host a live bidding war. Needs the Auction role or Admin.",
         needs_bet=False, tag="Host only"),
]

GAMES_BY_KEY = {g["key"]: g for g in GAMES}

CATEGORIES = {
    "solo":  ("🎲", "Solo Games",   "Play on your own, any time"),
    "multi": ("⚔️", "Multiplayer",  "Duels, tables and lobbies"),
}


def _games_in(cat: str) -> list[dict]:
    return [g for g in GAMES if g["cat"] == cat]


# ── Launcher ──────────────────────────────────────────────────────────────────

async def _launch(bot: commands.Bot, game: dict, ctx: Optional[commands.Context],
                  interaction: discord.Interaction, bet: int) -> Optional[str]:
    """Run the selected game. Returns an error string, or None on success.

    ``interaction`` must be un-responded for ``app_bet`` games.
    """
    cog = bot.get_cog(game["cog"])
    if cog is None:
        return f"`{game['name']}` isn't loaded right now. Try `{game['cmd']}`."

    style = game["style"]

    if style == "manual":
        return f"This one needs arguments — run it directly:\n`{game['cmd']}`"

    if style == "app_bet":
        cmd = getattr(cog, game["attr"], None)
        if cmd is None or not hasattr(cmd, "callback"):
            return f"Use `{game['cmd']}` for this one."
        params = list(inspect.signature(cmd.callback).parameters)
        if params and params[0] == "self":
            await cmd.callback(cog, interaction, bet)
        else:
            await cmd.callback(interaction, bet)
        return None

    if ctx is None:
        return f"Run `{game['cmd']}` to start this one."

    cmd = getattr(cog, game["attr"], None)
    if cmd is None:
        return f"Use `{game['cmd']}` for this one."

    if style == "ctx_only":
        await cmd(ctx)
    elif style == "ctx_bet":
        await cmd(ctx, bet)
    elif style == "ctx_opp_bet":
        await cmd(ctx, None, bet)
    else:
        return f"Use `{game['cmd']}` for this one."
    return None


class BetModal(discord.ui.Modal):
    def __init__(self, view: "CasinoLobbyView", game: dict, max_bet: int):
        label = game.get("bet_label", "Bet")
        super().__init__(title=f"{game['name']} — {label}")
        self.view_ref = view
        self.game     = game
        self.amount   = discord.ui.TextInput(
            label=f"{label} (🪙 {MIN_BET:,} – {max_bet:,})",
            placeholder=str(view.bet or MIN_BET),
            default=str(view.bet) if view.bet else None,
            required=True,
            max_length=12,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount.value.strip().replace(",", "").replace("_", "").lower()
        mult = 1
        if raw.endswith("k"):
            mult, raw = 1_000, raw[:-1]
        elif raw.endswith("m"):
            mult, raw = 1_000_000, raw[:-1]
        try:
            bet = int(float(raw) * mult)
        except ValueError:
            return await interaction.response.send_message(
                "❌ That's not a number. Try `500` or `10k`.", ephemeral=True)

        max_bet = await casino_premium.get_max_bet(interaction.user.id)
        if not (MIN_BET <= bet <= max_bet):
            return await interaction.response.send_message(
                f"❌ {self.game.get('bet_label', 'Bet')} must be 🪙 {MIN_BET:,}–{max_bet:,}.",
                ephemeral=True)

        bal = await casino_wallet.get_balance(interaction.user.id)
        if bal < bet:
            return await interaction.response.send_message(
                f"❌ Not enough casino coins — you have 🪙 {bal:,}.\n"
                f"Try `;casino daily` or `;casinoexchange buy <beycoins>`.",
                ephemeral=True)

        self.view_ref.bet = bet
        await self.view_ref.launch(interaction, self.game, bet)


class GameSelect(discord.ui.Select):
    def __init__(self, cat: str, current: Optional[str]):
        opts = []
        for g in _games_in(cat):
            opts.append(discord.SelectOption(
                label=g["name"],
                value=g["key"],
                emoji=g["emoji"],
                description=g.get("tag", ""),
                default=(g["key"] == current),
            ))
        emoji, title, _ = CATEGORIES[cat]
        super().__init__(placeholder=f"{emoji} Browse {title.lower()}…",
                         options=opts, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: "CasinoLobbyView" = self.view
        if not view.owns(interaction):
            return await interaction.response.send_message("Not your lobby.", ephemeral=True)
        view.selected = self.values[0]
        view.rebuild()
        await interaction.response.edit_message(embed=await view.build_embed(), view=view)


class CategoryButton(discord.ui.Button):
    def __init__(self, cat: str, active: bool):
        emoji, title, _ = CATEGORIES[cat]
        super().__init__(
            label=title,
            emoji=emoji,
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
            row=1,
        )
        self.cat = cat

    async def callback(self, interaction: discord.Interaction):
        view: "CasinoLobbyView" = self.view
        if not view.owns(interaction):
            return await interaction.response.send_message("Not your lobby.", ephemeral=True)
        view.cat      = self.cat
        view.selected = None
        view.rebuild()
        await interaction.response.edit_message(embed=await view.build_embed(), view=view)


class PlayButton(discord.ui.Button):
    def __init__(self, game: Optional[dict]):
        label = f"Play {game['name']}" if game else "Pick a game first"
        super().__init__(label=label, emoji="▶", row=2,
                         style=discord.ButtonStyle.success,
                         disabled=game is None)
        self.game = game

    async def callback(self, interaction: discord.Interaction):
        view: "CasinoLobbyView" = self.view
        if not view.owns(interaction):
            return await interaction.response.send_message("Not your lobby.", ephemeral=True)
        game = self.game
        if game is None:
            return await interaction.response.defer()

        if game["style"] == "manual":
            return await interaction.response.send_message(
                f"**{game['emoji']} {game['name']}** needs arguments.\n"
                f"Run it directly: `{game['cmd']}`", ephemeral=True)

        if not game.get("needs_bet", True):
            return await view.launch(interaction, game, 0)

        max_bet = await casino_premium.get_max_bet(interaction.user.id)
        await interaction.response.send_modal(BetModal(view, game, max_bet))


class CasinoLobbyView(discord.ui.View):
    def __init__(self, cog, player: discord.Member, ctx: Optional[commands.Context],
                 bet: int = 0):
        super().__init__(timeout=180)
        self.cog      = cog
        self.player   = player
        self.ctx      = ctx
        self.bet      = bet
        self.cat      = "solo"
        self.selected: Optional[str] = None
        self.message: Optional[discord.Message] = None
        self.rebuild()

    def owns(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.player.id

    # ── Component layout ──────────────────────────────────────────────────────
    def rebuild(self):
        self.clear_items()
        self.add_item(GameSelect(self.cat, self.selected))
        for cat in CATEGORIES:
            self.add_item(CategoryButton(cat, cat == self.cat))
        game = GAMES_BY_KEY.get(self.selected) if self.selected else None
        self.add_item(PlayButton(game))
        self.add_item(WalletButton())
        self.add_item(PremiumButton())

    # ── Embed ─────────────────────────────────────────────────────────────────
    async def build_embed(self) -> discord.Embed:
        bal      = await casino_wallet.get_balance(self.player.id)
        prem     = await casino_premium.get_premium(self.player.id)
        max_bet  = await casino_premium.get_max_bet(self.player.id)
        tax      = await casino_premium.get_exchange_tax(self.player.id)
        tier     = casino_premium.PACKS[prem["key"]]["display"] if prem else "None"
        colour   = casino_premium.PACKS[prem["key"]]["color"] if prem else 0x9b59b6

        game = GAMES_BY_KEY.get(self.selected) if self.selected else None

        if game:
            e = discord.Embed(
                title=f"{game['emoji']}  {game['name']}",
                description=game["blurb"],
                color=colour,
            )
            e.add_field(name="Command", value=f"`{game['cmd']}`", inline=False)
            if game.get("needs_bet", True):
                label = game.get("bet_label", "Bet")
                e.add_field(name=f"{label} range",
                            value=f"🪙 {MIN_BET:,} – {max_bet:,}", inline=True)
            e.add_field(name="Your balance", value=f"🪙 {bal:,}", inline=True)
            e.set_footer(text="▶ Play to start  •  use the buttons to browse other games")
            return e

        emoji, title, sub = CATEGORIES[self.cat]
        listing = "\n".join(
            f"{g['emoji']} **{g['name']}**"
            + ("  🆕" if g.get("new") else "")
            + f" — {g.get('tag', '')}"
            for g in _games_in(self.cat)
        )

        e = discord.Embed(
            title="🎰  Beycord Casino",
            description=(
                f"**{len(GAMES)} games** — pick one from the dropdown to see how it "
                f"works, then hit **▶ Play**.\n\n"
                f"**{emoji} {title}** — *{sub}*\n{listing}"
            ),
            color=colour,
        )
        e.add_field(name="💰 Balance",  value=f"🪙 {bal:,}",     inline=True)
        e.add_field(name="🎯 Max bet",  value=f"🪙 {max_bet:,}", inline=True)
        e.add_field(name="👑 Pass",     value=tier,             inline=True)
        e.add_field(
            name="💱 Cash-out tax",
            value=(f"**{tax:.0%}**" + ("  — tax free!" if tax == 0 else
                   f"  *(base {casino_premium.BASE_EXCHANGE_TAX:.0%}, "
                   f"Legend pass = 0%)*")),
            inline=False,
        )
        e.set_footer(text=f"{self.player.display_name}  •  ;casino daily for free coins")
        return e

    # ── Launch ────────────────────────────────────────────────────────────────
    async def launch(self, interaction: discord.Interaction, game: dict, bet: int):
        needs_fresh_interaction = game["style"] == "app_bet"

        if not needs_fresh_interaction and not interaction.response.is_done():
            await interaction.response.defer()

        for c in self.children:
            c.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass
        self.stop()

        try:
            err = await _launch(self.cog.bot, game, self.ctx, interaction, bet)
        except Exception as exc:
            log.error(f"[casino_menu] launching {game['key']} failed: {exc}", exc_info=True)
            err = f"Couldn't start **{game['name']}**. Run `{game['cmd']}` instead."

        if err:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(err, ephemeral=True)
                else:
                    await interaction.response.send_message(err, ephemeral=True)
            except Exception:
                pass

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class WalletButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Wallet", emoji="💰",
                         style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction: discord.Interaction):
        uid  = interaction.user.id
        bal  = await casino_wallet.get_balance(uid)
        tax  = await casino_premium.get_exchange_tax(uid)
        mb   = await casino_premium.get_max_bet(uid)
        daily = await casino_premium.get_daily_bonus(uid)
        e = discord.Embed(title="💰  Your Casino Wallet", color=0xf1c40f)
        e.add_field(name="Balance",     value=f"🪙 {bal:,}",   inline=True)
        e.add_field(name="Max bet",     value=f"🪙 {mb:,}",    inline=True)
        e.add_field(name="Daily bonus", value=f"🪙 {daily:,}", inline=True)
        e.add_field(name="Cash-out tax", value=f"{tax:.0%}", inline=True)
        e.add_field(
            name="Commands",
            value=("`;casinodaily` — free coins every day\n"
                   "`;casinoexchange buy <beycoins>` — top up\n"
                   "`;casinoexchange sell <coins>` — cash out\n"
                   "`;casinolb` — richest players"),
            inline=False,
        )
        await interaction.response.send_message(embed=e, ephemeral=True)


class PremiumButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Premium", emoji="👑",
                         style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction: discord.Interaction):
        prem = await casino_premium.get_premium(interaction.user.id)
        e = discord.Embed(
            title="👑  Casino Premium Passes",
            description=(
                "Passes last **7 days** and lower your cash-out tax "
                f"from **{casino_premium.BASE_EXCHANGE_TAX:.0%}** all the way to **0%**.\n"
                "Buy with `;premium buy <pack>`."
            ),
            color=0xf1c40f,
        )
        for key, p in casino_premium.PACKS.items():
            active = " ✅ **ACTIVE**" if prem and prem["key"] == key else ""
            e.add_field(
                name=f"{p['display']} — 🪙 {p['price']:,} Beycoins{active}",
                value=(f"Max bet **{p['max_bet']:,}** • "
                       f"Daily **{p['daily_bonus']:,}** • "
                       f"Tax **{p.get('exchange_tax', casino_premium.BASE_EXCHANGE_TAX):.0%}**\n"
                       f"`;premium buy {key}`"),
                inline=False,
            )
        await interaction.response.send_message(embed=e, ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class CasinoMenuCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="casino", aliases=["cmenu", "casinomenu", "gamelist", "gh"])
    async def casino_menu(self, ctx: commands.Context, bet: int = 0):
        """🎰 Open the casino lobby — browse and launch every game."""
        view = CasinoLobbyView(self, ctx.author, ctx, max(0, bet))
        view.message = await ctx.send(embed=await view.build_embed(), view=view)

    # Retired as a top-level slash command — it now lives at `/casino menu`,
    # which is the whole point of the regrouping: one `/casino` entry instead
    # of `/casino` plus `/casinomenu` plus `/crash` sitting beside it.
    # The `;casinomenu` prefix command below is untouched.
    async def casino_menu_slash(self, interaction: discord.Interaction):
        ctx = None
        try:
            ctx = await commands.Context.from_interaction(interaction)
        except Exception as exc:
            log.debug(f"[casino_menu] from_interaction failed: {exc}")

        view = CasinoLobbyView(self, interaction.user, ctx, 0)
        await interaction.response.send_message(embed=await view.build_embed(), view=view)
        try:
            view.message = await interaction.original_response()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(CasinoMenuCog(bot))
