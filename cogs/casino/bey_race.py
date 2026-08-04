"""
bey_race.py  —  🏁 Bey Race

Five wild Beyblades line up in the stadium. Each lane gets its own odds.
Back one, watch the race, collect if it wins.

Odds are honest: payout = 0.955 / win_chance, so RTP ≈ 95.5% on every lane.

Usage:  ;beyrace <bet>     (aliases: ;race, ;br)
"""

import asyncio
import random
from typing import Optional

import discord
from discord.ext import commands

from . import casino_premium, casino_wallet

MIN_BET   = 10
LANES     = 5
TRACK_LEN = 12
LANE_EMOJI = ["🔵", "🔴", "🟢", "🟡", "🟣"]

# Win weights per lane slot (shuffled onto blades each race) and their fair odds.
LANE_WEIGHTS = [34, 26, 19, 13, 8]          # sums to 100
LANE_ODDS    = [2.80, 3.65, 5.00, 7.30, 11.90]

_FALLBACK_NAMES = [
    "Dragoon Storm", "Dranzer Flame", "Draciel Shield", "Driger Fang",
    "Wolborg", "Valtryek", "Spryzen", "Fafnir", "Achilles", "Lucius",
    "Longinus", "Pegasus", "Roktavor", "Doomscizor", "Xcalius",
]


def _pick_racers() -> list[str]:
    """Five distinct blade names, pulled live from the database when possible."""
    try:
        from utils.database import load_beyblades
        beys = load_beyblades()
        names = [d.get("name") for d in beys.values() if d.get("name")]
        if len(names) >= LANES:
            return random.sample(names, LANES)
    except Exception:
        pass
    return random.sample(_FALLBACK_NAMES, LANES)


class Race:
    def __init__(self):
        self.racers  = _pick_racers()
        # shuffle which lane gets which strength so the field is never predictable
        order        = list(range(LANES))
        random.shuffle(order)
        self.weights = [LANE_WEIGHTS[order[i]] for i in range(LANES)]
        self.odds    = [LANE_ODDS[order[i]] for i in range(LANES)]

        self.winner  = random.choices(range(LANES), weights=self.weights, k=1)[0]

        # Finish "ticks" — winner always lowest, so the render stays consistent
        ticks = sorted(random.sample(range(10, 26), LANES))
        rest  = [i for i in range(LANES) if i != self.winner]
        random.shuffle(rest)
        self.finish_tick = [0] * LANES
        self.finish_tick[self.winner] = ticks[0]
        for slot, lane in enumerate(rest, start=1):
            self.finish_tick[lane] = ticks[slot]

        self.total_ticks = self.finish_tick[self.winner]

    def positions(self, tick: int) -> list[int]:
        out = []
        for lane in range(LANES):
            p = int(TRACK_LEN * tick / self.finish_tick[lane])
            out.append(min(TRACK_LEN, p))
        return out

    def standings(self) -> list[int]:
        """Lane indexes ordered from 1st to last."""
        return sorted(range(LANES), key=lambda l: self.finish_tick[l])


def _track_text(race: Race, tick: int, picked: Optional[int] = None) -> str:
    pos = race.positions(tick)
    lines = []
    for lane in range(LANES):
        p = pos[lane]
        bar = "─" * p + LANE_EMOJI[lane] + "─" * (TRACK_LEN - p)
        marker = "▶" if lane == picked else " "
        name = race.racers[lane]
        if len(name) > 16:
            name = name[:15] + "…"
        lines.append(f"{marker}`{bar}🏁` **{name}**")
    return "\n".join(lines)


class RacerButton(discord.ui.Button):
    def __init__(self, lane: int, race: Race):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=f"{race.odds[lane]:g}x  {race.racers[lane][:20]}",
            emoji=LANE_EMOJI[lane],
            row=lane,
        )
        self.lane = lane

    async def callback(self, interaction: discord.Interaction):
        view: "RaceLobbyView" = self.view
        if interaction.user.id != view.player.id:
            return await interaction.response.send_message("Not your race.", ephemeral=True)
        if view.started:
            return await interaction.response.defer()

        view.picked  = self.lane
        view.started = True
        for c in view.children:
            c.disabled = True
        self.style = discord.ButtonStyle.success
        await interaction.response.edit_message(view=view)
        view.stop()
        await view.cog._run_race(interaction.message, view.player, view.bet,
                                 view.race, self.lane)


class RaceLobbyView(discord.ui.View):
    def __init__(self, cog, player: discord.Member, bet: int, race: Race):
        super().__init__(timeout=60)
        self.cog     = cog
        self.player  = player
        self.bet     = bet
        self.race    = race
        self.picked: Optional[int] = None
        self.started = False
        self.message: Optional[discord.Message] = None
        for lane in range(LANES):
            self.add_item(RacerButton(lane, race))

    def build_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="🏁  Bey Race — Pick your blade",
            description="\n".join(
                f"{LANE_EMOJI[l]} **{self.race.racers[l]}**  —  "
                f"pays **{self.race.odds[l]:g}x**  "
                f"*({self.race.weights[l]}% chance)*"
                for l in range(LANES)
            ),
            color=0x9b59b6,
        )
        e.add_field(name="Bet", value=f"🪙 {self.bet:,}", inline=True)
        e.add_field(name="Max win",
                    value=f"🪙 {int(self.bet * max(self.race.odds)):,}", inline=True)
        e.set_footer(text=f"{self.player.display_name}  •  60s to choose")
        return e

    async def on_timeout(self):
        if self.started:
            return
        for c in self.children:
            c.disabled = True
        await casino_wallet.credit(self.player.id, self.bet)
        self.cog._active.discard(self.player.id)
        if self.message:
            try:
                await self.message.edit(content="⏰ Timed out — bet refunded.", view=self)
            except Exception:
                pass


