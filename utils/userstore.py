"""
utils/userstore.py
------------------
SQLite backing store for the user registry.

Why this exists
---------------
`users.json` grew to ~1 MB across 3,300+ rows. Every single `update_user()`
re-parsed and rewrote that whole file under a global lock — so one player
winning a battle cost a full 1 MB parse + 1 MB write, and every other write
in the bot queued behind it. That is fine at 40 active players and falls over
well before 500.

This module keeps the *exact same semantics* database.py already exposes
(`load_users`, `save_users`, `get_one`, `put_one`) but backs them with SQLite,
so a single-user write touches one row instead of the entire registry.

Design notes
------------
* The full profile still lives as a JSON blob in the `data` column, so nothing
  else in the codebase needs a schema change — profiles stay free-form.
* Hot fields (coins, level, wins, …) are ALSO mirrored into real columns on
  every write, so leaderboards can `ORDER BY` in SQL instead of loading and
  sorting 3,300 dicts in Python.
* `created_at` / `last_seen` are new. They cost nothing and finally make it
  possible to measure how many rows are real players vs. drive-by ghosts.
* WAL mode + a per-connection thread guard: the bot is async but database.py
  is called synchronously from many threads, so each thread gets its own
  connection.

Migration is automatic and non-destructive: on first use, if the DB is empty
and users.json exists, every row is imported and the JSON file is left alone
as a backup.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Optional

log = logging.getLogger("beyblade_bot")

# Mirrored columns: (column name, profile key, SQL type, default)
HOT_FIELDS = [
    ("coins",      "coins",      "INTEGER", 0),
    ("level",      "level",      "INTEGER", 0),
    ("xp",         "xp",         "INTEGER", 0),
    ("wins",       "wins",       "INTEGER", 0),
    ("losses",     "losses",     "INTEGER", 0),
    ("rank_score", "rank_score", "INTEGER", 0),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    coins       INTEGER NOT NULL DEFAULT 0,
    level       INTEGER NOT NULL DEFAULT 0,
    xp          INTEGER NOT NULL DEFAULT 0,
    wins        INTEGER NOT NULL DEFAULT 0,
    losses      INTEGER NOT NULL DEFAULT 0,
    rank_score  INTEGER NOT NULL DEFAULT 0,
    inv_count   INTEGER NOT NULL DEFAULT 0,
    created_at  REAL,
    last_seen   REAL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_coins      ON users(coins DESC);
CREATE INDEX IF NOT EXISTS idx_users_rank       ON users(rank_score DESC, wins DESC);
CREATE INDEX IF NOT EXISTS idx_users_level      ON users(level DESC);
CREATE INDEX IF NOT EXISTS idx_users_last_seen  ON users(last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_users_created    ON users(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_users_inv        ON users(inv_count);

-- ── Notifications ────────────────────────────────────────────────────────────
-- These live in the USER STORE and not in a JSON file under data/, and that is
-- a deliberate durability decision rather than a filing preference.
-- `mysql_store.kv_get` / `kv_put` have no readers anywhere: `migrate()` copies
-- the JSON files into MySQL once and every runtime read still goes to local
-- disk. On the MySQL deployment — which exists so data survives a container
-- rebuild — only the `users` table actually survives. A delivery ledger in a
-- JSON file would be wiped on the next rebuild and the next send would DM
-- everybody a second time, which is the one thing this feature must not do.
CREATE TABLE IF NOT EXISTS updates (
    update_id    TEXT PRIMARY KEY,
    version      TEXT,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    priority     TEXT NOT NULL,
    audience     TEXT NOT NULL,          -- JSON {kind, params}
    image_url    TEXT,
    event        TEXT,
    created_by   TEXT,
    created_at   REAL NOT NULL,
    scheduled_at REAL,
    state        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_updates_created ON updates(created_at DESC);

-- The composite PRIMARY KEY is the duplicate prevention, and it is the
-- DATABASE enforcing it rather than application logic. Enqueue is
-- INSERT OR IGNORE, so re-queueing after a restart, a retry, or an admin
-- pressing Send twice adds nothing and reports how many rows were new.
CREATE TABLE IF NOT EXISTS update_deliveries (
    update_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    state      TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    queued_at  REAL NOT NULL,
    sent_at    REAL,
    PRIMARY KEY (update_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_update_state
    ON update_deliveries(update_id, state);
CREATE INDEX IF NOT EXISTS idx_deliveries_state ON update_deliveries(state);

CREATE TABLE IF NOT EXISTS reports (
    report_id  TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    guild_id   TEXT,
    summary    TEXT NOT NULL,
    body       TEXT NOT NULL,
    image_url  TEXT,
    status     TEXT NOT NULL,
    created_at REAL NOT NULL,
    handled_by TEXT,
    handled_at REAL,
    message_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_user_time
    ON reports(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_status
    ON reports(status, created_at DESC);

-- ── Community systems (main server only) ─────────────────────────────────────
-- Config lives in a TABLE and not in data/config.json for the durability reason
-- spelled out above: config.json is a local file, so on the MySQL deployment it
-- does not survive a container rebuild. The main-server id is the switch every
-- community feature is gated on — if it evaporates the whole layer silently
-- turns itself off, which looks exactly like a bug.
CREATE TABLE IF NOT EXISTS community_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,          -- JSON-encoded
    updated_at REAL NOT NULL
);

-- Polls and giveaways carry every column they will ever need, because there is
-- no ALTER TABLE anywhere in this codebase and no migration path for adding one
-- to a table that already exists on the live install.
CREATE TABLE IF NOT EXISTS community_polls (
    poll_id    TEXT PRIMARY KEY,
    guild_id   TEXT NOT NULL,
    channel_id TEXT,
    message_id TEXT,
    author_id  TEXT NOT NULL,
    question   TEXT NOT NULL,
    options    TEXT NOT NULL,          -- JSON list[str]
    multi      INTEGER NOT NULL DEFAULT 0,
    anonymous  INTEGER NOT NULL DEFAULT 1,
    ends_at    REAL,
    state      TEXT NOT NULL,
    created_at REAL NOT NULL,
    closed_at  REAL,
    results    TEXT                    -- JSON {choice: count} at close
);
CREATE INDEX IF NOT EXISTS idx_polls_due ON community_polls(state, ends_at);

-- One row per voter. The composite PRIMARY KEY is the duplicate-vote
-- prevention and it is the DATABASE enforcing it, not a set in memory that a
-- restart would empty.
CREATE TABLE IF NOT EXISTS community_poll_votes (
    poll_id  TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    choice   TEXT NOT NULL,            -- JSON list[int] (multi) or "N"
    voted_at REAL NOT NULL,
    PRIMARY KEY (poll_id, user_id)
);

CREATE TABLE IF NOT EXISTS community_giveaways (
    giveaway_id TEXT PRIMARY KEY,
    guild_id    TEXT NOT NULL,
    channel_id  TEXT,
    message_id  TEXT,
    host_id     TEXT NOT NULL,
    prize       TEXT NOT NULL,
    winners     INTEGER NOT NULL DEFAULT 1,
    requirement TEXT,                  -- JSON, reserved: level/role gates
    ends_at     REAL,
    state       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    ended_at    REAL,
    winner_ids  TEXT                   -- JSON list[str], every winner ever drawn
);
CREATE INDEX IF NOT EXISTS idx_giveaways_due
    ON community_giveaways(state, ends_at);

CREATE TABLE IF NOT EXISTS community_giveaway_entries (
    giveaway_id TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    entered_at  REAL NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);
"""


