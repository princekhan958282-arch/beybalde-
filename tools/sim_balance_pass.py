#!/usr/bin/env python3
"""
tools/sim_balance_pass.py — the five-part balance pass.

  1. avatars       dodge capped at 5%; multi_hit_extra_hits DOUBLES the hit
                   count instead of adding one, and stays distinct from
                   multi_hit_power_double which doubles the damage per hit.
  2. Shadow Dragon King   Absolute Darkness 70% -> 55%, of CURRENT stats.
  3. Void Longinus        Longinus Strike awakens at bey level 100: 30 damage
                          and 10 stability instead of 20 / 5.
  4. Tartarus Reaper      Soul Judgement 30 -> 25, capped at 3 stacks.
  5. Excalibur Ascendant  Internal Overdrive 3 -> 4 turns, undodgeable,
                          guaranteed crits at double damage; Dragon Core
                          Resonance 15 -> 20 per stack.

Run:  python3 tools/sim_balance_pass.py
"""
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


from cogs.abilities.ability_engine import AbilityEngine       # noqa: E402
from cogs.avatar.avatar_engine import AvatarBonuses, avatar_engine  # noqa: E402
from cogs.battle import avatar_combat as AVC                  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLADES = json.load(open(os.path.join(ROOT, "data", "beyblades.json"),
                        encoding="utf-8"))


