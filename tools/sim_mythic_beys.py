#!/usr/bin/env python3
"""
tools/sim_mythic_beys.py — Shining Shuriken and Blood Dragon.

The two failure modes worth the most attention here are both invisible in the
JSON:

1. **Counter Point firing per Special hit.** `on_take_damage` is fired by
   `_fire_defensive` on every call to `apply()`, and a multi-hit Special calls
   `apply()` once per hit. An ungated counter returns 50 damage sixteen times
   against Rush Launch. The gate is asserted by running a real sixteen-hit
   Special into Shining Shuriken and counting.

2. **Unstable Attack's drawback going missing.** The +10 Attack half and the
   +0.2 stamina half are two separate ops on one rule. If they fall out of
   step — or if the flat surcharge never reaches `deduct_cost` — the blade is
   pure upside and the JSON still reads correctly. Both are driven to full
   stacks and the resulting Attack cost is measured.

Run:  python3 tools/sim_mythic_beys.py
"""
import json
import os
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

SS = "Shining Shuriken"
BD = "Blood Dragon"
WANT = {
    SS: {"attack": 148, "defense": 128, "stamina": 77, "hp": 69},
    BD: {"attack": 158, "defense": 29, "stamina": 81, "hp": 111},
}

print("\n── 1. both blades exist, as specified ───────────────────────────")
for n in (SS, BD):
    check(f"{n} is in the roster", n in DB)
check("the roster is at least 91 blades", len(DB) >= 91, len(DB))
ids = [b["id"] for b in DB.values()]
check("every id is still unique", len(set(ids)) == len(ids),
      len(ids) - len(set(ids)))

for n, want in WANT.items():
    b = DB[n]
    bad = {k: (b["stats"].get(k), v) for k, v in want.items()
           if b["stats"].get(k) != v}
    check(f"{n}: the specified statline", not bad, bad)
    check(f"{n} is Mythic", b["rarity"] == "Mythic", b["rarity"])
    check(f"{n} is an Attack type", b["type"] == "Attack", b["type"])
    check(f"{n} spins right", b["spin_direction"] == "Right",
          b.get("spin_direction"))
    check(f"{n}: image is a renderable CDN link",
          str(b.get("image_url", "")).startswith(
              "https://cdn.discordapp.com/attachments/"),
          str(b.get("image_url"))[:50])
    check(f"{n} has the plural `abilities` the engine actually reads",
          bool(b.get("abilities")))
    check(f"{n}: the plural mirrors the singular's name",
          b["abilities"][0]["name"] == b["ability"]["name"])
    check(f"{n} can spawn — it is not booster-only",
          not b.get("booster_exclusive"))

check("Shining Shuriken's ability is Counter Point",
      DB[SS]["ability"]["name"] == "Counter Point")
check("...and its Special is Shuriken Strike",
      DB[SS]["special_move"]["name"] == "Shuriken Strike")
check("Blood Dragon's ability is Unstable Attack",
      DB[BD]["ability"]["name"] == "Unstable Attack")
check("...and its Special is Blood Claw",
      DB[BD]["special_move"]["name"] == "Blood Claw")
check("Blood Claw's printed damage is the specified 170",
      DB[BD]["special_move"]["total_damage"] == 170,
      DB[BD]["special_move"]["total_damage"])
for n in (SS, BD):
    sm = DB[n]["special_move"]
    check(f"{n}: hits x damage really is the stated total",
          sm["hits"] * sm["damage_per_hit"] == sm["total_damage"],
          (sm["hits"], sm["damage_per_hit"], sm["total_damage"]))

print("\n── 2. Counter Point is gated to incoming ATTACKS ────────────────")
rules = DB[SS]["abilities"][0]["rules"]
take = [r for r in rules if r["when"] == "on_take_damage"]
check("there is exactly one on_take_damage rule", len(take) == 1, len(take))
check("it is gated on incoming_move_is: attack",
      any(c.get("cond") == "incoming_move_is" and c.get("value") == "attack"
          for c in take[0].get("if") or []), take[0].get("if"))
ops = {o["op"] for o in take[0]["do"]}
check("the counter costs the attacker HP, stamina and stability",
      ops == {"true_damage", "drain_stamina", "enemy_lose_stability"}, ops)
