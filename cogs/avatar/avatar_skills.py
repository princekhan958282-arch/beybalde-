"""
avatar_skills.py — one skill per battle, paid for out of an energy pool.

The rules
---------
An avatar that carries signature skills no longer applies all three at once.
The player commits to ONE before the battle, and pays for it:

    slot 1 ....  25 energy      (the cheapest, and the weakest)
    slot 2 ....  50 energy
    slot 3 ....  75 energy      (the most expensive, and the strongest)

The pool is 100, and it is a RANKED resource.

    casual   never touches the pool. No spend, no refill — your chosen skill
             is always granted. Casual play is free and always available.

    ranked   spends from the pool, and does NOT top you up when the match
             opens. You bring what you have, and 100 has to cover every round
             of a first-to-3-points match:

                 slot 1 four times · slot 2 twice · slot 3 once · or a mix

Running dry is a legal state, not an error: the battle starts with the card's
level bonuses and no skill.

Recovery
--------
+25 every 5 minutes, so a drained pool is full in twenty. It is settled ON READ
(`accrue`) rather than by a background ticker — the house rule, stated in
`cogs/tournament/cog.py`, is that state is derived from what is stored so a
missed tick costs nothing, and a ticker would rewrite every profile row on a
schedule to change a number nobody is looking at.

Recovery is FROZEN for the duration of a ranked match. The budget you walk in
with is the budget you fight the whole match on, which is the point of having
one. `end_match` re-stamps the clock so the time the match took is not credited
the instant it ends.

`ENERGY_REFILL_PRICE` coins buys an instant top-up, for players who would rather
pay than wait. Blocked mid-match for the same reason regen is: it would sell a
way around the budget.

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
    avatar_energy_ts    float              unix time regen was last settled to
    avatar_skill_locked int | None         the slot actually granted for the
                                           battle in progress; 0 means "could
                                           not afford one". Absent between
                                           battles, when the standing pick is
                                           what gets displayed.
    avatar_energy_match str | None         set while a ranked match is live, so
                                           regen stays frozen for its duration.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger("beyblade_bot.avatar_skills")


# ── Rules ─────────────────────────────────────────────────────────────────────

MAX_ENERGY: int = 100

# Cost by slot, 1-indexed: SKILL_ENERGY[0] is slot 1.
SKILL_ENERGY: tuple[int, ...] = (25, 50, 75)

# Recovery. One tick every five minutes, so a drained pool is full in twenty.
ENERGY_REGEN_AMOUNT:  int = 25
ENERGY_REGEN_SECONDS: int = 300

# Instant top-up, for players who would rather pay than wait.
ENERGY_REFILL_PRICE: int = 40_000

# Profile keys, named once so a typo cannot create a second silent field.
K_CHOICE    = "avatar_skill"
K_ENERGY    = "avatar_energy"
K_ENERGY_TS = "avatar_energy_ts"
K_LOCKED    = "avatar_skill_locked"
K_MATCH     = "avatar_energy_match"


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


# ── Recovery ──────────────────────────────────────────────────────────────────

def _stamp(profile: dict, now: float) -> None:
    profile[K_ENERGY_TS] = float(now)


def _settled_at(profile: dict, now: float) -> float:
    """When regen was last settled. An absent stamp means 'from now'.

    Defaulting to `now` rather than 0 matters: a profile that has never battled
    would otherwise be handed decades of accrued time on its first read, which
    is harmless at full energy and wrong the moment it is not.
    """
    try:
        raw = profile.get(K_ENERGY_TS)
        if raw is None:
            return float(now)
        ts = float(raw)
    except (TypeError, ValueError):
        return float(now)
    # A stamp from the future is a clock change, not a credit.
    return min(ts, float(now))


def accrue(profile: dict, now: Optional[float] = None) -> int:
    """Settle time-based recovery. Mutates `profile`. Returns energy gained.

    Frozen during a ranked match, and not banked above the cap — in both cases
    the clock is re-stamped so the skipped period cannot be claimed later.

    The stamp advances by whole ticks only, NOT to `now`. Advancing to `now`
    would discard the partial tick on every read, and since the UI reads this
    on every `;askill`, a player checking their energy often would never
    regenerate at all.
    """
    now = time.time() if now is None else float(now)
    have = energy(profile)

    if in_ranked_match(profile) or have >= MAX_ENERGY:
        _stamp(profile, now)
        return 0

    ts = _settled_at(profile, now)
    ticks = int((now - ts) // ENERGY_REGEN_SECONDS)
    if ticks <= 0:
        profile.setdefault(K_ENERGY_TS, ts)
        return 0

    gained = min(MAX_ENERGY - have, ticks * ENERGY_REGEN_AMOUNT)
    profile[K_ENERGY] = have + gained
    if profile[K_ENERGY] >= MAX_ENERGY:
        _stamp(profile, now)          # full: stop carrying a remainder
    else:
        profile[K_ENERGY_TS] = ts + ticks * ENERGY_REGEN_SECONDS
    return gained


def next_tick_at(profile: dict, now: Optional[float] = None) -> Optional[float]:
    """Unix time of the next +25, or None when full or frozen."""
    now = time.time() if now is None else float(now)
    if in_ranked_match(profile) or energy(profile) >= MAX_ENERGY:
        return None
    return _settled_at(profile, now) + ENERGY_REGEN_SECONDS


def full_at(profile: dict, now: Optional[float] = None) -> Optional[float]:
    """Unix time the pool reaches 100, or None when already full or frozen."""
    now = time.time() if now is None else float(now)
    have = energy(profile)
    if in_ranked_match(profile) or have >= MAX_ENERGY:
        return None
    ticks_needed = -(-(MAX_ENERGY - have) // ENERGY_REGEN_AMOUNT)   # ceil
    return _settled_at(profile, now) + ticks_needed * ENERGY_REGEN_SECONDS


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

# Bonuses that belong to the CARD rather than to the skill in play. Everything
# else is zeroed and re-supplied by the active skill; these are carried across
# from the card's own block whatever is equipped.
#
# HP is the case that forced this, and it is not a thing a skill *does* — it is
# what the avatar *is*, and it has to hold whichever of the three is chosen.
# Neither of the two ways to write it without this set works:
#
#   • at the top level only — zeroed here, so the shop advertises HP the fight
#     never applies;
#   • in all three skill blocks — invisible to `build_avatar_embed`, which
#     renders the card's TOP-LEVEL block (avatar_utils.py:284), so the fight
#     grants HP the shop cannot show. It also breaks the partition the skill
#     split relies on: the three blocks are meant to account for every live
#     top-level key exactly once.
#
# `hp_flat` is 0.0 on all nine skill cards today. It is here for consistency,
# so the next defensive card cannot reintroduce the same split by accident.
CARD_LEVEL_BONUS_KEYS = frozenset({"hp_percent", "hp_flat"})


def bonuses_for(avatar: Optional[dict], slot: int) -> dict:
    """The bonus block that applies for one slot.

    A card with no skills returns its whole block unchanged — that is the
    compatibility guarantee for the 27 avatars this system does not touch.

    A card WITH skills returns only the active slice, and explicitly zeroes
    every other key. Returning just the slice would leave the caller merging
    against defaults it cannot see; zeroing keeps the shape identical to the
    card's own block so `get_battle_bonuses` stays a straight field read.

    The exception is `CARD_LEVEL_BONUS_KEYS`, which survive the zeroing — see
    the note above it for why HP cannot be a per-skill value.
    """
    card = (avatar or {}).get("bonuses") or {}
    if not has_skills(avatar):
        return dict(card)

    out = {k: (v if k in CARD_LEVEL_BONUS_KEYS
               else (False if isinstance(v, bool) else 0))
           for k, v in card.items()}
    skill = skill_at(avatar, slot)
    if skill:
        for key, val in (skill.get("bonuses") or {}).items():
            out[key] = val
    return out


# ── Battle lifecycle ──────────────────────────────────────────────────────────

class RefillError(Exception):
    """Why a refill could not happen. The message is shown to the player."""


def _refill(profile: dict, now: Optional[float] = None) -> None:
    profile[K_ENERGY] = MAX_ENERGY
    _stamp(profile, time.time() if now is None else float(now))


def buy_refill(profile: dict, now: Optional[float] = None) -> dict:
    """Pay ENERGY_REFILL_PRICE for a full pool. Mutates `profile`.

    Raises RefillError rather than returning a failure, because the caller runs
    this inside `database.mutate_user` — where raising abandons the whole
    read-modify-write, so a refused purchase cannot half-apply and take the
    coins without giving the energy.
    """
    accrue(profile, now)

    if in_ranked_match(profile):
        raise RefillError(
            "You're mid-way through a ranked match — energy is frozen until "
            "it's decided. Buying now would just sell a way around the budget.")

    have = energy(profile)
    if have >= MAX_ENERGY:
        raise RefillError(f"Your energy is already full ({MAX_ENERGY}/{MAX_ENERGY}).")

    try:
        coins = int(profile.get("coins", 0) or 0)
    except (TypeError, ValueError):
        coins = 0
    if coins < ENERGY_REFILL_PRICE:
        raise RefillError(
            f"A refill costs 🪙 **{ENERGY_REFILL_PRICE:,}**. You have "
            f"🪙 {coins:,} — {ENERGY_REFILL_PRICE - coins:,} short.")

    profile["coins"] = coins - ENERGY_REFILL_PRICE
    _refill(profile, now)
    return {"spent": ENERGY_REFILL_PRICE, "from": have, "to": MAX_ENERGY,
            "coins_after": profile["coins"]}


def _no_skill_commit(energy_now: int) -> dict:
    return {"slot": 0, "name": None, "cost": 0, "energy_before": energy_now,
            "energy_after": energy_now, "afforded": True}


def begin_battle(profile: dict, avatar: Optional[dict],
                 ranked: bool = False, now: Optional[float] = None) -> dict:
    """Commit a skill for the battle about to start. Mutates `profile`.

    Returns a summary the caller can log or show:
        {slot, name, cost, energy_before, energy_after, afforded}

    CASUAL does not read or write the pool at all. The chosen skill is granted
    free, every time. Casual used to refill the pool instead, which meant any
    casual battle silently restocked a ranked resource — so recovery time and
    the paid refill were both worthless, because a free fight beat waiting and
    beat paying.

    RANKED spends, and does not top up on the way in. Called once per battle,
    which in ranked means once per ROUND — that is what makes 100 energy a
    match-long budget rather than a per-round allowance.
    """
    accrue(profile, now)
    before = energy(profile)

    if not has_skills(avatar):
        profile.pop(K_LOCKED, None)
        return _no_skill_commit(before)

    slot = chosen_slot(profile, avatar.get("id"))
    slot = max(1, min(slot, len(avatar.get("skills") or [])))
    cost = skill_cost(slot)
    sk = skill_at(avatar, slot)

    if not ranked:
        # Free, and deliberately reported with cost 0 — showing "25⚡" on a
        # battle that charged nothing is the kind of small lie that turns into
        # a bug report about energy not going down.
        profile[K_LOCKED] = slot
        return {"slot": slot, "name": (sk or {}).get("name"), "cost": 0,
                "energy_before": before, "energy_after": before,
                "afforded": True}

    profile.setdefault(K_MATCH, "1")

    if before >= cost:
        profile[K_ENERGY] = before - cost
        profile[K_LOCKED] = slot
        return {"slot": slot, "name": (sk or {}).get("name"), "cost": cost,
                "energy_before": before, "energy_after": before - cost,
                "afforded": True}

    # Out of budget: the round still happens, just without the skill. Charging
    # nothing matters — a partial charge would strand energy that can never buy
    # anything and would read as a bug to the player.
    profile[K_LOCKED] = 0
    return {"slot": 0, "name": None, "cost": cost, "energy_before": before,
            "energy_after": before, "afforded": False}


def end_battle(profile: dict, ranked: bool = False) -> None:
    """Release the per-battle lock. Mutates `profile`.

    Nothing else: casual never spent anything to give back, and ranked keeps
    what is left because the budget belongs to the match, not to the round.
    """
    profile.pop(K_LOCKED, None)


def end_match(profile: dict, now: Optional[float] = None) -> None:
    """The ranked match is over. Mutates `profile`.

    Drops the lock and the frozen flag, and re-stamps the recovery clock. The
    stamp is the important part: without it, the moment the flag clears, the
    entire duration of the match would settle at once and a long match would
    hand back a full pool.
    """
    profile.pop(K_LOCKED, None)
    profile.pop(K_MATCH, None)
    _stamp(profile, time.time() if now is None else float(now))


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


def accrue_for(player_id: int) -> int:
    """Settle recovery and persist it. Returns energy gained."""
    return _mutate(player_id, lambda p: accrue(p)) or 0


def buy_refill_for(player_id: int) -> dict:
    """Purchase a full pool. Raises RefillError with a player-facing reason.

    Not routed through `_mutate`: that swallows exceptions, and this is the one
    call where the exception IS the answer. `mutate_user` holds its lock across
    the read-modify-write and abandons it on a raise, so a refusal cannot leave
    the coins spent.
    """
    from utils.database import mutate_user
    return mutate_user(int(player_id), buy_refill)


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
    """Read-only snapshot for the UI.

    Settles recovery first and PERSISTS it, so what the panel prints is what
    the next battle will actually charge against. A read that showed accrued
    energy without banking it would let a player watch a number they do not
    have.
    """
    try:
        from utils.database import get_user
        from .avatar_engine import avatar_engine
        accrue_for(player_id)
        prof = get_user(int(player_id))
        avatar = avatar_engine.get_avatar(
            avatar_engine.get_equipped_avatar_id(int(player_id)) or "")
        return {
            "energy":       energy(prof),
            "slot":         active_slot(prof, avatar),
            "skill":        active_skill(prof, avatar),
            "avatar":       avatar,
            "in_match":     in_ranked_match(prof),
            "next_tick_at": next_tick_at(prof),
            "full_at":      full_at(prof),
            "coins":        int(prof.get("coins", 0) or 0),
        }
    except Exception as exc:                             # noqa: BLE001
        log.debug("avatar skill snapshot failed for %s: %s", player_id, exc)
        return {"energy": MAX_ENERGY, "slot": 0, "skill": None,
                "avatar": None, "in_match": False,
                "next_tick_at": None, "full_at": None, "coins": 0}
