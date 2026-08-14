"""
boss_tiers.py — pay to make a boss dangerous, and to shorten the odds.

Every boss win already drops a rolled copy, and `boss_copy.py` already grades
that copy on a ladder ending in **Perfect** — full stats, complete kit,
guaranteed awakening — at one in ten million. Its own comment calls that "a
legend to chase, not a reward anyone will actually receive".

This module is the dial. Five tiers: tier 1 is free and is *exactly* what a
boss fight is today, and each rung above it makes the boss meaningfully harder
while shortening the Perfect roll and lifting the whole grade ladder.

Two things about the design worth stating, because both were deliberate.

**The lottery is not the reward.** Even at the top tier a Perfect is one clear
in twenty thousand — most players will never see one, which is the point of a
chase item. What a paying player actually *feels* is `bands`: at Nightmare a
Pristine or Flawless copy is the common outcome rather than the exception. If
the tiers only moved `perfect_odds` the top tier would be an expensive way to
buy a lottery ticket nobody wins.

**Tier 1 must be a no-op.** A player who ignores this feature entirely gets the
fight they got yesterday: same AI rung, same HP, same rewards, same 1-in-10M.
Anything else makes a paid feature into a stealth nerf of the free one.

The AI rung is not a fixed name per tier — it *walks* from the boss's own
configured `cfg["difficulty"]`, so a boss authored as `legend` starts harder
than one authored as `elite`, and no tier ever hands you a boss easier than the
one that shipped.
"""
from __future__ import annotations

from typing import Optional

# The existing ladder in boss_ai.DIFFICULTY, easiest first. Imported lazily in
# `walk_difficulty` so this module stays importable by tools that don't want
# the whole AI.
ORDER = ("rookie", "veteran", "elite", "legend", "nightmare")

# Grade bands, per tier. Same shape as boss_copy.GRADE_BANDS —
# (weight, min_penalty, max_penalty, grade) — with the weight mass walked
# from the junk end toward the good end. Tier 1 is byte-identical to
# boss_copy.GRADE_BANDS and is asserted to be.
_BANDS_STANDARD = [
    (600, 120, 165, "Flawed"),
    (300,  80, 130, "Standard"),
    (85,   35,  80, "Refined"),
    (14,    8,  35, "Pristine"),
    (1,     0,   8, "Flawless"),
]
_BANDS_HARDENED = [
    (420, 120, 165, "Flawed"),
    (380,  80, 130, "Standard"),
    (160,  35,  80, "Refined"),
    (35,    8,  35, "Pristine"),
    (5,     0,   8, "Flawless"),
]
_BANDS_SAVAGE = [
    (220, 120, 165, "Flawed"),
    (380,  80, 130, "Standard"),
    (300,  35,  80, "Refined"),
    (85,    8,  35, "Pristine"),
    (15,    0,   8, "Flawless"),
]
_BANDS_MERCILESS = [
    (80,  120, 165, "Flawed"),
    (260,  80, 130, "Standard"),
    (420,  35,  80, "Refined"),
    (200,   8,  35, "Pristine"),
    (40,    0,   8, "Flawless"),
]
_BANDS_NIGHTMARE = [
    (20,  120, 165, "Flawed"),
    (130,  80, 130, "Standard"),
    (350,  35,  80, "Refined"),
    (400,   8,  35, "Pristine"),
    (100,   0,   8, "Flawless"),
]

# key, label, emoji, price, rungs above the boss's own, hp x, atk x,
# perfect odds (1 in N), reward x, bands
TIERS: dict[str, dict] = {
    "standard": {
        "key": "standard", "label": "Standard", "emoji": "⚪",
        "price": 0, "rungs": 0, "hp_mult": 1.00, "atk_mult": 1.00,
        "perfect_odds": 10_000_000, "reward_mult": 1.0,
        "bands": _BANDS_STANDARD,
        "blurb": "The fight as it has always been. Costs nothing.",
    },
    "hardened": {
        "key": "hardened", "label": "Hardened", "emoji": "🟢",
        "price": 25_000, "rungs": 1, "hp_mult": 1.25, "atk_mult": 1.10,
        "perfect_odds": 2_000_000, "reward_mult": 1.25,
        "bands": _BANDS_HARDENED,
        "blurb": "It stops blundering quite so often.",
    },
    "savage": {
        "key": "savage", "label": "Savage", "emoji": "🔵",
        "price": 75_000, "rungs": 2, "hp_mult": 1.55, "atk_mult": 1.20,
        "perfect_odds": 400_000, "reward_mult": 1.6,
        "bands": _BANDS_SAVAGE,
        "blurb": "It reads your habits and hits back harder.",
    },
    "merciless": {
        "key": "merciless", "label": "Merciless", "emoji": "🟣",
        "price": 200_000, "rungs": 4, "hp_mult": 1.90, "atk_mult": 1.32,
        "perfect_odds": 80_000, "reward_mult": 2.0,
        "bands": _BANDS_MERCILESS,
        "blurb": "No mistakes. Nearly twice the health.",
    },
    "nightmare": {
        "key": "nightmare", "label": "Nightmare", "emoji": "🔴",
        "price": 500_000, "rungs": 4, "hp_mult": 2.40, "atk_mult": 1.45,
        "perfect_odds": 20_000, "reward_mult": 2.5,
        "bands": _BANDS_NIGHTMARE,
        "blurb": "Bring friends. The best odds in the game.",
    },
}

