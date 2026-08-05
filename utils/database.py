"""
utils/database.py
-----------------
Centralised read/write helpers for beyblades.json and users.json.
All Cogs import from here so file-path logic lives in exactly one place.

Level System
------------
  XP is earned by battling:  WIN = 100 XP,  LOSS = 40 XP.
  Level formula: level = floor(sqrt(xp / 50))  capped at MAX_LEVEL = 100.
  XP to next level: (next_level)^2 * 50
"""

import copy
import json
import logging
import math
import os
import threading
import time
from typing import Optional

from .userstore import UserStore

log = logging.getLogger("beyblade_bot")

# ── Absolute paths (works regardless of CWD) ──────────────────────────────────
BASE_DIR       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEYBLADES_PATH = os.path.join(BASE_DIR, "data", "beyblades.json")
USERS_PATH     = os.path.join(BASE_DIR, "data", "users.json")
CONFIG_PATH    = os.path.join(BASE_DIR, "data", "config.json")
SPAWN_PATH     = os.path.join(BASE_DIR, "data", "spawn_state.json")
AVATARS_PATH   = os.path.join(BASE_DIR, "data", "avatar_inventory.json")
USERS_DB_PATH  = os.path.join(BASE_DIR, "data", "users.db")

# ── User registry backend ─────────────────────────────────────────────────────
# users.json used to be re-parsed and rewritten in full (~1 MB) on every single
# profile write. It is now backed by SQLite: one row touched per write instead
# of the whole registry. The public helpers below keep their old signatures, so
# no calling code had to change. users.json is imported automatically on first
# run and then left untouched as a rollback backup.
USER_STORE = UserStore(USERS_DB_PATH, USERS_PATH)

# ── Optional MySQL backend ────────────────────────────────────────────────────
# If MYSQL_URL is configured AND the host answers, the registry moves to MySQL
# so it survives a container rebuild. If it is missing, malformed, the driver
# isn't installed, or the host is simply down, everything stays on the local
# SQLite store and the bot runs normally. A hard dependency here would mean a
# database outage takes the whole bot offline, which is a worse failure than
# losing durability.
SQLITE_STORE = USER_STORE
MYSQL_STORE = None
BACKEND = "sqlite"


def _init_mysql() -> None:
    global USER_STORE, MYSQL_STORE, BACKEND
    try:
        from .secrets import get as _secret
        url = _secret("MYSQL_URL")
        if not url:
            return
        from .mysql_store import MySQLStore, migrate
        store = MySQLStore(url)
        ok, msg = store.probe()
        if not ok:
            log.warning(f"[db] MYSQL_URL set but unusable ({msg}) — "
                        f"staying on SQLite")
            return

        store.ensure_ready()
        if store.count() == 0 and SQLITE_STORE.count() > 0:
            log.info("[db] MySQL is empty — migrating the local registry across")
            rep = migrate(SQLITE_STORE, os.path.join(BASE_DIR, "data"), store)
            log.info(f"[db] migrated {rep['users']:,} profiles, "
                     f"kv: {rep['kv']}")
            if rep["errors"]:
                log.warning(f"[db] migration warnings: {rep['errors']}")

        MYSQL_STORE = store
        USER_STORE = store
        BACKEND = "mysql"
        log.info(f"[db] backend: MySQL ({msg}) — {store.count():,} profiles")
    except Exception as exc:
        log.warning(f"[db] MySQL init failed ({type(exc).__name__}: {exc}) — "
                    f"staying on SQLite")


_init_mysql()

# ── Per-file locks (Bug #1 fix: prevent concurrent read-modify-write corruption) ─
_users_lock  = threading.Lock()
_config_lock = threading.Lock()
_spawn_lock  = threading.Lock()
_avatar_lock = threading.Lock()

# ── Level system constants ────────────────────────────────────────────────────
MAX_LEVEL          = 100
XP_WIN             = 100
XP_LOSS            = 40
STAT_BONUS_PER_10  = 0.02   # +2% to all stats per 10 trainer levels (max +20% at Lv100)


# ── Durable JSON persistence helpers ──────────────────────────────────────────
# A direct open("w")+json.dump truncates the file first, so a crash / disk-full
# mid-write can leave a half-written (corrupt) file and wipe live player data.
# These helpers make writes atomic (write to a temp file, fsync, then os.replace
# — an atomic rename on POSIX) and make reads self-healing (a missing/empty/
# corrupt file recovers to a safe default instead of crashing every command).

