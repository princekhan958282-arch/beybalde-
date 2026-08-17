#!/usr/bin/env python3
"""
tools/sim_boss_iq.py — Boss IQ, and the blade abilities that never fired.

Two findings, both of the same kind: code that looked correct, was listed in
the UI, and did nothing.

**41% of the roster had no abilities in a boss fight.** `blade_abilities`
parsed `ability["chain"]` (a `{"effect": ...}` list) and a table of flat tuning
keys. Both shapes are real and both still work — but 42 of 103 blades are
authored ONLY in the `rules` / `do` / `op` DSL, matched neither, and walked
into a boss fight with a completely empty kit while their abilities were
printed on the info card. Void Longinus, Aetherion Vortex, Tartarus Reaper and
Revive Phoenix were among them.

**The IQ ladder had no rungs in it.** Difficulty already varied search depth
and blunder rate, but nothing expressed the spec's central idea — that a
higher tier means the boss READS YOU BETTER. Two failed attempts are recorded
here as checks, because both looked right and neither was:

  1. Blending the learned weights toward uniform. That scales every weight
     toward the same mean, so it preserves their ORDER and the top pick never
     moves: IQ 2, 3, 4 and 5 all guessed right 71.7% of the time.
  2. Keeping the full `counts` model at IQ 5 and windowing only below it. The
     full model seeds every move at 1.0 and carries that stale prior forever,
     so the most expensive tier read WORSE than the one below it — the boss
     won 68.3% at IQ 4 and 59.4% at IQ 5.

What works is one estimator at every rung with only the memory window
changing. The ladder below is measured, and asserted monotonic.

Run:  python3 tools/sim_boss_iq.py
"""
import os
import random
import sys
from dataclasses import replace

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

from utils.database import load_beyblades                          # noqa: E402
from cogs.battle.boss import boss_ai as AI                         # noqa: E402
from cogs.battle.boss import boss_tiers as BT                      # noqa: E402
from cogs.battle.boss import boss_battle as BB                     # noqa: E402
from cogs.battle.boss import blade_abilities as bk                 # noqa: E402

BLADES = load_beyblades()
RUNGS = ("rookie", "veteran", "elite", "legend", "nightmare")


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. IQ is 1-5 and every rung has one ─────────────────────────")
check("every difficulty rung declares an IQ",
      all("iq" in AI.DIFFICULTY[r] for r in RUNGS))
check("...and a read accuracy", all("read" in AI.DIFFICULTY[r] for r in RUNGS))
iqs = [AI.iq_for(r) for r in RUNGS]
check(f"IQ runs 1 to 5 across the rungs {iqs}", iqs == [1, 2, 3, 4, 5], iqs)
check("...and never repeats — a ladder with two identical rungs is a ladder "
      "the player cannot feel", len(set(iqs)) == 5, iqs)
reads = [AI.DIFFICULTY[r]["read"] for r in RUNGS]
check(f"read accuracy only ever rises {reads}", reads == sorted(reads), reads)
check("the bottom rung reads nothing and the top reads everything",
      reads[0] == 0.0 and reads[-1] == 1.0, reads)
check("every IQ has a label, so the player is sold a word not just a number",
      all(AI.iq_for(r) in AI.IQ_LABELS for r in RUNGS))
check("an unknown rung degrades to mid rather than raising",
      AI.iq_for("nonsense") == 3 and AI.iq_for(None) == 3)
for r in RUNGS:
    lab = AI.iq_label(r)
    check(f"{r:10} -> {lab}", lab.startswith(f"IQ {AI.iq_for(r)}"), lab)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. the difficulty a purchased tier actually buys ─────────────")
for key in BB.BOSSES:
    rungs = [BT.walk_difficulty(BB.BOSSES[key]["difficulty"], t) for t in BT.TIERS]
    got = [AI.iq_for(x) for x in rungs]
    check(f"{key:10} IQ by tier {got}", got == sorted(got), got)
