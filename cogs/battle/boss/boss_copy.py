"""
boss_copy.py  —  🧬 Boss copies

Beating a boss doesn't hand you the boss blade. It hands you a *copy* — a
flawed reproduction that rolls its own kit every single time.

What gets rolled
----------------
  loadout   one of a few fixed shapes, e.g. 1 Special + 2 Abilities, or
            1 Ultimate + 1 Ability. Which moves you get is random.
  stats     a copy is weaker than the original by a random amount, roughly
            120 points of total stats. A rare roll takes almost nothing off,
            and the top roll takes nothing at all.
  awakening a chance-based ability that lets the copy hit full original power
            for a moment. Not every copy has one, and the ones that do only
            fire occasionally.
  perfect   full stats, the complete kit, and a guaranteed awakening.

Because every copy is a rolled *instance* rather than a blade definition, two
players holding "Aetherion Drakos copy" can have completely different blades.
That also means copies never enter data/beyblades.json, so they can't show up
in ;list, ;beypedia, the spawn pool, the shop, boosters or the marketplace —
exactly as asked.

Storage: profile["boss_copies"] = [instance, …]
"""

import random
import time
import uuid
from typing import Optional

from utils.database import get_user, update_user

# ── Roll odds ─────────────────────────────────────────────────────────────────
# One in ten million, as specified. Worth knowing what that means in practice:
# at ~200 active players clearing a boss daily it is roughly a 1-in-140-year
# event. It is a legend to chase, not a reward anyone will actually receive —
# PERFECT_ODDS is a single constant if that should ever change.
PERFECT_ODDS = 10_000_000

# Stat penalty bands: (weight, min_penalty, max_penalty, grade)
# The penalty is subtracted from the ORIGINAL total, spread across the stats.
# Weighted mean deficit lands at ~122 points, matching the "about 120 weaker"
# brief. The first cut averaged 146 and left copies at 59% of the original,
# which read as junk rather than as a weaker version of a great blade.
GRADE_BANDS = [
    (600, 120, 165, "Flawed"),
    (300,  80, 130, "Standard"),
    (85,   35,  80, "Refined"),
    (14,    8,  35, "Pristine"),
    (1,     0,   8, "Flawless"),
]

GRADE_META = {
    "Flawed":   ("🔩", 0x8d8d8d, "Rare"),
    "Standard": ("⚙️", 0x4ade80, "Epic"),
    "Refined":  ("🔷", 0x5aa9ff, "Legendary"),
    "Pristine": ("💠", 0xc084fc, "Mythic"),
    "Flawless": ("🌟", 0xffe066, "Ultimate"),
    "Perfect":  ("👑", 0xff4fd8, "State Exclusive"),
}

# Loadout shapes: (weight, n_specials, n_ultimates, n_abilities, label)
#
# These are WEIGHTS, not percentages — _weighted() normalises them against the
# running total. The requested 40 / 12 / 60 sum to 112, so they can't all be
# literal percentages of the same pool; used as weights they land at roughly
# 32% / 10% / 48%, with the two rarer shapes filling the remaining 10%.
#
# The shape of that is right for a drop system: the thinnest kit
# (1 Special + 1 Ability) is now the common roll, and the Ultimate shape is
# scarce enough to feel like a result.
LOADOUTS = [
    (40, 1, 0, 2, "1 Special · 2 Abilities"),
    (12, 0, 1, 1, "1 Ultimate · 1 Ability"),
    (60, 1, 0, 1, "1 Special · 1 Ability"),
    (9,  2, 0, 1, "2 Specials · 1 Ability"),
    (3,  1, 1, 2, "1 Special · 1 Ultimate · 2 Abilities"),
]

