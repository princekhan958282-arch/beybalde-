"""
cogs/battle/special_gate.py — is this blade's Special actually available?

Until now the answer was one line in three different places: gauge >= 150.
`BattleView._special_disabled` asked it for the button, `story_ai.affordable`
asked it for the League opponent, and the boss AI asked it for itself. Three
copies of one rule is survivable while the rule never changes.

Kirindael changes it. Its Special needs a full gauge AND 100 Purifier Charge —
a second resource, built by its own abilities rather than by the move economy.
So the rule moves here, once, and every caller asks this module instead.

The shape in beyblades.json
---------------------------
    "special_requires": {
        "counter": "purifier_charge",
        "value":   100,
        "label":   "Purifier Charge",
        "emoji":   "⚡"
    }

Absent on every other blade, which is the point: `requirement()` returns None
and the gate collapses back to the gauge check it always was.

The counter itself lives on the AbilityEngine (`engine.counters[(key, name)]`),
where every other ability counter already lives, so the blade's own rules fill
it with the ordinary `gain_counter` op and nothing new has to persist it.

Never raises. A session mid-teardown, an engine that hasn't been built yet, a
malformed `special_requires` — all of them answer "no extra requirement",
because a blade that cannot fire its Special because of a KeyError is a worse
outcome than one that fires it slightly early.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("beyblade_bot.special_gate")


def requirement(blade: Optional[dict]) -> Optional[dict]:
    """The blade's extra Special requirement, normalised, or None.

    Fails open — a malformed block is treated as no requirement rather than as
    a Special nobody can ever fire.
    """
    raw = (blade or {}).get("special_requires")
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("counter") or "").strip()
    try:
        need = int(raw.get("value") or 0)
    except (TypeError, ValueError):
        return None
    if not name or need <= 0:
        return None
    return {
        "counter": name,
        "value":   need,
        "label":   str(raw.get("label") or name.replace("_", " ").title()),
        "emoji":   str(raw.get("emoji") or "✨"),
    }


def charge(session: Any, key: str, counter: str) -> int:
    """How much of `counter` this side has banked right now."""
    try:
        return int(session.ability.counters.get((str(key), counter), 0))
    except Exception:                                    # noqa: BLE001
        return 0


def progress(session: Any, key: str, blade: Optional[dict]) -> Optional[dict]:
    """`{have, need, label, emoji, ready}` for display, or None if not gated."""
    req = requirement(blade)
    if not req:
        return None
    have = charge(session, key, req["counter"])
    return {"have": have, "need": req["value"], "label": req["label"],
            "emoji": req["emoji"], "ready": have >= req["value"]}


def blocked_reason(session: Any, key: str, blade: Optional[dict],
                   gauge: int, gauge_max: int) -> Optional[str]:
    """Why the Special cannot be used, or None if it can.

    Returns a player-facing sentence rather than a bool: "the button is
    greyed out" is not an answer anybody can act on, and a blade with a
    second resource has two different reasons to be locked.
    """
    if gauge < gauge_max:
        return f"❌ Special gauge not full! ({int(gauge)}/{int(gauge_max)})"
    prog = progress(session, key, blade)
    if prog and not prog["ready"]:
        return (f"❌ {prog['emoji']} {prog['label']} not charged! "
                f"({prog['have']}/{prog['need']})")
    return None


def ready(session: Any, key: str, blade: Optional[dict],
          gauge: int, gauge_max: int) -> bool:
    """The whole gate as one boolean, for callers that only need yes/no."""
    return blocked_reason(session, key, blade, gauge, gauge_max) is None


def spend(session: Any, key: str, blade: Optional[dict]) -> int:
    """Zero the extra counter after the Special fires. Returns what was spent.

    Kept here beside the gate rather than in the blade's rules so the reset
    can never drift from the requirement it pays for.
    """
    req = requirement(blade)
    if not req:
        return 0
    spent = charge(session, key, req["counter"])
    try:
        session.ability.counters[(str(key), req["counter"])] = 0
    except Exception:                                    # noqa: BLE001
        log.debug("could not reset %s for %s", req["counter"], key)
        return 0
    return spent