drain = [o for o in take[0]["do"] if o["op"] == "drain_stamina"][0]
check("the stamina is destroyed, not stolen — steal is False",
      drain.get("steal") is False, drain)
check("the counter is 50 HP",
      [o["value"] for o in take[0]["do"] if o["op"] == "true_damage"] == [50])
check("...1 stamina",
      [o["value"] for o in take[0]["do"] if o["op"] == "drain_stamina"] == [1])
check("...and 3 stability",
      [o["amount"] for o in take[0]["do"]
       if o["op"] == "enemy_lose_stability"] == [3])

spec = [r for r in rules if r["when"] == "on_special"]
check("Shuriken Strike also fires the counter", len(spec) == 1
      and {"true_damage", "drain_stamina", "enemy_lose_stability"}
      <= {o["op"] for o in spec[0]["do"]},
      [o["op"] for o in spec[0]["do"]] if spec else None)
deb = [o for o in spec[0]["do"] if o["op"] == "enemy_debuff_pct"]
check("...and lowers the enemy's Attack by 40%",
      len(deb) == 1 and deb[0]["stat"] == "attack" and deb[0]["value"] == 40,
      deb)

print("\n── 3. it really fires once per Attack, not once per hit ─────────")
from cogs.battle.status_manager import StatusManager                # noqa: E402
from cogs.battle.stamina_manager import (StaminaManager,            # noqa: E402
                                         STAMINA_COST)
from cogs.abilities.ability_engine import AbilityEngine             # noqa: E402
from cogs.core.constants import (MOVE_ATTACK, MOVE_SPECIAL,         # noqa: E402
                                 MOVE_DEFENSE)


class Stub:
    """Two blades and the managers the engine touches. '1' is the mover."""

    def __init__(self, b1, b2, levels=(1, 1)):
        self.blades = {"1": dict(b1), "2": dict(b2)}
        self.hp = {"1": 700, "2": 700}
        self.max_hp = 700
        self.max_hp_per_player = {"1": 700, "2": 700}
        self.bey_levels = {"1": levels[0], "2": levels[1]}
        self.battle_stats = {k: dict(v.get("stats") or {})
                             for k, v in self.blades.items()}
        self.last_moves = {}
        self.round = 1
        self.status = StatusManager(self)
        self.stamina_manager = StaminaManager(
            self.blades, effective_stats=self.battle_stats)
        self.stability_manager = types.SimpleNamespace(
            _apply=lambda key, delta: self.stability_log.append((key, delta)))
        self.stability_log = []
        self.chain_handler = types.SimpleNamespace(resolve=lambda *a, **k: [])


def engine_for(b1, b2, levels=(1, 1)):
    s = Stub(b1, b2, levels)
    e = AbilityEngine(s)
    s.ability = e
    for k in ("1", "2"):
        e.setup(k, s.blades[k])
    return s, e


# Attacker is "1" (a plain blade), defender "2" is Shining Shuriken.
plain = {"name": "Plain", "spin_direction": "Right",
         "stats": {"attack": 100, "defense": 100, "stamina": 100, "hp": 100}}
s, e = engine_for(plain, DB[SS])
s.stamina_manager.stamina["1"] = 30.0
hp_before = s.hp["1"]
e.apply("1", "2", s.blades["1"], s.blades["2"], MOVE_ATTACK, "win", 60, 0)
check("one Attack costs the attacker 50 HP", hp_before - s.hp["1"] == 50,
      hp_before - s.hp["1"])
check("...and 1 stamina", round(30.0 - s.stamina_manager.stamina["1"], 2) == 1.0,
      s.stamina_manager.stamina["1"])
check("...which Shuriken does NOT gain (steal: False)",
      s.stamina_manager.stamina["2"]
      == StaminaManager(s.blades).stamina["2"],
      s.stamina_manager.stamina["2"])
check("...and 3 stability off the attacker",
      s.stability_log == [("1", -3)], s.stability_log)

# The whole point of the gate: sixteen Special hits must not be sixteen
# counters. This is the assertion that would have caught an ungated rule.
s2, e2 = engine_for(plain, DB[SS])
hp_before = s2.hp["1"]
for i in range(16):
    e2.apply("1", "2", s2.blades["1"], s2.blades["2"], MOVE_SPECIAL, "win",
             10, 0, is_first_hit=(i == 0), is_last_hit=(i == 15))
