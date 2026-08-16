#!/usr/bin/env python3
"""
tools/sim_janus_bahamut.py — the two-form exclusive, and five new ability ops.

Janus Bahamut is the first blade whose MODES CHANGE ITS KIT. Master Diabolos
swaps stats, type and Special between forms; Janus swaps the abilities too, so
`spin_mode.resolve` had to learn a fifth field and `AbilityEngine._rules_for`
had to stop caching compiled rules by blade NAME — two players in a mirror
match can hold the same blade in opposite forms, and a name-only cache key
would have served whichever compiled first to both of them.

Five ops are new. Three exist because the closest thing already in the engine
was subtly the wrong shape:

  ignore_defense_pct   `ignore_defense` zeroes DEF outright AND nullifies the
                       opponent's counter. A 50% pierce that did both would be
                       most of a full pierce, not half of one.
  gain_stability       `lose_stability` guards on `amt > 0` and negates, so it
                       cannot express a heal at all.
  reflect_pct_turns    `reflect_pct` only fires from a rule already running,
                       which cannot say "whoever hits me next turn eats a
                       counter" — the rule would have to know in advance that
                       it was going to be attacked.
  counter_damage_pct   new: the defender scales the counter they land.
  ability_amp          new: a blade amplifies its OWN opted-in numbers for N
                       turns. Opt-in via `"ampable": true` rather than a name
                       whitelist, so an amp can never reach sideways into an
                       unrelated ability the same player is carrying.

The balance question this file exists to answer: v95 removed every one-shot
from the game, and this blade must not put one back.

Run:  python3 tools/sim_janus_bahamut.py
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
from utils import spin_mode as SM                                  # noqa: E402
from cogs.battle.attack_manager import AttackManager               # noqa: E402
from cogs.battle.defense_manager import DefenseManager             # noqa: E402
from cogs.battle.stamina_manager import StaminaManager             # noqa: E402
from cogs.battle.status_manager import StatusManager               # noqa: E402
from cogs.battle import stability_manager as SBM                   # noqa: E402
from cogs.abilities import type_system as TS                       # noqa: E402
from cogs.abilities.ability_engine import AbilityEngine            # noqa: E402
from cogs.core.constants import (                                  # noqa: E402
    MOVE_SPECIAL, MOVE_ATTACK, MOVE_DEFENSE)

BLADES = load_beyblades()
NAME   = "Janus Bahamut"
J      = BLADES.get(NAME)
FOE    = BLADES["Galaxy Pegasus"]
POOL   = 2108           # the level-100 pool v95 balanced against


def form(mode):
    """The blade as it actually fights in `mode`."""
    return SM.resolve({SM.K_MODE: {NAME: mode}}, J)


class Sess:
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


def pinned(fn, *a, **kw):
    """Run with RNG pinned — crits would make every comparison a coin flip."""
    rr, ri = random.random, random.randint
    random.random = lambda: 1.0
    random.randint = lambda x, y: x
    try:
        return fn(*a, **kw)
    finally:
        random.random, random.randint = rr, ri


def special(mode, oblade=None, sess=None):
    """Total damage of one Special, through the real resolver."""
    blade = form(mode)
    s = sess or Sess(blade, oblade or FOE)
    am = AttackManager.__new__(AttackManager)
    am.session = s
    dmg, logs = pinned(am._resolve_special, "m", "o", MOVE_SPECIAL,
                       blade, s.blades["o"], [])
    return int(dmg), logs, s


print("\n── 1. the blade exists and carries both forms ──────────────────")
check(f"{NAME} is in the roster", J is not None)
check("rarity is Exclusive — so it can never spawn",
      J["rarity"] == "Exclusive", J.get("rarity"))
check("...and Exclusive is hard-excluded from spawns",
      "Exclusive" in __import__("cogs.spawn.spawn", fromlist=["x"])._NEVER_SPAWN)
check("it is flagged limited", J.get("limited") is True)
check("it is NOT booster-exclusive — it is not a pack blade",
      not J.get("booster_exclusive"))
check("the id is unique",
      sum(1 for b in BLADES.values() if b.get("id") == J["id"]) == 1, J["id"])
check("two forms", SM.modes(J) == ["Attack", "Defense"], SM.modes(J))
check("Attack form is the default (first)", SM.modes(J)[0] == "Attack")

atk, dfn = form("Attack"), form("Defense")
check("Sunder Form is 144/111/101",
      [atk["stats"][k] for k in ("attack", "defense", "stamina")] == [144, 111, 101],
      atk["stats"])
check("Bulwark Form is 111/144/101",
      [dfn["stats"][k] for k in ("attack", "defense", "stamina")] == [111, 144, 101],
      dfn["stats"])
check("the forms mirror each other exactly",
      atk["stats"]["attack"] == dfn["stats"]["defense"]
      and atk["stats"]["defense"] == dfn["stats"]["attack"])
check("Sunder types Attack, Bulwark types Defense",
      (atk["type"], dfn["type"]) == ("Attack", "Defense"))
check("the top-level record mirrors the default form, so a reader that has "
      "never heard of forms still sees a valid blade",
      J["stats"] == atk["stats"] and J["type"] == "Attack"
      and J["special_move"]["name"] == atk["special_move"]["name"])

print("\n── 2. the forms carry DIFFERENT abilities ──────────────────────")
check("resolve() swaps abilities", "abilities" in SM.MODE_FIELDS)
a_names = [a["name"] for a in atk["abilities"]]
d_names = [a["name"] for a in dfn["abilities"]]
check(f"Sunder: {a_names}", a_names == ["Sundering Edge", "Gatecleaver"], a_names)
check(f"Bulwark: {d_names}",
      d_names == ["Aegis Recoil", "Threshold Judgment"], d_names)
check("the two kits share no ability", not (set(a_names) & set(d_names)))
check("Specials differ",
      atk["special_move"]["name"] != dfn["special_move"]["name"])
check("Gatecleaver is 6 x 30",
      (atk["special_move"]["hits"], atk["special_move"]["damage_per_hit"]) == (6, 30))
check("Threshold Judgment is a single 140",
      (dfn["special_move"]["hits"], dfn["special_move"]["damage_per_hit"]) == (1, 140))

# The cache key regression: both forms share one name.
s = Sess(atk, FOE)
r_atk = s.ability._rules_for(atk)
r_dfn = s.ability._rules_for(dfn)
check("one engine compiles the two forms SEPARATELY — a name-only cache key "
      "would serve whichever came first to both",
      r_atk != r_dfn and len(r_atk) > 0 and len(r_dfn) > 0,
      (len(r_atk), len(r_dfn)))
check("...and a blade with no form still caches by bare name",
      s.ability._rules_for(FOE) is not None)

print("\n── 3. partial pierce: half a pierce, not most of one ───────────")
s = Sess(atk, FOE)
pct = s.ability.pierce_pct("m", atk)
check("Sunder pierces 50%", pct == 50.0, pct)
check("Bulwark pierces nothing", s.ability.pierce_pct("m", dfn) == 0.0)

ostats = {"defense": 120, "attack": 100, "stamina": 100}
out, logs = s.defense_manager.preprocess_defender_stats(
    "m", "o", atk, FOE, dict(ostats))
check("120 DEF becomes 60", out["defense"] == 60, out["defense"])
check("...and it says so in the log",
      any("Defense Pierce" in l and "50" in l for l in logs), logs)
check("the defender's real DEF is NOT zeroed — that is full pierce's job",
      out["defense"] > 0)
# Full pierce nullifies the counter; partial must not.
s2 = Sess(atk, FOE)
am = AttackManager.__new__(AttackManager)
am.session = s2
_d, taken, _m, _l = pinned(
    am.resolve_pair, "m", "o", MOVE_ATTACK, MOVE_DEFENSE, atk, FOE,
    dict(atk["stats"]), dict(FOE["stats"]))
check("a 50%-pierced Defense still counters", taken > 0, taken)

print("\n── 4. counter bonus ────────────────────────────────────────────")
s = Sess(dfn, FOE)
check("Bulwark adds 50% to a counter it lands",
      s.ability.counter_damage_pct("m", dfn) == 50.0,
      s.ability.counter_damage_pct("m", dfn))
check("Sunder adds nothing", s.ability.counter_damage_pct("m", atk) == 0.0)

# Measured through resolve_pair: the FOE attacks, Janus defends.
def counter_taken(defender_blade):
    s = Sess(FOE, defender_blade, my_move=MOVE_ATTACK, their=MOVE_DEFENSE)
    am = AttackManager.__new__(AttackManager)
    am.session = s
    _d, taken, _m, _l = pinned(
        am.resolve_pair, "m", "o", MOVE_ATTACK, MOVE_DEFENSE,
        FOE, defender_blade, dict(FOE["stats"]), dict(defender_blade["stats"]))
    return taken


plain = counter_taken(BLADES["Galaxy Pegasus"])
bul   = counter_taken(dfn)
check(f"Bulwark's counter beats a plain blade's ({plain} -> {bul})",
      bul > plain, (plain, bul))

print("\n── 5. the Specials, and what they leave behind ─────────────────")
dmg_a, logs_a, sa = special("Attack")
check(f"Gatecleaver deals {dmg_a} ({dmg_a / POOL * 100:.0f}% of a full pool)",
      dmg_a > 0)
check("...drains half a point of stamina",
      any("drained" in l and "0.5" in l for l in logs_a), logs_a)
# The stored entry is [mult, turns+1, granted_round]: the +1 pays for the
# granting round, which is dormant, and the round number is what makes it so.
check("...and arms a x2 amp for the 5 turns that follow",
      sa.ability.ability_amp.get("m") == [2.0, 6, 1],
      sa.ability.ability_amp)
check("...dormant on the turn it was cast",
      sa.ability.amp_mult("m") == 1.0)
check("...but no counter stance — that is the other form",
      not sa.ability.reflect_windows)

dmg_d, logs_d, sd = special("Defense")
check(f"Threshold Judgment deals {dmg_d} in one hit", dmg_d > 0)
check("...opens a 150% counter stance for 1 turn",
      sd.ability.reflect_windows.get("m") == [150.0, 1],
      sd.ability.reflect_windows)
check("...and arms a x2 amp for the 4 turns that follow",
      sd.ability.ability_amp.get("m") == [2.0, 5, 1], sd.ability.ability_amp)
check("...also dormant on the turn it was cast",
      sd.ability.amp_mult("m") == 1.0)
check("...and banks Defence and stamina on the way",
      any("Defense" in l for l in logs_d) and any("stamina" in l for l in logs_d),
      logs_d)

print("\n── 6. the counter stance answers whoever walks in ──────────────")
s = Sess(dfn, FOE)
pinned(s.ability.apply, "m", "o", dfn, FOE, MOVE_SPECIAL, "win", 0, 0)
check("the stance is up", s.ability.reflect_windows.get("m", [0, 0])[1] == 1)
_dealt, taken = pinned(s.ability._fire_defensive, "m", "o", dfn,
                       MOVE_ATTACK, "win", 100, 0, [])
check("a 100-damage attack into it comes back as 150", taken == 150, taken)
_d2, taken2 = pinned(s.ability._fire_defensive, "m", "o", dfn,
                     MOVE_ATTACK, "win", 0, 0, [])
check("an attack that deals nothing reflects nothing", taken2 == 0, taken2)

# It expires. A stance that never drops is a passive, not a Special.
s.ability.tick_dmg_amps()
check("one round later the stance has dropped", "m" not in s.ability.reflect_windows)
_d3, taken3 = pinned(s.ability._fire_defensive, "m", "o", dfn,
                     MOVE_ATTACK, "win", 100, 0, [])
check("...and the next hit is not answered", taken3 == 0, taken3)

print("\n── 7. the amp doubles the kit, then gives it back ──────────────")
s = Sess(atk, FOE)
check("un-amped, the multiplier is 1.0", s.ability.amp_mult("m") == 1.0)
check("un-amped pierce is 50%", s.ability.pierce_pct("m", atk) == 50.0)

s.ability.ability_amp["m"] = [2.0, 5]
check("amped, the multiplier is 2.0", s.ability.amp_mult("m") == 2.0)
check("amped pierce is 100% — the full reading of 'stats double'",
      s.ability.pierce_pct("m", atk) == 100.0, s.ability.pierce_pct("m", atk))
check("...and it is CLAMPED at 100, not 150",
      s.ability.pierce_pct("m", atk) <= 100.0)

sd2 = Sess(dfn, FOE)
sd2.ability.ability_amp["m"] = [2.0, 4]
check("amped counter bonus is 100%",
      sd2.ability.counter_damage_pct("m", dfn) == 100.0,
      sd2.ability.counter_damage_pct("m", dfn))

# THE TIMING BUG THIS SECTION EXISTS FOR. "Next 5 turns" means next — but
# `on_special` fires on the FIRST hit of the Special, so without a dormant
# granting round the remaining five hits of the very move that grants the amp
# are already amplified. The move paid its own reward, and a 6-hit Special
# read 230 when its honest value was 210.
s = Sess(atk, FOE)
am = AttackManager.__new__(AttackManager)
am.session = s
d_grant, glogs = pinned(am._resolve_special, "m", "o", MOVE_SPECIAL,
                        atk, FOE, [])
check(f"the Special that GRANTS the amp is not amplified by it ({d_grant})",
      all("15%" in l for l in glogs if "Sundering Edge" in l),
      [l for l in glogs if "Sundering Edge" in l][:3])
check("...so it is a clean 6 x (30 + 15%)", d_grant == 210, d_grant)
check("the amp is armed but dormant this round",
      s.ability.ability_amp["m"][0] == 2.0 and s.ability.amp_mult("m") == 1.0,
      s.ability.ability_amp)

s.ability.tick_dmg_amps()
s.round = 2
check("next round it goes live", s.ability.amp_mult("m") == 2.0)
d_amped, alogs = pinned(am._resolve_special, "m", "o", MOVE_SPECIAL,
                        atk, FOE, [])
check(f"and NOW Gatecleaver is amplified ({d_grant} -> {d_amped})",
      d_amped > d_grant, (d_grant, d_amped))
check("...every hit of it, not just the first",
      all("30%" in l for l in alogs if "Sundering Edge" in l),
      [l for l in alogs if "Sundering Edge" in l][:3])
check("re-casting while amped does not switch your own amp off — stamping "
      "the grant round on a refresh would re-arm the dormancy",
      s.ability.amp_mult("m") == 2.0, s.ability.ability_amp)

# Exactly five LIVE turns, counted through real rounds.
s6 = Sess(atk, FOE)
pinned(s6.ability.apply, "m", "o", atk, FOE, MOVE_SPECIAL, "win", 0, 0)
live = []
for rnd in range(1, 10):
    s6.round = rnd
    if s6.ability.amp_mult("m") > 1.0:
        live.append(rnd)
    s6.ability.tick_dmg_amps()
check(f"the amp is live for exactly 5 turns, rounds {live}",
      live == [2, 3, 4, 5, 6], live)
check("...then it is gone", "m" not in s6.ability.ability_amp)
check("...leaving pierce back at 50%", s6.ability.pierce_pct("m", atk) == 50.0)

# Bulwark's window is 4, not 5.
s7 = Sess(dfn, FOE)
pinned(s7.ability.apply, "m", "o", dfn, FOE, MOVE_SPECIAL, "win", 0, 0)
live = []
for rnd in range(1, 10):
    s7.round = rnd
    if s7.ability.amp_mult("m") > 1.0:
        live.append(rnd)
    s7.ability.tick_dmg_amps()
check(f"Bulwark's amp runs 4 turns, rounds {live}", live == [2, 3, 4, 5], live)

# Re-firing refreshes rather than stacking — 4x is not what "double" means.
s5 = Sess(atk, FOE)
for _ in range(3):
    pinned(s5.ability.apply, "m", "o", atk, FOE, MOVE_SPECIAL, "win", 0, 0)
check("three Specials in a row do not stack to x8",
      s5.ability.ability_amp["m"][0] == 2.0, s5.ability.ability_amp)

print("\n── 8. `ampable` is opt-in, so an amp cannot leak sideways ──────")
s = Sess(atk, FOE)
s.ability.ability_amp["m"] = [2.0, 5]
check("an op WITHOUT the flag is untouched",
      s.ability._amped("m", {"op": "bonus_damage", "value": 40}, 40) == 40)
check("an op WITH the flag doubles",
      s.ability._amped("m", {"op": "bonus_damage", "value": 40,
                             "ampable": True}, 40) == 80)
check("a player with no window open is never scaled",
      Sess(atk, FOE).ability._amped(
          "m", {"op": "x", "value": 40, "ampable": True}, 40) == 40)
# Nothing else in the roster opts in, so no existing blade changes behaviour.
leaked = [n for n, b in BLADES.items() if n != NAME
          and "ampable" in __import__("json").dumps(b)]
check("no other blade in the roster uses `ampable`", not leaked, leaked)

print("\n── 9. stability: it heals, and it cannot run away ──────────────")
s = Sess(atk, FOE)
stab = s.stability_manager
start = stab.stability["m"]
stab.stability["m"] = start - 40
pinned(s.ability.apply, "m", "o", atk, FOE, MOVE_ATTACK, "win", 50, 0)
check(f"landing an attack steadies the blade ({start - 40} -> "
      f"{stab.stability['m']})", stab.stability["m"] > start - 40)
check("...by 5", stab.stability["m"] == start - 35, stab.stability["m"])

# The per-hit heal on a 6-hit Special is the number worth watching.
s = Sess(atk, FOE)
s.stability_manager.stability["m"] = 10
_d, _l, s_after = special("Attack", sess=s)
healed = s.stability_manager.stability["m"]
cap = s.stability_manager.max.get("m", 100)
check(f"a 6-hit Gatecleaver from 10 stability heals to {healed} "
      f"(cap {cap})", healed > 10)
check("...and NEVER exceeds the blade's own maximum — the manager clamps, "
      "which is what keeps 6 hits x 5 (x2 amped) from running away",
      healed <= cap, (healed, cap))

print("\n── 10. it does not put a one-shot back in the game ─────────────")
# v95 removed every one-shot. The whole point of measuring here is that a new
# exclusive is exactly how one comes back.
worst = 0
for mode in ("Attack", "Defense"):
    for amped in (False, True):
        s = Sess(form(mode), FOE)
        if amped:
            s.ability.ability_amp["m"] = [2.0, 5]
        d, _l, _s = special(mode, sess=s)
        worst = max(worst, d)
        check(f"{mode:<8} {'amped' if amped else 'base ':<5} Special: "
              f"{d:>4} ({d / POOL * 100:>5.1f}% of the pool) — not a one-shot",
              d < POOL, d)
check(f"the worst case across both forms is {worst}, well under {POOL}",
      worst < POOL * 0.75, worst)

# And against the roster's toughest blade, not just a mid one.
tank = max(BLADES.values(), key=lambda b: (b.get("stats") or {}).get("defense", 0))
s = Sess(form("Attack"), tank)
s.ability.ability_amp["m"] = [2.0, 5]
d, _l, _s = special("Attack", oblade=tank, sess=s)
check(f"even amped into {tank['name']} (DEF {tank['stats']['defense']}) it is "
      f"{d}, not a kill", d < POOL, d)

print("\n── 11. the ;info form picker ───────────────────────────────────")
psrc = open(os.path.join(ROOT, "cogs", "economy", "profile.py"),
            encoding="utf-8").read()
check("`;info` attaches the picker for any two-form blade",
      "SpinModeView(ctx.author, blade, self) if is_dual(blade)" in psrc)
check("...built from modes(blade), so it does not hardcode Right/Left",
      "for mode in modes(blade)" in psrc)
check("...and labels each button from the blade's own data",
      "label=label(blade, mode)" in psrc)
check("the labels are set", SM.label(J, "Attack") == "⚔️ Sunder Form"
      and SM.label(J, "Defense") == "🛡️ Bulwark Form",
      (SM.label(J, "Attack"), SM.label(J, "Defense")))
check("an unknown stored mode falls back to a real one, never crashes",
      SM.chosen({SM.K_MODE: {NAME: "Sideways"}}, J) == "Attack")
check("no stored choice means the default form",
      SM.chosen({}, J) == "Attack")
check("a stored choice is honoured",
      SM.chosen({SM.K_MODE: {NAME: "Defense"}}, J) == "Defense")

print("\n── 12. nothing else moved ──────────────────────────────────────")
check("the roster grew by exactly one", len(BLADES) == 92, len(BLADES))
check("every blade still has a name and rarity",
      all("name" in b and "rarity" in b for b in BLADES.values()))
check("Master Diabolos still resolves both its spin modes",
      SM.modes(BLADES["Master Diabolos"]) == ["Right", "Left"],
      SM.modes(BLADES["Master Diabolos"]))
md = SM.resolve({SM.K_MODE: {"Master Diabolos": "Left"}},
                BLADES["Master Diabolos"])
check("...and its Left form still swaps stats and Special",
      md["type"] == "Defense" and md["special_move"]["name"] == "Master Upper")
check("a non-dual blade is returned untouched, not copied",
      SM.resolve({}, FOE) is FOE)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
