#!/usr/bin/env python3
"""
tools/sim_booster_beys.py — the two X boosters and Master Diabolos.

The X blades are the easy half: they must be strictly better than the Rare
starters they upgrade, on every line and in every effect, or the booster is a
sidegrade nobody should buy.

Master Diabolos is the interesting one. A dual-spin blade only means anything
if the chosen mode reaches COMBAT, and there are two independent stat paths to
reach — `loadout.effective_blade` (boss, Story, every card) and
`battle._apply_parts` (PvP). A mode that only changes the `;info` card is
cosmetic, and cosmetic is exactly what this would silently be if either path
were missed. Both are checked, in both modes.

Run:  python3 tools/sim_booster_beys.py
"""
import asyncio
import json
import os
import sys

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


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = json.load(open(os.path.join(ROOT, "data", "beyblades.json"),
                    encoding="utf-8"))

UPGRADES = {"Victory Valkyrie X": "Victory Valkyrie",
            "Storm Spriggan X": "Storm Spriggan"}

print("\n── 1. the three blades exist ────────────────────────────────────")
for n in list(UPGRADES) + ["Master Diabolos"]:
    check(f"{n} is in the roster", n in DB)
# A floor, not an equality. An exact count fails the moment the NEXT blade
# ships, which says nothing about these three and has already cost a red run.
check("the roster is at least 87 blades", len(DB) >= 87, len(DB))
ids = [b["id"] for b in DB.values()]
check("every id is still unique", len(set(ids)) == len(ids),
      len(ids) - len(set(ids)))
for n in list(UPGRADES) + ["Master Diabolos"]:
    check(f"{n}: image is a renderable CDN link",
          str(DB[n].get("image_url", "")).startswith(
              "https://cdn.discordapp.com/attachments/"),
          str(DB[n].get("image_url"))[:50])

print("\n── 2. the X blades beat what they upgrade ───────────────────────")
for x, base in UPGRADES.items():
    bx, bb = DB[x], DB[base]
    check(f"{x} is Epic", bx["rarity"] == "Epic", bx["rarity"])
    check(f"...and {base} was Rare", bb["rarity"] == "Rare")
    check(f"{x} keeps the same type", bx["type"] == bb["type"],
          (bx["type"], bb["type"]))
    worse = {k: (bx["stats"][k], bb["stats"][k]) for k in bb["stats"]
             if bx["stats"][k] <= bb["stats"][k]}
    check(f"{x} is strictly better on EVERY stat", not worse, worse)
    # "Same ability and special" — the identity has to survive the upgrade, or
    # it is a different blade wearing the name.
    check(f"{x} keeps the ability name (+ X)",
          bx["ability"]["name"].startswith(bb["ability"]["name"]),
          (bx["ability"]["name"], bb["ability"]["name"]))
    check(f"{x} keeps the special name (+ X)",
          bx["special_move"]["name"].startswith(bb["special_move"]["name"]),
          (bx["special_move"]["name"], bb["special_move"]["name"]))
    check(f"{x}: the special hits harder",
          bx["special_move"]["total_damage"] > bb["special_move"]["total_damage"],
          (bx["special_move"]["total_damage"],
           bb["special_move"]["total_damage"]))
    check(f"{x} is booster-only, so it cannot spawn",
          bx.get("booster_exclusive") is True)
    check(f"...and {base} still can", not bb.get("booster_exclusive"))

# Shape of the two upgraded specials, which were specified numerically.
vx = DB["Victory Valkyrie X"]["special_move"]
check("Rush Launch X is still 16 hits", vx["hits"] == 16, vx["hits"])
check("...at 15 each instead of 10", vx["damage_per_hit"] == 15,
      vx["damage_per_hit"])
sx = DB["Storm Spriggan X"]["special_move"]
check("Counter Break X hits harder than 130", sx["damage_per_hit"] > 130,
      sx["damage_per_hit"])

print("\n── 3. the X effects really are stronger ─────────────────────────")


def ops_of(blade, want):
    out = []
    for ab in blade.get("abilities") or []:
        for rule in ab.get("rules") or []:
            for op in rule.get("do") or []:
                if op.get("op") == want:
                    out.append(op)
    return out


for x, base in UPGRADES.items():
    for op_name, field in (("stacking_buff", "per_stack"),
                           ("crit_chance", "value"),
                           ("reflect_pct", "value"),
                           ("heal", "value")):
        xs = [o.get(field, 0) for o in ops_of(DB[x], op_name)]
        bs = [o.get(field, 0) for o in ops_of(DB[base], op_name)]
        if not bs:
            continue
        check(f"{x}: {op_name} is stronger ({max(bs)} -> {max(xs)})",
              xs and max(xs) > max(bs), (bs, xs))