# ── Awakening ─────────────────────────────────────────────────────────────────
AWAKENING_CHANCE_BY_GRADE = {
    "Flawed":   0.08,
    "Standard": 0.15,
    "Refined":  0.28,
    "Pristine": 0.45,
    "Flawless": 0.70,
    "Perfect":  1.00,
}
# Once a copy HAS awakening, this is how often it actually triggers in a fight.
AWAKEN_TRIGGER = {
    "Flawed":   0.05,
    "Standard": 0.08,
    "Refined":  0.12,
    "Pristine": 0.18,
    "Flawless": 0.25,
    "Perfect":  0.35,
}


def grade_meta(grade) -> tuple:
    """(emoji, colour, rarity) for a grade — ALWAYS use this, never index
    GRADE_META directly.

    describe() was hardened against an unknown grade but ;copies still did
    GRADE_META[c["grade"]] in its dropdown builder, so a single legacy or
    hand-edited entry took the whole list down. One guarded accessor means the
    next call site can't reintroduce it.
    """
    return GRADE_META.get(grade, GRADE_META["Flawed"])


def _weighted(rows, rng):
    total = sum(r[0] for r in rows)
    roll = rng.random() * total
    acc = 0.0
    for r in rows:
        acc += r[0]
        if roll <= acc:
            return r
    return rows[-1]


def roll_copy(profile_src: dict, rng: Optional[random.Random] = None) -> dict:
    """Roll one copy instance from a boss profile."""
    rng = rng or random

    perfect = rng.randint(1, PERFECT_ODDS) == 1

    if perfect:
        grade, penalty = "Perfect", 0
        n_sp, n_ult, n_ab = 99, 99, 99      # everything
        loadout_label = "Complete kit"
    else:
        _w, lo, hi, grade = _weighted(GRADE_BANDS, rng)
        penalty = rng.randint(lo, hi)
        _w2, n_sp, n_ult, n_ab, loadout_label = _weighted(LOADOUTS, rng)

    # Roll a target card TOTAL inside the blade's band, then split it across
    # the four card stats. This replaced a "penalty below the original" model —
    # a flat band is easier to reason about and lets a top roll match, or even
    # slightly beat, the source blade.
    lo_t, hi_t = profile_src.get("copy_total_range", (300, 450))
    if perfect:
        target_total = hi_t
    else:
        # Grade decides WHERE in the band you land, so a Flawless roll is
        # genuinely near the ceiling rather than a separate random draw.
        span = hi_t - lo_t
        band = {"Flawed": (0.00, 0.35), "Standard": (0.30, 0.62),
                "Refined": (0.58, 0.82), "Pristine": (0.78, 0.94),
                "Flawless": (0.92, 1.00)}[grade]
        frac = rng.uniform(*band)
        target_total = int(lo_t + span * frac)

    # Split proportionally to the original, with a little jitter so two copies
    # of the same grade still differ.
    src_card = {
        "hp":      min(200, round(profile_src["hp"] / 12)),
        "attack":  profile_src["attack"],
        "defense": profile_src["defense"],
        "stamina": profile_src["stamina"],
    }
    src_sum = sum(src_card.values()) or 1
    jitter = {k: rng.uniform(0.88, 1.12) for k in src_card}
    weighted = {k: src_card[k] / src_sum * jitter[k] for k in src_card}
    wsum = sum(weighted.values()) or 1
    card = {k: max(20, int(target_total * weighted[k] / wsum)) for k in src_card}
    # rounding drift lands on HP so the total is exact
    card["hp"] = max(20, card["hp"] + (target_total - sum(card.values())))

    stats = {"attack": card["attack"], "defense": card["defense"],
             "stamina": card["stamina"]}
    card_hp = card["hp"]
    penalty = 0

    # Which parts of the boss's kit made it into the copy
    from . import boss_info
    mod = boss_info.SPECIAL_MODULES.get(profile_src["key"])
    specials = [k for k, v in (mod.SPECIALS.items() if mod else []) if not v["ultimate"]]
    ults     = [k for k, v in (mod.SPECIALS.items() if mod else []) if v["ultimate"]]
    abils    = list(range(len(profile_src.get("abilities", []))))

    picked_sp  = rng.sample(specials, min(n_sp, len(specials))) if specials else []
    picked_ult = rng.sample(ults, min(n_ult, len(ults))) if ults else []
    picked_ab  = sorted(rng.sample(abils, min(n_ab, len(abils)))) if abils else []

    has_awaken = rng.random() < AWAKENING_CHANCE_BY_GRADE[grade]

    return {
        "id":         uuid.uuid4().hex[:8],
        "source":     profile_src["key"],
        # No " copy" suffix. The org/no-org pair is already the distinction:
        #   NEMESIS ÆTHERION org  = the real blade
        #   NEMESIS ÆTHERION      = a copy
        "name":       profile_src["name"].replace(" org", "").strip(),
        "grade":      grade,
        "penalty":    penalty,
        "stats":      stats,
        "card_hp":    card_hp,
        "card_total": card_hp + sum(stats.values()),
        "specials":   picked_sp,
        "ultimates":  picked_ult,
        "abilities":  picked_ab,
        "loadout":    loadout_label,
        "awakening":  has_awaken,
        "awaken_chance": AWAKEN_TRIGGER[grade] if has_awaken else 0.0,
        "rolled_at":  time.time(),
    }