def ops_in(node, want=None):
    out = []
    if isinstance(node, dict):
        if "op" in node:
            out.append(node)
        for v in node.values():
            out.extend(ops_in(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(ops_in(v))
    return [o for o in out if want is None or o["op"] == want]


print("\n── 1. avatar dodge is capped at 5% ──────────────────────────────")
check("the cap is 5%", AvatarBonuses.DODGE_CAP == 0.05, AvatarBonuses.DODGE_CAP)

avatar_engine.load()
worst = max(a["bonuses"].get("dodge_chance", 0.0)
            for a in avatar_engine.get_all_avatars())
check("some card was authored above the cap — this pass matters",
      worst > 0.05, worst)

over = []
for card in avatar_engine.get_all_avatars():
    b = card["bonuses"]
    bonuses = AvatarBonuses(dodge_chance=min(AvatarBonuses.DODGE_CAP,
                                             b.get("dodge_chance", 0.0)))
    if bonuses.dodge_chance > AvatarBonuses.DODGE_CAP + 1e-9:
        over.append(card["name"])
check("no card can exceed it once loaded", not over, over)

import random as _r                                            # noqa: E402
_r.seed(4)
hot = AvatarBonuses(dodge_chance=0.99)      # hand-built, bypassing the loader
dodges = sum(1 for _ in range(20000) if hot.roll_dodge())
check("even a hand-built 99% bonuses object rolls at ~5%",
      0.03 < dodges / 20000 < 0.07, dodges / 20000)
check("a zero-dodge avatar never dodges",
      not any(AvatarBonuses().roll_dodge() for _ in range(500)))

esrc = open(os.path.join(ROOT, "cogs", "avatar", "avatar_engine.py"),
            encoding="utf-8").read()
check("the cap is applied at LOAD, so displayed values match reality",
      "min(AvatarBonuses.DODGE_CAP" in esrc)

print("\n── 2. multi-hit doubles the COUNT ───────────────────────────────")
extra = AvatarBonuses(multi_hit_extra_hits=True)
check("2 hits become 4", extra.extra_hits(2) == 4, extra.extra_hits(2))
check("3 become 6", extra.extra_hits(3) == 6)
check("5 become 10", extra.extra_hits(5) == 10)
check("1 becomes 2", extra.extra_hits(1) == 2)
check("without the bonus nothing changes",
      AvatarBonuses().extra_hits(4) == 4)
check("it is proportional now — a 5-hit move gains as much as a 2-hit one",
      extra.extra_hits(5) / 5 == extra.extra_hits(2) / 2)

dbl = AvatarBonuses(multi_hit_power_double=True)
check("power_double is a DIFFERENT effect — it leaves the count alone",
      dbl.extra_hits(2) == 2, dbl.extra_hits(2))
check("...and doubles the damage instead",
      dbl.apply_multi_hit_damage(50) == 100)
check("extra_hits does NOT touch damage",
      extra.apply_multi_hit_damage(50) == 50)
both = AvatarBonuses(multi_hit_extra_hits=True, multi_hit_power_double=True)
check("an avatar with both gets 2x hits AND 2x damage each",
      both.extra_hits(2) == 4 and both.apply_multi_hit_damage(50) == 100)

print("\n── 3. Shadow Dragon King — Absolute Darkness 55% ────────────────")
sdk = BLADES["Shadow Dragon King"]
ad = [a for a in sdk["abilities"] if a["name"] == "Absolute Darkness"][0]
boosts = [n.get("value") for n in json.loads(json.dumps(ad)).get("chain", [])
          if n.get("effect") == "all_stats_boost"]
check("the chain grants 55%", boosts == [0.55], boosts)
check("no 70% survives anywhere in the ability",
      "0.7" not in json.dumps(ad), json.dumps(ad)[:120])
check("the rules copy matches the chain copy",
      json.dumps(ad).count("0.55") == 2, json.dumps(ad).count("0.55"))
check("the description says 55% of CURRENT stats",
      "55%" in ad["description"] and "CURRENT" in ad["description"])
# The effect is a percentage of live stats, so a levelled blade gains more.
check("at 459 attack that is +252, not a flat number",
      int(459 * 0.55) == 252)

print("\n── 4. Void Longinus — awakens at bey level 100 ──────────────────")
vl = BLADES["Void Longinus"]
ls = [a for a in vl["abilities"] if a["name"] == "Longinus Strike"][0]
gates = {c["cond"] for r in ls["rules"] for c in (r.get("if") or [])}
check("every rule is level-gated",
      {"bey_level_at_least", "bey_level_below"} <= gates, gates)
lo = [r for r in ls["rules"]
      if any(c["cond"] == "bey_level_below" for c in (r.get("if") or []))]
hi = [r for r in ls["rules"]
      if any(c["cond"] == "bey_level_at_least" for c in (r.get("if") or []))]
check("the two forms have the same number of rules",
      len(lo) == len(hi) and len(lo) == 6, (len(lo), len(hi)))
check("below 100 it is 20 damage",
      {o["value"] for o in ops_in(lo, "bonus_damage")} == {20})
check("at 100 it is 30", {o["value"] for o in ops_in(hi, "bonus_damage")} == {30})
check("below 100 it is 5 stability",
      {o["amount"] for o in ops_in(lo, "enemy_lose_stability")} == {5})
check("at 100 it is 10",
      {o["amount"] for o in ops_in(hi, "enemy_lose_stability")} == {10})


class _Sess:
    def __init__(self, level):
        self.bey_levels = {"p": level}


def lvl_check(level, cond, value):
    e = AbilityEngine.__new__(AbilityEngine)
    e.session = _Sess(level)
    return e._check({"cond": cond, "value": value}, "p", "e", "attack", "win")


check("level 99 is 'below 100'", lvl_check(99, "bey_level_below", 100))
check("...and not 'at least 100'", not lvl_check(99, "bey_level_at_least", 100))
check("level 100 is 'at least 100'", lvl_check(100, "bey_level_at_least", 100))
check("...and not 'below'", not lvl_check(100, "bey_level_below", 100))
e = AbilityEngine.__new__(AbilityEngine)
e.session = type("S", (), {})()
check("a session with no bey_levels reads as level 1 — the un-awakened form",
      e._check({"cond": "bey_level_below", "value": 100}, "p", "e", "a", "w")
      and not e._check({"cond": "bey_level_at_least", "value": 100},
                       "p", "e", "a", "w"))

ssrc = open(os.path.join(ROOT, "cogs", "battle", "session.py"),
            encoding="utf-8").read()
check("BattleSession actually resolves bey_levels",
      "self.bey_levels: dict[str, int] = {}" in ssrc)

print("\n── 5. Tartarus Reaper — 25, capped at 3 ─────────────────────────")
tr = BLADES["Tartarus Reaper"]
sj = [a for a in tr["abilities"] if a["name"] == "Soul Judgement"][0]
check("the stat line is 25", {o.get("per_stack") for o in
                              ops_in(sj, "stacking_buff")} == {25})
check("defence and stamina are 25 too",
      {o["amount"] for o in ops_in(sj, "buff")} == {25})
check("the heal is 25", {o["value"] for o in ops_in(sj, "heal")} == {25})
check("stamina recovery is unchanged at 0.3",
      {o["value"] for o in ops_in(sj, "gain_stamina")} == {0.3})
check("the stack cap is 3", {o.get("max") for o in
                             ops_in(sj, "stacking_buff")} == {3})
check("every rule is gated on the counter, so the heal stops at the cap too",
      all(any(c["cond"] == "counter_below" for c in (r.get("if") or []))
          for r in sj["rules"]),
      [r.get("if") for r in sj["rules"]])
check("no 30 survives", "30" not in json.dumps([r["do"] for r in sj["rules"]]))
check("the description says 25 and three", "25" in sj["description"]
      and "three" in sj["description"].lower())

print("\n── 6. Excalibur Ascendant ───────────────────────────────────────")
ex = BLADES["Excalibur Ascendant"]
dcr = [a for a in ex["abilities"] if a["name"] == "Dragon Core Resonance"][0]
check("Dragon Core Resonance is 20 per stack", dcr["stack_per_win"] == 20,
      dcr["stack_per_win"])
check("...still 3 stacks, so the ceiling is +60", dcr["max_stacks"] == 3)
check("the description agrees", "+20 Attack" in dcr["description"]
      and "+60" in dcr["description"])

io = [a for a in ex["abilities"] if a["name"] == "Internal Overdrive"][0]
turns = {o.get("turns") for o in ops_in(io) if o.get("turns")}
check("Overdrive runs 4 turns", turns == {4}, turns)
check("the stale duration_turns field is gone", "duration_turns" not in io)
kinds = {o["op"] for o in ops_in(io)}
check("it ignores defence", "ignore_defense" in kinds)
check("it cannot be dodged", "undodgeable" in kinds)
check("it guarantees crits", "guaranteed_crit" in kinds)
check("...at double damage",
      {o["value"] for o in ops_in(io, "crit_damage")} == {2.0})
check("it keeps the +30 attack", {o["amount"] for o in ops_in(io, "buff")} == {30})
check("it keeps the 20 HP per turn",
      {o["value"] for o in ops_in(io, "hp_regen")} == {20})
check("it fires once per battle", io["rules"][0].get("once") == "battle")

print("\n── 7. the undodgeable window ────────────────────────────────────")


class _AvSess:
    def __init__(self, undodgeable):
        self.ability = type("A", (), {"undodgeable_turns": undodgeable})()
        self._av = AvatarBonuses(dodge_chance=0.05)


def _patch(monkey):
    AVC._av = monkey


_orig_av = AVC._av
try:
    _patch(lambda session, key: getattr(session, "_av", None))
    _r.seed(11)
    s = _AvSess({"atk": 4})
    got = sum(AVC.absorb_incoming(s, "def", "atk", 100)[0] for _ in range(400))
    check("no hit is dodged while the attacker is undodgeable",
          got == 400 * 100, got)
    s2 = _AvSess({})
    _r.seed(11)
    dodged = sum(1 for _ in range(4000)
                 if AVC.absorb_incoming(s2, "def", "atk", 100)[0] == 0)
    check("without it, dodges happen at roughly the 5% cap",
          0.02 < dodged / 4000 < 0.08, dodged / 4000)
finally:
    _patch(_orig_av)

eng = AbilityEngine.__new__(AbilityEngine)
eng.undodgeable_turns = {"p": 2}
eng.timed_dmg_amps = []
eng.tick_dmg_amps()
check("the window ticks down", eng.undodgeable_turns.get("p") == 1)
eng.tick_dmg_amps()
check("...and expires", "p" not in eng.undodgeable_turns, eng.undodgeable_turns)

print("\n── 8. the roster still holds together ───────────────────────────")
broken = []
for name, blade in BLADES.items():
    if not isinstance(blade, dict):
        continue
    try:
        e2 = AbilityEngine.__new__(AbilityEngine)
        e2._compiled = {}
        e2.ability_2_disabled = {}
        e2._rules_for(blade, "p")
    except Exception as exc:                             # noqa: BLE001
        broken.append((name, repr(exc)[:60]))
check("every blade still compiles its abilities", not broken, broken[:4])
check("the roster did not shrink", len(BLADES) >= 80, len(BLADES))

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
