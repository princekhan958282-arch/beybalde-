"""
cog.py — the Discord surface.

Deliberately thin: every rule lives in service.py, so this file only validates
Discord-shaped input, renders results and runs the background loop. That split
is what lets the whole lifecycle be tested without a gateway connection.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import brackets, notifications as notify, timeslots as ts, ui_v2
from .views import AnnouncementComposer, RSVPCheckView, RSVPView, TournamentCard
from .models import Mode, MatchState, TournamentState
from .service import (CHECKIN_WINDOW_MIN, MATCH_DURATION_MIN, TournamentService)
from .store import Store

log = logging.getLogger("beyblade_bot.tournament")

MASTER_ID = 956773141265391676
ADMIN_ROLE = "Tournament Admin"
COLOUR = 0x5865F2

MODE_CHOICES = [
    app_commands.Choice(name="Single elimination", value=Mode.SINGLE.value),
    app_commands.Choice(name="Double elimination", value=Mode.DOUBLE.value),
    app_commands.Choice(name="Round robin", value=Mode.ROUND_ROBIN.value),
]


def is_tournament_admin(user) -> bool:
    """Owner, or anyone holding the admin role. Checked on EVERY admin command
    rather than once at registration, because roles change mid-tournament."""
    if getattr(user, "id", None) == MASTER_ID:
        return True
    roles = getattr(user, "roles", None) or []
    return any(getattr(r, "name", "") == ADMIN_ROLE for r in roles)


async def deny(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("Not authorized.", ephemeral=True)


# ── Registration modal ────────────────────────────────────────────────────────

class RegisterModal(discord.ui.Modal, title="Tournament registration"):
    country = discord.ui.TextInput(label="Country", placeholder="India",
                                   max_length=60, required=True)
    offset = discord.ui.TextInput(label="UTC offset", placeholder="+5:30",
                                  max_length=10, required=True)
    availability = discord.ui.TextInput(
        label="Weekly availability",
        style=discord.TextStyle.paragraph,
        placeholder="Mon 18:00-22:00\nSat 10:00-14:00",
        required=True, max_length=400)

    def __init__(self, cog: "TournamentCog", tournament_id: str = ""):
        super().__init__()
        self.cog = cog
        self.tournament_id = tournament_id

    async def on_submit(self, interaction: discord.Interaction):
        slots, bad = [], []
        for raw in str(self.availability).splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.replace("—", "-").replace("to", "-").split()
            if len(parts) < 2:
                bad.append(line)
                continue
            day, _, span = parts[0], None, "".join(parts[1:])
            if "-" not in span:
                bad.append(line)
                continue
            start, _, end = span.partition("-")
            slots.append({"day": day, "start": start, "end": end})
        if bad:
            return await interaction.response.send_message(
                "Couldn't read: " + ", ".join(f"`{b}`" for b in bad[:3])
                + "\nUse one slot per line, like `Mon 18:00-22:00`.",
                ephemeral=True)

        ok, msg, player = self.cog.svc.register(
            interaction.user.id, str(self.country), str(self.offset), slots)
        if not ok:
            return await interaction.response.send_message(f"❌ {msg}",
                                                           ephemeral=True)
        lines = "\n".join(f"• {s['day']} {s['start']}–{s['end']}"
                          for s in player.slots)
        e = discord.Embed(title="✅ Registered", colour=COLOUR,
                          description=f"**{player.country}** · UTC"
                                      f"{player.utc_offset:+g}\n{lines}")
        if self.tournament_id:
            ok2, msg2, _ = self.cog.svc.join(self.tournament_id,
                                             interaction.user.id)
            e.add_field(name="Tournament", value=("✅ " if ok2 else "❌ ") + msg2,
                        inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)


# ── Check-in button ───────────────────────────────────────────────────────────

class CheckinView(discord.ui.View):
    """Check-in button, built to survive a restart.

    The match id lives in the button's custom_id, not just on the instance.
    A check-in window is only ten minutes, but a deploy landing inside one
    would otherwise turn every button in flight into "interaction failed" —
    and the player forfeits for a restart that wasn't their fault. On restart
    `cog_load` registers a bare instance; the id is read back off the click.
    """

    def __init__(self, cog: "TournamentCog", match_id: str = ""):
        super().__init__(timeout=None)
        self.cog = cog
        self.match_id = match_id
        for child in self.children:
            if isinstance(child, discord.ui.Button) and match_id:
                child.custom_id = f"tourney:checkin:{match_id}"

    @discord.ui.button(label="Check in", emoji="✅",
                       style=discord.ButtonStyle.success,
                       custom_id="tourney:checkin")
    async def checkin(self, interaction: discord.Interaction, _btn):
        mid = self.match_id
        if not mid:
            raw = str((interaction.data or {}).get("custom_id", ""))
            mid = raw.split(":")[-1] if raw.count(":") >= 2 else ""
        if not mid:
            return await interaction.response.send_message(
                "I couldn't tell which match this was for — use "
                "`/match checkin <match_id>`.", ephemeral=True)
        ok, msg, _ = self.cog.svc.checkin(mid, interaction.user.id)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + msg,
                                                ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class TournamentCog(commands.Cog, name="Tournaments"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.svc = TournamentService(Store())
        self.scheduler.start()

    async def cog_load(self) -> None:
        """Re-register the views that outlive a restart.

        Discord's persistent-view registration matches a component's custom_id
        EXACTLY — it is not a prefix match. A single bare `CheckinView(self)`
        (custom_id "tourney:checkin") therefore never routes a click on the
        real button a player sees (custom_id "tourney:checkin:m_xyz") no
        matter how the id is read back afterwards; that was a false fix. The
        working pattern, used consistently below, is to enumerate the actual
        live entities and register one correctly-configured view per entity —
        exactly what TournamentCard already did correctly. Each set is
        naturally small: only cards for still-open tournaments, check-in
        buttons for matches that haven't finished, and RSVP buttons for
        announcements sent in roughly the last week.
        """
        for t in self.svc.store.active_tournaments():
            try:
                self.bot.add_view(TournamentCard(self, t.id))
            except Exception as e:                   # noqa: BLE001
                # A LayoutView may not be registrable on older discord.py;
                # one bad card must not stop the cog loading.
                log.warning("could not register card for %s: %s", t.id, e)

        live = self.svc.store.matches_in_states(
            [MatchState.SCHEDULED.value, MatchState.CHECKIN.value])
        for m in live:
            try:
                self.bot.add_view(CheckinView(self, m.id))
            except Exception as e:                   # noqa: BLE001
                log.warning("could not register check-in for %s: %s", m.id, e)

        recent = self.svc.store.recent_dm_announcements(time.time() - 7 * 86400)
        for ann in recent:
            try:
                self.bot.add_view(RSVPView(self, ann["id"]))
                self.bot.add_view(RSVPCheckView(self, ann["id"]))
            except Exception as e:                   # noqa: BLE001
                log.warning("could not register RSVP for %s: %s", ann["id"], e)

        if live or recent:
            log.info("🔁 Re-registered %d check-in and %d announcement view(s).",
                     len(live), len(recent))

    def cog_unload(self):
        self.scheduler.cancel()

    # ── background loop ──────────────────────────────────────────────────────

    @tasks.loop(seconds=60)
    async def scheduler(self):
        """Drives check-in windows and no-show forfeits.

        Everything it does is derived from stored state, so a missed run or a
        crash costs nothing — the next tick reads the same data and catches up.
        """
        try:
            events = self.svc.tick()
        except Exception as e:                       # noqa: BLE001
            log.exception("tournament tick failed: %s", e)
            return

        for m in events["checkin_open"]:
            t = self.svc.store.get_tournament(m.tournament_id)
            players = self.svc.store.get_players(m.players)
            for uid in m.players:
                p = players.get(uid)
                when = (ts.local_str(m.scheduled_for, p.utc_offset) if p
                        else ts.discord_ts(m.scheduled_for))
                await notify.dm(
                    self.bot, uid,
                    f"⏰ Check-in is open for your match `{m.id}` — "
                    f"starts **{when}**.\nYou have {CHECKIN_WINDOW_MIN} minutes "
                    f"or you forfeit.",
                    view=CheckinView(self, m.id))
            if t:
                await notify.announce(
                    self.bot, t.channel_id,
                    f"⏰ Check-in open: <@{m.player_a}> vs <@{m.player_b}> "
                    f"— {ts.discord_ts(m.scheduled_for, 'R')}")

        for m in events["forfeit"]:
            t = self.svc.store.get_tournament(m.tournament_id)
            if t and m.winner:
                await notify.announce(
                    self.bot, t.channel_id,
                    f"🚫 <@{m.loser}> no-showed — <@{m.winner}> advances.")
            await notify.dm_all(self.bot, m.players,
                                content=f"Your match `{m.id}` ended: {m.score}.")

        for m in events["rescheduled"]:
            await notify.dm_all(
                self.bot, m.players,
                content=(f"Both players missed check-in for `{m.id}`. "
                         f"Rescheduled to {ts.discord_ts(m.scheduled_for)}. "
                         f"Miss it again and you both forfeit."))

    @scheduler.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _bracket_embed(self, tid: str) -> discord.Embed:
        t = self.svc.store.get_tournament(tid)
        matches = self.svc.store.get_matches(tid)
        e = discord.Embed(
            title=f"🏆 {t.name}" if t else "Bracket",
            colour=COLOUR,
            description=(f"Mode **{t.mode}** · {len(t.entrants)} entrants · "
                         f"state **{t.state}**") if t else "")
        if not matches:
            e.description = (e.description or "") + "\n\nNot started yet."
            return e
        icons = {"pending": "⚪", "scheduled": "🕒", "checkin": "⏰",
                 "active": "🔴", "completed": "✅", "forfeit": "🚫"}
        for bracket in ("winners", "losers", "grand_final", "rr"):
            group = [m for m in matches if m.bracket == bracket]
            if not group:
                continue
            for rnd in sorted({m.round_no for m in group}):
                rows = []
                for m in sorted([x for x in group if x.round_no == rnd],
                                key=lambda x: x.slot):
                    a = f"<@{m.player_a}>" if m.player_a else "—"
                    b = f"<@{m.player_b}>" if m.player_b else "—"
                    mark = icons.get(m.state, "•")
                    line = f"{mark} {a} vs {b}"
                    if m.winner:
                        line += f" → **<@{m.winner}>**"
                    elif m.scheduled_for:
                        line += f" · {ts.discord_ts(m.scheduled_for, 'R')}"
                    rows.append(f"`{m.id[-6:]}` {line}")
                # Embed fields cap at 1024 characters; a wide round can exceed
                # that, and an over-long field fails the whole send.
                text, part = "", 1
                for row in rows:
                    if len(text) + len(row) + 1 > 1000:
                        e.add_field(name=f"{bracket.title()} · R{rnd}"
                                         + (f" ({part})" if part > 1 else ""),
                                    value=text, inline=False)
                        text, part = "", part + 1
                    text += row + "\n"
                if text:
                    e.add_field(name=f"{bracket.title()} · R{rnd}"
                                     + (f" ({part})" if part > 1 else ""),
                                value=text, inline=False)
        if t and t.mode == Mode.ROUND_ROBIN.value:
            table = brackets.standings(matches)[:10]
            if table:
                e.add_field(name="Standings",
                            value="\n".join(f"`{i}.` <@{u}> — {w}W {l}L"
                                            for i, (u, w, l) in
                                            enumerate(table, 1)),
                            inline=False)
        return e

    # ── /tournament ──────────────────────────────────────────────────────────

    tournament = app_commands.Group(name="tournament",
                                    description="Tournament system")
    match = app_commands.Group(name="match", description="Your matches")

    @tournament.command(name="join", description="Join a tournament")
    @app_commands.describe(tournament_id="Leave blank to use the newest open one")
    async def t_join(self, interaction: discord.Interaction,
                     tournament_id: Optional[str] = None):
        tid = tournament_id or self._newest_open(interaction.guild_id)
        if not tid:
            return await interaction.response.send_message(
                "No open tournament here.", ephemeral=True)
        if self.svc.store.get_player(interaction.user.id) is None:
            # First time: collect the profile, then join in the same flow.
            return await interaction.response.send_modal(RegisterModal(self, tid))
        ok, msg, _ = self.svc.join(tid, interaction.user.id)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + msg,
                                                ephemeral=not ok)

    @tournament.command(name="register",
                        description="Set or update your timezone & availability")
    async def t_register(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RegisterModal(self))

    @tournament.command(name="leave", description="Leave before it starts")
    async def t_leave(self, interaction: discord.Interaction,
                      tournament_id: Optional[str] = None):
        tid = tournament_id or self._newest_open(interaction.guild_id)
        if not tid:
            return await interaction.response.send_message("Nothing to leave.",
                                                           ephemeral=True)
        ok, msg, _ = self.svc.leave(tid, interaction.user.id)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + msg,
                                                ephemeral=True)

    @tournament.command(name="info", description="Tournament details")
    async def t_info(self, interaction: discord.Interaction,
                     tournament_id: Optional[str] = None):
        tid = tournament_id or self._newest_open(interaction.guild_id)
        if not tid:
            return await interaction.response.send_message("No tournaments here.",
                                                           ephemeral=True)
        await interaction.response.send_message(embed=self._bracket_embed(tid))

    @tournament.command(name="bracket", description="Show the bracket")
    async def t_bracket(self, interaction: discord.Interaction,
                        tournament_id: str):
        await interaction.response.send_message(
            embed=self._bracket_embed(tournament_id))

    @tournament.command(name="matches", description="Your upcoming matches")
    async def t_matches(self, interaction: discord.Interaction):
        p = self.svc.store.get_player(interaction.user.id)
        rows = []
        for t in self.svc.store.active_tournaments():
            for m in self.svc.store.get_matches(t.id):
                if interaction.user.id not in m.players:
                    continue
                if m.state in (MatchState.COMPLETED.value,
                               MatchState.FORFEIT.value):
                    continue
                when = (ts.local_str(m.scheduled_for, p.utc_offset)
                        if p and m.scheduled_for else "unscheduled")
                rows.append(f"`{m.id}` · {t.name} · vs "
                            f"<@{m.opponent_of(interaction.user.id)}> · {when}")
        await interaction.response.send_message(
            "\n".join(rows) if rows else "No upcoming matches.", ephemeral=True)

    # ── admin ────────────────────────────────────────────────────────────────

    # ── /match ───────────────────────────────────────────────────────────────

    @match.command(name="checkin", description="Confirm you're ready")
    async def m_checkin(self, interaction: discord.Interaction, match_id: str):
        ok, msg, _ = self.svc.checkin(match_id, interaction.user.id)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + msg,
                                                ephemeral=True)

    @match.command(name="report", description="Report the result")
    @app_commands.describe(winner="Who won", score="e.g. 3-1")
    async def m_report(self, interaction: discord.Interaction, match_id: str,
                       winner: discord.Member, score: str = ""):
        ok, msg, m = self.svc.report(match_id, interaction.user.id, winner.id,
                                     score)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + msg,
                                                ephemeral=not ok)
        if not ok or not m:
            return
        t = self.svc.store.get_tournament(m.tournament_id)
        if not t:
            return
        await notify.announce(
            self.bot, t.channel_id,
            f"✅ **{t.name}** — <@{m.winner}> beat <@{m.loser}>"
            + (f" ({m.score})" if m.score else ""))
        if t.state == TournamentState.COMPLETED.value:
            champ = brackets.champion(self.svc.store.get_matches(t.id), t.mode)
            await self._award(t, champ)

    @match.command(name="dispute", description="Flag a result for review")
    async def m_dispute(self, interaction: discord.Interaction, match_id: str,
                        reason: str):
        ok, msg = self.svc.dispute(match_id, interaction.user.id, reason)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + msg,
                                                ephemeral=True)
        if ok:
            m = self.svc.store.get_match(match_id)
            t = self.svc.store.get_tournament(m.tournament_id) if m else None
            if t:
                await notify.announce(
                    self.bot, t.channel_id,
                    f"⚠️ Dispute on `{match_id}` from <@{interaction.user.id}>: "
                    f"{reason[:200]}")

    # ── rewards ──────────────────────────────────────────────────────────────

    async def _award(self, t, champ: Optional[int]) -> None:
        if champ is None:
            return
        await notify.announce(self.bot, t.channel_id,
                              f"🏆 **{t.name}** champion: <@{champ}>!")
        if not t.reward_type:
            return
        try:
            if t.reward_type == "currency":
                from utils.database import get_user, update_user
                prof = get_user(champ)
                prof["coins"] = prof.get("coins", 0) + int(t.reward_value or 0)
                update_user(champ, prof)
            elif t.reward_type == "item":
                from utils.database import get_user, update_user
                prof = get_user(champ)
                prof.setdefault("inventory", []).append(t.reward_value)
                update_user(champ, prof)
            elif t.reward_type == "role":
                guild = self.bot.get_guild(t.guild_id)
                member = guild.get_member(champ) if guild else None
                role = discord.utils.get(guild.roles, name=t.reward_value) \
                    if guild else None
                if member and role:
                    await member.add_roles(role, reason="Tournament win")
        except Exception as e:                       # noqa: BLE001
            # A prize that fails to deliver must not swallow the win
            # announcement that already went out.
            log.warning("reward delivery failed for %s: %s", champ, e)
        await notify.dm(self.bot, champ,
                        f"🏆 You won **{t.name}**! Prize: {t.reward_value}")

    # ── misc ─────────────────────────────────────────────────────────────────

    def _newest_open(self, guild_id: Optional[int]) -> str:
        for t in self.svc.store.list_tournaments(
                guild_id, [TournamentState.SIGNUP.value]):
            return t.id
        return ""


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TournamentCog(bot))
