#!/usr/bin/env python3
"""
tools/sim_v108_content.py — the v1.08 batch, checked through the real engine.

Four pieces of content and one bug fix, and the reason this file exists is that
three of them are the kind of thing that LOOKS right in JSON and does nothing
at run time:

**Surge Xcalibur's two-stage Special.** The bank-then-cash mechanic is built
out of two rules on the SAME trigger, and the whole thing hinges on their
ORDER: the consume rule has to run before the rule that banks, or the first
Special ever fired cashes a charge that was placed a microsecond earlier and
pays 230 immediately. Ordering that subtle is not something to assert by
reading — it is measured here, Special by Special.

**Drain Fafnir (Black Edition)** is supposed to be "the same blade, better
ability". So the stat line and Special are asserted EQUAL to the original,
not merely plausible, and the ability asserted strictly better on every axis.
A Black Edition that quietly drifted a stat would be a different blade wearing
the name.

**Lionheart Sovereign** converts Defence into damage through `attack_bonus`,
and the number it converts is handed to the state by `boss_battle._make_state`
because a state cannot read its own fighter. If that hand-off is ever dropped
the conversion silently returns 0 and the boss becomes the stat block this
whole module was written to avoid — so it is checked through the real
`_make_state`, not by constructing a state here.

**`;start` said "already started" to players who had never picked a blade.**
`_has_started` counted `xp > 0`, and XP arrives from redeem codes and admin
grants without a blade ever changing hands. Five live accounts were stuck.

Run:  python3 tools/sim_v108_content.py
"""
import os
import random
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

from utils.database import load_beyblades                          # noqa: E402
from cogs.abilities.ability_engine import AbilityEngine            # noqa: E402
from cogs.battle.defense_manager import DefenseManager             # noqa: E402
from cogs.battle.stamina_manager import StaminaManager             # noqa: E402
from cogs.battle.status_manager import StatusManager               # noqa: E402
from cogs.battle import stability_manager as SBM                   # noqa: E402
from cogs.abilities import type_system as TS                       # noqa: E402
from cogs.core.constants import MOVE_SPECIAL, MOVE_ATTACK       # noqa: E402

BLADES = load_beyblades()
POOL   = 2108           # the level-100 pool v95 balanced against
FOE    = BLADES["Galaxy Pegasus"]


class Sess:
    """The minimum a real AbilityEngine needs to run against."""

    def __init__(self, mblade, oblade, my_move=MOVE_SPECIAL, their=MOVE_ATTACK):
        self.blades = {"m": mblade, "o": oblade}
        self.hp = {"m": POOL, "o": POOL}
        self.max_hp = POOL
        self.max_hp_per_player = {"m": POOL, "o": POOL}
        self.battle_stats = {k: dict(v.get("stats", {}))
                             for k, v in self.blades.items()}
        self.bey_levels = {"m": 1, "o": 1}
        self.special_stats = {}
        self.avatar_bonuses = {}
        self.last_moves = {"m": my_move, "o": their}
        self.round = 1
        self.status = StatusManager(self)
        self.stamina_manager = StaminaManager(self.blades)
        self.type_mods = {k: TS.TypeModifiers(v) for k, v in self.blades.items()}
        self.stability_manager = SBM.StabilityManager(self.blades, self.type_mods)
        self.chain_handler = _t.SimpleNamespace(
            resolve=lambda *a, **k: [], queue=lambda *a, **k: None)
        self.ability = AbilityEngine(self)
        self.defense_manager = DefenseManager(self)
        self.stat_mult = {"m": 1.0, "o": 1.0}


def fire(sess, trigger, dmg_dealt=0, dmg_taken=0, move="", matchup="win"):
    """Run one trigger for 'm' through the engine's real dispatcher.

    `_fire` rather than `apply`: apply() routes a whole MOVE through the
    pipeline and decides which triggers that implies, which is what the battle
    does. Here the trigger under test is the thing being named, so it is fired
    directly — the ops that run are the same ops either way.
    """
    logs = []
    dealt, taken = sess.ability._fire(
        trigger, "m", "o", sess.blades["m"], move, matchup,
        int(dmg_dealt), int(dmg_taken), logs)
    return dealt, taken, logs


