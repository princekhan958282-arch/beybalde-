"""Persistent state for Beycord Horror Story encounters."""

from __future__ import annotations

import json
import os
import threading
import time

from utils.database import BASE_DIR

STATE_PATH = os.path.join(BASE_DIR, "data", "horror_state.json")
_LOCK = threading.Lock()


def _blank() -> dict:
    return {"curses": {}, "encounters": {}}


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
    return data


def _write(data: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_PATH)


def curse_multiplier(user_id: int) -> float:
    """Return the active combat multiplier. Normal=1.0, Horror curse=0.8."""
    with _LOCK:
        row = _read()["curses"].get(str(int(user_id)), {})
    if not row.get("active"):
        return 1.0
    try:
        return float(row.get("multiplier", 0.8))
    except (TypeError, ValueError):
        return 0.8


def is_cursed(user_id: int) -> bool:
    return curse_multiplier(user_id) < 1.0


def apply_curse(user_id: int, *, source: str = "unknown_challenger") -> None:
    with _LOCK:
        data = _read()
        data["curses"][str(int(user_id))] = {
            "active": True,
            "multiplier": 0.8,
            "source": source,
            "applied_at": int(time.time()),
        }
        _write(data)


def clear_curse(user_id: int) -> None:
    with _LOCK:
        data = _read()
        row = data["curses"].setdefault(str(int(user_id)), {})
        row["active"] = False
        row["cleared_at"] = int(time.time())
        _write(data)


def save_encounter(user_id: int, **fields) -> dict:
    key = str(int(user_id))
    with _LOCK:
        data = _read()
        row = dict(data["encounters"].get(key, {}))
        row.update(fields)
        row["updated_at"] = int(time.time())
        data["encounters"][key] = row
        _write(data)
    return row


def encounter(user_id: int) -> dict:
    with _LOCK:
        return dict(_read()["encounters"].get(str(int(user_id)), {}))
