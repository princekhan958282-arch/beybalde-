"""
utils/snapshot.py — point-in-time copies of everything a player owns.

Why this exists
---------------
"Back up the profiles" sounds like one table. It is not. Player data on this
install is spread across the user store AND several JSON side files, and the
one people reach for first — the `users` table — does not contain avatars:

    users table   beys, bey_progress, parts, coins, ranked, story,
                  community XP and level                 (SQLite or MySQL)
    avatar_inventory.json   which avatars a player OWNS  <- not in the profile
    casino_wallets.json     casino balances
    clans.json / clan_wars.json
    backup_codes.json       the recovery codes in cogs/codes/backup.py

A snapshot that dumped only the table would restore a player with their whole
collection and none of their avatars, and nothing would report an error. So the
file list below is the feature, and `tools/sim_snapshots.py` asserts avatars
survive a wipe-and-restore specifically.

Three properties worth stating
------------------------------
**Secrets cannot end up in here.** `collect()` names the files it takes.
`.env` and `config_local.py` are not on that list, and the suite asserts no
snapshot contains them or anything token-shaped. This is structural rather
than a filter that could be got round.

**It follows the live backend.** `DB.USER_STORE` is rebound at import by
`_init_mysql`, so this reads it fresh on every call — snapshot MySQL when MySQL
is live, SQLite otherwise. Caching the reference would quietly back up an empty
local file on the deployment that most needs backing up.

**Writes are atomic.** tmp + `os.replace`, so a crash mid-write cannot leave a
half-written file that looks restorable. A backup you cannot trust is worse
than none, because you stop taking the other kind.

Measured on the real 3,410-profile store: 0.87 MB of JSON, 56 KB gzipped,
0.63 s to collect. Small enough that trimming what goes in would buy nothing
and cost a restore its completeness.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

log = logging.getLogger("beyblade_bot.snapshot")

FORMAT = 2                      # bump when the shape below changes

# Player-owned side files, relative to `data/`. Each is (key, filename).
# Anything added here is backed up AND restored; anything not here is not.
SIDE_FILES = (
    ("avatars",       "avatar_inventory.json"),
    ("casino",        "casino_wallets.json"),
    ("clans",         "clans.json"),
    ("clan_wars",     "clan_wars.json"),
    ("backup_codes",  "backup_codes.json"),
)

# Never collected, at any depth. Listed so the intent is greppable — the real
# guarantee is that `collect()` only ever reads the names above.
NEVER = (".env", "config_local.py", "config.json", "beyblades.json")

DAILY_DIR = "daily"
PRE_RESTORE_DIR = "pre-restore"
LATEST = "latest.json.gz"
KEEP_DAILY = 14

# ── Sections: what a restore may put back without disturbing the rest ────────
SECTIONS: dict[str, tuple[str, ...]] = {
    "beys": ("inventory", "bey_progress", "active_beyblade", "boss_copies",
             "active_copy", "equipped_parts", "parts", "mastery"),
    "avatars": ("equipped_avatar",),        # plus the avatars side file
    "community": ("community_xp", "com_level", "com_day", "com_last_msg",
                  "com_recent_hashes"),
    "economy": ("coins", "last_daily"),     # plus the casino side file
    "ranked": ("rank_score", "ranked_wins", "ranked_losses", "best_streak",
               "win_streak", "beys_caught", "ranked_pairs"),
    "story": ("story", "story_progress", "league", "bosses_cleared"),
    "trainer": ("xp", "level", "wins", "losses", "quests", "achievements"),
}
# Which side file each section owns, so restoring `avatars` really does bring
# the avatars back rather than only the equipped pointer.
SECTION_FILES = {"avatars": ("avatars",), "economy": ("casino",),
                 "clans": ("clans", "clan_wars")}
ALL = "everything"


def _store():
    """Read fresh — `_init_mysql` rebinds this at import. See the docstring."""
    from utils import database as DB
    return DB.USER_STORE


def folder() -> str:
    """`backups/` at the install ROOT — deliberately not under `data/`.

    `data/` is the directory that gets lost. A backup living inside the thing
    it protects is not a backup. At the root it also sits in the tree you zip,
    so downloading the bot folder takes the snapshots with it, and
    `utils/updater.py` PROTECTED keeps an auto-update from ever writing here.
    """
    from utils import database as DB
    return os.path.join(os.path.dirname(os.path.dirname(DB.USERS_DB_PATH)),
                        "backups")


def _data_dir() -> str:
    from utils import database as DB
    return os.path.dirname(DB.USERS_DB_PATH)


def stamp(now: Optional[float] = None) -> str:
    ts = time.time() if now is None else float(now)
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _full_stamp(now: Optional[float] = None) -> str:
    ts = time.time() if now is None else float(now)
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


# ══════════════════════════════════════════════════════════════════════════════
#  Collect
# ══════════════════════════════════════════════════════════════════════════════

def collect(now: Optional[float] = None) -> dict:
    """Everything a player owns, as one dict. Never raises on a missing file."""
    from utils import database as DB

    profiles = {}
    try:
        profiles = _store().load_all() or {}
    except Exception:                                    # noqa: BLE001
        log.exception("[snapshot] could not read the user store")

    files: dict[str, object] = {}
    for key, filename in SIDE_FILES:
        path = os.path.join(_data_dir(), filename)
        if not os.path.exists(path):
            continue                     # not every install has every file
        try:
            files[key] = DB._read_json(path)
        except Exception:                                # noqa: BLE001
            log.exception("[snapshot] could not read %s", filename)

    return {
        "format": FORMAT,
        "taken_at": time.time() if now is None else float(now),
        "backend": getattr(__import__("utils.database", fromlist=["x"]),
                           "BACKEND", "sqlite"),
        "profiles": profiles,
        "files": files,
    }


def describe(snap: dict) -> dict:
    """What a snapshot holds — what the panel shows before a restore."""
    profiles = (snap or {}).get("profiles") or {}
    files = (snap or {}).get("files") or {}
    avatars = files.get("avatars") or {}
    beys = sum(len(p.get("inventory") or []) for p in profiles.values()
               if isinstance(p, dict))
    owned = 0
    if isinstance(avatars, dict):
        for entry in avatars.values():
            if isinstance(entry, list):
                owned += len(entry)
            elif isinstance(entry, dict):
                owned += len(entry.get("avatars") or [])
    levelled = sum(1 for p in profiles.values()
                   if isinstance(p, dict) and int(p.get("com_level") or 0) > 0)
    return {
        "taken_at": (snap or {}).get("taken_at", 0),
        "format": (snap or {}).get("format", 0),
        "backend": (snap or {}).get("backend", "?"),
        "profiles": len(profiles),
        "beys": beys,
        "avatars": owned,
        "community_levelled": levelled,
        "files": sorted(files),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Write / read
# ══════════════════════════════════════════════════════════════════════════════

def _atomic_gzip(path: str, payload: bytes) -> None:
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write(folder: str, snap: Optional[dict] = None, *,
          kind: str = DAILY_DIR, now: Optional[float] = None,
          also_latest: bool = True) -> str:
    """Write one snapshot. Returns its path."""
    snap = collect(now) if snap is None else snap
    name = (f"{stamp(now)}.json.gz" if kind == DAILY_DIR
            else f"{_full_stamp(now)}.json.gz")
    path = os.path.join(folder, kind, name)
    payload = gzip.compress(
        json.dumps(snap, separators=(",", ":")).encode("utf-8"), 6)
    _atomic_gzip(path, payload)
    if also_latest:
        _atomic_gzip(os.path.join(folder, LATEST), payload)
    log.info("[snapshot] wrote %s (%s KB, %s profiles)", path,
             len(payload) // 1024, len(snap.get("profiles") or {}))
    return path


def read(path: str) -> dict:
    with gzip.open(path, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))


def listing(folder: str, kind: Optional[str] = None) -> list[dict]:
    """Every snapshot on disk, newest first."""
    out = []
    for sub in ([kind] if kind else [DAILY_DIR, PRE_RESTORE_DIR]):
        base = os.path.join(folder, sub)
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            if not name.endswith(".json.gz"):
                continue
            path = os.path.join(base, name)
            try:
                out.append({"path": path, "name": name, "kind": sub,
                            "size": os.path.getsize(path),
                            "mtime": os.path.getmtime(path)})
            except OSError:
                continue
    # Sorted by NAME first, then mtime. The names are ISO stamps, so
    # lexicographic order is chronological order — and unlike mtime that stays
    # true if a file is ever rewritten, copied or restored from elsewhere.
    # Sorting on mtime alone made "keep the newest 14" mean "keep the 14 most
    # recently touched", which is a different and wrong thing.
    out.sort(key=lambda r: (r["name"], r["mtime"]), reverse=True)
    return out


def prune(folder: str, keep: int = KEEP_DAILY, kind: str = DAILY_DIR) -> int:
    """Delete all but the newest `keep`. Returns how many went."""
    rows = listing(folder, kind)
    gone = 0
    for row in rows[max(0, int(keep)):]:
        try:
            os.remove(row["path"])
            gone += 1
        except OSError:                                  # noqa: PERF203
            log.warning("[snapshot] could not remove %s", row["path"])
    return gone


# ══════════════════════════════════════════════════════════════════════════════
#  Restore
# ══════════════════════════════════════════════════════════════════════════════

def _keys_for(sections: Iterable[str]) -> Optional[set]:
    """The profile keys these sections cover, or None for everything."""
    wanted = {s.strip().lower() for s in sections if str(s).strip()}
    if not wanted or ALL in wanted:
        return None
    keys: set = set()
    for name in wanted:
        keys.update(SECTIONS.get(name, ()))
    return keys


def restore(snap: dict, sections: Iterable[str] = (ALL,)) -> dict:
    """Put a snapshot back. Returns what changed.

    With `everything`, each snapshotted profile replaces the live one wholesale.
    With named sections, ONLY those keys are written onto the live profile —
    so restoring `beys` brings back a collection without rolling back coins
    earned since. A profile in the snapshot that no longer exists is recreated;
    a profile that exists now but not in the snapshot is left alone, because a
    restore is not a way to delete players.
    """
    from utils import database as DB

    profiles = (snap or {}).get("profiles") or {}
    keys = _keys_for(sections)
    wanted = {s.strip().lower() for s in sections if str(s).strip()}
    whole = keys is None

    merged: dict[str, dict] = {}
    touched = 0
    for uid, saved in profiles.items():
        if not isinstance(saved, dict):
            continue
        if whole:
            merged[str(uid)] = saved
            touched += 1
            continue
        live = _store().get_one(str(uid))
        if live is None:
            live = DB._default_profile(str(uid))
        changed = False
        for key in keys:
            if key in saved:
                live[key] = saved[key]
                changed = True
        if changed:
            merged[str(uid)] = live
            touched += 1

    if merged:
        _store().save_all(merged)

    files_written = []
    for key, filename in SIDE_FILES:
        if key not in (snap.get("files") or {}):
            continue
        if not whole:
            owned = {sec for sec, names in SECTION_FILES.items()
                     if key in names}
            if not (owned & wanted):
                continue
        path = os.path.join(_data_dir(), filename)
        try:
            DB._atomic_write_json(path, snap["files"][key])
            files_written.append(filename)
        except Exception:                                # noqa: BLE001
            log.exception("[snapshot] could not restore %s", filename)

    # Cached readers would otherwise serve the pre-restore contents.
    try:
        DB._read_json_cached.cache_clear()               # type: ignore[attr-defined]
    except Exception:                                    # noqa: BLE001
        pass

    log.info("[snapshot] restored %s profile(s) and %s file(s)",
             touched, len(files_written))
    return {"profiles": touched, "files": files_written,
            "sections": sorted(wanted) if not whole else [ALL]}


# ── Used by the suite to prove no secret can ride along ──────────────────────
_TOKEN_SHAPE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27}|mysql://|AIza[0-9A-Za-z_-]{20,})")


def audit_secrets(snap: dict) -> list[str]:
    """Anything in this snapshot that looks like a credential. Should be []."""
    blob = json.dumps(snap)
    found = {m.group(0)[:12] + "…" for m in _TOKEN_SHAPE.finditer(blob)}
    for name in NEVER:
        if name in (snap.get("files") or {}):
            found.add(name)
    return sorted(found)
