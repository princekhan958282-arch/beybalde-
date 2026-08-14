#!/usr/bin/env python3
"""
tools/sim_types.py — the type advantage chart, tested for the first time.

Before this file existed you could invert the entire type chart and all 23
other suites still passed. That is not a hypothetical: the chart WAS partly
inverted. `battle/stability_manager.py` kept a second table in which an Attack
bey's stability effects were switched OFF against Stamina — the exact matchup
Attack is supposed to dominate — Defence was active against everything
including the type that beats it, and every mirror was active while
`resolve_active_bonuses` returns (False, False) for mirrors. Two charts, one
game, no test.

A third chart in `ui/help_cog.py` was hand-written, disagreed with both, and
had no Balance row at all.

So the assertions here are mostly of one shape: **every consumer must agree
with `type_system.ADVANTAGE`, on all sixteen type pairs.**

Run:  python3 tools/sim_types.py
"""
import os
import sys
import types as _t

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

from cogs.abilities import type_system as TS                     # noqa: E402
from cogs.battle import stability_manager as SBM                 # noqa: E402
from cogs.battle.stamina_manager import StaminaManager, STAMINA_COST  # noqa: E402
from cogs.core.constants import MOVE_ATTACK, MOVE_SPECIAL        # noqa: E402

TYPES = ("attack", "defense", "stamina", "balance")
PAIRS = [(a, b) for a in TYPES for b in TYPES]

print("\n── 1. the chart is the triangle that was asked for ──────────────")
check("Attack beats Stamina", TS.ADVANTAGE["attack"] == "stamina")
check("Stamina beats Defence", TS.ADVANTAGE["stamina"] == "defense")
check("Defence beats Attack", TS.ADVANTAGE["defense"] == "attack")
check("Balance is not in the triangle", "balance" not in TS.ADVANTAGE
      and "balance" not in TS.ADVANTAGE.values())
check("the triangle is closed — three entries, no more",
      len(TS.ADVANTAGE) == 3, TS.ADVANTAGE)
check("...and every entry points at a real type",
      set(TS.ADVANTAGE.values()) <= set(TYPES))
check("no type beats itself",
      all(k != v for k, v in TS.ADVANTAGE.items()))
check("it is a cycle, not a chain — every type both beats and is beaten",
      set(TS.ADVANTAGE) == set(TS.ADVANTAGE.values()))
check("the second, contradicting chart is gone",
      not hasattr(SBM, "_ACTIVE_MATCHUPS"))

print("\n── 2. one normaliser, and everything uses it ────────────────────")
for raw, want in (("Attack", "attack"), ("attack", "attack"),
                  ("ATTACK", "attack"), ("  Attack  ", "attack"),
                  ("Attack Type", "attack"), ("attack/balance", "attack"),
                  ("Defense", "defense"), ("Defence", "defense"),
                  ("defence", "defense"), ("Stamina", "stamina"),
                  ("Balance", "balance"), ("", ""), (None, ""),
                  ("   ", ""), ("nonsense", ""), (123, "")):
    check(f"normalise_type({raw!r}) -> {want!r}",
          TS.normalise_type(raw) == want, TS.normalise_type(raw))
check("a composite resolves to its PRIMARY type, not to Balance",
      TS.normalise_type("attack/balance") == "attack"
      and TS.normalise_type("balance/attack") == "attack",
      (TS.normalise_type("attack/balance"),
       TS.normalise_type("balance/attack")))

# THE BUG: TypeModifiers matched by substring and resolve_active_bonuses by
# exact lookup, so this blade got a multiplier that was never switched on.
odd = {"name": "Odd", "type": "Attack Type",
       "stats": {"attack": 150, "defense": 50, "stamina": 50}}
mod = TS.TypeModifiers(odd)
a_act, _ = TS.resolve_active_bonuses(odd["type"], "stamina")
check("a blade typed 'Attack Type' gets an attack multiplier...",
      mod.atk_mult > 1.0, mod.atk_mult)
check("...AND has it switched on against Stamina — the two halves agree now",
      a_act, a_act)
check("...and its stability effects agree too",
      SBM.stability_effects_active(odd["type"], "stamina"))

