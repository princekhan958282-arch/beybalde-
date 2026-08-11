#!/usr/bin/env python3
"""
tools/sim_stats_pipeline.py — one answer to "what are this player's stats?"

Three bugs lived here at once, and they were only visible by comparing screens:

  1. PARTS COUNTED TWICE in PvP. `battle._apply_parts` baked part bonuses into
     the blade, and then `BattleSession` independently added
     `shop.get_part_stat_deltas` on top of that same blade.

  2. PENALTIES DROPPED in two of three implementations. 33 of the 50 parts
     carry `penalty_stat`/`penalty`. `shop.get_part_stat_deltas` applied them;
     `loadout.part_bonuses` and `battle._apply_parts` did not — so one loadout
     produced different numbers on the profile card, on `;info`, in a boss
     fight and in PvP.

  3. LEVELS NEVER REACHED PvP for attack/defence/stamina. Boss, Story and every
     card grow stats along the bey's TYPE archetype. PvP fed the printed stats
     in and leaned on `get_stat_multiplier`, a flat trainer scalar applied
     equally to all three — so levelling raised a defence bey's attack at
     exactly the same rate as its defence, for every bey of every type.

The assertions below are all cross-checks between paths, because every one of
these bugs passed a single-path test.

Run:  python3 tools/sim_stats_pipeline.py
"""
import json
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {detail}")


from cogs.economy.shop import PARTS_CATALOG, get_part_stat_deltas   # noqa: E402
from utils.loadout import part_bonuses, bey_level_and_stats         # noqa: E402
from cogs.battle.battle import _apply_parts                         # noqa: E402
from utils import bey_levels as BL                                  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "data", "beyblades.json"), encoding="utf-8") as fh:
    _doc = json.load(fh)
BLADES = _doc["beyblades"] if isinstance(_doc, dict) and "beyblades" in _doc else _doc
if isinstance(BLADES, dict):
    BLADES = list(BLADES.values())
BY = {b["name"]: b for b in BLADES}


def at_level(name, level):
    """A profile whose copy of `name` sits at `level`."""
    xp = BL.xp_for_level(level) if hasattr(BL, "xp_for_level") else BL.XP_BASE * level * level
    return {"bey_progress": {name: {"xp": xp, "ivs": {}}}}


print("\n── 1. one definition of what a part does ────────────────────────")
PENALTY_PARTS = [p for p in PARTS_CATALOG if p.get("penalty_stat") and p.get("penalty")]
check("most of the catalogue carries a tradeoff",
      len(PENALTY_PARTS) >= 30, f"{len(PENALTY_PARTS)}/{len(PARTS_CATALOG)}")

mismatch = []
for p in PARTS_CATALOG:
    prof = {"equipped_parts": [p["name"]]}
    if part_bonuses(prof) != get_part_stat_deltas([p["name"]]):
        mismatch.append(p["name"])
check("loadout.part_bonuses agrees with shop.get_part_stat_deltas on all 50",
      not mismatch, mismatch[:5])

# The specific drop: a part whose penalty was silently ignored.
hyper = {"equipped_parts": ["Hyper Driver"]}
check("a tradeoff part reports BOTH halves",
      part_bonuses(hyper) == {"attack": 35, "defense": -30},
      part_bonuses(hyper))

# A matched pair genuinely cancels. That is the catalogue's design, and the
# point is that every screen now says so rather than one screen claiming +30.
pair = {"equipped_parts": ["Hyper Driver", "Titan Disk"]}
check("a matched pair nets to zero on the contested stat",
      part_bonuses(pair).get("defense") == 0, part_bonuses(pair))

print("\n── 2. parts are applied EXACTLY once in PvP ─────────────────────")
# _apply_parts runs before BattleSession, which then adds part_deltas itself.
# If _apply_parts touches parts at all, every part counts twice.
blade = BY["Dead Phoenix"]
prof = {"equipped_parts": [p["name"] for p in PARTS_CATALOG[:3]]}
out = _apply_parts(blade, prof)
check("_apply_parts leaves an unlevelled blade byte-identical",
      out["stats"] == blade["stats"], out["stats"])
check("...so the session's part_deltas is the only place parts land",
      part_bonuses(prof) != {} and out["stats"] == blade["stats"])

# With levels AND parts, the parts must still not be in the blade.
prof_lvl = dict(at_level("Dead Phoenix", 100), **prof)
out = _apply_parts(blade, prof_lvl)
lvl = BL.stats_at(blade, 100)
check("with levels, the blade carries the LEVEL and still no parts",
      all(out["stats"][s] == lvl[s] for s in ("attack", "defense", "stamina")),
      {s: (out["stats"][s], lvl[s]) for s in ("attack", "defense", "stamina")})

print("\n── 3. PvP and the boss path agree, blade for blade ──────────────")
# This is the cross-check that would have caught bug 3. `_apply_parts` is what
# PvP feeds BattleSession; BL.stats_at is what loadout.effective_blade uses for
# boss, Story and every card.
drift = []
for b in BLADES:
    p = at_level(b["name"], 100)
    pvp = _apply_parts(b, p)["stats"]
    ref = BL.stats_at(b, 100)
    for s in ("attack", "defense", "stamina"):
        if pvp.get(s) != ref.get(s):
            drift.append((b["name"], s, pvp.get(s), ref.get(s)))
check(f"all {len(BLADES)} blades: PvP stats == boss/Story stats at Lv100",
      not drift, drift[:4])

