"""Safe, bounded cleanup for the owner-only admin panel action.

This deliberately uses an allow-list.  Player data, assets, backups, logs,
database files, and updater rollback files are never cleanup candidates.
"""

from __future__ import annotations

import importlib
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .database import BASE_DIR
from .updater import purge_pycache


log = logging.getLogger("beyblade_bot.cleanup")


_CACHE_DIRS = {".pytest_cache", ".ruff_cache", ".mypy_cache"}
_SKIP_DIRS = {
    ".git", ".update_backup", "assets", "backups", "logs", "node_modules",
    "venv", ".venv", "env", "site-packages",
}
_STALE_TMP_AGE = 24 * 60 * 60


@dataclass(frozen=True)
class CleanupReport:
    pycache_dirs: int = 0
    tool_cache_dirs: int = 0
    stale_temp_files: int = 0
    bytes_freed: int = 0
    memory_cache_groups: int = 0
    skipped: int = 0


def _tree_size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def _inside(path: Path, base: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(base.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def purge_disk_junk(root: Optional[str] = None, *, now: Optional[float] = None,
                    stale_tmp_age: int = _STALE_TMP_AGE) -> CleanupReport:
    """Remove only known disposable files below *root*.

    ``*.tmp`` files are eligible only after 24 hours by default.  Atomic JSON
    writers use that suffix, so the age gate prevents cleanup from racing an
    active write while still removing leftovers from an interrupted deploy.
    Symlinks are never followed or removed.
    """
    base = Path(root or BASE_DIR).resolve(strict=True)
    timestamp = time.time() if now is None else float(now)
    pycache_dirs, pycache_bytes = purge_pycache(str(base))
    cache_dirs = temp_files = freed = skipped = 0

    for dirpath, dirnames, filenames in os.walk(base, topdown=True):
        current = Path(dirpath)
        dirnames[:] = [
            name for name in dirnames
            if name not in _SKIP_DIRS and name != "__pycache__"
        ]

        for name in list(dirnames):
            if name not in _CACHE_DIRS:
                continue
            target = current / name
            dirnames.remove(name)
            if target.is_symlink() or not _inside(target, base):
                skipped += 1
                continue
            size = _tree_size(target)
            try:
                shutil.rmtree(target)
                cache_dirs += 1
                freed += size
            except OSError:
                skipped += 1

        for name in filenames:
            if not name.endswith(".tmp"):
                continue
            target = current / name
            try:
                stat = target.stat(follow_symlinks=False)
                if target.is_symlink() or not target.is_file() or not _inside(target, base):
                    skipped += 1
                    continue
                if timestamp - stat.st_mtime < max(0, stale_tmp_age):
                    skipped += 1
                    continue
                target.unlink()
                temp_files += 1
                freed += stat.st_size
            except OSError:
                skipped += 1

    return CleanupReport(
        pycache_dirs=pycache_dirs,
        tool_cache_dirs=cache_dirs,
        stale_temp_files=temp_files,
        bytes_freed=pycache_bytes + freed,
        skipped=skipped,
    )


def clear_memory_caches() -> int:
    """Clear rebuildable image/card caches and return the groups cleared."""
    cleared = 0
    for name in ("utils.info_card_router", "utils.image_generator",
                 "utils.profile_card", "utils.tournament_card"):
        try:
            module = importlib.import_module(name)
            clear = getattr(module, "clear_cache", None)
            if callable(clear):
                clear()
                cleared += 1
        except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
            log.warning("[cleanup] could not reset %s: %s", name, exc)
    return cleared


def cleanup(root: Optional[str] = None) -> CleanupReport:
    disk = purge_disk_junk(root)
    return CleanupReport(
        pycache_dirs=disk.pycache_dirs,
        tool_cache_dirs=disk.tool_cache_dirs,
        stale_temp_files=disk.stale_temp_files,
        bytes_freed=disk.bytes_freed,
        memory_cache_groups=clear_memory_caches(),
        skipped=disk.skipped,
    )
