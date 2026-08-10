"""
avatar_skills.py — one skill per battle, paid for out of an energy pool.

The rules
---------
An avatar that carries signature skills no longer applies all three at once.
The player commits to ONE before the battle, and pays for it:

    slot 1 ....  25 energy      (the cheapest, and the weakest)
    slot 2 ....  50 energy
    slot 3 ....  75 energy      (the most expensive, and the strongest)

The pool is 100. A casual battle refills it, so a casual player always gets the
skill they picked. A RANKED match does not refill until the whole match is
decided — 100 energy has to cover every round of a first-to-3-points match, so
the pick is a budget, not a free choice:

    slot 1 four times  ·  slot 2 twice  ·  slot 3 once  ·  or any mix under 100

Running dry is a legal state, not an error: the battle starts with the card's
level bonuses and no skill.

Why slot order is the price ladder
----------------------------------
Charging more for slot 3 only works if slot 3 is the strongest. Six of the nine
signature cards were authored the other way round — Mare's 209-defence Bulwark
was slot 1 and her weakest skill was slot 3 — which would have made the
expensive pick strictly worse. `tools/split_avatar_skills.py` scores the three
slices and writes them back in ascending order, so position, price and power
agree on every card.

What this does NOT change
-------------------------
Avatars with no `skills` block — 27 of the 36 — are untouched. Their bonuses
have never been skill-gated and they keep applying in full, for free. Card
LEVEL bonuses are also unaffected: those are bought with coins and belong to the
card, not to any one skill, so they apply whichever skill is active and even
when the player cannot afford one.

Storage (all on the player profile)
-----------------------------------
    avatar_skill        {avatar_id: slot}  the standing pick, per card
    avatar_energy       int 0-100          the pool
    avatar_skill_locked int | None         the slot actually granted for the
                                           battle in progress; 0 means "could
                                           not afford one". Absent between
                                           battles, when the standing pick is
                                           what gets displayed.
    avatar_energy_match str | None         set while a ranked match is live, so
                                           a round ending does not refill.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("beyblade_bot.avatar_skills")


# ── Rules ─────────────────────────────────────────────────────────────────────

MAX_ENERGY: int = 100

# Cost by slot, 1-indexed: SKILL_ENERGY[0] is slot 1.
SKILL_ENERGY: tuple[int, ...] = (25, 50, 75)

# Profile keys, named once so a typo cannot create a second silent field.
K_CHOICE = "avatar_skill"
K_ENERGY = "avatar_energy"
K_LOCKED = "avatar_skill_locked"
K_MATCH  = "avatar_energy_match"


def skill_cost(slot: int) -> int:
    """Energy price of a slot. Out-of-range slots cost nothing and grant nothing."""
    idx = int(slot) - 1
    if idx < 0 or idx >= len(SKILL_ENERGY):
        return 0
    return SKILL_ENERGY[idx]


def uses_affordable(slot: int, energy: int = MAX_ENERGY) -> int:
    """How many times this slot fits in `energy`. Used by the picker's preview."""
    cost = skill_cost(slot)
    if cost <= 0:
        return 0
    return int(energy) // cost


def has_skills(avatar: Optional[dict]) -> bool:
    return bool((avatar or {}).get("skills"))


def skill_at(avatar: Optional[dict], slot: int) -> Optional[dict]:
    """The skill in a slot, or None when the card has no such slot."""
    skills = (avatar or {}).get("skills") or []
    idx = int(slot) - 1
    if idx < 0 or idx >= len(skills):
        return None
    return skills[idx]


# ── Reading player state ──────────────────────────────────────────────────────

def energy(profile: dict) -> int:
    """Current pool, clamped. A profile that has never battled starts full."""
    try:
        raw = profile.get(K_ENERGY)
        if raw is None:
            return MAX_ENERGY
        return max(0, min(MAX_ENERGY, int(raw)))
    except (TypeError, ValueError):
        return MAX_ENERGY


def in_ranked_match(profile: dict) -> bool:
    return bool(profile.get(K_MATCH))


def chosen_slot(profile: dict, avatar_id: Optional[str]) -> int:
    """The player's standing pick for a card. Defaults to slot 1.

    Slot 1 is the default rather than "none" because it is the cheapest skill
    on the card: a player who never touches the picker still fights with
    something, and it is the something that costs them least.
    """
    if not avatar_id:
        return 1
    try:
        raw = (profile.get(K_CHOICE) or {}).get(str(avatar_id))
    except AttributeError:
        return 1
    try:
        slot = int(raw)
    except (TypeError, ValueError):
        return 1
    return slot if 1 <= slot <= len(SKILL_ENERGY) else 1


def active_slot(profile: dict, avatar: Optional[dict]) -> int:
    """The slot whose bonuses apply right now.

    Inside a battle this is the locked slot — including 0, meaning the player
    could not afford one. Outside a battle it is the standing pick, so the
    profile and `;ainfo` show what the next fight will actually use.

    Always clamped to what the card really has: a pick of 3 on a card with two
    skills resolves to the card's last slot rather than to nothing.
    """
    if not has_skills(avatar):
        return 0
    locked = profile.get(K_LOCKED)
    if locked is not None:
        try:
            slot = int(locked)
        except (TypeError, ValueError):
            slot = 0
        if slot <= 0:
            return 0
    else:
        slot = chosen_slot(profile, (avatar or {}).get("id"))
    return max(1, min(slot, len(avatar.get("skills") or [])))


def active_skill(profile: dict, avatar: Optional[dict]) -> Optional[dict]:
    slot = active_slot(profile, avatar)
    return skill_at(avatar, slot) if slot else None