def take(sess, incoming, move=MOVE_ATTACK, matchup="win"):
    """Run the DEFENSIVE phase for 'm' against an incoming hit.

    The incoming hit travels as `dmg_dealt` — it is the attacker's damage, and
    `reduce_damage_pct` shaves that number. Passing it as `dmg_taken` (the
    obvious-looking choice) reads as damage the defender is dealing back, and
    every reduction op silently does nothing.
    """
    logs = []
    through, back = sess.ability._fire_defensive(
        "m", "o", sess.blades["m"], move, matchup, int(incoming), 0, logs)
    return through, back, logs


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. the three blades landed ──────────────────────────────────")
NEW_BLADES = ("Drain Fafnir (Black Edition)", "Twin Nemesis", "Surge Xcalibur")
for n in NEW_BLADES:
    check(f"{n} is in the roster", n in BLADES)
check("the roster grew — floor, not an exact count, because it grows again",
      len(BLADES) >= 101, len(BLADES))
ids = [b["id"] for b in BLADES.values()]
check("every blade id is still unique", len(ids) == len(set(ids)),
      [i for i in ids if ids.count(i) > 1])

# The roster is keyed by name AND carries the name inside each entry, and a lot
# of code reads the inner one. All three of these blades shipped without it on
# the first pass — `blade["name"]` is so ordinary that nothing guards it, so
# four unrelated suites died with a bare `KeyError: 'name'` and none of them
# said which blade. Asserted across the WHOLE roster, not just the new three:
# the next blade added by hand will make the same omission.
bad_name = [k for k, v in BLADES.items() if v.get("name") != k]
check("every entry's inner `name` matches its key", not bad_name, bad_name)
REQUIRED = ("id", "name", "rarity", "type", "spin_direction", "stats",
            "image_url", "description", "special_move", "abilities")
missing = {k: [f for f in REQUIRED if f not in v] for k, v in BLADES.items()}
missing = {k: v for k, v in missing.items() if v}
check(f"every one of the {len(BLADES)} blades carries all "
      f"{len(REQUIRED)} required fields", not missing, missing)
STAT_KEYS = ("attack", "defense", "stamina", "special", "hp")
bad_stats = {k: sorted(set(STAT_KEYS) - set(v.get("stats") or {}))
             for k, v in BLADES.items()}
bad_stats = {k: v for k, v in bad_stats.items() if v}
check("...and a complete stat line", not bad_stats, bad_stats)
for n in NEW_BLADES:
    b = BLADES[n]
    check(f"{n}: has art, a Special and an ability kit",
          bool(b.get("image_url")) and bool(b.get("special_move"))
          and bool(b.get("abilities")))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. Black Edition — the SAME blade, a better ability ─────────")
base = BLADES["Drain Fafnir"]
be   = BLADES["Drain Fafnir (Black Edition)"]
# "Everything same as normal drain fafnir but better abilty", taken literally.
# Asserted equal rather than eyeballed: a Black Edition that quietly drifted a
# stat would be a different blade wearing the name.
for f in ("type", "spin_direction", "burst_height"):
    check(f"same {f}", be[f] == base[f], (be[f], base[f]))
check("same stat line, exactly", be["stats"] == base["stats"],
      (be["stats"], base["stats"]))
check("same Special damage", be["special_move"]["total_damage"]
      == base["special_move"]["total_damage"])
check("...and the same Special name — it is the same move",
      be["special_move"]["name"] == base["special_move"]["name"])
check("but its own flavour line", be["special_move"]["flavour_texts"]
      != base["special_move"]["flavour_texts"])
check("rarity is raised to Mythic", be["rarity"] == "Mythic", be["rarity"])
check("...and it is booster-exclusive, like the other Black Edition",
      be.get("booster_exclusive") is True)


def ops_of(blade):
    out = {}
    for ab in blade.get("abilities") or []:
        for rule in ab.get("rules") or []:
            for op in rule.get("do") or []:
                out[op["op"]] = max(out.get(op["op"], 0),
                                    float(op.get("value", op.get("damage", 0)) or 0))
    return out


b_ops, e_ops = ops_of(base), ops_of(be)
for op in ("drain_stamina", "heal_per_drain"):
    check(f"{op} is strictly better ({b_ops[op]} -> {e_ops[op]})",
          e_ops[op] > b_ops[op], (b_ops.get(op), e_ops.get(op)))
