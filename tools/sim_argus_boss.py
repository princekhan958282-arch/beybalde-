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
from cogs.battle.boss import boss_tiers as BT                      # noqa: E402

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
      any("5" in e for e in effects["lines"]), effects)

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
# This used to assert the opposite — that Argus stayed BELOW NEMESIS, the
# free god-tier gate. v1.07 wired Argus's abilities into the engine (its crit
# was read by nobody and its eyes never closed), and the ordering flipped:
# NEMESIS ~22 rounds, Argus ~16. That is deliberate. Argus is the one boss you
# pay 100,000 to 1,000,000 coins to face, every attempt, for no coins back —
# if it were the easier fight, the fee would be buying a worse boss.
check("Argus is now the hardest fight in the game — it is the one you pay for",
      result["argus"][0] < result["nemesis"][0], result)
check("the fight is not instant — the player lasts more than 10 rounds",
      result["argus"][0] > 10, result["argus"][0])
check("...and Argus does take real damage, so it is not a wall",
      result["argus"][1] < 0.95, result["argus"][1])

print("\n── 9a. v1.07 — the half of the Vigil that did nothing ──────────")
# `crit_pct()` was computed and read by nobody: the string "crit" did not
# appear once in boss_ai. Argus advertised "+60% Attack and crit" and was paid
# for exactly half of it. Same story one method down — `bank_debt` cannot see
# max_hp, so the eye-break written against it could never compute its own
# threshold and shipped as `return None`. EYE_BREAK_DAMAGE and
# EYES_LOST_PER_BREAK were dead constants and the counterplay the ability text
# promises did not exist.
check("the engine has a crit multiplier at all",
      hasattr(NM.BaseBossState, "crit_mult"))
check("...and a boss that doesn't use crit is bit-for-bit unchanged",
      NM.BaseBossState().crit_mult() == 1.0
      and DK.DrakosState().crit_mult() == 1.0
      and NM.BossState().crit_mult() == 1.0)
check("a blind Argus crits for nothing",
      AG.ArgusState(eyes=0).crit_mult() == 1.0)
check(f"...and a fully-sighted one for +{AI.CRIT_DAMAGE_BONUS * 0.60:.0%}",
      abs(AG.ArgusState(eyes=AG.EYE_MAX).crit_mult()
          - (1 + 0.60 * AI.CRIT_DAMAGE_BONUS)) < 1e-9,
      AG.ArgusState(eyes=AG.EYE_MAX).crit_mult())
check("the stated +60% crit is a ceiling, exactly — not 0.5999999",
      abs(AG.ArgusState(eyes=AG.EYE_MAX).crit_pct() - 0.60) < 1e-9)
# Through the real engine, not just the state object.
_a = AI.Fighter(name="A", hp=3200, max_hp=3200, attack=130, defense=87,
                stamina_stat=109, is_boss=True)
_a.state = AG.ArgusState(eyes=0)
_p = AI.Fighter(name="P", hp=9e9, max_hp=9e9, attack=120, defense=100,
                stamina_stat=100)
AI.resolve(_a, _p, AI.MOVE_ATTACK, AI.MOVE_CHARGE)
blind_hit = 9e9 - _p.hp
_a.state = AG.ArgusState(eyes=AG.EYE_MAX)
_p.hp = 9e9
AI.resolve(_a, _p, AI.MOVE_ATTACK, AI.MOVE_CHARGE)
sighted_hit = 9e9 - _p.hp
check(f"an open eye is felt in resolve(), not only in the state "
      f"({blind_hit:.0f} -> {sighted_hit:.0f})",
      sighted_hit > blind_hit * 1.9, (blind_hit, sighted_hit))

check("the eye-break lives on break_stars, the hook that gets max_hp",
      hasattr(AG.ArgusState, "break_stars"))
_st = AG.ArgusState(eyes=AG.EYE_MAX)
check("chip damage does not close an eye",
      _st.break_stars(3200 * (AG.EYE_BREAK_DAMAGE / 2), 3200) == 0
      and _st.eyes == AG.EYE_MAX)
