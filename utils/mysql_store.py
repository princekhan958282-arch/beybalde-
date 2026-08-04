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
    INDEX idx_seen  (last_seen DESC)
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
