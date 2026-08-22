"""
cogs/updates/store.py — the one place this package talks to the database.

Every method here forwards to `utils.database.USER_STORE`, which is either the
SQLite `UserStore` or the `MySQLStore` depending on what the install has. Both
carry the same 24 notification methods under identical names, so nothing above
this file ever asks which backend is live.

Going through a module rather than importing `USER_STORE` directly matters for
one reason: `utils.database` REBINDS that name at import time when MySQL comes
up (`_init_mysql`). A caller that did `from utils.database import USER_STORE`
at module scope would capture whichever store existed first and keep writing to
it forever. `_store()` reads the attribute each time, so the swap is honoured.
"""

from __future__ import annotations

import secrets
import time
from typing import Optional


def _store():
    """The live store. Read fresh every call — see the module docstring."""
    from utils import database as DB
    return DB.USER_STORE


def new_id(prefix: str) -> str:
    """A short, sortable, collision-free id. `upd_1a2b3c_9f4e`."""
    return f"{prefix}_{int(time.time()):x}_{secrets.token_hex(3)}"


# ── Updates ───────────────────────────────────────────────────────────────────
def put_update(row: dict) -> None:
    _store().updates_put(row)


def get_update(update_id: str) -> Optional[dict]:
    return _store().updates_get(update_id)


def list_updates(limit: int = 20) -> list[dict]:
    return _store().updates_list(limit)


def set_update_state(update_id: str, state: str) -> None:
    _store().updates_set_state(update_id, state)


# ── The delivery ledger ───────────────────────────────────────────────────────
def enqueue(update_id: str, user_ids) -> int:
    """Queue an update for these users. Returns how many rows were NEW.

    Duplicate prevention is the composite primary key, not this function: a
    pair that is already queued is ignored by the database.
    """
    return _store().deliveries_enqueue(update_id, user_ids)


def claim(update_id: str, limit: int = 25) -> list[str]:
    return _store().deliveries_claim(update_id, limit)


def mark(update_id: str, user_id: str, state: str,
         error: Optional[str] = None, bump_attempt: bool = True) -> None:
    _store().deliveries_mark(update_id, user_id, state, error, bump_attempt)


def counts(update_id: str) -> dict:
    return _store().deliveries_counts(update_id)


def attempts(update_id: str, user_id: str) -> int:
    return _store().deliveries_attempts(update_id, user_id)


def reset_stuck() -> int:
    return _store().deliveries_reset_stuck()


def drop_pending(update_id: str) -> int:
    return _store().deliveries_drop_pending(update_id)


def pending_updates() -> list[str]:
    return _store().deliveries_pending_updates()


# ── Reports ───────────────────────────────────────────────────────────────────
def put_report(row: dict) -> None:
    _store().reports_put(row)


def get_report(report_id: str) -> Optional[dict]:
    return _store().reports_get(report_id)


def set_report_status(report_id: str, status: str,
                      handled_by: Optional[str] = None) -> None:
    _store().reports_set_status(report_id, status, handled_by)


def set_report_field(report_id: str, field: str, value) -> None:
    _store().reports_set_field(report_id, field, value)


def open_reports(limit: int = 10) -> list[dict]:
    return _store().reports_open(limit)


def reports_since(user_id, cutoff: float) -> int:
    return _store().reports_count_since(str(user_id), cutoff)


def last_report_at(user_id) -> Optional[float]:
    return _store().reports_last_at(str(user_id))