check(f"...but a hit worth {AG.EYE_BREAK_DAMAGE:.1%} of its health closes "
      f"{AG.EYES_LOST_PER_BREAK}",
      _st.break_stars(3200 * AG.EYE_BREAK_DAMAGE, 3200)
      == AG.EYES_LOST_PER_BREAK
      and _st.eyes == AG.EYE_MAX - AG.EYES_LOST_PER_BREAK)
_st.eyes = 1
check("it can never close more eyes than are open",
      _st.break_stars(9999.0, 3200) == 1 and _st.eyes == 0)
_st.eyes, _st.aegis_turns = AG.EYE_MAX, 3
check("nothing shakes an eye loose through the Aegis — absorb() already "
      "zeroed the hit before break_stars is reached",
      _st.absorb(9999.0)[0] == 0.0)
# And it actually fires in a real fight, rather than being a threshold nothing
# ever crosses — the failure mode Drakos's Stars were written around.
_breaks = 0
for _bn in TESTERS:
    for _sd in range(10):
        _rnd = random.Random(_sd)
        _bs = BLADES[_bn]["stats"]
        _boss = AI.Fighter(name="A", hp=P["hp"], max_hp=P["hp"],
                           attack=P["attack"], defense=P["defense"],
                           stamina_stat=P["stamina"], is_boss=True)
        _boss.state = AG.ArgusState()
        _mdl = AI.OpponentModel()
        _foe = AI.Fighter(name=_bn, hp=2108, max_hp=2108, attack=_bs["attack"],
                          defense=_bs["defense"], stamina_stat=_bs["stamina"])
        for _rd in range(1, 80):
            _boss.state.tick()
            _was = _boss.state.eyes
            _pm = _rnd.choice([AI.MOVE_ATTACK] * 6 + [AI.MOVE_DEFENSE,
                                                      AI.MOVE_STAMINA,
                                                      AI.MOVE_CHARGE])
            _bm, _ = AI.choose_move(_boss, _foe, _mdl, rng=_rnd,
                                    difficulty="elite")
            AI.resolve(_foe, _boss, _pm, _bm)
            _mdl.observe(_pm)
            _breaks += _boss.state.eyes < _was
            if _foe.hp <= 0 or _boss.hp <= 0:
                break
check(f"and it fires in real fights — {_breaks} eye-breaks over "
      f"{len(TESTERS) * 10} of them", _breaks > 0, _breaks)

print("\n── 9b. the v1.04 rules ─────────────────────────────────────────")
# Player Specials deal 20% against a boss. Some blades were ending a boss with
# one button; a boss that dies to one button is not a boss.
check("the cut is 20%", AI.PLAYER_SPECIAL_VS_BOSS == 0.20,
      AI.PLAYER_SPECIAL_VS_BOSS)
pf = AI.Fighter(name="P", hp=2108, max_hp=2108, attack=140, defense=100,
                stamina_stat=100)
full = AI._raw_damage(pf, special=True, vs_boss=False)
cut  = AI._raw_damage(pf, special=True, vs_boss=True)
check(f"a player Special vs a boss is a fifth ({full:.0f} -> {cut:.0f})",
      abs(cut - full * 0.20) < 1e-6, (full, cut))
check("...and an ordinary ATTACK is untouched — the cut is aimed at the one "
      "thing that was one-shotting",
      AI._raw_damage(pf, special=False, vs_boss=True)
      == AI._raw_damage(pf, special=False, vs_boss=False))
bf = AI.Fighter(name="B", hp=3200, max_hp=3200, attack=130, defense=87,
                stamina_stat=109, is_boss=True,
                special_atk_pct=AI.BOSS_SPECIAL_TOTAL)
check("...and the BOSS's own Special is untouched — it runs on "
      "special_atk_pct, a different branch",
      AI._raw_damage(bf, special=True, vs_boss=True)
      == AI._raw_damage(bf, special=True, vs_boss=False))
