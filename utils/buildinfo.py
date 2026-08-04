"""
buildinfo.py — which build is actually running, and is it internally consistent?

Why this exists
---------------
A zip extracted over a live install on a panel can land partially: `cogs/`
updates while `utils/` keeps yesterday's files, or a stale `__pycache__` shadows
a source file. The result is a bot whose halves disagree — and the symptom
surfaces much later as something baffling. It happened concretely: the
announcement composer (new) called `database.all_user_ids()` (old), and an
admin saw

    ⚠️ Couldn't read the player list
    'MySQLStore' object has no attribute 'all_user_ids'

which looks like a database problem and is actually a file that didn't copy.

`selfcheck()` runs at startup and states plainly whether the halves match, so a
half-applied deploy is one line in the log instead of a mystery in a week.

Bump VERSION whenever a build ships.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("beyblade_bot.build")

VERSION = "v64"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mtime(rel: str) -> float:
    try:
        return os.path.getmtime(os.path.join(_ROOT, rel))
    except OSError:
        return 0.0


def store_parity() -> list[str]:
    """Public methods the SQLite store has that the MySQL store doesn't.

    Anything listed here breaks the moment MYSQL_URL is set, because
    database.py calls these on whichever store is active. This is the exact
    class of bug that produced the "no attribute 'all_user_ids'" report.
    """
    try:
        from .userstore import UserStore
        from .mysql_store import MySQLStore
    except Exception as e:                           # noqa: BLE001
        log.debug("[build] parity check skipped: %s", e)
        return []
    a = {m for m in dir(UserStore) if not m.startswith("_")}
    b = {m for m in dir(MySQLStore) if not m.startswith("_")}
    return sorted(a - b)


def stale_pycache() -> list[str]:
    """Compiled files newer than nothing in particular but older than source.

    Python normally invalidates these itself; a zip extract that rewrites
    sources with preserved timestamps can defeat that. Cheap to check, and it
    rules out the other classic cause of "my fix didn't apply".
    """
    bad: list[str] = []
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        if "__pycache__" not in dirpath:
            continue
        src_dir = os.path.dirname(dirpath)
        for f in filenames:
            if not f.endswith(".pyc"):
                continue
            stem = f.split(".")[0]
            src = os.path.join(src_dir, stem + ".py")
            try:
                if os.path.exists(src) and os.path.getmtime(src) > os.path.getmtime(
                        os.path.join(dirpath, f)):
                    bad.append(os.path.relpath(src, _ROOT))
            except OSError:
                continue
    return sorted(set(bad))[:10]


def selfcheck(verbose: bool = True) -> dict:
    """Report on the running build. Never raises — it only ever reports."""
    report: dict = {
        "version": VERSION,
        "python": sys.version.split()[0],
        "parity_gaps": [],
        "stale_pyc": [],
        "discord_version": "",
        "ok": True,
    }
    try:
        import discord
        report["discord_version"] = getattr(discord, "__version__", "?")
    except Exception:                                # noqa: BLE001
        pass

    try:
        report["parity_gaps"] = store_parity()
    except Exception as e:                           # noqa: BLE001
        log.debug("[build] parity check failed: %s", e)
    try:
        report["stale_pyc"] = stale_pycache()
    except Exception as e:                           # noqa: BLE001
        log.debug("[build] pycache check failed: %s", e)

    report["ok"] = not report["parity_gaps"] and not report["stale_pyc"]

    if verbose:
        log.info("[build] Beycord %s · python %s · discord.py %s",
                 report["version"], report["python"],
                 report["discord_version"] or "?")
        if report["parity_gaps"]:
            log.error("[build] STORE MISMATCH — MySQLStore is missing: %s",
                      ", ".join(report["parity_gaps"]))
            log.error("[build] This usually means utils/ didn't update with the "
                      "rest of the build. Re-upload the whole zip.")
        if report["stale_pyc"]:
            log.error("[build] stale __pycache__ for: %s",
                      ", ".join(report["stale_pyc"]))
            log.error("[build] delete every __pycache__ folder and restart.")
        if report["ok"]:
            log.info("[build] self-check passed — all modules agree.")
    return report
