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

A second, independent shape covers a plain turn-count cooldown instead of a
resource threshold — Cosmic Phoenix's Phoenix Nova, which fires freely but
can't be re-used for 4 rounds after it does:

    "special_requires": {
        "cooldown_name": "phoenix_nova_cd",
        "label":         "Phoenix Nova",
        "emoji":         "☄️"
    }

The blade's own `on_special` rule arms this with the *existing* `start_cooldown`
op; `engine.cooldowns` is decremented every round by the *existing*
`tick_extras()` sweep. No new state and no new decrement logic — this module
just gained a second way to read a resource that was already there. The two
shapes can combine (a counter AND a cooldown) or be used alone.

Never raises. A session mid-teardown, an engine that hasn't been built yet, a
malformed `special_requires` — all of them answer "no extra requirement",
because a blade that cannot fire its Special because of a KeyError is a worse
outcome than one that fires it slightly early.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from cogs.battle import button_profile

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
        need = 0
    has_counter = bool(name) and need > 0

    cd_name = str(raw.get("cooldown_name") or "").strip()
    has_cooldown = bool(cd_name)

    if not has_counter and not has_cooldown:
        return None
    label_src = name or cd_name
    return {
        "counter":       name if has_counter else "",
        "value":         need if has_counter else 0,
        "cooldown_name": cd_name if has_cooldown else "",
        "label":         str(raw.get("label") or label_src.replace("_", " ").title()),
        "emoji":         str(raw.get("emoji") or "✨"),
    }


def charge(session: Any, key: str, counter: str) -> int:
    """How much of `counter` this side has banked right now."""
    try:
        return int(session.ability.counters.get((str(key), counter), 0))
    except Exception:                                    # noqa: BLE001
        return 0


def cooldown_left(session: Any, key: str, cooldown_name: str) -> int:
    """Rounds left before `cooldown_name` clears, from `engine.cooldowns`."""
    try:
        return int(session.ability.cooldowns.get((str(key), cooldown_name), 0))
    except Exception:                                    # noqa: BLE001
        return 0


def progress(session: Any, key: str, blade: Optional[dict]) -> Optional[dict]:
    """`{have, need, label, emoji, ready}` for display, or None if not gated.

    For a cooldown-style requirement `have`/`need` describe rounds remaining
    (0/0 when off cooldown) rather than a banked resource — still enough for a
    caller to decide whether to show a "charged" state.
    """
    req = requirement(blade)
    if not req:
        return None
    if req["cooldown_name"]:
        left = cooldown_left(session, key, req["cooldown_name"])
        return {"have": left, "need": 0, "label": req["label"],
                "emoji": req["emoji"], "ready": left <= 0}
    have = charge(session, key, req["counter"])
    return {"have": have, "need": req["value"], "label": req["label"],
            "emoji": req["emoji"], "ready": have >= req["value"]}


def gauge_cost(blade: Optional[dict]) -> int:
    """How much gauge THIS blade's Special costs to fire.

    Was a hardcoded SPECIAL_GAUGE_MAX for all 113 blades, so a Special could
    be gated by a second resource (Purifier Charge) or a cooldown but never
    simply be cheaper or dearer than everyone else's. Authored as
    `button_profile.special.gauge_cost`; absent, this is the full bar and
    every caller behaves exactly as before.
    """
    return button_profile.special_gauge_cost(blade)


def blocked_reason(session: Any, key: str, blade: Optional[dict],
                   gauge: int, gauge_max: Optional[int] = None) -> Optional[str]:
    """Why the Special cannot be used, or None if it can.

    Returns a player-facing sentence rather than a bool: "the button is
    greyed out" is not an answer anybody can act on, and a blade with a
    second resource has two different reasons to be locked.

    `gauge_max` is now OPTIONAL and callers should omit it: left None it is
    read from the blade, which is the only way the button, the League AI and
    the resolver can be guaranteed to agree. Passing it explicitly overrides
    the blade's own cost and is kept only so existing callers cannot break —
    for a blade with no authored cost the two are the same number anyway.
    """
    need = gauge_cost(blade) if gauge_max is None else int(gauge_max)
    if gauge < need:
        return f"❌ Special gauge not full! ({int(gauge)}/{need})"
    req = requirement(blade)
    prog = progress(session, key, blade)
    if prog and not prog["ready"]:
        if req and req["cooldown_name"]:
            return (f"❌ {prog['emoji']} {prog['label']} recharging! "
                    f"({prog['have']} turn(s) left)")
        return (f"❌ {prog['emoji']} {prog['label']} not charged! "
                f"({prog['have']}/{prog['need']})")
    return None


def ready(session: Any, key: str, blade: Optional[dict],
          gauge: int, gauge_max: Optional[int] = None) -> bool:
    """The whole gate as one boolean, for callers that only need yes/no."""
    return blocked_reason(session, key, blade, gauge, gauge_max) is None


def spend(session: Any, key: str, blade: Optional[dict]) -> int:
    """Pay for a Special that just fired. Returns the gauge deducted.

    This used to reset only the extra counter, and had ZERO callers anywhere
    in the repo — the gauge half was done by `stamina_manager.consume_gauge`,
    which hard-zeroes. That was fine while every Special cost the whole bar
    and there was nothing else to pay. It stops being fine the moment a blade
    can have a cheaper Special: zeroing would silently confiscate the change.

    So both halves of "pay for the Special" now live here, together, where
    they cannot drift from the requirement they pay for:
      * deduct exactly `gauge_cost(blade)` from the gauge, floored at 0
      * zero the extra counter (Purifier Charge and friends), as before
    """
    cost = gauge_cost(blade)
    try:
        sm = session.stamina_manager
        sm.gauge[key] = max(0, int(sm.gauge.get(key, 0)) - cost)
    except Exception:                                    # noqa: BLE001
        log.debug("could not deduct special gauge for %s", key, exc_info=True)
        cost = 0

    req = requirement(blade)
    if req and req["counter"]:
        try:
            session.ability.counters[(str(key), req["counter"])] = 0
        except Exception:                                # noqa: BLE001
            log.debug("could not reset %s for %s", req["counter"], key)
    return cost


def apply_stability_cost(session: Any, key: str,
                         blade: Optional[dict]) -> list[str]:
    """Self-inflicted stability for firing a Special. Opt-in, and never lethal.

    Specials cost 0 stability for everyone by default and that is deliberate —
    the old -10 let a blade ring ITSELF out by unleashing its own Special (see
    the comment in attack_manager._resolve_special). A blade can now author a
    cost, but it is clamped to leave at least 1 stability standing, so
    "my Special is risky" can never become "my Special killed me".
    """
    cost = button_profile.special_stability_cost(blade)
    if cost <= 0:
        return []
    try:
        stm = session.stability_manager
        current = int(stm.stability.get(key, 0))
        # Leave 1 behind. A Special that rings its own user out is a bug
        # wearing a drawback's clothes.
        applied = min(cost, max(0, current - 1))
        if applied <= 0:
            return []
        return list(stm._apply(key, -applied) or [])
    except Exception:                                    # noqa: BLE001
        log.debug("could not apply special stability cost for %s", key,
                  exc_info=True)
        return []