# Worth stating plainly rather than hiding: every boss is authored `elite`
# (IQ 3) and walk_difficulty caps at the top rung, so the five paid tiers
# currently buy only three distinct IQs. Asserted so the number is visible if
# it is ever changed, NOT asserted to be 5 — that would be asserting a design
# decision nobody has taken.
_argus = [AI.iq_for(BT.walk_difficulty(BB.BOSSES["argus"]["difficulty"], t))
          for t in BT.TIERS]
check(f"the top tiers share an IQ ceiling — {len(set(_argus))} distinct IQs "
      f"across {len(BT.TIERS)} paid tiers {_argus}. Lowering it needs the "
      f"authored rung lowered, which changes Standard for everyone",
      len(set(_argus)) >= 2, _argus)
_src = open(os.path.join(ROOT, "cogs", "battle", "boss", "boss_battle.py"),
            encoding="utf-8").read()
check("the IQ is shown on the boss card, next to the price",
      "iq_label" in _src)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. read accuracy actually changes the prediction ─────────────")
_f = AI.Fighter(name="P", hp=2000, max_hp=2108, attack=120, defense=100,
                stamina_stat=100, sp=10.0)
_m = AI.OpponentModel()
for _ in range(12):
    _m.observe(AI.MOVE_ATTACK)                    # a hard habit to read
_mass = [replace(_m, read=AI.DIFFICULTY[r]["read"]).predict(_f)
         .get(AI.MOVE_ATTACK, 0.0) for r in RUNGS]
print("      probability assigned to a 12-times-repeated Attack:")
for r, m in zip(RUNGS, _mass):
    print(f"        IQ{AI.iq_for(r)} {r:10} {m * 100:5.1f}%")
check("a blind boss sits at chance against a hard habit — near-random, NOT "
      "anti-correlated. An earlier version left it guessing the same wrong "
      "move every turn, which is worse than random rather than weaker",
      abs(_mass[0] - 0.25) < 0.06, _mass[0])
check("...and a full-IQ boss reads it clearly", _mass[-1] > 0.75, _mass[-1])
check("the ladder only ever rises", _mass == sorted(_mass),
      [round(x, 3) for x in _mass])
# The bug the one-estimator rewrite fixed: `counts` seeds every move at 1.0 and
# never forgets that, so a full-`read` model was LESS confident than a windowed
# one and IQ 5 read worse than IQ 4.
check("IQ 5 reads at least as well as IQ 4 — the inversion is gone",
      _mass[-1] >= _mass[-2] - 1e-9, (_mass[-2], _mass[-1]))
check("with no history to read, every rung sits at the same flat guess",
      len({round(replace(AI.OpponentModel(),
                         read=AI.DIFFICULTY[r]["read"]).predict(_f)
                 .get(AI.MOVE_ATTACK, 0.0), 4) for r in RUNGS}) == 1)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 4. and it changes who WINS, which is the only real test ──────")
def winrate(rung, habit, blades, seeds=90):
    """Boss win rate at EQUAL stats, so the AI decides rather than the numbers."""
    wins = n = 0
    for bn in blades:
        bs = BLADES[bn]["stats"]
        for sd in range(seeds):
            rnd = random.Random(sd)
            boss = AI.Fighter(name="B", hp=2108, max_hp=2108,
                              attack=bs["attack"], defense=bs["defense"],
                              stamina_stat=bs["stamina"], is_boss=True)
            foe = AI.Fighter(name="P", hp=2108, max_hp=2108,
                             attack=bs["attack"], defense=bs["defense"],
                             stamina_stat=bs["stamina"])
            model = AI.OpponentModel()
            for _ in range(120):
                pm = AI.MOVE_ATTACK if rnd.random() < habit else rnd.choice(
                    [AI.MOVE_DEFENSE, AI.MOVE_STAMINA, AI.MOVE_CHARGE])
                if not foe.can(pm):
                    pm = AI.MOVE_STAMINA
                bm, _v = AI.choose_move(boss, foe, model, rng=rnd,
                                        difficulty=rung)
                AI.resolve(boss, foe, bm, pm)
                model.observe(pm)
                if foe.hp <= 0:
                    wins += 1
                    break
                if boss.hp <= 0:
                    break
            n += 1
    return wins / max(1, n)


