"""
crash.py  —  📈 Crash (multiplayer)

A shared round per channel. The multiplier climbs; cash out before it crashes.
Everyone in the channel plays the same round off the same message.

How a round goes:
  1. Someone runs  ;crash <bet>  (or /crash) — this opens a lobby.
  2. Anyone else joins with the Join button or by running the command too.
  3. After the betting window the multiplier starts climbing.
  4. Each player hits Cash Out whenever they like. Whatever is still riding
     when it crashes is lost.

Rounds are keyed by channel, run in their own background task, and refund
everyone if anything goes wrong. House edge ~3% (RTP ≈ 97%).
"""

import asyncio
import logging
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from . import casino_premium, casino_wallet

log = logging.getLogger("beyblade_bot")

MIN_BET       = 10
LOBBY_SECONDS = 15      # betting window before the multiplier starts
TICK_DELAY    = 1.1     # seconds between steps (stays under Discord's
                        # 5-edits-per-5s message rate limit)
MAX_PLAYERS   = 25


def _gen_crash() -> float:
    """Crash point with a ~3% house edge (RTP ≈ 97% at any cash-out target)."""
    r = random.random()
    if r < 0.03:
        return 1.0                       # instant crash
    return round(min(0.97 / (1 - r), 1000.0), 2)


MULTIPLIER_STEPS = [
    1.0, 1.1, 1.2, 1.3, 1.5, 1.7, 2.0, 2.5, 3.0, 4.0,
    5.0, 6.0, 7.5, 10.0, 12.0, 15.0, 20.0, 25.0, 30.0,
    40.0, 50.0, 75.0, 100.0,
]


def _steps_up_to(crash: float) -> list[float]:
    return [s for s in MULTIPLIER_STEPS if s < crash]


class CrashRound:
    """One round, scoped to one channel."""

    LOBBY, RUNNING, DONE = "lobby", "running", "done"

    def __init__(self, channel_id: int):
        self.channel_id   = channel_id
        self.crash_point  = _gen_crash()
        self.current_mult = 1.0
        self.state        = self.LOBBY
        self.bets: dict[int, dict] = {}     # uid -> {amount, user, cashed_out, cash_mult}
        self.lock         = asyncio.Lock()
        self.opened_at    = time.time()
        self.settled      = False

    @property
    def running(self) -> bool:
        return self.state == self.RUNNING

    @property
    def crashed(self) -> bool:
        return self.state == self.DONE

    @property
    def seconds_left(self) -> int:
        return max(0, int(LOBBY_SECONDS - (time.time() - self.opened_at)))

    @property
    def total_staked(self) -> int:
        return sum(b["amount"] for b in self.bets.values())

    def place_bet(self, uid: int, user, amount: int) -> None:
        self.bets[uid] = {"amount": amount, "user": user,
                          "cashed_out": False, "cash_mult": None}

    def cashout(self, uid: int) -> Optional[int]:
        b = self.bets.get(uid)
        if not b or b["cashed_out"]:
            return None
        b["cashed_out"] = True
        b["cash_mult"]  = self.current_mult
        return int(b["amount"] * self.current_mult)


class JoinBetModal(discord.ui.Modal, title="Join the Crash round"):
    amount = discord.ui.TextInput(label="Bet (casino coins)",
                                  placeholder="500", required=True, max_length=12)

    def __init__(self, view: "CrashView"):
        super().__init__()
        self.view_ref = view

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

        ok, msg = await self.view_ref.cog.add_player(
            self.view_ref.round, interaction.user, bet)
        await interaction.response.send_message(msg, ephemeral=True)
        if ok:
            await self.view_ref.refresh(force=True)