check("a level-1 bey is untouched (no silent buff for anyone)",
      all(_apply_parts(b, {})["stats"] == b["stats"] for b in BLADES))

print("\n── 4. levelling follows the TYPE archetype in PvP ───────────────")
# The complaint: "every bey gains attack even if it's defence or stamina type".
# Under a flat multiplier every stat rises by the same ratio; under the
# archetype the bey's own stat has to rise fastest.
wrong = []
for b in BLADES:
    t = str(b.get("type", "")).lower()
    key = {"attack": "attack", "defense": "defense", "stamina": "stamina"}.get(t)
    if not key:
        continue                                  # balance grows evenly by design
    p = at_level(b["name"], 100)
    grown = _apply_parts(b, p)["stats"]
    gains = {s: grown[s] - b["stats"][s] for s in ("attack", "defense", "stamina")}
    if max(gains, key=gains.get) != key:
        wrong.append((b["name"], t, gains))
check("every attack/defence/stamina blade gains most in its OWN stat",
      not wrong, wrong[:4])

# And the flat-multiplier signature must be gone: gains must NOT be equal.
flat = []
for b in BLADES:
    if str(b.get("type", "")).lower() == "balance":
        continue
    p = at_level(b["name"], 100)
    grown = _apply_parts(b, p)["stats"]
    gains = {s: grown[s] - b["stats"][s] for s in ("attack", "defense", "stamina")}
    if len(set(gains.values())) == 1:
        flat.append((b["name"], gains))
check("no non-balance blade gains the same amount in all three stats",
      not flat, flat[:4])

# A concrete pair, printed, so the numbers are on the record.
dp = _apply_parts(BY["Dead Phoenix"], at_level("Dead Phoenix", 100))["stats"]
bl = _apply_parts(BY["Bloody Longinus"], at_level("Bloody Longinus", 100))["stats"]
print(f"       Dead Phoenix (Defense) Lv100  atk {dp['attack']}  def {dp['defense']}  sta {dp['stamina']}")
print(f"       Bloody Longinus (Attack) Lv100  atk {bl['attack']}  def {bl['defense']}  sta {bl['stamina']}")
check("the defence blade out-defends the attack blade",
      dp["defense"] > bl["defense"] * 2, (dp["defense"], bl["defense"]))
check("the attack blade out-attacks the defence blade",
      bl["attack"] > dp["attack"] * 1.5, (bl["attack"], dp["attack"]))

print("\n── 5. bey_level_and_stats is the single lookup ──────────────────")
check("no progress entry -> level 1 and the printed stats",
      bey_level_and_stats({}, BY["Dead Phoenix"])
      == (1, BY["Dead Phoenix"]["stats"]))
check("a junk profile degrades to level 1 rather than raising",
      bey_level_and_stats({"bey_progress": "not a dict"},
                          BY["Dead Phoenix"])[0] == 1)
check("a blade with no name degrades to level 1",
      bey_level_and_stats(at_level("Dead Phoenix", 100), {"stats": {}})[0] == 1)
lvl100 = bey_level_and_stats(at_level("Dead Phoenix", 100), BY["Dead Phoenix"])
check("a levelled bey reports its level and grown stats",
      lvl100[0] == 100 and lvl100[1]["defense"] > BY["Dead Phoenix"]["stats"]["defense"],
      lvl100[0])

print("\n── 6. __pycache__ purge deletes caches and nothing else ─────────")
from utils.updater import purge_pycache                          # noqa: E402

tmp = tempfile.mkdtemp()
try:
    kill = ["cogs/battle/__pycache__", "utils/__pycache__", "__pycache__"]
    keep = [".git/__pycache__", "data/__pycache__",
            ".update_backup/x/__pycache__", "venv/lib/site-packages/__pycache__"]
    for d in kill + keep:
        os.makedirs(os.path.join(tmp, d), exist_ok=True)
        with open(os.path.join(tmp, d, "m.cpython-311.pyc"), "wb") as fh:
            fh.write(b"x" * 100)
    os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
    with open(os.path.join(tmp, "data", "users.json"), "w") as fh:
        fh.write('{"real": "player data"}')
    with open(os.path.join(tmp, "app.py"), "w") as fh:
        fh.write("# source")

    removed, freed = purge_pycache(tmp)
    check("every ordinary __pycache__ is removed", removed == len(kill), removed)
    check("...and the bytes are reported", freed == len(kill) * 100, freed)
    left = sorted(os.path.relpath(os.path.join(r, d), tmp)
                  for r, ds, _ in os.walk(tmp) for d in ds if d == "__pycache__")
    check("protected trees are untouched", sorted(left) == sorted(keep), left)
    check("live player data survives",
          open(os.path.join(tmp, "data", "users.json")).read() == '{"real": "player data"}')
    check("source files survive", os.path.exists(os.path.join(tmp, "app.py")))
    check("a second run is a clean no-op", purge_pycache(tmp)[0] == 0)

    # A symlinked cache must never take the delete outside the tree.
    outside = tempfile.mkdtemp()
    with open(os.path.join(outside, "precious.txt"), "w") as fh:
        fh.write("do not delete")
    try:
        os.symlink(outside, os.path.join(tmp, "cogs", "__pycache__"))
        purge_pycache(tmp)
        check("a symlinked __pycache__ is refused, not followed",
              os.path.exists(os.path.join(outside, "precious.txt")))
    except OSError:
        check("a symlinked __pycache__ is refused, not followed (no symlink support)",
              True)
    finally:
        shutil.rmtree(outside, ignore_errors=True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
