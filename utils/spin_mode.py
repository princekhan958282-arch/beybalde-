"""
utils/spin_mode.py — which way a dual-spin blade is mounted.

A dual-spin blade carries TWO complete configurations. Master Diabolos spins
right and hunts, or is flipped over to spin left and endure — different stats,
a different type, and a different Special. The blader chooses, on `;info`, and
the choice sticks.

The shape in beyblades.json
---------------------------
    "dual_spin": true,
    "spin_modes": {
        "Right": {"label": ..., "spin_direction": "Right", "type": "Attack",
                  "stats": {...}, "special_move": {...}},
        "Left":  {...}
    }

The blade's own top-level `stats` / `type` / `special_move` mirror the DEFAULT
mode, so every reader that has never heard of dual spin — the shop, the quiz,
the marketplace, an old card renderer — still sees a complete, valid blade
instead of one with no stats.

Why it lives in utils/
----------------------
`resolve()` has to run in front of BOTH stat paths or the mode is cosmetic:
`loadout.effective_blade` (boss, Story, every card) and `battle._apply_parts`
(PvP). Those are the same two callers `bey_level_and_stats` already serves, and
for the same reason — the alternative is two copies that drift.

Never raises. A blade with no `spin_modes`, an unreadable profile, or a stored
mode that no longer exists all resolve to "leave the blade exactly as it is".
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("beyblade_bot.spin_mode")

# Profile key: {blade_name: mode}
K_MODE = "spin_mode"

DEFAULT_MODE = "Right"


def is_dual(blade: Optional[dict]) -> bool:
    return bool((blade or {}).get("dual_spin")
                and isinstance((blade or {}).get("spin_modes"), dict))


def modes(blade: Optional[dict]) -> list[str]:
    """Mode names in a stable order — the default first."""
    if not is_dual(blade):
        return []
    keys = list(blade["spin_modes"].keys())
    keys.sort(key=lambda k: (k != DEFAULT_MODE, k))
    return keys


def chosen(profile: Optional[dict], blade: Optional[dict]) -> str:
    """The mode this player fights this blade in. Always a mode that exists."""
    available = modes(blade)
    if not available:
        return ""
    try:
        raw = (profile or {}).get(K_MODE) or {}
        want = str(raw.get(blade.get("name"), "")).strip().title()
    except Exception:                                    # noqa: BLE001
        want = ""
    return want if want in available else available[0]


def resolve(profile: Optional[dict], blade: Optional[dict]) -> dict:
    """Return the blade as it actually fights, with the chosen mode applied.

    Non-dual blades are returned untouched — not copied — so the overwhelming
    majority of lookups cost nothing.
    """
    if not is_dual(blade):
        return blade
    mode = chosen(profile, blade)
    cfg = (blade.get("spin_modes") or {}).get(mode)
    if not isinstance(cfg, dict):
        return blade

    out = dict(blade)
    for field in ("stats", "special_move", "spin_direction", "type"):
        if field in cfg:
            out[field] = cfg[field]
    out["active_spin_mode"] = mode
    return out


def label(blade: Optional[dict], mode: str) -> str:
    cfg = ((blade or {}).get("spin_modes") or {}).get(mode) or {}
    return str(cfg.get("label") or f"{mode} Mode")


def set_choice(player_id: int, blade_name: str, mode: str) -> str:
    """Persist a mode choice. Returns the mode stored.

    Swallows database failures: a picker that raises is worse than one that
    silently keeps the old mode, because the caller is a button handler and an
    exception there shows the player nothing at all.
    """
    mode = str(mode).strip().title()

    def _apply(profile: dict) -> str:
        table = profile.get(K_MODE)
        if not isinstance(table, dict):
            table = {}
        table[str(blade_name)] = mode
        profile[K_MODE] = table
        return mode

    try:
        from utils.database import mutate_user
        return mutate_user(int(player_id), _apply) or mode
    except Exception as exc:                             # noqa: BLE001
        log.debug("could not store spin mode for %s: %s", player_id, exc)
        return mode
