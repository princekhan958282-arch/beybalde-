"""Runtime selector for Beycord's info-card designs.

V2 is the default.  The exact pre-redesign implementation is preserved in
``info_card_legacy.py`` and can be restored instantly by setting
``data/config.json`` -> ``{"info_card_version": "legacy"}`` or the environment
variable ``BEYCORD_INFO_CARD_VERSION=legacy``.  The config file is read for
every render, so changing it does not require a code revert.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from . import info_card_legacy as legacy
from . import info_card_v2 as v2

CARD_ENABLED = True

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG = os.path.join(_ROOT, "data", "config.json")


def selected_version() -> str:
    env = os.getenv("BEYCORD_INFO_CARD_VERSION", "").strip().lower()
    if env:
        return env
    try:
        with open(_CONFIG, "r", encoding="utf-8") as fh:
            cfg = json.load(fh) or {}
        return str(cfg.get("info_card_version", "v2")).strip().lower()
    except Exception:
        return "v2"


def using_v2() -> bool:
    return selected_version() not in {"legacy", "old", "v1", "1"}


async def render_info_card(blade: dict, parts: Optional[dict] = None):
    if not CARD_ENABLED:
        return None
    if using_v2():
        return await v2.render_info_card(blade, parts=parts)
    return await legacy.render_info_card(blade, parts=parts)


def clear_cache() -> None:
    try:
        v2.clear_cache()
    except Exception:
        pass
    try:
        legacy.clear_cache()
    except Exception:
        pass


def refresh_art_index() -> None:
    try:
        v2.refresh_art_index()
    except Exception:
        pass
    try:
        legacy.refresh_art_index()
    except Exception:
        pass


async def shutdown() -> None:
    # V2 is Pillow-only.  Legacy owns the shared Chromium browser used by the
    # old info card and by boss_card.py, so its shutdown remains authoritative.
    try:
        await legacy.shutdown()
    except Exception:
        pass


# Preserve the old helper/API surface.  Several existing renderers and smoke
# tests import these from utils.info_card even though the visible ;info renderer
# is now V2.
_named = legacy._named
card_filename = legacy.card_filename
build_html = legacy.build_html
theme_for = legacy.theme_for
_TYPE_LABEL = legacy._TYPE_LABEL
_SPIN_ICON = legacy._SPIN_ICON
_collect_abilities = legacy._collect_abilities
_chip_for = legacy._chip_for
_stat_rows = legacy._stat_rows
_stat_total = legacy._stat_total
_parts_slots = legacy._parts_slots
_get_context = legacy._get_context


def __getattr__(name: str):
    # Mutable browser state (_browser, _browser_unavailable, etc.) must be read
    # live from legacy rather than copied once at import time.
    return getattr(legacy, name)
