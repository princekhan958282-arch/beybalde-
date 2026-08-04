"""
models.py — tournament domain types and the match state machine.

Pure data and pure rules. Nothing here imports discord or touches a database,
so the whole lifecycle can be exercised headlessly in tests and simulations.

Every time value in this package is a POSIX timestamp in UTC (float seconds).
Local times exist only at the edges, when a notification is rendered for one
player. Storing local time is what makes tournament bots drift across DST.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


# ── Enums ─────────────────────────────────────────────────────────────────────

class Mode(str, Enum):
    SINGLE = "single"
    DOUBLE = "double"
    ROUND_ROBIN = "round_robin"


class TournamentState(str, Enum):
    SIGNUP = "signup"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MatchState(str, Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    CHECKIN = "checkin"
    ACTIVE = "active"
    COMPLETED = "completed"
    FORFEIT = "forfeit"


# The only legal moves. Anything else raises — a tournament that lets a match
# jump from PENDING straight to COMPLETED is a tournament that can be cheated,
# and silent bad transitions are exactly how brackets end up unexplainable.
VALID_TRANSITIONS: dict[MatchState, frozenset[MatchState]] = {
    MatchState.PENDING:   frozenset({MatchState.SCHEDULED, MatchState.FORFEIT}),
    MatchState.SCHEDULED: frozenset({MatchState.CHECKIN, MatchState.SCHEDULED,
                                     MatchState.FORFEIT}),
    MatchState.CHECKIN:   frozenset({MatchState.ACTIVE, MatchState.FORFEIT,
                                     MatchState.SCHEDULED}),
    MatchState.ACTIVE:    frozenset({MatchState.COMPLETED, MatchState.FORFEIT}),
    MatchState.COMPLETED: frozenset(),
    MatchState.FORFEIT:   frozenset(),
}
# SCHEDULED -> SCHEDULED is deliberate: that is a reschedule.
# CHECKIN -> SCHEDULED is the both-players-missed reschedule path.


class TransitionError(RuntimeError):
    """Raised when code tries to move a match somewhere it cannot go."""


def can_transition(src: MatchState, dst: MatchState) -> bool:
    return dst in VALID_TRANSITIONS.get(src, frozenset())


# ── Records ───────────────────────────────────────────────────────────────────

def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class Player:
    """A registration. `slots` is availability in the player's LOCAL time; the
    UTC conversion happens in timeslots.py so the stored value stays meaningful
    if the player later corrects their offset."""
    user_id: int
    country: str = ""
    utc_offset: float = 0.0            # hours, e.g. 5.5 for IST. Never a string.
    slots: list[dict] = field(default_factory=list)
    elo: int = 1000
    no_shows: int = 0
    banned_until: float = 0.0
    ban_reason: str = ""
    registered_at: float = field(default_factory=time.time)

    def is_banned(self, now: Optional[float] = None) -> bool:
        return self.banned_until > (now if now is not None else time.time())


@dataclass
class Match:
    id: str = field(default_factory=lambda: _new_id("m"))
    tournament_id: str = ""
    round_no: int = 1
    bracket: str = "winners"           # winners | losers | grand_final | rr
    slot: int = 0                      # position within the round
    player_a: Optional[int] = None
    player_b: Optional[int] = None
    state: str = MatchState.PENDING.value
    scheduled_for: float = 0.0         # UTC epoch seconds
    checkin_opened_at: float = 0.0
    checked_in: list[int] = field(default_factory=list)
    winner: Optional[int] = None
    loser: Optional[int] = None
    score: str = ""
    dispute: str = ""
    # Explicit progression links, set at generation time:
    #   ("<match id>", "a"|"b")  or  None
    # Inferring the next match from round/slot arithmetic works for single
    # elimination and quietly breaks on double elimination, where losers-bracket
    # rounds alternate between absorbing winners-bracket droppers (width stays)
    # and pairing off (width halves). Storing the edge removes the guesswork —
    # and lets a half-filled match tell the difference between "this is a bye"
    # and "the other player hasn't arrived yet".
    winner_to: Optional[list] = None
    loser_to: Optional[list] = None
    # One timestamp per phase entered, so an admin can always reconstruct what
    # happened and when without a separate audit log.
    history: dict = field(default_factory=dict)

    @property
    def players(self) -> list[int]:
        return [p for p in (self.player_a, self.player_b) if p is not None]

    def is_bye(self) -> bool:
        return len(self.players) == 1

    def opponent_of(self, user_id: int) -> Optional[int]:
        if user_id == self.player_a:
            return self.player_b
        if user_id == self.player_b:
            return self.player_a
        return None

    def transition(self, dst: MatchState, now: Optional[float] = None) -> None:
        src = MatchState(self.state)
        if not can_transition(src, dst):
            raise TransitionError(
                f"match {self.id}: {src.value} -> {dst.value} is not allowed")
        self.state = dst.value
        self.history[dst.value] = now if now is not None else time.time()


@dataclass
class Tournament:
    id: str = field(default_factory=lambda: _new_id("t"))
    guild_id: int = 0
    channel_id: int = 0
    name: str = "Tournament"
    mode: str = Mode.SINGLE.value
    max_players: int = 16
    start_time: float = 0.0            # UTC epoch seconds
    state: str = TournamentState.SIGNUP.value
    entrants: list[int] = field(default_factory=list)
    use_elo: bool = True
    reward_type: str = ""              # role | currency | item
    reward_value: str = ""
    created_at: float = field(default_factory=time.time)
    created_by: int = 0

    def is_full(self) -> bool:
        return len(self.entrants) >= self.max_players


def to_dict(obj) -> dict:
    return asdict(obj)