print("\n── 3. all sixteen pairs, and every consumer agrees ──────────────")
EXPECT = {}
for a, b in PAIRS:
    if a == "balance" or b == "balance":
        EXPECT[(a, b)] = (True, True)
    else:
        EXPECT[(a, b)] = (TS.ADVANTAGE.get(a) == b, TS.ADVANTAGE.get(b) == a)

for (a, b), want in EXPECT.items():
    got = TS.resolve_active_bonuses(a, b)
    check(f"{a:8} vs {b:8} -> {got}", got == want, (got, want))

check("the chart is symmetric — reading it backwards gives the mirror image",
      all(TS.resolve_active_bonuses(a, b)
          == tuple(reversed(TS.resolve_active_bonuses(b, a)))
          for a, b in PAIRS))
check("no non-Balance mirror activates either side",
      all(TS.resolve_active_bonuses(t, t) == (False, False)
          for t in ("attack", "defense", "stamina")))
check("both sides are active in a Balance mirror",
      TS.resolve_active_bonuses("balance", "balance") == (True, True))
check("exactly one side is active in each non-Balance, non-mirror pair",
      all(sum(TS.resolve_active_bonuses(a, b)) == 1
          for a, b in PAIRS
          if a != b and "balance" not in (a, b)))

# The assertion that would have caught the inverted table.
for a, b in PAIRS:
    want = TS.resolve_active_bonuses(a, b)[0]
    check(f"stability agrees for {a:8} vs {b:8}",
          SBM.stability_effects_active(a, b) == want,
          (SBM.stability_effects_active(a, b), want))

print("\n── 4. Balance is half, both ways ────────────────────────────────")
stats = {"attack": 100, "defense": 100, "stamina": 100}
pure = {t: TS.TypeModifiers({"type": t, "stats": stats}) for t in TYPES}
bal = pure["balance"]
check("Balance carries all three multipliers",
      bal.atk_mult > 1 and bal.def_mult > 1 and bal.sta_mult > 1,
      (bal.atk_mult, bal.def_mult, bal.sta_mult))
for stat, attr in (("attack", "atk_mult"), ("defense", "def_mult"),
                   ("stamina", "sta_mult")):
    full = getattr(pure[stat], attr) - 1.0
    half = getattr(bal, attr) - 1.0
    check(f"Balance's {stat} bonus is exactly half of a pure {stat} blade's "
          f"({full:.4f} -> {half:.4f})",
          abs(half - full * TS.BALANCE_SCALE_FACTOR) < 1e-6, (full, half))
check("a pure type gets ONLY its own multiplier",
      all(getattr(pure[t], other) == 1.0
          for t, own in (("attack", "atk_mult"), ("defense", "def_mult"),
                         ("stamina", "sta_mult"))
          for other in ("atk_mult", "def_mult", "sta_mult") if other != own))
check("only Defence starts with extra stability",
      pure["defense"].stability_start > pure["attack"].stability_start
      and pure["attack"].stability_start == pure["balance"].stability_start,
      {t: m.stability_start for t, m in pure.items()})
check("an unknown type is fully neutral",
      TS.TypeModifiers({"type": "???", "stats": stats}).atk_mult == 1.0)
check("suppress_bonus, which nothing called, is gone",
      not hasattr(TS.TypeModifiers, "suppress_bonus"))

print("\n── 5. the three signature effects ───────────────────────────────")
check("Attack strips stability", TS.ATTACK_STABILITY_STRIP > 0,
      TS.ATTACK_STABILITY_STRIP)
check("Stamina cuts its own costs", 0 < TS.STAMINA_COST_CUT < 1,
      TS.STAMINA_COST_CUT)
check("Defence reflects what it blocks", 0 < TS.DEFENSE_REFLECT <= 1,
      TS.DEFENSE_REFLECT)
check("Balance halves whatever is aimed at it",
      TS.BALANCE_EFFECT_SCALE == 0.5, TS.BALANCE_EFFECT_SCALE)
check("Attack's effect does NOT touch stamina — it is stability only",
      "drain" not in open(os.path.join(ROOT, "cogs", "battle",
                                       "attack_manager.py"),
                          encoding="utf-8").read()
      .split("_type_signature_attack")[1][:900].lower())

asrc = open(os.path.join(ROOT, "cogs", "battle", "attack_manager.py"),
            encoding="utf-8").read()
