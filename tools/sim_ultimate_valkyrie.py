#!/usr/bin/env python3
"""
tools/sim_ultimate_valkyrie.py — Ultimate Valkyrie and its Black Edition.

Three things here can fail silently, so all three are checked end to end
rather than by reading the JSON:

1. **The drawback might not exist at all.** `stamina_cost_increase` is a new
   op. Its neighbour `stamina_cost_reduction` clamps to 0.0–0.9, so before
   this build there was no way to make a move cost MORE — an ability that
   claimed to would have rendered perfectly in `;info` and changed no number
   in any battle. The op is driven through a real StaminaManager here.

2. **The level-100 clause might be permanent or never apply.** The surcharge
   is gated by `bey_level_below`, evaluated at setup. Both sides of the gate
   are fired through a real AbilityEngine, at 99 and at 100.

3. **The hidden drop might be visible, or reachable.** The Black Edition must
   be out of the weighted pool entirely (so it can never be rolled normally),
   out of spawns, and its 1-in-5,000,000 must not appear in any string a
   player can read.

Run:  python3 tools/sim_ultimate_valkyrie.py
"""
import json
import os
import random
import sys
import types

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

BASE = "Ultimate Valkyrie"
BLACK = "Ultimate Valkyrie (Black Edition)"
WANT = {"attack": 165, "defense": 40, "stamina": 117, "hp": 134}

print("\n── 1. both blades exist and carry the specified statline ────────")
for n in (BASE, BLACK):
    check(f"{n} is in the roster", n in DB)
check("the roster is at least 89 blades", len(DB) >= 89, len(DB))
ids = [b["id"] for b in DB.values()]
check("every id is still unique", len(set(ids)) == len(ids),
      len(ids) - len(set(ids)))

for n in (BASE, BLACK):
    b = DB[n]
    bad = {k: (b["stats"].get(k), v) for k, v in WANT.items()
           if b["stats"].get(k) != v}
    check(f"{n}: hp 134 / atk 165 / def 40 / sta 117", not bad, bad)
    check(f"{n} is Ultimate rarity", b["rarity"] == "Ultimate", b["rarity"])
    check(f"{n} spins right", b["spin_direction"] == "Right",
          b.get("spin_direction"))
    check(f"{n} is an Attack type", b["type"] == "Attack", b["type"])
    check(f"{n}: image is a renderable CDN link",
          str(b.get("image_url", "")).startswith(
              "https://cdn.discordapp.com/attachments/"),
          str(b.get("image_url"))[:50])

check("the Black Edition really is the SAME statline",
      DB[BASE]["stats"] == DB[BLACK]["stats"],
      (DB[BASE]["stats"], DB[BLACK]["stats"]))

print("\n── 2. the ability is wired, not just described ──────────────────")
# The recurring trap in this roster: the engine runs `abilities` (the plural
# list) and the singular `ability` is display only. A blade authored with the
# singular alone has an ability that never fires.
for n in (BASE, BLACK):
    b = DB[n]
    check(f"{n} has the plural `abilities` the engine actually reads",
          bool(b.get("abilities")))
    check(f"{n}: the ability is named Ultimate Blade",
          b["ability"]["name"] == "Ultimate Blade", b["ability"]["name"])
    check(f"{n}: the plural mirrors the singular's name",
          b["abilities"][0]["name"] == b["ability"]["name"])
    rules = b["abilities"][0]["rules"]
    whens = [r["when"] for r in rules]
    check(f"{n} hits harder on a normal attack", "on_attack_hit" in whens)
    check(f"{n} hits harder on each Special hit", "on_hit" in whens)
    check(f"{n} sets up its crit rate at battle start", "setup" in whens)


def ops_of(name, want, when=None):
    out = []
    for ab in DB[name].get("abilities") or []:
        for rule in ab.get("rules") or []:
            if when and rule.get("when") != when:
                continue
            for op in rule.get("do") or []:
                if op.get("op") == want:
                    out.append((rule, op))
    return out


