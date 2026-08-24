"""
cogs/community/config.py — typed settings over the `community_config` table.

Why a table and not `data/config.json`, where the announce and report channels
live: `config.json` is a local file. On the MySQL deployment — which exists so
data survives a container rebuild — only the `users` table survives, because
`mysql_store.kv_get`/`kv_put` have no runtime readers. Losing the announce
channel to a rebuild is an annoyance. Losing MAIN_GUILD silently switches the
entire community layer off in a way that looks exactly like a bug.

Reads are cached for a few seconds because `main_guild_id()` is consulted on
every message the bot sees.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

from . import store as S

log = logging.getLogger("beyblade_bot.community")

CACHE_TTL = 5.0

# ── Keys ─────────────────────────────────────────────────────────────────────
K_MAIN_GUILD     = "main_guild_id"
# The authority for both of these is `personality.PERSONALITIES` and
# `chat.BANTER` — the names are not repeated here, because a comment listing
# five values is a sixth place for them to drift.
K_PERSONALITY    = "personality"
K_BANTER         = "banter_intensity"     # see chat.BANTER
# `K_ROAST_MAX` was declared here in v1.28 and never read by anything. It is
# gone rather than wired up: the personality picker already IS the sharpness
# dial (SAVAGE vs FRIENDLY), and a second one that only modifies a single
# personality would be a setting to explain, test and keep honest for no
# behaviour the owner cannot already get.
K_ANNOUNCE       = "level_channel_id"
K_LEVEL_ROLES    = "level_roles"          # {"10": role_id, ...}
K_XP_ENABLED     = "xp_enabled"
K_POLL_STAFF_ONLY = "poll_staff_only"
K_BANTER_DENY    = "banter_deny_channels"

DEFAULTS: dict[str, Any] = {
    K_MAIN_GUILD:  None,
    K_PERSONALITY: "FRIENDLY",
    K_BANTER:      "OFF",   # ships OFF: merging must not make a live server chatty
    K_ANNOUNCE:    None,
    K_LEVEL_ROLES: {},
    K_XP_ENABLED:  True,
    K_POLL_STAFF_ONLY: True,
    K_BANTER_DENY: [],
}

_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


def get(key: str, default: Any = None) -> Any:
    """One setting, JSON-decoded, cached for `CACHE_TTL` seconds."""
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    fallback = DEFAULTS.get(key, default)
    try:
        raw = S.config_get(key)
        value = fallback if raw is None else json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        value = fallback
    except Exception:                                    # noqa: BLE001
        # A store that is down must not take the message path with it. Falling
        # back to the default means the community layer stays OFF, which is the
        # safe direction for a feature that is meant to run in one server.
        log.exception("[community] config read failed for %s", key)
        return fallback
    with _lock:
        _cache[key] = (now, value)
    return value


def put(key: str, value: Any) -> None:
    S.config_put(key, json.dumps(value))
    with _lock:
        _cache[key] = (time.time(), value)


def delete(key: str) -> None:
    S.config_delete(key)
    with _lock:
        _cache.pop(key, None)


def invalidate() -> None:
    """Drop the cache. For tests, and for anything that writes behind us."""
    with _lock:
        _cache.clear()


def snapshot() -> dict:
    """Every setting with its current value — what the panel renders."""
    return {k: get(k) for k in DEFAULTS}


# ── Typed accessors used on hot paths ────────────────────────────────────────
def main_guild_id() -> Optional[int]:
    raw = get(K_MAIN_GUILD)
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def level_roles() -> dict[int, int]:
    raw = get(K_LEVEL_ROLES) or {}
    out: dict[int, int] = {}
    for level, role in (raw.items() if isinstance(raw, dict) else []):
        try:
            out[int(level)] = int(role)
        except (TypeError, ValueError):
            continue
    return out