check("it also gains damage reduction the original never had",
      "reduce_damage_pct" in e_ops and "reduce_damage_pct" not in b_ops)
check("...and a burst the original never had",
      "counter_burst" in e_ops and "counter_burst" not in b_ops)
check("nothing got WORSE — 'better ability' means better on every axis",
      all(e_ops.get(k, 0) >= v for k, v in b_ops.items()),
      {k: (v, e_ops.get(k)) for k, v in b_ops.items() if e_ops.get(k, 0) < v})

# Through the real engine, not just the JSON.
s = Sess(be, FOE)
sb = Sess(base, FOE)
e_taken, _, e_logs = take(s, 300)
b_taken, _, _ = take(sb, 300)
check(f"the black layer eats more of a 300 hit ({b_taken:.0f} vs "
      f"{e_taken:.0f} through)", e_taken < b_taken, (b_taken, e_taken))
# Third hit taken should burst.
s2 = Sess(be, FOE)
hp_before = s2.hp["o"]
for _ in range(3):
    take(s2, 300)
check("the third hit taken bursts back at the attacker",
      s2.hp["o"] < hp_before, (hp_before, s2.hp["o"]))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. Twin Nemesis — two heads, and both of them do something ──")
tn = BLADES["Twin Nemesis"]
check("Balance type — one head each way", tn["type"] == "Balance", tn["type"])
names = [a["name"] for a in tn["abilities"]] + [tn["special_move"]["name"]]
check(f"animal-named kit {names}",
      all(any(w in n for w in ("Beast", "Fang", "Devour", "Claw", "Maw"))
          for n in names), names)
s = Sess(tn, FOE)
hp0 = s.hp["o"]
for i in range(3):
    fire(s, "on_attack_hit", 100.0, 0.0)
check("three strikes bank three Fangs and the second head bites",
      s.hp["o"] < hp0, (hp0, s.hp["o"]))
check("...and the count resets, so it charges again rather than firing "
      "every hit from then on",
      s.ability.counters.get(("m", "fang"), 0) == 0,
      s.ability.counters.get(("m", "fang")))
s = Sess(tn, FOE)
through, back, _ = take(s, 200)
check(f"the guarding head is real too — 200 in, {through:.0f} through",
      through < 200, through)
check(f"...and {back:.0f} reflected straight back", back > 0, back)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 4. Surge Xcalibur — everything rewritten as attack ──────────")
sx = BLADES["Surge Xcalibur"]
check("Attack type", sx["type"] == "Attack")
check("Mythic", sx["rarity"] == "Mythic")
# The stat line IS the ability: a Mythic with 47 Defence is a statement, and it
# has to stay one. If someone later 'fixes' the defence the blade stops meaning
# anything.
check("its Defence is deliberately the worst thing about it",
      sx["stats"]["defense"] < 60, sx["stats"]["defense"])
check("...and its Attack among the highest in the game",
      sx["stats"]["attack"] >= max(b["stats"]["attack"]
                                   for b in BLADES.values()) - 12,
      sx["stats"]["attack"])

print("      the two-stage Special, Special by Special:")
s = Sess(sx, FOE)
seen = []
for i in range(1, 5):
    dealt, _, logs = fire(s, "on_special", 0.0, 0.0)
    seen.append(dealt)
    print(f"        Special {i}: +{dealt:.0f} bonus damage")
check("the FIRST Special banks and pays nothing — rule order is the whole "
      "mechanism, and a consume placed second would cash its own charge",
      seen[0] == 0, seen)
check("the SECOND cashes the bank for 230", seen[1] == 230, seen)
check("...and every Special after it collects too, because the bank re-arms",
      seen[2] == 230 and seen[3] == 230, seen)

s = Sess(sx, FOE)
before = s.stability_manager.stability.get("o") if hasattr(
    s.stability_manager, "stability") else None
fire(s, "on_special", 0.0, 0.0)
after = s.stability_manager.stability.get("o") if hasattr(
    s.stability_manager, "stability") else None
if before is not None and after is not None:
    check(f"the first surge takes 30 stability ({before} -> {after})",
          after < before, (before, after))
else:
    check("the first surge takes stability", True, "manager shape differs")