# Slice from the DEFINITION, not the first mention — the call site comes first
# in the file, so splitting on the bare name lands in the wrong place.
_atk_body = asrc.split("def _apply_atk_type_mod", 1)[1].split("\n    def ", 1)[0]
check("the Attack effect hangs off the existing advantage gate",
      "_type_signature_attack" in _atk_body)
check("the Defence effect reuses the mitigation already computed",
      "pre - dmg_dealt" in asrc)
check("both check the acting blade really IS that type",
      asrc.count('normalise_type(getattr(mod, "btype", ""))') == 2)
check("both halve for a Balance opponent",
      asrc.count("* BALANCE_EFFECT_SCALE") == 1
      and asrc.count("*= BALANCE_EFFECT_SCALE") == 1,
      (asrc.count("* BALANCE_EFFECT_SCALE"),
       asrc.count("*= BALANCE_EFFECT_SCALE")))

ssrc = open(os.path.join(ROOT, "cogs", "battle", "session.py"),
            encoding="utf-8").read()
check("Stamina's cut is resolved once at session build, not per round",
      "STAMINA_COST_CUT as _SCC" in ssrc)
check("...into the field deduct_cost already honours",
      "sm.drain_reduction[_k] = max(" in ssrc)

# drain_reduction is combined with max(), not summed — a blade carrying the
# stamina_cost_reduction ability op AND the type edge gets the larger, not
# both. Two 25% cuts stacking to 44% is a different game.
sm = StaminaManager({"1": {"name": "A", "stats": {"stamina": 100}},
                     "2": {"name": "B", "stats": {"stamina": 100}}})
sm.stamina = {"1": 100.0, "2": 100.0}
sm.drain_reduction["1"] = TS.STAMINA_COST_CUT


def cost(key, move):
    before = sm.stamina[key]
    sm.deduct_cost(key, move)
    out = round(before - sm.stamina[key], 2)
    sm.stamina[key] = before
    return out


check(f"a {int(TS.STAMINA_COST_CUT*100)}% cut really lands on the Attack cost",
      cost("1", MOVE_ATTACK)
      == round(STAMINA_COST[MOVE_ATTACK] * (1 - TS.STAMINA_COST_CUT), 2),
      cost("1", MOVE_ATTACK))
check("...and on the Special", cost("1", MOVE_SPECIAL)
      == round(STAMINA_COST[MOVE_SPECIAL] * (1 - TS.STAMINA_COST_CUT), 2),
      cost("1", MOVE_SPECIAL))
check("the opponent pays full price", cost("2", MOVE_ATTACK)
      == STAMINA_COST[MOVE_ATTACK])
sm.drain_reduction["1"] = max(TS.STAMINA_COST_CUT, 0.10)
check("max(), not sum: a weaker ability cut does not stack on top",
      cost("1", MOVE_ATTACK)
      == round(STAMINA_COST[MOVE_ATTACK] * (1 - TS.STAMINA_COST_CUT), 2),
      cost("1", MOVE_ATTACK))

print("\n── 6. the effects fire on advantage, and only on advantage ──────")
from cogs.battle.status_manager import StatusManager             # noqa: E402
from cogs.abilities.ability_engine import AbilityEngine          # noqa: E402
from cogs.battle.attack_manager import AttackManager             # noqa: E402


def blade(t, name=None):
    return {"name": name or t.title(), "type": t.title(),
            "spin_direction": "Right",
            "stats": {"attack": 100, "defense": 100, "stamina": 100, "hp": 100}}


class Sess:
    def __init__(self, t1, t2):
        self.blades = {"1": blade(t1), "2": blade(t2)}
        self.hp = {"1": 700, "2": 700}
        self.max_hp = 700
        self.max_hp_per_player = {"1": 700, "2": 700}
        self.battle_stats = {k: dict(v["stats"]) for k, v in self.blades.items()}
        self.bey_levels = {"1": 1, "2": 1}
        self.last_moves = {}
        self.round = 1
        self.status = StatusManager(self)
        self.stamina_manager = StaminaManager(self.blades)
        self.type_mods = {k: TS.TypeModifiers(v) for k, v in self.blades.items()}
        self.stability_manager = SBM.StabilityManager(self.blades, self.type_mods)
        self.chain_handler = _t.SimpleNamespace(resolve=lambda *a, **k: [])
        self.ability = AbilityEngine(self)