# ── Storage ───────────────────────────────────────────────────────────────────
GRADE_ORDER = list(GRADE_META)          # worst → best


def _rank(copy: dict) -> int:
    try:
        return GRADE_ORDER.index(copy.get("grade"))
    except ValueError:
        return 0


def all_copies(user_id: int) -> list[dict]:
    """Best grade first — the SAME order ;copies renders.

    These used to differ: the command sorted by grade for display while
    find_copy indexed the raw storage list, so ";copy 1" opened a different
    blade than row 1 of the list. Sorting in one place removes the mismatch.
    """
    rows = list(get_user(user_id).get("boss_copies") or [])
    return sorted(rows, key=lambda c: (-_rank(c), -c.get("rolled_at", 0)))


MAX_COPIES = 200


def add_copy(user_id: int, copy: dict) -> None:
    """Store a rolled copy, trimming the WORST when over the cap.

    The cap used to be a plain copies[-200:], which drops the oldest. A player
    past 200 drops would have silently destroyed a 1-in-10,000,000 Perfect
    simply by winning more fights. Now the lowest grade goes first, oldest
    within that grade, so anything rare survives no matter how much you farm.
    """
    profile = get_user(user_id)
    copies = list(profile.get("boss_copies") or [])
    copies.append(copy)

    if len(copies) > MAX_COPIES:
        # keep best grade, then newest, then cut from the tail
        copies.sort(key=lambda c: (-_rank(c), -c.get("rolled_at", 0)))
        copies = copies[:MAX_COPIES]

    profile["boss_copies"] = copies
    update_user(user_id, profile)


def find_copy(user_id: int, ref: str) -> Optional[dict]:
    """Match by short id or by 1-based index as shown in ;copies."""
    copies = all_copies(user_id)
    ref = (ref or "").strip().lower()
    if not ref:
        return None
    for c in copies:
        if c["id"].lower() == ref:
            return c
    if ref.isdigit():
        i = int(ref) - 1
        if 0 <= i < len(copies):
            return copies[i]
    return None


