"""
tournament.py  —  🏆 one command, one panel, one bracket

What this replaces
------------------
Two tournament systems, ~4,000 lines between them, that between them had been
used by exactly nobody: 3,410 profiles carried no tournament key of any kind
and all seven tables of `data/tournaments.db` were empty. One was a scheduled
esports admin tool (ELO, timezone availability, auto-scheduling, check-in
windows, no-show bans, RSVP announcements) whose results were SELF-REPORTED and
which never once touched the battle engine. The other was a live bracket that
did run real battles, and had been commented out of the loader.

They also both claimed the app-command name `tournament`, which is a
`CommandAlreadyRegistered` crash waiting for anyone to load both.

This is the whole feature now: an admin runs one command, picks a size, the
panel is posted publicly, players press Join, and the bracket runs itself with
real battles.

Why the panel is a public message
---------------------------------
It IS the announcement. An ephemeral panel would be a tournament nobody can
join — the entire point is that everyone reading the channel sees it and
presses the button. `open_panel` therefore never sets `ephemeral=True` on the
panel itself; only refusals and "not your button" notes are ephemeral.

Why state is in memory
----------------------
The persistent version shipped seven tables and collected zero rows. A lobby
that lives on the cog cannot corrupt anything, cannot half-write a bracket, and
disappears cleanly on restart — which is the correct behaviour for a lobby
nobody has joined yet. If tournaments ever need to survive a restart, that is
one store module against a feature people actually use.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import (get_beyblade, get_user, load_beyblades,
                            mutate_user)

from . import brackets
from .models import Match, MatchState, Mode

log = logging.getLogger("beyblade_bot.tournament")

# Kept identical to the retired cog so `cogs/admin/console.py:117`, which looks
# the cog up by this literal string, keeps working.
COG_NAME   = "Tournaments"

MASTER_ID  = 956773141265391676
ADMIN_ROLE = "Tournament Admin"
COLOUR     = 0x5865F2

# The only sizes offered. Powers of two so the bracket needs no byes in the
# common case; `brackets.auto_byes` still covers a lobby that starts short.
SIZES = (4, 8, 16)

# A lobby nobody joins should not hold its players — or the channel — forever.
LOBBY_TIMEOUT = 600          # 10 minutes
MIN_PLAYERS   = 2

# Paid to the champion. Coins rather than an item on purpose: an item prize has
# to be checked against the inventory cap and can be refused after the player
# has already won, which is a bad thing to discover at the trophy ceremony.
# Coins have no cap. Scaled by bracket size so a 16-player run is worth more
# than a 4-player one — measured against the live economy, where the median
# coin-holder sits on ~101,000, this is a real but not distorting prize.
PRIZE_PER_ENTRANT = 1_500

# Rarity weights for the random draft. You do not fight with your collection,
# you fight with what you are dealt, which is what stops a tournament being
# decided by who has spent the most.
# Never dealt, matching `spawn._NEVER_SPAWN`. Exclusive blades are event or
# owner content; a tournament handing one out for free undoes whatever made it
# exclusive.
NEVER_DRAFT = {"Exclusive"}

RARITY_WEIGHT = {
    "Common": 40, "Rare": 26, "Epic": 16, "Legendary": 9,
    "Mythic": 5, "Ultimate": 3, "Exclusive": 1,
}


def is_tournament_admin(user) -> bool:
    """Owner, or anyone holding the admin role.

    Checked on every call rather than once at registration, because roles
    change mid-tournament. Lifted verbatim from the retired cog — the auth rule
    was never the thing that was wrong.
    """
    if getattr(user, "id", None) == MASTER_ID:
        return True
    roles = getattr(user, "roles", None) or []
    return any(getattr(r, "name", "") == ADMIN_ROLE for r in roles)


def draft_pool() -> list[dict]:
    """Every blade a tournament may deal out.

    The filters are NOT optional and are the same three every other random
    blade source in this bot applies (`cogs/spawn/spawn.py:137`,
    `cogs/economy/shop.py:910`). The first draft of this function kept only
    `if b.get("name")`, and the consequences were measured against the live
    roster:

        Ultimate Valkyrie               hidden_drop_one_in: 10,000,000
        Ultimate Valkyrie (Black Ed.)   hidden_drop_one_in:  5,000,000
        Janus Bahamut                   owner-bound to one player
        + 5 Exclusive blades

    all became draftable at roughly **1 in 578** — four orders of magnitude
    better than the odds the rest of the game guarantees, and handed out free.
    `tools/sim_ultimate_valkyrie.py` spends 200,000 simulated spawns proving
    those blades cannot be rolled, but scopes the proof to the spawn and shop
    pools, so this route walked straight past it and the suite stayed green.
    """
    from utils.availability import obtainable
    return [b for b in load_beyblades().values()
            if b.get("name")
            and b.get("rarity") not in NEVER_DRAFT
            and not b.get("hidden_drop_one_in")
            and not b.get("booster_exclusive")
            and obtainable(b)]


def draft_blades(rng: random.Random, count: int) -> list[Optional[dict]]:
    """`count` blades, rarity-weighted and WITHOUT replacement.

    Without replacement because dealing both sides of a match the same blade
    turns a draft into a mirror — the retired bracket drew this way and the
    first rewrite lost it by calling a single-blade helper per entrant.
    Falls back to allowing repeats only if the pool is smaller than the field.
    """
    try:
        pool = draft_pool()
    except Exception:                                    # noqa: BLE001
        return [None] * count
    if not pool:
        return [None] * count

    out: list[Optional[dict]] = []
    remaining = list(pool)
    for _ in range(count):
        if not remaining:                    # pool smaller than the bracket
            remaining = list(pool)
        weights = [RARITY_WEIGHT.get(str(b.get("rarity", "Common")), 1)
                   for b in remaining]
        pick = rng.choices(remaining, weights=weights, k=1)[0]
        remaining.remove(pick)
        out.append(pick)
    return out


@dataclass
class Lobby:
    """One tournament, start to finish. Lives on the cog, not on disk."""

    host_id:    int
    channel_id: int
    guild_id:   int
    size:       int = 8
    entrants:   list[int] = field(default_factory=list)
    blades:     dict = field(default_factory=dict)      # user_id -> blade name
    matches:    list = field(default_factory=list)
    started:    bool = False
    finished:   bool = False
    champion:   Optional[int] = None

    @property
    def full(self) -> bool:
        return len(self.entrants) >= self.size

    @property
    def slots_left(self) -> int:
        return max(0, self.size - len(self.entrants))


class TournamentPanel(discord.ui.View):
    """The public announcement: pick a size, join, start.

    Modelled on `BossLobbyView` in cogs/battle/boss/boss_battle.py, which is
    the closest working analogue in this bot — a lobby people join by button
    that then runs itself.
    """

    def __init__(self, cog: "TournamentCog", lobby: Lobby):
        super().__init__(timeout=LOBBY_TIMEOUT)
        self.cog = cog
        self.lobby = lobby
        self.message: Optional[discord.Message] = None
        self._build()

    # ── layout ───────────────────────────────────────────────────────────────
    def _build(self) -> None:
        self.clear_items()
        if self.lobby.started:
            return
        self.add_item(SizeSelect(self.lobby))
        self.add_item(JoinButton(self.lobby))
        self.add_item(StartButton(self.lobby))

    def embed(self) -> discord.Embed:
        lob = self.lobby
        if lob.finished:
            who = f"<@{lob.champion}>" if lob.champion else "nobody"
            return discord.Embed(
                title="🏆  Tournament complete",
                description=f"Champion: {who}",
                colour=0xF1C40F)

        roster = "\n".join(
            f"`{i + 1}.` <@{uid}>" for i, uid in enumerate(lob.entrants)
        ) or "*nobody yet — press Join*"

        e = discord.Embed(
            title="🏆  Tournament — open for entries",
            description=(f"**{lob.size}-player** single elimination.\n"
                         f"Random blade drafted for everyone at the start — "
                         f"you fight with what you're dealt."),
            colour=COLOUR)
        e.add_field(name=f"Entrants ({len(lob.entrants)}/{lob.size})",
                    value=roster, inline=False)
        e.set_footer(text=f"{lob.slots_left} slot(s) left · "
                          f"the host starts it · "
                          f"expires in {LOBBY_TIMEOUT // 60} min")
        return e

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    # ── lifetime ─────────────────────────────────────────────────────────────
    async def close(self, reason: str) -> None:
        """Tear the panel down: release everyone, kill the buttons, say why.

        ONE teardown, called by both the timeout and `admin_cancel`. Cancel
        used to release the lobby without touching the view, which left a live
        panel over a lobby the cog no longer tracked — and every button on it
        still worked. A player could Join a cancelled tournament (marking
        themselves busy with nothing), or Start it, while `open_panel` happily
        opened a second one because the first had been popped. Two brackets in
        one guild, and the zombie's own `_release` would then evict the real
        one on the way out.
        """
        self.cog._release(self.lobby)
        self.lobby.finished = True
        for child in self.children:
            child.disabled = True
        self.stop()
        if self.message:
            try:
                e = self.embed()
                e.colour = 0x99AAB5
                e.set_footer(text=reason)
                await self.message.edit(embed=e, view=self)
            except Exception:                            # noqa: BLE001
                pass

    async def on_timeout(self) -> None:
        """Release every entrant, always.

        The boss lobby carries this same handler because omitting it left
        players stuck in the active set forever, unable to start anything
        again. A tournament holds MORE players than a boss lobby does, so the
        same omission here would be worse.
        """
        await self.close("⏰ Expired — nobody started it in time.")


def _guard(fn):
    """Wrap a component callback so a failure cannot freeze the panel.

    The v1.07 boss-battle hang was a callback with a `finally` and no `except`:
    the state advanced, the message never did, and the fight sat there with
    live buttons pointing at a state that no longer existed. The same shape
    here would strand a whole lobby, so every callback in this file is wrapped
    from the start rather than after the bug report.
    """
    async def wrapper(self, interaction: discord.Interaction):
        try:
            # A closed lobby swallows its clicks rather than acting on them.
            # Discord leaves a cancelled panel on screen until someone
            # scrolls past it, so its buttons stay pressable; the boss view
            # guards the same way at boss_battle.py:987.
            lob = getattr(getattr(self, "view", None), "lobby", None)
            if lob is not None and lob.finished:
                return await interaction.response.defer()
            return await fn(self, interaction)
        except Exception:                                # noqa: BLE001
            log.exception("[tournament] %s failed", fn.__name__)
            note = "⚠️ Something went wrong — the tournament is still open."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(note, ephemeral=True)
                else:
                    await interaction.response.send_message(note, ephemeral=True)
            except Exception:                            # noqa: BLE001
                pass
    wrapper.__name__ = fn.__name__
    return wrapper


class SizeSelect(discord.ui.Select):
    def __init__(self, lobby: Lobby):
        # Values are non-empty ASCII, never "". An empty select value is a
        # Discord 400 — `components.…value: Must be between 1 and 100 in
        # length` — and it took `;inv` down for 75% of bey holders in v96.
        super().__init__(
            placeholder="Bracket size",
            min_values=1, max_values=1, row=0,
            options=[
                discord.SelectOption(
                    label=f"{n} players", value=str(n), emoji="🏆",
                    description=f"Single elimination, {n} entrants",
                    default=(n == lobby.size))
                for n in SIZES
            ])
        self.lobby = lobby

    @_guard
    async def callback(self, interaction: discord.Interaction):
        view: TournamentPanel = self.view
        if interaction.user.id != self.lobby.host_id:
            return await interaction.response.send_message(
                "Only the host picks the bracket size.", ephemeral=True)
        size = int(self.values[0])
        if size < len(self.lobby.entrants):
            return await interaction.response.send_message(
                f"{len(self.lobby.entrants)} players have already joined — "
                f"you can't shrink it to {size}.", ephemeral=True)
        self.lobby.size = size
        await view.refresh(interaction)


class JoinButton(discord.ui.Button):
    def __init__(self, lobby: Lobby):
        super().__init__(label="Join", emoji="⚔️",
                         style=discord.ButtonStyle.success, row=1)
        self.lobby = lobby

    @_guard
    async def callback(self, interaction: discord.Interaction):
        view: TournamentPanel = self.view
        uid = interaction.user.id
        lob = self.lobby

        if uid in lob.entrants:
            lob.entrants.remove(uid)
            view.cog._active.discard(uid)
            return await view.refresh(interaction)

        if lob.full:
            return await interaction.response.send_message(
                "That bracket is full.", ephemeral=True)
        if uid in view.cog.banned:
            return await interaction.response.send_message(
                "You're banned from tournaments.", ephemeral=True)
        if uid in view.cog._active:
            return await interaction.response.send_message(
                "You're already in a tournament.", ephemeral=True)
        if view.cog._in_battle(uid):
            return await interaction.response.send_message(
                "Finish your current battle first.", ephemeral=True)
        if not (get_user(uid).get("inventory") or []):
            return await interaction.response.send_message(
                "You need a blade first — run `;start`.", ephemeral=True)

        lob.entrants.append(uid)
        view.cog._active.add(uid)
        await view.refresh(interaction)

        if lob.full:
            view.cog._spawn(view.cog.run(view))


class StartButton(discord.ui.Button):
    def __init__(self, lobby: Lobby):
        super().__init__(label="Start", emoji="▶️",
                         style=discord.ButtonStyle.primary, row=1,
                         disabled=len(lobby.entrants) < MIN_PLAYERS)
        self.lobby = lobby

    @_guard
    async def callback(self, interaction: discord.Interaction):
        view: TournamentPanel = self.view
        if interaction.user.id != self.lobby.host_id:
            return await interaction.response.send_message(
                "Only the host can start it.", ephemeral=True)
        if len(self.lobby.entrants) < MIN_PLAYERS:
            return await interaction.response.send_message(
                f"Needs at least {MIN_PLAYERS} players.", ephemeral=True)
        await interaction.response.defer()
        view.cog._spawn(view.cog.run(view))


class TournamentCog(commands.Cog, name=COG_NAME):
    """One command. The panel and the bracket runner both live here."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # guild_id -> Lobby. One live tournament per guild: two panels in one
        # server would fight over the same players' `_active` entries.
        self.lobbies: dict[int, Lobby] = {}
        # Every player currently committed to a tournament, across all guilds.
        self._active: set[int] = set()
        # Barred from joining, set by `/admin ban_player`. A plain set rather
        # than a table: with no ELO ledger and no no-show counter left, "banned"
        # has exactly one meaning now — cannot join the next lobby.
        self.banned: set[int] = set()
        self._tasks: set = set()
        # guild_id -> the live panel, so an admin can force-start a lobby whose
        # panel has scrolled out of reach.
        self.panels: dict[int, "TournamentPanel"] = {}

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def cog_unload(self):
        for t in list(self._tasks):
            t.cancel()

    def _release(self, lobby: Lobby) -> None:
        for uid in lobby.entrants:
            self._active.discard(uid)
        # Identity-checked: a stale lobby finishing late must not evict the
        # live one that has since taken its slot.
        if self.lobbies.get(lobby.guild_id) is lobby:
            self.lobbies.pop(lobby.guild_id, None)
        if getattr(self.panels.get(lobby.guild_id), "lobby", None) is lobby:
            self.panels.pop(lobby.guild_id, None)

    def _in_battle(self, user_id: int) -> bool:
        """True when this player is committed elsewhere.

        Two sessions would fight over the same player's button presses — the
        legacy bracket learned that one the hard way and guarded it, so the
        guard comes across rather than being rediscovered.

        Checks BOTH other registries. There are three independent "player is
        busy" sets in this bot — this cog's, `BossCog._active`, and
        `BattleCog.active_battles` — and nobody consults this one. Reaching
        outward from here is one-directional and does not fix that asymmetry,
        but it does make the tournament defensively complete on its own side
        without editing two cogs this change does not otherwise touch.
        """
        for cog_name, attr in (("Battle", "active_battles"),
                               ("Boss Battles", "_active"),
                               ("Boss", "_active")):
            cog = self.bot.get_cog(cog_name)
            busy = getattr(cog, attr, None) if cog else None
            if busy and user_id in busy:
                return True
        return False

    def _name(self, guild, uid: int) -> str:
        """Guild member, then the global user, then the id.

        The middle tier matters: an entrant who joined by button but is not in
        the member cache — or who left mid-bracket, which `_play` explicitly
        handles — would otherwise render as `Player 956773141265391676` on the
        bracket PNG and in the champion announcement. `ranked_cog._display_name`
        carries the same three tiers and says why in as many words.
        """
        from cogs.ranked.ranked_cog import _display_name
        try:
            return _display_name(self.bot, guild, uid)
        except Exception:                                # noqa: BLE001
            m = guild.get_member(uid) if guild else None
            return m.display_name if m else f"Player {uid}"

    # ── the command ──────────────────────────────────────────────────────────
    async def open_panel(self, send, guild, author, channel) -> None:
        """The single entry point both `;tournament` and `/tournament` call.

        One implementation means the prefix and slash surfaces cannot drift —
        and the two-modules-one-name split is exactly what made the old system
        crash on load.
        """
        if not is_tournament_admin(author):
            return await send("🏆 An admin opens tournaments — ask one to "
                              "start the next.", ephemeral=True)

        live = self.lobbies.get(guild.id if guild else 0)
        if live and not live.finished:
            return await send("A tournament is already open in this server.",
                              ephemeral=True)

        lobby = Lobby(host_id=author.id,
                      channel_id=getattr(channel, "id", 0),
                      guild_id=guild.id if guild else 0)
        self.lobbies[lobby.guild_id] = lobby
        view = TournamentPanel(self, lobby)
        self.panels[lobby.guild_id] = view
        # Public on purpose: the panel IS the announcement, and an ephemeral
        # one would be a tournament nobody else can see or join.
        view.message = await send(embed=view.embed(), view=view)

    @commands.command(name="tournament", aliases=["tourney"])
    @commands.guild_only()
    async def tournament_prefix(self, ctx: commands.Context) -> None:
        """🏆 Open a tournament (admin only). Players join with the button."""
        async def send(content=None, *, embed=None, view=None, ephemeral=False):
            # `ephemeral` has no meaning for a prefix command; a refusal is
            # sent and auto-deleted instead of silently ignoring the flag.
            return await ctx.send(content, embed=embed, view=view,
                                  delete_after=20 if ephemeral else None)
        await self.open_panel(send, ctx.guild, ctx.author, ctx.channel)

    @app_commands.command(name="tournament",
                          description="Open a tournament (admin only)")
    @app_commands.guild_only()
    async def tournament_slash(self, interaction: discord.Interaction) -> None:
        async def send(content=None, *, embed=None, view=None, ephemeral=False):
            await interaction.response.send_message(
                content, embed=embed, view=view, ephemeral=ephemeral)
            return await interaction.original_response()
        await self.open_panel(send, interaction.guild,
                              interaction.user, interaction.channel)

    # ── running the bracket ──────────────────────────────────────────────────
    async def run(self, view: TournamentPanel) -> None:
        """Draft, seed, and play every round through to a champion."""
        lobby = view.lobby
        if lobby.started:
            return
        lobby.started = True
        try:
            await self._run(view, lobby)
        except Exception:                                # noqa: BLE001
            log.exception("[tournament] bracket crashed")
        finally:
            # ONE release, on every exit. It used to be called at three
            # explicit returns and missed the fourth: `_play` posts to the
            # channel unguarded, so a Forbidden there escaped into a dead task
            # and left the guild wedged on "a tournament is already open" with
            # nothing actually running.
            self._release(lobby)

    async def _run(self, view: "TournamentPanel", lobby: Lobby) -> None:
        channel = self.bot.get_channel(lobby.channel_id)
        guild = self.bot.get_guild(lobby.guild_id)
        if channel is None:
            return

        rng = random.Random()
        drafted = draft_blades(rng, len(lobby.entrants))
        for uid, blade in zip(lobby.entrants, drafted):
            lobby.blades[uid] = blade["name"] if blade else ""

        try:
            lobby.matches = brackets.generate(
                f"t_{lobby.guild_id}", list(lobby.entrants), Mode.SINGLE.value)
            brackets.auto_byes(lobby.matches, Mode.SINGLE.value, time.time())
        except ValueError as exc:
            await channel.send(f"❌ {exc}")
            return

        view._build()
        for child in view.children:
            child.disabled = True
        # STOP the view, or its LOBBY_TIMEOUT stays armed through the whole
        # bracket. A 16-player run is 15 sequential interactive matches at up
        # to BATTLE_TIMEOUT each, so the 10-minute timer reliably fires
        # mid-tournament — and on_timeout calls _release, which drops every
        # entrant from _active and pops the lobby the runner is still using.
        # The boss views this one is modelled on call stop() in five places.
        view.stop()
        if view.message:
            try:
                await view.message.edit(view=view)
            except Exception:                            # noqa: BLE001
                pass

        first = min((m.round_no for m in lobby.matches), default=1)
        await self._post_bracket(channel, guild, lobby, "Bracket drawn", first)

        while not brackets.is_complete(lobby.matches, Mode.SINGLE.value):
            ready = brackets.ready_matches(lobby.matches)
            if not ready:
                break
            rnd = min(m.round_no for m in ready)
            for match in ready:
                await self._play(channel, guild, lobby, match, rnd)
            # Posted for the round just PLAYED, so the card shows results
            # rather than an empty payload for a round that does not exist.
            await self._post_bracket(channel, guild, lobby,
                                     f"Round {rnd}", rnd)

        lobby.finished = True
        lobby.champion = brackets.champion(lobby.matches, Mode.SINGLE.value)
        name = self._name(guild, lobby.champion) if lobby.champion else None
        last = max((m.round_no for m in lobby.matches), default=1)
        await self._post_bracket(channel, guild, lobby, "Champion", last,
                                 champion=name)

        prize = self._award(lobby)
        if lobby.champion:
            await channel.send(
                f"🏆 **{name}** takes the tournament — 🪙 **{prize:,}** coins!")
        else:
            await channel.send("The tournament ended without a champion.")

    def _award(self, lobby: Lobby) -> int:
        """Pay the champion. Returns what was actually paid.

        Through `mutate_user`, not get_user/update_user: that pair is a race
        where a concurrent write between the two calls is silently discarded,
        and `redeem.grant` erasing blades is what this codebase has already
        paid to learn. Never raises — a failed payout must not take down the
        trophy message that tells everyone who won.
        """
        if not lobby.champion:
            return 0
        prize = PRIZE_PER_ENTRANT * max(1, len(lobby.entrants))
        try:
            mutate_user(lobby.champion,
                        lambda p: p.__setitem__("coins",
                                                int(p.get("coins", 0)) + prize))
            return prize
        except Exception:                                # noqa: BLE001
            log.exception("[tournament] payout failed for %s", lobby.champion)
            return 0

    async def _play(self, channel, guild, lobby: Lobby, match: Match,
                    rnd: int) -> None:
        """One match, through the real PvP engine."""
        a, b = match.player_a, match.player_b
        if a is None or b is None:
            self._finish(lobby, match, a if b is None else b)
            return

        p1 = guild.get_member(a) if guild else None
        p2 = guild.get_member(b) if guild else None
        if p1 is None or p2 is None:
            # Someone left the server — the other advances rather than the
            # bracket stalling on a player who cannot press a button.
            self._finish(lobby, match, b if p1 is None else a)
            return

        from cogs.battle.battle import _apply_parts
        from cogs.battle.session import BattleSession

        raw1 = get_beyblade(lobby.blades.get(a, ""))
        raw2 = get_beyblade(lobby.blades.get(b, ""))
        if not raw1 or not raw2:
            self._finish(lobby, match, b if not raw1 else a)
            return

        blade1 = _apply_parts(raw1, get_user(a))
        blade2 = _apply_parts(raw2, get_user(b))

        await channel.send(embed=discord.Embed(
            title=f"⚔️  Round {rnd}",
            description=(f"{p1.mention} (**{blade1['name']}**)\n"
                         f"vs {p2.mention} (**{blade2['name']}**)"),
            colour=COLOUR))

        bc = self.bot.get_cog("Battle")
        registry = getattr(bc, "active_battles", None) if bc else None
        winner_id = None
        try:
            session = BattleSession(bot=self.bot, channel=channel,
                                    p1=p1, p2=p2, blade1=blade1, blade2=blade2)
            if registry is not None:
                registry[a] = session
                registry[b] = session
            await session.run()
            winner_id = getattr(session, "winner_id", None)
        except Exception:                                # noqa: BLE001
            log.exception("[tournament] match crashed")
            await channel.send("⚠️ That match crashed — advancing on a coin flip.")
        finally:
            if registry is not None:
                registry.pop(a, None)
                registry.pop(b, None)

        # A crash or a draw must never stall the bracket: someone advances.
        if winner_id not in (a, b):
            winner_id = random.choice((a, b))
        self._finish(lobby, match, winner_id)

    def _finish(self, lobby: Lobby, match: Match, winner: Optional[int]) -> None:
        match.winner = winner
        match.loser = (match.player_b if winner == match.player_a
                       else match.player_a)
        match.state = MatchState.COMPLETED.value
        brackets.advance(lobby.matches, match, Mode.SINGLE.value)

    # ── rendering ────────────────────────────────────────────────────────────
    def _card_payload(self, guild, lobby: Lobby, rnd: int) -> list[dict]:
        """The shape utils/tournament_card documents: left/right/winner."""
        blades = load_beyblades()

        def side(uid):
            if uid is None:
                return None
            bname = lobby.blades.get(uid, "")
            return {"name": self._name(guild, uid),
                    "blade": bname or "—",
                    "rarity": (blades.get(bname) or {}).get("rarity", "Common")}

        out = []
        for m in lobby.matches:
            if m.round_no != rnd:
                continue
            winner = None
            if m.winner is not None:
                winner = "left" if m.winner == m.player_a else "right"
            out.append({"left": side(m.player_a),
                        "right": side(m.player_b),
                        "winner": winner})
        return out

    async def _post_bracket(self, channel, guild, lobby: Lobby, label: str,
                            rnd: int, champion: Optional[str] = None) -> None:
        """Post the PNG bracket card, falling back to an embed.

        Guarded because a failed render is decoration failing, not a
        tournament ending — the same lesson the boss card learned when a full
        disk froze a fight mid-battle.
        """
        file = None
        try:
            from utils.tournament_card import render_tournament_card
            payload = self._card_payload(guild, lobby, rnd)
            if payload:
                buf = await asyncio.to_thread(
                    render_tournament_card, label, payload, 0, 0,
                    len(lobby.entrants), champion=champion)
                if buf is not None:
                    file = discord.File(buf, filename="tournament.png")
        except Exception:                                # noqa: BLE001
            log.debug("[tournament] card render failed", exc_info=True)

        try:
            if file is not None:
                await channel.send(file=file)
            else:
                await channel.send(embed=discord.Embed(
                    title=f"🏆  {label}", colour=COLOUR,
                    description="\n".join(
                        f"<@{m.player_a}> vs "
                        + (f"<@{m.player_b}>" if m.player_b else "*bye*")
                        for m in lobby.matches
                        if m.round_no == rnd) or "—"))
        except Exception:                                # noqa: BLE001
            log.debug("[tournament] bracket post failed", exc_info=True)

    # ── admin hooks, called by cogs/admin/console.py ──────────────────────────
    def admin_lobby(self, guild_id: int) -> Optional[Lobby]:
        return self.lobbies.get(guild_id)

    def admin_start(self, guild_id: int) -> bool:
        """Force-start the open lobby. False when there is nothing to start."""
        lobby = self.lobbies.get(guild_id)
        view = self.panels.get(guild_id)
        if not lobby or not view or lobby.started or len(lobby.entrants) < MIN_PLAYERS:
            return False
        self._spawn(self.run(view))
        return True

    async def admin_cancel(self, guild_id: int) -> bool:
        """Close the lobby AND its panel. False when there is nothing open."""
        view = self.panels.get(guild_id)
        lobby = self.lobbies.get(guild_id)
        if not lobby:
            return False
        if view is not None:
            await view.close("✖️ Cancelled by an admin.")
        else:
            lobby.finished = True
            self._release(lobby)
        return True

    def admin_ban(self, guild_id: int, user_id: int) -> None:
        """Bar a player, and pull them out of the open lobby if they are in it.

        Owns the whole "entrants and _active move together" invariant, which
        the admin console was previously hand-rolling half of from outside. A
        third thing joining that invariant later would have silently stopped
        being maintained there.
        """
        self.banned.add(user_id)
        lobby = self.lobbies.get(guild_id)
        if lobby and user_id in lobby.entrants:
            lobby.entrants.remove(user_id)
            self._active.discard(user_id)

    def admin_force_win(self, guild_id: int, user_id: int) -> bool:
        """Advance a player out of their unfinished match. False if none."""
        lobby = self.lobbies.get(guild_id)
        if not lobby or not lobby.matches:
            return False
        live = [m for m in lobby.matches
                if m.winner is None and user_id in (m.player_a, m.player_b)]
        if not live:
            return False
        self._finish(lobby, live[0], user_id)
        return True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TournamentCog(bot))
