"""
trainer_levels.py — the trainer level curve, in one place.

Not to be confused with `utils/bey_levels.py`, which is the per-BLADE level
system. This is the player's own profile level: the number on the profile card,
the one `;rank` and the level board sort on, and the one that pays coins on the
way up.

Why it is its own module
------------------------
It used to live in `utils/database.py` — and a second copy of it lived in
`utils/profile_card.py`, with its own `MAX_LEVEL` and its own arithmetic,
because importing `utils.database` from a renderer runs `_init_mysql()` and
probes a database over the network just to draw a picture. That reason is
sound; the duplicate was not. Two copies of a curve is two answers to "what
level is this player", and the card is exactly where a disagreement would be
seen and disbelieved.

So the curve lives here, in a module with no imports beyond `math` and no side
effects at all. `utils/database.py` re-exports these names, so every existing
`from utils.database import MAX_LEVEL` keeps working.

The curve
---------
    level = floor(sqrt(xp / 50))        xp to reach a level = 50 · level²

Deliberately unchanged when the cap went from 100 to 9,999 in v1.20. Raising a
cap costs nobody anything; rescaling the curve would silently re-level every
profile in the store overnight, and the highest player in the live registry is
level 57 — so the old cap was never actually reached, and the new range is
headroom rather than a promotion.
"""

from __future__ import annotations

import math

# 9,999 since v1.20, up from 100. At 50–90 XP a chat message, level 100 is
# roughly 7,100 messages and the top of the range is somewhere past the heat
# death of the server — which is the point of a ceiling.
MAX_LEVEL = 9999

# XP per level-squared. `xp_for_level(1)` is 50, `xp_for_level(100)` is 500,000.
XP_PER_LEVEL_SQ = 50


def xp_for_level(level: int) -> int:
    """Total XP required to reach `level` (from 0)."""
    level = int(level)
    return level * level * XP_PER_LEVEL_SQ


def level_from_xp(xp: int) -> int:
    """Current level for a total XP figure, capped at MAX_LEVEL.

    Negative XP clamps to 0: `math.sqrt` of a negative raises, and one
    corrupted profile must not break every command that renders a level.
    """
    return min(MAX_LEVEL,
               int(math.floor(math.sqrt(max(0, int(xp)) / XP_PER_LEVEL_SQ))))


def xp_to_next_level(xp: int) -> tuple[int, int, int]:
    """`(level, xp_span_of_this_level, xp_progress_into_it)`.

    At MAX_LEVEL the last two are 0, which is what the card and the level-up
    embed render as a full bar rather than a division by zero.
    """
    lvl = level_from_xp(xp)
    if lvl >= MAX_LEVEL:
        return MAX_LEVEL, 0, 0
    current_floor = xp_for_level(lvl)
    next_floor = xp_for_level(lvl + 1)
    return lvl, next_floor - current_floor, int(xp) - current_floor