print("\n── 3. the level-100 clause gates the DRAWBACK, nothing else ─────")
for n in (BASE, BLACK):
    inc = ops_of(n, "stamina_cost_increase")
    check(f"{n} has exactly one stamina surcharge rule", len(inc) == 1,
          len(inc))
    rule, op = inc[0]
    check(f"{n}: the surcharge is applied at setup",
          rule["when"] == "setup", rule["when"])
    check(f"{n}: it is gated on bey_level_below 100",
          any(c.get("cond") == "bey_level_below" and c.get("value") == 100
              for c in rule.get("if") or []), rule.get("if"))
    check(f"{n}: it costs Attack and Special, and only those",
          sorted(op.get("moves") or []) == ["attack", "special"],
          op.get("moves"))
    # The offence half must NOT be gated — the user asked for the effect
    # (the extra cost) to be removed at 100, not the ability.
    for kind in ("bonus_damage_pct", "crit_chance"):
        gated = [r for r, _o in ops_of(n, kind) if r.get("if")]
        check(f"{n}: {kind} is never level-gated", not gated,
              [r.get("_name") for r in gated])

print("\n── 4. the Black Edition is strictly the better blade ────────────")
for kind, field in (("bonus_damage_pct", "value"), ("crit_chance", "value")):
    hi = [o.get(field, 0) for _r, o in ops_of(BLACK, kind)]
    lo = [o.get(field, 0) for _r, o in ops_of(BASE, kind)]
    check(f"Black Edition's {kind} is higher ({max(lo)} -> {max(hi)})",
          hi and lo and max(hi) > max(lo), (lo, hi))
check("Black Edition crits harder as well as more often",
      any(o.get("value", 0) > 1.5 for _r, o in ops_of(BLACK, "crit_damage")))
b_inc = ops_of(BLACK, "stamina_cost_increase")[0][1]["value"]
w_inc = ops_of(BASE, "stamina_cost_increase")[0][1]["value"]
check(f"...and pays a LIGHTER surcharge for it ({w_inc}% -> {b_inc}%)",
      b_inc < w_inc, (w_inc, b_inc))
check("Black Edition still carries the same ability NAME",
      DB[BLACK]["ability"]["name"] == DB[BASE]["ability"]["name"])

print("\n── 5. the two Specials ──────────────────────────────────────────")
for n, want_name, want_total in ((BASE, "Ultimate V", 185),
                                 (BLACK, "Ultimate Wing V", 190)):
    sm = DB[n]["special_move"]
    check(f"{n}: the Special is {want_name}", sm["name"] == want_name,
          sm["name"])
    check(f"{want_name} totals {want_total}",
          sm["total_damage"] == want_total, sm["total_damage"])
    check(f"{want_name}: hits x damage really is the stated total",
          sm["hits"] * sm["damage_per_hit"] == sm["total_damage"],
          (sm["hits"], sm["damage_per_hit"], sm["total_damage"]))
    check(f"{want_name} has flavour to print", bool(sm.get("flavour_texts")))
check("Ultimate Wing V out-damages Ultimate V",
      DB[BLACK]["special_move"]["total_damage"]
      > DB[BASE]["special_move"]["total_damage"])
check("Ultimate V pierces defence",
      bool(ops_of(BASE, "ignore_defense", "on_special")))
check("Ultimate Wing V pierces defence AND cannot be dodged",
      bool(ops_of(BLACK, "ignore_defense", "on_special"))
      and bool(ops_of(BLACK, "undodgeable", "on_special")))

print("\n── 6. the surcharge changes a real stamina number ───────────────")
from cogs.battle.stamina_manager import StaminaManager, STAMINA_COST  # noqa: E402
from cogs.core.constants import (MOVE_ATTACK, MOVE_SPECIAL, MOVE_DEFENSE,  # noqa: E402
                                 MOVE_CHARGE, MOVE_STAMINA)