def _atomic_write_json(path: str, data) -> None:
    """Write JSON atomically: temp file → fsync → atomic rename."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)   # atomic on POSIX; never leaves a partial file
    with _cache_lock:       # the on-disk file changed — drop the parsed copy
        _json_cache.pop(path, None)


# ── Parsed-JSON cache ─────────────────────────────────────────────────────────
# Every read used to re-open and re-parse the whole file. beyblades.json is
# ~660 KB (1.8 ms) and users.json ~1 MB (8 ms), and several commands call these
# inside a loop — ;list on a large inventory burned ~125 ms and a tournament
# round ~194 ms of *synchronous* parsing, which blocks the asyncio event loop
# and freezes the whole bot for every other user meanwhile.
#
# The cache is keyed on (mtime_ns, size) so an external edit to the file is
# still picked up, and _atomic_write_json drops the entry on every write.
# Callers that mutate must copy — see get_beyblade / get_user, which hand back
# a deepcopy of the single record rather than the shared structure.
_json_cache: dict[str, tuple[tuple[int, int], object]] = {}
_cache_lock = threading.Lock()


def _read_json_cached(path: str, factory=dict):
    """_read_json, but reuses the parsed object while the file is unchanged.

    The returned object is SHARED — never mutate it, copy what you need.
    """
    try:
        st  = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return factory()
    with _cache_lock:
        hit = _json_cache.get(path)
        if hit is not None and hit[0] == sig:
            return hit[1]
    data = _read_json(path, factory)
    with _cache_lock:
        _json_cache[path] = (sig, data)
    return data


def _read_json(path: str, factory=dict):
    """Read JSON, recovering to factory() if the file is missing/empty/corrupt.

    A corrupt file is renamed to  <path>.corrupt  (so it can be inspected) rather
    than silently discarded, then the default is returned so the bot keeps running.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return factory()
    if not text.strip():
        return factory()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        try:
            os.replace(path, f"{path}.corrupt")
        except OSError:
            pass
        return factory()


def xp_for_level(level: int) -> int:
    """Total XP required to reach `level` (from 0)."""
    return level * level * 50


def level_from_xp(xp: int) -> int:
    """Current level given total accumulated XP (capped at MAX_LEVEL).

    Clamps negative XP to 0 — math.sqrt of a negative raises ValueError, which
    would break every profile/battle command for a corrupted profile.
    """
    return min(MAX_LEVEL, int(math.floor(math.sqrt(max(0, xp) / 50))))


def xp_to_next_level(xp: int) -> tuple[int, int, int]:
    """
    Returns (current_level, xp_needed_for_next, xp_progress_in_current_level).
    At MAX_LEVEL returns (100, 0, 0).
    """
    lvl = level_from_xp(xp)
    if lvl >= MAX_LEVEL:
        return MAX_LEVEL, 0, 0
    current_floor = xp_for_level(lvl)
    next_floor    = xp_for_level(lvl + 1)
    return lvl, next_floor - current_floor, xp - current_floor