def strip_for(t1, t2):
    """Stability the DEFENDER loses to one landing Attack by a t1 blade."""
    s = Sess(t1, t2)
    am = AttackManager.__new__(AttackManager)
    am.session = s
    before = s.stability_manager.stability["2"]
    am._apply_atk_type_mod("1", s.blades["1"], MOVE_ATTACK, 100, [], okey="2")
    return before - s.stability_manager.stability["2"]


for t2 in TYPES:
    got = strip_for("attack", t2)
    if t2 == "stamina":
        want = TS.ATTACK_STABILITY_STRIP
    elif t2 == "balance":
        want = round(TS.ATTACK_STABILITY_STRIP * TS.BALANCE_EFFECT_SCALE)
    else:
        want = 0
    check(f"Attack vs {t2:8}: strips {got} stability (want {want})",
          got == want, (got, want))

for t1 in ("defense", "stamina", "balance"):
    check(f"a {t1} blade never strips stability — that is Attack's edge",
          all(strip_for(t1, t2) == 0 for t2 in TYPES),
          {t2: strip_for(t1, t2) for t2 in TYPES})


def reflect_for(t1, t2):
    """HP the ATTACKER (t1) loses to the defender's (t2) reflect."""
    s = Sess(t1, t2)
    am = AttackManager.__new__(AttackManager)
    am.session = s
    before = s.hp["1"]
    am._apply_def_type_mod("2", s.blades["2"], 200, [], mkey="1")
    return before - s.hp["1"]


check("Defence vs Attack sends damage back", reflect_for("attack", "defense") > 0,
      reflect_for("attack", "defense"))
check("Defence vs Stamina sends nothing back — Stamina beats it",
      reflect_for("stamina", "defense") == 0,
      reflect_for("stamina", "defense"))
check("a Defence mirror reflects nothing",
      reflect_for("defense", "defense") == 0)
check("Balance's attack is reflected at half strength",
      0 < reflect_for("balance", "defense")
      < reflect_for("attack", "defense"),
      (reflect_for("balance", "defense"), reflect_for("attack", "defense")))
for t2 in ("attack", "stamina", "balance"):
    check(f"a {t2} blade never reflects — that is Defence's edge",
          all(reflect_for(t1, t2) == 0
              for t1 in ("attack", "stamina", "defense")),
          {t1: reflect_for(t1, t2) for t1 in ("attack", "stamina", "defense")})

print("\n── 7. the help chart is generated, not written ──────────────────")
from cogs.ui import help_cog as HC                                # noqa: E402

check("every type has a row", set(HC.TYPE_MATCHUPS) == {
    "Attack", "Defense", "Stamina", "Balance"}, set(HC.TYPE_MATCHUPS))
check("Balance finally has one", "Balance" in HC.TYPE_MATCHUPS)
check("every row covers every type",
      all(set(v) == set(HC.TYPE_MATCHUPS) for v in HC.TYPE_MATCHUPS.values()))
for mine in HC.TYPE_MATCHUPS:
    for theirs, text in HC.TYPE_MATCHUPS[mine].items():
        a_act, b_act = TS.resolve_active_bonuses(mine, theirs)
        if a_act and not b_act:
            want, why = "✅", "strong"
        elif b_act and not a_act:
            want, why = "❌", "weak"
        else:
            want, why = "⚖️", "neither"
        check(f"help says {mine:8} vs {theirs:8} is {why}",
              text.startswith(want), text)
check("the chart renders without raising",
      HC.build_matchups_embed() is not None)
hsrc = open(os.path.join(ROOT, "cogs", "ui", "help_cog.py"),
            encoding="utf-8").read()
check("it is derived from ADVANTAGE, not hand-written",
      "ADVANTAGE as _ADV" in hsrc and "_matchup(mine, theirs)" in hsrc)
check("the embed no longer hardcodes a row order",
      '["Attack", "Defense", "Stamina"]' not in hsrc)
check("the stale pro tip is corrected",
      "If opponent is Stamina, spam Attack" not in hsrc)

bsrc = open(os.path.join(ROOT, "cogs", "battle", "battle.py"),
            encoding="utf-8").read()
check("the ;battle blurb still describes the MOVE wheel, which is unchanged",
      "Attack beats Stamina" in bsrc)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