blades = {"1": dict(DB[BASE]), "2": dict(DB[BASE])}
sm = StaminaManager(blades)
sm.stamina = {"1": 100.0, "2": 100.0}
sm.max_stamina = {"1": 100.0, "2": 100.0}


def cost_of(mgr, key, move):
    before = mgr.stamina[key]
    mgr.deduct_cost(key, move)
    spent = round(before - mgr.stamina[key], 2)
    mgr.stamina[key] = before
    return spent


check("a fresh manager charges nobody a surcharge",
      cost_of(sm, "1", MOVE_ATTACK) == STAMINA_COST[MOVE_ATTACK],
      cost_of(sm, "1", MOVE_ATTACK))

sm.cost_increase["1"] = 0.35
want_atk = round(STAMINA_COST[MOVE_ATTACK] * 1.35, 2)
want_spc = round(STAMINA_COST[MOVE_SPECIAL] * 1.35, 2)
check(f"+35% makes Attack cost {want_atk} instead of "
      f"{STAMINA_COST[MOVE_ATTACK]}",
      cost_of(sm, "1", MOVE_ATTACK) == want_atk, cost_of(sm, "1", MOVE_ATTACK))
check(f"...and Special {want_spc} instead of {STAMINA_COST[MOVE_SPECIAL]}",
      cost_of(sm, "1", MOVE_SPECIAL) == want_spc,
      cost_of(sm, "1", MOVE_SPECIAL))
check("Defense is NOT surcharged — the drawback is offensive",
      cost_of(sm, "1", MOVE_DEFENSE) == STAMINA_COST[MOVE_DEFENSE],
      cost_of(sm, "1", MOVE_DEFENSE))
check("neither is Charge",
      cost_of(sm, "1", MOVE_CHARGE) == STAMINA_COST[MOVE_CHARGE])
check("the free Stamina move stays free",
      cost_of(sm, "1", MOVE_STAMINA) == 0.0)
check("the OPPONENT is untouched by it",
      cost_of(sm, "2", MOVE_ATTACK) == STAMINA_COST[MOVE_ATTACK],
      cost_of(sm, "2", MOVE_ATTACK))
check("the surcharge really is a cost INCREASE, not a rounding wobble",
      cost_of(sm, "1", MOVE_ATTACK) > cost_of(sm, "2", MOVE_ATTACK))

# A blade carrying both halves must not have one silently cancel the other.
sm.drain_reduction["1"] = 0.5
check("a discount applies on top of the surcharge, not instead of it",
      cost_of(sm, "1", MOVE_ATTACK)
      == round(round(STAMINA_COST[MOVE_ATTACK] * 1.35, 2) * 0.5, 2),
      cost_of(sm, "1", MOVE_ATTACK))
sm.drain_reduction["1"] = 0.0

print("\n── 7. the gate fires at 99 and is gone at 100 ───────────────────")
from cogs.battle.status_manager import StatusManager               # noqa: E402
from cogs.abilities.ability_engine import AbilityEngine            # noqa: E402


class Stub:
    def __init__(self, blade, level):
        self.blades = {"1": blade, "2": dict(DB["Dranzer"])}
        self.hp = {"1": 700, "2": 700}
        self.max_hp = 700
        self.max_hp_per_player = {"1": 700, "2": 700}
        self.bey_levels = {"1": level, "2": 1}
        self.last_moves = {}
        self.round = 1
        self.status = StatusManager(self)
        self.stamina_manager = StaminaManager(self.blades)
        self.chain_handler = types.SimpleNamespace(resolve=lambda *a, **k: [])


def surcharge_at(name, level):
    s = Stub(dict(DB[name]), level)
    e = AbilityEngine(s)
    s.ability = e
    for k in ("1", "2"):
        e.setup(k, s.blades[k])
    return s.stamina_manager.cost_increase.get("1", 0.0), s, e


