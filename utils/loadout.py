"""
loadout.py — one answer to "what are this player's actual stats?"

The problem this fixes
----------------------
A player's effective stats came from three sources — the blade, equipped parts,
and their avatar — and every consumer assembled them differently:

  * PvP battle      applied parts, applied avatar
  * boss battle     applied NEITHER
  * profile card    applied neither (avatar bonuses were computed on the
                    fallback embed path only, which runs when the card render
                    FAILS, so the card players actually see showed base stats)
  * info card       applied neither

So equipping parts or an avatar visibly changed nothing outside PvP, and did
nothing at all in a boss fight. Anyone testing it would conclude the equip
didn't work.

Fixing that in four places would leave four copies to drift apart again, so
resolution lives here and every consumer calls `effective_blade()`.

What it does NOT do
-------------------
Only the flat/percent STAT bonuses are folded into the returned blade. An
avatar's timing-based signature skills (dodge, counter, nth-hit, resistance)
stay with `AvatarBonuses` and are applied by the battle engine at the moment
they trigger — flattening those into a stat would silently change what they
mean. `effective_blade()` returns the bonuses object alongside the blade so a
caller that can honour them still can.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("beyblade_bot.loadout")

# Stat keys as they appear in a blade dict, mapped to the AvatarBonuses field
# pair that modifies them.
_AVATAR_STAT_MAP = {
    "attack":  ("attack_flat", "attack_percent"),
    "defense": ("defence_flat", "defence_percent"),
    "stamina": ("stamina_flat", "stamina_percent"),
    "special": ("special_move_flat", "special_move_percent"),
}


def part_bonuses(profile: dict) -> dict[str, int]:
    """Total stat bonus from the player's EQUIPPED parts.

    Reads `equipped_parts` (the active loadout), not `parts` (everything they
    own) — owning a part has never been meant to grant its bonus.
    """
    equipped = profile.get("equipped_parts") or []
    if not equipped:
        return {}
    try:
        from cogs.battle.battle import PARTS_CATALOG
    except Exception as e:                           # noqa: BLE001
        log.debug("parts catalog unavailable: %s", e)
        return {}
    catalog = {p["name"].lower(): p for p in PARTS_CATALOG}
    out: dict[str, int] = {}
    for name in equipped:
        part = catalog.get(str(name).lower())
        if part:
            out[part["stat"]] = out.get(part["stat"], 0) + part["bonus"]
    return out


def avatar_bonuses(user_id: int):
    """The player's avatar bonuses, or the null set if anything goes wrong.

    Never raises: a broken avatar must not stop a card rendering or a fight
    starting.
    """
    try:
        from cogs.avatar import avatar_engine, NULL_BONUSES
        return avatar_engine.get_battle_bonuses(user_id) or NULL_BONUSES
    except Exception as e:                           # noqa: BLE001
        log.debug("avatar bonuses unavailable for %s: %s", user_id, e)
        try:
            from cogs.avatar import NULL_BONUSES
            return NULL_BONUSES
        except Exception:                            # noqa: BLE001
            return None


def effective_blade(user_id: int, profile: Optional[dict] = None,
                    blade: Optional[dict] = None,
                    include_parts: bool = True,
                    include_avatar: bool = True) -> tuple[dict, dict, object]:
    """The blade a player actually fights with.

    Returns (blade, breakdown, avatar_bonuses):
      blade      — a COPY with parts and avatar stat bonuses folded in
      breakdown  — {stat: {"base", "parts", "avatar", "total"}} for display
      avatar_bonuses — the full object, for callers that honour timed effects

    `include_parts=False` is used for boss copies: a copy is a fixed roll, and
    letting parts stack on top would make a farmed copy stronger than the boss
    it came from.
    """
    from utils.database import get_user
    profile = profile if profile is not None else get_user(user_id)

    if blade is None:
        try:
            from cogs.battle.boss import boss_copy as bcopy
            blade, copy = bcopy.equipped_blade(user_id)
            if copy is not None:
                include_parts = False
        except Exception as e:                       # noqa: BLE001
            log.debug("couldn't resolve equipped blade for %s: %s", user_id, e)
            blade = None
    if not blade:
        return {}, {}, avatar_bonuses(user_id) if include_avatar else None

    raw_base = dict(blade.get("stats") or {})
    base = dict(raw_base)
    level = 1
    if include_parts:
        # Levels apply to owned beys, not to boss copies — a copy is a fixed
        # roll and levelling it would let a farmed copy overtake the boss it
        # came from. `include_parts` is already False for copies, so it doubles
        # as "is this a normal, ownable bey".
        try:
            from utils import bey_levels as BL
            name = blade.get("name")
            if name:
                entry = (profile.get("bey_progress") or {}).get(str(name))
                if entry:
                    level = BL.level_from_xp(entry.get("xp", 0))
                    if level > 1:
                        base = BL.stats_at(blade, level, entry.get("ivs"))
        except Exception as e:                       # noqa: BLE001
            log.debug("bey level lookup failed for %s: %s", user_id, e)

    parts = part_bonuses(profile) if include_parts else {}
    av = avatar_bonuses(user_id) if include_avatar else None

    stats: dict[str, float] = {}
    breakdown: dict[str, dict] = {}
    for stat in set(base) | set(parts):
        # `b` is the LEVELLED base; `raw` is the blade's printed stat. Keeping
        # both means a caller can tell what levelling contributed — the boss
        # fight needs exactly that to turn hp growth into a bigger HP pool.
        b = float(base.get(stat, 0) or 0)
        raw = float(raw_base.get(stat, b) or 0)
        p = float(parts.get(stat, 0) or 0)
        subtotal = b + p

        a = 0.0
        if av is not None and stat in _AVATAR_STAT_MAP:
            flat_key, pct_key = _AVATAR_STAT_MAP[stat]
            flat = float(getattr(av, flat_key, 0.0) or 0.0)
            pct = float(getattr(av, pct_key, 0.0) or 0.0)
            # Percent applies to base+parts, so an avatar that reads "+10%
            # attack" is worth more on a better-equipped blade — which is what
            # a percentage is supposed to mean.
            a = flat + subtotal * pct

        total = subtotal + a
        stats[stat] = round(total)
        breakdown[stat] = {"base": round(raw), "level": round(b - raw),
                           "parts": round(p), "avatar": round(a),
                           "total": round(total)}

    out = dict(blade)
    out["stats"] = stats
    out["level"] = level
    breakdown["_level"] = level
    return out, breakdown, av


def effective_hp(user_id: int, base_hp: float,
                 av=None) -> float:
    """Starting HP including the avatar's HP bonus."""
    av = av if av is not None else avatar_bonuses(user_id)
    if av is None:
        return base_hp
    try:
        return float(av.apply_hp_bonus(base_hp))
    except Exception:                                # noqa: BLE001
        return base_hp


def summary_lines(breakdown: dict) -> list[str]:
    """Human-readable '+N from parts, +N from avatar' lines, only for stats
    that actually changed — so an unmodified card stays clean."""
    out = []
    for stat in ("attack", "defense", "stamina", "special"):
        d = breakdown.get(stat)
        if not d:
            continue
        extra = d.get("level", 0) + d["parts"] + d["avatar"]
        if extra <= 0:
            continue
        bits = []
        if d.get("level"):
            bits.append(f"+{d['level']} level")
        if d["parts"]:
            bits.append(f"+{d['parts']} parts")
        if d["avatar"]:
            bits.append(f"+{d['avatar']} avatar")
        out.append(f"{stat.title()} {d['total']} ({', '.join(bits)})")
    return out