# ── Bonus filtering ───────────────────────────────────────────────────────────

def bonuses_for(avatar: Optional[dict], slot: int) -> dict:
    """The bonus block that applies for one slot.

    A card with no skills returns its whole block unchanged — that is the
    compatibility guarantee for the 27 avatars this system does not touch.

    A card WITH skills returns only the active slice, and explicitly zeroes
    every other key. Returning just the slice would leave the caller merging
    against defaults it cannot see; zeroing keeps the shape identical to the
    card's own block so `get_battle_bonuses` stays a straight field read.
    """
    card = (avatar or {}).get("bonuses") or {}
    if not has_skills(avatar):
        return dict(card)

    out = {k: (False if isinstance(v, bool) else 0) for k, v in card.items()}
    skill = skill_at(avatar, slot)
    if skill:
        for key, val in (skill.get("bonuses") or {}).items():
            out[key] = val
    return out


# ── Battle lifecycle ──────────────────────────────────────────────────────────

def _refill(profile: dict) -> None:
    profile[K_ENERGY] = MAX_ENERGY
    profile.pop(K_LOCKED, None)


def begin_battle(profile: dict, avatar: Optional[dict],
                 ranked: bool = False) -> dict:
    """Commit a skill for the battle about to start. Mutates `profile`.

    Returns a summary the caller can log or show:
        {slot, name, cost, energy_before, energy_after, afforded}

    Casual play refills first, so the pool is only ever a real constraint
    inside a ranked match. Called once per battle — in ranked that means once
    per ROUND, which is what makes 100 energy a match-long budget.
    """
    if not ranked and not in_ranked_match(profile):
        _refill(profile)
    if ranked:
        profile.setdefault(K_MATCH, "1")

    before = energy(profile)
    if not has_skills(avatar):
        profile.pop(K_LOCKED, None)
        profile[K_ENERGY] = before
        return {"slot": 0, "name": None, "cost": 0, "energy_before": before,
                "energy_after": before, "afforded": True}

    slot = chosen_slot(profile, avatar.get("id"))
    slot = max(1, min(slot, len(avatar.get("skills") or [])))
    cost = skill_cost(slot)

    if before >= cost:
        profile[K_ENERGY] = before - cost
        profile[K_LOCKED] = slot
        sk = skill_at(avatar, slot)
        return {"slot": slot, "name": (sk or {}).get("name"), "cost": cost,
                "energy_before": before, "energy_after": before - cost,
                "afforded": True}

    # Out of budget: the fight still happens, just without the skill. Charging
    # nothing matters — a partial charge would strand energy that can never buy
    # anything and would read as a bug to the player.
    profile[K_ENERGY] = before
    profile[K_LOCKED] = 0
    return {"slot": 0, "name": None, "cost": cost, "energy_before": before,
            "energy_after": before, "afforded": False}


def end_battle(profile: dict, ranked: bool = False) -> None:
    """Release the per-battle lock. Mutates `profile`.

    Casual refills immediately. Ranked keeps whatever is left, because the
    budget belongs to the match and not to the round — that is the whole point
    of the ranked rule.
    """
    profile.pop(K_LOCKED, None)
    if not ranked and not in_ranked_match(profile):
        profile[K_ENERGY] = MAX_ENERGY


def end_match(profile: dict) -> None:
    """The ranked match is over: drop the lock and refill. Mutates `profile`."""
    profile.pop(K_MATCH, None)
    _refill(profile)


# ── Convenience wrappers that own their own database access ───────────────────
# Every one swallows failures: an unreadable profile must never stop a battle
# starting, exactly as loadout.avatar_bonuses does.

def _mutate(player_id: int, fn):
    try:
        from utils.database import mutate_user
        return mutate_user(int(player_id), fn)
    except Exception as exc:                             # noqa: BLE001
        log.debug("avatar skill state unavailable for %s: %s", player_id, exc)
        return None


def begin_battle_for(player_id: int, avatar: Optional[dict],
                     ranked: bool = False) -> Optional[dict]:
    return _mutate(player_id, lambda p: begin_battle(p, avatar, ranked))


def end_battle_for(player_id: int, ranked: bool = False) -> None:
    _mutate(player_id, lambda p: end_battle(p, ranked))


def end_match_for(player_id: int) -> None:
    _mutate(player_id, lambda p: end_match(p))


def set_choice(player_id: int, avatar_id: str, slot: int) -> int:
    """Store a standing pick. Returns the slot stored."""
    slot = max(1, min(int(slot), len(SKILL_ENERGY)))

    def _apply(profile: dict) -> int:
        table = profile.get(K_CHOICE)
        if not isinstance(table, dict):
            table = {}
        table[str(avatar_id)] = slot
        profile[K_CHOICE] = table
        return slot

    return _mutate(player_id, _apply) or slot


def state_for(player_id: int) -> dict:
    """Read-only snapshot for the UI: {energy, slot, skill, avatar, in_match}."""
    try:
        from utils.database import get_user
        from .avatar_engine import avatar_engine
        prof = get_user(int(player_id))
        avatar = avatar_engine.get_avatar(
            avatar_engine.get_equipped_avatar_id(int(player_id)) or "")
        return {
            "energy":   energy(prof),
            "slot":     active_slot(prof, avatar),
            "skill":    active_skill(prof, avatar),
            "avatar":   avatar,
            "in_match": in_ranked_match(prof),
        }
    except Exception as exc:                             # noqa: BLE001
        log.debug("avatar skill snapshot failed for %s: %s", player_id, exc)
        return {"energy": MAX_ENERGY, "slot": 0, "skill": None,
                "avatar": None, "in_match": False}