for n, pct in ((BASE, 0.35), (BLACK, 0.20)):
    got, sess, _e = surcharge_at(n, 1)
    check(f"{n} at level 1 pays +{int(pct * 100)}%", got == pct, got)
    check(f"{n} at level 1: the surcharge lists attack and special",
          sorted(sess.stamina_manager.cost_increase_moves["1"])
          == ["attack", "special"],
          sess.stamina_manager.cost_increase_moves["1"])
    got99, _s, _e = surcharge_at(n, 99)
    check(f"{n} at level 99 STILL pays it — 99 is not mastered", got99 == pct,
          got99)
    got100, _s, _e = surcharge_at(n, 100)
    check(f"{n} at level 100 pays nothing — the blade is mastered",
          got100 == 0.0, got100)
    got120, _s, _e = surcharge_at(n, 120)
    check(f"{n} past 100 stays free", got120 == 0.0, got120)

# And the offence survives mastery — the whole point of removing only the cost.
#
# Ultimate Blade also raises the crit rate, and the engine rolls that crit with
# `random.random() < p` inside the same apply() call. Comparing exact damage
# across two calls is therefore a coin flip unless the crit is held still:
# these three checks are about the +20% ability bonus, and the crit is asserted
# separately from the data in section 4. Pinning `random.random` to 1.0 makes
# `< p` false for any p, so no roll can ever crit.
_real_random = random.random
random.random = lambda: 1.0
try:
    _g, s100, e100 = surcharge_at(BASE, 100)
    dealt, _taken, _logs = e100.apply("1", "2", s100.blades["1"],
                                      s100.blades["2"], "attack", "win", 100, 0)
    check("a mastered Ultimate Valkyrie still hits for +20%", dealt == 120,
          dealt)
    _g, s1, e1 = surcharge_at(BASE, 1)
    d1, _t, _l = e1.apply("1", "2", s1.blades["1"], s1.blades["2"],
                          "attack", "win", 100, 0)
    check("an unmastered one hits for the same +20% — only the cost differs",
          d1 == dealt, (d1, dealt))
    _g, sb, eb = surcharge_at(BLACK, 100)
    db_, _t, _l = eb.apply("1", "2", sb.blades["1"], sb.blades["2"],
                           "attack", "win", 100, 0)
    check("the Black Edition hits harder than the base blade on the same swing",
          db_ > dealt, (dealt, db_))
finally:
    random.random = _real_random

# ...and the crit really is live when it is not being held still. 15% over 400
# swings misses entirely about once in 10^28 runs.
_g, sc, ec = surcharge_at(BASE, 1)
crits = sum(1 for _ in range(400)
            if ec.apply("1", "2", sc.blades["1"], sc.blades["2"],
                        "attack", "win", 100, 0)[0] > 120)
check(f"Ultimate Blade's crit fires on its own ({crits}/400 swings)",
      crits > 0, crits)

# The surcharge has to survive contact with the manager the session built,
# not just the one this file constructed.
_g, sx, _e = surcharge_at(BASE, 1)
sx.stamina_manager.stamina["1"] = 50.0
sx.stamina_manager.deduct_cost("1", MOVE_ATTACK)
check("an unmastered attack really removes 2.97 stamina in a battle",
      round(50.0 - sx.stamina_manager.stamina["1"], 2) == 2.97,
      round(50.0 - sx.stamina_manager.stamina["1"], 2))

print("\n── 8. the hidden drop is hidden, and unreachable by normal means ─")
import cogs.economy.shop as SHOP                                   # noqa: E402

pool = SHOP._load_booster_pool()
names = {b["name"] for b in pool}
check("the weighted booster pool is not empty", bool(pool), len(pool))
check("the Black Edition is NOT in the weighted pool — it can never be rolled "
      "normally", BLACK not in names)
check("the other booster blades are all still there",
      {"Victory Valkyrie X", "Storm Spriggan X"} <= names,
      sorted(names))
check("the base Ultimate Valkyrie is not booster-only, so it can spawn",
      not DB[BASE].get("booster_exclusive"))