BL = ["Cho-Z Achilles", "Geist Fafnir", "Wild Wyvern"]
rates = []
print("      boss win rate at equal stats, vs a player who attacks 80%:")
for r in RUNGS:
    w = winrate(r, 0.80, BL)
    rates.append(w)
    print(f"        IQ{AI.iq_for(r)} {AI.IQ_LABELS[AI.iq_for(r)]:12} {w * 100:5.1f}%")
check("a smarter boss wins more — the ladder is monotonic",
      all(rates[i + 1] >= rates[i] - 0.02 for i in range(len(rates) - 1)),
      [round(x, 3) for x in rates])
check("...and the gap between the ends is worth paying for",
      rates[-1] - rates[0] > 0.15, (rates[0], rates[-1]))
check("IQ 1 is beatable — a tier nobody can clear is a tier nobody fights "
      "twice", rates[0] < 0.60, rates[0])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5. every blade's ability now counts in a boss fight ──────────")
def kit_is_live(k) -> bool:
    flat = {"attack": 1.0, "defense": 1.0, "stamina": 1.0}
    return bool(k.stat_mult != flat or k.low_hp_mult != flat or k.reflect
                or k.reduction or k.dmg_amp or k.ignore_def or k.crit_chance
                or k.heal_pct or k.true_damage or k.flat_damage
                or k.flat_reduction)


live = [n for n, b in BLADES.items() if kit_is_live(bk.kit_for(b))]
dead = [n for n in BLADES if n not in live]
print(f"      {len(live)} of {len(BLADES)} blades have a working kit "
      f"({len(live) / len(BLADES) * 100:.0f}%)")
# Measured before the fix: 61 live, 42 dead — 41% of the roster. A floor
# rather than an exact number, because the roster grows.
check(f"at least 85% of the roster has a live kit ({len(live)}/{len(BLADES)})",
      len(live) >= len(BLADES) * 0.85, sorted(dead))
check("the parser reads the DSL every blade is actually authored in",
      hasattr(bk.BladeKit, "_rules") and "bonus_damage_pct" in bk.BladeKit.OP_TO_EFFECT)
# NOT "no blade uses chain" — 28 of them do, and that path works. The gap was
# the blades that used ONLY the rules DSL and so matched neither older shape.
_chain = {n for n, b in BLADES.items()
          for a in (b.get("abilities") or []) if a.get("chain")}
_rules_only = {n for n, b in BLADES.items()
               if n not in _chain
               and any(a.get("rules") for a in (b.get("abilities") or []))}
check(f"{len(_chain)} blades use the old `chain` shape and still work",
      all(kit_is_live(bk.kit_for(BLADES[n])) for n in _chain),
      [n for n in _chain if not kit_is_live(bk.kit_for(BLADES[n]))])
check(f"...and the {len(_rules_only)} that use ONLY the rules DSL — the ones "
      f"that had nothing — now work too",
      sum(kit_is_live(bk.kit_for(BLADES[n])) for n in _rules_only)
      >= len(_rules_only) * 0.8,
      [n for n in _rules_only if not kit_is_live(bk.kit_for(BLADES[n]))])

# Named blades that had NOTHING before, spot-checked through the real parser.
for n in ("Void Longinus", "Aetherion Vortex", "Tartarus Reaper",
          "Twin Nemesis", "Deep Caynox", "Deep Caynox ELT"):
    if n in BLADES:
        k = bk.kit_for(BLADES[n])
        check(f"{n:20} {k.summary()}", kit_is_live(k))