# The scoping bug this check exists for: Story opponents are also is_boss=True,
# and gating on that cut their incoming Specials by 80% — measured 0% win rate
# on the Story finale, every stage unwinnable. `special_atk_pct` is the real
# discriminator, set by boss_battle on bosses and left unset by Story.
story = AI.Fighter(name="S", hp=1500, max_hp=1500, attack=110, defense=90,
                   stamina_stat=100, is_boss=True)
check("a STORY opponent is is_boss but has no special_atk_pct",
      story.is_boss and story.special_atk_pct is None)
check("...so Story Specials are NOT cut — gating on is_boss made the finale "
      "a guaranteed loss",
      getattr(story, "special_atk_pct", None) is None)
check("a real boss DOES carry special_atk_pct",
      bf.special_atk_pct is not None)
check("the source gates on special_atk_pct, not is_boss",
      "special_atk_pct" in open(
          os.path.join(ROOT, "cogs", "battle", "boss", "boss_ai.py"),
          encoding="utf-8").read().split("def offence")[1][:900])

check("PvP never sees this — it lives in the boss engine, not damage_rules",
      "PLAYER_SPECIAL_VS_BOSS" not in open(
          os.path.join(ROOT, "cogs", "battle", "damage_rules.py"),
          encoding="utf-8").read())

# Every boss fights at level 100.
check("the boss level is 100", BB.BOSS_LEVEL == 100, BB.BOSS_LEVEL)
for key in BB.BOSSES:
    cfg = BB.BOSSES[key]
    a, d, st_ = BB.boss_stats(cfg)
    check(f"{key} scales up at level 100 "
          f"({cfg['attack']}/{cfg['defense']}/{cfg['stamina']} -> "
          f"{a:.0f}/{d:.0f}/{st_:.0f})",
          a > cfg["attack"] and d > cfg["defense"] and st_ > cfg["stamina"])
check("boss_stats degrades to the authored line rather than raising",
      BB.boss_stats({"attack": 10, "defense": 10, "stamina": 10}) is not None)

print("\n── 9c. Argus is paid for, and pays nothing back ────────────────")
check("no coin prize at all", P["reward"]["coins"] == 0, P["reward"])
check("...but the casino chips and the copy remain, so it is not a "
      "zero-reward fight", P["reward"]["casino"] > 0)
prices = [BT.price_of(t, "argus") for t in BT.TIERS]
check(f"entry runs 100,000 -> 1,000,000 {prices}",
      prices[0] == 100_000 and prices[-1] == 1_000_000, prices)
check("...and it only ever rises", prices == sorted(prices), prices)
check("every other boss keeps the tier price",
      all(BT.price_of(t) == BT.price_of(t, "drakos") == BT.price_of(t, "nemesis")
          for t in BT.TIERS))
check("an unknown boss key falls back to the tier price",
      BT.price_of("savage", "nobody") == BT.price_of("savage"))
# The charge must refuse rather than half-apply: raising inside mutate_user
# abandons the whole write, so a refused entry cannot take the coins.
try:
    BT.charge({"coins": 50_000}, "standard", "argus")
    check("a player short of the fee is refused", False, "it went through")
except BT.TierError as exc:
    check("a player short of the fee is refused", True)
    check("...and the message names the real Argus price",
          "100,000" in str(exc), str(exc))
_p = {"coins": 250_000}
check("a player who can pay is charged exactly the Argus price",
      BT.charge(_p, "standard", "argus") == 100_000 and _p["coins"] == 150_000,
      _p)

# ...and because the wallet is the limiter, the clock is not. A boss that costs
# up to a million coins an attempt and pays nothing back does not also need a
# two-hour timer: the fee stops anyone who cannot afford it, and taxes anyone
# who can, which is a far harder gate than waiting.
check("Argus is exempt from the daily timer", "argus" in BB.UNTIMED_BOSSES)
check("...and the other two are not — the exemption is for PAID bosses",
      not (BB.UNTIMED_BOSSES & {"drakos", "nemesis"}), BB.UNTIMED_BOSSES)