class RaceResultView(discord.ui.View):
    def __init__(self, cog, player: discord.Member, bet: int):
        super().__init__(timeout=60)
        self.cog    = cog
        self.player = player
        self.bet    = bet
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="🏁  New Race", style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("Not your race.", ephemeral=True)
        if self.player.id in self.cog._active:
            return await interaction.response.send_message("Finish your current race first.",
                                                           ephemeral=True)
        max_bet = await casino_premium.get_max_bet(self.player.id)
        if self.bet > max_bet:
            return await interaction.response.send_message(
                f"Your max bet is now 🪙 {max_bet:,}.", ephemeral=True)
        if not await casino_wallet.deduct(self.player.id, self.bet):
            return await interaction.response.send_message("❌ Not enough casino coins.",
                                                           ephemeral=True)
        self.cog._active.add(self.player.id)
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

        race = Race()
        view = RaceLobbyView(self.cog, self.player, self.bet, race)
        view.message = await interaction.followup.send(
            embed=view.build_embed(), view=view, wait=True)

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class BeyRaceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active: set[int] = set()

    async def _run_race(self, message: discord.Message, player: discord.Member,
                        bet: int, race: Race, picked: int):
        """Animate the race and settle. Bet must already be deducted."""
        try:
            frames = max(3, race.total_ticks // 4)
            for f in range(1, 5):
                tick = int(race.total_ticks * f / 5)
                e = discord.Embed(title="🏁  Bey Race — GO!",
                                  description=_track_text(race, tick, picked),
                                  color=0x3498db)
                e.add_field(name="Your pick",
                            value=f"{LANE_EMOJI[picked]} **{race.racers[picked]}** "
                                  f"({race.odds[picked]:g}x)", inline=False)
                try:
                    await message.edit(embed=e, view=None)
                except discord.DiscordException:
                    break
                await asyncio.sleep(0.85)

            won    = picked == race.winner
            payout = int(bet * race.odds[picked]) if won else 0
            profit = payout - bet
            if payout > 0:
                await casino_wallet.credit(player.id, payout)
            bal = await casino_wallet.get_balance(player.id)

            order  = race.standings()
            podium = "\n".join(
                f"{['🥇','🥈','🥉','4️⃣','5️⃣'][i]} {LANE_EMOJI[l]} **{race.racers[l]}**"
                + ("  ← your pick" if l == picked else "")
                for i, l in enumerate(order)
            )

            e = discord.Embed(
                title=("🏆  Your blade took it!" if won else "🏁  Race Over"),
                description=_track_text(race, race.total_ticks, picked) + "\n\n" + podium,
                color=(0x2ecc71 if won else 0xe74c3c),
            )
            e.add_field(name="Winner",
                        value=f"{LANE_EMOJI[race.winner]} **{race.racers[race.winner]}**",
                        inline=False)
            e.add_field(name="Bet",     value=f"🪙 {bet:,}",    inline=True)
            e.add_field(name="Payout",  value=f"🪙 {payout:,}", inline=True)
            e.add_field(name="Profit",
                        value=f"{'+' if profit >= 0 else ''}🪙 {profit:,}", inline=True)
            e.add_field(name="Balance", value=f"🪙 {bal:,}",    inline=True)
            e.set_footer(text=f"{player.display_name}  •  Bey Race")

            view = RaceResultView(self, player, bet)
            try:
                await message.edit(embed=e, view=view)
                view.message = message
            except discord.DiscordException:
                pass
        finally:
            self._active.discard(player.id)

    @commands.command(name="beyrace", aliases=["race", "br"])
    async def beyrace(self, ctx: commands.Context, bet: int = 0):
        """🏁 Bet on a Beyblade to win the race. Usage: ;beyrace <bet>"""
        pid = ctx.author.id

        if pid in self._active:
            return await ctx.send("❌ You already have a race running!")

        max_bet = await casino_premium.get_max_bet(pid)
        if not (MIN_BET <= bet <= max_bet):
            return await ctx.send(f"❌ Bet must be 🪙 {MIN_BET:,}–{max_bet:,}.")

        if not await casino_wallet.deduct(pid, bet):
            return await ctx.send("❌ Not enough casino coins.")

        self._active.add(pid)
        race = Race()
        view = RaceLobbyView(self, ctx.author, bet, race)
        try:
            view.message = await ctx.send(embed=view.build_embed(), view=view)
        except discord.DiscordException:
            await casino_wallet.credit(pid, bet)
            self._active.discard(pid)


async def setup(bot: commands.Bot):
    await bot.add_cog(BeyRaceCog(bot))
