"""
cogs/community/memory.py — what the bot remembers. Imports no discord.

Four axes, and they are deliberately not stored the same way, because they do
not deserve the same durability:

    rapport   DURABLE   on the player profile. Losing "we've talked 400 times"
                        to a restart would be felt, so it is written down.
    memory    DERIVED   read off the profile the game already keeps — level,
                        wins, losses, favourite bey. Nothing new is recorded
                        to support it; this is projection, not bookkeeping.
    mood      VOLATILE  in memory, decays back to baseline. A restart forgets
                        that the bot was in a good mood, which costs nothing.
    room      VOLATILE  in memory, a short rolling window per channel. Same.

`cooldowns.py` already states this rule for timers — "in memory for things
where a restart resetting the timer costs nothing, on the profile where a
restart resetting it is an exploit" — and this module follows it rather than
inventing a second policy.

Why no new table
----------------
`XPManager` puts per-user community state on the profile under prefixed keys
(`community_xp`, `com_level`, `com_day`). Adding a table instead would mean
adding it to BOTH backends — the `store_parity()` hazard `utils/buildinfo.py`
exists to catch, where SQLite gets a method MySQL doesn't and the bot breaks
the moment MYSQL_URL is set. Living on the profile also means chat memory is
inside the daily snapshot for free.

Everything here is a pure function of `(profile, now)` or of an explicit
event, so the whole module is testable with no Discord, no database and no
clock of its own.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from . import cooldowns as CD

# ── Profile keys ─────────────────────────────────────────────────────────────
# `chat_` prefix, matching the `com_` convention XPManager set. NOT `level` or
# any other name `get_user` recomputes — see xp.py's note about
# `profile["level"]` being overwritten within milliseconds of being written.
K_RAPPORT = "chat_rapport"       # int, lifetime
K_FIRST   = "chat_first_seen"    # unix ts, first time the bot spoke to them
K_LAST    = "chat_last_seen"     # unix ts, most recent exchange
K_DAY     = "chat_day"           # {"date": "...", "rapport": n} daily cap
K_OPTOUT  = "chat_optout"        # bool — ";chat off"

# ── Rapport ──────────────────────────────────────────────────────────────────
# Capped per day for the same reason XP is: without it, rapport measures how
# much you spammed rather than how much you talked, and the warmest lines in
# the bot go to whoever held down enter the longest.
RAPPORT_PER_EXCHANGE = 1
RAPPORT_DAILY_CAP    = 25

# Thresholds are lifetime totals. Named rather than numeric at the call site so
# the prompt says "a familiar regular" instead of leaking "rapport 61".
TIERS = (
    (0,   "stranger"),
    (10,  "regular"),
    (60,  "familiar"),
    (250, "ride-or-die"),
)


def tier_for(rapport: int) -> str:
    name = TIERS[0][1]
    for floor, label in TIERS:
        if rapport >= floor:
            name = label
    return name


def rapport_of(profile: dict) -> int:
    try:
        return int((profile or {}).get(K_RAPPORT) or 0)
    except (TypeError, ValueError):
        return 0


def opted_out(profile: dict) -> bool:
    return bool((profile or {}).get(K_OPTOUT))


def note_exchange(profile: dict, now: Optional[float] = None) -> dict:
    """Record one interaction. Mutates `profile`; returns what changed.

    Written to be called INSIDE a `mutate_user` callback, like
    `XPManager.grant`, so the read-modify-write is under the process-wide
    profile lock rather than racing another writer.
    """
    ts = time.time() if now is None else now
    before = rapport_of(profile)

    if not profile.get(K_FIRST):
        profile[K_FIRST] = ts
    profile[K_LAST] = ts

    today = CD.day_total(profile, K_DAY, "rapport", ts)
    gained = 0
    if today < RAPPORT_DAILY_CAP:
        gained = RAPPORT_PER_EXCHANGE
        profile[K_RAPPORT] = before + gained
        CD.bump_day(profile, K_DAY, "rapport", gained, ts)

    after = rapport_of(profile)
    return {
        "gained":     gained,
        "rapport":    after,
        "tier":       tier_for(after),
        "tier_up":    tier_for(before) != tier_for(after),
        "capped":     gained == 0,
    }


# ── What the bot knows about a person ────────────────────────────────────────

def recall(profile: dict, *, display_name: str = "",
           now: Optional[float] = None) -> dict:
    """Everything worth putting in a prompt about one player.

    Reads only. Every field is derived from data the game already stores, so
    this stays correct without anything else having to remember to update it.
    """
    ts = time.time() if now is None else now
    profile = profile or {}
    rap = rapport_of(profile)

    last = profile.get(K_LAST)
    try:
        away_days = max(0.0, (ts - float(last)) / 86400.0) if last else None
    except (TypeError, ValueError):
        away_days = None

    wins = _int(profile.get("wins"))
    losses = _int(profile.get("losses"))
    total = wins + losses

    return {
        "name":        display_name or "",
        "rapport":     rap,
        "tier":        tier_for(rap),
        "known":       bool(profile.get(K_FIRST)),
        "away_days":   away_days,
        "returning":   bool(away_days is not None and away_days >= 3),
        "level":       _int(profile.get("level")),
        "com_level":   _int(profile.get("com_level")),
        "wins":        wins,
        "losses":      losses,
        "matches":     total,
        "win_pct":     (100.0 * wins / total) if total else None,
        "favourite":   _favourite_bey(profile),
        "streak":      _int(profile.get("win_streak")),
    }


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _favourite_bey(profile: dict) -> str:
    """The blade they actually play, best-effort.

    The equipped blade is the honest answer and the cheapest — an inventory
    scan would need the roster and would still only tell us what they own.
    Several key names have been used across builds, so all are tried rather
    than assuming whichever one this build happens to write.
    """
    for key in ("equipped_beyblade", "equipped_bey", "equipped", "current_bey"):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return ""


# ── Mood ─────────────────────────────────────────────────────────────────────
# One scalar per guild in [-1, 1]. Nudged by events, pulled back to 0 over
# time. It colours the prompt; it never gates whether the bot speaks, because
# a bot that goes silent when "sad" reads as broken rather than as moody.

MOOD_DECAY_HALFLIFE = 900.0     # 15 minutes to lose half the distance from 0
MOOD_MIN, MOOD_MAX = -1.0, 1.0

# What moves it, and by how much. Small numbers: mood is a tint, not a switch.
MOOD_EVENTS = {
    "boss_defeated":   0.35,
    "rare_pull":       0.30,
    "milestone":       0.25,
    "praise_given":    0.10,
    "got_roasted":    -0.20,
    "player_lost":    -0.10,
    "long_silence":   -0.15,
}

MOOD_LABELS = (
    (-1.00, "flat"),
    (-0.25, "level"),
    (0.25,  "good"),
    (0.60,  "buzzing"),
)


class Mood:
    """Per-guild mood with exponential decay toward neutral."""

    def __init__(self, halflife: float = MOOD_DECAY_HALFLIFE) -> None:
        self.halflife = float(halflife)
        self._value: dict = {}      # guild_id -> (value, stamped_at)
        self._lock = threading.Lock()

    def value(self, guild_id, now: Optional[float] = None) -> float:
        ts = time.time() if now is None else now
        with self._lock:
            entry = self._value.get(guild_id)
        if not entry:
            return 0.0
        value, at = entry
        elapsed = max(0.0, ts - at)
        # 0.5 ** (elapsed / halflife) — half the distance to neutral per
        # halflife, so it approaches 0 without ever snapping to it.
        return value * (0.5 ** (elapsed / self.halflife)) if self.halflife > 0 else 0.0

    def nudge(self, guild_id, event: str, now: Optional[float] = None) -> float:
        """Apply a named event. Unknown events are a no-op, not an error."""
        delta = MOOD_EVENTS.get(event)
        if delta is None:
            return self.value(guild_id, now)
        ts = time.time() if now is None else now
        current = self.value(guild_id, ts)
        new = max(MOOD_MIN, min(MOOD_MAX, current + delta))
        with self._lock:
            self._value[guild_id] = (new, ts)
        return new

    def label(self, guild_id, now: Optional[float] = None) -> str:
        value = self.value(guild_id, now)
        name = MOOD_LABELS[0][1]
        for floor, text in MOOD_LABELS:
            if value >= floor:
                name = text
        return name

    def clear(self) -> None:
        with self._lock:
            self._value.clear()


# ── Room ─────────────────────────────────────────────────────────────────────
# How lively a channel is right now, from a short rolling window. Used both to
# decide whether unprompted banter is welcome and to pick which pool to draw
# from — chiming into a dead channel and into a busy one are different acts.

ROOM_WINDOW   = 300.0    # five minutes
ROOM_MAX_KEEP = 60       # per channel; bounded so an active server can't grow

# Tuned to be conservative about "busy": the cost of misreading a quiet channel
# as busy is the bot talking into silence, which is the failure people actually
# notice.
BUSY_MESSAGES  = 12
BUSY_SPEAKERS  = 3
QUIET_MESSAGES = 3


class Room:
    """Per-channel activity, as a bounded ring of (timestamp, speaker)."""

    def __init__(self, window: float = ROOM_WINDOW) -> None:
        self.window = float(window)
        self._seen: dict = {}       # channel_id -> [(ts, user_id), ...]
        self._lock = threading.Lock()

    def observe(self, channel_id, user_id, now: Optional[float] = None) -> None:
        ts = time.time() if now is None else now
        with self._lock:
            ring = [e for e in self._seen.get(channel_id, ())
                    if ts - e[0] < self.window]
            ring.append((ts, user_id))
            self._seen[channel_id] = ring[-ROOM_MAX_KEEP:]

    def stats(self, channel_id, now: Optional[float] = None) -> dict:
        ts = time.time() if now is None else now
        with self._lock:
            ring = [e for e in self._seen.get(channel_id, ())
                    if ts - e[0] < self.window]
        return {"messages": len(ring),
                "speakers": len({u for _, u in ring})}

    def state(self, channel_id, now: Optional[float] = None) -> str:
        """`quiet` / `normal` / `busy`."""
        s = self.stats(channel_id, now)
        if s["messages"] >= BUSY_MESSAGES and s["speakers"] >= BUSY_SPEAKERS:
            return "busy"
        if s["messages"] <= QUIET_MESSAGES:
            return "quiet"
        return "normal"

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


# ── Conversation ─────────────────────────────────────────────────────────────
# Every addressed reply used to be answered in isolation — the prompt carried
# who the player is, but nothing about what either side had just said. That
# makes "chat with the bot" a series of unrelated one-liners rather than a
# conversation: reply to its own joke and it has no idea there was one.
#
# In memory, per `cooldowns.py`'s own rule: losing a conversation thread to a
# restart costs nothing a player would notice, unlike rapport. A THREAD is
# also scoped to (channel, user) rather than to the user alone, because the
# same two people talking in #general and in #bot-spam are not continuing one
# conversation just because the same player is on both ends of it.

CONVO_TURNS   = 3        # exchanges kept — 3 of each side, 6 lines total
CONVO_WINDOW  = 900.0    # 15 minutes of silence ends a thread
CONVO_MAX_KEEP = 4000    # distinct (channel, user) threads across the process


class Conversation:
    """A short, bounded memory of the last few things said back and forth.

    Nothing here is a transcript of the whole channel — only of one player's
    exchanges with the bot, and only the recent ones. A model handed the
    entire channel history would be answering everyone at once; a model handed
    nothing has no idea it already made this joke twice.
    """

    def __init__(self, turns: int = CONVO_TURNS,
                 window: float = CONVO_WINDOW) -> None:
        self.turns = int(turns)
        self.window = float(window)
        self._threads: dict = {}    # (channel_id, user_id) -> [(who, text, ts), ...]
        self._lock = threading.Lock()

    def _key(self, channel_id, user_id):
        return (channel_id, user_id)

    def recall(self, channel_id, user_id,
              now: Optional[float] = None) -> list[tuple[str, str]]:
        """`[(who, text), ...]` oldest first, or [] once the thread has gone
        quiet for `window` seconds — a conversation from an hour ago is not
        this one."""
        ts = time.time() if now is None else now
        key = self._key(channel_id, user_id)
        with self._lock:
            turns = self._threads.get(key, ())
            if not turns or ts - turns[-1][2] > self.window:
                return []
            return [(who, text) for who, text, _ in turns]

    def remember(self, channel_id, user_id, *, said: str, replied: str,
                now: Optional[float] = None) -> None:
        """Record one exchange. Mutates nothing the caller passed in.

        Stale turns are dropped here rather than only at read time, so a
        conversation that has gone quiet starts genuinely fresh instead of
        the old thread quietly resuming the moment two entries fall back
        inside the window by coincidence.
        """
        ts = time.time() if now is None else now
        key = self._key(channel_id, user_id)
        with self._lock:
            turns = list(self._threads.get(key, ()))
            if turns and ts - turns[-1][2] > self.window:
                turns = []
            turns.append(("them", said, ts))
            turns.append(("you", replied, ts))
            # 2 lines per turn (them + you), so the cap is doubled.
            self._threads[key] = turns[-(self.turns * 2):]
            if len(self._threads) > CONVO_MAX_KEEP:
                # Drop the stalest half rather than growing without bound —
                # the same trade `cooldowns.Bucket` makes, for the same
                # reason: forgetting an idle thread is harmless.
                by_age = sorted(self._threads.items(),
                                key=lambda kv: kv[1][-1][2] if kv[1] else 0)
                for stale_key, _ in by_age[:len(by_age) // 2]:
                    self._threads.pop(stale_key, None)

    def active(self, channel_id, user_id, now: Optional[float] = None) -> bool:
        """Is there a live thread right now — used to pick the opening
        moment ("mentioned" vs a mid-conversation reply)."""
        return bool(self.recall(channel_id, user_id, now))

    def clear(self) -> None:
        with self._lock:
            self._threads.clear()
