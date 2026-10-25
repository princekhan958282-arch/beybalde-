"""Tournament commands configure and publish persistent V2 registrations.

The legacy lobby and bracket runner remain available internally, unchanged.
V2 registrations never enter that runner; V2 battle execution remains deferred.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import (get_beyblade, get_user, load_beyblades,
                            mutate_user)

from . import brackets
from .hosters import is_tournament_hoster
from .models import Match, MatchState, Mode
from .drafts import DraftStore
from .host_panel import HostPanel
from .registration import Registration, RegistrationPanel, RegistrationStore, validate_channel

log = logging.getLogger("beyblade_bot.tournament")

# Kept identical to the retired cog so the admin action registry, which looks
# the cog up by this literal string (`cogs/admin/actions.py:_tcog`), keeps
# working.
COG_NAME   = "Tournaments"

from ..admin.actions import ADMIN_IDS, MASTER_ID
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
    """Owner, Tournament Hoster, or anyone holding the admin role.

    Checked on every call rather than once at registration, because roles
    change mid-tournament.
    """
    uid = getattr(user, "id", None)
    try:
        uid_int = int(uid) if uid is not None else None
    except (ValueError, TypeError):
        uid_int = None
    if uid in ADMIN_IDS or uid_int in ADMIN_IDS or uid == MASTER_ID or uid_int == MASTER_ID or is_tournament_hoster(user):
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
        if not ((await get_user(uid)).get("inventory") or []):
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


@dataclass
class ActiveV2Tournament:
    guild_id: int
    reg: Registration
    panel: RegistrationPanel
    mode: str
    matches: list[Match]
    blades: dict[int, str]
    channel: discord.TextChannel
    guild: discord.Guild
    cancelled: bool = False
    match_events: dict[str, asyncio.Event] = field(default_factory=dict)
    match_messages: dict[str, Any] = field(default_factory=dict)


class AdvancePlayerView(discord.ui.View):
    def __init__(self, cog: "TournamentCog", active: ActiveV2Tournament, match: Match,
                 p1: Optional[discord.Member], p2: Optional[discord.Member]):
        super().__init__(timeout=60)
        self.cog = cog
        self.active = active
        self.match = match
        p1_name = getattr(p1, "display_name", f"Player {match.player_a}") if p1 else f"Player {match.player_a}"
        p2_name = getattr(p2, "display_name", f"Player {match.player_b}") if p2 else f"Player {match.player_b}"

        btn1 = discord.ui.Button(label=f"Advance {p1_name[:35]}", style=discord.ButtonStyle.success)
        btn2 = discord.ui.Button(label=f"Advance {p2_name[:35]}", style=discord.ButtonStyle.success)

        async def cb1(interaction: discord.Interaction):
            await interaction.response.send_message(f"👑 Force-advanced <@{match.player_a}>.", ephemeral=True)
            await self.cog.resolve_match(self.active, self.match, match.player_a)
            self.stop()

        async def cb2(interaction: discord.Interaction):
            await interaction.response.send_message(f"👑 Force-advanced <@{match.player_b}>.", ephemeral=True)
            await self.cog.resolve_match(self.active, self.match, match.player_b)
            self.stop()

        btn1.callback = cb1
        btn2.callback = cb2
        self.add_item(btn1)
        self.add_item(btn2)


class TournamentMatchView(discord.ui.View):
    def __init__(self, cog: "TournamentCog", active: ActiveV2Tournament, match: Match,
                 p1: Optional[discord.Member], p2: Optional[discord.Member]):
        super().__init__(timeout=None)
        self.cog = cog
        self.active = active
        self.match = match
        self.p1 = p1
        self.p2 = p2

    @discord.ui.button(label="Battle Now", style=discord.ButtonStyle.primary, emoji="⚔️", custom_id="t_match_battle")
    async def btn_battle(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = self.match
        reg = self.active.reg
        uid = interaction.user.id
        is_admin = is_tournament_admin(interaction.user) or await self.cog.bot.is_owner(interaction.user)
        if uid not in (match.player_a, match.player_b, reg.host_id) and not is_admin:
            return await interaction.response.send_message(
                "❌ Only match participants, the host, or an admin can start this battle.",
                ephemeral=True
            )
        if match.state == MatchState.COMPLETED.value or match.winner is not None:
            return await interaction.response.send_message("❌ This match is already completed.", ephemeral=True)

        if self.cog._in_battle(match.player_a) or self.cog._in_battle(match.player_b):
            return await interaction.response.send_message(
                "⚠️ One of the players is currently in another battle. Please finish it first.",
                ephemeral=True
            )

        button.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

        try:
            msg_text = f"⚔️ **Launching battle!** <@{match.player_a}> vs <@{match.player_b}>, choose your moves below!"
            if interaction.response.is_done():
                await interaction.followup.send(msg_text)
            else:
                await interaction.response.send_message(msg_text)
        except Exception:
            pass
        self.cog._spawn(self.cog._launch_interactive_match_battle(self.active, match, interaction.channel))

    @discord.ui.button(label="Forfeit", style=discord.ButtonStyle.secondary, emoji="🏳️", custom_id="t_match_forfeit")
    async def btn_forfeit(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = self.match
        uid = interaction.user.id
        if uid not in (match.player_a, match.player_b):
            return await interaction.response.send_message("❌ Only match participants can forfeit.", ephemeral=True)
        if match.state == MatchState.COMPLETED.value or match.winner is not None:
            return await interaction.response.send_message("❌ This match is already completed.", ephemeral=True)

        winner_id = match.player_b if uid == match.player_a else match.player_a
        await interaction.response.send_message(
            f"🏳️ <@{uid}> has forfeited this tournament match. <@{winner_id}> advances!"
        )
        await self.cog.resolve_match(self.active, match, winner_id)

    @discord.ui.button(label="Advance Player", style=discord.ButtonStyle.danger, emoji="👑", custom_id="t_match_advance")
    async def btn_advance(self, interaction: discord.Interaction, button: discord.ui.Button):
        reg = self.active.reg
        is_admin = is_tournament_admin(interaction.user) or await self.cog.bot.is_owner(interaction.user)
        if interaction.user.id != reg.host_id and not is_admin:
            return await interaction.response.send_message("❌ Only the tournament host or an admin can force-advance a player.", ephemeral=True)
        if self.match.state == MatchState.COMPLETED.value or self.match.winner is not None:
            return await interaction.response.send_message("❌ This match is already completed.", ephemeral=True)

        view = AdvancePlayerView(self.cog, self.active, self.match, self.p1, self.p2)
        await interaction.response.send_message("Select which player should be awarded the win and advance:", view=view, ephemeral=True)


class TournamentCog(commands.Cog, name=COG_NAME):
    """Shared command entry point, draft editor, and preserved legacy runner."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.setups: dict[int, HostPanel] = {}
        self.draft_store = DraftStore()
        self.registration_store = RegistrationStore(self.draft_store)
        self.registrations: dict[int, RegistrationPanel] = {}
        # guild_id -> Lobby. One live tournament per guild: two panels in one
        # server would fight over the same players' `_active` entries.
        self.lobbies: dict[int, Lobby] = {}
        # Every player currently committed to a tournament, across all guilds.
        self._active: set[int] = set()
        # Barred from joining, set by `/admin ban_player`. A plain set rather
        # than a table: with no ELO ledger and no no-show counter left, "banned"
        # has exactly one meaning now — cannot join the next lobby.
        self.banned: set[int] = set()
        self.active_tournaments: dict[int, "ActiveV2Tournament"] = {}
        self._tasks: set = set()
        self._expiry_task: Optional[asyncio.Task] = None
        # guild_id -> the live panel, so an admin can force-start a lobby whose
        # panel has scrolled out of reach.
        self.panels: dict[int, "TournamentPanel"] = {}

    async def cog_load(self):
        for reg in await asyncio.to_thread(self.registration_store.restore):
            panel = RegistrationPanel(self, reg)
            channel = self.bot.get_partial_messageable(reg.channel_id)
            panel.message = channel.get_partial_message(reg.message_id)
            self.registrations[reg.guild_id] = panel
            self._active.update(reg.entrants)
            self.bot.add_view(panel, message_id=reg.message_id)
        loop = getattr(self.bot, "loop", None) or asyncio.get_running_loop()
        self._expiry_task = loop.create_task(self._expiry_monitor())

    async def _expiry_monitor(self):
        """Monitor open registrations and handle 24-hour expiration."""
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(60)
                now = time.time()
                for guild_id, panel in list(self.registrations.items()):
                    reg = panel.reg
                    if reg.status == 'open' and now >= reg.created_at + 86400:
                        async with panel.lock:
                            if reg.status != 'open':
                                continue
                            if len(reg.entrants) >= 2:
                                reg.status = 'closed'
                                await asyncio.to_thread(self.registration_store.write, reg)
                                panel.build()
                                if panel.message:
                                    try:
                                        await panel.message.edit(view=panel, allowed_mentions=discord.AllowedMentions.none())
                                    except Exception:
                                        pass
                                channel = self.bot.get_channel(reg.channel_id)
                                if channel:
                                    try:
                                        await channel.send(
                                            f"⏰ <@{reg.host_id}>, the 24-hour registration window for **{reg.config.name}** "
                                            f"has closed with **{len(reg.entrants)}/{reg.config.slots}** players registered!\n"
                                            f"You can now press the **Start Tournament** button on the registration panel to launch the bracket!"
                                        )
                                    except Exception:
                                        pass
                            else:
                                channel = self.bot.get_channel(reg.channel_id)
                                if channel:
                                    try:
                                        await channel.send(
                                            f"✖️ Registration for **{reg.config.name}** expired: fewer than 2 players joined within 24 hours. "
                                            f"Registration cancelled and entry fees refunded."
                                        )
                                    except Exception:
                                        pass
                                await panel.close("Registration expired with fewer than 2 players.")
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("[tournament] error in registration expiry monitor")

    async def publish_registration(self, host_panel, guild):
        channel = validate_channel(guild, guild.get_channel(host_panel.channel_id))
        live = self.lobbies.get(guild.id)
        if guild.id in self.registrations or (live and not live.finished):
            raise ValueError('A tournament is already active in this server.')
        host_panel.config.validate()
        # Clone so subsequent changes to an editor cannot change published rules.
        from dataclasses import replace
        config = replace(host_panel.config)
        if config.bey_selection == 'Random Beyblade' and not draft_pool():
            raise ValueError('No eligible random Beyblades are available.')
        reg = Registration(guild.id, host_panel.host_id, channel.id, config)
        panel = RegistrationPanel(self, reg)
        self.registrations[guild.id] = panel
        begun = False
        try:
            await asyncio.to_thread(self.registration_store.begin, reg)
            begun = True
            panel.message = await channel.send(view=panel, allowed_mentions=discord.AllowedMentions.none())
            reg.message_id = panel.message.id
            reg.status = 'open'
            await asyncio.to_thread(self.registration_store.write, reg)
            panel.build()
            await panel.message.edit(view=panel, allowed_mentions=discord.AllowedMentions.none())
            return panel
        except Exception:
            reg.status = 'cancelled'
            if begun:
                try:
                    await asyncio.to_thread(self.registration_store.write, reg)
                except Exception:
                    log.exception('Failed to clear publication reservation')
            if self.registrations.get(guild.id) is panel:
                self.registrations.pop(guild.id)
            panel.stop()
            if panel.message:
                panel.build()
                try:
                    await panel.message.edit(view=panel, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    log.warning('Failed to disable unsuccessful registration publication', exc_info=True)
            raise

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def cog_unload(self):
        if hasattr(self, '_expiry_task') and self._expiry_task and not self._expiry_task.done():
            self._expiry_task.cancel()
        for active in list(self.active_tournaments.values()):
            active.cancelled = True
            for evt in active.match_events.values():
                evt.set()
        self.active_tournaments.clear()
        for panel in list(self.setups.values()):
            await panel.close("Setup closed because the tournament extension reloaded.")
        for registration in list(self.registrations.values()):
            async with registration.lock:
                registration.stop()  # durable registrations are reattached by cog_load
        self.registrations.clear()  # stale modals must not write through a retired cog
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
        if not is_tournament_admin(author) and not await self.bot.is_owner(author):
            return await send("Only a tournament host or Tournament Admin can configure tournaments.", ephemeral=True)
        if guild is None:
            return await send("Use this command in a server.", ephemeral=True)
        live = self.lobbies.get(guild.id)
        if (live and not live.finished) or guild.id in self.setups or guild.id in self.registrations or guild.id in self.active_tournaments:
            return await send("A tournament or setup is already open in this server.", ephemeral=True)
        # Reserve before the first await, preventing simultaneous command opens.
        view = HostPanel(self, guild.id, author.id)
        self.setups[guild.id] = view
        try:
            saved = await asyncio.to_thread(self.draft_store.load, guild.id)
            if saved:
                view.config = saved[1]
                view.note = "Editing the saved draft. Changes are saved only on Confirm Setup."
                view.build()
            view.message = await send(view=view)
        except Exception:
            view.release()
            raise

    @commands.command(name="tournament", aliases=["tourney"])
    @commands.guild_only()
    async def tournament_prefix(self, ctx: commands.Context) -> None:
        """🏆 Configure a tournament draft (host only)."""
        async def send(content=None, *, embed=None, view=None, ephemeral=False):
            # `ephemeral` has no meaning for a prefix command; a refusal is
            # sent and auto-deleted instead of silently ignoring the flag.
            return await ctx.send(content, embed=embed, view=view,
                                  delete_after=20 if ephemeral else None)
        await self.open_panel(send, ctx.guild, ctx.author, ctx.channel)

    @app_commands.command(name="tournament",
                          description="Configure a tournament draft (host only)")
    @app_commands.guild_only()
    async def tournament_slash(self, interaction: discord.Interaction) -> None:
        async def send(content=None, *, embed=None, view=None, ephemeral=False):
            await interaction.response.send_message(
                content, embed=embed, view=view, ephemeral=ephemeral)
            return await interaction.original_response()
        await self.open_panel(send, interaction.guild,
                              interaction.user, interaction.channel)

    @commands.command(name="tourney_win", aliases=["twin", "tournament_win", "t_win"])
    @commands.guild_only()
    async def tournament_win_command(self, ctx: commands.Context, member: discord.Member) -> None:
        """Advance a player in their active tournament match (host/admin only)."""
        active = self.active_tournaments.get(ctx.guild.id)
        is_admin = is_tournament_admin(ctx.author) or await self.bot.is_owner(ctx.author)
        if not active or (ctx.author.id != active.reg.host_id and not is_admin):
            if is_admin and self.admin_force_win(ctx.guild.id, member.id):
                return await ctx.send(f"👑 Advanced {member.mention} in their match.")
            return await ctx.send("❌ Only the tournament host or an admin can advance a player.")

        target_match = None
        for m in active.matches:
            if m.state != MatchState.COMPLETED.value and m.winner is None:
                if member.id in (m.player_a, m.player_b):
                    target_match = m
                    break

        if not target_match:
            return await ctx.send(f"❌ {member.mention} is not in an active, unfinished match in this tournament.")

        await self.resolve_match(active, target_match, member.id)
        await ctx.send(f"👑 Host has advanced {member.mention} to the next round!")

    @app_commands.command(name="tourney_win", description="Advance a player in their tournament match (host/admin only)")
    @app_commands.guild_only()
    async def tournament_win_slash(self, interaction: discord.Interaction, member: discord.Member) -> None:
        active = self.active_tournaments.get(interaction.guild_id)
        is_admin = is_tournament_admin(interaction.user) or await self.bot.is_owner(interaction.user)
        if not active or (interaction.user.id != active.reg.host_id and not is_admin):
            return await interaction.response.send_message("❌ Only the tournament host or an admin can advance a player.", ephemeral=True)

        target_match = None
        for m in active.matches:
            if m.state != MatchState.COMPLETED.value and m.winner is None:
                if member.id in (m.player_a, m.player_b):
                    target_match = m
                    break

        if not target_match:
            return await interaction.response.send_message(f"❌ {member.mention} is not in an active unfinished match.", ephemeral=True)

        await self.resolve_match(active, target_match, member.id)
        await interaction.response.send_message(f"👑 Advanced {member.mention} to the next round!")

    @commands.command(name="tourney_bracket", aliases=["tournament_bracket", "tbracket"])
    @commands.guild_only()
    async def tourney_bracket_prefix(self, ctx: commands.Context) -> None:
        """🏆 Show the current live tournament bracket."""
        active = self.active_tournaments.get(ctx.guild.id)
        if not active:
            lobby = self.lobbies.get(ctx.guild.id)
            if not lobby or not lobby.matches:
                return await ctx.send("❌ No tournament bracket is currently running in this server.")
            rnd = min((m.round_no for m in lobby.matches if m.winner is None), default=1)
            return await self._post_bracket(ctx.channel, ctx.guild, lobby, f"Round {rnd}", rnd)

        rnd = min((m.round_no for m in active.matches if m.winner is None), default=1)
        await self._post_v2_bracket(ctx.channel, ctx.guild, active.reg, active.matches, active.blades, f"Live Bracket (Round {rnd})", rnd)

    @app_commands.command(name="tourney_bracket", description="Show the current live tournament bracket")
    @app_commands.guild_only()
    async def tourney_bracket_slash(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        active = self.active_tournaments.get(interaction.guild_id)
        if not active:
            lobby = self.lobbies.get(interaction.guild_id)
            if not lobby or not lobby.matches:
                return await interaction.followup.send("❌ No tournament bracket is currently running in this server.", ephemeral=True)
            rnd = min((m.round_no for m in lobby.matches if m.winner is None), default=1)
            return await self._post_bracket(interaction.channel, interaction.guild, lobby, f"Round {rnd}", rnd)

        rnd = min((m.round_no for m in active.matches if m.winner is None), default=1)
        await self._post_v2_bracket(interaction.channel, interaction.guild, active.reg, active.matches, active.blades, f"Live Bracket (Round {rnd})", rnd)

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

        prize = await self._award(lobby)
        if lobby.champion:
            await channel.send(
                f"🏆 **{name}** takes the tournament — 🪙 **{prize:,}** coins!")
        else:
            await channel.send("The tournament ended without a champion.")

    async def _award(self, lobby: Lobby) -> int:
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
            await mutate_user(lobby.champion,
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

        blade1 = _apply_parts(raw1, await get_user(a))
        blade2 = _apply_parts(raw2, await get_user(b))

        await channel.send(embed=discord.Embed(
            title=f"⚔️  Round {rnd}",
            description=(f"{p1.mention} (**{blade1['name']}**)\n"
                         f"vs {p2.mention} (**{blade2['name']}**)"),
            colour=COLOUR))

        bc = self.bot.get_cog("Battle") if hasattr(self.bot, "get_cog") else None if hasattr(self.bot, "get_cog") else None
        registry = getattr(bc, "active_battles", None) if bc else None
        winner_id = None
        try:
            session = await BattleSession.create(bot=self.bot, channel=channel,
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

    # ── V2 Execution Runner ──────────────────────────────────────────────────
    async def run_v2(self, reg: Registration, panel: RegistrationPanel) -> None:
        """Execute a V2 tournament bracket through to the champion ceremony."""
        try:
            await self._run_v2(reg, panel)
        except Exception:                                # noqa: BLE001
            log.exception("[tournament] V2 tournament execution crashed")
        finally:
            for uid in reg.entrants:
                self._active.discard(uid)
            if self.registrations.get(reg.guild_id) is panel:
                self.registrations.pop(reg.guild_id, None)
            if reg.status != 'cancelled':
                reg.status = 'completed'
            try:
                await asyncio.to_thread(self.registration_store.write, reg)
            except Exception:
                pass
            panel.build()
            if panel.message:
                try:
                    await panel.message.edit(view=panel, allowed_mentions=discord.AllowedMentions.none())
                except Exception:
                    pass

    async def _run_v2(self, reg: Registration, panel: RegistrationPanel) -> None:
        channel = self.bot.get_channel(reg.channel_id)
        guild = self.bot.get_guild(reg.guild_id)
        if channel is None:
            return

        blades: dict[int, str] = {}
        if reg.config.bey_selection == 'Random Beyblade':
            rng = random.Random()
            drafted = draft_blades(rng, len(reg.entrants))
            for uid, b in zip(reg.entrants, drafted):
                blades[uid] = b["name"] if b else ""
        else:
            from cogs.battle.boss.boss_copy import equipped_blade
            for uid in reg.entrants:
                blade_obj, _ = await equipped_blade(uid)
                if blade_obj and blade_obj.get("name"):
                    blades[uid] = blade_obj["name"]
                else:
                    profile = await get_user(uid)
                    inv = profile.get("inventory") or []
                    if inv:
                        blades[uid] = inv[0]
                    else:
                        d = draft_blades(random.Random(), 1)
                        blades[uid] = d[0]["name"] if d and d[0] else "Dranzer S"

        mode = Mode.SINGLE.value
        if reg.config.format == 'Double Elimination':
            mode = Mode.DOUBLE.value
        elif reg.config.format == 'Round Robin':
            mode = Mode.ROUND_ROBIN.value

        try:
            matches = brackets.generate(f"t_{reg.guild_id}", list(reg.entrants), mode)
            brackets.auto_byes(matches, mode, time.time())
        except ValueError as exc:
            await channel.send(f"❌ Could not generate bracket: {exc}")
            return

        active = ActiveV2Tournament(
            guild_id=reg.guild_id,
            reg=reg,
            panel=panel,
            mode=mode,
            matches=matches,
            blades=blades,
            channel=channel,
            guild=guild
        )
        self.active_tournaments[reg.guild_id] = active

        first_rnd = min((m.round_no for m in matches), default=1)
        await self._post_v2_bracket(channel, guild, reg, matches, blades, "Bracket Drawn", first_rnd)

        try:
            while not brackets.is_complete(matches, mode):
                if active.cancelled:
                    reg.status = 'cancelled'
                    return
                ready = [m for m in brackets.ready_matches(matches) if m.winner is None and m.state != MatchState.COMPLETED.value]
                if not ready:
                    break
                rnd = min(m.round_no for m in ready)
                current_round_matches = [m for m in ready if m.round_no == rnd]

                for match in current_round_matches:
                    if match.id not in active.match_events:
                        active.match_events[match.id] = asyncio.Event()
                    await self._announce_v2_match(active, match, rnd)

                await asyncio.gather(*(active.match_events[m.id].wait() for m in current_round_matches))

                if active.cancelled:
                    return

                await self._post_v2_bracket(channel, guild, reg, matches, blades, f"Round {rnd} Results", rnd)

            if not active.cancelled:
                champ_id = brackets.champion(matches, mode)
                standings = self._compute_standings(reg.entrants, matches, champ_id)
                prizes = await self._award_v2(reg, standings)
                await self._post_v2_champion(channel, guild, reg, champ_id, standings, prizes)
        finally:
            self.active_tournaments.pop(reg.guild_id, None)

    async def _play_v2(self, channel, guild, reg: Registration, match: Match,
                       rnd: int, blades: dict[int, str], matches: list[Match], mode: str) -> None:
        """Execute one match with thread isolation if supported."""
        a, b = match.player_a, match.player_b
        if a is None or b is None:
            self._finish_v2(matches, match, a if b is None else b, mode)
            return

        p1 = guild.get_member(a) if guild else None
        if p1 is None and hasattr(self.bot, "get_user"):
            p1 = self.bot.get_user(a)
        if p1 is None and hasattr(self.bot, "fetch_user"):
            try:
                p1 = await self.bot.fetch_user(a)
            except Exception:
                p1 = None

        p2 = guild.get_member(b) if guild else None
        if p2 is None and hasattr(self.bot, "get_user"):
            p2 = self.bot.get_user(b)
        if p2 is None and hasattr(self.bot, "fetch_user"):
            try:
                p2 = await self.bot.fetch_user(b)
            except Exception:
                p2 = None

        if p1 is None or p2 is None:
            winner_id = b if p1 is None else a
            self._finish_v2(matches, match, winner_id, mode)
            return

        p1_name = getattr(p1, "display_name", getattr(p1, "name", f"Player {a}"))
        p2_name = getattr(p2, "display_name", getattr(p2, "name", f"Player {b}"))
        p1_mention = getattr(p1, "mention", f"<@{a}>")
        p2_mention = getattr(p2, "mention", f"<@{b}>")

        from cogs.battle.battle import _apply_parts
        from cogs.battle.session import BattleSession

        bname1 = blades.get(a, "")
        bname2 = blades.get(b, "")
        raw1 = get_beyblade(bname1) or get_beyblade("Dranzer S")
        raw2 = get_beyblade(bname2) or get_beyblade("Dranzer S")
        if not raw1 or not isinstance(raw1, dict):
            raw1 = {"name": bname1 or "Default Beyblade", "stats": {"attack": 50, "defense": 50, "stamina": 50}, "level": 1}
        if not raw2 or not isinstance(raw2, dict):
            raw2 = {"name": bname2 or "Default Beyblade", "stats": {"attack": 50, "defense": 50, "stamina": 50}, "level": 1}

        blade1 = _apply_parts(raw1, await get_user(a))
        blade2 = _apply_parts(raw2, await get_user(b))

        match_channel = channel
        thread = None
        try:
            if hasattr(channel, "create_thread") and guild and guild.me:
                perms = channel.permissions_for(guild.me)
                if perms.create_public_threads and perms.send_messages_in_threads:
                    t_name = f"⚔️ R{rnd}: {p1_name} vs {p2_name}"[:100]
                    thread = await channel.create_thread(name=t_name, auto_archive_duration=60)
                    match_channel = thread
        except Exception:
            match_channel = channel

        await match_channel.send(embed=discord.Embed(
            title=f"⚔️ {reg.config.name} — Round {rnd}",
            description=(f"{p1_mention} (**{blade1['name']}**)\n"
                         f"vs {p2_mention} (**{blade2['name']}**)"),
            colour=COLOUR
        ))

        bc = self.bot.get_cog("Battle") if hasattr(self.bot, "get_cog") else None
        registry = getattr(bc, "active_battles", None) if bc else None
        winner_id = None
        try:
            session = await BattleSession.create(
                bot=self.bot,
                channel=match_channel,
                p1=p1,
                p2=p2,
                blade1=blade1,
                blade2=blade2,
                avatars_enabled=(reg.config.avatars == "Allowed"),
                bey_level=(100 if reg.config.level_rule == "Equalized Level 100" else None),
            )
            if registry is not None:
                registry[a] = session
                registry[b] = session
            await session.run()
            winner_id = getattr(session, "winner_id", None)
        except Exception:
            log.exception("[tournament] match crashed in V2 runner")
            await match_channel.send("⚠️ Match error encountered — advancing on a coin flip.")
        finally:
            if registry is not None:
                registry.pop(a, None)
                registry.pop(b, None)

        if winner_id not in (a, b):
            winner_id = random.choice((a, b))

        self._finish_v2(matches, match, winner_id, mode)

        loser_id = b if winner_id == a else a
        p_win = p1 if winner_id == a else p2
        p_lose = p2 if winner_id == a else p1
        p_win_mention = getattr(p_win, "mention", f"<@{winner_id}>")
        p_lose_mention = getattr(p_lose, "mention", f"<@{loser_id}>")

        if thread:
            try:
                await thread.send(f"🏆 **{p_win_mention} wins and advances!**")
            except Exception:
                pass
            try:
                await channel.send(f"⚔️ **Round {rnd} Match Complete:** {p_win_mention} defeated {p_lose_mention}!")
            except Exception:
                pass
        else:
            await channel.send(f"🏆 **{p_win_mention}** wins round {rnd} against {p_lose_mention}!")

    def _finish_v2(self, matches: list[Match], match: Match, winner: Optional[int], mode: str) -> None:
        match.winner = winner
        match.loser = match.player_b if winner == match.player_a else match.player_a
        match.state = MatchState.COMPLETED.value
        brackets.advance(matches, match, mode)

    async def resolve_match(self, active: ActiveV2Tournament, match: Match, winner_id: int) -> None:
        """Resolve a tournament match with the given winner, advancing the bracket and waking the runner."""
        if match.state == MatchState.COMPLETED.value or match.winner is not None:
            return
        a, b = match.player_a, match.player_b
        if winner_id not in (a, b):
            return

        self._finish_v2(active.matches, match, winner_id, active.mode)
        loser_id = b if winner_id == a else a

        msg = active.match_messages.get(match.id)
        if msg:
            try:
                await msg.edit(view=None)
            except Exception:
                pass

        try:
            await active.channel.send(
                f"🏆 **Round {match.round_no} Match Complete:** <@{winner_id}> defeated <@{loser_id}> and advances!"
            )
        except Exception:
            pass

        evt = active.match_events.get(match.id)
        if evt and not evt.is_set():
            evt.set()

    async def _announce_v2_match(self, active: ActiveV2Tournament, match: Match, rnd: int) -> None:
        """Announce one tournament match with manual battle instructions and controls."""
        a, b = match.player_a, match.player_b
        if a is None or b is None:
            self._finish_v2(active.matches, match, a if b is None else b, active.mode)
            evt = active.match_events.get(match.id)
            if evt and not evt.is_set():
                evt.set()
            return

        guild = active.guild
        p1 = guild.get_member(a) if guild else None
        if p1 is None and hasattr(self.bot, "get_user"):
            p1 = self.bot.get_user(a)
        if p1 is None and hasattr(self.bot, "fetch_user"):
            try:
                p1 = await self.bot.fetch_user(a)
            except Exception:
                p1 = None

        p2 = guild.get_member(b) if guild else None
        if p2 is None and hasattr(self.bot, "get_user"):
            p2 = self.bot.get_user(b)
        if p2 is None and hasattr(self.bot, "fetch_user"):
            try:
                p2 = await self.bot.fetch_user(b)
            except Exception:
                p2 = None

        if p1 is None or p2 is None:
            winner_id = b if p1 is None else a
            self._finish_v2(active.matches, match, winner_id, active.mode)
            evt = active.match_events.get(match.id)
            if evt and not evt.is_set():
                evt.set()
            loser = a if p1 is None else b
            try:
                await active.channel.send(f"⚠️ A player left the server — <@{winner_id}> advances by default over <@{loser}>.")
            except Exception:
                pass
            return

        p1_name = getattr(p1, "display_name", getattr(p1, "name", f"Player {a}"))
        p2_name = getattr(p2, "display_name", getattr(p2, "name", f"Player {b}"))
        p1_mention = getattr(p1, "mention", f"<@{a}>")
        p2_mention = getattr(p2, "mention", f"<@{b}>")

        blade1_name = active.blades.get(a, "Equipped Beyblade")
        blade2_name = active.blades.get(b, "Equipped Beyblade")

        match_channel = active.channel
        thread = None
        try:
            if hasattr(active.channel, "create_thread") and guild and guild.me:
                perms = active.channel.permissions_for(guild.me)
                if perms.create_public_threads and perms.send_messages_in_threads:
                    t_name = f"⚔️ R{rnd}: {p1_name} vs {p2_name}"[:100]
                    thread = await active.channel.create_thread(name=t_name, auto_archive_duration=60)
                    match_channel = thread
        except Exception:
            match_channel = active.channel

        view = TournamentMatchView(self, active, match, p1, p2)
        desc = (
            f"{p1_mention} (**{blade1_name}**)\n"
            f"vs {p2_mention} (**{blade2_name}**)\n\n"
            f"🥊 **Manual Battle:** Run `;battle {p2_mention}` in any channel to fight your match!\n"
            f"*(Or click **Battle Now** below to fight here directly)*\n\n"
            f"-# Either player can use **Forfeit**. The host can use **Advance Player** if an opponent is offline."
        )
        embed = discord.Embed(
            title=f"⚔️ {active.reg.config.name} — Round {rnd}",
            description=desc,
            colour=COLOUR
        )

        msg = await match_channel.send(
            content=f"📢 {p1_mention} {p2_mention} — Your Round {rnd} tournament match is ready!",
            embed=embed,
            view=view
        )
        active.match_messages[match.id] = msg

        if thread and match_channel is not active.channel:
            try:
                t_notice = (
                    f"⚔️ **Round {rnd} Match:** {p1_mention} vs {p2_mention} has started in {thread.mention}!\n"
                    f"Use `;battle` inside the thread or click the button there to fight."
                )
                await active.channel.send(t_notice)
            except Exception:
                pass

    async def _launch_interactive_match_battle(self, active: ActiveV2Tournament, match: Match, channel: discord.abc.Messageable) -> None:
        """Launch an interactive in-channel/thread BattleSession when Battle Now is clicked."""
        a, b = match.player_a, match.player_b
        if a is None or b is None or match.winner is not None:
            return
        guild = active.guild
        p1 = guild.get_member(a) if guild else None
        if p1 is None and hasattr(self.bot, "get_user"):
            p1 = self.bot.get_user(a)
        p2 = guild.get_member(b) if guild else None
        if p2 is None and hasattr(self.bot, "get_user"):
            p2 = self.bot.get_user(b)

        if p1 is None or p2 is None:
            winner_id = b if p1 is None else a
            await self.resolve_match(active, match, winner_id)
            return

        from cogs.battle.battle import _apply_parts
        from cogs.battle.session import BattleSession

        bname1 = active.blades.get(a, "")
        bname2 = active.blades.get(b, "")
        raw1 = get_beyblade(bname1) or get_beyblade("Dranzer S")
        raw2 = get_beyblade(bname2) or get_beyblade("Dranzer S")
        if not raw1 or not isinstance(raw1, dict):
            raw1 = {"name": bname1 or "Default Beyblade", "stats": {"attack": 50, "defense": 50, "stamina": 50}, "level": 1}
        if not raw2 or not isinstance(raw2, dict):
            raw2 = {"name": bname2 or "Default Beyblade", "stats": {"attack": 50, "defense": 50, "stamina": 50}, "level": 1}

        blade1 = _apply_parts(raw1, await get_user(a))
        blade2 = _apply_parts(raw2, await get_user(b))

        bc = self.bot.get_cog("Battle") if hasattr(self.bot, "get_cog") else None
        registry = getattr(bc, "active_battles", None) if bc else None
        try:
            session = await BattleSession.create(
                bot=self.bot,
                channel=channel,
                p1=p1,
                p2=p2,
                blade1=blade1,
                blade2=blade2,
                avatars_enabled=(active.reg.config.avatars == "Allowed"),
                bey_level=(100 if active.reg.config.level_rule == "Equalized Level 100" else None),
            )
            if registry is not None:
                registry[a] = session
                registry[b] = session
            await session.run()
        except Exception:
            log.exception("[tournament] interactive match battle error")
            if match.state != MatchState.COMPLETED.value and match.winner is None:
                try:
                    await channel.send("⚠️ Battle crashed — advancing on a coin flip so bracket doesn't hang.")
                except Exception:
                    pass
                await self.resolve_match(active, match, random.choice((a, b)))
        finally:
            if registry is not None:
                registry.pop(a, None)
                registry.pop(b, None)

    @commands.Cog.listener()
    async def on_beycord_battle_blades(self, result: dict) -> None:
        """Capture results from manual ;battle runs and advance tournament matches."""
        try:
            guild_id = int(result.get("guild_id") or 0)
        except (ValueError, TypeError):
            return
        if not guild_id:
            return
        active = self.active_tournaments.get(guild_id)
        if not active or active.cancelled:
            return

        try:
            participants = {int(p) for p in (result.get("participants") or []) if p is not None}
        except (ValueError, TypeError):
            return
        if len(participants) != 2:
            return

        target_match = None
        for m in active.matches:
            if m.state != MatchState.COMPLETED.value and m.winner is None:
                try:
                    m_parts = {int(m.player_a), int(m.player_b)}
                except (ValueError, TypeError):
                    continue
                if m_parts == participants:
                    target_match = m
                    break

        if not target_match:
            return

        if result.get("draw") or result.get("winner_id") is None:
            try:
                draw_notice = (
                    f"⚠️ Tournament match between <@{target_match.player_a}> and <@{target_match.player_b}> ended in a **DRAW**!\n"
                    f"Please rematch using `;battle` to determine who advances!"
                )
                await active.channel.send(draw_notice)
            except Exception:
                pass
            return

        try:
            winner_id = int(result.get("winner_id"))
        except (ValueError, TypeError):
            winner_id = None

        if winner_id:
            await self.resolve_match(active, target_match, winner_id)

    def _compute_standings(self, entrants: list[int], matches: list[Match], champion_id: Optional[int]) -> dict[str, Optional[int]]:
        standings: dict[str, Optional[int]] = {"first": champion_id, "second": None, "third": None}
        if not champion_id or not matches:
            return standings

        final_matches = [m for m in matches if m.winner == champion_id]
        if final_matches:
            final_match = max(final_matches, key=lambda m: m.round_no)
            standings["second"] = final_match.loser
            semi_rnd = final_match.round_no - 1
            if semi_rnd >= 1:
                semi_losers = [m.loser for m in matches if m.round_no == semi_rnd and m.loser not in (champion_id, standings["second"]) and m.loser is not None]
                if semi_losers:
                    standings["third"] = semi_losers[0]
        return standings

    async def _award_v2(self, reg: Registration, standings: dict[str, Optional[int]]) -> dict[int, int]:
        num_entrants = max(1, len(reg.entrants))
        base_pot = PRIZE_PER_ENTRANT * num_entrants
        fee_pot = reg.config.entry_fee * num_entrants
        total_pot = base_pot + fee_pot

        first_id = standings.get("first")
        second_id = standings.get("second")
        third_id = standings.get("third")

        payouts: dict[int, int] = {}
        if len(reg.entrants) == 2 or not second_id:
            if first_id:
                payouts[first_id] = total_pot
        elif len(reg.entrants) < 8 or not third_id:
            if first_id:
                payouts[first_id] = int(total_pot * 0.70)
            if second_id:
                payouts[second_id] = total_pot - payouts.get(first_id, 0)
        else:
            if first_id:
                payouts[first_id] = int(total_pot * 0.70)
            if second_id:
                payouts[second_id] = int(total_pot * 0.20)
            if third_id:
                payouts[third_id] = total_pot - payouts.get(first_id, 0) - payouts.get(second_id, 0)

        for uid, prize in payouts.items():
            if prize > 0:
                try:
                    await mutate_user(uid, lambda p, a=prize: p.__setitem__("coins", int(p.get("coins", 0)) + a))
                except Exception:
                    log.exception("[tournament] failed to pay prize to %s", uid)
        return payouts

    async def _post_v2_bracket(self, channel, guild, reg: Registration, matches: list[Match],
                               blades: dict[int, str], label: str, rnd: int) -> None:
        embed = discord.Embed(
            title=f"🏆  {reg.config.name} — {label}",
            colour=COLOUR
        )
        round_matches = [m for m in matches if m.round_no == rnd]
        lines = []
        for m in round_matches:
            p1_name = self._name(guild, m.player_a) if m.player_a else "TBD"
            p2_name = self._name(guild, m.player_b) if m.player_b else ("*bye*" if m.player_b is None else "TBD")
            b1 = f" ({blades.get(m.player_a, '')})" if m.player_a in blades and blades[m.player_a] else ""
            b2 = f" ({blades.get(m.player_b, '')})" if m.player_b in blades and blades[m.player_b] else ""
            if m.winner is not None:
                w_name = self._name(guild, m.winner)
                lines.append(f"✅ **{w_name}** won")
            else:
                lines.append(f"⚔️ **{p1_name}**{b1} vs **{p2_name}**{b2}")
        embed.description = "\n".join(lines) if lines else "No matches in this round."
        try:
            await channel.send(embed=embed)
        except Exception:
            log.exception("[tournament] failed to post v2 bracket")

    async def _post_v2_champion(self, channel, guild, reg: Registration,
                                champion_id: Optional[int], standings: dict, prizes: dict[int, int]) -> None:
        embed = discord.Embed(
            title=f"🏆  {reg.config.name} — Champion Crowned! 🏆",
            colour=0xF1C40F
        )
        if champion_id:
            c_name = self._name(guild, champion_id)
            p1_amt = prizes.get(champion_id, 0)
            lines = [f"🥇 **Champion:** <@{champion_id}> ({c_name}) — 🪙 **{p1_amt:,}** Beycoins!"]
            sec_id = standings.get("second")
            if sec_id:
                p2_amt = prizes.get(sec_id, 0)
                s_name = self._name(guild, sec_id)
                lines.append(f"🥈 **Runner-Up:** <@{sec_id}> ({s_name}) — 🪙 **{p2_amt:,}** Beycoins")
            thd_id = standings.get("third")
            if thd_id:
                p3_amt = prizes.get(thd_id, 0)
                t_name = self._name(guild, thd_id)
                lines.append(f"🥉 **3rd Place:** <@{thd_id}> ({t_name}) — 🪙 **{p3_amt:,}** Beycoins")
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Format: {reg.config.format} · {len(reg.entrants)} Entrants")
            try:
                await channel.send(content=f"🎉 Congratulations to <@{champion_id}> for winning **{reg.config.name}**!", embed=embed)
            except Exception:
                pass
        else:
            await channel.send("The tournament ended without a champion.")

    # ── admin hooks, called by cogs/admin/actions.py ──────────────────────────
    async def admin_announce(self, channel, author, note: str = "") -> bool:
        """Open a tournament and post the panel into `channel`.

        Posts the host configuration editor; the host chooses where to publish registration.
        Announcement text is omitted because Components V2 uses TextDisplay.

        Returns False when a tournament is already open here or `author` isn't
        an admin; `open_panel` says which through the sender.
        """
        posted: list = []

        async def send(content=None, *, embed=None, view=None, ephemeral=False):
            # A refusal (ephemeral=True) has nowhere to go in a channel that is
            # not where the command was typed, so it is swallowed here and
            # reported through the return value instead of being announced to
            # everyone reading the announcement channel.
            if ephemeral:
                posted.append(None)
                return None
            if isinstance(view, HostPanel):
                msg = await channel.send(view=view)
            else:
                msg = await channel.send(content=note or None, embed=embed, view=view)
            posted.append(msg)
            return msg

        await self.open_panel(send, getattr(channel, "guild", None), author,
                              channel)
        return bool(posted) and posted[-1] is not None

    def admin_lobby(self, guild_id: int) -> Optional[Lobby]:
        return self.lobbies.get(guild_id)

    def admin_start(self, guild_id: int) -> bool:
        """Force-start the open lobby or V2 registration. False when there is nothing to start."""
        registration = self.registrations.get(guild_id)
        if registration is not None and registration.reg.status in ('open', 'closed') and len(registration.reg.entrants) >= 2:
            registration.reg.status = 'running'
            self._spawn(self.run_v2(registration.reg, registration))
            return True
        lobby = self.lobbies.get(guild_id)
        view = self.panels.get(guild_id)
        if not lobby or not view or lobby.started or len(lobby.entrants) < MIN_PLAYERS:
            return False
        self._spawn(self.run(view))
        return True

    async def admin_cancel(self, guild_id: int) -> bool:
        """Close the lobby AND its panel. False when there is nothing open."""
        active = self.active_tournaments.get(guild_id)
        if active is not None:
            active.cancelled = True
            active.reg.status = 'cancelled'
            for evt in active.match_events.values():
                evt.set()
            if active.channel:
                try:
                    await active.channel.send("✖️ Tournament cancelled by an admin.")
                except Exception:
                    pass
            registration = self.registrations.get(guild_id)
            if registration is not None:
                await registration.close()
            return True

        registration = self.registrations.get(guild_id)
        if registration is not None:
            await registration.close()
            return True
        setup_panel = self.setups.get(guild_id)
        if setup_panel is not None:
            await setup_panel.close("✖️ Setup cancelled by an admin.")
            return True
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
        the admin surface was previously hand-rolling half of from outside. A
        third thing joining that invariant later would have silently stopped
        being maintained there.
        """
        self.banned.add(user_id)
        registration = self.registrations.get(guild_id)
        if registration is not None and user_id in registration.reg.entrants:
            self._spawn(registration.remove_banned(user_id))
        lobby = self.lobbies.get(guild_id)
        if lobby and user_id in lobby.entrants:
            lobby.entrants.remove(user_id)
            self._active.discard(user_id)

    def admin_force_win(self, guild_id: int, user_id: int) -> bool:
        """Advance a player out of their unfinished match. False if none."""
        active = self.active_tournaments.get(guild_id)
        if active and not active.cancelled:
            for m in active.matches:
                if m.winner is None and user_id in (m.player_a, m.player_b) and m.state != MatchState.COMPLETED.value:
                    self._spawn(self.resolve_match(active, m, user_id))
                    return True

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
