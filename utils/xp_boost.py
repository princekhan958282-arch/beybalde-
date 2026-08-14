"""
utils/xp_boost.py — the EXP Surge: ten times EXP, for an hour.

Not called a "booster". `;buy booster` already means the Beyblade gacha pack,
and a second meaning for the word would make that command ambiguous.

Why time-boxed rather than charges
----------------------------------
Both the battle and story reward paths do the same three things in the same
order, and both carry a comment explaining why:

    award(profile, ...)      # bey EXP, on the profile dict already in hand
    update_user(id, profile) # persist
    grant_xp(id, ...)        # trainer EXP — RE-READS the profile

A charge counter would be decremented by `award` on the in-hand dict and again
by `grant_xp` on a fresh read: one battle burns two charges. Interleave it the
other way and `update_user` clobbers `grant_xp`'s decrement, so the charge is
never spent at all. A timestamp is only ever READ by both, so the ordering
stops mattering. That is the whole reason for the design.

What it multiplies
------------------
Battles, story and boss wins are boosted on BOTH tracks. Chat is boosted on
BEY EXP ONLY — trainer EXP from chat stays at its normal rate.

That split is deliberate and it has a consequence worth stating out loud:
chat EXP has no cooldown (`XP_CHAT_COOLDOWN_S = 0`), so under a Surge a bey
reaches level 100 in roughly 112 messages. Trainer level, which gates far
more, is protected from that; bey level is not, by choice.

Deliberately discord-free — `utils/bey_levels.py` says it must be importable
from the parts of the bot that cannot import discord, and it imports this.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("beyblade_bot.xp_boost")

# "1000% EXP" as ten times the EXP, not eleven. Named as a multiplier rather
# than stored as `1000` so no reader has to guess which convention was meant.
XP_SURGE_MULT = 10.0
XP_SURGE_SECONDS = 3600
XP_SURGE_PRICE = 400_000

K_SURGE_UNTIL = "xp_surge_until"


class SurgeError(Exception):
    """A refused purchase, carrying the player-facing reason."""


def surge_until(profile: dict, now: float | None = None) -> int:
    """Unix second the Surge ends, or 0 if none is running.

    Takes `now` like everything else here. It used to read `time.time()`
    directly, which meant `buy()` — which does accept an injected clock —
    consulted the real wall clock to decide whether to EXTEND, and so never
    extended when driven from a test on a frozen clock. Same class of bug in
    production if the two ever disagreed.

    Reads only — never strips the key. An expired stamp is harmless and
    rewriting the profile on every EXP grant would turn a read into a write on
    the hottest path in the bot.
    """
    try:
        end = int((profile or {}).get(K_SURGE_UNTIL, 0) or 0)
    except (TypeError, ValueError):
        return 0
    return end if end > (time.time() if now is None else float(now)) else 0


def is_active(profile: dict, now: float | None = None) -> bool:
    try:
        end = int((profile or {}).get(K_SURGE_UNTIL, 0) or 0)
    except (TypeError, ValueError):
        return False
    return end > (time.time() if now is None else float(now))


def multiplier(profile: dict, now: float | None = None) -> float:
    """`XP_SURGE_MULT` while a Surge runs, else 1.0."""
    return XP_SURGE_MULT if is_active(profile, now) else 1.0


def apply(amount: int, profile: dict, now: float | None = None) -> int:
    """`amount` scaled by any live Surge. Negative/zero passes through."""
    try:
        amt = int(amount)
    except (TypeError, ValueError):
        return 0
    if amt <= 0:
        return amt
    return int(round(amt * multiplier(profile, now)))


def buy(profile: dict, now: float | None = None) -> dict:
    """Charge for a Surge and start (or extend) it. Mutates `profile`.

    Call this INSIDE `database.mutate_user`: the balance is re-read under the
    lock, and raising abandons the whole write so a refusal can never take the
    coins without starting the Surge.

    Buying while one is already running EXTENDS it rather than overlapping —
    two Surges at once would still be 10x, so overlapping would silently
    charge 400,000 for nothing.
    """
    now = time.time() if now is None else float(now)
    coins = int((profile or {}).get("coins", 0) or 0)
    if coins < XP_SURGE_PRICE:
        raise SurgeError(
            f"An **EXP Surge** costs 🪙 **{XP_SURGE_PRICE:,}** — you have "
            f"**{coins:,}**, short by **{XP_SURGE_PRICE - coins:,}**.")

    base = max(now, float(surge_until(profile, now) or now))
    profile["coins"] = coins - XP_SURGE_PRICE
    profile[K_SURGE_UNTIL] = int(base + XP_SURGE_SECONDS)
    return {
        "spent": XP_SURGE_PRICE,
        "coins": profile["coins"],
        "until": profile[K_SURGE_UNTIL],
        "extended": base > now,
    }


def buy_for(player_id: int) -> dict:
    """`buy` under the user lock."""
    from utils.database import mutate_user
    return mutate_user(int(player_id), buy)
