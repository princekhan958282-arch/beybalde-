"""
cogs/battle/button_profile.py — what does THIS blade's button do?

Until now the answer was a module-level constant, the same for all 113 blades:
Attack always cost 2.2 stamina, Defense always gave 20 gauge, Charge always
gave 50 and nothing else, Special always cost the full 150 gauge. A blade
could differ in its *stats* and its *abilities*, but never in how its buttons
behaved — so four of the five buttons were the same button on every blade in
the game, and Charge was a skip-turn that no blade in the roster reacted to.

The shape in beyblades.json
---------------------------
    "button_profile": {
      "attack":  {"stamina_cost": 2.2, "stability_hit": -10, "gauge": 15},
      "defense": {"stability_blocked": 5, "gauge": 20},
      "stamina": {"stability_recovery": 25},
      "charge":  {"max_stacks": 3, "per_stack_pct": 12,
                  "lost_on_hit": true, "stability_per_stack": 2},
      "special": {"gauge_cost": 150, "stability_cost": 0}
    }

Absent on every blade today, which is the point — and the reason this module
exists rather than the fields being read inline. Every getter below falls back
to the constant it always used, so a blade with no `button_profile` resolves
bit-identically to before this file existed. `tools/sim_button_effects.py`
proves that over the whole roster rather than asserting it here.

Partial blocks are fine and are the expected case: a blade that only wants a
cheaper Special writes `{"special": {"gauge_cost": 90}}` and keeps today's
numbers everywhere else. Merging is per-key, not per-block.

Never raises
------------
A malformed profile — a string where a number belongs, a list where a dict
belongs, a null block — resolves to the defaults rather than propagating. A
blade whose Attack button raises a TypeError mid-battle is a far worse outcome
than one quietly playing by the standard numbers, and this is data that
non-programmers edit by hand.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from cogs.core.constants import (
    BUTTON_REWORK_GLOBAL,
    GAUGE_PER_ATTACK, GAUGE_PER_CHARGE, GAUGE_PER_DEFENSE,
    GAUGE_PER_DMG_TAKEN, GAUGE_PER_STAMINA,
    MOVE_ATTACK, MOVE_CHARGE, MOVE_DEFENSE, MOVE_SPECIAL, MOVE_STAMINA,
    SPECIAL_GAUGE_MAX,
    STABILITY_ATTACK_HIT, STABILITY_ATTACK_MISS, STABILITY_DEF_BLOCKED,
    STABILITY_DEF_PASSIVE, STABILITY_STAMINA_RECOVERY, STABILITY_TIERS,
)

log = logging.getLogger(__name__)

# The neutral profile: today's behavior, spelled out once.
#
# Every value here is IMPORTED from constants rather than retyped, so "the
# default equals the current constant" is true by construction and cannot rot
# when someone tunes a constant and forgets this file. That property is what
# the roster-wide neutrality check in sim_button_effects.py actually verifies.
_DEFAULTS: dict[str, dict[str, Any]] = {
    "attack": {
        "stability_hit":  STABILITY_ATTACK_HIT,
        "stability_miss": STABILITY_ATTACK_MISS,
        "gauge":          GAUGE_PER_ATTACK,
    },
    "defense": {
        "stability_passive": STABILITY_DEF_PASSIVE,
        "stability_blocked": STABILITY_DEF_BLOCKED,
        "gauge":             GAUGE_PER_DEFENSE,
    },
    "stamina": {
        "stability_recovery": STABILITY_STAMINA_RECOVERY,
        "gauge":              GAUGE_PER_STAMINA,
    },
    "charge": {
        "gauge": GAUGE_PER_CHARGE,
        # max_stacks 0 is what makes Charge stacks opt-in: with no stacks to
        # bank, the Charge branch does exactly what it does today.
        "max_stacks":          0,
        "per_stack_pct":       0.0,
        "lost_on_hit":         True,
        "stability_per_stack": 0,
    },
    "special": {
        "gauge_cost": SPECIAL_GAUGE_MAX,
        # Deliberately 0. Specials used to cost -10 stability, which let a
        # blade ring ITSELF out by unleashing its own Special (see the comment
        # in attack_manager._resolve_special). Opt-in only, and clamped by
        # stability_cost() so it can never be lethal.
        "stability_cost": 0,
    },
    "stability": {
        "tiers": STABILITY_TIERS,
    },
}

# Which gauge source belongs to which button, so gauge_gain() can answer for
# the "dmg_taken" source too — that one is an event rather than a button and
# has no profile block of its own.
_GAUGE_SOURCE_BUTTON = {
    "attack": "attack", "defense": "defense",
    "stamina": "stamina", "charge": "charge",
}

_MOVE_BUTTON = {
    MOVE_ATTACK: "attack", MOVE_DEFENSE: "defense",
    MOVE_STAMINA: "stamina", MOVE_CHARGE: "charge", MOVE_SPECIAL: "special",
}


def _block(blade: Optional[dict], button: str) -> dict:
    """The authored block for one button, or {} — never raises."""
    try:
        prof = (blade or {}).get("button_profile")
        if not isinstance(prof, dict):
            return {}
        blk = prof.get(button)
        return blk if isinstance(blk, dict) else {}
    except Exception:                                    # noqa: BLE001
        log.debug("unreadable button_profile on %s",
                  (blade or {}).get("name", "?"), exc_info=True)
        return {}


def resolve(blade: Optional[dict], button: str) -> dict:
    """Effective settings for one button: defaults, overlaid with what's authored.

    Per-key merge, so a partial block keeps today's numbers for everything it
    does not mention.
    """
    base = dict(_DEFAULTS.get(button, {}))
    base.update(_block(blade, button))
    return base


def _num(blade: Optional[dict], button: str, key: str, default: Any) -> Any:
    """One numeric setting, coerced and defaulted. Never raises."""
    val = resolve(blade, button).get(key, default)
    try:
        return type(default)(val) if default is not None else val
    except (TypeError, ValueError):
        log.debug("bad button_profile.%s.%s=%r on %s — using %r",
                  button, key, val, (blade or {}).get("name", "?"), default)
        return default


# ── Public getters ───────────────────────────────────────────────────────────

def gauge_gain(blade: Optional[dict], source: str) -> int:
    """Gauge for one action or event. `source` matches StaminaManager's names."""
    if source == "dmg_taken":
        return GAUGE_PER_DMG_TAKEN          # an event, not a button
    button = _GAUGE_SOURCE_BUTTON.get(source)
    if button is None:
        return 0
    return _num(blade, button, "gauge", _DEFAULTS[button]["gauge"])