check("a 16-hit Special takes NO counter damage — it is not an Attack",
      hp_before - s2.hp["1"] == 0, hp_before - s2.hp["1"])
check("...and no stability either", s2.stability_log == [], s2.stability_log)

s3, e3 = engine_for(plain, DB[SS])
hp_before = s3.hp["1"]
for _ in range(3):
    e3.apply("1", "2", s3.blades["1"], s3.blades["2"], MOVE_ATTACK, "win",
             60, 0)
check("three separate Attacks cost 150 HP — once each, not once ever",
      hp_before - s3.hp["1"] == 150, hp_before - s3.hp["1"])

s4, e4 = engine_for(plain, DB[SS])
hp_before = s4.hp["1"]
e4.apply("1", "2", s4.blades["1"], s4.blades["2"], MOVE_ATTACK, "win", 0, 0)
check("a blocked Attack that deals 0 damage triggers no counter",
      hp_before - s4.hp["1"] == 0, hp_before - s4.hp["1"])

# Shuriken Strike, fired BY Shuriken: the counter plus the 40% debuff.
s5, e5 = engine_for(DB[SS], plain)
hp_before = s5.hp["2"]
_d, _t, logs = e5.apply("1", "2", s5.blades["1"], s5.blades["2"],
                        MOVE_SPECIAL, "win", 45, 0, is_first_hit=True,
                        is_last_hit=False)
check("Shuriken Strike fires the counter on its own Special",
      hp_before - s5.hp["2"] >= 50, hp_before - s5.hp["2"])
atk_buff = s5.status.get_buff("2", "attack") if hasattr(
    s5.status, "get_buff") else None
check("...and drops 40% of the enemy's Attack (100 -> -40)",
      any("attack" in ln.lower() and "-40" in ln for ln in logs),
      [ln for ln in logs if "attack" in ln.lower()])

print("\n── 4. Unstable Attack: both halves, in lockstep ─────────────────")
rules = DB[BD]["abilities"][0]["rules"]
for when in ("on_attack_hit", "on_hit"):
    r = [x for x in rules if x["when"] == when]
    check(f"Blood Dragon stacks on {when}", len(r) == 1, len(r))
    ops = {o["op"] for o in r[0]["do"]}
    check(f"{when}: it banks Attack AND raises its own cost",
          ops == {"stacking_buff", "stamina_cost_increase"}, ops)
    buf = [o for o in r[0]["do"] if o["op"] == "stacking_buff"][0]
    cost = [o for o in r[0]["do"] if o["op"] == "stamina_cost_increase"][0]
    check(f"{when}: +10 Attack a stack, 10 stacks",
          buf["per_stack"] == 10 and buf["max"] == 10, buf)
    check(f"{when}: +0.2 stamina a stack, same 10 cap",
          cost["flat_per_stack"] == 0.2 and cost["max"] == 10, cost)
    check(f"{when}: the surcharge hits Attack and Special only",
          sorted(cost["moves"]) == ["attack", "special"], cost["moves"])

s, e = engine_for(DB[BD], plain)
sm = s.stamina_manager


def cost_of(mgr, key, move):
    before = mgr.stamina[key]
    mgr.deduct_cost(key, move)
    spent = round(before - mgr.stamina[key], 2)
    mgr.stamina[key] = before
    return spent


sm.stamina["1"] = 100.0
check("before any hit, an Attack costs the printed 2.2",
      cost_of(sm, "1", MOVE_ATTACK) == STAMINA_COST[MOVE_ATTACK],
      cost_of(sm, "1", MOVE_ATTACK))

for n in range(1, 13):          # deliberately past the 10-stack cap
    e.apply("1", "2", s.blades["1"], s.blades["2"], MOVE_ATTACK, "win", 50, 0)
    if n == 1:
        check("one hit adds +0.2 to the Attack cost",
              cost_of(sm, "1", MOVE_ATTACK) == 2.4,
              cost_of(sm, "1", MOVE_ATTACK))
    if n == 5:
        check("five hits: +1.0 (2.2 -> 3.2)",
              cost_of(sm, "1", MOVE_ATTACK) == 3.2,
              cost_of(sm, "1", MOVE_ATTACK))

