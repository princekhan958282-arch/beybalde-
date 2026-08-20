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

What a level is worth
---------------------
Coins, and nothing else. Reaching level N pays `N x 100` — level 2 pays 200,
level 3 pays 300, and so on up.

Trainer level used to also grant a flat stat multiplier (+2% per 10 levels, to
a +20% ceiling). That is gone as of v1.23: it applied equally to attack,
defence and stamina, so it never changed a decision, only made the same battle
resolve faster for whoever had played longer. A coin is a reward the player can
spend; a scalar on every stat at once is a tax on everyone who started later.

The arithmetic lands where it does for a reason. XP from level N-1 to N is
`50(N² - (N-1)²)` = `100N - 50`, and the reward for reaching N is `100N`. So the
payout is almost exactly **one coin per XP earned, at every level forever** —
the reward curve is the derivative of the XP curve. Nothing has to be re-tuned
as the cap rises, and no level is a better or worse deal than any other.
"""

from __future__ import annotations

import math

# 9,999 since v1.20, up from 100. At 50–90 XP a chat message, level 100 is
# roughly 7,100 messages and the top of the range is somewhere past the heat
# death of the server — which is the point of a ceiling.
MAX_LEVEL = 9999

# XP per level-squared. `xp_for_level(1)` is 50, `xp_for_level(100)` is 500,000.
XP_PER_LEVEL_SQ = 50

# Coins paid for reaching a level, per level number. Level 2 pays 200, level 3
# pays 300, level 100 pays 10,000.
COINS_PER_LEVEL = 100


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


def level_reward(level: int) -> int:
    """Coins for reaching this level. Level 2 is 200, level 3 is 300.

    Level 0 and anything below it pay nothing: level 0 is where a profile
    starts, so paying for it would hand every new account a bonus for existing.
    """
    level = int(level)
    if level < 1 or level > MAX_LEVEL:
        return 0
    return level * COINS_PER_LEVEL


def level_up_payout(old_level: int, new_level: int) -> int:
    """Coins for every level crossed going from `old_level` to `new_level`.

    Summed rather than "levels gained x a flat rate", because the reward is not
    flat: one grant that jumps a player from 3 to 6 owes 400 + 500 + 600, and
    paying `3 x 600` or `3 x 100` would both be wrong. A big XP drop and a
    slow climb through the same levels are worth exactly the same.
    """
    old_level = max(0, int(old_level))
    new_level = min(MAX_LEVEL, int(new_level))
    if new_level <= old_level:
        return 0
    # Closed form for the sum of an arithmetic run — a loop here would run
    # 9,999 times for one `;givexp` of a few billion.
    lo, hi = old_level + 1, new_level
    return COINS_PER_LEVEL * (lo + hi) * (hi - lo + 1) // 2
