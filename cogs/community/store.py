"""
cogs/community/store.py — the only module in this package that touches the DB.

`DB.USER_STORE` is read fresh on every call and never cached at import time.
`utils.database._init_mysql()` REBINDS that global when MySQL is reachable, so a
module-level `from utils.database import USER_STORE` captures the SQLite store
forever and silently writes to the wrong backend. `cogs/updates/store.py` exists
for the same reason and says the same thing.
"""

from __future__ import annotations

import secrets
import time
from typing import Optional


def _store():
    from utils import database as DB
    return DB.USER_STORE


def new_id(prefix: str) -> str:
    """`poll_1a2b3c_9f4e` — sortable-ish, short enough for a custom_id."""
    return f"{prefix}_{int(time.time()):x}_{secrets.token_hex(3)}"


# ── Config ───────────────────────────────────────────────────────────────────
def config_get(key: str) -> Optional[str]:
    return _store().community_config_get(key)


def config_put(key: str, value: str) -> None:
    _store().community_config_put(key, value)


def config_delete(key: str) -> None:
    _store().community_config_delete(key)


def config_all() -> dict:
    return _store().community_config_all()


# ── Polls ────────────────────────────────────────────────────────────────────
def put_poll(row: dict) -> None:
    _store().polls_put(row)


def get_poll(poll_id: str) -> Optional[dict]:
    return _store().polls_get(poll_id)


def list_polls(guild_id, limit: int = 10) -> list[dict]:
    return _store().polls_list(str(guild_id), limit)


def set_poll_state(poll_id: str, state: str, *, results=None,
                   closed_at: Optional[float] = None) -> None:
    _store().polls_set_state(poll_id, state, results=results,
                             closed_at=closed_at)


def set_poll_message(poll_id: str, channel_id, message_id) -> None:
    _store().polls_set_message(poll_id, channel_id, message_id)


def claim_due_polls(now: Optional[float] = None, limit: int = 20) -> list[str]:
    return _store().polls_claim_due(now, limit)


def recover_polls() -> int:
    return _store().polls_recover()


def vote(poll_id: str, user_id, choice: str,
         now: Optional[float] = None) -> bool:
    return _store().poll_vote(poll_id, str(user_id), choice, now=now)


def vote_of(poll_id: str, user_id) -> Optional[str]:
    return _store().poll_vote_of(poll_id, str(user_id))


def tally(poll_id: str) -> dict:
    return _store().poll_tally(poll_id)


def voters(poll_id: str) -> list[str]:
    return _store().poll_voters(poll_id)


# ── Giveaways ────────────────────────────────────────────────────────────────
def put_giveaway(row: dict) -> None:
    _store().giveaways_put(row)


def get_giveaway(giveaway_id: str) -> Optional[dict]:
    return _store().giveaways_get(giveaway_id)


def list_giveaways(guild_id, limit: int = 10) -> list[dict]:
    return _store().giveaways_list(str(guild_id), limit)


def set_giveaway_state(giveaway_id: str, state: str, *, winner_ids=None,
                       ended_at: Optional[float] = None) -> None:
    _store().giveaways_set_state(giveaway_id, state, winner_ids=winner_ids,
                                 ended_at=ended_at)


def set_giveaway_message(giveaway_id: str, channel_id, message_id) -> None:
    _store().giveaways_set_message(giveaway_id, channel_id, message_id)


def claim_due_giveaways(now: Optional[float] = None,
                        limit: int = 20) -> list[str]:
    return _store().giveaways_claim_due(now, limit)


def recover_giveaways() -> int:
    return _store().giveaways_recover()


def enter(giveaway_id: str, user_id, now: Optional[float] = None) -> bool:
    return _store().giveaway_enter(giveaway_id, str(user_id), now=now)


def entries(giveaway_id: str) -> list[str]:
    return _store().giveaway_entries(giveaway_id)


def entry_count(giveaway_id: str) -> int:
    return _store().giveaway_entry_count(giveaway_id)