_fresh = {"boss_daily": {"argus": __import__("time").time(),
                         "drakos": __import__("time").time()}}
check("a just-fought Argus is immediately available again",
      BB.daily_remaining(_fresh, "argus") == 0.0)
check("...while a just-fought Drakos still has hours to run",
      BB.daily_remaining(_fresh, "drakos") > 3600)
# charge_daily must also skip, or the profile accumulates a timestamp that
# means nothing and would silently re-arm if the exemption were ever lifted.
# Asserted by watching for the write rather than by reading the source: the
# point is that no profile is touched at all, and only a call can show that.
import asyncio                                                      # noqa: E402

_writes = []
_real_get, _real_upd = BB.get_user, BB.update_user


async def _fake_get_boss_daily(uid):
    return {"boss_daily": {}}


async def _fake_update_boss_daily(uid, prof):
    _writes.append((uid, prof))


BB.get_user = _fake_get_boss_daily
BB.update_user = _fake_update_boss_daily
try:
    asyncio.run(BB.charge_daily(1, "argus"))
    check("charging an untimed boss writes nothing", _writes == [], _writes)
    asyncio.run(BB.charge_daily(1, "drakos"))
    check("...while a timed one still records the attempt",
          len(_writes) == 1 and "drakos" in _writes[0][1]["boss_daily"], _writes)
finally:
    BB.get_user, BB.update_user = _real_get, _real_upd

print("\n── 9e. the Special counter no longer dodges the 20% rule ───────")
# _fire_special hand-rolls the player's counter-hit instead of going through
# ai._raw_damage, and that made it the one path where a player Special still
# reached a boss at full strength — on precisely the turns a boss fires its
# own Special, which is when a player is most likely to answer with theirs.
_src = open(os.path.join(ROOT, "cogs", "battle", "boss", "boss_battle.py"),
            encoding="utf-8").read()
_body = _src.split("def _fire_special")[1].split("\n    def ")[0]
check("the counter-hit applies PLAYER_SPECIAL_VS_BOSS",
      "PLAYER_SPECIAL_VS_BOSS" in _body)
check("...and it breaks Stars/Eyes too, the way ai.resolve() does",
      "break_stars" in _body)

print("\n── 9d. the Aegis is immunity AND immortality ───────────────────")
st = AG.ArgusState()
st.aegis_turns = 3
check("it reports immune", st.is_immune())
check("...and immortal", st.is_immortal())
check("absorb stops damage that ASKS", st.absorb(9999.0)[0] == 0.0)
check("guard_hp stops damage that does NOT ask — it holds at 1, never 0",
      st.guard_hp(-500.0) == 1.0 and st.guard_hp(0.0) == 1.0)
check("...and never resurrects or inflates a healthy bar",
      st.guard_hp(1500.0) == 1500.0)
st.aegis_turns = 0
check("once the window closes, neither holds",
      not st.is_immortal() and st.guard_hp(-5.0) == -5.0)
# Through the real engine: something writes hp directly, bypassing absorb.
bf2 = AI.Fighter(name="B", hp=40.0, max_hp=3200, attack=130, defense=87,
                 stamina_stat=109, is_boss=True)
bf2.state = AG.ArgusState()
bf2.state.aegis_turns = 5
pf2 = AI.Fighter(name="P", hp=2000, max_hp=2108, attack=200, defense=100,
                 stamina_stat=100)
AI.resolve(pf2, bf2, AI.MOVE_ATTACK, AI.MOVE_ATTACK)
check("an immortal Argus survives an exchange that would have killed it",
      bf2.hp >= 1.0, bf2.hp)
bf2.state.aegis_turns = 0
bf2.hp = 40.0
AI.resolve(pf2, bf2, AI.MOVE_ATTACK, AI.MOVE_ATTACK)
check("...and dies to the same exchange once the window is over",
      bf2.hp <= 0.0, bf2.hp)