hidden = SHOP._hidden_drop_pool()
check("the hidden pool holds exactly the Black Edition",
      [b["name"] for b in hidden] == [BLACK], [b["name"] for b in hidden])
check("its chance is 1 in 5,000,000",
      hidden[0][SHOP.HIDDEN_DROP_KEY] == 5_000_000,
      hidden[0].get(SHOP.HIDDEN_DROP_KEY))

# Statistically it must essentially never fire. 200,000 packs is ~40x more
# than the entire playerbase is ever likely to open, and should still miss.
random.seed(20260813)
hits = sum(1 for _ in range(200_000) if SHOP._roll_hidden(hidden) is not None)
check("200,000 simulated packs produced no hidden drop", hits == 0, hits)

# ...but it must be reachable, or it is decoration.
_real = random.randrange
random.randrange = lambda _n: 0
try:
    forced = SHOP._roll_hidden(hidden)
finally:
    random.randrange = _real
check("a winning roll DOES return the blade",
      forced is not None and forced["name"] == BLACK, forced)

check("a blade with no hidden key is ignored by the hidden roll",
      SHOP._roll_hidden([DB["Victory Valkyrie X"]]) is None)
check("a malformed hidden key does not crash the pack open",
      SHOP._roll_hidden([{"name": "x", SHOP.HIDDEN_DROP_KEY: "lots"}]) is None)

src = open(os.path.join(ROOT, "cogs", "economy", "shop.py"),
           encoding="utf-8").read()
check("the pack open swaps the winning slot so the reveal still matches",
      "revealed[0] = rare" in src)
for token in ("5,000,000", "5000000", "1-in-5", "hidden_drop_one_in"):
    # Only the module's own machinery may mention it. Nothing may be sent.
    leaked = [ln.strip() for ln in src.splitlines()
              if token in ln and ("await ctx.send" in ln
                                  or "description=" in ln
                                  or "set_footer" in ln)]
    check(f"no user-facing string mentions {token!r}", not leaked, leaked)

print("\n── 9. spawns and Story cannot hand one out ──────────────────────")
import cogs.spawn.spawn as SPAWN                                   # noqa: E402
ssrc = open(os.path.join(ROOT, "cogs", "spawn", "spawn.py"),
            encoding="utf-8").read()
check("the spawn table filters booster_exclusive out",
      "booster_exclusive" in ssrc)
try:
    spawnable = SPAWN._spawn_pool() if hasattr(SPAWN, "_spawn_pool") else None
except Exception:                                                  # noqa: BLE001
    spawnable = None
if spawnable is not None:
    check("the Black Edition is not spawnable",
          BLACK not in {b.get("name") for b in spawnable})

print("\n── 10. Ultimate Valkyrie is a HIDDEN spawn, 1 in 10,000,000 ─────")
UV = BASE
uv = DB[UV]
check("it carries a hidden spawn chance", uv.get("hidden_drop_one_in"))
check("...of exactly 10,000,000", uv["hidden_drop_one_in"] == 10_000_000,
      uv.get("hidden_drop_one_in"))
check("...and it is NOT booster-exclusive, so the two systems stay separate",
      not uv.get("booster_exclusive"))

# The exclusion is the half that is easy to forget. Without it the key is
# inert: Ultimate Valkyrie keeps its ordinary 1-in-400 through the Ultimate
# tier and the hidden roll is decoration on odds 25,000x better.
check("the weighted pool excludes hidden blades",
      SPAWN._hidden_spawn_n(uv) > 0
      and "_hidden_spawn_n(data)" in ssrc)

import random as _rnd                                              # noqa: E402
_seen = set()
for _ in range(200_000):
    got = SPAWN._pick_random_beyblade(DB)
    if got:
        _seen.add(got.get("name"))
check(f"200,000 weighted spawns produce no Ultimate Valkyrie "
      f"({len(_seen)} distinct blades seen)", UV not in _seen)
