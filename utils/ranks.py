"""
utils/ranks.py
--------------
Rank tier definitions for the Beyblade bot.
Ranks are awarded based on rank_score (wins +25, losses -10).
"""

from __future__ import annotations

# ── Rank tiers ────────────────────────────────────────────────────────────────
# (min_score, rank_name, emoji, colour_hex)
RANK_TIERS: list[tuple[int, str, str, int]] = [
    (0,    "Rookie",      "⚪", 0x95a5a6),
    (50,   "Bronze I",    "🟫", 0xcd7f32),
    (150,  "Bronze II",   "🟫", 0xcd7f32),
    (300,  "Silver I",    "⬜", 0xbdc3c7),
    (500,  "Silver II",   "⬜", 0xbdc3c7),
    (750,  "Gold I",      "🟡", 0xf1c40f),
    (1000, "Gold II",     "🟡", 0xf1c40f),
    (1350, "Platinum I",  "💠", 0x1abc9c),
    (1750, "Platinum II", "💠", 0x1abc9c),
    (2200, "Diamond I",   "💎", 0x3498db),
    (2700, "Diamond II",  "💎", 0x3498db),
    (3300, "Legend",      "🌟", 0xe74c3c),
    (4000, "Blader God",  "👑", 0xf39c12),
]

WIN_SCORE  = 25   # points gained per win
LOSS_SCORE = 10   # points lost per loss (floor: 0)


def rank_score_for(profile: dict) -> int:
    return profile.get("rank_score", 0)


def tier_for_score(score: int) -> tuple[int, str, str, int]:
    tier = RANK_TIERS[0]
    for t in RANK_TIERS:
        if score >= t[0]:
            tier = t
    return tier


def rank_name_for(position: int) -> str:
    if position == 1:
        return "Blader God 👑"
    if position <= 3:
        return "Legend 🌟"
    if position <= 10:
        return "Diamond 💎"
    if position <= 25:
        return "Platinum 💠"
    if position <= 50:
        return "Gold 🟡"
    return "Ranked"


def apply_win(profile: dict) -> dict:
    profile["rank_score"] = profile.get("rank_score", 0) + WIN_SCORE
    return profile


def apply_loss(profile: dict) -> dict:
    profile["rank_score"] = max(0, profile.get("rank_score", 0) - LOSS_SCORE)
    return profile