class CrashView(discord.ui.View):
    # A round runs ~40s at most; 5 min is a safety net, not a game timer.
    def __init__(self, cog, rnd: CrashRound):
        super().__init__(timeout=300)
        self.cog   = cog
        self.round = rnd
        self.message: Optional[discord.Message] = None

    def build_embed(self, final: bool = False) -> discord.Embed:
        r    = self.round
        mult = r.current_mult

        if r.crashed:
            colour = 0xe74c3c
            title  = f"💥  CRASHED at {r.crash_point:.2f}x"
        elif r.state == CrashRound.LOBBY:
            colour = 0x3498db
            title  = "📈  Crash — betting open"
        elif mult >= 10:
            colour, title = 0x9b59b6, f"🚀  {mult:.2f}x  🔥"
        elif mult >= 3:
            colour, title = 0xf39c12, f"📈  {mult:.2f}x"
        else:
            colour, title = 0x2ecc71, f"🟢  {mult:.2f}x"

        e = discord.Embed(title=title, color=colour)

        if r.state == CrashRound.LOBBY:
            e.description = (
                f"⏳ Round starts in **{r.seconds_left}s**\n"
                f"Press **➕ Join** or run `;crash <bet>` to get in."
            )

        if r.bets:
            lines = []
            for b in sorted(r.bets.values(), key=lambda x: -x["amount"]):
                name = b["user"].display_name
                if b["cashed_out"]:
                    pay    = int(b["amount"] * b["cash_mult"])
                    profit = pay - b["amount"]
                    lines.append(f"✅ **{name}** — out at {b['cash_mult']:.2f}x "
                                 f"→ 🪙 {pay:,} (+{profit:,})")
                elif r.crashed:
                    lines.append(f"💥 **{name}** — lost 🪙 {b['amount']:,}")
                elif r.running:
                    lines.append(f"🎯 **{name}** — 🪙 {b['amount']:,} "
                                 f"→ 🪙 {int(b['amount'] * mult):,} riding")
                else:
                    lines.append(f"🎯 **{name}** — 🪙 {b['amount']:,}")
            text = "\n".join(lines)
            if len(text) > 1000:
                text = "\n".join(lines[:12]) + f"\n…and {len(lines) - 12} more"
            e.add_field(name=f"Players ({len(r.bets)})", value=text, inline=False)
            e.add_field(name="Total staked", value=f"🪙 {r.total_staked:,}", inline=True)

        if r.crashed:
            cashed = sum(1 for b in r.bets.values() if b["cashed_out"])
            e.set_footer(text=f"{cashed}/{len(r.bets)} cashed out in time  •  "
                              f"run ;crash <bet> for a new round")
        elif r.running:
            e.set_footer(text="Hit Cash Out before it blows!")
        return e

    @discord.ui.button(label="Cash Out", emoji="💰", style=discord.ButtonStyle.success)
    async def cashout_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        r   = self.round
        uid = interaction.user.id
        async with r.lock:
            if uid not in r.bets:
                return await interaction.response.send_message(
                    "You're not in this round. Press **➕ Join**!", ephemeral=True)
            if r.state == CrashRound.LOBBY:
                return await interaction.response.send_message(
                    "Round hasn't started yet — hang on.", ephemeral=True)
            if r.crashed:
                return await interaction.response.send_message(
                    "💥 Already crashed — too late!", ephemeral=True)
            if r.bets[uid]["cashed_out"]:
                return await interaction.response.send_message(
                    "You already cashed out.", ephemeral=True)

            payout = r.cashout(uid)
            mult   = r.bets[uid]["cash_mult"]
            stake  = r.bets[uid]["amount"]

        if payout:
            await casino_wallet.credit(uid, payout)
            await interaction.response.send_message(
                f"✅ Cashed out at **{mult:.2f}x** → 🪙 {payout:,} "
                f"(+{payout - stake:,})", ephemeral=True)
            # No edit here on purpose — the tick loop refreshes within ~1s.
        else:
            await interaction.response.send_message("Already cashed out.", ephemeral=True)

    async def on_timeout(self):
        self.lock_buttons()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="Join", emoji="➕", style=discord.ButtonStyle.primary)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        r = self.round
        if r.state != CrashRound.LOBBY:
            return await interaction.response.send_message(
                "Betting is closed for this round — wait for the next one.", ephemeral=True)
        if interaction.user.id in r.bets:
            return await interaction.response.send_message(
                "You're already in this round.", ephemeral=True)
        await interaction.response.send_modal(JoinBetModal(self))

    def lock_buttons(self):
        for c in self.children:
            c.disabled = True

    async def refresh(self, final: bool = False, force: bool = False):
        """Edit the round message.

        While the multiplier is climbing the tick loop already edits roughly
        once a second, so cash-outs skip their own edit — otherwise a busy
        round would blow straight through Discord's edit rate limit.
        """
        if not self.message:
            return
        if not force and self.round.running and not self.round.crashed:
            return
        try:
            await self.message.edit(embed=self.build_embed(final), view=self)
        except Exception:
            pass


class CrashCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._rounds: dict[int, CrashRound]   = {}    # channel_id -> round
        self._locks:  dict[int, asyncio.Lock] = {}    # channel_id -> lock

    def _chan_lock(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._locks:
            self._locks[channel_id] = asyncio.Lock()
        return self._locks[channel_id]

    async def add_player(self, rnd: CrashRound, user, bet: int) -> tuple[bool, str]:
        """Validate, charge and seat a player. Returns (ok, message)."""
        max_bet = await casino_premium.get_max_bet(user.id)
        if not (MIN_BET <= bet <= max_bet):
            return False, f"❌ Bet must be 🪙 {MIN_BET:,}–{max_bet:,}."

        async with rnd.lock:
            if rnd.state != CrashRound.LOBBY:
                return False, "❌ Betting is closed for this round."
            if user.id in rnd.bets:
                return False, "❌ You're already in this round."
            if len(rnd.bets) >= MAX_PLAYERS:
                return False, f"❌ Round is full ({MAX_PLAYERS} players)."
            if not await casino_wallet.deduct(user.id, bet):
                return False, "❌ Not enough casino coins."
            rnd.place_bet(user.id, user, bet)

        return True, (f"✅ You're in with 🪙 {bet:,} — "
                      f"round starts in ~{rnd.seconds_left}s. Watch the message!")

    async def _run_round(self, rnd: CrashRound, view: CrashView):
        """Owns the whole life of a round. Always settles, even on error."""
        try:
            deadline = rnd.opened_at + LOBBY_SECONDS
            while time.time() < deadline:
                await asyncio.sleep(min(3.0, max(0.5, deadline - time.time())))
                if rnd.state == CrashRound.LOBBY:
                    await view.refresh(force=True)

            async with rnd.lock:
                if not rnd.bets:
                    rnd.state   = CrashRound.DONE
                    rnd.settled = True
                    view.lock_buttons()
                    e = discord.Embed(title="😴  Round cancelled",
                                      description="Nobody placed a bet.", color=0x95a5a6)
                    if view.message:
                        try:
                            await view.message.edit(embed=e, view=view)
                        except Exception:
                            pass
                    view.stop()
                    return
                rnd.state = CrashRound.RUNNING
                for c in view.children:
                    if getattr(c, "label", None) == "Join":
                        c.disabled = True

            for step in _steps_up_to(rnd.crash_point):
                rnd.current_mult = step
                await view.refresh(force=True)
                await asyncio.sleep(TICK_DELAY)

            async with rnd.lock:
                rnd.state        = CrashRound.DONE
                rnd.current_mult = rnd.crash_point
                rnd.settled      = True
            view.lock_buttons()
            await view.refresh(final=True, force=True)
            view.stop()          # release the view instead of leaking it

            losers = [b for b in rnd.bets.values() if not b["cashed_out"]]
            if losers and view.message:
                try:
                    await view.message.channel.send(
                        f"💥 Crashed at **{rnd.crash_point:.2f}x** — "
                        f"🪙 {sum(b['amount'] for b in losers):,} went up in smoke. "
                        f"`;crash <bet>` for the next round."
                    )
                except Exception:
                    pass

        except asyncio.CancelledError:
            await self._emergency_refund(rnd, view)
            raise
        except Exception as exc:
            log.error(f"[crash] round failed in channel {rnd.channel_id}: {exc}",
                      exc_info=True)
            await self._emergency_refund(rnd, view)
        finally:
            if self._rounds.get(rnd.channel_id) is rnd:
                self._rounds.pop(rnd.channel_id, None)

    async def _emergency_refund(self, rnd: CrashRound, view: CrashView):
        """Give everyone who hadn't cashed out their stake back."""
        async with rnd.lock:
            if rnd.settled:
                return
            rnd.settled = True
            rnd.state   = CrashRound.DONE
            refunds = [(uid, b["amount"]) for uid, b in rnd.bets.items()
                       if not b["cashed_out"]]
        for uid, amount in refunds:
            await casino_wallet.credit(uid, amount)
        view.lock_buttons()
        if view.message:
            try:
                await view.message.edit(
                    embed=discord.Embed(
                        title="⚠️  Round aborted",
                        description="Something went wrong — every open bet was refunded.",
                        color=0xe67e22),
                    view=view)
            except Exception:
                pass
        view.stop()

    async def _enter(self, channel, user, bet: int) -> tuple[bool, str]:
        """Join the channel's round, opening one if needed. Returns (ok, message)."""
        async with self._chan_lock(channel.id):
            rnd = self._rounds.get(channel.id)

            if rnd is not None and rnd.state == CrashRound.LOBBY:
                return await self.add_player(rnd, user, bet)

            if rnd is not None and rnd.state == CrashRound.RUNNING:
                return False, ("⏳ A round is already climbing in this channel — "
                               "wait for it to crash, then run `;crash <bet>` again.")

            max_bet = await casino_premium.get_max_bet(user.id)
            if not (MIN_BET <= bet <= max_bet):
                return False, f"❌ Bet must be 🪙 {MIN_BET:,}–{max_bet:,}."

            rnd = CrashRound(channel.id)
            self._rounds[channel.id] = rnd     # claim the slot before any await

            if not await casino_wallet.deduct(user.id, bet):
                self._rounds.pop(channel.id, None)
                return False, "❌ Not enough casino coins."
            rnd.place_bet(user.id, user, bet)

            view = CrashView(self, rnd)
            try:
                view.message = await channel.send(
                    f"📈 **{user.display_name}** started a Crash round — "
                    f"**{LOBBY_SECONDS}s** to join!",
                    embed=view.build_embed(), view=view)
            except discord.DiscordException:
                await casino_wallet.credit(user.id, bet)
                self._rounds.pop(channel.id, None)
                return False, "❌ Couldn't post the round here."

            asyncio.create_task(self._run_round(rnd, view))

        return True, f"✅ Round opened with your 🪙 {bet:,} bet — good luck!"

    @commands.command(name="crash")
    async def crash_prefix(self, ctx: commands.Context, bet: int = 0):
        """📈 Multiplayer crash. Usage: ;crash <bet>"""
        if bet <= 0:
            return await ctx.send("❌ Usage: `;crash <bet>`")
        ok, msg = await self._enter(ctx.channel, ctx.author, bet)
        if not ok:
            await ctx.send(msg)
        else:
            try:
                await ctx.message.add_reaction("✅")
            except Exception:
                pass

    # Retired as a top-level slash command — reachable as `/casino play`
    # with game "crash". The `;crash` prefix command is untouched.
    async def crash(self, interaction: discord.Interaction, bet: int):
        ok, msg = await self._enter(interaction.channel, interaction.user, bet)
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CrashCog(bot))