check("...and the pool is still healthy — this is not an empty-pool pass",
      len(_seen) > 50, len(_seen))
check("the Black Edition stays out too", BLACK not in _seen)

# The other half: a forced roll DOES produce one.
#
# Scoped to a roster holding ONLY Ultimate Valkyrie. `_roll_hidden_spawn`
# returns the first candidate whose own test hits, so once a second hidden
# blade exists — v1.15 gave Dead Phoenix a 1-in-1000 — a forced roll over the
# whole roster returns whichever comes first in file order, which is a fact
# about JSON ordering and not about this blade. The claim being made here is
# "Ultimate Valkyrie can be produced by the hidden roll", and that is what this
# now tests. Its independence from any other hidden blade is asserted below.
_real_range = _rnd.randrange
_solo = {UV: DB[UV]}
try:
    _rnd.randrange = lambda n: 0                    # every hidden test hits
    hit = SPAWN._roll_hidden_spawn(_solo)
    check("a forced hidden roll produces Ultimate Valkyrie",
          hit is not None and hit.get("name") == UV,
          hit and hit.get("name"))
    # Over the full roster a forced roll must still produce SOME hidden blade,
    # never nothing — that would mean the mechanism had stopped working.
    hit_all = SPAWN._roll_hidden_spawn(DB)
    check("...and over the whole roster it still produces a hidden blade",
          hit_all is not None and bool(hit_all.get("hidden_drop_one_in")),
          hit_all and hit_all.get("name"))
    _rnd.randrange = lambda n: 1                    # every hidden test misses
    check("a missed hidden roll produces nothing at all",
          SPAWN._roll_hidden_spawn(DB) is None)
finally:
    _rnd.randrange = _real_range

# 10,000,000 spawns is not simulable, so the ODDS are asserted structurally:
# each candidate gets its own independent randrange(N) against its own N.
_calls = []
try:
    _rnd.randrange = lambda n: _calls.append(n) or 1
    SPAWN._roll_hidden_spawn(DB)
finally:
    _rnd.randrange = _real_range
check("the roll uses each blade's own N, once each",
      10_000_000 in _calls and len(_calls) == len(set(_calls)), _calls)
# Each candidate is tested independently, so a second hidden blade cannot
# dilute this one's odds — the reason a 1-in-1000 and a 1-in-10,000,000 blade
# can share the mechanism.
#
# The expected set is computed with `_roll_hidden_spawn`'s OWN filters rather
# than restated, which is how this check surfaced something worth knowing:
# that function gates on `_NEVER_SPAWN` and `obtainable()` and does NOT check
# `booster_exclusive`, while `_pick_random_beyblade` right beside it does. So
# the Black Edition — a booster-pack-only blade — is eligible for the wild
# hidden roll at its 1-in-5,000,000. Pre-existing, unrelated to any of the
# blades here, and left alone deliberately: it is asserted as the behaviour
# that exists so that changing it is a decision somebody makes on purpose.
from utils.availability import obtainable as _obtainable           # noqa: E402

_expected = sorted(int(b["hidden_drop_one_in"]) for b in DB.values()
                   if b.get("hidden_drop_one_in")
                   and b.get("rarity") not in SPAWN._NEVER_SPAWN
                   and _obtainable(b))
check("...including every other hidden blade, at its own N",
      sorted(_calls) == _expected, (sorted(_calls), _expected))
check("...and the hidden roll runs BEFORE the weighted pick in the spawner",
      ssrc.index("_roll_hidden_spawn(beyblades)")
      < ssrc.index("chosen = _pick_random_beyblade(beyblades)"))

for token in ("10000000", "10,000,000"):
    leaked = [ln.strip() for ln in ssrc.splitlines()
              if token in ln and ("await" in ln or "add_field" in ln
                                  or "description=" in ln or "title=" in ln
                                  or "set_footer" in ln)]
    check(f"no user-facing spawn string mentions {token!r}", not leaked, leaked)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
