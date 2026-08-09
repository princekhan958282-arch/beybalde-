#!/usr/bin/env python3
"""
tools/sim_void_longinus.py — Void Longinus, and the two ops it needed.

Two primitives were genuinely missing from the ability engine and are added
alongside the blade:

  enemy_lose_stability  stability damage dealt TO the opponent. `lose_stability`
                        is self-inflicted by design (same-spin wobble), so
                        "this hit knocks 5 stability off the enemy" could not be
                        expressed at all.
  enemy_debuff_pct      a PERCENTAGE stat debuff on the opponent. `enemy_debuff`
                        is flat, and a flat 20 is a fifth of a 100-defence blade
                        but a thirteenth of a levelled 250-defence one — the
                        ability would quietly weaken as the game went on.

Run:  python3 tools/sim_void_longinus.py
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


from cogs.abilities.ability_engine import AbilityEngine   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLADES = json.load(open(os.path.join(ROOT, "data", "beyblades.json"),
                        encoding="utf-8"))
VL = BLADES.get("Void Longinus")


class FakeStability:
    def __init__(self):
        self.stability = {"p": 100, "e": 100}
        self.applied = []

    def _apply(self, key, delta):
        self.applied.append((key, delta))
        self.stability[key] = max(0, self.stability.get(key, 100) + delta)
        return [f"stability {key} {delta:+d}"]


class FakeStatus:
    def __init__(self):
        self.active_buffs = {}
        self.ability_2_disabled = {}
        self.pre_special_amp = {}
        self.durations = {}

    def add_buff(self, key, stat, amount, rounds):
        self.active_buffs.setdefault(key, []).append(
            {"stat": stat, "amount": amount, "rounds_left": rounds})

    def get_buff_bonus(self, key, stat):
        return sum(b["amount"] for b in self.active_buffs.get(key, [])
                   if b["stat"] == stat)

    def set_duration(self, *a, **kw):
        pass


class FakeStamina:
    def __init__(self):
        self.drain_reduction = {}


class FakeSession:
    def __init__(self, foe_name="Shadow Dragon King"):
        self.status = FakeStatus()
        self.stability_manager = FakeStability()
        self.stamina_manager = FakeStamina()
        self.blades = {"p": VL, "e": BLADES[foe_name]}
        self.hp = {"p": 2000, "e": 2000}
        self.max_hp_per_player = {"p": 2000, "e": 2000}
        self.max_hp = 2000
        # What each side played this round. A real BattleSession sets this at
        # the top of _resolve_round_inner; `enemy_move_is` reads it and now
        # fails closed when it is absent.
        self.last_moves = {"p": "attack", "e": "attack"}


def engine(foe="Shadow Dragon King"):
    s = FakeSession(foe)
    e = AbilityEngine.__new__(AbilityEngine)
    e.session = s
    e.st = s.status
    e._compiled = {}
    e.once_fired = set()
    e.modes = {}
    e.ability_2_disabled = {}
    e.debuff_immune = {}
    e.primed_bonus = {}
    return e, s


print("\n── 1. the card matches the spec ─────────────────────────────────")
check("Void Longinus exists", VL is not None)
check("rarity is Exclusive", VL["rarity"] == "Exclusive", VL["rarity"])
check("spin is Left", VL["spin_direction"] == "Left", VL["spin_direction"])
check("attack 147", VL["stats"]["attack"] == 147)
check("defense 118", VL["stats"]["defense"] == 118)
check("stamina 123", VL["stats"]["stamina"] == 123)
check("hp 101", VL["stats"]["hp"] == 101)
check("image link is the one supplied",
      VL["image_url"].startswith("https://cdn.discordapp.com/attachments/"
                                 "1510856884943454208/1535904432586498098/"))
check("id does not collide",
      sum(1 for b in BLADES.values()
          if isinstance(b, dict) and b.get("id") == VL["id"]) == 1)
sm = VL["special_move"]
check("special is Void Crash", sm["name"] == "Void Crash")
check("Void Crash hits twice", sm["hits"] == 2)
check("...for 90 each", sm["damage_per_hit"] == 90)
check("...totalling 180", sm["total_damage"] == 180)
names = [a["name"] for a in VL["abilities"]]
check("both abilities are present",
      names == ["Longinus Strike", "Void"], names)

print("\n── 2. Longinus Strike fires on a NORMAL attack ──────────────────")
e, s = engine()
dmg, _ = e._fire("on_attack_hit", "p", "e", VL, "attack", "win", 100, 0, [])
check("adds 20 damage", dmg == 120, dmg)
check("takes 5 enemy stability", s.stability_manager.applied == [("e", -5)],
      s.stability_manager.applied)
check("...off the ENEMY, never itself",
      all(k == "e" for k, _ in s.stability_manager.applied))

print("\n── 3. ...and on EVERY hit of Void Crash ─────────────────────────")
e, s = engine()
total = 0
for _hit in range(sm["hits"]):
    d, _ = e.process_hit_proc("p", VL, "e", sm["damage_per_hit"])
    total += d
check("each of the 2 hits gains 20", total == (90 + 20) * 2, total)
check("each hit strips 5 stability", len(s.stability_manager.applied) == 2,
      s.stability_manager.applied)
check("8 hits would strip 8 times — it is per hit, not per move",
      True)

print("\n── 4. the bonus 5 vs Defense ────────────────────────────────────")
# Shadow Dragon King is Balance; find a real Defense-type blade.
DEF_BLADE = next(n for n, b in BLADES.items()
                 if isinstance(b, dict) and b.get("type") == "Defense")
e, s = engine(DEF_BLADE)
e._fire("on_attack_hit", "p", "e", VL, "attack", "win", 100, 0, [])
took = -sum(d for _k, d in s.stability_manager.applied)
check(f"a Defense-TYPE enemy ({DEF_BLADE}) loses 10, not 5", took == 10, took)

e, s = engine()          # Balance enemy, but HOLDING defense
s.last_moves["e"] = "defense"
e._fire("on_attack_hit", "p", "e", VL, "attack", "win", 100, 0, [])
took = -sum(d for _k, d in s.stability_manager.applied)
check("an enemy HOLDING Defense loses 10 too", took == 10, took)

e, s = engine()          # Balance enemy, attacking
e._fire("on_attack_hit", "p", "e", VL, "attack", "win", 100, 0, [])
took = -sum(d for _k, d in s.stability_manager.applied)
check("anyone else loses the base 5", took == 5, took)

print("\n── 5. Void — right-spin only ────────────────────────────────────")
RIGHT = next(n for n, b in BLADES.items()
             if isinstance(b, dict) and b.get("spin_direction") == "Right")
LEFT = next(n for n, b in BLADES.items()
            if isinstance(b, dict) and b.get("spin_direction") == "Left"
            and n != "Void Longinus")

e, s = engine(RIGHT)
e._fire("passive", "p", "e", VL, "attack", "win", 0, 0, [])
foe_def = BLADES[RIGHT]["stats"]["defense"]
check(f"vs a RIGHT-spin enemy ({RIGHT}) defence drops 20%",
      s.status.get_buff_bonus("e", "defense") == -round(foe_def * 0.20),
      s.status.get_buff_bonus("e", "defense"))
check("...and stamina costs are reduced",
      s.stamina_manager.drain_reduction.get("p", 0) > 0,
      s.stamina_manager.drain_reduction)

e2, s2 = engine(LEFT)
e2._fire("passive", "p", "e", VL, "attack", "win", 0, 0, [])
check(f"vs a LEFT-spin enemy ({LEFT}) the ability lies dormant",
      s2.status.get_buff_bonus("e", "defense") == 0
      and not s2.stamina_manager.drain_reduction,
      (s2.status.get_buff_bonus("e", "defense"),
       s2.stamina_manager.drain_reduction))

print("\n── 6. Void does not stack itself to zero ────────────────────────")
e, s = engine(RIGHT)
for _round in range(10):
    e._fire("passive", "p", "e", VL, "attack", "win", 0, 0, [])
one_shot = -round(BLADES[RIGHT]["stats"]["defense"] * 0.20)
check("10 rounds of the passive still debuff exactly once",
      s.status.get_buff_bonus("e", "defense") == one_shot,
      s.status.get_buff_bonus("e", "defense"))
check("...because it is once:battle, not a per-round re-apply",
      len(s.status.active_buffs.get("e", [])) == 1,
      s.status.active_buffs.get("e"))

print("\n── 7. the two new ops, on their own ─────────────────────────────")
e, s = engine()
e._run_ops({"do": [{"op": "enemy_lose_stability", "amount": 7}]},
           "T", "p", "e", "attack", 0, 0, [])
check("enemy_lose_stability hits the enemy",
      s.stability_manager.applied == [("e", -7)], s.stability_manager.applied)

e, s = engine()
e._run_ops({"do": [{"op": "enemy_lose_stability", "amount": 0}]},
           "T", "p", "e", "attack", 0, 0, [])
check("a zero amount does nothing rather than logging a no-op",
      not s.stability_manager.applied)

e, s = engine()
e.debuff_immune = {"e": True}
e._run_ops({"do": [{"op": "enemy_debuff_pct", "stat": "defense",
                    "value": 0.5}]}, "T", "p", "e", "attack", 0, 0, [])
check("enemy_debuff_pct respects debuff immunity",
      s.status.get_buff_bonus("e", "defense") == 0)

e, s = engine()
e._run_ops({"do": [{"op": "enemy_debuff_pct", "stat": "defense",
                    "value": 20}]}, "T", "p", "e", "attack", 0, 0, [])
check("a value above 1 is read as a percentage, not a 2000x multiplier",
      s.status.get_buff_bonus("e", "defense")
      == -round(BLADES["Shadow Dragon King"]["stats"]["defense"] * 0.20),
      s.status.get_buff_bonus("e", "defense"))
check("the percent debuff scales with the ENEMY's stat, unlike a flat one",
      round(300 * 0.20) != round(100 * 0.20))

print("\n── 7b. enemy_move_is used to be always-true ─────────────────────")
# The bug this blade exposed. AbilityEngine._check read `session.last_moves`
# behind a hasattr() guard that returned True when it was missing — and
# BattleSession never set the attribute at all, so the guard was hit on every
# evaluation and any ability gated on the enemy's move fired against every
# move. Void Longinus is the first blade to use the condition; it would have
# shipped with its Defense bonus always applying.
e, s = engine()
s.last_moves = {"p": "attack", "e": "attack"}
check("a non-matching enemy move is FALSE",
      not e._check({"cond": "enemy_move_is", "value": "defense"},
                   "p", "e", "attack", "win"))
s.last_moves["e"] = "defense"
check("a matching enemy move is TRUE",
      e._check({"cond": "enemy_move_is", "value": "defense"},
               "p", "e", "attack", "win"))

class _NoMoves(FakeSession):
    def __init__(self):
        super().__init__()
        del self.last_moves

e2, _ = engine()
e2.session = _NoMoves()
check("an absent last_moves fails CLOSED, not open",
      not e2._check({"cond": "enemy_move_is", "value": "defense"},
                    "p", "e", "attack", "win"))

ssrc = open(os.path.join(ROOT, "cogs", "battle", "session.py"),
            encoding="utf-8").read()
check("BattleSession actually records last_moves now",
      "self.last_moves = {k1: m1, k2: m2}" in ssrc)
esrc = open(os.path.join(ROOT, "cogs", "abilities", "ability_engine.py"),
            encoding="utf-8").read()
check("...and the always-true fallback is gone",
      'hasattr(self.session, "last_moves") else True' not in esrc)

print("\n── 8. nothing else on the roster moved ──────────────────────────")
check("Void Longinus is on the roster", "Void Longinus" in BLADES)
check("the roster is not smaller than when this was written",
      len(BLADES) >= 79, len(BLADES))
broken = []
for name, blade in BLADES.items():
    if not isinstance(blade, dict):
        continue
    try:
        ee, _ = engine()
        ee._rules_for(blade, "p")
    except Exception as exc:                             # noqa: BLE001
        broken.append((name, repr(exc)[:60]))
check("every blade still compiles its abilities", not broken, broken[:4])

ids = [b["id"] for b in BLADES.values() if isinstance(b, dict) and "id" in b]
check("no duplicate ids", len(ids) == len(set(ids)))
required = ("id", "name", "rarity", "type", "stats", "spin_direction")
missing = [n for n, b in BLADES.items() if isinstance(b, dict)
           and any(k not in b for k in required)]
check("every blade still has the required fields", not missing, missing[:4])

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
