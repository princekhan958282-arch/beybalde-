"""
bey_levels.py — per-bey levels with Pokémon-style stat progression.

The formula, as specified:

    Stat = floor(Base + (Growth × Level) + IV + CurveBonus)
    CurveBonus = floor(Level / 5) * 2

Linear, not exponential. Each bey scales differently because Growth comes from
its TYPE archetype, so an Attack bey and a Defense bey pull apart as they level
instead of everything rising by the same percentage.

Two things worth knowing before tuning
--------------------------------------
1. `Growth × Level` means a level-1 bey is ALREADY above its base stats — a
   fresh bey is stronger than the same bey is today. `LEVEL_OFFSET` controls
   this: 0 follows the spec literally, 1 makes level 1 equal the base stats and
   preserves the game's current balance as the floor. Set to 1 here; flip it
   for the literal reading.

2. STAT_CAP has to clear the whole growth curve or it silently deletes the top
   of the game. Attack archetypes grow at 3.0/level and this roster's attack
   stats already average 97 and reach 160, so a 300 cap was reached around
   level 45-60 and every level after that was dead. Worse, once most blades sit
   ON the cap they are all the same blade. `report_cap_pressure()` prints
   exactly where each stat caps, per bey, so this can be checked against real
   numbers rather than guessed — run it after any growth-rate change.

IVs are per OWNED bey, not per species: two players' copies of the same blade
differ. They're rolled once, on first sight, and stored in the profile.
"""

from __future__ import annotations

import math
import random
from typing import Optional

MAX_LEVEL = 100
# Raised from 300, which was binding hard enough to erase the roster. Measured
# across all 78 blades at level 100: at 300, HP had thirteen distinct values
# with 51 blades sitting exactly on the cap, and attack had 53 of 78 capped —
# every maxed bey was effectively the same bey. At 500 nothing caps at all
# (0/78 on hp, attack and defence; 1/78 on stamina) and the distinct-value
# counts run 50-65. 600 was measured too and buys exactly one more blade, so
# 500 is where the cap stops mattering.
#
# The growth rates below were NOT changed: the cap was the problem, not the
# curve, and lowering growth to fit a small cap would have flattened the
# archetypes instead.
STAT_CAP = 500
IV_MAX = 10
LEVEL_OFFSET = 1          # see note 1 above

# Total XP needed to REACH a level. Quadratic, matching the trainer/mastery
# curves already in the bot. At 8 * L^2, level 100 is 80,000 XP — roughly a
# thousand battle wins, or a long time chatting, which is the intended shape
# for a 100-level ceiling.
XP_BASE = 8

STATS = ("hp", "attack", "defense", "stamina", "special")

# Growth per level, by type. These are the archetypes as specified.
#
# `special` was added later. It used to be excluded from STATS, so it was the
# one stat that never grew — and since damage_rules.resolve_special read a
# hardcoded number out of beyblades.json rather than the stat, growing it alone
# would have changed nothing. Both halves are needed; see resolve_special.
# Attack archetypes grow it fastest, matching how they already lead on attack.
GROWTH = {
    "attack":  {"hp": 1.5, "attack": 3.0, "defense": 0.8, "stamina": 1.2, "special": 2.6},
    "defense": {"hp": 2.8, "attack": 1.0, "defense": 3.0, "stamina": 1.5, "special": 1.6},
    "stamina": {"hp": 2.2, "attack": 1.3, "defense": 1.5, "stamina": 3.2, "special": 1.8},
    "balance": {"hp": 2.0, "attack": 2.0, "defense": 2.0, "stamina": 2.0, "special": 2.0},
}
DEFAULT_GROWTH = GROWTH["balance"]

# EXP rewards. Raised to a 50-459 band so levelling moves at a visible pace —
# the old 5-10 chat award meant roughly 10,400 messages to reach Lv100.
# The band is spread across sources rather than applied flat: chatting is the
# steady trickle, a win is the payoff, a loss still pays so a losing streak
# isn't dead time.
XP_CHAT = (50, 90)
XP_CHAT_COOLDOWN_S = 0        # no cooldown — every qualifying message pays
XP_BATTLE_WIN = (300, 459)
XP_BATTLE_LOSS = (120, 220)

NOTIFY_EVERY = 5          # a level-up ping every 5 levels


# ── curve ─────────────────────────────────────────────────────────────────────

def xp_for_level(level: int) -> int:
    """Total XP needed to reach `level`."""
    level = max(1, min(MAX_LEVEL, int(level)))
    return XP_BASE * (level - 1) * (level - 1)


