#!/usr/bin/env python3
"""
sim_levels.py — proof that levelling a bey actually changes how it fights.

Every check here corresponds to something that was silently broken: a stat cap
that made maxed beys identical, a Special that read a hardcoded number instead
of the special stat, a stamina bar that was 15 for everybody past level ~48,
and HP growth that PvP threw away.

Runs against the real 78-blade roster with no bot token. It does touch the user
store, but only under one throwaway id.

    python tools/sim_levels.py

Exits non-zero if any check fails.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.battle.damage_rules import resolve_special, special_scale   # noqa: E402
from cogs.battle.stamina_manager import (                             # noqa: E402
    StaminaManager, STAMINA_COST, max_stamina_for,
)
from utils import bey_levels as BL                                    # noqa: E402
from utils.hp_system import max_hp_for_blade                          # noqa: E402

# A dedicated id so the checks never read or write a real player's row.
SIM_UID = 99900000000000003

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def roster() -> dict:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "data", "beyblades.json")) as f:
        return json.load(f)


# ── Stat cap ─────────────────────────────────────────────────────────────────

def check_stat_cap(blades: dict) -> None:
    """A cap that most of the roster reaches makes every maxed bey the same bey."""
    print("\n▸ stat cap leaves blades distinguishable at level 100")
    for stat in BL.STATS:
        vals = [BL.stats_at(b, 100, {}).get(stat)
                for b in blades.values() if stat in (b.get("stats") or {})]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        capped = sum(1 for v in vals if v >= BL.STAT_CAP)
        distinct = len(set(vals))
        check(f"{stat}: almost nothing pinned to the cap",
              capped <= 2, f"{capped}/{len(vals)} capped")
        check(f"{stat}: blades stay distinct",
              distinct >= 40, f"{distinct} distinct values")


# ── Special ──────────────────────────────────────────────────────────────────

def check_special(blades: dict) -> None:
    print("\n▸ Specials scale with the special stat")

    # The calibration guarantee: adding the hook must not rebalance the roster.
    identical = 0
    for blade in blades.values():
        authored = resolve_special(blade)[:2]
        at_base = resolve_special(blade, (blade.get("stats") or {}).get("special"))[:2]
        identical += authored == at_base
    check("level 1 reproduces the authored damage for every blade",
          identical == len(blades), f"{identical}/{len(blades)}")

    grew = same = 0
    for blade in blades.values():
        if not (blade.get("stats") or {}).get("special"):
            continue
        base = resolve_special(blade, blade["stats"]["special"])
        top = resolve_special(blade, BL.stats_at(blade, 100, {})["special"])
        if top[0] * top[1] > base[0] * base[1]:
            grew += 1
        else:
            same += 1
    check("every blade's Special is stronger at level 100", same == 0,
          f"{grew} grew, {same} did not")

    check("scale never drops below 1.0 on a debuffed stat",
          special_scale({"stats": {"special": 120}}, 40) == 1.0)
    check("scale is 1.0 when no stat is supplied",
          special_scale({"stats": {"special": 120}}, None) == 1.0)


# ── Stamina ──────────────────────────────────────────────────────────────────

def check_stamina(blades: dict) -> None:
    """The reported bug: every bey had exactly 15 stamina at level 100."""
    print("\n▸ stamina bars differ per bey")

    sta = max((b for b in blades.values() if str(b.get("type", "")).lower() == "stamina"),
              key=lambda b: b["stats"]["stamina"])
    atk = max((b for b in blades.values() if str(b.get("type", "")).lower() == "attack"),
              key=lambda b: b["stats"]["attack"])

    for level in (1, 100):
        s1, s2 = BL.stats_at(sta, level, {}), BL.stats_at(atk, level, {})
        sm = StaminaManager({"sta": {**sta, "stats": s1}, "atk": {**atk, "stats": s2}},
                            effective_stats={"sta": s1, "atk": s2})
        check(f"L{level}: the two blades have different maximums",
              sm.cap_for("sta") != sm.cap_for("atk"),
              f'{sm.cap_for("sta")} vs {sm.cap_for("atk")}')
        check(f"L{level}: they start on different stamina",
              sm.stamina["sta"] != sm.stamina["atk"],
              f'{sm.stamina["sta"]} vs {sm.stamina["atk"]}')
        check(f"L{level}: the stamina blade holds the bigger bar",
              sm.cap_for("sta") > sm.cap_for("atk"))

    # The stat has to keep paying past the old flat ceiling of 15.
    check("the bar keeps growing past the old 240-stat plateau",
          max_stamina_for(500) > max_stamina_for(240) > max_stamina_for(150),
          f"{max_stamina_for(150)} < {max_stamina_for(240)} < {max_stamina_for(500)}")

    # Recovery scales too — it used to be a flat +3 for everyone.
    s100 = BL.stats_at(sta, 100, {})
    sm = StaminaManager({"sta": {**sta, "stats": s100}}, effective_stats={"sta": s100})
    before = sm.stamina["sta"] = 1.0
    sm.apply_stamina_action("sta", {"sta": 1000}, None, max_hp=4000)
    recovered = sm.stamina["sta"] - before
    check("the Stamina move recovers more than the old flat +3",
          recovered > 3.0, f"+{recovered:.2f}")

    # Passive regen was a stub returning [] and doing nothing.
    sm.stamina["sta"] = 1.0
    sm.apply_passive_regen("sta", {"sta": 1000})
    check("passive regen actually restores stamina",
          sm.stamina["sta"] > 1.0, f'1.0 -> {sm.stamina["sta"]}')

    # Deeper bars must not mean the stamina economy stops existing: a baseline
    # bey should still get roughly the number of actions it got before.
    baseline = max_stamina_for(93) / STAMINA_COST["attack"]
    check("a baseline bey's actions-before-exhaustion stays near the old 10",
          6.0 <= baseline <= 9.0, f"{baseline:.1f} attacks from full")


# ── HP reaching PvP ──────────────────────────────────────────────────────────

def check_pvp_hp(blades: dict) -> None:
    """PvP clamped the levelled HP stat back into the type band and lost it."""
    print("\n▸ bey level HP reaches PvP")
    from cogs.battle.session import _level_hp_gain, _effective_special
    from utils.database import get_user, update_user

    name = "Bloody Longinus"
    blade = blades[name]
    pools, specials = [], []
    for level in (1, 50, 100):
        profile = get_user(SIM_UID)
        profile["active_beyblade"] = name
        profile["bey_progress"] = {
            name: {"xp": BL.xp_for_level(level), "ivs": {s: 0 for s in BL.STATS}}}
        profile["equipped_parts"] = []
        profile["equipped_avatar"] = None
        profile["active_copy"] = None
        update_user(SIM_UID, profile)
        pools.append(max_hp_for_blade(blade) + _level_hp_gain(SIM_UID, blade))
        specials.append(_effective_special(SIM_UID, blade))

    print(f"     pools {pools}   special stat {specials}")
    check("the HP pool grows with level",
          pools[0] < pools[1] < pools[2], " < ".join(map(str, pools)))
    check("the special stat grows with level",
          specials[0] < specials[1] < specials[2], " < ".join(map(str, specials)))


def main() -> int:
    blades = roster()
    print(f"roster: {len(blades)} blades  •  STAT_CAP {BL.STAT_CAP}")
    check_stat_cap(blades)
    check_special(blades)
    check_stamina(blades)
    check_pvp_hp(blades)

    print()
    if FAILURES:
        print(f"✗ {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"    - {f}")
        return 1
    print("✓ all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
