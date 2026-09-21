#!/usr/bin/env python3
"""Focused safety checks for the owner-only cache/temp cleanup action."""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.admin import actions as admin_actions  # noqa: E402
from utils import image_generator, profile_card, tournament_card  # noqa: E402
from utils.cleanup import clear_memory_caches, purge_disk_junk  # noqa: E402


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print(f"ok  {label}")


with tempfile.TemporaryDirectory(prefix="beycord-cleanup-") as folder:
    root = Path(folder)
    old = time.time() - (25 * 60 * 60)

    pycache = root / "utils" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "old.pyc").write_bytes(b"compiled")

    tool_cache = root / ".pytest_cache"
    tool_cache.mkdir()
    (tool_cache / "state").write_bytes(b"cache")

    stale_tmp = root / "data" / "users.json.tmp"
    stale_tmp.parent.mkdir()
    stale_tmp.write_text("stale", encoding="utf-8")
    os.utime(stale_tmp, (old, old))

    fresh_tmp = root / "data" / "config.json.tmp"
    fresh_tmp.write_text("active", encoding="utf-8")

    database = root / "data" / "users.db"
    database.write_bytes(b"important")
    asset = root / "assets" / "beys" / "blade.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"art")
    backup_tmp = root / "backups" / "players.tmp"
    backup_tmp.parent.mkdir()
    backup_tmp.write_bytes(b"backup")
    os.utime(backup_tmp, (old, old))

    report = purge_disk_junk(str(root), now=time.time())

    check("compiled cache removed", not pycache.exists())
    check("tool cache removed", not tool_cache.exists())
    check("stale temp removed", not stale_tmp.exists())
    check("fresh temp preserved", fresh_tmp.exists())
    check("database preserved", database.read_bytes() == b"important")
    check("assets preserved", asset.read_bytes() == b"art")
    check("backups preserved", backup_tmp.read_bytes() == b"backup")
    check("report counts removed items", (
        report.pycache_dirs, report.tool_cache_dirs, report.stale_temp_files
    ) == (1, 1, 1))

image_generator._art_cache["probe"] = None
profile_card._AVATAR_CACHE["probe"] = object()
tournament_card._bg_cache[123] = object()
groups = clear_memory_caches()
check("all render cache groups invoked", groups == 4)
check("battle art cache reset", not image_generator._art_cache)
check("profile avatar cache reset", not profile_card._AVATAR_CACHE)
check("tournament background cache reset", not tournament_card._bg_cache)

action = admin_actions.REGISTRY.get("cleanup")
check("cleanup is exposed in the System panel", action is not None and action.category == "system")
check("cleanup remains owner-only", action is not None and action.owner_only)
check("cleanup requires confirmation", action is not None and bool(action.confirm))

print("cleanup safety checks passed")