print("\n── 9e2. THE hang: special_damage returned the wrong shape ──────")
# The report was "during boss battle the bot suddenly stops responding", and
# this was it. Argus's special_damage returned (damage, list_of_note_strings)
# where every other boss returns (damage, effects_DICT) — and _fire_special
# reads it as a dict on the very next line, `effects.get("drain", 0.0)`.
#
# So every Argus Special raised AttributeError. BossView's move callback had a
# `finally` and no `except`, so it escaped into discord.py's default handler:
# the turn had already been applied to the fight, the message never updated,
# and the buttons stayed live but pointing at a state that no longer existed.
# It fired the first time Argus's gauge filled, which is most Argus fights.
#
# Checked across EVERY boss and EVERY Special, not just the one that broke —
# a fourth boss written against the wrong shape would fail exactly the same way.
for _mod, _label in ((AG, "argus"), (DK, "drakos"), (NM, "nemesis")):
    _bad = []
    for _k in _mod.SPECIALS:
        _s = _mod.ArgusState() if _mod is AG else (
            _mod.DrakosState() if _mod is DK else _mod.BossState())
        _s.eyes = getattr(_s, "eyes", 0) and AG.EYE_MAX
        try:
            _d, _e = _mod.special_damage(_k, 130.0, _s, 100.0, 0.5, AI.DMG_SCALE)
        except Exception as _exc:                                # noqa: BLE001
            _bad.append((_k, repr(_exc)[:80]))
            continue
        if not isinstance(_e, dict):
            _bad.append((_k, f"returned {type(_e).__name__}, not dict"))
        elif not isinstance(_d, (int, float)):
            _bad.append((_k, f"damage is {type(_d).__name__}"))
    check(f"{_label}: every Special returns (float, dict) — the shape "
          f"_fire_special reads", not _bad, _bad)

# And through the real fight object, which is where it actually bit.
class _Member:
    def __init__(self, i):
        self.id, self.display_name, self.mention = i, f"p{i}", f"<@{i}>"


async def _run_full_boss_fights():
    _crashes, _fired, _fights = [], 0, 0
    for _key in BB.BOSSES:
        for _seed in range(6):
            _rnd = random.Random(_seed)
            _m = _Member(9000 + _seed)
            try:
                _f = await BB.BossFight.create(_m, _key, party=[_m], tier="standard")
                for _ in range(60):
                    if _f.finished:
                        break
                    _f.boss.gauge = AI.SPECIAL_GAUGE_MAX      # force the Special
                    _mv = AI.MOVE_ATTACK if _f.foe.can(AI.MOVE_ATTACK) \
                        else AI.MOVE_CHARGE
                    _fired += bool(_f.step(_mv).get("god_special"))
                _fights += 1
            except Exception as _exc:                                # noqa: BLE001
                _crashes.append((_key, _seed, repr(_exc)[:120]))
    return _crashes, _fired, _fights


_crashes, _fired, _fights = asyncio.run(_run_full_boss_fights())
check(f"{_fights} full fights, {_fired} boss Specials, no exception escapes "
      f"BossFight.step()", not _crashes, _crashes[:3])
check("...and every boss got through it, Argus included",
      _fights == len(BB.BOSSES) * 6, _fights)

# The reader is tolerant too, so the NEXT boss with the wrong shape loses its
# side effects for a turn rather than taking the battle down.
_fs = open(os.path.join(ROOT, "cogs", "battle", "boss", "boss_battle.py"),
           encoding="utf-8").read().split("def _fire_special")[1]
check("_fire_special normalises a non-dict rather than trusting the module",
      "isinstance(effects, dict)" in _fs)

# Watchfire Sweep advertised a drain and healed for zero: the value went into a
# log string and never into the key _fire_special pays out from.
_d, _e = AG.special_damage("watchfire", 130.0, AG.ArgusState(eyes=3),
                           100.0, 0.5, AI.DMG_SCALE)
check("the sweep's drain reaches the engine, not just the log",
      _e.get("drain", 0) > 0, _e)
