"""
utils/hp_system.py — Per-blade HP stat system
=============================================

Every Beyblade carries a fifth core stat: **HP**.

Design
------
* ``stats.hp`` lives in ``data/beyblades.json`` alongside attack/defense/
  stamina/special.  It is authored data — balance it by hand there.
* Each **type owns an HP band** (see ``TYPE_HP_BAND``). The band is the balance
  lever: widen it to make a type's blades differ more from each other, shift it
  to make the whole type tankier or squishier. Every read path clamps into the
  owning type's band, so no blade can ever drift outside its class.
* If a blade is missing ``stats.hp`` (new entry, hand-added JSON, mod data),
  ``derive_hp_stat()`` computes a value from its stats and maps it into its
  type's band, so nothing crashes and no blade fights at 0 HP.
* Battle max HP is **additive**: ``BASE_HP + hp_stat`` (then avatar HP boosts
  stack on top inside BattleSession). 80 gives 680 HP, 139 gives 739 HP.

Every existing damage number, heal amount and % threshold in the engine stays
valid — only the size of the pool changes.
"""
from __future__ import annotations

from cogs.core.constants import BASE_HP

# ── Global hard limits (nothing may ever escape these) ───────────────────────
HP_STAT_MIN      = 80
HP_STAT_MAX      = 150
HP_STAT_BASELINE = 100      # hp == 100 → exactly BASE_HP in battle

# ── Per-type HP bands ────────────────────────────────────────────────────────
# (floor, ceiling) for each type. This is the primary balance dial.
#
#   Attack   80–130   widest spread: rushdown blades stay fragile, but the
#                     heavy hitters can now take a punch.
#   Balance  106–122  the middle.
#   Defense  100–128  compressed — walls no longer trivialise a match.
#   Stamina  119–139  outlasts by design.
TYPE_HP_BAND: dict[str, tuple[int, int]] = {
    "Attack":  (80, 130),
    "Balance": (106, 122),
    "Defense": (100, 128),
    "Stamina": (119, 139),
}
_GLOBAL_BAND = (HP_STAT_MIN, HP_STAT_MAX)

# ── Display scale ────────────────────────────────────────────────────────────
# HP bars normalise over the range actually *reachable* across all types, not
# the global 80–150 hard limit. Derived from the bands, so retuning
# TYPE_HP_BAND above automatically retunes every bar — nothing to hand-sync.
# Cross-type comparable (a Stamina wall out-bars an Attack blade) while still
# letting the strongest blade in the game fill the bar.
HP_DISPLAY_MIN = min(lo for lo, _ in TYPE_HP_BAND.values())
HP_DISPLAY_MAX = max(hi for _, hi in TYPE_HP_BAND.values())


def hp_display_pct(blade: dict | None) -> float:
    """Where a blade's HP sits on the 0–100 display scale."""
    span = max(1, HP_DISPLAY_MAX - HP_DISPLAY_MIN)
    pct  = (blade_hp_stat(blade) - HP_DISPLAY_MIN) / span * 100.0
    return max(0.0, min(100.0, pct))

# ── Derivation weights (only used when stats.hp is absent) ───────────────────
_RARITY_BUMP = {
    "Common": 0, "Uncommon": 2, "Rare": 4, "Epic": 6,
    "Legendary": 8, "Mythic": 10, "Ultimate": 13,
    "Exclusive": 9, "State Exclusive": 9,
}


def band_for(blade_type) -> tuple[int, int]:
    """The (floor, ceiling) HP band a type is allowed to occupy."""
    return TYPE_HP_BAND.get(str(blade_type), _GLOBAL_BAND)


def clamp_hp_stat(value, blade_type=None) -> int:
    """Force a value into the legal band — the type's band when known,
    otherwise the global 80–150."""
    lo, hi = band_for(blade_type) if blade_type is not None else _GLOBAL_BAND
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return max(lo, min(hi, HP_STAT_BASELINE))
    return max(lo, min(hi, v))


def derive_hp_stat(blade: dict) -> int:
    """Compute an HP stat for a blade that has none. Deterministic — no RNG.

    Scores bulk from the blade's own stats, then maps that score into its
    type's band, so a derived blade always lands in-class.
    """
    blade = blade or {}
    st    = blade.get("stats", {}) or {}
    atk   = st.get("attack", 80)
    dfn   = st.get("defense", 80)
    sta   = st.get("stamina", 80)

    # Raw bulk score, roughly 0–100 across real data.
    score  = 50.0
    score += (dfn - 70) / 2.2          # bulk rewards defense hardest
    score += (sta - 70) / 4.0          # spin endurance helps survive
    score -= (atk - 85) / 3.0          # glass-cannon tax
    score += _RARITY_BUMP.get(blade.get("rarity"), 0)
    score  = max(0.0, min(100.0, score))

    lo, hi = band_for(blade.get("type"))
    return clamp_hp_stat(lo + (hi - lo) * score / 100.0, blade.get("type"))


def blade_hp_stat(blade: dict | None) -> int:
    """The displayed HP stat for a blade, always inside its type band.
    Never raises."""
    if not blade:
        return HP_STAT_BASELINE
    raw = (blade.get("stats", {}) or {}).get("hp")
    if raw is None:
        return derive_hp_stat(blade)
    return clamp_hp_stat(raw, blade.get("type"))


def max_hp_for_blade(blade: dict | None, base: int = BASE_HP) -> int:
    """Battle HP pool = BASE_HP + the blade's HP stat.

        80 → 680      100 → 700      130 → 730      139 → 739

    Avatar HP bonuses are NOT applied here — BattleSession stacks them on top
    of this value, so the order is always: base + bey HP → avatar boost → max.
    """
    return max(1, int(base) + blade_hp_stat(blade))
