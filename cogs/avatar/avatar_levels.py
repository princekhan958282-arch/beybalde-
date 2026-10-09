"""
avatar_levels.py — per-card avatar progression: types, growth, and cost curves.

Pure arithmetic. No database, no discord, no I/O — so the whole curve is testable
headlessly and a balance change is a one-line edit with a simulator to check it.

── The two rules that keep this from breaking the live game ──────────────────

1. **Level 1 adds nothing.** Growth is `growth * (level - 1)`, so a card at
   level 1 contributes byte-identically to what it contributed before this
   module existed. Every one of the 29 authored cards keeps its exact tuning
   until somebody spends coins, and no player wakes up stronger or weaker
   because a migration ran. This is the same calibration guarantee that let
   `resolve_special()` start reading the special stat without re-tuning 78
   blades.

   The spec's §2.2 formula was `AVATAR_BASE + growth * (level - 1)` with
   `AVATAR_BASE = 20`, which would have handed every card +20/+20/+20 the moment
   it shipped. That is a roster-wide buff disguised as a schema change.

2. **Growth is flat stat lines, not a multiplier on the card's own bonuses.**
   Scaling authored bonuses would multiply Argus's +40% crit into +69% at Lv5
   and Dyrroth's defence-break with it. Flat lines are legible, cap-safe, and
   they do not turn a card's signature mechanic into a different mechanic.

── Growth rates ──────────────────────────────────────────────────────────────

The spec offered 22/level for a primary stat and then flagged its own number:
at `BASE_HP = 2000`, +88 flat attack against a roster averaging 97 attack is the
single most likely source of a broken launch. Its own recommendation was 12.
That is what is implemented — Lv5 attack card is +48 attack, meaningful against
97 without rewriting the game.
"""

from __future__ import annotations

import math

TYPES = ("attack", "defense", "stamina", "balance")
STATS = ("attack", "defense", "stamina")

from .avatar_config import (MAX_CARD_LEVEL, MAX_SKILL_LEVEL, GROWTH,
    CARD_LEVEL_COSTS, SKILL_COST_BASE, SKILL_COST_RATIO, SKILL_MAGNITUDE_STEP,
    SKILL_COST_ROUNDING, RESET_REFUND, SKILL_CAP_PER_CARD_LEVEL)


def growth_for(avatar_type: str) -> dict[str, int]:
    """Per-level growth for a type. Unknown types fall back to balance rather
    than raising — a typo in avatar_data.json must not stop a fight starting."""
    return GROWTH.get(str(avatar_type or "").lower(), GROWTH["balance"])


def clamp_level(level) -> int:
    try:
        lvl = int(level)
    except (TypeError, ValueError):
        return 1
    return max(1, min(MAX_CARD_LEVEL, lvl))


def card_stat_bonus(avatar_type: str, level) -> dict[str, int]:
    """Flat stat lines a card contributes at `level`. All zero at level 1."""
    lvl = clamp_level(level)
    if lvl <= 1:
        return {stat: 0 for stat in STATS}
    g = growth_for(avatar_type)
    steps = lvl - 1
    return {stat: int(g.get(stat, 0)) * steps for stat in STATS}


def max_skill_level_for(card_level) -> int:
    """The spec's gate: `max_skill_level = avatar_level * 2`.

    Lv1 caps skills at 2, Lv5 unlocks 10. This is what stops two independent
    grinds and forces card investment before skill investment.
    """
    return max(1, min(MAX_SKILL_LEVEL, clamp_level(card_level) * SKILL_CAP_PER_CARD_LEVEL))


def card_level_cost(from_level, to_level=None) -> int:
    """Coins to go from `from_level` to `to_level` (default: one level).

    Sums the individual steps. Never multiplies one step by a count — that is
    the bug the spec calls out at §5.4, and it overcharges or undercharges by
    a wide margin on any curve that is not flat.
    """
    start = clamp_level(from_level)
    end = clamp_level(to_level if to_level is not None else start + 1)
    if end <= start:
        return 0
    return sum(CARD_LEVEL_COSTS[lvl - 1] for lvl in range(start, end))


def skill_level_cost(from_level, to_level=None) -> int:
    """Coins to raise one skill. Same step-summing rule as card levels."""
    start = max(1, min(MAX_SKILL_LEVEL, int(from_level or 1)))
    end = max(1, min(MAX_SKILL_LEVEL,
                     int(to_level if to_level is not None else start + 1)))
    if end <= start:
        return 0
    total = 0
    for lvl in range(start, end):
        raw = SKILL_COST_BASE * (SKILL_COST_RATIO ** (lvl - 1))
        # Rounded to the nearest 500 so the shop shows readable numbers instead
        # of 12000 / 16200 / 21870 / 29524.
        total += int(round(raw / SKILL_COST_ROUNDING) * SKILL_COST_ROUNDING)
    return total


def skill_magnitude_mult(skill_level) -> float:
    """×1.00 at Lv1 rising to ×1.72 at Lv10."""
    lvl = max(1, min(MAX_SKILL_LEVEL, int(skill_level or 1)))
    return 1.0 + SKILL_MAGNITUDE_STEP * (lvl - 1)


def full_card_cost() -> int:
    """Every level and all three skills maxed on one card — the headline number."""
    return (card_level_cost(1, MAX_CARD_LEVEL)
            + 3 * skill_level_cost(1, MAX_SKILL_LEVEL))


def refund_for(spent: int, fraction: float = RESET_REFUND) -> int:
    """70% of coins ACTUALLY spent, per the spec's §2.5 reset rule.

    Takes the real recorded spend rather than recomputing from the cost table.
    The moment a cost is rebalanced — and it will be — a table-derived refund
    starts silently paying the wrong amount to everyone who bought at the old
    price, with no way to detect it after the fact.
    """
    return int(math.floor(max(0, int(spent or 0)) * fraction))