check("at 10 stacks an Attack costs 4.2 instead of 2.2",
      cost_of(sm, "1", MOVE_ATTACK) == 4.2, cost_of(sm, "1", MOVE_ATTACK))
check("...and a Special 6.4 instead of 4.4",
      cost_of(sm, "1", MOVE_SPECIAL) == 6.4, cost_of(sm, "1", MOVE_SPECIAL))
check("hits 11 and 12 add nothing — the cap really caps",
      sm.cost_increase_flat["1"] == 2.0, sm.cost_increase_flat["1"])
check("Defense is not surcharged — the drawback is offensive",
      cost_of(sm, "1", MOVE_DEFENSE) == STAMINA_COST[MOVE_DEFENSE],
      cost_of(sm, "1", MOVE_DEFENSE))
check("the opponent pays nothing for Blood Dragon's stacks",
      cost_of(sm, "2", MOVE_ATTACK) == STAMINA_COST[MOVE_ATTACK],
      cost_of(sm, "2", MOVE_ATTACK))
check("the Attack buff capped at +100 in the same 10 stacks",
      e.counters.get(("1", "unstable")) == 10,
      e.counters.get(("1", "unstable")))
check("the two halves stayed in lockstep",
      e.counters.get(("1", "unstable"))
      == e.counters.get(("1", "unstable_cost")),
      (e.counters.get(("1", "unstable")),
       e.counters.get(("1", "unstable_cost"))))

# The Special path banks stacks too — same ability, other trigger. `on_hit`
# is fired by process_hit_proc, NOT by apply(): the engine keeps the two
# strictly apart so an attack-side effect cannot go off once per Special hit.
# Going through apply() here would have "passed" by never firing at all.
s, e = engine_for(DB[BD], plain)
for _ in range(4):
    e.process_hit_proc("1", s.blades["1"], "2", 40)
check("each Special HIT banks a stack as well",
      e.counters.get(("1", "unstable")) == 4,
      e.counters.get(("1", "unstable")))
check("...raising the cost by the same 0.8",
      s.stamina_manager.cost_increase_flat["1"] == 0.8,
      s.stamina_manager.cost_increase_flat["1"])

print("\n── 5. Blood Claw adds its full Attack, and pays for it ──────────")
spec = [r for r in DB[BD]["abilities"][0]["rules"] if r["when"] == "on_special"]
check("there is one Blood Claw rule", len(spec) == 1, len(spec))
bd_ops = {o["op"] for o in spec[0]["do"]}
check("it adds stat-scaled damage and drains stamina",
      bd_ops == {"bonus_damage_stat", "gain_stamina"}, bd_ops)
stat_op = [o for o in spec[0]["do"] if o["op"] == "bonus_damage_stat"][0]
check("the rider is the FULL Attack stat (scale 1.0)",
      stat_op["stat"] == "attack" and stat_op["scale"] == 1.0, stat_op)
check("the extra drain is 3", [o["value"] for o in spec[0]["do"]
                               if o["op"] == "gain_stamina"] == [-3])

s, e = engine_for(DB[BD], plain)
# Inside its own bar, not above it — gain_stamina clamps to the ceiling, so a
# start value over the cap would silently absorb the drain.
s.stamina_manager.stamina["1"] = 10.0
dealt, _t, _l = e.apply("1", "2", s.blades["1"], s.blades["2"],
                        MOVE_SPECIAL, "win", 85, 0, is_first_hit=True,
                        is_last_hit=False)
check("the first Blood Claw hit carries +158 from its Attack stat",
      dealt == 85 + 158, dealt)
check("...and the claw costs 3 extra stamina",
      round(10.0 - s.stamina_manager.stamina["1"], 2) == 3.0,
      s.stamina_manager.stamina["1"])

# The rider lands ONCE, on the first hit — on_special is gated on
# is_first_hit. A per-hit rider would double the Special outright.
s, e = engine_for(DB[BD], plain)
total = 0
for i in range(2):
    d, _t, _l = e.apply("1", "2", s.blades["1"], s.blades["2"],
                        MOVE_SPECIAL, "win", 85, 0, is_first_hit=(i == 0),
                        is_last_hit=(i == 1))
    total += d