check("Valkyrie X crits harder as well as more often",
      any(o.get("value", 0) > 1.5 for o in ops_of(DB["Victory Valkyrie X"],
                                                  "crit_damage")))
check("Spriggan X's counter also leaves a shield",
      bool(ops_of(DB["Storm Spriggan X"], "shield")))

print("\n── 4. Master Diabolos is a real dual-spin blade ─────────────────")
from utils.spin_mode import (is_dual, modes, chosen, resolve, label,  # noqa: E402
                             DEFAULT_MODE)

md = DB["Master Diabolos"]
check("it is Legendary", md["rarity"] == "Legendary", md["rarity"])
check("it is flagged dual_spin", is_dual(md))
check("spin_direction reads Dual", md["spin_direction"] == "Dual",
      md["spin_direction"])
check("it has exactly two modes", modes(md) == ["Right", "Left"], modes(md))
check("it can spawn — it is not booster-only",
      not md.get("booster_exclusive"))

WANT = {
    "Right": {"attack": 136, "defense": 100, "stamina": 103, "hp": 110},
    "Left":  {"attack": 118, "defense": 115, "stamina": 106, "hp": 110},
}
for mode, want in WANT.items():
    got = md["spin_modes"][mode]["stats"]
    bad = {k: (got[k], v) for k, v in want.items() if got[k] != v}
    check(f"{mode} mode has the specified statline", not bad, bad)
check("Right mode's special is Master Smash",
      md["spin_modes"]["Right"]["special_move"]["name"] == "Master Smash")
check("Left mode's special is Master Upper",
      md["spin_modes"]["Left"]["special_move"]["name"] == "Master Upper")
check("Right mode really spins right",
      md["spin_modes"]["Right"]["spin_direction"] == "Right")
check("Left mode really spins left",
      md["spin_modes"]["Left"]["spin_direction"] == "Left")
check("both named abilities are present",
      [a["name"] for a in md["abilities"]]
      == ["Flippable Master Layer", "Dual-Spin Capability"],
      [a["name"] for a in md["abilities"]])

# The top-level mirror. Every reader that has never heard of dual spin — the
# shop, the quiz, the marketplace — still has to see a complete blade.
check("the top-level stats mirror the default mode",
      md["stats"] == md["spin_modes"][DEFAULT_MODE]["stats"])
check("...and so does the top-level special",
      md["special_move"]["name"]
      == md["spin_modes"][DEFAULT_MODE]["special_move"]["name"])

print("\n── 5. the chosen mode reaches BOTH stat paths ───────────────────")
check("no stored choice falls back to the default",
      chosen({}, md) == DEFAULT_MODE, chosen({}, md))
check("a stored choice is honoured",
      chosen({"spin_mode": {"Master Diabolos": "Left"}}, md) == "Left")
check("a mode that does not exist falls back rather than breaking",
      chosen({"spin_mode": {"Master Diabolos": "Sideways"}}, md)
      == DEFAULT_MODE)
check("case is normalised",
      chosen({"spin_mode": {"Master Diabolos": "left"}}, md) == "Left")
check("a non-dual blade has no modes and no choice",
      modes(DB["Dranzer"]) == [] and chosen({}, DB["Dranzer"]) == "")
check("...and is returned untouched, not copied",
      resolve({}, DB["Dranzer"]) is DB["Dranzer"])

# Path 1: loadout.effective_blade — boss, Story, and every card.
# Path 2: battle._apply_parts — PvP.
# A mode that reaches one but not the other is a blade that fights as two
# different things depending on the opponent.
from cogs.battle.battle import _apply_parts                     # noqa: E402
import utils.database as DBASE                                  # noqa: E402
from utils.loadout import effective_blade                       # noqa: E402

for mode in ("Right", "Left"):
    prof = {"spin_mode": {"Master Diabolos": mode}}
    want = md["spin_modes"][mode]["stats"]

    pvp = _apply_parts(md, prof)["stats"]
    bad = {k: (pvp[k], want[k]) for k in ("attack", "defense", "stamina")
           if pvp[k] != want[k]}
    check(f"PvP fights {mode} mode's statline", not bad, bad)

    _real = DBASE.get_user
    async def _fake_get_user(uid, _p=prof):
        return _p
    DBASE.get_user = _fake_get_user
    try:
        eff, _bd, _av = asyncio.run(
            effective_blade(1, prof, md, include_avatar=False))
    finally:
        DBASE.get_user = _real
    bad = {k: (eff["stats"][k], want[k])
           for k in ("attack", "defense", "stamina")
           if eff["stats"][k] != want[k]}
    check(f"boss/Story fight {mode} mode's statline too", not bad, bad)
    check(f"...and pick up {mode} mode's special",
          eff["special_move"]["name"]
          == md["spin_modes"][mode]["special_move"]["name"],
          eff["special_move"]["name"])
    check(f"...and {mode} mode's spin direction",
          eff["spin_direction"] == md["spin_modes"][mode]["spin_direction"])

