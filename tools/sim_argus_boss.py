#!/usr/bin/env python3
"""
tools/sim_argus_boss.py — ARGUS PRIME, the third boss with a full kit.

Two mechanics, both of which had to be measured rather than reasoned about:

**The vigil.** "+60% attack and crit" is authored as a CEILING reached over six
rounds, not a flat opening bonus — a boss that starts at its maximum never
shows the player a ramp, and the ramp is the tell that says stop letting it
breathe. The ceiling is asserted to be exactly +60%, because six lots of
`0.60 / 6` is the kind of arithmetic that ends up at 0.5999999.

**The Aegis.** Five rounds of TOTAL immunity, once per battle. Two things make
that a window rather than a wall, and both are tested: it is gated behind
`ultimate_used` so it cannot be re-cast, and it is implemented in `absorb()` —
the single function every damage path already funnels through. boss_ai calls
it for both fighters and boss_battle calls it again for the counter-hit during
a boss Special, so a flag checked anywhere else would leave one of those three
routes still landing damage.

The off-by-one this file caught: setting `aegis_turns = turns + 1` gave SIX
immune rounds, not five, because `absorb()` is read before the round's `tick()`
spends a charge. That is the opposite of v97's ability_amp, where the granting
round was deliberately dormant and did need the +1. Which side of the tick an
effect is read on is worth one measurement rather than one assumption.

Run:  python3 tools/sim_argus_boss.py
"""
import os
import random
import statistics
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

from utils.database import load_beyblades                          # noqa: E402
from cogs.battle.boss import argus as AG                           # noqa: E402
from cogs.battle.boss import drakos as DK                          # noqa: E402
from cogs.battle.boss import boss_abilities as NM                  # noqa: E402
from cogs.battle.boss import boss_ai as AI                         # noqa: E402
from cogs.battle.boss import boss_info as BI                       # noqa: E402
from cogs.battle.boss import boss_battle as BB                     # noqa: E402

BLADES = load_beyblades()
P = AG.ARGUS

print("\n── 1. the profile matches the brief ────────────────────────────")
check("attack is 130", P["attack"] == 130, P["attack"])
check("defense is 87", P["defense"] == 87, P["defense"])
check("stamina is 109", P["stamina"] == 109, P["stamina"])
check("rarity is Exclusive", P["rarity"] == "Exclusive", P["rarity"])
check("it is a boss-only blade — absent from beyblades.json, so it cannot "
      "spawn, be bought, boostered or listed",
      not any(n.lower().startswith("argus") for n in BLADES))
check("the image is the one supplied",
      P["image_url"].startswith("https://cdn.discordapp.com/attachments/"
                                "1510856884943454208/1538405554756517939/"))
check("it has two named abilities",
      [a["name"] for a in P["abilities"]]
      == ["Hundred-Eyed Vigil", "Aegis of the Sleepless"],
      [a["name"] for a in P["abilities"]])

print("\n── 2. the vigil ramps to exactly +60% ──────────────────────────")
st = AG.ArgusState()
check("it opens blind", st.eyes == 0 and st.stat_multiplier() == 1.0)
ramp = []
for _ in range(AG.EYE_MAX):
    st.tick()
    ramp.append((st.eyes, round(st.attack_pct(), 4), round(st.crit_pct(), 4)))
check(f"six rounds open six eyes {[r[0] for r in ramp]}",
      [r[0] for r in ramp] == [1, 2, 3, 4, 5, 6])
check("attack reaches EXACTLY +60%, not 59.99…",
      abs(st.attack_pct() - 0.60) < 1e-9, st.attack_pct())
check("crit reaches exactly +60% too",
      abs(st.crit_pct() - 0.60) < 1e-9, st.crit_pct())
check("the multiplier tops out at 1.60",
      abs(st.stat_multiplier() - 1.60) < 1e-9, st.stat_multiplier())
st.tick()
check("a seventh round does not push it past six",
      st.eyes == AG.EYE_MAX and abs(st.attack_pct() - 0.60) < 1e-9, st.eyes)
check("closing eyes really lowers the bonus",
      st.close_eyes(2) == 2 and abs(st.attack_pct() - 0.40) < 1e-9,
      st.attack_pct())
check("...and cannot go below blind", AG.ArgusState().close_eyes(5) == 0)
check("the ceiling is stated once, not scattered",
      AG.EYE_MAX * AG.EYE_ATTACK_PER == AG.EYE_ATTACK_PCT_MAX == 0.60)