check("across both Blood Claw hits the Attack rider is added once",
      total == 85 + 158 + 85, total)

# It must scale with the bey, not with the number printed in the JSON — that
# is the whole reason bonus_damage_stat reads battle_stats.
s, e = engine_for(DB[BD], plain)
s.battle_stats["1"]["attack"] = 400
dealt, _t, _l = e.apply("1", "2", s.blades["1"], s.blades["2"],
                        MOVE_SPECIAL, "win", 85, 0, is_first_hit=True,
                        is_last_hit=False)
check("a levelled Blood Dragon rides its LEVELLED Attack, not the printed one",
      dealt == 85 + 400, dealt)

# Temporary buffs must NOT feed the rider, or Unstable Attack pays out twice
# on one move — once through the generic active-buff path that raises all
# damage, and again through the rider. The rider is measured by its own log
# line, because the buff lands on the same total and would mask it.
s, e = engine_for(DB[BD], plain)
for _ in range(10):
    e.apply("1", "2", s.blades["1"], s.blades["2"], MOVE_ATTACK, "win", 50, 0)
check("ten stacks really are +100 ATK of timed buff",
      s.status.get_buff_bonus("1", "attack") == 100,
      s.status.get_buff_bonus("1", "attack"))
dealt, _t, logs = e.apply("1", "2", s.blades["1"], s.blades["2"],
                          MOVE_SPECIAL, "win", 85, 0, is_first_hit=True,
                          is_last_hit=False)
rider = [ln for ln in logs if "full Attack" in ln]
check("the rider is still the unbuffed 158 — no double-dip",
      len(rider) == 1 and "+158 damage" in rider[0], rider)
check("...and battle_stats was never mutated by the buff",
      s.battle_stats["1"]["attack"] == 158, s.battle_stats["1"]["attack"])
check("the timed buff still reaches the Special through its own path",
      dealt == 85 + 100 + 158, dealt)

print("\n── 6. the new ops are safe on their own ─────────────────────────")
# A session built by an older path — or a harness — has no battle_stats. The
# rider must fall back to the blade's printed stat rather than vanishing.
s, e = engine_for(DB[BD], plain)
del s.battle_stats
d, _t, _l = e.apply("1", "2", s.blades["1"], s.blades["2"], MOVE_SPECIAL,
                    "win", 85, 0, is_first_hit=True, is_last_hit=False)
check("with no battle_stats the rider falls back to the printed 158",
      d == 85 + 158, d)

s, e = engine_for(plain, plain)
check("an unknown condition still fails closed",
      not e._check({"cond": "incoming_move_was"}, "1", "2", "attack", "win"))
check("incoming_move_is reads the move it is handed",
      e._check({"cond": "incoming_move_is", "value": "attack"},
               "1", "2", "attack", "win")
      and not e._check({"cond": "incoming_move_is", "value": "attack"},
                       "1", "2", "special", "win"))

# drain_stamina's default is unchanged — everything already using it steals.
s, e = engine_for(plain, plain)
s.stamina_manager.stamina = {"1": 5.0, "2": 5.0}
e.session.blades["1"]["abilities"] = [{"name": "T", "rules": [
    {"when": "on_attack_hit", "do": [{"op": "drain_stamina", "value": 2}]}]}]
e._compiled.clear()
e.apply("1", "2", s.blades["1"], s.blades["2"], MOVE_ATTACK, "win", 10, 0)
check("drain_stamina still steals by default — nothing else changed",
      s.stamina_manager.stamina["1"] > 5.0
      and s.stamina_manager.stamina["2"] < 5.0,
      (s.stamina_manager.stamina["1"], s.stamina_manager.stamina["2"]))

print("\n── 7. the cards render ──────────────────────────────────────────")
from utils import info_card_pillow as ICP                           # noqa: E402
for n in (SS, BD):
    try:
        buf = ICP.render_info_card_pillow(DB[n])
        ok = buf is not None and len(buf.getvalue()) > 1000
    except Exception as exc:                                        # noqa: BLE001
        ok, buf = False, exc
    check(f"{n}'s ;info card renders", ok, buf)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