print("\n── 6. the spin conditions the ability needs ─────────────────────")
# `opposite_spin` / `same_spin` did not exist, and an unknown condition FAILS
# CLOSED — so Dual-Spin Capability would have been silently inert.
import types                                                    # noqa: E402
from cogs.battle.status_manager import StatusManager             # noqa: E402
from cogs.battle.stamina_manager import StaminaManager           # noqa: E402
from cogs.abilities.ability_engine import AbilityEngine          # noqa: E402


class Stub:
    def __init__(self, b1, b2):
        self.blades = {"1": b1, "2": b2}
        self.hp = {"1": 700, "2": 700}
        self.max_hp = 700
        self.max_hp_per_player = {"1": 700, "2": 700}
        self.last_moves = {}
        self.round = 1
        self.status = StatusManager(self)
        self.stamina_manager = StaminaManager(self.blades)
        self.chain_handler = types.SimpleNamespace(resolve=lambda *a, **k: [])


def cond(mine, theirs, name):
    s = Stub({"name": "A", "spin_direction": mine},
             {"name": "B", "spin_direction": theirs})
    e = AbilityEngine(s)
    s.ability = e
    return e._check({"cond": name}, "1", "2", "", "")


check("Right vs Left is opposite", cond("Right", "Left", "opposite_spin"))
check("Left vs Right is opposite", cond("Left", "Right", "opposite_spin"))
check("Right vs Right is NOT opposite",
      not cond("Right", "Right", "opposite_spin"))
check("Right vs Right is same", cond("Right", "Right", "same_spin"))
check("Left vs Left is same", cond("Left", "Left", "same_spin"))
check("Right vs Left is NOT same", not cond("Right", "Left", "same_spin"))
check("an unresolved Dual counts as neither",
      not cond("Dual", "Right", "opposite_spin")
      and not cond("Dual", "Right", "same_spin"))
check("a typo'd condition still fails closed",
      not cond("Right", "Left", "opposit_spin"))

# And the ability fires end to end, in the mode that should trigger it.
right = resolve({"spin_mode": {"Master Diabolos": "Right"}}, md)
s = Stub(right, {"name": "Lefty", "spin_direction": "Left", "stats": {}})
e = AbilityEngine(s)
s.ability = e
for k in ("1", "2"):
    e.setup(k, s.blades[k])
before = s.stamina_manager.stamina["2"]
dealt, _taken, _logs = e.apply("1", "2", s.blades["1"], s.blades["2"],
                               "attack", "win", 100, 0)
check("against an opposite spin, Dual-Spin Capability adds damage",
      dealt > 100, dealt)
check("...and drains the opponent's stamina",
      s.stamina_manager.stamina["2"] < before,
      (before, s.stamina_manager.stamina["2"]))

s2 = Stub(right, {"name": "Righty", "spin_direction": "Right", "stats": {}})
e2 = AbilityEngine(s2)
s2.ability = e2
for k in ("1", "2"):
    e2.setup(k, s2.blades[k])
d2, _t, _l = e2.apply("1", "2", s2.blades["1"], s2.blades["2"],
                      "attack", "win", 100, 0)
check("against the same spin it does NOT add damage", d2 <= dealt, (dealt, d2))

print("\n── 7. the ;info picker ──────────────────────────────────────────")
src = open(os.path.join(ROOT, "cogs", "economy", "profile.py"),
           encoding="utf-8").read()
check("a SpinModeView exists", "class SpinModeView" in src)
check("the card path attaches it only for dual blades",
      "SpinModeView.create(ctx.author, blade, self) if is_dual(blade) else None" in src)
check("the button acknowledges BEFORE re-rendering",
      src.index("await interaction.response.send_message")
      < src.index("await self.cog._send_bey_card"))
check("it refuses somebody else's card",
      "That's not your Beyblade" in src)
check("the choice is persisted, not just displayed", "set_choice(" in src)
# The bug that took the skill picker down: discord.ui.Item.parent has no
# setter. Nothing here may assign a reserved attribute.
check("the view stores no attribute discord reserves",
      ".parent = " not in src)

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
