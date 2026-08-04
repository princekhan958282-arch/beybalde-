"""
cogs/extras/tournament.py
-------------------------
Single-elimination bracket tournaments using REAL battles.

Every command is a HYBRID command — it works both ways:

  /tournament create <size> [entry_fee] [mode]   ;tournament create 8 500
  /tournament join                               ;tournament join
  /tournament leave                              ;tournament leave
  /tournament begin                              ;tournament begin
  /tournament next                               ;tournament next
  /tournament show                               ;tournament  (or ;tournament bracket)
  /tournament cancel                             ;tournament cancel

Slash commands only appear after the tree is synced — app.py syncs on
startup, and ``;sync`` (master only) force-syncs the current guild.

Draft modes
-----------
  random   (default) — everyone is dealt a RANDOM bey at ``begin``, rarity
                       weighted, no duplicates while the pool allows it.
                       Nobody needs an equipped bey to join.
  equipped           — classic: each player fights with their own bey.

Either way the player's owned parts are still applied on top, so upgrades
keep mattering.

Every state change posts a rendered bracket CARD (utils/tournament_card.py)
showing the pot, the entry fee, the pairings and each player's drafted bey.
If rendering fails the cog silently falls back to the text embed.

State is in-memory per guild (a tournament is a live event; a restart
cancels it — fees are only at risk between begin and final, keep events
short).
"""
from __future__ import annotations

import asyncio
import random

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import get_user, update_user, get_beyblade, load_beyblades

try:
    from utils.tournament_card import render_tournament_card
except Exception:                                   # pragma: no cover
    render_tournament_card = None                   # type: ignore

VALID_SIZES = (2, 4, 8, 16)

# Rarity weights for the random draft — high tiers stay a lucky roll.
RARITY_WEIGHT = {
    "Common":    40,
    "Rare":      26,
    "Epic":      16,
    "Legendary":  9,
    "Mythic":     5,
    "Ultimate":   2,
    "Exclusive":  1,
}


def _draft_blades(count: int) -> list[str]:
    """Deal ``count`` rarity-weighted random bey names, avoiding duplicates
    while the pool is large enough. Never raises — falls back to a plain
    pool draw if the database is odd."""
    try:
        pool = list(load_beyblades().keys())
    except Exception:
        pool = []
    if not pool:
        return ["Unknown Bey"] * count

    weights = {}
    for name in pool:
        try:
            rar = (get_beyblade(name) or {}).get("rarity", "Common")
        except Exception:
            rar = "Common"
        weights[name] = RARITY_WEIGHT.get(rar, 10)

    picked: list[str] = []
    remaining = pool[:]
    for _ in range(count):
        if not remaining:                      # more players than beys
            remaining = pool[:]
        choice = random.choices(remaining, weights=[weights[n] for n in remaining], k=1)[0]
        picked.append(choice)
        remaining.remove(choice)
    return picked


# Coin-pot splits. Values are percentages for 1st / 2nd / 3rd.
# 3rd-place money is shared between BOTH losing semi-finalists.
SPLITS = {
    "winner":  (100,),
    "70-30":   (70, 30),
    "50-30-20": (50, 30, 20),
}