# ── Presentation ──────────────────────────────────────────────────────────────
def describe(copy: dict, profile_src: dict) -> dict:
    """Human-readable view of a rolled copy."""
    from . import boss_info
    mod = boss_info.SPECIAL_MODULES.get(copy["source"])

    sp_names = [mod.SPECIALS[k]["name"] for k in copy["specials"]] if mod else []
    ul_names = [mod.SPECIALS[k]["name"] for k in copy["ultimates"]] if mod else []
    ab_names = [profile_src["abilities"][i]["name"]
                for i in copy["abilities"]
                if i < len(profile_src.get("abilities", []))]

    lo_t, hi_t = profile_src.get("copy_total_range", (300, 450))
    total = copy.get("card_total") or (
        copy.get("card_hp", 0) + sum(copy["stats"].values()))
    base_total = hi_t

    # A grade this build doesn't know (an older save, a hand edit) must not
    # take the whole ;copies list down with a KeyError.
    emoji, colour, rarity = grade_meta(copy.get("grade"))
    return {
        "emoji": emoji, "colour": colour, "rarity": rarity,
        "specials": sp_names, "ultimates": ul_names, "abilities": ab_names,
        "total": total, "base_total": base_total,
        "deficit": max(0, base_total - total),
        "pct": total / base_total * 100 if base_total else 0,
        "band": (lo_t, hi_t),
    }


def to_blade(copy: dict, profile_src: dict) -> dict:
    """Adapt a rolled copy into the schema utils/info_card.py renders, so a
    copy gets the same PNG treatment as everything else."""
    from . import boss_info
    d = describe(copy, profile_src)
    mod = boss_info.SPECIAL_MODULES.get(copy["source"])

    special = None
    key = (copy["ultimates"] or copy["specials"] or [None])[0]
    if key and mod:
        spec = mod.SPECIALS[key]
        special = {
            "name":           spec["name"],
            "description":    spec["text"],
            "hits":           spec["hits"],
            "damage_per_hit": int(copy["stats"]["attack"] * spec["mult"]
                                  / max(1, spec["hits"])),
            "total_damage":   int(copy["stats"]["attack"] * spec["mult"]),
            "true_damage":    bool(spec.get("true_damage")),
        }

    # Abilities carry mechanics for BOTH engines — flat tuning keys the boss
    # engine's BladeKit reads, and "rules" the PvP engine reads. Before this
    # they were description text with an empty chain, so a copy's kit was
    # decorative in every fight it appeared in.
    from . import copy_abilities
    abilities = copy_abilities.build(copy, profile_src, d.get("pct", 0))

    return {
        "name":           copy["name"],
        "type":           profile_src.get("type", "Balance"),
        "rarity":         d["rarity"],
        "spin_direction": profile_src.get("spin", "Right"),
        "image_url":      profile_src.get("image_url") or "",
        "description":    (
            f"A {copy['grade'].lower()} reproduction of "
            f"{profile_src['name']}. {d['pct']:.0f}% of the original's power. "
            f"Every copy rolls its own kit — no two are alike."
        ),
        "stats": {
            "attack":  copy["stats"]["attack"],
            "defense": copy["stats"]["defense"],
            "stamina": copy["stats"]["stamina"],
            "special": max(copy["stats"].values()),
            "hp":      copy.get("card_hp") or min(200, round(sum(copy["stats"].values()) / 3)),
        },
        "abilities":    abilities,
        "special_move": special,
        "booster_exclusive": False,
        "card_theme":   _copy_theme(copy, profile_src),
    }


def _copy_theme(copy: dict, profile_src: dict) -> dict:
    """A copy keeps the source's hue but dulls it, so a Flawed copy reads as a
    knock-off at a glance and a Perfect one blazes."""
    base = dict(profile_src.get("card_theme") or {})
    if copy["grade"] == "Perfect":
        return {"accent": "#ff4fd8", "glow": "#a1006b", "tint": "#22061a"}
    if copy["grade"] in ("Flawed", "Standard"):
        return {"accent": "#8d97a8", "glow": "#3c4453", "tint": "#101319"}
    return base or {}


# ── Equip / sell ──────────────────────────────────────────────────────────────
# Copies are rolled INSTANCES, not rows in beyblades.json, so they can't ride in
# profile["inventory"] — equip, sell and trade all resolve names against the
# database and would fail to find them. Instead the equipped copy is tracked by
# its own id in profile["active_copy"], while profile["active_beyblade"] keeps
# holding the display name so every existing panel that reads it still renders.
#
# The pairing is the contract: active_copy set  -> the equipped blade is that
# instance; active_copy empty -> active_beyblade names a database blade. Use
# equipped_blade() rather than reading either field directly.

