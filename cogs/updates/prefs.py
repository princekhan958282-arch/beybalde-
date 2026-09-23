"""
cogs/updates/prefs.py — who wants to hear from the bot, and about what.

Two switches on the profile, both defaulting ON:

    notify_updates   release notes, balance changes, maintenance
    notify_events    "a tournament is starting", "the EXP Surge is live"

Defaulting on is deliberate. A notification system that ships opted-OUT
reaches nobody until every player finds a setting they do not know exists,
which is the same as not shipping it.

Two priorities ignore the switch — IMPORTANT and CRITICAL. That override is
narrow on purpose: it is the difference between "we changed a number" and "the
bot is down for an hour", and if it were used for the former it would train
players to turn the switch off and stop reading the latter.
"""

from __future__ import annotations

# Lowest to highest. Order matters: `_RANK` is what the override compares.
LOW = "LOW"
NORMAL = "NORMAL"
IMPORTANT = "IMPORTANT"
CRITICAL = "CRITICAL"

PRIORITIES = (LOW, NORMAL, IMPORTANT, CRITICAL)
_RANK = {p: i for i, p in enumerate(PRIORITIES)}

PRIORITY_LABEL = {
    LOW:       ("🔈", "Low", 0x95A5A6),
    NORMAL:    ("🔔", "Normal", 0x3498DB),
    IMPORTANT: ("⚠️", "Important", 0xE67E22),
    CRITICAL:  ("🚨", "Critical", 0xED4245),
}

# At or above this, a player's opt-out is overridden.
OVERRIDE_AT = IMPORTANT

K_UPDATES = "notify_updates"
K_EVENTS = "notify_events"

# Which switch an event answers to. An event not listed here is treated as an
# update, which is the conservative half — it respects a switch rather than
# ignoring one nobody thought to map.
EVENT_SWITCH = {
    "UPDATE_RELEASED":  K_UPDATES,
    "BALANCE_CHANGE":   K_UPDATES,
    "MAINTENANCE":      K_UPDATES,
    "IMPORTANT_NOTICE": K_UPDATES,
    "EVENT_STARTED":    K_EVENTS,
}


def normalise(priority) -> str:
    """Anything unrecognised becomes NORMAL rather than raising."""
    p = str(priority or "").strip().upper()
    return p if p in _RANK else NORMAL


def overrides_optout(priority) -> bool:
    return _RANK[normalise(priority)] >= _RANK[OVERRIDE_AT]


def switch_for(event) -> str:
    return EVENT_SWITCH.get(str(event or "").strip().upper(), K_UPDATES)


def get(profile: dict, key: str) -> bool:
    """A switch's value. Absent means ON — see the module docstring."""
    val = (profile or {}).get(key)
    return True if val is None else bool(val)


def wants(profile: dict, *, event, priority) -> tuple[bool, str]:
    """`(deliver?, why_not)`.

    The reason string is not decoration: `/update status` has to distinguish
    "they opted out" from "their DMs are closed", because one is a setting the
    player chose and the other is a wall. Both would otherwise land in the
    ledger as BLOCKED with nothing to tell them apart.
    """
    key = switch_for(event)
    if get(profile, key):
        return True, ""
    which = "update" if key == K_UPDATES else "event"
    return False, f"opted out of {which} DMs"


def set_switch(profile: dict, key: str, on: bool) -> dict:
    """Set one switch on a profile dict. The caller persists it."""
    if key not in (K_UPDATES, K_EVENTS):
        raise ValueError(f"unknown notification switch: {key!r}")
    profile[key] = bool(on)
    return profile


def summary(profile: dict) -> str:
    """The two lines a player sees in `;notifications`."""
    on = "✅ on"
    off = "🚫 off"
    return (f"📣 **Update DMs** — {on if get(profile, K_UPDATES) else off}\n"
            f"🎉 **Event DMs** — {on if get(profile, K_EVENTS) else off}\n"
            f"-# Turning these off stops those DM notifications.")
