"""Persistent state for Beycord Horror Story encounters."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta

from utils.database import BASE_DIR

STATE_PATH = os.path.join(BASE_DIR, "data", "horror_state.json")
_LOCK = threading.Lock()
HORROR_EFFECT_SECONDS = 24 * 60 * 60
ENCOUNTER_STALE_SECONDS = 24 * 60 * 60
IST = timezone(timedelta(hours=5, minutes=30))


def _blank() -> dict:
    return {"curses": {}, "encounters": {}, "taken": {}, "guild_daily_spawns": {}}


def _read() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return _blank()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _blank()
    data.setdefault("curses", {})
    data.setdefault("encounters", {})
    data.setdefault("taken", {})
    data.setdefault("guild_daily_spawns", {})
    return data


def _write(data: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_PATH)


def _ist_day(now: float | None = None) -> str:
    stamp = time.time() if now is None else float(now)
    return datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(IST).date().isoformat()


def guild_spawned_today(guild_id: int, now: float | None = None) -> bool:
    """True after this guild has consumed its one UNKNOWN spawn for the IST day."""
    day = _ist_day(now)
    with _LOCK:
        row = _read()["guild_daily_spawns"].get(str(int(guild_id)), {})
        return row.get("ist_day") == day


def claim_guild_daily_spawn(guild_id: int, user_id: int, now: float | None = None) -> bool:
    """Atomically reserve a guild's one UNKNOWN encounter until 00:00 IST."""
    stamp = time.time() if now is None else float(now)
    day = _ist_day(stamp)
    with _LOCK:
        data = _read()
        key = str(int(guild_id))
        row = data["guild_daily_spawns"].get(key, {})
        if row.get("ist_day") == day:
            return False
        data["guild_daily_spawns"][key] = {
            "ist_day": day,
            "target_user_id": int(user_id),
            "claimed_at": int(stamp),
        }
        _write(data)
        return True


def curse_multiplier(user_id: int) -> float:
    key = str(int(user_id)); now = int(time.time())
    with _LOCK:
        data = _read(); row = data["curses"].get(key, {})
        if row.get("active") and int(row.get("expires_at") or 0) <= now:
            row["active"] = False; row["cleared_at"] = now; data["curses"][key] = row; _write(data)
        active = bool(row.get("active"))
    if not active: return 1.0
    try: return float(row.get("multiplier", 0.8))
    except (TypeError, ValueError): return 0.8


def is_cursed(user_id: int) -> bool:
    return curse_multiplier(user_id) < 1.0


def apply_curse(user_id: int, *, source: str = "unknown_challenger", duration: int = HORROR_EFFECT_SECONDS) -> dict:
    now = int(time.time())
    row = {"active": True, "multiplier": 0.8, "source": source, "applied_at": now, "expires_at": now + int(duration)}
    with _LOCK:
        data = _read(); data["curses"][str(int(user_id))] = row; _write(data)
    return dict(row)


def clear_curse(user_id: int) -> None:
    with _LOCK:
        data = _read(); row = data["curses"].setdefault(str(int(user_id)), {})
        row["active"] = False; row["cleared_at"] = int(time.time()); _write(data)


def record_taken(user_id: int, *, kind: str, amount=1, value=None, source: str = "horror") -> str:
    now = int(time.time()); token = f"{int(user_id)}:{now}:{time.time_ns()}"
    with _LOCK:
        data = _read()
        data["taken"][token] = {"user_id": int(user_id), "kind": str(kind), "amount": amount, "value": value, "source": source, "taken_at": now, "restore_at": now + HORROR_EFFECT_SECONDS, "returned": False}
        _write(data)
    return token


def due_restorations(now: int | None = None) -> list[tuple[str, dict]]:
    now = int(now or time.time())
    with _LOCK:
        rows = _read()["taken"]
        return [(token, dict(row)) for token, row in rows.items() if not row.get("returned") and int(row.get("restore_at") or 0) <= now]


def mark_returned(token: str) -> bool:
    with _LOCK:
        data = _read(); row = data["taken"].get(str(token))
        if not row or row.get("returned"): return False
        row["returned"] = True; row["returned_at"] = int(time.time()); _write(data); return True


def save_encounter(user_id: int, **fields) -> dict:
    key = str(int(user_id)); now = int(time.time())
    with _LOCK:
        data = _read(); row = dict(data["encounters"].get(key, {})); row.update(fields); row["updated_at"] = now
        if fields.get("status") == "completed" and fields.get("bey_claimed") and not row.get("restoration_token"):
            token = f"{int(user_id)}:{now}:{time.time_ns()}"
            value = {"name": fields.get("claimed_bey") or row.get("equipped_bey"), "copy_id": row.get("equipped_copy_id")}
            data["taken"][token] = {"user_id": int(user_id), "kind": "bey", "amount": 1, "value": value, "source": "unknown_battle", "taken_at": now, "restore_at": now + HORROR_EFFECT_SECONDS, "returned": False}
            row["restoration_token"] = token
        data["encounters"][key] = row; _write(data)
    return row


def encounter(user_id: int) -> dict:
    with _LOCK:
        data = _read(); key = str(int(user_id)); row = dict(data["encounters"].get(key, {}))
        if row.get("status") in {"spawned", "declined_once", "battle_requested", "battle_failed"} and int(row.get("updated_at") or 0) + ENCOUNTER_STALE_SECONDS <= int(time.time()):
            row["status"] = "expired"; row["expired_at"] = int(time.time()); data["encounters"][key] = row; _write(data)
        return row
