"""
service.py — the orchestration layer.

Everything that changes tournament state goes through here: registration,
joining, starting, scheduling, check-in, reporting, forfeits, admin overrides.
The Discord cog calls these methods and renders the results; it contains no
rules of its own, so the entire lifecycle can be driven headlessly in a test.

Methods return (ok: bool, message: str, payload) so the caller can reply without
having to know why something failed.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from . import brackets, timeslots as ts
from .models import (Match, MatchState, Mode, Player, Tournament,
                     TournamentState, TransitionError)
from .store import Store

log = logging.getLogger("beyblade_bot.tournament")

# ── Tunables. No rule below is hardcoded at its use site. ─────────────────────
CHECKIN_LEAD_MIN   = 10      # check-in opens this long before kickoff
CHECKIN_WINDOW_MIN = 10      # players have this long to confirm
MATCH_DURATION_MIN = 30      # reserved per match, for conflict detection
NO_SHOW_BAN_LIMIT  = 3       # no-shows before an automatic ban
NO_SHOW_BAN_DAYS   = 7
JOIN_COOLDOWN_MIN  = 5       # between leaving and re-joining anything
ELO_K              = 32
DEFAULT_ELO        = 1000


def expected_score(a: int, b: int) -> float:
    return 1.0 / (1.0 + 10 ** ((b - a) / 400.0))


def elo_update(winner: int, loser: int, k: int = ELO_K) -> tuple[int, int]:
    exp_w = expected_score(winner, loser)
    return (round(winner + k * (1 - exp_w)),
            round(loser + k * (0 - (1 - exp_w))))


class TournamentService:
    def __init__(self, store: Optional[Store] = None):
        self.store = store or Store()
        self._recent_leave: dict[int, float] = {}

    # ── Registration ─────────────────────────────────────────────────────────

    def register(self, user_id: int, country: str, offset_raw,
                 slots_raw: list[dict]) -> tuple[bool, str, Optional[Player]]:
        try:
            offset = ts.parse_offset(offset_raw)
            slots = ts.validate_slots(slots_raw)
        except ts.SlotError as e:
            return False, str(e), None
        country = (country or "").strip()[:60]
        existing = self.store.get_player(user_id)
        p = existing or Player(user_id=user_id)
        # Re-registering updates the profile rather than being rejected as a
        # duplicate — people move and fix typos. Duplicate ENTRY into one
        # tournament is what actually needs blocking, and join() does that.
        p.country, p.utc_offset, p.slots = country, offset, slots
        self.store.put_player(p)
        return True, ("Profile updated." if existing else "Registered."), p

    # ── Tournament lifecycle ─────────────────────────────────────────────────

    def create(self, guild_id: int, channel_id: int, name: str, mode: str,
               max_players: int, start_time: float, created_by: int
               ) -> tuple[bool, str, Optional[Tournament]]:
        if mode not in {m.value for m in Mode}:
            return False, f"Unknown mode `{mode}`.", None
        if not (2 <= max_players <= 128):
            return False, "max_players must be between 2 and 128.", None
        t = Tournament(guild_id=guild_id, channel_id=channel_id,
                       name=(name or "Tournament").strip()[:80], mode=mode,
                       max_players=max_players, start_time=start_time,
                       created_by=created_by)
        self.store.put_tournament(t)
        return True, f"Created **{t.name}** (`{t.id}`).", t

    def join(self, tid: str, user_id: int) -> tuple[bool, str, Optional[Tournament]]:
        t = self.store.get_tournament(tid)
        if not t:
            return False, "No tournament with that id.", None
        if t.state != TournamentState.SIGNUP.value:
            return False, "Signups are closed.", None
        if user_id in t.entrants:
            return False, "You're already in this tournament.", None
        if t.is_full():
            return False, "This tournament is full.", None

        p = self.store.get_player(user_id)
        if not p:
            return False, ("Register first — `/tournament join` needs your "
                           "timezone and availability."), None
        if p.is_banned():
            return False, f"You're banned from tournaments: {p.ban_reason}", None
        left_at = self._recent_leave.get(user_id, 0)
        if time.time() - left_at < JOIN_COOLDOWN_MIN * 60:
            wait = int((JOIN_COOLDOWN_MIN * 60 - (time.time() - left_at)) / 60) + 1
            return False, f"Join cooldown — try again in {wait} min.", None

        t.entrants.append(user_id)
        self.store.put_tournament(t)
        return True, f"Joined **{t.name}** ({len(t.entrants)}/{t.max_players}).", t

    def leave(self, tid: str, user_id: int) -> tuple[bool, str, Optional[Tournament]]:
        t = self.store.get_tournament(tid)
        if not t:
            return False, "No tournament with that id.", None
        if t.state != TournamentState.SIGNUP.value:
            return False, "Too late — the tournament has already started.", None
        if user_id not in t.entrants:
            return False, "You're not in this tournament.", None
        t.entrants.remove(user_id)
        self.store.put_tournament(t)
        self._recent_leave[user_id] = time.time()
        return True, f"Left **{t.name}**.", t

    def start(self, tid: str, now: Optional[float] = None
              ) -> tuple[bool, str, list[Match]]:
        now = now or time.time()
        t = self.store.get_tournament(tid)
        if not t:
            return False, "No tournament with that id.", []
        if t.state != TournamentState.SIGNUP.value:
            return False, "Already started.", []
        if len(t.entrants) < 2:
            return False, "Need at least 2 entrants.", []

        entrants = self._seed(t)
        try:
            matches = brackets.generate(t.id, entrants, t.mode)
        except ValueError as e:
            return False, str(e), []
        brackets.auto_byes(matches, t.mode, now)
        t.state = TournamentState.RUNNING.value
        self.store.put_tournament(t)
        self.store.put_matches(matches)
        self.schedule_ready(t.id, now)
        return True, f"**{t.name}** has begun.", self.store.get_matches(t.id)

    def _seed(self, t: Tournament) -> list[int]:
        """Seed by ELO when enabled, so the bracket means something."""
        if not t.use_elo:
            return list(t.entrants)
        players = self.store.get_players(t.entrants)
        return sorted(t.entrants,
                      key=lambda u: -(players[u].elo if u in players
                                      else DEFAULT_ELO))

    def set_state(self, tid: str, state: TournamentState) -> tuple[bool, str]:
        t = self.store.get_tournament(tid)
        if not t:
            return False, "No tournament with that id."
        t.state = state.value
        self.store.put_tournament(t)
        return True, f"**{t.name}** is now {state.value}."

    # ── Scheduling ───────────────────────────────────────────────────────────

    def schedule_ready(self, tid: str, now: Optional[float] = None) -> list[Match]:
        """Give every ready match a kickoff time both players can make."""
        now = now or time.time()
        t = self.store.get_tournament(tid)
        if not t or t.state != TournamentState.RUNNING.value:
            return []
        matches = self.store.get_matches(tid)
        ready = brackets.ready_matches(matches)
        if not ready:
            return []
        players = self.store.get_players(
            [u for m in ready for u in m.players])
        floor = max(now, t.start_time or 0)
        scheduled: list[Match] = []
        for m in ready:
            a, b = m.player_a, m.player_b
            pa, pb = players.get(a), players.get(b)
            if not pa or not pb:
                continue
            busy = (self.store.player_bookings(a, m.id)
                    + self.store.player_bookings(b, m.id))
            when = ts.best_match_time(pa.slots, pa.utc_offset,
                                      pb.slots, pb.utc_offset,
                                      after=floor,
                                      duration_min=MATCH_DURATION_MIN,
                                      busy=busy)
            if when is None:
                # No shared availability at all. Fall back to the tournament
                # start rather than leaving the match unscheduled forever —
                # an admin can reschedule, but the bracket keeps moving.
                when = floor + 3600
                log.info("no overlap for match %s; using fallback slot", m.id)
            m.scheduled_for = when
            try:
                m.transition(MatchState.SCHEDULED, now)
            except TransitionError:
                continue
            scheduled.append(m)
        self.store.put_matches(scheduled)
        return scheduled

    def reschedule(self, mid: str, new_time: float,
                   now: Optional[float] = None) -> tuple[bool, str, Optional[Match]]:
        now = now or time.time()
        m = self.store.get_match(mid)
        if not m:
            return False, "No match with that id.", None
        if m.state in (MatchState.COMPLETED.value, MatchState.FORFEIT.value):
            return False, "That match is already finished.", None
        for u in m.players:
            for bs, be in self.store.player_bookings(u, m.id):
                if new_time < be and bs < new_time + MATCH_DURATION_MIN * 60:
                    return False, (f"<@{u}> already has a match then — "
                                   f"pick another time."), None
        m.scheduled_for = new_time
        m.checked_in = []
        m.checkin_opened_at = 0.0
        try:
            m.transition(MatchState.SCHEDULED, now)
        except TransitionError as e:
            return False, str(e), None
        self.store.put_match(m)
        return True, f"Rescheduled to {ts.discord_ts(new_time)}.", m

    # ── Check-in ─────────────────────────────────────────────────────────────

    def open_checkin(self, m: Match, now: Optional[float] = None) -> bool:
        now = now or time.time()
        try:
            m.transition(MatchState.CHECKIN, now)
        except TransitionError:
            return False
        m.checkin_opened_at = now
        m.checked_in = []
        self.store.put_match(m)
        return True

    def checkin(self, mid: str, user_id: int, now: Optional[float] = None
                ) -> tuple[bool, str, Optional[Match]]:
        now = now or time.time()
        m = self.store.get_match(mid)
        if not m:
            return False, "No match with that id.", None
        if m.state != MatchState.CHECKIN.value:
            return False, "Check-in isn't open for that match.", None
        if user_id not in m.players:
            return False, "That isn't your match.", None
        if user_id in m.checked_in:
            return True, "Already checked in.", m
        m.checked_in.append(user_id)
        both = len(m.checked_in) == 2
        if both:
            m.transition(MatchState.ACTIVE, now)
        self.store.put_match(m)
        return True, ("Both players in — the match is live!" if both
                      else "Checked in. Waiting for your opponent."), m

    def resolve_checkin_timeout(self, m: Match, now: Optional[float] = None
                                ) -> tuple[str, Optional[Match]]:
        """Called when the check-in window expires. Returns what happened."""
        now = now or time.time()
        missing = [u for u in m.players if u not in m.checked_in]
        if not missing:
            return "ok", m
        if len(missing) == 1:
            winner = m.checked_in[0]
            loser = missing[0]
            self._record_no_show(loser)
            m.winner, m.loser, m.score = winner, loser, "no-show"
            m.transition(MatchState.FORFEIT, now)
            self.store.put_match(m)
            self._apply_result(m)
            return "forfeit", m
        # Both missed. First time, reschedule; second time, both out.
        for u in missing:
            self._record_no_show(u)
        if m.history.get("rescheduled_after_noshow"):
            m.score = "double no-show"
            m.transition(MatchState.FORFEIT, now)
            self.store.put_match(m)
            self._apply_result(m)
            return "double_forfeit", m
        m.history["rescheduled_after_noshow"] = now
        m.checked_in = []
        m.checkin_opened_at = 0.0
        m.scheduled_for = now + 24 * 3600
        m.transition(MatchState.SCHEDULED, now)
        self.store.put_match(m)
        return "rescheduled", m

    def _record_no_show(self, user_id: int) -> None:
        p = self.store.get_player(user_id) or Player(user_id=user_id)
        p.no_shows += 1
        self.store.add_penalty(user_id, "no_show")
        if p.no_shows >= NO_SHOW_BAN_LIMIT:
            p.banned_until = time.time() + NO_SHOW_BAN_DAYS * 86400
            p.ban_reason = f"{p.no_shows} no-shows"
            self.store.add_penalty(user_id, "auto_ban", p.ban_reason)
        self.store.put_player(p)

    # ── Results ──────────────────────────────────────────────────────────────

    def report(self, mid: str, reporter: int, winner_id: int,
               score: str = "", now: Optional[float] = None
               ) -> tuple[bool, str, Optional[Match]]:
        now = now or time.time()
        m = self.store.get_match(mid)
        if not m:
            return False, "No match with that id.", None
        if m.state != MatchState.ACTIVE.value:
            return False, "That match isn't live.", None
        if reporter not in m.players:
            return False, "That isn't your match.", None
        if winner_id not in m.players:
            return False, "The winner must be one of the two players.", None
        m.winner = winner_id
        m.loser = m.opponent_of(winner_id)
        m.score = score[:40]
        m.transition(MatchState.COMPLETED, now)
        self.store.put_match(m)
        self._apply_result(m)
        return True, "Result recorded.", m

    def force_win(self, mid: str, winner_id: int, now: Optional[float] = None
                  ) -> tuple[bool, str, Optional[Match]]:
        now = now or time.time()
        m = self.store.get_match(mid)
        if not m:
            return False, "No match with that id.", None
        if m.state in (MatchState.COMPLETED.value, MatchState.FORFEIT.value):
            return False, "That match is already finished.", None
        if winner_id not in m.players:
            return False, "The winner must be one of the two players.", None
        m.winner = winner_id
        m.loser = m.opponent_of(winner_id)
        m.score = "admin"
        # Walk the machine forward legally rather than assigning state directly,
        # so an admin override still leaves a coherent history.
        for step in (MatchState.SCHEDULED, MatchState.CHECKIN, MatchState.ACTIVE,
                     MatchState.COMPLETED):
            if MatchState(m.state) == step:
                continue
            try:
                m.transition(step, now)
            except TransitionError:
                continue
        if m.state != MatchState.COMPLETED.value:
            m.transition(MatchState.COMPLETED, now)
        self.store.put_match(m)
        self._apply_result(m)
        return True, f"<@{winner_id}> advanced.", m

    def _apply_result(self, m: Match) -> list[Match]:
        """Update ELO, advance the bracket, schedule whatever opened up."""
        t = self.store.get_tournament(m.tournament_id)
        if not t:
            return []
        if t.use_elo and m.winner is not None and m.loser is not None:
            players = self.store.get_players([m.winner, m.loser])
            pw = players.get(m.winner) or Player(user_id=m.winner)
            pl = players.get(m.loser) or Player(user_id=m.loser)
            pw.elo, pl.elo = elo_update(pw.elo, pl.elo)
            self.store.put_player(pw)
            self.store.put_player(pl)

        matches = self.store.get_matches(t.id)
        live = next((x for x in matches if x.id == m.id), None)
        if live is not None:
            live.__dict__.update(m.__dict__)
        touched = brackets.advance(matches, live or m, t.mode)
        touched += brackets.auto_byes(matches, t.mode, time.time())
        self.store.put_matches(touched)

        if brackets.is_complete(matches, t.mode):
            t.state = TournamentState.COMPLETED.value
            self.store.put_tournament(t)
        else:
            self.schedule_ready(t.id)
        return touched

    def dispute(self, mid: str, user_id: int, reason: str) -> tuple[bool, str]:
        m = self.store.get_match(mid)
        if not m:
            return False, "No match with that id."
        if user_id not in m.players:
            return False, "That isn't your match."
        m.dispute = reason[:400]
        self.store.put_match(m)
        self.store.add_penalty(user_id, "dispute", reason[:200])
        return True, "Dispute filed — an admin will review it."

    # ── Admin ────────────────────────────────────────────────────────────────

    def replace_player(self, tid: str, old: int, new: int) -> tuple[bool, str]:
        t = self.store.get_tournament(tid)
        if not t:
            return False, "No tournament with that id."
        if old not in t.entrants:
            return False, "That player isn't in this tournament."
        if new in t.entrants:
            return False, "The replacement is already in this tournament."
        t.entrants = [new if u == old else u for u in t.entrants]
        self.store.put_tournament(t)
        swapped = []
        for m in self.store.get_matches(tid):
            if m.state in (MatchState.COMPLETED.value, MatchState.FORFEIT.value):
                continue
            if m.player_a == old:
                m.player_a = new
                swapped.append(m)
            elif m.player_b == old:
                m.player_b = new
                swapped.append(m)
        self.store.put_matches(swapped)
        return True, f"<@{old}> replaced by <@{new}> in {len(swapped)} match(es)."

    def ban_player(self, user_id: int, reason: str, days: int = 30
                   ) -> tuple[bool, str]:
        p = self.store.get_player(user_id) or Player(user_id=user_id)
        p.banned_until = time.time() + days * 86400
        p.ban_reason = reason[:200] or "no reason given"
        self.store.put_player(p)
        self.store.add_penalty(user_id, "ban", p.ban_reason)
        return True, f"<@{user_id}> banned for {days} days."

    def unban_player(self, user_id: int) -> tuple[bool, str]:
        p = self.store.get_player(user_id)
        if not p:
            return False, "No such player."
        p.banned_until = 0.0
        p.ban_reason = ""
        p.no_shows = 0
        self.store.put_player(p)
        return True, f"<@{user_id}> unbanned."

    # ── Scheduler tick ───────────────────────────────────────────────────────

    def tick(self, now: Optional[float] = None) -> dict[str, list]:
        """One pass of the background loop.

        Deliberately idempotent and driven entirely by stored state, so a crash
        or a missed run costs nothing — the next tick picks up exactly where the
        data says things are.
        """
        now = now or time.time()
        out: dict[str, list] = {"checkin_open": [], "forfeit": [],
                                "rescheduled": [], "live": []}
        lead = CHECKIN_LEAD_MIN * 60
        for m in self.store.due_matches(now + lead, [MatchState.SCHEDULED.value]):
            if m.scheduled_for - now <= lead and self.open_checkin(m, now):
                out["checkin_open"].append(m)

        window = CHECKIN_WINDOW_MIN * 60
        for m in self.store.due_matches(now, [MatchState.CHECKIN.value]):
            if m.checkin_opened_at and now - m.checkin_opened_at >= window:
                what, updated = self.resolve_checkin_timeout(m, now)
                if what == "forfeit" or what == "double_forfeit":
                    out["forfeit"].append(updated)
                elif what == "rescheduled":
                    out["rescheduled"].append(updated)
                elif what == "ok":
                    out["live"].append(updated)
        return out