class UserStore:
    def __init__(self, db_path: str, json_path: Optional[str] = None):
        self.db_path   = db_path
        self.json_path = json_path
        self._local    = threading.local()
        self._init_lock = threading.Lock()
        self._ready    = False

    # ── Connection handling ──────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=15.0,
                                   isolation_level=None)   # autocommit
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=15000")
            # Checkpoint often so users.db is self-contained. Without this the
            # recent writes live in users.db-wal, and anyone backing up or
            # moving just users.db from the panel would silently lose them.
            conn.execute("PRAGMA wal_autocheckpoint=200")
            self._local.conn = conn
        return conn

    def checkpoint(self) -> None:
        """Fold the WAL back into the main DB file (safe to call any time)."""
        try:
            self._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:
            log.debug(f"[userstore] checkpoint skipped: {exc}")

    def ensure_ready(self) -> None:
        if self._ready:
            return
        with self._init_lock:
            if self._ready:
                return
            conn = self._conn()
            conn.executescript(_SCHEMA)
            self._maybe_import_json(conn)
            self._ready = True

    # ── One-time import from users.json ──────────────────────────────────────
    def _maybe_import_json(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        if row["n"] > 0:
            return
        if not self.json_path or not os.path.exists(self.json_path):
            return

        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                text = f.read()
            data = json.loads(text) if text.strip() else {}
        except Exception as exc:
            log.error(f"[userstore] couldn't read {self.json_path}: {exc}")
            return

        if not isinstance(data, dict) or not data:
            return

        now = time.time()
        rows = [self._row_tuple(str(uid), prof, created=now, seen=None)
                for uid, prof in data.items() if isinstance(prof, dict)]
        conn.execute("BEGIN")
        conn.executemany(self._UPSERT, rows)
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        log.info(f"[userstore] migrated {len(rows):,} profiles from users.json "
                 f"into {os.path.basename(self.db_path)} "
                 f"(JSON left in place as a backup)")

    # ── Row helpers ──────────────────────────────────────────────────────────
    _UPSERT = """
        INSERT INTO users (user_id, coins, level, xp, wins, losses, rank_score,
                           inv_count, created_at, last_seen, data)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            coins=excluded.coins, level=excluded.level, xp=excluded.xp,
            wins=excluded.wins, losses=excluded.losses,
            rank_score=excluded.rank_score, inv_count=excluded.inv_count,
            last_seen=COALESCE(excluded.last_seen, users.last_seen),
            created_at=COALESCE(users.created_at, excluded.created_at),
            data=excluded.data
    """

    @staticmethod
    def _int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _row_tuple(self, uid: str, prof: dict,
                   created: Optional[float] = None,
                   seen: Optional[float] = None) -> tuple:
        inv = prof.get("inventory")
        return (
            uid,
            self._int(prof.get("coins")),
            self._int(prof.get("level")),
            self._int(prof.get("xp")),
            self._int(prof.get("wins")),
            self._int(prof.get("losses")),
            self._int(prof.get("rank_score")),
            len(inv) if isinstance(inv, list) else 0,
            created if created is not None else time.time(),
            seen,
            json.dumps(prof, separators=(",", ":")),
        )

    # ── Public API (mirrors database.py) ─────────────────────────────────────
    def get_one(self, uid: str) -> Optional[dict]:
        self.ensure_ready()
        row = self._conn().execute(
            "SELECT data FROM users WHERE user_id = ?", (uid,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["data"])
        except (json.JSONDecodeError, ValueError):
            return None

    def put_one(self, uid: str, profile: dict, touch: bool = True) -> None:
        self.ensure_ready()
        self._conn().execute(
            self._UPSERT,
            self._row_tuple(uid, profile, seen=time.time() if touch else None))

    def has(self, uid: str) -> bool:
        self.ensure_ready()
        return self._conn().execute(
            "SELECT 1 FROM users WHERE user_id = ?", (uid,)).fetchone() is not None

    def load_all(self) -> dict:
        """Whole registry as {uid: profile}. Used by leaderboards / tournaments."""
        self.ensure_ready()
        out = {}
        for row in self._conn().execute("SELECT user_id, data FROM users"):
            try:
                out[row["user_id"]] = json.loads(row["data"])
            except (json.JSONDecodeError, ValueError):
                continue
        return out

    def all_user_ids(self) -> list[int]:
        """Every registered user id, without deserialising a single profile.

        load_all() parses every JSON blob, which is real work for a few
        thousand rows and pure waste when the caller only wants "who exists" —
        announcements, broadcasts, migration counts. Ids that aren't numeric
        are skipped rather than raising; a malformed row shouldn't take down a
        send to everyone else.
        """
        self.ensure_ready()
        out: list[int] = []
        for row in self._conn().execute("SELECT user_id FROM users"):
            try:
                out.append(int(row["user_id"]))
            except (TypeError, ValueError):
                continue
        return out

    def save_all(self, data: dict) -> None:
        """Bulk upsert. Does NOT delete rows missing from `data`."""
        self.ensure_ready()
        rows = [self._row_tuple(str(uid), prof)
                for uid, prof in data.items() if isinstance(prof, dict)]
        if not rows:
            return
        conn = self._conn()
        conn.execute("BEGIN")
        conn.executemany(self._UPSERT, rows)
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def count(self) -> int:
        self.ensure_ready()
        return self._conn().execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    # ── Query helpers (these are the whole point of the hot columns) ─────────
    def top_by(self, column: str, limit: int = 10) -> list[dict]:
        """Leaderboard without loading the registry into Python."""
        self.ensure_ready()
        allowed = {c[0] for c in HOT_FIELDS} | {"inv_count"}
        if column not in allowed:
            raise ValueError(f"not a sortable column: {column}")
        rows = self._conn().execute(
            f"SELECT data FROM users ORDER BY {column} DESC LIMIT ?", (limit,))
        out = []
        for row in rows:
            try:
                out.append(json.loads(row["data"]))
            except (json.JSONDecodeError, ValueError):
                continue
        return out

    def stats(self) -> dict:
        """Economy / population snapshot for the audit command."""
        self.ensure_ready()
        c = self._conn()
        row = c.execute("""
            SELECT COUNT(*)                                   AS total,
                   COALESCE(SUM(coins), 0)                    AS coin_supply,
                   COALESCE(MAX(coins), 0)                    AS richest,
                   SUM(CASE WHEN coins = 0 AND inv_count = 0
                             AND xp = 0 THEN 1 ELSE 0 END)    AS ghosts,
                   SUM(CASE WHEN wins + losses > 0
                            THEN 1 ELSE 0 END)                AS battlers,
                   SUM(CASE WHEN inv_count > 0 THEN 1 ELSE 0 END) AS collectors
            FROM users
        """).fetchone()
        return dict(row)

    def coin_percentiles(self) -> dict:
        self.ensure_ready()
        c = self._conn()
        total = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if not total:
            return {}
        out = {}
        for label, frac in (("p50", 0.50), ("p90", 0.90), ("p99", 0.99)):
            off = min(total - 1, int(total * frac))
            row = c.execute(
                "SELECT coins FROM users ORDER BY coins ASC LIMIT 1 OFFSET ?",
                (off,)).fetchone()
            out[label] = row["coins"] if row else 0
        return out

    def top_wallets(self, limit: int = 10) -> list[dict]:
        self.ensure_ready()
        rows = self._conn().execute("""
            SELECT user_id, coins, level, wins, losses, inv_count,
                   created_at, last_seen
            FROM users ORDER BY coins DESC LIMIT ?
        """, (limit,))
        return [dict(r) for r in rows]

    def active_since(self, cutoff: float) -> int:
        self.ensure_ready()
        return self._conn().execute(
            "SELECT COUNT(*) AS n FROM users WHERE last_seen >= ?",
            (cutoff,)).fetchone()["n"]

    def active_users_since(self, cutoff: float, limit: int = 25) -> list[dict]:
        """WHO was active since `cutoff`, most recent first.

        `active_since` answers how many, which is the whole answer for a
        health check and half of it for "who played today". Same index
        (`idx_users_last_seen`), so this is the same query with the rows kept.
        """
        self.ensure_ready()
        rows = self._conn().execute("""
            SELECT user_id, coins, level, wins, losses, inv_count, last_seen
            FROM users WHERE last_seen >= ?
            ORDER BY last_seen DESC LIMIT ?
        """, (cutoff, max(1, int(limit))))
        return [dict(r) for r in rows]

    def export_json(self, path: str) -> int:
        """Dump the whole store back to a JSON file (for backups / rollback)."""
        data = self.load_all()
        tmp  = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return len(data)

    # ── Notifications: updates, the delivery ledger, and reports ─────────────
    #
    # Every method below has a same-named twin on `MySQLStore` with the same
    # signature, so no caller ever branches on which backend is live. The only
    # differences are the placeholder style and the ignore-duplicate spelling.

    _UPDATE_COLS = ("update_id", "version", "title", "body", "priority",
                    "audience", "image_url", "event", "created_by",
                    "created_at", "scheduled_at", "state")

    def updates_put(self, row: dict) -> None:
        """Insert or replace one update. `audience` is stored as JSON text."""
        self.ensure_ready()
        data = dict(row)
        if isinstance(data.get("audience"), (dict, list)):
            data["audience"] = json.dumps(data["audience"])
        vals = tuple(data.get(c) for c in self._UPDATE_COLS)
        marks = ",".join("?" for _ in self._UPDATE_COLS)
        self._conn().execute(
            f"INSERT OR REPLACE INTO updates "
            f"({','.join(self._UPDATE_COLS)}) VALUES ({marks})", vals)

    @staticmethod
    def _update_row(row) -> dict:
        out = dict(row)
        try:
            out["audience"] = json.loads(out.get("audience") or "{}")
        except (json.JSONDecodeError, ValueError, TypeError):
            out["audience"] = {}
        return out

    def updates_get(self, update_id: str) -> Optional[dict]:
        self.ensure_ready()
        row = self._conn().execute(
            "SELECT * FROM updates WHERE update_id = ?",
            (str(update_id),)).fetchone()
        return self._update_row(row) if row else None

    def updates_list(self, limit: int = 20) -> list[dict]:
        self.ensure_ready()
        rows = self._conn().execute(
            "SELECT * FROM updates ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),))
        return [self._update_row(r) for r in rows]

    def updates_set_state(self, update_id: str, state: str) -> None:
        self.ensure_ready()
        self._conn().execute(
            "UPDATE updates SET state = ? WHERE update_id = ?",
            (str(state), str(update_id)))

    def deliveries_enqueue(self, update_id: str, user_ids, *,
                           now: Optional[float] = None) -> int:
        """Queue an update for these users. Returns how many rows were NEW.

        `INSERT OR IGNORE` against the composite primary key is the whole of
        the duplicate prevention: queueing the same (update, user) twice is a
        no-op, so a retry, a restart or a double-click cannot produce a second
        DM. The count comes from `total_changes` rather than `rowcount`, which
        reports -1 for an executemany on some builds.
        """
        self.ensure_ready()
        ts = time.time() if now is None else float(now)
        rows = [(str(update_id), str(u), "PENDING", ts)
                for u in dict.fromkeys(user_ids)]   # de-duped, order kept
        if not rows:
            return 0
        conn = self._conn()
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO update_deliveries "
            "(update_id, user_id, state, queued_at) VALUES (?, ?, ?, ?)", rows)
        return conn.total_changes - before

    def deliveries_claim(self, update_id: str, limit: int = 25) -> list[str]:
        """Take a batch off the queue, atomically.

        One statement flips PENDING/RETRY to SENDING and hands back the ids it
        took, so two workers cannot claim the same row even if one ever exists.
        Written this way rather than select-then-update because that pair has a
        window between the two halves, and the window is exactly the bug.
        """
        self.ensure_ready()
        rows = self._conn().execute("""
            UPDATE update_deliveries SET state = 'SENDING'
             WHERE rowid IN (
                   SELECT rowid FROM update_deliveries
                    WHERE update_id = ? AND state IN ('PENDING', 'RETRY')
                    ORDER BY queued_at LIMIT ?)
         RETURNING user_id
        """, (str(update_id), max(1, int(limit)))).fetchall()
        return [r["user_id"] for r in rows]

    def deliveries_mark(self, update_id: str, user_id: str, state: str,
                        error: Optional[str] = None,
                        bump_attempt: bool = True) -> None:
        self.ensure_ready()
        self._conn().execute("""
            UPDATE update_deliveries
               SET state = ?,
                   attempts = attempts + ?,
                   last_error = ?,
                   sent_at = CASE WHEN ? = 'SENT' THEN ? ELSE sent_at END
             WHERE update_id = ? AND user_id = ?
        """, (str(state), 1 if bump_attempt else 0,
              (error or None), str(state), time.time(),
              str(update_id), str(user_id)))

    def deliveries_counts(self, update_id: str) -> dict:
        self.ensure_ready()
        rows = self._conn().execute(
            "SELECT state, COUNT(*) AS n FROM update_deliveries "
            "WHERE update_id = ? GROUP BY state", (str(update_id),))
        return {r["state"]: r["n"] for r in rows}

    def deliveries_attempts(self, update_id: str, user_id: str) -> int:
        self.ensure_ready()
        row = self._conn().execute(
            "SELECT attempts FROM update_deliveries "
            "WHERE update_id = ? AND user_id = ?",
            (str(update_id), str(user_id))).fetchone()
        return int(row["attempts"]) if row else 0

    def deliveries_reset_stuck(self) -> int:
        """SENDING -> PENDING, for every update. Run once at startup.

        A process killed mid-batch leaves rows claimed and nobody holding them.
        Without this they are stranded forever, which for the player is an
        update that never arrives and for the ledger is a send that never
        finishes.
        """
        self.ensure_ready()
        conn = self._conn()
        before = conn.total_changes
        conn.execute("UPDATE update_deliveries SET state = 'PENDING' "
                     "WHERE state = 'SENDING'")
        return conn.total_changes - before

    def deliveries_drop_pending(self, update_id: str) -> int:
        """Cancel: forget what has not gone out. Sent rows are left alone."""
        self.ensure_ready()
        conn = self._conn()
        before = conn.total_changes
        conn.execute(
            "DELETE FROM update_deliveries WHERE update_id = ? "
            "AND state IN ('PENDING', 'RETRY', 'SENDING')", (str(update_id),))
        return conn.total_changes - before

    def deliveries_pending_updates(self) -> list[str]:
        """Update ids with work left, so a restarted worker knows where to go."""
        self.ensure_ready()
        rows = self._conn().execute(
            "SELECT DISTINCT update_id FROM update_deliveries "
            "WHERE state IN ('PENDING', 'RETRY')")
        return [r["update_id"] for r in rows]

    def users_never_received(self, update_id: str,
                             limit: Optional[int] = None) -> list[int]:
        """Everyone in the registry with no delivery row for this update.

        The "players who have not received a specific update" audience, as one
        indexed LEFT JOIN rather than loading the registry and subtracting.
        """
        self.ensure_ready()
        sql = ("SELECT u.user_id AS uid FROM users u "
               "LEFT JOIN update_deliveries d "
               "  ON d.user_id = u.user_id AND d.update_id = ? "
               "WHERE d.user_id IS NULL")
        args: tuple = (str(update_id),)
        if limit:
            sql += " LIMIT ?"
            args += (max(1, int(limit)),)
        out = []
        for r in self._conn().execute(sql, args):
            try:
                out.append(int(r["uid"]))
            except (TypeError, ValueError):
                continue
        return out

    def created_since(self, cutoff: float, limit: Optional[int] = None
                      ) -> list[int]:
        """Accounts registered since `cutoff` — the "new players" audience."""
        self.ensure_ready()
        sql = ("SELECT user_id FROM users WHERE created_at >= ? "
               "ORDER BY created_at DESC")
        args: tuple = (float(cutoff),)
        if limit:
            sql += " LIMIT ?"
            args += (max(1, int(limit)),)
        out = []
        for r in self._conn().execute(sql, args):
            try:
                out.append(int(r["user_id"]))
            except (TypeError, ValueError):
                continue
        return out

    # ── Audience filters ─────────────────────────────────────────────────────
    #
    # "Owns nothing" is the filter, and it is deliberately EXACT rather than
    # just `inv_count = 0`.
    #
    # `inv_count` is `len(profile["inventory"])`, mirrored into a real column
    # on every write, so it is an index scan. But a boss COPY lives in
    # `profile["boss_copies"]`, not in `inventory` — so a player whose only
    # blade is a copy has `inv_count = 0` while being someone who has very
    # obviously played. That is the same disagreement between "what the column
    # says" and "what the player can actually fight with" that had Story and
    # PvP refusing copy holders in v1.26, and repeating it here would be
    # repeating a bug I just removed.
    #
    # So: the index does the bulk of the work, and only the profiles that come
    # back EMPTY — the minority, and precisely the ones about to be dropped —
    # are deserialised to check for copies.

    def _copy_holders(self, uids) -> set:
        """Of these ids, which hold a boss copy. Reads JSON; keep the set small."""
        wanted = [str(u) for u in uids]
        if not wanted:
            return set()
        out: set = set()
        for i in range(0, len(wanted), 500):
            chunk = wanted[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            for r in self._conn().execute(
                    f"SELECT user_id, data FROM users "
                    f"WHERE user_id IN ({marks})", chunk):
                try:
                    prof = json.loads(r["data"])
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
                if prof.get("boss_copies"):
                    out.add(r["user_id"])
        return out

    def _empty_ids(self, limit: Optional[int] = None) -> list[str]:
        sql = "SELECT user_id FROM users WHERE inv_count < 1"
        args: tuple = ()
        if limit:
            sql += " LIMIT ?"
            args = (max(1, int(limit)),)
        return [r["user_id"] for r in self._conn().execute(sql, args)]

    def user_ids_with_beys(self, minimum: int = 1,
                           limit: Optional[int] = None) -> list[int]:
        """Everyone holding at least `minimum` blades.

        At `minimum = 1` a boss copy counts, because it is a blade the player
        fights with. Above 1 the count is inventory only — one copy is not two
        blades.
        """
        self.ensure_ready()
        minimum = max(1, int(minimum))
        sql = "SELECT user_id FROM users WHERE inv_count >= ?"
        args: tuple = (minimum,)
        if limit:
            sql += " LIMIT ?"
            args += (max(1, int(limit)),)
        ids = [r["user_id"] for r in self._conn().execute(sql, args)]
        if minimum <= 1:
            ids.extend(sorted(self._copy_holders(self._empty_ids())))
        out = []
        for u in ids:
            try:
                out.append(int(u))
            except (TypeError, ValueError):
                continue
        return out

    def ids_without_beys(self, user_ids) -> set:
        """Which of THESE ids own nothing at all.

        The composable half: an audience already narrowed some other way — a
        guild's members, everyone who missed update X — is filtered by
        subtracting this rather than re-querying from scratch. Ids the registry
        has never seen count as owning nothing, because they do.
        """
        self.ensure_ready()
        wanted = [str(u) for u in dict.fromkeys(user_ids)]
        if not wanted:
            return set()
        have: set = set()
        for i in range(0, len(wanted), 500):
            chunk = wanted[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            rows = self._conn().execute(
                f"SELECT user_id FROM users "
                f"WHERE inv_count >= 1 AND user_id IN ({marks})", chunk)
            have.update(r["user_id"] for r in rows)
        maybe_empty = [u for u in wanted if u not in have]
        have |= self._copy_holders(maybe_empty)
        out = set()
        for u in wanted:
            if u not in have:
                try:
                    out.add(int(u))
                except (TypeError, ValueError):
                    continue
        return out

    def count_without_beys(self) -> int:
        """How many registered profiles own nothing — the number to report."""
        self.ensure_ready()
        empty = self._empty_ids()
        return len(empty) - len(self._copy_holders(empty))

    # ── Reports ──────────────────────────────────────────────────────────────
    _REPORT_COLS = ("report_id", "kind", "user_id", "guild_id", "summary",
                    "body", "image_url", "status", "created_at",
                    "handled_by", "handled_at", "message_id")

    def reports_put(self, row: dict) -> None:
        self.ensure_ready()
        vals = tuple(row.get(c) for c in self._REPORT_COLS)
        marks = ",".join("?" for _ in self._REPORT_COLS)
        self._conn().execute(
            f"INSERT OR REPLACE INTO reports "
            f"({','.join(self._REPORT_COLS)}) VALUES ({marks})", vals)

    def reports_get(self, report_id: str) -> Optional[dict]:
        self.ensure_ready()
        row = self._conn().execute(
            "SELECT * FROM reports WHERE report_id = ?",
            (str(report_id),)).fetchone()
        return dict(row) if row else None

    def reports_set_status(self, report_id: str, status: str,
                           handled_by: Optional[str] = None) -> None:
        self.ensure_ready()
        self._conn().execute(
            "UPDATE reports SET status = ?, handled_by = ?, handled_at = ? "
            "WHERE report_id = ?",
            (str(status), (str(handled_by) if handled_by else None),
             time.time(), str(report_id)))

    def reports_set_field(self, report_id: str, field: str, value) -> None:
        """Set `image_url` or `message_id` after the fact.

        The column name is checked against a whitelist rather than formatted in
        blind: it is the one place here a caller supplies an identifier.
        """
        if field not in ("image_url", "message_id"):
            raise ValueError(f"reports_set_field: {field!r} is not settable")
        self.ensure_ready()
        self._conn().execute(
            f"UPDATE reports SET {field} = ? WHERE report_id = ?",
            (value, str(report_id)))

    def reports_open(self, limit: int = 10) -> list[dict]:
        self.ensure_ready()
        rows = self._conn().execute(
            "SELECT * FROM reports WHERE status IN ('OPEN', 'ACK') "
            "ORDER BY created_at DESC LIMIT ?", (max(1, int(limit)),))
        return [dict(r) for r in rows]

    def reports_count_since(self, user_id: str, cutoff: float) -> int:
        """How many this player has filed since `cutoff` — the spam gate.

        Counted from the TABLE, not from an in-process dict, so restarting the
        bot is not a way to reset your own cooldown.
        """
        self.ensure_ready()
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM reports "
            "WHERE user_id = ? AND created_at >= ?",
            (str(user_id), float(cutoff))).fetchone()
        return int(row["n"]) if row else 0

    def reports_last_at(self, user_id: str) -> Optional[float]:
        self.ensure_ready()
        row = self._conn().execute(
            "SELECT created_at FROM reports WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 1", (str(user_id),)).fetchone()
        return float(row["created_at"]) if row else None

    # ── Community: config, polls, giveaways ──────────────────────────────────
    #
    # Same rule as the notification methods above: every one has a same-named
    # twin on `MySQLStore` with the same signature.

    def community_config_get(self, key: str) -> Optional[str]:
        self.ensure_ready()
        row = self._conn().execute(
            "SELECT value FROM community_config WHERE key = ?",
            (str(key),)).fetchone()
        return row["value"] if row else None

    def community_config_put(self, key: str, value: str) -> None:
        self.ensure_ready()
        self._conn().execute(
            "INSERT OR REPLACE INTO community_config (key, value, updated_at) "
            "VALUES (?, ?, ?)", (str(key), str(value), time.time()))

    def community_config_delete(self, key: str) -> None:
        self.ensure_ready()
        self._conn().execute("DELETE FROM community_config WHERE key = ?",
                             (str(key),))

    def community_config_all(self) -> dict:
        self.ensure_ready()
        rows = self._conn().execute("SELECT key, value FROM community_config")
        return {r["key"]: r["value"] for r in rows}

    _POLL_COLS = ("poll_id", "guild_id", "channel_id", "message_id",
                  "author_id", "question", "options", "multi", "anonymous",
                  "ends_at", "state", "created_at", "closed_at", "results")

    def polls_put(self, row: dict) -> None:
        self.ensure_ready()
        data = dict(row)
        for key in ("options", "results"):
            if isinstance(data.get(key), (dict, list)):
                data[key] = json.dumps(data[key])
        vals = tuple(data.get(c) for c in self._POLL_COLS)
        marks = ",".join("?" for _ in self._POLL_COLS)
        self._conn().execute(
            f"INSERT OR REPLACE INTO community_polls "
            f"({','.join(self._POLL_COLS)}) VALUES ({marks})", vals)

    @staticmethod
    def _poll_row(row) -> dict:
        out = dict(row)
        for key, empty in (("options", []), ("results", None)):
            raw = out.get(key)
            if raw is None:
                out[key] = empty
                continue
            try:
                out[key] = json.loads(raw)
            except (json.JSONDecodeError, ValueError, TypeError):
                out[key] = empty
        return out

    def polls_get(self, poll_id: str) -> Optional[dict]:
        self.ensure_ready()
        row = self._conn().execute(
            "SELECT * FROM community_polls WHERE poll_id = ?",
            (str(poll_id),)).fetchone()
        return self._poll_row(row) if row else None

    def polls_list(self, guild_id: str, limit: int = 10) -> list[dict]:
        self.ensure_ready()
        rows = self._conn().execute(
            "SELECT * FROM community_polls WHERE guild_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (str(guild_id), max(1, int(limit))))
        return [self._poll_row(r) for r in rows]

    def polls_set_state(self, poll_id: str, state: str, *,
                        results=None, closed_at: Optional[float] = None) -> None:
        self.ensure_ready()
        payload = (json.dumps(results) if isinstance(results, (dict, list))
                   else results)
        self._conn().execute(
            "UPDATE community_polls SET state = ?, "
            "results = COALESCE(?, results), closed_at = COALESCE(?, closed_at) "
            "WHERE poll_id = ?",
            (str(state), payload, closed_at, str(poll_id)))

    def polls_set_message(self, poll_id: str, channel_id, message_id) -> None:
        self.ensure_ready()
        self._conn().execute(
            "UPDATE community_polls SET channel_id = ?, message_id = ? "
            "WHERE poll_id = ?",
            (str(channel_id), str(message_id), str(poll_id)))

    def polls_claim_due(self, now: Optional[float] = None,
                        limit: int = 20) -> list[str]:
        """Flip every poll whose timer has run out to CLOSING, atomically.

        One statement, exactly like `deliveries_claim`: select-then-update has
        a window between the halves, and with two ticks in flight that window
        is a poll closed and announced twice.
        """
        self.ensure_ready()
        ts = time.time() if now is None else float(now)
        rows = self._conn().execute("""
            UPDATE community_polls SET state = 'CLOSING'
             WHERE rowid IN (
                   SELECT rowid FROM community_polls
                    WHERE state = 'OPEN' AND ends_at IS NOT NULL
                      AND ends_at <= ?
                    ORDER BY ends_at LIMIT ?)
         RETURNING poll_id
        """, (ts, max(1, int(limit)))).fetchall()
        return [r["poll_id"] for r in rows]

    def polls_recover(self) -> int:
        """Return anything a dead process left mid-close to the queue."""
        self.ensure_ready()
        conn = self._conn()
        before = conn.total_changes
        conn.execute("UPDATE community_polls SET state = 'OPEN' "
                     "WHERE state = 'CLOSING'")
        return conn.total_changes - before

    def poll_vote(self, poll_id: str, user_id: str, choice: str, *,
                  now: Optional[float] = None) -> bool:
        """Record one vote. Returns False if this player already voted."""
        self.ensure_ready()
        ts = time.time() if now is None else float(now)
        conn = self._conn()
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO community_poll_votes "
            "(poll_id, user_id, choice, voted_at) VALUES (?, ?, ?, ?)",
            (str(poll_id), str(user_id), str(choice), ts))
        return conn.total_changes > before

    def poll_vote_of(self, poll_id: str, user_id: str) -> Optional[str]:
        self.ensure_ready()
        row = self._conn().execute(
            "SELECT choice FROM community_poll_votes "
            "WHERE poll_id = ? AND user_id = ?",
            (str(poll_id), str(user_id))).fetchone()
        return row["choice"] if row else None

    def poll_tally(self, poll_id: str) -> dict:
        self.ensure_ready()
        rows = self._conn().execute(
            "SELECT choice, COUNT(*) AS n FROM community_poll_votes "
            "WHERE poll_id = ? GROUP BY choice", (str(poll_id),))
        return {r["choice"]: r["n"] for r in rows}

    def poll_voters(self, poll_id: str) -> list[str]:
        self.ensure_ready()
        rows = self._conn().execute(
            "SELECT user_id FROM community_poll_votes WHERE poll_id = ?",
            (str(poll_id),))
        return [r["user_id"] for r in rows]

    _GIVEAWAY_COLS = ("giveaway_id", "guild_id", "channel_id", "message_id",
                      "host_id", "prize", "winners", "requirement", "ends_at",
                      "state", "created_at", "ended_at", "winner_ids")

    def giveaways_put(self, row: dict) -> None:
        self.ensure_ready()
        data = dict(row)
        for key in ("requirement", "winner_ids"):
            if isinstance(data.get(key), (dict, list)):
                data[key] = json.dumps(data[key])
        vals = tuple(data.get(c) for c in self._GIVEAWAY_COLS)
        marks = ",".join("?" for _ in self._GIVEAWAY_COLS)
        self._conn().execute(
            f"INSERT OR REPLACE INTO community_giveaways "
            f"({','.join(self._GIVEAWAY_COLS)}) VALUES ({marks})", vals)

    @staticmethod
    def _giveaway_row(row) -> dict:
        out = dict(row)
        for key, empty in (("requirement", {}), ("winner_ids", [])):
            raw = out.get(key)
            if raw is None:
                out[key] = empty
                continue
            try:
                out[key] = json.loads(raw)
            except (json.JSONDecodeError, ValueError, TypeError):
                out[key] = empty
        return out

    def giveaways_get(self, giveaway_id: str) -> Optional[dict]:
        self.ensure_ready()
        row = self._conn().execute(
            "SELECT * FROM community_giveaways WHERE giveaway_id = ?",
            (str(giveaway_id),)).fetchone()
        return self._giveaway_row(row) if row else None

    def giveaways_list(self, guild_id: str, limit: int = 10) -> list[dict]:
        self.ensure_ready()
        rows = self._conn().execute(
            "SELECT * FROM community_giveaways WHERE guild_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (str(guild_id), max(1, int(limit))))
        return [self._giveaway_row(r) for r in rows]

    def giveaways_set_state(self, giveaway_id: str, state: str, *,
                            winner_ids=None,
                            ended_at: Optional[float] = None) -> None:
        self.ensure_ready()
        payload = (json.dumps(winner_ids)
                   if isinstance(winner_ids, (dict, list)) else winner_ids)
        self._conn().execute(
            "UPDATE community_giveaways SET state = ?, "
            "winner_ids = COALESCE(?, winner_ids), "
            "ended_at = COALESCE(?, ended_at) WHERE giveaway_id = ?",
            (str(state), payload, ended_at, str(giveaway_id)))

    def giveaways_set_message(self, giveaway_id: str, channel_id,
                              message_id) -> None:
        self.ensure_ready()
        self._conn().execute(
            "UPDATE community_giveaways SET channel_id = ?, message_id = ? "
            "WHERE giveaway_id = ?",
            (str(channel_id), str(message_id), str(giveaway_id)))

    def giveaways_claim_due(self, now: Optional[float] = None,
                            limit: int = 20) -> list[str]:
        self.ensure_ready()
        ts = time.time() if now is None else float(now)
        rows = self._conn().execute("""
            UPDATE community_giveaways SET state = 'CLOSING'
             WHERE rowid IN (
                   SELECT rowid FROM community_giveaways
                    WHERE state = 'OPEN' AND ends_at IS NOT NULL
                      AND ends_at <= ?
                    ORDER BY ends_at LIMIT ?)
         RETURNING giveaway_id
        """, (ts, max(1, int(limit)))).fetchall()
        return [r["giveaway_id"] for r in rows]

    def giveaways_recover(self) -> int:
        self.ensure_ready()
        conn = self._conn()
        before = conn.total_changes
        conn.execute("UPDATE community_giveaways SET state = 'OPEN' "
                     "WHERE state = 'CLOSING'")
        return conn.total_changes - before

    def giveaway_enter(self, giveaway_id: str, user_id: str, *,
                       now: Optional[float] = None) -> bool:
        """Enter once. Returns False if this player was already in."""
        self.ensure_ready()
        ts = time.time() if now is None else float(now)
        conn = self._conn()
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO community_giveaway_entries "
            "(giveaway_id, user_id, entered_at) VALUES (?, ?, ?)",
            (str(giveaway_id), str(user_id), ts))
        return conn.total_changes > before

    def giveaway_entries(self, giveaway_id: str) -> list[str]:
        self.ensure_ready()
        rows = self._conn().execute(
            "SELECT user_id FROM community_giveaway_entries "
            "WHERE giveaway_id = ? ORDER BY entered_at",
            (str(giveaway_id),))
        return [r["user_id"] for r in rows]

    def giveaway_entry_count(self, giveaway_id: str) -> int:
        self.ensure_ready()
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM community_giveaway_entries "
            "WHERE giveaway_id = ?", (str(giveaway_id),)).fetchone()
        return int(row["n"]) if row else 0