def _split_amounts(pot: int, split: str) -> list[int]:
    """Exact coin amounts per placement. The remainder from integer division
    is handed to 1st so the payout always sums to the pot — no coins created,
    none lost."""
    pct = SPLITS.get(split, SPLITS["winner"])
    amounts = [pot * p // 100 for p in pct]
    amounts[0] += pot - sum(amounts)
    return amounts


class _Tourney:
    def __init__(self, host_id: int, size: int, fee: int, channel_id: int,
                 random_beys: bool = True, prize: str = "", split: str = "winner"):
        self.host_id     = host_id
        self.size        = size
        self.fee         = max(0, fee)
        self.channel_id  = channel_id
        self.random_beys = random_beys
        # Free-text reward the host is putting up — a game top-up, a weekly
        # pass, a role, anything. Purely announced; the host hands it over.
        self.prize       = (prize or "").strip()
        self.split       = split if split in SPLITS else "winner"
        self.players:    list[int] = []
        self.started     = False
        self.pot         = 0
        self.round_no    = 0
        # current round: list of [p1_id, p2_id, winner_id|None]
        self.matches:    list[list] = []
        self.match_running = False
        # user_id → drafted bey name (random mode only)
        self.assigned:   dict[int, str] = {}
        # knocked-out players, oldest round first; last tier == the finalist
        self.elim_tiers: list[list[int]] = []


class TournamentCog(commands.Cog, name="Tournament"):
    """Single-elimination tournaments with entry fees, a winner pot and
    random bey drafting."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.tournaments: dict[int, _Tourney] = {}   # guild_id → tourney

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _t(self, ctx) -> "_Tourney | None":
        return self.tournaments.get(ctx.guild.id)

    @staticmethod
    def _mname(guild: discord.Guild, uid: int) -> str:
        m = guild.get_member(uid)
        return m.display_name if m else f"<{uid}>"

    def _blade_of(self, t: _Tourney, uid: int) -> str:
        """The bey this player is fighting with in this tournament."""
        if t.random_beys:
            return t.assigned.get(uid) or "—"
        try:
            return get_user(uid).get("active_beyblade") or "—"
        except Exception:
            return "—"

    def _bracket_text(self, guild: discord.Guild, t: _Tourney) -> str:
        lines = [f"**Round {t.round_no}**"]
        for i, (a, b, w) in enumerate(t.matches, 1):
            an = f"{self._mname(guild, a)} *({self._blade_of(t, a)})*"
            bn = (f"{self._mname(guild, b)} *({self._blade_of(t, b)})*"
                  if b else "— BYE —")
            if w:
                lines.append(f"`M{i}` {an} vs {bn} → 🏆 **{self._mname(guild, w)}**")
            else:
                lines.append(f"`M{i}` {an} vs {bn} — *pending*")
        return "\n".join(lines)

    @staticmethod
    def _split_label(t: _Tourney) -> str:
        return {"winner": "winner takes all",
                "70-30": "70 / 30",
                "50-30-20": "50 / 30 / 20"}.get(t.split, "winner takes all")

    def _payout_rows(self, t: _Tourney) -> list:
        """[(place, amount)] for the card ribbon. Empty when there is no pot."""
        if t.pot <= 0:
            return []
        names = ("1st", "2nd", "3rd")
        return [(names[i], f"{amt:,}")
                for i, amt in enumerate(_split_amounts(t.pot, t.split))]

    # ── Card rendering ────────────────────────────────────────────────────────

    def _card_payload(self, guild: discord.Guild, t: _Tourney) -> list[dict]:
        def side(uid):
            if uid is None:
                return None
            blade = self._blade_of(t, uid)
            try:
                rar = (get_beyblade(blade) or {}).get("rarity", "Common")
            except Exception:
                rar = "Common"
            return {"name": self._mname(guild, uid), "blade": blade, "rarity": rar}

        out = []
        for a, b, w in t.matches:
            winner = None if w is None else ("left" if w == a else "right")
            out.append({"left": side(a), "right": side(b), "winner": winner})
        return out

    async def _card_file(self, guild, t: _Tourney, *, champion: str | None = None):
        """Render the bracket card off the event loop. None on any failure."""
        if render_tournament_card is None or not t.matches:
            return None
        try:
            payload = self._card_payload(guild, t)
            buf = await asyncio.to_thread(
                render_tournament_card,
                f"Round {t.round_no}", payload, t.pot, t.fee, len(t.players),
                champion=champion,
                prize=t.prize,
                payout=self._payout_rows(t),
            )
            if buf is None:
                return None
            return discord.File(buf, filename="tournament.png")
        except Exception:
            return None

    async def _post_state(self, ctx, t: _Tourney, title: str, note: str = "",
                          champion: str | None = None) -> None:
        """Post the bracket card + embed. Card is attached when available."""
        card = await self._card_file(ctx.guild, t, champion=champion)
        embed = discord.Embed(
            title=title,
            description=self._bracket_text(ctx.guild, t) + (f"\n\n{note}" if note else ""),
            color=discord.Color.gold(),
        )
        if card is not None:
            embed.set_image(url="attachment://tournament.png")
            await ctx.send(embed=embed, file=card)
        else:
            await ctx.send(embed=embed)

    # ── Command group ─────────────────────────────────────────────────────────

    @commands.guild_only()
    @commands.hybrid_group(name="tournament", aliases=["tourney"], fallback="show",
                           invoke_without_command=True,
                           description="Bracket tournaments with random beys")
    async def tournament(self, ctx: commands.Context) -> None:
        """Show the current tournament / bracket."""
        t = self._t(ctx)
        if not t:
            return await ctx.send(
                "🏆 No active tournament. Start one:\n"
                "`/tournament create size:8 entry_fee:500`"
            )
        if t.started:
            await self._post_state(
                ctx, t, f"🏆 Tournament — Round {t.round_no}",
                f"🎁 Prize pool: **{t.prize or 'coin pot only'}**\n"
                f"💰 Pot: **{t.pot:,}** coins ({self._split_label(t)})")
        else:
            names = ", ".join(self._mname(ctx.guild, p) for p in t.players) or "*none yet*"
            mode = "🎲 Random beys" if t.random_beys else "⚔️ Equipped beys"
            await ctx.send(embed=discord.Embed(
                title=f"🏆 Tournament Signups ({len(t.players)}/{t.size})",
                description=(f"Mode: **{mode}**\n"
                             f"🎁 Prize pool: **{t.prize or 'coin pot only'}**\n"
                             f"Entry fee: **{t.fee:,}** 💰 | Pot: **{t.pot:,}** 💰 "
                             f"({self._split_label(t)})\n"
                             f"Players: {names}\n\n`/tournament join` to enter!"),
                color=discord.Color.gold()))

    @tournament.command(name="create", description="Open tournament signups")
    @app_commands.describe(size="Bracket size: 2, 4, 8 or 16",
                           entry_fee="Coins each player pays into the pot",
                           mode="random = everyone is dealt a random bey",
                           prize="Custom reward — anything you want to put up",
                           split="How the coin pot is divided")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="🎲 Random beys (everyone gets dealt one)", value="random"),
            app_commands.Choice(name="⚔️ Equipped beys (bring your own)", value="equipped"),
        ],
        split=[
            app_commands.Choice(name="🥇 Winner takes all", value="winner"),
            app_commands.Choice(name="🥈 70 / 30 — top two", value="70-30"),
            app_commands.Choice(name="🥉 50 / 30 / 20 — top three", value="50-30-20"),
        ],
    )
    async def create(self, ctx: commands.Context, size: int,
                     entry_fee: int = 0, mode: str = "random",
                     prize: str = "", split: str = "winner") -> None:
        if self._t(ctx):
            return await ctx.send("⚠️ A tournament is already active in this server.")
        if size not in VALID_SIZES:
            return await ctx.send(f"❌ Size must be one of: {', '.join(map(str, VALID_SIZES))}")
        if entry_fee < 0:
            return await ctx.send("❌ Entry fee can't be negative.")
        if len(prize) > 120:
            return await ctx.send("❌ Keep the prize description under 120 characters.")
        rnd = str(mode).lower() != "equipped"
        t = _Tourney(ctx.author.id, size, entry_fee, ctx.channel.id,
                     random_beys=rnd, prize=prize, split=split)
        self.tournaments[ctx.guild.id] = t
        await ctx.send(embed=discord.Embed(
            title="🏆 TOURNAMENT OPEN!",
            description=(
                f"Host: {ctx.author.mention}\n"
                f"Bracket size: **{size}** players\n"
                f"Entry fee: **{max(0, entry_fee):,}** 💰 (into the coin pot)\n"
                f"Prize pool: **{t.prize or 'coin pot only'}**\n"
                f"Coin split: **{self._split_label(t)}**\n"
                f"Mode: **{'🎲 Random beys — dealt at start' if rnd else '⚔️ Equipped beys'}**\n\n"
                f"⚔️ `/tournament join` to enter!"
            ),
            color=discord.Color.gold()))

    @tournament.command(name="join", description="Join the open tournament")
    async def join(self, ctx: commands.Context) -> None:
        t = self._t(ctx)
        if not t:
            return await ctx.send("❌ No open tournament. `/tournament create size:8`")
        if t.started:
            return await ctx.send("❌ The tournament has already started.")
        if ctx.author.id in t.players:
            return await ctx.send("⚠️ You're already signed up!")
        if len(t.players) >= t.size:
            return await ctx.send("❌ The bracket is full!")
        profile = get_user(ctx.author.id)
        # Random mode deals the bey at begin, so no equipped bey is required.
        if not t.random_beys and not profile.get("active_beyblade"):
            return await ctx.send("❌ Equip a Beyblade first: `;equip <name>`")
        if t.fee > 0:
            if profile.get("coins", 0) < t.fee:
                return await ctx.send(f"❌ Entry fee is **{t.fee:,}** 💰 — not enough coins.")
            profile["coins"] -= t.fee
            update_user(ctx.author.id, profile)
            t.pot += t.fee
        t.players.append(ctx.author.id)
        await ctx.send(
            f"✅ **{ctx.author.display_name}** joined! "
            f"({len(t.players)}/{t.size}) — Pot: **{t.pot:,}** 💰"
        )

    @tournament.command(name="leave", description="Leave before it starts (fee refunded)")
    async def leave(self, ctx: commands.Context) -> None:
        t = self._t(ctx)
        if not t or t.started or ctx.author.id not in t.players:
            return await ctx.send("❌ You're not in an open tournament.")
        t.players.remove(ctx.author.id)
        if t.fee > 0:
            profile = get_user(ctx.author.id)
            profile["coins"] = profile.get("coins", 0) + t.fee
            update_user(ctx.author.id, profile)
            t.pot -= t.fee
        await ctx.send(f"👋 **{ctx.author.display_name}** left the tournament (fee refunded).")

    @tournament.command(name="begin",
                        description="(host) Lock signups, deal beys, build the bracket")
    async def begin(self, ctx: commands.Context) -> None:
        t = self._t(ctx)
        if not t:
            return await ctx.send("❌ No tournament to begin.")
        if ctx.author.id != t.host_id and not ctx.author.guild_permissions.administrator:
            return await ctx.send("❌ Only the host (or an admin) can begin.")
        if t.started:
            return await ctx.send("⚠️ Already started — use `/tournament next`.")
        if len(t.players) < 2:
            return await ctx.send("❌ Need at least 2 players.")

        t.started  = True
        t.round_no = 1

        # ── Random bey draft ─────────────────────────────────────────────────
        if t.random_beys:
            drafted = _draft_blades(len(t.players))
            t.assigned = dict(zip(t.players, drafted))

        pool = t.players[:]
        random.shuffle(pool)
        # Seed byes properly. Padding the pool with None and pairing blindly
        # produced a None-vs-None ghost match whenever the entrant count sat
        # more than one slot below the power of two (e.g. 5 players → 8 slots
        # → 3 Nones → one all-bye match). Instead: give the first `byes`
        # players a straight auto-advance and pair the remainder, which always
        # leaves exactly n/2 winners.
        n = 1
        while n < len(pool):
            n *= 2
        byes = n - len(pool)
        advancing, contested = pool[:byes], pool[byes:]
        t.matches = [[uid, None, uid] for uid in advancing]
        t.matches += [[contested[i], contested[i + 1], None]
                      for i in range(0, len(contested), 2)]

        if t.random_beys:
            roll = "\n".join(
                f"🎲 **{self._mname(ctx.guild, uid)}** → **{blade}**"
                for uid, blade in t.assigned.items())
            await ctx.send(embed=discord.Embed(
                title="🎲 RANDOM BEY DRAFT",
                description=roll + "\n\n*Your owned parts are still applied on top.*",
                color=discord.Color.blurple()))

        await self._post_state(
            ctx, t, "🏆 TOURNAMENT BEGINS!",
            f"🎁 Prize pool: **{t.prize or 'coin pot only'}**\n"
            f"💰 Pot: **{t.pot:,}** 💰 ({self._split_label(t)})\n"
            f"Host: `/tournament next` to run the first match!")

    @tournament.command(name="next", description="(host) Run the next pending match")
    async def next_match(self, ctx: commands.Context) -> None:
        t = self._t(ctx)
        if not t or not t.started:
            return await ctx.send("❌ No running tournament.")
        if ctx.author.id != t.host_id and not ctx.author.guild_permissions.administrator:
            return await ctx.send("❌ Only the host (or an admin) can run matches.")
        if t.match_running:
            return await ctx.send("⚠️ A match is already in progress!")

        # Slash: a real battle takes minutes, so answer the interaction now.
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            try:
                await ctx.defer()
            except Exception:
                pass

        pending = next((m for m in t.matches if m[2] is None), None)
        if pending is None:
            return await self._advance_round(ctx, t)

        a_id, b_id, _ = pending
        p1 = ctx.guild.get_member(a_id)
        p2 = ctx.guild.get_member(b_id)
        if not p1 or not p2:
            # Missing member forfeits
            pending[2] = a_id if p2 is None else b_id
            await ctx.send("⚠️ A player left the server — opponent advances by forfeit.")
            return await self._maybe_advance(ctx, t)

        # Build blades exactly like a normal battle
        try:
            from cogs.battle.battle import _apply_parts
            from cogs.battle.session import BattleSession
        except Exception:
            return await ctx.send("❌ Battle system unavailable.")

        prof1, prof2 = get_user(p1.id), get_user(p2.id)
        raw1 = get_beyblade(self._blade_of(t, a_id))
        raw2 = get_beyblade(self._blade_of(t, b_id))
        if not raw1 or not raw2:
            missing = p1 if not raw1 else p2
            other   = b_id if not raw1 else a_id
            pending[2] = other
            await ctx.send(f"⚠️ {missing.mention} has no valid bey — opponent advances!")
            return await self._maybe_advance(ctx, t)

        blade1 = _apply_parts(raw1, prof1)
        blade2 = _apply_parts(raw2, prof2)

        await ctx.send(embed=discord.Embed(
            title=f"⚔️ TOURNAMENT MATCH — Round {t.round_no}",
            description=f"{p1.mention} (**{blade1['name']}**) vs {p2.mention} (**{blade2['name']}**)\n"
                        f"Battle starting now — choose your moves!",
            color=discord.Color.red()))

        t.match_running = True
        # Register in BattleCog.active_battles so players can't start a
        # parallel ;battle mid-match (two sessions would fight over the
        # same player's button presses).
        bc = self.bot.get_cog("Battle")
        ab = getattr(bc, "active_battles", None) if bc else None
        if ab is not None and (a_id in ab or b_id in ab):
            t.match_running = False
            return await ctx.send(
                "⚠️ A player is currently in another battle — finish it first, "
                "then `/tournament next`.")
        try:
            session = BattleSession(bot=self.bot, channel=ctx.channel,
                                    p1=p1, p2=p2, blade1=blade1, blade2=blade2)
            if ab is not None:
                ab[a_id] = session
                ab[b_id] = session
            await session.run()
            winner_id = getattr(session, "winner_id", None)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            winner_id = None
            await ctx.send(f"⚠️ Match crashed: `{type(exc).__name__}: {exc}`")
        finally:
            t.match_running = False
            if ab is not None:
                ab.pop(a_id, None)
                ab.pop(b_id, None)

        if winner_id is None:
            # Draw / error → coin-flip advance so the bracket never stalls
            winner_id = random.choice([a_id, b_id])
            await ctx.send("🎲 Draw! Winner decided by launcher toss…")
        pending[2] = winner_id
        await ctx.send(f"🏆 **{self._mname(ctx.guild, winner_id)}** advances!")
        await self._maybe_advance(ctx, t)

    async def _maybe_advance(self, ctx: commands.Context, t: _Tourney) -> None:
        if all(m[2] is not None for m in t.matches):
            await self._advance_round(ctx, t)
        else:
            await self._post_state(ctx, t, f"🏆 Round {t.round_no} — Bracket",
                                   "▶️ Host: `/tournament next` for the next match.")

    async def _advance_round(self, ctx: commands.Context, t: _Tourney) -> None:
        winners = [m[2] for m in t.matches]

        # Record who was knocked out this round so 2nd / 3rd place can be
        # paid. Byes have no loser, so they contribute nothing.
        losers = [(a if w == b else b)
                  for a, b, w in t.matches if b is not None and w is not None]
        if losers:
            t.elim_tiers.append(losers)

        if len(winners) == 1:
            champ_id   = winners[0]
            champ_name = self._mname(ctx.guild, champ_id)
            champ_bey  = self._blade_of(t, champ_id)
            lines = await self._pay_out(ctx.guild, t, champ_id)

            card  = await self._card_file(ctx.guild, t, champion=champ_name)
            desc  = f"🏆 **{champ_name}** wins the tournament with **{champ_bey}**!"
            if t.prize:
                desc += f"\n\n🎁 **Prize pool: {t.prize}**\n*Host: hand this over to the champion.*"
            if lines:
                desc += "\n\n**💰 Coin payout**\n" + "\n".join(lines)
            embed = discord.Embed(title="👑 TOURNAMENT CHAMPION!",
                                  description=desc, color=discord.Color.gold())
            self.tournaments.pop(ctx.guild.id, None)
            if card is not None:
                embed.set_image(url="attachment://tournament.png")
                return await ctx.send(embed=embed, file=card)
            return await ctx.send(embed=embed)

        t.round_no += 1
        t.matches = [[winners[i], winners[i + 1], None] for i in range(0, len(winners), 2)]
        await self._post_state(ctx, t, f"🏆 Round {t.round_no}!",
                               "Host: `/tournament next`")

    async def _pay_out(self, guild, t: _Tourney, champ_id: int) -> list[str]:
        """Pay the coin pot out across the configured placements.

        Placement tiers come from ``elim_tiers``: the last tier is the beaten
        finalist (2nd), the one before it the beaten semi-finalists (joint
        3rd, who share that slice). Any slice with nobody to award — a 3-place
        split in a 2-player bracket, say — rolls back to the champion so the
        pot is never silently destroyed.
        """
        if t.pot <= 0:
            return []
        amounts = _split_amounts(t.pot, t.split)

        tiers: list[list[int]] = [[champ_id]]
        if len(amounts) > 1:
            tiers.append(t.elim_tiers[-1] if len(t.elim_tiers) >= 1 else [])
        if len(amounts) > 2:
            tiers.append(t.elim_tiers[-2] if len(t.elim_tiers) >= 2 else [])

        # Unclaimed slices roll up to the champion.
        for i in range(len(amounts) - 1, 0, -1):
            if not tiers[i]:
                amounts[0] += amounts[i]
                amounts[i] = 0

        labels = ("🥇 1st", "🥈 2nd", "🥉 3rd")
        lines: list[str] = []
        for i, group in enumerate(tiers):
            if not group or amounts[i] <= 0:
                continue
            each = amounts[i] // len(group)
            extra = amounts[i] - each * len(group)   # remainder to the first
            for j, uid in enumerate(group):
                share = each + (extra if j == 0 else 0)
                if share <= 0:
                    continue
                try:
                    prof = get_user(uid)
                    prof["coins"] = prof.get("coins", 0) + share
                    update_user(uid, prof)
                except Exception:
                    continue
                lines.append(f"{labels[i]} **{self._mname(guild, uid)}** — {share:,} 💰")
        return lines

    @tournament.command(name="bracket", description="Show the bracket card")
    async def bracket(self, ctx: commands.Context) -> None:
        await self.tournament(ctx)

    @tournament.command(name="cancel", description="(host/admin) Cancel + refund fees")
    async def cancel(self, ctx: commands.Context) -> None:
        t = self._t(ctx)
        if not t:
            return await ctx.send("❌ No tournament to cancel.")
        if ctx.author.id != t.host_id and not ctx.author.guild_permissions.administrator:
            return await ctx.send("❌ Only the host (or an admin) can cancel.")
        if t.fee > 0:
            for pid in t.players:
                try:
                    prof = get_user(pid)
                    prof["coins"] = prof.get("coins", 0) + t.fee
                    update_user(pid, prof)
                except Exception:
                    continue
        self.tournaments.pop(ctx.guild.id, None)
        await ctx.send("🛑 Tournament cancelled — all entry fees refunded.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TournamentCog(bot))