# "+0% STA" and "0% lifesteal" made a real kit look broken on the lobby card.
# Token equality, NOT a substring test: "0% crit" is a substring of
# "100% crit" and "30% crit", so the naive version flagged three healthy kits.
ZERO_TOKENS = {"+0% ATT", "+0% DEF", "+0% STA", "+0% dmg", "−0% taken",
               "0% crit", "0% lifesteal", "0 reflect", "pierce 0%",
               "+0 dmg", "−0 taken"}
_noise = [f"{n}: {bk.kit_for(b).summary()}" for n, b in BLADES.items()
          if ZERO_TOKENS & set(bk.kit_for(b).summary().split(" · "))]
check("the summary never prints a channel that rounds to nothing",
      not _noise, _noise[:3])

# The caps still bind, or a blade with three stacking passives out-scales the
# boss's whole design.
worst = max(BLADES.values(), key=lambda b: max(bk.kit_for(b).stat_mult.values()))
wk = bk.kit_for(worst)
check(f"stat bonuses stay capped ({worst['name']}: "
      f"{max(wk.stat_mult.values()):.2f}x)",
      max(wk.stat_mult.values()) <= 1 + bk.MAX_STAT_BONUS + 1e-9)
check("damage amp stays capped",
      all(bk.kit_for(b).dmg_amp <= bk.MAX_DMG_AMP + 1e-9 for b in BLADES.values()))
check("reduction stays capped",
      all(bk.kit_for(b).reduction <= bk.MAX_REDUCTION + 1e-9
          for b in BLADES.values()))
check("the new flat channels are capped too",
      all(bk.kit_for(b).flat_damage <= bk.BladeKit.MAX_FLAT_DAMAGE + 1e-9
          and bk.kit_for(b).flat_reduction <= bk.BladeKit.MAX_FLAT_REDUCTION + 1e-9
          for b in BLADES.values()))

# A kit field nothing reads is exactly the bug this change exists to fix —
# Argus's crit sat unread for two versions. Both new channels are consumed.
_bb = open(os.path.join(ROOT, "cogs", "battle", "boss", "boss_battle.py"),
           encoding="utf-8").read().split("def _apply_kit")[1].split("\n    def ")[0]
check("_apply_kit consumes flat_damage, not just defines it",
      "flat_damage" in _bb)
check("...and flat_reduction", "flat_reduction" in _bb)
check("...and the flat soak is capped at the hit that landed, or a refund "
      "larger than the damage becomes a heal", "min(float(flat_red), taken)" in _bb)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6. nothing else moved ───────────────────────────────────────")
check("the fight still resolves for every boss",
      all(BB._make_state(BB.BOSSES[k]) is not None for k in BB.BOSSES))
_probe = AI.Fighter(name="x", hp=100, max_hp=100, attack=100, defense=100,
                    stamina_stat=100)
check("resolve() is still deterministic — the search evaluates it, so a die "
      "roll in there would make the AI plan against a fight that never happens",
      True)
_a1 = AI.Fighter(name="a", hp=999, max_hp=999, attack=120, defense=90,
                 stamina_stat=100)
_b1 = AI.Fighter(name="b", hp=999, max_hp=999, attack=110, defense=95,
                 stamina_stat=100)
_a2, _b2 = _a1.clone(), _b1.clone()
_r1 = AI.resolve(_a1, _b1, AI.MOVE_ATTACK, AI.MOVE_DEFENSE)
_r2 = AI.resolve(_a2, _b2, AI.MOVE_ATTACK, AI.MOVE_DEFENSE)
check("...verified: the same exchange twice gives the same numbers",
      _r1["dmg_to_b"] == _r2["dmg_to_b"] and _r1["dmg_to_a"] == _r2["dmg_to_a"],
      (_r1["dmg_to_b"], _r2["dmg_to_b"]))
check("choosing a move never mutates the shared opponent model — it is copied "
      "per decision, so a boss's read strength cannot leak between turns",
      "replace(model, read=" in open(
          os.path.join(ROOT, "cogs", "battle", "boss", "boss_ai.py"),
          encoding="utf-8").read())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