DEFAULT_TIER = "standard"
ORDERED = ("standard", "hardened", "savage", "merciless", "nightmare")


class TierError(Exception):
    """Refused purchase. Carries the player-facing reason."""


def get(key: Optional[str]) -> dict:
    """The tier config. Any unknown value resolves to the free tier.

    Fails SAFE rather than closed: a stale button, a renamed key or a
    half-applied deploy gives somebody a free standard fight, never a paid
    tier they didn't buy or a crash mid-lobby.
    """
    return TIERS.get(str(key or "").strip().lower(), TIERS[DEFAULT_TIER])


def price_of(key: Optional[str]) -> int:
    return int(get(key)["price"])


def walk_difficulty(base: Optional[str], key: Optional[str]) -> str:
    """The AI rung for this tier, walked up from the boss's OWN difficulty.

    A boss configured `elite` at Savage (+2) becomes `nightmare`; the same tier
    on a `legend` boss is also `nightmare`, because the ladder ends there. The
    result is never below `base` — paying can only ever make the fight harder.
    """
    tier = get(key)
    try:
        i = ORDER.index(str(base or "").strip().lower())
    except ValueError:
        # Unknown configured difficulty — treat it as the middle rung rather
        # than the easiest, so a typo in a boss profile can't hand out a
        # rookie AI at Nightmare prices.
        i = ORDER.index("elite")
    return ORDER[min(len(ORDER) - 1, i + int(tier["rungs"]))]


def scale_hp(hp: int, key: Optional[str]) -> int:
    return int(round(int(hp) * get(key)["hp_mult"]))


def scale_attack(attack: float, key: Optional[str]) -> float:
    return float(attack) * get(key)["atk_mult"]


def scale_reward(amount: int, key: Optional[str]) -> int:
    return int(round(int(amount) * get(key)["reward_mult"]))


def charge(profile: dict, key: Optional[str]) -> int:
    """Deduct this tier's price. Mutates `profile`. Returns what was spent.

    Call this INSIDE `database.mutate_user`, never around it. The balance has
    to be re-read under the lock — a lobby card can sit on screen for 45
    seconds, and the number printed on it is a display, not an authority — and
    raising has to abandon the whole read-modify-write, so a refused purchase
    cannot take the coins without granting the tier.
    """
    tier  = get(key)
    price = int(tier["price"])
    if price <= 0:
        return 0
    coins = int(profile.get("coins", 0) or 0)
    if coins < price:
        raise TierError(
            f"**{tier['label']}** costs 🪙 **{price:,}** — you have "
            f"**{coins:,}**, short by **{price - coins:,}**.")
    profile["coins"] = coins - price
    return price


def charge_for(player_id: int, key: Optional[str]) -> int:
    """`charge` under the user lock. Raises TierError if they can't pay."""
    from utils.database import mutate_user
    return mutate_user(int(player_id), lambda prof: charge(prof, key))


def can_afford(profile: dict, key: Optional[str]) -> bool:
    return int(profile.get("coins", 0) or 0) >= price_of(key)


def summary_line(key: Optional[str]) -> str:
    """One line for a lobby card / embed."""
    t = get(key)
    price = "free" if not t["price"] else f"🪙 {t['price']:,}"
    return (f"{t['emoji']} **{t['label']}** — {price}  ·  "
            f"❤️ ×{t['hp_mult']:g}  ·  ⚔️ ×{t['atk_mult']:g}  ·  "
            f"👑 1 in {t['perfect_odds']:,}")