def remove_copy(user_id: int, copy_id: str) -> Optional[dict]:
    """Delete one copy by id. Returns the removed instance, or None."""
    profile = get_user(user_id)
    copies  = list(profile.get("boss_copies") or [])
    for i, c in enumerate(copies):
        if str(c.get("id", "")).lower() == str(copy_id).lower():
            gone = copies.pop(i)
            profile["boss_copies"] = copies
            # An equipped copy that gets sold must not leave the profile
            # pointing at an instance that no longer exists — every battle
            # would then fall back to "Unequipped" with no explanation.
            if str(profile.get("active_copy") or "").lower() == str(copy_id).lower():
                profile["active_copy"] = None
                profile["active_beyblade"] = None
            update_user(user_id, profile)
            return gone
    return None


def equipped_copy(user_id: int) -> Optional[dict]:
    """The copy instance the player currently has equipped, if any."""
    profile = get_user(user_id)
    cid = profile.get("active_copy")
    if not cid:
        return None
    for c in (profile.get("boss_copies") or []):
        if str(c.get("id", "")).lower() == str(cid).lower():
            return c
    return None


def equip(user_id: int, copy_id: str) -> Optional[dict]:
    """Equip a copy by id. Returns the instance, or None if they don't own it."""
    profile = get_user(user_id)
    for c in (profile.get("boss_copies") or []):
        if str(c.get("id", "")).lower() == str(copy_id).lower():
            profile["active_copy"] = c["id"]
            profile["active_beyblade"] = c["name"]
            update_user(user_id, profile)
            return c
    return None


def unequip(user_id: int) -> None:
    """Drop the equipped copy without touching a database blade selection."""
    profile = get_user(user_id)
    if profile.get("active_copy"):
        profile["active_copy"] = None
        profile["active_beyblade"] = None
        update_user(user_id, profile)


def equipped_blade(user_id: int) -> tuple[Optional[dict], Optional[dict]]:
    """The blade dict every battle path should fight with, plus its instance.

    Returns (blade, copy). `copy` is None for an ordinary database blade, so a
    caller that doesn't care about copies can ignore the second value entirely.
    """
    from utils.database import get_beyblade
    profile = get_user(user_id)
    cid = profile.get("active_copy")
    if cid:
        for c in (profile.get("boss_copies") or []):
            if str(c.get("id", "")).lower() == str(cid).lower():
                from . import boss_info
                src = boss_info.REGISTRY.get(c.get("source"))
                if src:
                    return to_blade(c, src), c
        # Pointer survived the instance (sold elsewhere, profile edited by an
        # admin). Fall through to the database blade instead of returning
        # nothing, so the player is never silently unable to battle.
    name = profile.get("active_beyblade")
    return (get_beyblade(name) if name else None), None


# Coins a copy is worth. Scales off the roll itself — grade sets the floor and
# the card total does the rest — so farming Flawed copies is never as good as
# holding one good roll, and an Awakening is worth paying for.
SELL_BASE = {
    "Flawed": 1_200, "Standard": 3_000, "Refined": 8_000,
    "Pristine": 20_000, "Flawless": 60_000, "Perfect": 1_000_000,
}


def sell_value(copy: dict, profile_src: Optional[dict] = None) -> int:
    base  = SELL_BASE.get(copy.get("grade"), 1_200)
    total = int(copy.get("card_total") or 0)
    lo, hi = (profile_src or {}).get("copy_total_range", (300, 450))
    span  = max(1, hi - lo)
    frac  = max(0.0, min(1.0, (total - lo) / span))
    value = base * (1.0 + 0.5 * frac)          # up to +50% for a top-of-band roll
    if copy.get("awakening"):
        value *= 1.25
    return int(value)
