#!/usr/bin/env python3
"""
tools/sim_numbers.py — the card must print the number the game uses.

Four places told a player something the engine disagreed with. None of them
crashed, none of them appeared in a log, and all four were visible on the most
looked-at screens in the bot:

  * `;info` printed a level-100 bey's HP pool as the CLAMPED figure while the
    fight added the levelled gain on top — the card understated its own blade
    by hundreds.
  * the stat bars had two different ceilings (500 on the profile card, 200 on
    the info card), so one surface showed a full bar and the other a partial
    one for the same blade at the same moment.
  * `;equippart` read only the legacy penalty pair, so a part carrying the
    newer `penalties` dict printed its bonus and none of its cost — and
    contradicted the loadout total printed directly beneath it.
  * `tools/sim_levels.py` asserted every Special grows with level, which is
    false by design for the three `non_damage` Specials, and had been red for
    weeks while asserting the opposite of `sim_v108_content.py`.

Run:  python3 tools/sim_numbers.py
"""

from __future__ import annotations

import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

logging.disable(logging.CRITICAL)

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {detail}")


import tempfile                                                   # noqa: E402
import utils.database as DB                                       # noqa: E402
from utils.userstore import UserStore                             # noqa: E402

_TMP = tempfile.mkdtemp()
STORE = UserStore(os.path.join(_TMP, "sim.db"))
STORE.ensure_ready()
DB.USER_STORE = STORE

from utils import bey_levels as BL                                # noqa: E402
from utils import loadout as LO                                   # noqa: E402
from utils.hp_system import max_hp_for_blade                      # noqa: E402
from cogs.economy import profile as PF                            # noqa: E402
from cogs.economy import shop as SHOP                             # noqa: E402

UID = 90001


async def main() -> int:
    blades = DB.load_beyblades()
    name = next(iter(blades))
    blade = blades[name]

    # A player who has levelled this blade to 100.
    prof = DB._default_profile(str(UID))
    prof["inventory"] = [name]
    prof["active_beyblade"] = name
    prof["bey_progress"] = {name: {"xp": BL.xp_for_level(100), "ivs": {}}}
    STORE.put_one(str(UID), prof)

    print("\n── 1. the card prints the pool the fight uses ──────────────────")
    printed_clamped = max_hp_for_blade(blade)
    real_pool = await LO.battle_pool(UID, blade)
    gain = await LO.level_hp_gain(UID, blade)
    print(f"       {name} at level 100: clamped {printed_clamped}, "
          f"gain {gain}, real pool {real_pool}")
    check("a levelled bey really does fight with more HP than the clamped "
          "figure", gain > 0, gain)
    check("...and `battle_pool` is exactly clamped + gain",
          real_pool == printed_clamped + gain, (real_pool, printed_clamped))

    line = await PF._hp_stat_line(blade, UID)
    check("the ;profile HP line prints the real pool",
          f"{real_pool} pool" in line, line)
    check("...and not the clamped one",
          f"{printed_clamped} pool" not in line, line)
    fallback_line = await PF._hp_stat_line(blade)
    check("with no viewer, the card falls back to the printed pool rather "
          "than guessing", f"{printed_clamped} pool" in fallback_line)

    # The battle path and the card path must be the same function.
    from cogs.battle import session as SS
    battle_gain = await SS._level_hp_gain(UID, blade)
    check("the battle asks the same helper the card does",
          battle_gain == gain, (battle_gain, gain))

    print("\n── 2. one stat ceiling, not three ──────────────────────────────")
    from utils import profile_card as PC
    from utils import info_card as IC
    from utils import info_card_pillow as ICP
    ceilings = {"profile_card": PC.STAT_MAX, "info_card": IC.STAT_MAX,
                "info_card_pillow": ICP.STAT_MAX, "embeds": PF._BAR_MAX}
    check("every surface uses the same bar ceiling",
          len(set(ceilings.values())) == 1, ceilings)
    check("...and it is the cap levelled stats can actually reach",
          set(ceilings.values()) == {BL.STAT_CAP}, (ceilings, BL.STAT_CAP))
    topped = BL.stats_at(blade, 100, {})
    hot = max(topped.get("attack", 0), topped.get("defense", 0),
              topped.get("stamina", 0))
    check(f"a level-100 stat ({hot}) is inside that ceiling, so the bar is not "
          f"pinned full", hot <= BL.STAT_CAP, hot)

    print("\n── 3. equipping a part shows what it costs ─────────────────────")
    multi = [p for p in SHOP.PARTS_CATALOG if p.get("penalties")]
    check("the catalogue really has parts with multi-stat penalties",
          bool(multi), len(multi))
    for part in multi:
        pens = SHOP.part_penalties(part)
        check(f"{part['name']} reports every penalty it carries",
              len(pens) >= 2, pens)
        legacy = {}
        if part.get("penalty_stat") and part.get("penalty"):
            legacy[part["penalty_stat"]] = part["penalty"]
        check("...which the legacy pair alone would have missed",
              set(pens) - set(legacy), (pens, legacy))
    src = open(os.path.join(ROOT, "cogs/economy/shop.py"),
               encoding="utf-8").read()
    # Sliced on a real function boundary. Slicing on a phrase from the message
    # cut the body short the moment a comment happened to contain that phrase —
    # the check then read the half of the function without the fix in it.
    equip = src[src.index("async def equippart"):]
    nxt = equip.find("\n    @commands.", 1)
    equip = equip[:nxt if nxt > 0 else len(equip)]
    check("the equip message builds its penalty line from `part_penalties`",
          "part_penalties(part)" in equip)
    check("...and no longer reads the legacy pair directly",
          'part.get("penalty_stat")' not in equip, equip[-400:])

    print("\n── 4. the Special check agrees with the flag ───────────────────")
    from cogs.battle.damage_rules import resolve_special
    non_damage = [b for b in blades.values()
                  if (b.get("special_move") or {}).get("non_damage")]
    check("the roster really contains non-damaging Specials",
          len(non_damage) >= 3, len(non_damage))
    for b in non_damage:
        top = resolve_special(b, BL.stats_at(b, 100, {})["special"])
        check(f"{b['name']}'s Special still deals exactly 0 at level 100",
              top[1] == 0, top)
    damaging = [b for b in blades.values()
                if (b.get("stats") or {}).get("special")
                and not (b.get("special_move") or {}).get("non_damage")]
    grew = sum(1 for b in damaging
               if (resolve_special(b, BL.stats_at(b, 100, {})["special"])[0]
                   * resolve_special(b, BL.stats_at(b, 100, {})["special"])[1])
               > (resolve_special(b, b["stats"]["special"])[0]
                  * resolve_special(b, b["stats"]["special"])[1]))
    check(f"all {len(damaging)} damaging Specials grow with level",
          grew == len(damaging), f"{grew}/{len(damaging)}")

    print("\n" + "=" * 66)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