def level_from_xp(xp: int) -> int:
    if xp <= 0:
        return 1
    return min(MAX_LEVEL, 1 + int(math.isqrt(int(xp) // XP_BASE)))


def progress(xp: int) -> tuple[int, int, int]:
    """(level, xp_into_level, xp_needed_for_next). Needed is 0 at max."""
    lvl = level_from_xp(xp)
    if lvl >= MAX_LEVEL:
        return lvl, 0, 0
    floor_xp = xp_for_level(lvl)
    next_xp = xp_for_level(lvl + 1)
    return lvl, int(xp) - floor_xp, next_xp - floor_xp


# ── stats ─────────────────────────────────────────────────────────────────────

def growth_for(blade: dict) -> dict[str, float]:
    return GROWTH.get(str(blade.get("type", "")).strip().lower(), DEFAULT_GROWTH)


def roll_ivs(rng: Optional[random.Random] = None) -> dict[str, int]:
    r = rng or random
    return {s: r.randint(0, IV_MAX) for s in STATS}


def curve_bonus(level: int) -> int:
    return (max(1, int(level)) // 5) * 2


def stat_at(base: float, growth: float, level: int, iv: int) -> int:
    """One stat at one level. floor()ed and capped, per spec."""
    level = max(1, min(MAX_LEVEL, int(level)))
    raw = base + growth * (level - LEVEL_OFFSET) + iv + curve_bonus(level)
    return int(min(STAT_CAP, math.floor(raw)))


def stats_at(blade: dict, level: int, ivs: Optional[dict] = None) -> dict[str, int]:
    """Every stat for a blade at a level.

    Any stat the blade carries that the growth table doesn't cover is passed
    through unchanged rather than dropped — losing one here would silently
    disarm whatever reads it.
    """
    base = dict(blade.get("stats") or {})
    g = growth_for(blade)
    ivs = ivs or {}
    out = dict(base)
    for s in STATS:
        if s not in base:
            continue
        out[s] = stat_at(float(base[s]), g.get(s, 0.0), level, int(ivs.get(s, 0)))
    return out


def gains_at(blade: dict, level: int, ivs: Optional[dict] = None) -> dict[str, int]:
    """What levelling INTO `level` actually added, per spec's level-up feedback."""
    if level <= 1:
        return {s: 0 for s in STATS if s in (blade.get("stats") or {})}
    now = stats_at(blade, level, ivs)
    was = stats_at(blade, level - 1, ivs)
    return {s: now[s] - was[s] for s in now if s in was and s in STATS}


# ── profile storage ───────────────────────────────────────────────────────────

def _bucket(profile: dict) -> dict:
    return profile.setdefault("bey_progress", {})


def entry_for(profile: dict, blade_name: str,
              rng: Optional[random.Random] = None) -> dict:
    """This player's record for this bey, rolling IVs the first time.

    IVs are rolled once and stored. Deriving them from the name instead would
    make every player's copy of a blade identical, which defeats the point.
    """
    bucket = _bucket(profile)
    key = str(blade_name)
    e = bucket.get(key)
    if not e:
        e = {"xp": 0, "ivs": roll_ivs(rng)}
        bucket[key] = e
    elif "ivs" not in e:
        e["ivs"] = roll_ivs(rng)
    return e


def level_of(profile: dict, blade_name: str) -> int:
    e = (profile.get("bey_progress") or {}).get(str(blade_name))
    return level_from_xp(e.get("xp", 0)) if e else 1


def award(profile: dict, blade_name: str, amount: int) -> dict:
    """Add XP to one bey. Returns a summary of what happened.

    Mutates `profile` — the caller persists it. Returning the summary rather
    than sending a message keeps this importable from anywhere, including the
    parts of the bot that must not import discord.
    """
    e = entry_for(profile, blade_name)
    before_xp = int(e.get("xp", 0))
    before = level_from_xp(before_xp)
    e["xp"] = before_xp + max(0, int(amount))
    after = level_from_xp(e["xp"])

    return {
        "blade": blade_name,
        "gained": max(0, int(amount)),
        "xp": e["xp"],
        "old_level": before,
        "level": after,
        "leveled": after > before,
        # A ping only every Nth level, as asked — crossing several at once
        # still counts if any of them was a milestone.
        "milestone": after > before and (after // NOTIFY_EVERY) > (before // NOTIFY_EVERY),
        "ivs": e["ivs"],
    }


# ── tuning aid ────────────────────────────────────────────────────────────────

def cap_level(blade: dict, stat: str, ivs: Optional[dict] = None) -> Optional[int]:
    """First level at which `stat` hits STAT_CAP, or None if it never does."""
    base = (blade.get("stats") or {}).get(stat)
    if base is None:
        return None
    g = growth_for(blade).get(stat, 0.0)
    ivs = ivs or {}
    for lvl in range(1, MAX_LEVEL + 1):
        if stat_at(float(base), g, lvl, int(ivs.get(stat, 0))) >= STAT_CAP:
            return lvl
    return None


def report_cap_pressure(blades: dict) -> list[tuple[str, str, int]]:
    """(blade, stat, level) for every stat that caps before MAX_LEVEL.

    A capped stat means the levels after it are dead for that stat, which is
    the main thing to watch when choosing STAT_CAP and growth rates.
    """
    out = []
    for name, blade in blades.items():
        for s in STATS:
            lvl = cap_level(blade, s)
            if lvl is not None and lvl < MAX_LEVEL:
                out.append((name, s, lvl))
    return sorted(out, key=lambda r: r[2])