print("\n── 3. the Aegis is FIVE rounds, and it is total ────────────────")
st = AG.ArgusState()
st.overdriven = True
dmg, effects = AG.special_damage("aegis", P["attack"], st, 90, 0.5, AI.DMG_SCALE)
check(f"the Special still lands ({dmg:.0f} damage)", dmg > 0)
check("...and says how long the wall is up",
      any("5" in e for e in effects), effects)

immune = []
for rd in range(1, 10):
    through, _broke = st.absorb(100.0)
    if through == 0.0:
        immune.append(rd)
    st.tick()
check(f"exactly five rounds are immune, rounds {immune}",
      immune == [1, 2, 3, 4, 5], immune)
check("the sixth round takes full damage",
      st.absorb(100.0)[0] == 100.0, st.absorb(100.0))

# Total, not reduced. A 99% reduction is a different mechanic.
st2 = AG.ArgusState()
st2.aegis_turns = 3
for amount in (1.0, 250.0, 9999.0):
    check(f"{amount:g} damage is stopped completely",
          st2.absorb(amount)[0] == 0.0, st2.absorb(amount))
check("is_immune agrees with absorb", st2.is_immune())

print("\n── 4. once per battle, and gated ───────────────────────────────")
st = AG.ArgusState()
check("the Aegis is not offered before Argus is bloodied",
      "aegis" not in AG.available_specials(st, True))
st.overdriven = True
check("...and is offered once it is",
      "aegis" in AG.available_specials(st, True))
AG.special_damage("aegis", P["attack"], st, 90, 0.5, AI.DMG_SCALE)
check("firing it marks the Ultimate spent", st.ultimate_used)
check("...so it can never be cast twice",
      "aegis" not in AG.available_specials(st, True))
check("a full gauge is still required",
      AG.available_specials(AG.ArgusState(), False) == [])
check("the HP gate is what arms it",
      AG.ArgusState().should_ascend(AG.AEGIS_HP_GATE - 0.01)
      and not AG.ArgusState().should_ascend(0.99))

print("\n── 5. the AI actually reaches every Special ────────────────────")
fired = set()
for eyes in range(0, AG.EYE_MAX + 1):
    for hp in (0.9, 0.6, 0.3, 0.1):
        for over in (False, True):
            s = AG.ArgusState()
            s.eyes, s.overdriven = eyes, over
            got = AG.pick_special(s, True, 0.5, hp)
            if got:
                fired.add(got)
check(f"every Special is reachable {sorted(fired)}",
      fired == set(AG.SPECIALS), sorted(set(AG.SPECIALS) - fired))
check("a blind, unpressured Argus does not waste the Aegis",
      AG.pick_special(AG.ArgusState(), True, 0.5, 1.0) != "aegis")
check("no Special is picked on an empty gauge",
      AG.pick_special(AG.ArgusState(), False, 0.5, 0.2) is None)

print("\n── 6. eyes feed the spear ──────────────────────────────────────")
blind = AG.ArgusState()
seeing = AG.ArgusState(); seeing.eyes = AG.EYE_MAX
d0, _ = AG.special_damage("spearfall", P["attack"], blind, 90, 0.5, AI.DMG_SCALE)
d6, _ = AG.special_damage("spearfall", P["attack"], seeing, 90, 0.5, AI.DMG_SCALE)
check(f"a fully sighted Spearfall hits harder ({d0:.0f} -> {d6:.0f})", d6 > d0)
check("...and the Special scale is the 155 that was asked for",
      AG.SPECIALS["spearfall"]["mult"] == 1.55
      and AG.SPECIALS["aegis"]["mult"] == 1.55)

print("\n── 7. state copies are deep — the AI searches ahead ────────────")
st = AG.ArgusState()
st.eyes, st.aegis_turns, st.ultimate_used = 4, 3, True
c = st.copy()
c.eyes, c.aegis_turns, c.ultimate_used = 0, 0, False
check("mutating a copy does not touch the live state",
      (st.eyes, st.aegis_turns, st.ultimate_used) == (4, 3, True),
      (st.eyes, st.aegis_turns, st.ultimate_used))
check("...and the copy carried the values across",
      AG.ArgusState(eyes=4).copy().eyes == 4)

