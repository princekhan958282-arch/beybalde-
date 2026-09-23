"""
cogs/updates/targeting.py — who an update is for.

An audience is a small dict, `{"kind": ..., ...}`, stored on the update row as
JSON so a send can be re-resolved after a restart rather than depending on a
list held in memory.

Almost none of this is new work: the store already indexes `last_seen`,
`created_at` and `inv_count`, and `all_user_ids()`'s own docstring names
announcements as the reason it exists. What this module adds is the mapping
from a name to the right query, and one filter applied on top of all of them.
"""

from __future__ import annotations

import time

from . import store as S

ALL = "all"
ACTIVE = "active"
NEW = "new"
SERVER = "server"
CONDITION = "condition"
NOT_RECEIVED = "not_received"
USERS = "users"           # an explicit list — how a report reply is addressed

DAY = 86400.0

KINDS = (ALL, ACTIVE, NEW, SERVER, CONDITION, NOT_RECEIVED, USERS)

LABEL = {
    ALL:          ("🌍", "Everyone"),
    ACTIVE:       ("🔥", "Active players"),
    NEW:          ("🌱", "New players"),
    SERVER:       ("🏠", "One server"),
    CONDITION:    ("🎚️", "Matching a condition"),
    NOT_RECEIVED: ("📭", "Missed an earlier update"),
    USERS:        ("👤", "Specific players"),
}

# Condition fields that are real indexed columns. Anything else is refused
# rather than silently ignored: an audience that quietly matches everyone is
# how a "level 50+" announcement reaches level 1.
CONDITION_FIELDS = ("level", "coins", "wins", "rank_score")


def describe(audience: dict) -> str:
    """One human line, for the preview and the history list."""
    a = audience or {}
    kind = a.get("kind", ALL)
    emoji, label = LABEL.get(kind, ("❓", kind))
    bits = [f"{emoji} {label}"]
    if kind == ACTIVE:
        bits.append(f"seen in the last {int(a.get('days', 14))} days")
    elif kind == NEW:
        bits.append(f"joined in the last {int(a.get('days', 7))} days")
    elif kind == SERVER:
        bits.append(f"guild `{a.get('guild_id')}`")
    elif kind == CONDITION:
        bits.append(f"{a.get('field')} ≥ {a.get('min')}")
    elif kind == NOT_RECEIVED:
        bits.append(f"missed `{a.get('update_id')}`")
    elif kind == USERS:
        bits.append(f"{len(a.get('ids') or [])} named")
    bits.append("owning at least one blade")
    return " · ".join(bits)


def _base_ids(bot, audience: dict) -> list[int]:
    st = S._store()
    a = audience or {}
    kind = a.get("kind", ALL)

    if kind == USERS:
        out = []
        for u in (a.get("ids") or []):
            try:
                out.append(int(u))
            except (TypeError, ValueError):
                continue
        return out

    if kind == ACTIVE:
        days = float(a.get("days", 14))
        rows = st.active_users_since(time.time() - days * DAY, limit=1_000_000)
        out = []
        for r in rows:
            try:
                out.append(int(r["user_id"]))
            except (TypeError, ValueError, KeyError):
                continue
        return out

    if kind == NEW:
        return st.created_since(time.time() - float(a.get("days", 7)) * DAY)

    if kind == NOT_RECEIVED:
        return st.users_never_received(str(a.get("update_id") or ""))

    if kind == SERVER:
        guild = bot.get_guild(int(a.get("guild_id") or 0)) if bot else None
        if guild is None:
            return []
        members = {m.id for m in guild.members if not m.bot}
        # Intersected with the registry: a guild member who has never played
        # has no profile, and a DM about a balance change would be spam.
        return [u for u in st.all_user_ids() if u in members]

    if kind == CONDITION:
        field = str(a.get("field") or "")
        if field not in CONDITION_FIELDS:
            raise ValueError(f"not a targetable field: {field!r}")
        minimum = float(a.get("min", 0))
        out = []
        for uid, prof in st.load_all().items():
            try:
                if float(prof.get(field, 0) or 0) >= minimum:
                    out.append(int(uid))
            except (TypeError, ValueError):
                continue
        return out

    return st.all_user_ids()


def resolve(bot, audience: dict) -> dict:
    """`{"ids": [...], "dropped_no_beys": n, "total_before": n}`.

    The drop count is returned rather than swallowed. Filtering out players
    who own nothing removes real accounts — `cogs/core/onboarding.py` records
    five live profiles sitting on 60,000 XP and an empty inventory — so the
    preview says how many vanished instead of quietly shrinking the audience.
    """
    a = dict(audience or {})
    ids = list(dict.fromkeys(_base_ids(bot, a)))
    before = len(ids)
    dropped = 0
    # Update DMs are only for database players who own at least one Bey.
    # This is mandatory rather than an audience option.
    if ids:
        empty = S._store().ids_without_beys(ids)
        if empty:
            ids = [u for u in ids if u not in empty]
            dropped = len(empty)
    return {"ids": ids, "dropped_no_beys": dropped, "total_before": before}


def reachable(bot, ids) -> set:
    """Of these, whom the bot could actually DM.

    A bot may only open a DM with someone it shares a guild with. A targeted
    player who shares none is still queued — they may rejoin — but they will
    resolve to BLOCKED on the first attempt, and the preview should say so
    before a send rather than after it.
    """
    if bot is None:
        return set()
    seen: set = set()
    for guild in getattr(bot, "guilds", []) or []:
        for m in getattr(guild, "members", []) or []:
            seen.add(m.id)
    want = {int(u) for u in ids}
    return want & seen
