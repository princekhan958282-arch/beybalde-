"""
utils/mysql_store.py
--------------------
Optional MySQL backend for the user registry and the small JSON stores.

Why you'd want this
-------------------
SQLite lives inside the container. Panels like Pterodactyl can and do wipe the
filesystem on a rebuild or a reinstall, and when that happens `users.db` goes
with it. A managed MySQL host survives that, can be backed up independently,
and can be inspected from outside the bot.

Why it is OPTIONAL, and falls back
----------------------------------
SQLite reads take ~0.07 ms because the file is right there. Every MySQL query
crosses a network, so it is slower by orders of magnitude — and, more
importantly, it introduces a way for the bot to be *completely dead* that
didn't exist before: if the database host is unreachable, a hard dependency
means nobody can play at all.

So MySQL is used when it's configured AND reachable, and the moment it isn't,
everything falls back to the local SQLite store and the bot keeps running. A
degraded bot beats a dead one.

Configuration — same three sources as every other secret:
    MYSQL_URL = "mysql://user:password@host:3306/dbname"

A JDBC-style string ("jdbc:mysql://…") is accepted too, since that is what
hosting panels usually hand you.
"""

import json
import logging
import os
import threading
import time
from typing import Optional
from urllib.parse import unquote, urlparse

log = logging.getLogger("beyblade_bot")

# Hot fields mirrored into real columns, same as the SQLite store, so
# leaderboards can ORDER BY instead of loading every row into Python.
HOT_FIELDS = ["coins", "level", "xp", "wins", "losses", "rank_score"]