# ══════════════════════════════════════════════════════════════════════════════
#  Beyblade helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_beyblades() -> dict:
    """Return the full beyblades registry as a dict keyed by name."""
    with open(BEYBLADES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_beyblade(name: str) -> Optional[dict]:
    """
    Case-insensitive lookup for a single Beyblade by name.
    Returns a private copy of the beyblade dict, or None if not found.

    Reads through the parse cache and deepcopies only the one record
    (~0.04 ms) instead of re-parsing the whole registry (~1.8 ms) — this is
    called once per item by ;list, ;inventory and the tournament seeder.
    """
    blades = _read_json_cached(BEYBLADES_PATH)
    data = blades.get(name)
    if data is None:
        lowered = name.lower()
        for key, val in blades.items():
            if key.lower() == lowered:
                data = val
                break
    return copy.deepcopy(data) if data is not None else None


# ══════════════════════════════════════════════════════════════════════════════
#  User helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_users() -> dict:
    """Return the full users registry as {uid: profile}.

    Kept for leaderboards / tournament seeding that genuinely need every row.
    Prefer get_user() for single lookups — that is now a single-row SELECT.
    """
    return USER_STORE.load_all()


def all_user_ids() -> list[int]:
    """Every registered player's id — ids only where the backend supports it.

    Falls back to load_all() keys rather than raising if a backend hasn't
    implemented the fast path. The fast path is an optimisation; "who is
    registered" must never depend on which store happens to be active, because
    a backend-specific AttributeError here surfaces as an announcement quietly
    reaching nobody.
    """
    store = USER_STORE
    fast = getattr(store, "all_user_ids", None)
    if callable(fast):
        return fast()
    log.warning("[db] %s has no all_user_ids() — falling back to load_all()",
                type(store).__name__)
    out: list[int] = []
    for uid in store.load_all().keys():
        try:
            out.append(int(uid))
        except (TypeError, ValueError):
            continue
    return out


def save_users(data: dict) -> None:
    """Bulk-persist a registry dict. Upserts only — it never deletes rows."""
    USER_STORE.save_all(data)


def _default_profile(user_id: str) -> dict:
    return {
        "user_id":          user_id,
        "active_beyblade":  None,
        "inventory":        [],
        "wins":             0,
        "losses":           0,
        "xp":               0,
        "level":            0,   # Bug #2 fix: level_from_xp(0) == 0, not 1
        "coins":            0,
        "rank_score":       0,
        "parts":            [],
        "last_daily":       None,
        "equipped_avatar":  None,   # Avatar system
        "win_streak":       0,
        "best_streak":      0,
        "quests":           {},
    }


def get_user(user_id: int) -> dict:
    """
    Fetch a user profile by Discord ID (int).
    Auto-creates a blank profile if the user doesn't exist yet.
    Migrates old profiles that are missing xp/level fields.
    """
    with _users_lock:
        uid  = str(user_id)
        prof = USER_STORE.get_one(uid)

        if prof is None:
            prof = _default_profile(uid)
            USER_STORE.put_one(uid, prof)
            return copy.deepcopy(prof)

        changed = False
        for field, default in [
            ("xp",              0),
            ("coins",           0),
            ("rank_score",      0),
            ("parts",           []),
            ("last_daily",      None),
            ("equipped_avatar", None),   # Avatar system migration
            ("win_streak",      0),
            ("best_streak",     0),
            ("quests",          {}),
        ]:
            if field not in prof:
                prof[field] = default
                changed = True

        # Level is DERIVED from xp — it is never an independent value.
        # Recalculating only when the key was missing left profiles whose
        # xp was written by a path that didn't call grant_xp (imports,
        # admin grants, migrations) permanently desynced: the profile card
        # computes level from xp and showed L34 while get_stat_multiplier
        # read the stored L0 and gave them no level bonus in battle.
        # Self-heal on every read so both sides always agree.
        _true_level = level_from_xp(prof.get("xp", 0))
        if prof.get("level") != _true_level:
            prof["level"] = _true_level
            changed = True

        if changed:
            USER_STORE.put_one(uid, prof, touch=False)

        return copy.deepcopy(prof)


def update_user(user_id: int, profile: dict) -> None:
    """Write a single user's profile back to disk (single-row upsert)."""
    with _users_lock:
        USER_STORE.put_one(str(user_id), profile)


def mutate_user(user_id: int, fn):
    """Read-modify-write one profile atomically. Returns whatever `fn` returns.

    `get_user()` then `update_user()` is a race: the profile can change between
    the two calls and the write silently discards the change. That is tolerable
    for a coin reward — worst case somebody is paid twice — and NOT tolerable
    for a purchase, where the two halves are "take the coins" and "grant the
    thing". A crash or a concurrent write between them is the one bug in a
    shop that cannot be reconstructed afterwards without logs.

    `fn(profile)` receives the live profile and mutates it in place. It must not
    call get_user/update_user itself — `_users_lock` is a plain Lock, so a
    nested call deadlocks. Raising from `fn` abandons the write entirely, which
    is the desired behaviour for "you cannot afford this".
    """
    with _users_lock:
        uid = str(user_id)
        prof = USER_STORE.get_one(uid)
        if prof is None:
            prof = _default_profile(uid)
        result = fn(prof)
        USER_STORE.put_one(uid, prof)
        return result


def touch_user(user_id: int) -> None:
    """Record activity without rewriting the profile blob."""
    prof = USER_STORE.get_one(str(user_id))
    if prof is not None:
        USER_STORE.put_one(str(user_id), prof)


def user_exists(user_id: int) -> bool:
    """True if this ID already has a row (does NOT create one)."""
    return USER_STORE.has(str(user_id))


def grant_xp(user_id: int, xp_amount: int) -> tuple[int, int, bool]:
    """
    Add xp_amount XP to a user and recalculate their level.
    Returns (new_level, total_xp, leveled_up: bool).
    """
    with _users_lock:
        uid       = str(user_id)
        profile   = USER_STORE.get_one(uid) or _default_profile(uid)
        old_level = profile.get("level", 0)
        new_xp    = profile.get("xp", 0) + xp_amount
        new_level = level_from_xp(new_xp)

        profile["xp"]    = new_xp
        profile["level"] = new_level
        USER_STORE.put_one(uid, profile)

    return new_level, new_xp, (new_level > old_level)


def get_stat_multiplier(user_id: int, blade_name: Optional[str] = None) -> float:
    """
    Returns a damage/stat multiplier based on trainer level.
    Every 10 levels adds +2% (e.g. Lv50 → ×1.10, Lv100 → ×1.20).

    When `blade_name` is given, that blade's mastery bonus is added on top
    (+0.5% per mastery level, +5% at Mastery 10). Callers that don't pass a
    blade get the old trainer-level-only behaviour, so this stays backwards
    compatible with every existing call site.
    """
    profile = get_user(user_id)
    level   = profile.get("level", 1)
    bonus   = (level // 10) * STAT_BONUS_PER_10

    if blade_name:
        try:
            from cogs.extras.mastery import MASTERY_BONUS_PER_LEVEL, level_from_xp
            entry = (profile.get("mastery") or {}).get(blade_name) or {}
            bonus += level_from_xp(entry.get("xp", 0)) * MASTERY_BONUS_PER_LEVEL
        except Exception:
            pass

    return 1.0 + bonus


def add_beyblade_to_inventory(user_id: int, beyblade_name: str) -> bool:
    """
    Append a Beyblade to a user's inventory.
    If the user has no active Beyblade, automatically equip this one.
    Always adds the Beyblade, even if the user already owns a copy (duplicates allowed).
    Returns True on success.
    """
    with _users_lock:
        uid     = str(user_id)
        profile = USER_STORE.get_one(uid) or _default_profile(uid)
        profile.setdefault("inventory", []).append(beyblade_name)
        if profile.get("active_beyblade") is None:
            profile["active_beyblade"] = beyblade_name
        USER_STORE.put_one(uid, profile)
    return True


def set_active_beyblade(user_id: int, beyblade_name: str) -> bool:
    """
    Set the user's active Beyblade.
    Returns True on success, False if the blade isn't in their inventory.
    """
    with _users_lock:
        uid     = str(user_id)
        profile = USER_STORE.get_one(uid)
        if profile is None:
            return False
        if beyblade_name not in (profile.get("inventory") or []):
            return False
        profile["active_beyblade"] = beyblade_name
        # Equipping a database blade takes the copy off. Without this the copy
        # pointer outlives the swap, so ";equip <blade>" would report success
        # while every battle still resolved to the boss copy.
        profile["active_copy"] = None
        USER_STORE.put_one(uid, profile)
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  Guild config helpers  (spawn channel, etc.)
# ══════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    """Load guild config from config.json. Returns {} if file doesn't exist yet."""
    return _read_json(CONFIG_PATH)


def save_config(data: dict) -> None:
    _atomic_write_json(CONFIG_PATH, data)


def get_spawn_channel(guild_id: int) -> Optional[int]:
    """Return the configured spawn channel ID for a guild, or None if not set."""
    with _config_lock:
        cfg = load_config()
    return cfg.get(str(guild_id), {}).get("spawn_channel_id")


def set_spawn_channel(guild_id: int, channel_id: Optional[int]) -> None:
    """Save (or clear) the spawn channel for a guild."""
    with _config_lock:
        cfg = load_config()
        key = str(guild_id)
        if key not in cfg:
            cfg[key] = {}
        if channel_id is None:
            cfg[key].pop("spawn_channel_id", None)
        else:
            cfg[key]["spawn_channel_id"] = channel_id
        save_config(cfg)


# ══════════════════════════════════════════════════════════════════════════════
#  Spawn state helpers
#  spawn_state.json schema:
#  {
#    "<guild_id>": {
#      "counter": int,
#      "target":  int,
#      "active":  [{"bey": {...}, "channel_id": int, "spawned_at": float}, ...]
#    }
#  }
# ══════════════════════════════════════════════════════════════════════════════

def _load_spawn_file() -> dict:
    return _read_json(SPAWN_PATH)


def _save_spawn_file(data: dict) -> None:
    _atomic_write_json(SPAWN_PATH, data)


def load_spawn_state(guild_id: int) -> Optional[dict]:
    """
    Returns {"counter": int, "target": int} for the guild, or None if not set.
    """
    with _spawn_lock:
        data = _load_spawn_file()
    entry = data.get(str(guild_id))
    if not entry:
        return None
    return {"counter": entry.get("counter", 0), "target": entry.get("target", 18)}


def save_spawn_state(guild_id: int, counter: int, target: int) -> None:
    """Upsert counter + target for the guild."""
    with _spawn_lock:
        data = _load_spawn_file()
        key  = str(guild_id)
        if key not in data:
            data[key] = {}
        data[key]["counter"] = counter
        data[key]["target"]  = target
        _save_spawn_file(data)


def load_active_spawns(guild_id: int) -> list:
    """
    Returns list of {"bey": {...}, "channel_id": int, "spawned_at": float}.
    Returns [] if none stored.
    """
    with _spawn_lock:
        data = _load_spawn_file()
    return data.get(str(guild_id), {}).get("active", [])


def save_active_spawn(guild_id: int, channel_id: int, bey_data: dict, spawned_at: float) -> None:
    """Upsert one active spawn for (guild_id, channel_id). Replaces any existing entry for that channel."""
    with _spawn_lock:
        data = _load_spawn_file()
        key  = str(guild_id)
        if key not in data:
            data[key] = {}
        active = data[key].get("active", [])
        # Remove any existing entry for this channel before inserting
        active = [s for s in active if s.get("channel_id") != channel_id]
        active.append({"bey": bey_data, "channel_id": channel_id, "spawned_at": spawned_at})
        data[key]["active"] = active
        _save_spawn_file(data)


def clear_active_spawn(guild_id: int, channel_id: int) -> None:
    """Remove the active spawn entry for (guild_id, channel_id)."""
    with _spawn_lock:
        data = _load_spawn_file()
        key  = str(guild_id)
        if key not in data:
            return
        data[key]["active"] = [
            s for s in data[key].get("active", [])
            if s.get("channel_id") != channel_id
        ]
        _save_spawn_file(data)



# ══════════════════════════════════════════════════════════════════════════════
#  Avatar inventory helpers
#  avatar_inventory.json schema:
#  {
#    "<user_id>": ["avatar_001", "avatar_r003", ...]
#  }
# ══════════════════════════════════════════════════════════════════════════════

def _load_avatar_file() -> dict:
    return _read_json(AVATARS_PATH)


def _save_avatar_file(data: dict) -> None:
    _atomic_write_json(AVATARS_PATH, data)


def get_avatar_inventory(user_id: int) -> list[str]:
    """Return list of avatar IDs owned by this user."""
    with _avatar_lock:
        data = _load_avatar_file()
    return data.get(str(user_id), [])


def add_avatar_to_inventory(user_id: int, avatar_id: str) -> bool:
    """
    Add an avatar to the user's collection.
    Returns True if added, False if already owned (duplicate).
    """
    with _avatar_lock:
        data = _load_avatar_file()
        uid  = str(user_id)
        owned = data.get(uid, [])
        if avatar_id in owned:
            return False
        owned.append(avatar_id)
        data[uid] = owned
        _save_avatar_file(data)
    return True


def player_owns_avatar(user_id: int, avatar_id: str) -> bool:
    """Check if a user owns a specific avatar."""
    return avatar_id in get_avatar_inventory(user_id)


def get_equipped_avatar(user_id: int) -> Optional[str]:
    """Return the equipped avatar ID for a user, or None."""
    profile = get_user(user_id)
    return profile.get("equipped_avatar")


def set_equipped_avatar(user_id: int, avatar_id: Optional[str]) -> None:
    """Set or clear the equipped avatar for a user."""
    with _users_lock:
        uid     = str(user_id)
        profile = USER_STORE.get_one(uid) or _default_profile(uid)
        profile["equipped_avatar"] = avatar_id
        USER_STORE.put_one(uid, profile)