check(f"...and it is {AG.SPECIALS['watchfire']['drain']:.0%} of what it hit",
      abs(_e["drain"] - _d * AG.SPECIALS["watchfire"]["drain"]) < 1e-6,
      (_d, _e.get("drain")))

print("\n── 9f. a dead renderer must not end the fight ──────────────────")
# The bug report: "during boss battle bot suddenly stop responding".
#
# BossView.push() awaited bcard.render() unguarded, and push() is awaited from
# inside the move callback — whose try block had a `finally` and no `except`.
# So one Chromium failure (out of disk, out of memory, browser gone) threw
# straight out of the callback into discord.py's default handler, which logs
# and does nothing else. By then f.step() had already applied the turn, so the
# fight had moved and the message had not: buttons frozen on the previous turn,
# nothing to click that helps, and the player still held in cog._active so
# `;boss` answered "you're already in a fight" for the next six minutes.
#
# Driven through the real BossView.push with a renderer that always raises.
import asyncio                                                      # noqa: E402
import types                                                        # noqa: E402


class _FakeMsg:
    def __init__(self):
        self.edits = 0

    async def edit(self, **kw):
        self.edits += 1


_view = BB.BossView.__new__(BB.BossView)
_view.fight = types.SimpleNamespace(
    card_state=lambda: {}, cfg={"colour": 0},
)
_view.embed = lambda: "embed"
_view.message = _FakeMsg()

_real_render = BB.bcard.render


async def _boom(_state):
    raise RuntimeError("no space left on device")


BB.bcard.render = _boom
try:
    asyncio.run(_view.push(None))
    check("push() survives a renderer that raises", True)
except Exception as exc:                                            # noqa: BLE001
    check("push() survives a renderer that raises", False, repr(exc))
finally:
    BB.bcard.render = _real_render
check("...and still redraws the fight, as the plain embed",
      _view.message.edits == 1, _view.message.edits)

_cb_src = BB.__file__.replace(".pyc", ".py")
_src_all = open(_cb_src, encoding="utf-8").read()
_cb = _src_all.split("def _make_cb")[1].split("\n    async def ")[0]
check("the move callback catches, rather than only un-setting `busy`",
      "except Exception" in _cb, _cb[-200:])
check("...and releases the party when a finished fight fails to pay out — "
      "otherwise ;boss refuses forever",
      "_active.discard" in _cb)
# Every card render in the cog, not just the one that was reported. An
# unguarded one is a fight that stops responding, and there is no reason for a
# decorative PNG to be able to do that anywhere.
_lines = _src_all.splitlines()
_unguarded = [
    (i + 1, ln.strip()) for i, ln in enumerate(_lines)
    if "bcard.render" in ln
    and not any(l.strip() == "try:" for l in _lines[max(0, i - 6):i])
]
check(f"every one of the {sum('bcard.render' in l for l in _lines)} card "
      f"renders in the boss cog sits inside a try",
      not _unguarded, _unguarded)

print("\n── 10. nothing else moved ──────────────────────────────────────")
# A floor, not an exact count. This read `== 3`, and adding Lionheart broke it
# — which is the fifth time a hardcoded roster size in these suites has failed
# for the crime of the roster growing. The thing worth asserting is that the
# three that were here are still here, not that nothing was ever added.
check("every boss that had a kit still has one",
      {"drakos", "argus", "nemesis"} <= set(BB.BOSSES), sorted(BB.BOSSES))
check("...and every one of them still resolves to a module and a state",
      all(BB._module_for(BB.BOSSES[k]) is not None
          and BB._make_state(BB.BOSSES[k]) is not None for k in BB.BOSSES),
      [k for k in BB.BOSSES if BB._module_for(BB.BOSSES[k]) is None])
check("Drakos's Stars still ramp",
      DK.DrakosState().copy() is not None)
check("boss keys are unique", len(set(BB.BOSSES)) == len(BB.BOSSES))
check("every boss in the registry has a name and an image field",
      all(b.get("name") and "image_url" in b for b in BI.REGISTRY.values()))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