_SCHEMA_USERS = """
CREATE TABLE IF NOT EXISTS users (
    user_id     VARCHAR(32)  NOT NULL PRIMARY KEY,
    coins       BIGINT       NOT NULL DEFAULT 0,
    level       INT          NOT NULL DEFAULT 0,
    xp          BIGINT       NOT NULL DEFAULT 0,
    wins        INT          NOT NULL DEFAULT 0,
    losses      INT          NOT NULL DEFAULT 0,
    rank_score  INT          NOT NULL DEFAULT 0,
    inv_count   INT          NOT NULL DEFAULT 0,
    created_at  DOUBLE       NULL,
    last_seen   DOUBLE       NULL,
    data        LONGTEXT     NOT NULL,
    INDEX idx_coins (coins DESC),
    INDEX idx_rank  (rank_score DESC, wins DESC),
    INDEX idx_level (level DESC),
    INDEX idx_seen  (last_seen DESC),
    INDEX idx_created (created_at DESC),
    INDEX idx_inv   (inv_count)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# Everything that currently lives in a small JSON file goes here, one row each.
_SCHEMA_KV = """
CREATE TABLE IF NOT EXISTS kv_store (
    name        VARCHAR(64)  NOT NULL PRIMARY KEY,
    data        LONGTEXT     NOT NULL,
    updated_at  DOUBLE       NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# ── Notifications ────────────────────────────────────────────────────────────
# The SQLite twin of these lives in `utils/userstore.py`; the two schemas and
# every method name below are deliberately kept identical so no caller has to
# know which backend is live. Only the placeholder style (%s vs ?) and the
# ignore-duplicate spelling differ.
_SCHEMA_UPDATES = """
CREATE TABLE IF NOT EXISTS updates (
    update_id    VARCHAR(64)  NOT NULL PRIMARY KEY,
    version      VARCHAR(32)  NULL,
    title        VARCHAR(256) NOT NULL,
    body         LONGTEXT     NOT NULL,
    priority     VARCHAR(16)  NOT NULL,
    audience     LONGTEXT     NOT NULL,
    image_url    TEXT         NULL,
    event        VARCHAR(32)  NULL,
    created_by   VARCHAR(32)  NULL,
    created_at   DOUBLE       NOT NULL,
    scheduled_at DOUBLE       NULL,
    state        VARCHAR(16)  NOT NULL,
    INDEX idx_updates_created (created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# The composite PRIMARY KEY is the duplicate prevention, enforced by the
# database rather than by application logic — see the note in userstore.py.
_SCHEMA_DELIVERIES = """
CREATE TABLE IF NOT EXISTS update_deliveries (
    update_id  VARCHAR(64) NOT NULL,
    user_id    VARCHAR(32) NOT NULL,
    state      VARCHAR(16) NOT NULL,
    attempts   INT         NOT NULL DEFAULT 0,
    last_error TEXT        NULL,
    queued_at  DOUBLE      NOT NULL,
    sent_at    DOUBLE      NULL,
    PRIMARY KEY (update_id, user_id),
    INDEX idx_deliveries_update_state (update_id, state),
    INDEX idx_deliveries_state (state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_SCHEMA_REPORTS = """
CREATE TABLE IF NOT EXISTS reports (
    report_id  VARCHAR(64)  NOT NULL PRIMARY KEY,
    kind       VARCHAR(16)  NOT NULL,
    user_id    VARCHAR(32)  NOT NULL,
    guild_id   VARCHAR(32)  NULL,
    summary    VARCHAR(256) NOT NULL,
    body       LONGTEXT     NOT NULL,
    image_url  TEXT         NULL,
    status     VARCHAR(16)  NOT NULL,
    created_at DOUBLE       NOT NULL,
    handled_by VARCHAR(32)  NULL,
    handled_at DOUBLE       NULL,
    message_id VARCHAR(32)  NULL,
    INDEX idx_reports_user_time (user_id, created_at DESC),
    INDEX idx_reports_status (status, created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# The JSON stores worth moving. beyblades.json is deliberately NOT here — it's
# static game content that ships with the code, not player data.
KV_FILES = [
    "casino_wallets.json",
    "clans.json",
    "clan_wars.json",
    "redeem_codes.json",
    "backup_codes.json",
    "spawn_state.json",
    "avatar_inventory.json",
    "config.json",
]


def parse_url(url: str) -> Optional[dict]:
    """Accept mysql://… or jdbc:mysql://… and return connect kwargs."""
    if not url:
        return None
    raw = url.strip()
    if raw.lower().startswith("jdbc:"):
        raw = raw[5:]
    try:
        u = urlparse(raw)
        if not u.hostname:
            return None
        return {
            "host":     u.hostname,
            "port":     u.port or 3306,
            "user":     unquote(u.username or ""),
            "password": unquote(u.password or ""),
            "database": (u.path or "").lstrip("/"),
        }
    except Exception as exc:
        log.error(f"[mysql] couldn't parse MYSQL_URL: {exc}")
        return None


def driver_available() -> bool:
    try:
        import pymysql  # noqa: F401
        return True
    except ImportError:
        return False


class MySQLStore:
    """Same surface as utils.userstore.UserStore, backed by MySQL."""

    def __init__(self, url: str):
        self.cfg = parse_url(url)
        self._local = threading.local()
        self._ready = False
        self._init_lock = threading.Lock()
        self.last_error: Optional[str] = None

    # ── Connection ───────────────────────────────────────────────────────────
    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.ping(reconnect=True)
                return conn
            except Exception:
                conn = None
        import pymysql
        conn = pymysql.connect(
            **self.cfg,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=8,
            read_timeout=15,
            write_timeout=15,
            cursorclass=pymysql.cursors.DictCursor,
        )
        self._local.conn = conn
        return conn

    def probe(self) -> tuple[bool, str]:
        """Can we actually reach it? Returns (ok, message). Never raises."""
        if not self.cfg:
            return False, "MYSQL_URL is missing or malformed"
        if not driver_available():
            return False, ("PyMySQL isn't installed — add `PyMySQL` to "
                           "requirements.txt or the panel's extra packages")
        try:
            with self._conn().cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                cur.fetchone()
            return True, f"connected to {self.cfg['host']}/{self.cfg['database']}"
        except Exception as exc:
            self.last_error = str(exc)
            return False, f"{type(exc).__name__}: {exc}"

    def ensure_ready(self) -> None:
        if self._ready:
            return
        with self._init_lock:
            if self._ready:
                return
            with self._conn().cursor() as cur:
                cur.execute(_SCHEMA_USERS)
                cur.execute(_SCHEMA_KV)
                cur.execute(_SCHEMA_UPDATES)
                cur.execute(_SCHEMA_DELIVERIES)
                cur.execute(_SCHEMA_REPORTS)
            self._ready = True

    # ── Row helpers ──────────────────────────────────────────────────────────
    _UPSERT = """
        INSERT INTO users (user_id, coins, level, xp, wins, losses, rank_score,
                           inv_count, created_at, last_seen, data)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            coins=VALUES(coins), level=VALUES(level), xp=VALUES(xp),
            wins=VALUES(wins), losses=VALUES(losses),
            rank_score=VALUES(rank_score), inv_count=VALUES(inv_count),
            last_seen=COALESCE(VALUES(last_seen), last_seen),
            created_at=COALESCE(created_at, VALUES(created_at)),
            data=VALUES(data)
    """

    @staticmethod
    def _int(v, default=0) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def _row(self, uid: str, prof: dict, created=None, seen=None) -> tuple:
        inv = prof.get("inventory")
        return (
            uid,
            self._int(prof.get("coins")), self._int(prof.get("level")),
            self._int(prof.get("xp")), self._int(prof.get("wins")),
            self._int(prof.get("losses")), self._int(prof.get("rank_score")),
            len(inv) if isinstance(inv, list) else 0,
            created if created is not None else time.time(),
            seen,
            json.dumps(prof, separators=(",", ":")),
        )

    # ── Public API (mirrors UserStore) ───────────────────────────────────────
    def get_one(self, uid: str) -> Optional[dict]:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("SELECT data FROM users WHERE user_id=%s", (uid,))
            row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row["data"])
        except (json.JSONDecodeError, ValueError):
            return None

    def put_one(self, uid: str, profile: dict, touch: bool = True) -> None:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute(self._UPSERT,
                        self._row(uid, profile,
                                  seen=time.time() if touch else None))

    def has(self, uid: str) -> bool:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE user_id=%s", (uid,))
            return cur.fetchone() is not None

    def load_all(self) -> dict:
        self.ensure_ready()
        out = {}
        with self._conn().cursor() as cur:
            cur.execute("SELECT user_id, data FROM users")
            for row in cur.fetchall():
                try:
                    out[row["user_id"]] = json.loads(row["data"])
                except (json.JSONDecodeError, ValueError):
                    continue
        return out

    def all_user_ids(self) -> list[int]:
        """Every registered user id, without parsing a single profile blob.

        Must exist on BOTH backends. It didn't at first — only the SQLite store
        had it — so with MySQL active `USER_STORE.all_user_ids()` raised
        AttributeError, the caller's broad except swallowed it, and the
        announcement audience silently fell back to an empty list. Any method
        the app calls on USER_STORE has to be implemented on both stores or
        the behaviour changes with the backend.
        """
        self.ensure_ready()
        out: list[int] = []
        with self._conn().cursor() as cur:
            cur.execute("SELECT user_id FROM users")
            for row in cur.fetchall():
                try:
                    out.append(int(row["user_id"]))
                except (TypeError, ValueError, KeyError):
                    continue
        return out

    def save_all(self, data: dict) -> None:
        self.ensure_ready()
        rows = [self._row(str(u), p) for u, p in data.items()
                if isinstance(p, dict)]
        if not rows:
            return
        with self._conn().cursor() as cur:
            cur.executemany(self._UPSERT, rows)

    def count(self) -> int:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            return cur.fetchone()["n"]

    def top_by(self, column: str, limit: int = 10) -> list[dict]:
        self.ensure_ready()
        if column not in set(HOT_FIELDS) | {"inv_count"}:
            raise ValueError(f"not a sortable column: {column}")
        with self._conn().cursor() as cur:
            cur.execute(f"SELECT data FROM users ORDER BY {column} DESC LIMIT %s",
                        (limit,))
            rows = cur.fetchall()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["data"]))
            except (json.JSONDecodeError, ValueError):
                continue
        return out

    def stats(self) -> dict:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(coins),0) AS coin_supply,
                       COALESCE(MAX(coins),0) AS richest,
                       SUM(CASE WHEN coins=0 AND inv_count=0 AND xp=0
                                THEN 1 ELSE 0 END) AS ghosts,
                       SUM(CASE WHEN wins+losses>0 THEN 1 ELSE 0 END) AS battlers,
                       SUM(CASE WHEN inv_count>0 THEN 1 ELSE 0 END) AS collectors
                FROM users
            """)
            return dict(cur.fetchone())

    def coin_percentiles(self) -> dict:
        self.ensure_ready()
        out = {}
        with self._conn().cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            total = cur.fetchone()["n"]
            if not total:
                return {}
            for label, frac in (("p50", .50), ("p90", .90), ("p99", .99)):
                off = min(total - 1, int(total * frac))
                cur.execute("SELECT coins FROM users ORDER BY coins ASC "
                            "LIMIT 1 OFFSET %s", (off,))
                r = cur.fetchone()
                out[label] = r["coins"] if r else 0
        return out

    def top_wallets(self, limit: int = 10) -> list[dict]:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("""SELECT user_id, coins, level, wins, losses,
                                  inv_count, created_at, last_seen
                           FROM users ORDER BY coins DESC LIMIT %s""", (limit,))
            return [dict(r) for r in cur.fetchall()]

    def active_since(self, cutoff: float) -> int:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users WHERE last_seen>=%s",
                        (cutoff,))
            return cur.fetchone()["n"]

    def active_users_since(self, cutoff: float, limit: int = 25) -> list[dict]:
        """WHO was active since `cutoff`, most recent first.

        Parity with UserStore matters more than usual here: `database.py`
        calls whichever store is live, so a method on one and not the other
        breaks the moment MYSQL_URL is set — and `buildinfo.store_parity()`
        exists because that exact class of bug shipped once already.
        """
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("""
                SELECT user_id, coins, level, wins, losses, inv_count, last_seen
                FROM users WHERE last_seen>=%s
                ORDER BY last_seen DESC LIMIT %s
            """, (cutoff, max(1, int(limit))))
            return [dict(r) for r in cur.fetchall()]

    def checkpoint(self) -> None:
        """No-op — MySQL has no WAL to fold in. Here so callers don't branch."""
        return None

    def export_json(self, path: str) -> int:
        data = self.load_all()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return len(data)

    # ── Key-value stores (the small JSON files) ──────────────────────────────
    # ── Notifications: updates, the delivery ledger, and reports ─────────────
    #
    # Every method here has a same-named twin on `utils.userstore.UserStore`
    # with the same signature. Two spellings differ and nothing else:
    # placeholders are %s, and "insert unless it exists" is INSERT IGNORE
    # rather than INSERT OR IGNORE.

    _UPDATE_COLS = ("update_id", "version", "title", "body", "priority",
                    "audience", "image_url", "event", "created_by",
                    "created_at", "scheduled_at", "state")

    def updates_put(self, row: dict) -> None:
        self.ensure_ready()
        data = dict(row)
        if isinstance(data.get("audience"), (dict, list)):
            data["audience"] = json.dumps(data["audience"])
        vals = tuple(data.get(c) for c in self._UPDATE_COLS)
        cols = ",".join(self._UPDATE_COLS)
        marks = ",".join("%s" for _ in self._UPDATE_COLS)
        dup = ",".join(f"{c}=VALUES({c})" for c in self._UPDATE_COLS[1:])
        with self._conn().cursor() as cur:
            cur.execute(f"INSERT INTO updates ({cols}) VALUES ({marks}) "
                        f"ON DUPLICATE KEY UPDATE {dup}", vals)

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
        with self._conn().cursor() as cur:
            cur.execute("SELECT * FROM updates WHERE update_id=%s",
                        (str(update_id),))
            row = cur.fetchone()
        return self._update_row(row) if row else None

    def updates_list(self, limit: int = 20) -> list[dict]:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("SELECT * FROM updates ORDER BY created_at DESC "
                        "LIMIT %s", (max(1, int(limit)),))
            rows = cur.fetchall() or []
        return [self._update_row(r) for r in rows]

    def updates_set_state(self, update_id: str, state: str) -> None:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("UPDATE updates SET state=%s WHERE update_id=%s",
                        (str(state), str(update_id)))

    def deliveries_enqueue(self, update_id: str, user_ids, *,
                           now: Optional[float] = None) -> int:
        self.ensure_ready()
        ts = time.time() if now is None else float(now)
        rows = [(str(update_id), str(u), "PENDING", ts)
                for u in dict.fromkeys(user_ids)]
        if not rows:
            return 0
        with self._conn().cursor() as cur:
            cur.executemany(
                "INSERT IGNORE INTO update_deliveries "
                "(update_id, user_id, state, queued_at) "
                "VALUES (%s,%s,%s,%s)", rows)
            return int(cur.rowcount or 0)

    def deliveries_claim(self, update_id: str, limit: int = 25) -> list[str]:
        """Take a batch off the queue, atomically.

        MySQL has no UPDATE…RETURNING, so this is a SELECT … FOR UPDATE inside
        a transaction rather than SQLite's single statement. Same guarantee —
        the rows are locked before they are read, so a second claimer blocks
        instead of taking them too — reached a different way because the engine
        offers a different tool.
        """
        self.ensure_ready()
        conn = self._conn()
        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM update_deliveries "
                    "WHERE update_id=%s AND state IN ('PENDING','RETRY') "
                    "ORDER BY queued_at LIMIT %s FOR UPDATE",
                    (str(update_id), max(1, int(limit))))
                ids = [r["user_id"] for r in (cur.fetchall() or [])]
                if ids:
                    marks = ",".join(["%s"] * len(ids))
                    cur.execute(
                        f"UPDATE update_deliveries SET state='SENDING' "
                        f"WHERE update_id=%s AND user_id IN ({marks})",
                        (str(update_id), *ids))
            conn.commit()
            return ids
        except Exception:
            conn.rollback()
            raise

    def deliveries_mark(self, update_id: str, user_id: str, state: str,
                        error: Optional[str] = None,
                        bump_attempt: bool = True) -> None:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("""
                UPDATE update_deliveries
                   SET state=%s,
                       attempts = attempts + %s,
                       last_error=%s,
                       sent_at = CASE WHEN %s='SENT' THEN %s ELSE sent_at END
                 WHERE update_id=%s AND user_id=%s
            """, (str(state), 1 if bump_attempt else 0, (error or None),
                  str(state), time.time(), str(update_id), str(user_id)))

    def deliveries_counts(self, update_id: str) -> dict:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("SELECT state, COUNT(*) AS n FROM update_deliveries "
                        "WHERE update_id=%s GROUP BY state", (str(update_id),))
            return {r["state"]: int(r["n"]) for r in (cur.fetchall() or [])}

    def deliveries_attempts(self, update_id: str, user_id: str) -> int:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("SELECT attempts FROM update_deliveries "
                        "WHERE update_id=%s AND user_id=%s",
                        (str(update_id), str(user_id)))
            row = cur.fetchone()
        return int(row["attempts"]) if row else 0

    def deliveries_reset_stuck(self) -> int:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("UPDATE update_deliveries SET state='PENDING' "
                        "WHERE state='SENDING'")
            return int(cur.rowcount or 0)

    def deliveries_drop_pending(self, update_id: str) -> int:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("DELETE FROM update_deliveries WHERE update_id=%s "
                        "AND state IN ('PENDING','RETRY','SENDING')",
                        (str(update_id),))
            return int(cur.rowcount or 0)

    def deliveries_pending_updates(self) -> list[str]:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("SELECT DISTINCT update_id FROM update_deliveries "
                        "WHERE state IN ('PENDING','RETRY')")
            return [r["update_id"] for r in (cur.fetchall() or [])]

    def users_never_received(self, update_id: str,
                             limit: Optional[int] = None) -> list[int]:
        self.ensure_ready()
        sql = ("SELECT u.user_id AS uid FROM users u "
               "LEFT JOIN update_deliveries d "
               "  ON d.user_id = u.user_id AND d.update_id = %s "
               "WHERE d.user_id IS NULL")
        args: tuple = (str(update_id),)
        if limit:
            sql += " LIMIT %s"
            args += (max(1, int(limit)),)
        out = []
        with self._conn().cursor() as cur:
            cur.execute(sql, args)
            for r in (cur.fetchall() or []):
                try:
                    out.append(int(r["uid"]))
                except (TypeError, ValueError):
                    continue
        return out

    def created_since(self, cutoff: float,
                      limit: Optional[int] = None) -> list[int]:
        self.ensure_ready()
        sql = ("SELECT user_id FROM users WHERE created_at >= %s "
               "ORDER BY created_at DESC")
        args: tuple = (float(cutoff),)
        if limit:
            sql += " LIMIT %s"
            args += (max(1, int(limit)),)
        out = []
        with self._conn().cursor() as cur:
            cur.execute(sql, args)
            for r in (cur.fetchall() or []):
                try:
                    out.append(int(r["user_id"]))
                except (TypeError, ValueError):
                    continue
        return out

    # ── Audience filters ─────────────────────────────────────────────────────
    # See the SQLite twin in utils/userstore.py for why this is exact rather
    # than a bare `inv_count = 0`: a boss copy lives in the JSON blob, not in
    # `inventory`, and a player whose only blade is a copy has very obviously
    # played. Only the profiles that come back EMPTY get deserialised.

    def _copy_holders(self, uids) -> set:
        wanted = [str(u) for u in uids]
        if not wanted:
            return set()
        out: set = set()
        with self._conn().cursor() as cur:
            for i in range(0, len(wanted), 500):
                chunk = wanted[i:i + 500]
                marks = ",".join(["%s"] * len(chunk))
                cur.execute(f"SELECT user_id, data FROM users "
                            f"WHERE user_id IN ({marks})", chunk)
                for r in (cur.fetchall() or []):
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
            sql += " LIMIT %s"
            args = (max(1, int(limit)),)
        with self._conn().cursor() as cur:
            cur.execute(sql, args)
            return [r["user_id"] for r in (cur.fetchall() or [])]

    def user_ids_with_beys(self, minimum: int = 1,
                           limit: Optional[int] = None) -> list[int]:
        """Everyone holding at least `minimum` blades. A boss copy counts at
        `minimum = 1` and not above it — one copy is not two blades."""
        self.ensure_ready()
        minimum = max(1, int(minimum))
        sql = "SELECT user_id FROM users WHERE inv_count >= %s"
        args: tuple = (minimum,)
        if limit:
            sql += " LIMIT %s"
            args += (max(1, int(limit)),)
        with self._conn().cursor() as cur:
            cur.execute(sql, args)
            ids = [r["user_id"] for r in (cur.fetchall() or [])]
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
        """Which of THESE ids own nothing at all. Unknown ids count as none."""
        self.ensure_ready()
        wanted = [str(u) for u in dict.fromkeys(user_ids)]
        if not wanted:
            return set()
        have: set = set()
        with self._conn().cursor() as cur:
            for i in range(0, len(wanted), 500):
                chunk = wanted[i:i + 500]
                marks = ",".join(["%s"] * len(chunk))
                cur.execute(f"SELECT user_id FROM users "
                            f"WHERE inv_count >= 1 AND user_id IN ({marks})",
                            chunk)
                have.update(r["user_id"] for r in (cur.fetchall() or []))
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
        cols = ",".join(self._REPORT_COLS)
        marks = ",".join("%s" for _ in self._REPORT_COLS)
        dup = ",".join(f"{c}=VALUES({c})" for c in self._REPORT_COLS[1:])
        with self._conn().cursor() as cur:
            cur.execute(f"INSERT INTO reports ({cols}) VALUES ({marks}) "
                        f"ON DUPLICATE KEY UPDATE {dup}", vals)

    def reports_get(self, report_id: str) -> Optional[dict]:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("SELECT * FROM reports WHERE report_id=%s",
                        (str(report_id),))
            row = cur.fetchone()
        return dict(row) if row else None

    def reports_set_status(self, report_id: str, status: str,
                           handled_by: Optional[str] = None) -> None:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("UPDATE reports SET status=%s, handled_by=%s, "
                        "handled_at=%s WHERE report_id=%s",
                        (str(status),
                         (str(handled_by) if handled_by else None),
                         time.time(), str(report_id)))

    def reports_set_field(self, report_id: str, field: str, value) -> None:
        if field not in ("image_url", "message_id"):
            raise ValueError(f"reports_set_field: {field!r} is not settable")
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute(f"UPDATE reports SET {field}=%s WHERE report_id=%s",
                        (value, str(report_id)))

    def reports_open(self, limit: int = 10) -> list[dict]:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("SELECT * FROM reports WHERE status IN "
                        "('OPEN','ACK') ORDER BY created_at DESC LIMIT %s",
                        (max(1, int(limit)),))
            return [dict(r) for r in (cur.fetchall() or [])]

    def reports_count_since(self, user_id: str, cutoff: float) -> int:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM reports "
                        "WHERE user_id=%s AND created_at >= %s",
                        (str(user_id), float(cutoff)))
            row = cur.fetchone()
        return int(row["n"]) if row else 0

    def reports_last_at(self, user_id: str) -> Optional[float]:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("SELECT created_at FROM reports WHERE user_id=%s "
                        "ORDER BY created_at DESC LIMIT 1", (str(user_id),))
            row = cur.fetchone()
        return float(row["created_at"]) if row else None

    def kv_get(self, name: str) -> Optional[dict]:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("SELECT data FROM kv_store WHERE name=%s", (name,))
            row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row["data"])
        except (json.JSONDecodeError, ValueError):
            return None

    def kv_put(self, name: str, data) -> None:
        self.ensure_ready()
        blob = json.dumps(data, separators=(",", ":"))
        with self._conn().cursor() as cur:
            cur.execute("""INSERT INTO kv_store (name, data, updated_at)
                           VALUES (%s,%s,%s)
                           ON DUPLICATE KEY UPDATE
                             data=VALUES(data), updated_at=VALUES(updated_at)""",
                        (name, blob, time.time()))

    def kv_names(self) -> list[str]:
        self.ensure_ready()
        with self._conn().cursor() as cur:
            cur.execute("SELECT name FROM kv_store")
            return [r["name"] for r in cur.fetchall()]