def stamina_cost(blade: Optional[dict], move: str, base: float) -> float:
    """This blade's authored stamina cost for a move, else `base`.

    `base` is passed in rather than imported because STAMINA_COST lives in
    stamina_manager, which is the only caller — importing it here would make
    the two modules import each other. The caller owns the table; this owns
    the per-blade override.

    Note this is the flat override only. The stat-scaling curve is applied by
    StaminaManager.deduct_cost, which is the single chokepoint every move's
    cost already flows through, so surcharge and discount ops keep composing
    on top of it in the documented order.
    """
    button = _MOVE_BUTTON.get(move)
    if button is None:
        return base
    return float(_num(blade, button, "stamina_cost", float(base)))


def special_gauge_cost(blade: Optional[dict]) -> int:
    """What this blade's Special costs to fire. Defaults to the full bar.

    Floored at 1: a free Special is not a cheap Special, it is a Special with
    no gate at all, and the gauge economy is the only thing pacing them.
    """
    return max(1, _num(blade, "special", "gauge_cost", SPECIAL_GAUGE_MAX))


def special_stability_cost(blade: Optional[dict]) -> int:
    """Self-inflicted stability for firing a Special. 0 unless authored.

    Never negative: this is a COST, and an authored -5 meaning "gain 5" would
    silently invert a blade's drawback into a bonus. A blade that wants to
    gain stability from its Special has `gain_stability` in its rules.
    """
    return max(0, _num(blade, "special", "stability_cost", 0))


def charge_cfg(blade: Optional[dict]) -> dict:
    """Charge-stack settings. `max_stacks` 0 (the default) means no stacks."""
    cfg = resolve(blade, "charge")
    return {
        "max_stacks":          max(0, _num(blade, "charge", "max_stacks", 0)),
        "per_stack_pct":       max(0.0, _num(blade, "charge",
                                             "per_stack_pct", 0.0)),
        "lost_on_hit":         bool(cfg.get("lost_on_hit", True)),
        "stability_per_stack": max(0, _num(blade, "charge",
                                           "stability_per_stack", 0)),
    }


def stability_tiers(blade: Optional[dict]) -> tuple:
    """Ordered (fraction, effects) tiers, highest fraction first.

    Falls back to the global STABILITY_TIERS, which is empty by default — so
    with nothing authored anywhere, stability keeps its all-or-nothing
    behavior and this whole system is inert.
    """
    tiers = resolve(blade, "stability").get("tiers") or STABILITY_TIERS
    try:
        clean = [(float(f), dict(eff)) for f, eff in tiers
                 if isinstance(eff, dict)]
    except (TypeError, ValueError):
        log.debug("bad stability tiers on %s — ignoring",
                  (blade or {}).get("name", "?"))
        return ()
    clean.sort(key=lambda t: t[0], reverse=True)
    return tuple(clean)


def stability_setting(blade: Optional[dict], button: str, key: str) -> int:
    """One stability number for a button (e.g. defense/stability_blocked)."""
    return _num(blade, button, key, _DEFAULTS.get(button, {}).get(key, 0))


def has_profile(blade: Optional[dict]) -> bool:
    """True if this blade authored ANY button override.

    Used by the neutrality check to split the roster, and by callers that want
    to skip new-mechanic work entirely on the ~113 blades that opt out.
    """
    try:
        return isinstance((blade or {}).get("button_profile"), dict)
    except Exception:                                    # noqa: BLE001
        return False


def rework_active(blade: Optional[dict]) -> bool:
    """Should this blade get the reworked (non-neutral) button behavior?

    True when the blade opted in, or when the global switch is thrown. Kept as
    one function so the "opt-in per blade OR globally" rule is stated once
    rather than re-derived at each call site.
    """
    return BUTTON_REWORK_GLOBAL or has_profile(blade)