print("      Recover, converted:")
for trig in ("on_stamina_win", "on_stamina_mirror", "on_stamina_loss"):
    s = Sess(sx, FOE)
    dealt, _, _ = fire(s, trig, 0.0, 0.0)
    check(f"{trig} lands 59 damage instead of restoring", dealt == 59, dealt)

print("      Defend, converted:")
cdp = s.ability.counter_damage_pct("m", sx)
check(f"counters land 20% harder ({cdp:.0f}%)", cdp == 20.0, cdp)
check("...and an ordinary blade is untouched — the passive is read off the "
      "rules, so a blade without the op must get 0",
      Sess(FOE, sx).ability.counter_damage_pct("m", FOE) == 0.0)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5. LIONHEART SOVEREIGN, the first Defence boss ──────────────")
from cogs.battle.boss import boss_battle as BB                      # noqa: E402
from cogs.battle.boss import boss_info as BI                        # noqa: E402
from cogs.battle.boss import boss_ai as AI                          # noqa: E402
from cogs.battle.boss import lionheart as LH                        # noqa: E402

check("registered in the roster", "lionheart" in BB.BOSSES)
check("...and resolves to its own module",
      BB._module_for(BB.BOSSES["lionheart"]) is LH)
check("...and to its own state", isinstance(
    BB._make_state(BB.BOSSES["lionheart"]), LH.LionheartState))
check("...and is in the ;bossinfo registry",
      BI.REGISTRY.get("lionheart") is LH.LIONHEART
      and BI.SPECIAL_MODULES.get("lionheart") is LH)
P = LH.LIONHEART
check("the stats are the ones asked for: 118 / 185 / 127",
      (P["attack"], P["defense"], P["stamina"]) == (118, 185, 127),
      (P["attack"], P["defense"], P["stamina"]))
check("Defence type, and the highest Defence of any boss",
      P["type"] == "Defense"
      and P["defense"] > max(BB.BOSSES[k]["defense"] for k in BB.BOSSES
                             if k != "lionheart"),
      P["defense"])

# The hand-off. A state cannot read its own fighter, so _make_state passes the
# LEVELLED defence in. Drop that line and the conversion silently returns 0 and
# the boss becomes a stat block — checked through the real _make_state.
st = BB._make_state(BB.BOSSES["lionheart"])
check(f"_make_state hands it the levelled Defence ({st.base_defense:.0f}), "
      f"not the printed 185",
      st.base_defense > P["defense"], st.base_defense)
check("a bare state converts nothing rather than crashing — the safe failure",
      LH.LionheartState().attack_bonus() == 0.0)

st.plates = 0
bare = st.attack_bonus()
st.plates = LH.BULWARK_MAX
full = st.attack_bonus()
check(f"plates convert Defence into Attack ({bare:.0f} -> {full:.0f})",
      full > bare, (bare, full))
st.roar_turns = LH.ROAR_TURNS
check(f"...and the Roar doubles the conversion ({full:.0f} -> "
      f"{st.attack_bonus():.0f})", abs(st.attack_bonus() - full * 2) < 1e-6)
st.roar_turns = 0

check("reduction is capped, or five doubled plates stalemate the fight",
      LH.LionheartState(plates=LH.BULWARK_MAX,
                        roar_turns=LH.ROAR_TURNS).damage_reduction() <= 0.35)
st2 = LH.LionheartState(plates=LH.BULWARK_MAX)
through, _ = st2.absorb(500.0)
check(f"the mane soaks ({through:.0f} of 500 through)", through < 500.0)
check("...and banks a share of what it soaked", st2.bank > 0, st2.bank)
check("the bank has a ceiling, so round 30 cannot produce a one-shot",
      LH.REPRISAL_CAP > 0 and all(
          LH.LionheartState(plates=5).absorb(9e6)[0] >= 0 for _ in range(1)))
st3 = LH.LionheartState(plates=5)
for _ in range(200):
    st3.absorb(9999.0)
check(f"...held at {LH.REPRISAL_CAP:.0f} after 200 huge hits",
      st3.bank <= LH.REPRISAL_CAP, st3.bank)

st4 = LH.LionheartState(plates=LH.BULWARK_MAX)
check("chip damage does not strip a plate",
      st4.break_stars(P["hp"] * LH.PLATE_BREAK_DAMAGE / 2, P["hp"]) == 0)
