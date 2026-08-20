"""
story_data.py — the School League. Data only; imports no discord.

What changed, and why the old table is gone
-------------------------------------------
Story Mode used to be twelve stages across three chapters, each naming a human
PERSONA ("Kenta", "Alley King Doji") whose stats were derived from a four-row
archetype table. There was no blade on the other side of the fight at all — so
there was nothing for the ability engine to run, and nothing for a named
Special to belong to.

The School League fields **real blades from the roster, at level 100**, in a
real `BattleSession`. That is the whole point: Omni Odax's Blast Beat banks
beats against you, Hyper Horusood's field strips your stability, King Kerbeus
actually blocks. None of that was reachable before.

Difficulty is AI, not stat inflation
------------------------------------
Both runs field the same blades at the same level. What changes is the rung on
`boss_ai.DIFFICULTY`, which is a real ladder of four levers — search depth,
opponent modelling, blunder rate and how much of your history it reads:

    Normal    -> elite      IQ 3   blunder 0.12   read 0.60
    Nightmare -> nightmare  IQ 5   blunder 0.00   read 1.00

A Nightmare opponent never throws a move away and remembers everything you
have done. It does not get a single extra point of Attack.

Rewards
-------
One-time per battle per difficulty. A cleared battle stays replayable — for
practice, for a blade's EXP, to test a build — and pays nothing the second
time. The totals are exact:

    Normal     600 .. 2,550   = 12,000
    Nightmare  x2.5 of Normal = 30,000

Nightmare is gated behind clearing all eight on Normal, so those 30,000 are an
endgame payout rather than a shortcut past the League.
"""

from __future__ import annotations

from typing import Optional

# ── Difficulty ────────────────────────────────────────────────────────────────
# Keys into `cogs/battle/boss/boss_ai.py:DIFFICULTY`. Named here rather than
# inlined so the two modes have one definition each and the League cannot drift
# from the ladder it is built on.
NORMAL = "normal"
NIGHTMARE = "nightmare"

DIFFICULTIES = (NORMAL, NIGHTMARE)

# `elite` is IQ 3, which is the level asked for. `nightmare` is IQ 5.
AI_RUNG = {
    NORMAL:    "elite",
    NIGHTMARE: "nightmare",
}

DIFFICULTY_LABEL = {
    NORMAL:    ("🏫", "Normal"),
    NIGHTMARE: ("💀", "Nightmare"),
}

# Every League blade is fielded at this level, on both difficulties.
OPPONENT_LEVEL = 100

# Points needed to take a battle. Story's own constant, deliberately NOT
# `ranked.MATCH_TARGET`: the two happen to agree at 3 today, and a future
# change to the ranked ladder must not silently retune Story. How each point is
# AWARDED is still ranked's rule — see `story_match.py`.
VICTORY_TARGET = 3

# A stalemate has to end somewhere. At 2 points a round this is comfortably
# more rounds than a decided battle needs.
MAX_ROUNDS = 9

# Nightmare pays this multiple of the Normal reward for the same battle.
# 12,000 x 2.5 = 30,000 exactly.
NIGHTMARE_REWARD_MULT = 2.5


# ── The league ────────────────────────────────────────────────────────────────
# `blade` must name an entry in data/beyblades.json — asserted by the suite,
# because a typo here is an opponent that cannot be built and a battle that
# cannot start.
SCHOOL_LEAGUE: list[dict] = [
    {"n": 1, "blade": "Rising Ragnaruk",     "coins": 600,
     "blurb": "First bell. A steady spin that simply refuses to stop."},
    {"n": 2, "blade": "King Kerbeus",        "coins": 850,
     "blurb": "Three heads, one wall. Nothing gets through cheaply."},
    {"n": 3, "blade": "Hollow Deathscyther", "coins": 1100,
     "blurb": "The scythe finds the gap your guard leaves open."},
    {"n": 4, "blade": "Hyper Horusood",      "coins": 1350,
     "blurb": "It never stoops. It just takes the floor out from under you."},
    {"n": 5, "blade": "Wild Wyvern",         "coins": 1600,
     "blurb": "Armour with a temper. Hit it hard enough and it hits back harder."},
    {"n": 6, "blade": "Omni Odax",           "coins": 1850,
     "blurb": "It fights on the beat. Let it find the tempo and the drop lands."},
    {"n": 7, "blade": "Victory Valkyrie",    "coins": 2100,
     "blurb": "The school's ace. Fast, direct, and out of patience."},
    {"n": 8, "blade": "Storm Spriggan",      "coins": 2550,
     "blurb": "Final bell. Balanced, unhurried, and better than you."},
]

