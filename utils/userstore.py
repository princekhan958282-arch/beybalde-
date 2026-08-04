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