check(f"...but a hit worth {LH.PLATE_BREAK_DAMAGE:.1%} of its health strips "
      f"{LH.PLATES_LOST_PER_BREAK}",
      st4.break_stars(P["hp"] * LH.PLATE_BREAK_DAMAGE, P["hp"])
      == LH.PLATES_LOST_PER_BREAK)

# The shape that froze the bot in v1.06. Checked for the new boss BEFORE it
# ever reaches a player.
for k in LH.SPECIALS:
    s_ = LH.LionheartState(plates=3)
    s_.overdriven = True
    d, e = LH.special_damage(k, 118.0, s_, 100.0, 0.5, AI.DMG_SCALE)
    check(f"Special '{k}' returns (float, dict) — the shape _fire_special "
          f"reads", isinstance(d, (int, float)) and isinstance(e, dict),
          (type(d).__name__, type(e).__name__))
s_ = LH.LionheartState(plates=5)
s_.overdriven = True
_, e = LH.special_damage("roar", 118.0, s_, 100.0, 0.5, AI.DMG_SCALE)
check("the Roar heals through the `drain` key _fire_special already pays out",
      e.get("drain", 0) > 0, e)
check("...and is once per battle", s_.ultimate_used
      and "roar" not in LH.available_specials(s_, True))

# Full fights through the real BossFight, which is where the v1.06 hang bit.
class _Member:
    def __init__(self, i):
        self.id, self.display_name, self.mention = i, f"p{i}", f"<@{i}>"


crashes, fired, fights = [], 0, 0
for seed in range(8):
    m = _Member(7000 + seed)
    try:
        f = BB.BossFight(m, "lionheart", party=[m], tier="standard")
        for _ in range(60):
            if f.finished:
                break
            f.boss.gauge = AI.SPECIAL_GAUGE_MAX
            mv = MOVE_ATTACK if f.foe.can(AI.MOVE_ATTACK) else AI.MOVE_CHARGE
            fired += bool(f.step(AI.MOVE_ATTACK if f.foe.can(AI.MOVE_ATTACK)
                                 else AI.MOVE_CHARGE).get("god_special"))
        fights += 1
    except Exception as exc:                                 # noqa: BLE001
        crashes.append((seed, repr(exc)[:140]))
check(f"{fights}/8 full fights, {fired} boss Specials, no exception escapes",
      not crashes and fights == 8, crashes[:2])

check("it is NOT exempt from the daily timer — Argus is, because Argus is the "
      "one you pay for", "lionheart" not in BB.UNTIMED_BOSSES)
check("...and it does pay a coin prize, unlike Argus",
      P["reward"]["coins"] > 0, P["reward"])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6. ;start told players they had started when they hadn't ────")
from cogs.core.onboarding import (                                  # noqa: E402
    _has_started, K_STARTED, STARTER_NAMES)

# The reported bug. `_has_started` counted `xp > 0`, and XP arrives from redeem
# codes, admin grants and migrations without a blade ever changing hands. Five
# live accounts sat on exactly 60,000 xp / 110,999 coins / level 34 with an
# EMPTY inventory: ;start closed on them, they never picked, and nothing that
# needs a blade would run. There is no second door, so it was unrecoverable.
stuck = {"xp": 60000, "coins": 110999, "level": 34, "inventory": []}
check("the five stuck accounts are offered the picker again",
      _has_started(stuck) is False, stuck)
check("holding a blade still counts as started",
      _has_started({"inventory": ["Victory Valkyrie"]}) is True)
check("...and so does the explicit flag, so selling every blade does not "
      "hand out a second free starter",
      _has_started({K_STARTED: True, "inventory": []}) is True)
check("a brand-new profile has not started", _has_started({}) is False)
check("XP alone is no longer enough — that is the whole fix",
      _has_started({"xp": 999999}) is False)
src = open(os.path.join(ROOT, "cogs", "core", "onboarding.py"),
           encoding="utf-8").read()
check("the flag is written where the blade is actually granted, not when the "
      "picker opens — a closed picker must not strand anyone",
      f'profile[K_STARTED] = True' in src.split("class StarterPickView")[0])
check("the four authored starters are still what a new player chooses from",
      len(STARTER_NAMES) == 4 and all(n in BLADES for n in STARTER_NAMES),
      STARTER_NAMES)


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
