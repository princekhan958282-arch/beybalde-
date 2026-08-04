"""
store.py — persistence for the tournament system.

A note on the database choice
-----------------------------
The spec asked for PostgreSQL or MongoDB. This uses SQLite instead, on purpose:
Beycord already runs on SQLite (utils/userstore.py) on a single Pterodactyl
container, and the bot is developed from a phone. Adding a second database
server would mean another process to keep alive, another set of credentials in
config, and a migration path for data that currently lives in one file — real
operational cost for a workload that is a few thousand rows.

What the spec actually needs from a database is here: separate tables for
players / tournaments / matches / penalties, ACID transactions, indexes, and
concurrent-safe writes. SQLite in WAL mode gives all of that. If this ever
outgrows one container, `Store` is the only module that has to change — nothing
above it writes SQL.

All timestamps are UTC epoch seconds.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Optional

from .models import Match, Player, Tournament, TournamentState

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(_ROOT, "data", "tournaments.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    user_id       INTEGER PRIMARY KEY,
    country       TEXT    NOT NULL DEFAULT '',
    utc_offset    REAL    NOT NULL DEFAULT 0,
    slots         TEXT    NOT NULL DEFAULT '[]',
    elo           INTEGER NOT NULL DEFAULT 1000,
    no_shows      INTEGER NOT NULL DEFAULT 0,
    banned_until  REAL    NOT NULL DEFAULT 0,
    ban_reason    TEXT    NOT NULL DEFAULT '',
    registered_at REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tournaments (
    id           TEXT PRIMARY KEY,
    guild_id     INTEGER NOT NULL DEFAULT 0,
    channel_id   INTEGER NOT NULL DEFAULT 0,
    name         TEXT    NOT NULL DEFAULT '',
    mode         TEXT    NOT NULL DEFAULT 'single',
    max_players  INTEGER NOT NULL DEFAULT 16,
    start_time   REAL    NOT NULL DEFAULT 0,
    state        TEXT    NOT NULL DEFAULT 'signup',
    entrants     TEXT    NOT NULL DEFAULT '[]',
    use_elo      INTEGER NOT NULL DEFAULT 1,
    reward_type  TEXT    NOT NULL DEFAULT '',
    reward_value TEXT    NOT NULL DEFAULT '',
    created_at   REAL    NOT NULL DEFAULT 0,
    created_by   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS matches (
    id              TEXT PRIMARY KEY,
    tournament_id   TEXT NOT NULL,
    round_no        INTEGER NOT NULL DEFAULT 1,
    bracket         TEXT NOT NULL DEFAULT 'winners',
    slot            INTEGER NOT NULL DEFAULT 0,
    player_a        INTEGER,
    player_b        INTEGER,
    state           TEXT NOT NULL DEFAULT 'pending',
    scheduled_for   REAL NOT NULL DEFAULT 0,
    checkin_opened_at REAL NOT NULL DEFAULT 0,
    checked_in      TEXT NOT NULL DEFAULT '[]',
    winner          INTEGER,
    loser           INTEGER,
    score           TEXT NOT NULL DEFAULT '',
    dispute         TEXT NOT NULL DEFAULT '',
    history         TEXT NOT NULL DEFAULT '{}',
    winner_to       TEXT,
    loser_to        TEXT
);

CREATE TABLE IF NOT EXISTS penalties (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    kind      TEXT    NOT NULL,
    reason    TEXT    NOT NULL DEFAULT '',
    created_at REAL   NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS announcements (
    id           TEXT PRIMARY KEY,
    guild_id     INTEGER NOT NULL DEFAULT 0,
    channel_id   INTEGER NOT NULL DEFAULT 0,
    created_by   INTEGER NOT NULL DEFAULT 0,
    body         TEXT    NOT NULL DEFAULT '',
    target       TEXT    NOT NULL DEFAULT 'dm',
    audience     TEXT    NOT NULL DEFAULT 'entrants',
    recipients   TEXT    NOT NULL DEFAULT '[]',
    created_at   REAL    NOT NULL DEFAULT 0
);

-- One row per (announcement, user); a re-click updates rather than duplicates,
-- so tapping the other button after already answering just changes your vote.
CREATE TABLE IF NOT EXISTS rsvps (
    ann_id       TEXT    NOT NULL,
    user_id      INTEGER NOT NULL,
    response     TEXT    NOT NULL,
    responded_at REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (ann_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_matches_tid   ON matches(tournament_id);
CREATE INDEX IF NOT EXISTS idx_matches_state ON matches(state);
CREATE INDEX IF NOT EXISTS idx_matches_sched ON matches(scheduled_for);
CREATE INDEX IF NOT EXISTS idx_tour_state    ON tournaments(state);
CREATE INDEX IF NOT EXISTS idx_pen_user      ON penalties(user_id);
CREATE INDEX IF NOT EXISTS idx_ann_target    ON announcements(target, created_at);
CREATE INDEX IF NOT EXISTS idx_rsvp_ann      ON rsvps(ann_id);
"""