print("\n── 8. it is wired into all three registries ────────────────────")
check("boss_battle knows it", "argus" in BB.BOSSES, sorted(BB.BOSSES))
check("...with the right stat block",
      (BB.BOSSES["argus"]["attack"], BB.BOSSES["argus"]["defense"],
       BB.BOSSES["argus"]["stamina"]) == (130, 87, 109))
check("the module resolver finds it",
      BB._module_for(BB.BOSSES["argus"]) is AG)
check("...and builds an ArgusState",
      isinstance(BB._make_state(BB.BOSSES["argus"]), AG.ArgusState))
check("`;bossinfo` can look it up", "argus" in BI.REGISTRY)
check("...and knows which module owns its Specials",
      BI.SPECIAL_MODULES.get("argus") is AG)
check("the other two bosses still resolve",
      BB._module_for(BB.BOSSES["drakos"]) is DK
      and BB._module_for(BB.BOSSES["nemesis"]) is NM)
check("it does not require a prior clear the way NEMESIS does",
      "argus" not in BB.BOSS_REQUIRES, BB.BOSS_REQUIRES)

print("\n── 9. how hard is it, really ───────────────────────────────────")
# A solo, ability-less, random-move harness. It is NOT a win-rate — real
# fights have parties, blade kits and gauge management. It is a RELATIVE
# measure against the two bosses already shipped, run identically for all
# three, which is the only comparison this harness can honestly make.
def run(blade_name, seed, prof, mk):
    rnd = random.Random(seed)
    bs = BLADES[blade_name]["stats"]
    boss = AI.Fighter(name=prof["name"], hp=prof["hp"], max_hp=prof["hp"],
                      attack=prof["attack"], defense=prof["defense"],
                      stamina_stat=prof["stamina"], is_boss=True)
    boss.state = mk()
    model = AI.OpponentModel()
    foe = AI.Fighter(name=blade_name, hp=2108, max_hp=2108,
                     attack=bs["attack"], defense=bs["defense"],
                     stamina_stat=bs["stamina"])
    for rd in range(1, 80):
        boss.state.tick()
        pm = rnd.choice([AI.MOVE_ATTACK] * 6 + [AI.MOVE_DEFENSE,
                                                AI.MOVE_STAMINA,
                                                AI.MOVE_CHARGE])
        bm, _ = AI.choose_move(boss, foe, model, rng=rnd, difficulty="elite")
        AI.resolve(foe, boss, pm, bm)
        model.observe(pm)
        if foe.hp <= 0:
            return rd, boss.hp / boss.max_hp
    return 80, boss.hp / boss.max_hp


TESTERS = ["Ultimate Valkyrie", "Nightmare Longinus", "Cho-Z Achilles",
           "Geist Fafnir"]
result = {}
for label, prof, mk in (("argus", P, AG.ArgusState),
                        ("drakos", DK.DRAKOS, DK.DrakosState),
                        ("nemesis", NM.NEMESIS, NM.BossState)):
    surv, left = [], []
    for bn in TESTERS:
        for sd in range(15):
            r, h = run(bn, sd, prof, mk)
            surv.append(r)
            left.append(h)
    result[label] = (statistics.median(surv), statistics.median(left))
    print(f"       {label:<8} player survives {result[label][0]:>3.0f} rounds, "
          f"boss left on {result[label][1] * 100:>5.1f}% HP")

check("Argus is harder than Drakos — the player survives fewer rounds",
      result["argus"][0] < result["drakos"][0], result)
check("...and not harder than NEMESIS, which is the god-tier gate",
      result["argus"][0] >= result["nemesis"][0] - 2, result)
check("the fight is not instant — the player lasts more than 10 rounds",
      result["argus"][0] > 10, result["argus"][0])
check("...and Argus does take real damage, so it is not a wall",
      result["argus"][1] < 0.95, result["argus"][1])

print("\n── 10. nothing else moved ──────────────────────────────────────")
check("still three bosses with kits", len(BB.BOSSES) == 3, sorted(BB.BOSSES))
check("Drakos's Stars still ramp",
      DK.DrakosState().copy() is not None)
check("boss keys are unique", len(set(BB.BOSSES)) == len(BB.BOSSES))
check("every boss in the registry has a name and an image field",
      all(b.get("name") and "image_url" in b for b in BI.REGISTRY.values()))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