# ── Chapters ──────────────────────────────────────────────────────────────────
SCHOOL = "school"
XENDER = "xender"

CHAPTERS: list[dict] = [
    {"key": SCHOOL, "emoji": "🏫", "name": "Beyblade Burst School",
     "available": True,
     "blurb": "Eight opponents, one league. Beat each to open the next."},
    {"key": XENDER, "emoji": "🔒", "name": "Xender Dojo — Coming Soon",
     "available": False,
     "blurb": "Not open yet."},
]


# ── Profile key ───────────────────────────────────────────────────────────────
# New key. The old `story_cleared` / `story_stats` are deliberately left on
# file untouched: deleting a field from thousands of live profiles to tidy up
# is a migration with no upside, and the old campaign could be revived.
K_LEAGUE = "school_league"


def _progress(profile: dict) -> dict:
    raw = (profile or {}).get(K_LEAGUE)
    if not isinstance(raw, dict):
        return {}
    return raw


def cleared(profile: dict, difficulty: str) -> set[int]:
    """Battle numbers this player has already won on `difficulty`."""
    raw = _progress(profile).get(difficulty)
    out: set[int] = set()
    for v in (raw or []):
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return out


def record_clear(profile: dict, difficulty: str, n: int) -> bool:
    """Mark battle `n` cleared. True if this was the FIRST clear."""
    prog = dict(_progress(profile))
    done = sorted(cleared(profile, difficulty) | {int(n)})
    first = int(n) not in cleared(profile, difficulty)
    prog[difficulty] = done
    profile[K_LEAGUE] = prog
    return first


def battle(n) -> Optional[dict]:
    """The League entry numbered `n`, or None.

    Anything that is not a battle number — a name, a stage code left over from
    the old campaign, None — is a miss rather than a ValueError. Every caller
    already handles None, and one of them is a user-typed argument.
    """
    try:
        want = int(n)
    except (TypeError, ValueError):
        return None
    for b in SCHOOL_LEAGUE:
        if b["n"] == want:
            return b
    return None


def total_battles() -> int:
    return len(SCHOOL_LEAGUE)


def reward_for(n: int, difficulty: str) -> int:
    """Coins for a FIRST clear of battle `n` on `difficulty`."""
    b = battle(n)
    if b is None:
        return 0
    base = int(b["coins"])
    if difficulty == NIGHTMARE:
        return int(round(base * NIGHTMARE_REWARD_MULT))
    return base


def total_reward(difficulty: str) -> int:
    return sum(reward_for(b["n"], difficulty) for b in SCHOOL_LEAGUE)


def normal_complete(profile: dict) -> bool:
    return len(cleared(profile, NORMAL)) >= total_battles()


def is_unlocked(profile: dict, n: int, difficulty: str) -> bool:
    """Battle `n` is open on `difficulty` when its predecessor is cleared.

    Nightmare has one extra gate in front of the whole run: the entire League
    on Normal. It is the endgame lap, not a shortcut to the bigger payout.
    """
    entry = battle(n)
    if entry is None:
        return False
    n = int(entry["n"])
    if difficulty == NIGHTMARE and not normal_complete(profile):
        return False
    if n <= 1:
        return True
    return (n - 1) in cleared(profile, difficulty)


def lock_reason(profile: dict, n: int, difficulty: str) -> str:
    """Why this battle is shut, or "" when it is open."""
    if is_unlocked(profile, n, difficulty):
        return ""
    if battle(n) is None:
        return f"There is no battle {n}."
    if difficulty == NIGHTMARE and not normal_complete(profile):
        done = len(cleared(profile, NORMAL))
        return (f"💀 Nightmare opens when the whole League is clear on Normal "
                f"— **{done}/{total_battles()}** done.")
    n = int(n)
    prev = battle(n - 1)
    if prev is None:
        return f"🔒 Battle **{n}** is locked."
    return (f"🔒 Battle **{n}** is locked. Beat battle **{n - 1} · "
            f"{prev['blade']}** first.")


def next_battle(profile: dict, difficulty: str) -> Optional[int]:
    """The lowest battle not yet cleared on this difficulty."""
    done = cleared(profile, difficulty)
    for b in SCHOOL_LEAGUE:
        if b["n"] not in done:
            return b["n"]
    return None