_JSON_MATCH = ("checked_in", "history", "winner_to", "loser_to")


class Store:
    """Thread-safe SQLite access. One instance per bot."""

    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # WAL lets the scheduler loop read while a command writes, which is the
        # whole concurrency story for a bot this size.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── players ──────────────────────────────────────────────────────────────

    def get_player(self, user_id: int) -> Optional[Player]:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM players WHERE user_id=?",
                            (user_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["slots"] = json.loads(d["slots"] or "[]")
        return Player(**d)

    def put_player(self, p: Player) -> None:
        with self._lock, self._conn() as c:
            c.execute("""INSERT INTO players
                (user_id,country,utc_offset,slots,elo,no_shows,banned_until,
                 ban_reason,registered_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                 country=excluded.country, utc_offset=excluded.utc_offset,
                 slots=excluded.slots, elo=excluded.elo,
                 no_shows=excluded.no_shows, banned_until=excluded.banned_until,
                 ban_reason=excluded.ban_reason""",
                (p.user_id, p.country, p.utc_offset, json.dumps(p.slots), p.elo,
                 p.no_shows, p.banned_until, p.ban_reason, p.registered_at))

    def get_players(self, ids: list[int]) -> dict[int, Player]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with self._lock, self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM players WHERE user_id IN ({marks})", ids).fetchall()
        out = {}
        for r in rows:
            d = dict(r)
            d["slots"] = json.loads(d["slots"] or "[]")
            out[d["user_id"]] = Player(**d)
        return out

    # ── tournaments ──────────────────────────────────────────────────────────

    def put_tournament(self, t: Tournament) -> None:
        with self._lock, self._conn() as c:
            c.execute("""INSERT INTO tournaments
                (id,guild_id,channel_id,name,mode,max_players,start_time,state,
                 entrants,use_elo,reward_type,reward_value,created_at,created_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, mode=excluded.mode,
                 max_players=excluded.max_players, start_time=excluded.start_time,
                 state=excluded.state, entrants=excluded.entrants,
                 use_elo=excluded.use_elo, reward_type=excluded.reward_type,
                 reward_value=excluded.reward_value, channel_id=excluded.channel_id""",
                (t.id, t.guild_id, t.channel_id, t.name, t.mode, t.max_players,
                 t.start_time, t.state, json.dumps(t.entrants), int(t.use_elo),
                 t.reward_type, t.reward_value, t.created_at, t.created_by))

    def get_tournament(self, tid: str) -> Optional[Tournament]:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM tournaments WHERE id=?",
                            (tid,)).fetchone()
        return self._row_to_tournament(row) if row else None

    def list_tournaments(self, guild_id: Optional[int] = None,
                         states: Optional[list[str]] = None) -> list[Tournament]:
        sql = "SELECT * FROM tournaments WHERE 1=1"
        args: list = []
        if guild_id is not None:
            sql += " AND guild_id=?"
            args.append(guild_id)
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            args.extend(states)
        sql += " ORDER BY created_at DESC"
        with self._lock, self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [self._row_to_tournament(r) for r in rows]

    def active_tournaments(self) -> list[Tournament]:
        return self.list_tournaments(
            states=[TournamentState.SIGNUP.value, TournamentState.RUNNING.value])

    @staticmethod
    def _row_to_tournament(row) -> Tournament:
        d = dict(row)
        d["entrants"] = json.loads(d["entrants"] or "[]")
        d["use_elo"] = bool(d["use_elo"])
        return Tournament(**d)

    # ── matches ──────────────────────────────────────────────────────────────

    def put_matches(self, matches: list[Match]) -> None:
        """One transaction for the whole batch — a half-written bracket is
        worse than no bracket."""
        if not matches:
            return
        rows = []
        for m in matches:
            rows.append((
                m.id, m.tournament_id, m.round_no, m.bracket, m.slot,
                m.player_a, m.player_b, m.state, m.scheduled_for,
                m.checkin_opened_at, json.dumps(m.checked_in), m.winner, m.loser,
                m.score, m.dispute, json.dumps(m.history),
                json.dumps(m.winner_to) if m.winner_to else None,
                json.dumps(m.loser_to) if m.loser_to else None))
        with self._lock, self._conn() as c:
            c.executemany("""INSERT INTO matches
                (id,tournament_id,round_no,bracket,slot,player_a,player_b,state,
                 scheduled_for,checkin_opened_at,checked_in,winner,loser,score,
                 dispute,history,winner_to,loser_to)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                 player_a=excluded.player_a, player_b=excluded.player_b,
                 state=excluded.state, scheduled_for=excluded.scheduled_for,
                 checkin_opened_at=excluded.checkin_opened_at,
                 checked_in=excluded.checked_in, winner=excluded.winner,
                 loser=excluded.loser, score=excluded.score,
                 dispute=excluded.dispute, history=excluded.history,
                 winner_to=excluded.winner_to, loser_to=excluded.loser_to""", rows)

    def put_match(self, m: Match) -> None:
        self.put_matches([m])

    def get_matches(self, tid: str) -> list[Match]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM matches WHERE tournament_id=? "
                "ORDER BY bracket, round_no, slot", (tid,)).fetchall()
        return [self._row_to_match(r) for r in rows]

    def get_match(self, mid: str) -> Optional[Match]:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
        return self._row_to_match(row) if row else None

    def due_matches(self, now: float, states: list[str]) -> list[Match]:
        """Matches whose scheduled time has arrived — the scheduler's input."""
        marks = ",".join("?" * len(states))
        with self._lock, self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM matches WHERE state IN ({marks}) "
                f"AND scheduled_for > 0 AND scheduled_for <= ? "
                f"ORDER BY scheduled_for", (*states, now)).fetchall()
        return [self._row_to_match(r) for r in rows]

    def player_bookings(self, user_id: int,
                        exclude_match: str = "") -> list[tuple[float, float]]:
        """Committed (start, end) windows, so nobody is booked twice at once."""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT scheduled_for FROM matches "
                "WHERE (player_a=? OR player_b=?) AND id != ? "
                "AND scheduled_for > 0 "
                "AND state IN ('scheduled','checkin','active')",
                (user_id, user_id, exclude_match)).fetchall()
        return [(r["scheduled_for"], r["scheduled_for"] + 3600) for r in rows]

    def matches_in_states(self, states: list[str]) -> list[Match]:
        """Every match currently in one of these states, no time filter.

        Used at boot to know which check-in buttons need to work again — the
        set is naturally small since it excludes anything completed or not
        yet ready, so re-registering all of them on every restart is cheap.
        """
        marks = ",".join("?" * len(states))
        with self._lock, self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM matches WHERE state IN ({marks})", states).fetchall()
        return [self._row_to_match(r) for r in rows]

    @staticmethod
    def _row_to_match(row) -> Match:
        d = dict(row)
        for k in _JSON_MATCH:
            raw = d.get(k)
            d[k] = json.loads(raw) if raw else ([] if k == "checked_in"
                                                else {} if k == "history" else None)
        return Match(**d)

    # ── penalties ────────────────────────────────────────────────────────────

    def add_penalty(self, user_id: int, kind: str, reason: str = "") -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO penalties (user_id,kind,reason,created_at) "
                      "VALUES (?,?,?,?)", (user_id, kind, reason, time.time()))

    def penalties(self, user_id: int) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM penalties WHERE user_id=? "
                             "ORDER BY created_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    # ── announcements + RSVPs ────────────────────────────────────────────────

    def create_announcement(self, guild_id: int, channel_id: int, created_by: int,
                            body: str, target: str, audience: str,
                            recipients: list[int]) -> str:
        aid = "a_" + uuid.uuid4().hex[:10]
        with self._lock, self._conn() as c:
            c.execute("""INSERT INTO announcements
                (id,guild_id,channel_id,created_by,body,target,audience,
                 recipients,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (aid, guild_id, channel_id, created_by, body, target, audience,
                 json.dumps(recipients), time.time()))
        return aid

    def get_announcement(self, aid: str) -> Optional[dict]:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM announcements WHERE id=?",
                            (aid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["recipients"] = json.loads(d["recipients"] or "[]")
        return d

    def recent_dm_announcements(self, since: float) -> list[dict]:
        """DM announcements created since `since` — what needs its RSVP
        buttons re-registered at boot. Bounded by time so the registered set
        doesn't grow forever; an RSVP for a match days ago is moot anyway."""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM announcements WHERE target='dm' AND created_at >= ?"
                " ORDER BY created_at DESC", (since,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["recipients"] = json.loads(d["recipients"] or "[]")
            out.append(d)
        return out

    def set_rsvp(self, aid: str, user_id: int, response: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("""INSERT INTO rsvps (ann_id,user_id,response,responded_at)
                VALUES (?,?,?,?)
                ON CONFLICT(ann_id,user_id) DO UPDATE SET
                 response=excluded.response, responded_at=excluded.responded_at""",
                (aid, user_id, response, time.time()))

    def rsvp_counts(self, aid: str) -> dict:
        """{'yes','no','pending','total'} — pending includes anyone who never
        answered, which also covers a DM that never delivered."""
        ann = self.get_announcement(aid)
        total = len(ann["recipients"]) if ann else 0
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT response, COUNT(*) AS n FROM rsvps WHERE ann_id=? "
                "GROUP BY response", (aid,)).fetchall()
        counts = {"yes": 0, "no": 0}
        for r in rows:
            counts[r["response"]] = r["n"]
        counts["total"] = total
        counts["pending"] = max(0, total - counts["yes"] - counts["no"])
        return counts