# ── Migration ─────────────────────────────────────────────────────────────────

def migrate(source_store, data_dir: str, mysql: MySQLStore,
            force: bool = False) -> dict:
    """Copy everything from the local stores into MySQL.

    Non-destructive in both directions: the local SQLite file and every JSON
    file are left exactly as they are, so a failed or half-finished migration
    costs nothing and can simply be run again.

    Skips the user table if it already has rows, unless `force`.
    """
    report = {"users": 0, "kv": {}, "skipped": False, "errors": []}
    mysql.ensure_ready()

    existing = mysql.count()
    if existing and not force:
        report["skipped"] = True
        report["users"] = existing
    else:
        try:
            everyone = source_store.load_all()
            if everyone:
                mysql.save_all(everyone)
            report["users"] = mysql.count()
        except Exception as exc:
            report["errors"].append(f"users: {exc}")

    for fname in KV_FILES:
        path = os.path.join(data_dir, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read().strip()
            payload = json.loads(text) if text else {}
            key = fname[:-5] if fname.endswith(".json") else fname
            if mysql.kv_get(key) is not None and not force:
                report["kv"][key] = "already present"
                continue
            mysql.kv_put(key, payload)
            n = len(payload) if hasattr(payload, "__len__") else 1
            report["kv"][key] = f"{n} entries"
        except Exception as exc:
            report["errors"].append(f"{fname}: {exc}")

    return report


def verify(source_store, mysql: MySQLStore, sample: int = 200) -> dict:
    """Spot-check that what landed in MySQL matches the source."""
    import random
    out = {"source_rows": 0, "mysql_rows": 0, "checked": 0, "mismatched": 0}
    try:
        src = source_store.load_all()
        out["source_rows"] = len(src)
        out["mysql_rows"] = mysql.count()
        keys = list(src)
        if len(keys) > sample:
            keys = random.sample(keys, sample)
        for k in keys:
            out["checked"] += 1
            if mysql.get_one(k) != src[k]:
                out["mismatched"] += 1
    except Exception as exc:
        out["error"] = str(exc)
    return out
