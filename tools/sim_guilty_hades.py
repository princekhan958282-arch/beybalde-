#!/usr/bin/env python3
"""
tools/sim_guilty_hades.py — Guilty Longinus rebuilt, Dread Hades nerfed.

Dread Hades
-----------
Soul Reaper stole 20 stamina. Battle stamina bars run 11-30 and are 15-17 for
almost every blade below level 100, so ONE Special emptied the opponent's bar
completely — they could not attack, could not Special, could do nothing but
take the follow-up. A Special costs 4.4 and an Attack 2.2, so the drain is now
4: roughly one Special or two Attacks taken off them, which is a real bite and
not a lockout. The percentages are measured here rather than asserted from the
raw number, because "20" only reads as broken once you know the bar size.

Guilty Longinus
---------------
New ability and new Special. Written in the modern `rules` DSL, which also
retires a bug: the old Guilty Verdict declared `streak_threshold: 3` to gate
CONDEMNED MODE, and that gate did not survive `legacy_convert` — the converted
rule fired `guaranteed_crit` on EVERY winning attack, so the blade had
permanent guaranteed crits from the first hit instead of every third.

Guilty Upper is Lui Shirosagijo's blade doing what it does in the anime: it
does not out-last anyone, it winds up and launches them out of the stadium. So
three landed hits build it, the third lands the uppercut, and the uppercut
knocks the opponent off balance rather than simply hitting harder. Left-spin
contact against an opposite-spinning blade — the destructive matchup the whole
Longinus line is built on — hits harder still.

The trap this suite exists to catch
-----------------------------------
`_run_ops` did not read `op["_if"]`. `legacy_convert` has emitted that gate
since it was written, and the engine ignored it — so a gated op fired
unconditionally. Guilty Upper is the first blade in the roster to use it, and
the first draft launched the opponent on EVERY hit instead of every third.
Both halves are asserted below: the gate is honoured, and it is evaluated
before `counter_burst` resets the counter it reads.

Run:  python3 tools/sim_guilty_hades.py
"""
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


from cogs.abilities.ability_engine import AbilityEngine        # noqa: E402
from cogs.battle.stamina_manager import (STAMINA_COST,         # noqa: E402
                                         max_stamina_for)
from cogs.core.constants import MOVE_ATTACK, MOVE_SPECIAL      # noqa: E402
from utils.database import get_beyblade                        # noqa: E402

GL = get_beyblade("Guilty Longinus")
DH = get_beyblade("Dread Hades")
# Longinus spins Left; this is the same blade turned Right, to isolate the
# opposite-spin clause without dragging another blade's stats in.
RIGHT = dict(GL, spin_direction="Right")


class FakeStability:
    def __init__(self):
        self.v = {"p": 100.0, "e": 100.0}

    def _apply(self, key, amount, *a, **kw):
        self.v[key] = self.v.get(key, 100.0) + amount
        return []

    def add(self, key, amount, *a, **kw):
        return self._apply(key, amount)


class FakeStatus:
    def __init__(self):
        self.shields = {}
        self.special_boost_flat = {}
        self.buffs = {}

    def add_shield(self, key, amount):
        self.shields[key] = self.shields.get(key, 0) + amount

    def add_buff(self, key, stat, amount, rounds):
        self.buffs.setdefault(key, []).append((stat, amount, rounds))

    def get_buff_bonus(self, key, stat):
        return sum(a for s, a, _ in self.buffs.get(key, []) if s == stat)

    def add_dmg_amp(self, *a, **kw):
        pass

    def get_dmg_amp(self, key):
        return 0.0

    def set_duration(self, *a, **kw):
        pass

    def apply_burn(self, *a, **kw):
        return []


class FakeStamina:
    def __init__(self, bar):
        self.max_stamina = {"p": bar, "e": bar}
        self.stamina = {"p": bar, "e": bar}
        self.drain_reduction = {}


class FakeSession:
    def __init__(self, mine, enemy, bar=16.0):
        self.status = FakeStatus()
        self.stamina_manager = FakeStamina(bar)
        self.stability_manager = FakeStability()
        self.blades = {"p": mine, "e": enemy}
        self.hp = {"p": 900, "e": 1000}
        self.max_hp_per_player = {"p": 1000, "e": 1000}
        self.max_hp = 1000
        self.last_moves = {}
        self.round = 1


def engine(mine=None, enemy=None, bar=16.0):
    mine = mine or GL
    s = FakeSession(mine, enemy or mine, bar)
    e = AbilityEngine.__new__(AbilityEngine)
    e.session = s
    e.st = s.status
    e._compiled = {}
    e.once_fired = set()
    e.modes = {}
    e.counters = {}
    e.ability_2_disabled = {}
    e.debuff_immune = {}
    e.primed_bonus = {}
    e.crit_chance_bonus = {}
    e.crit_damage_mult = {}
    e.special_boost_flat = s.status.special_boost_flat
    e.special_amp_stack = {}
    e.timed_dmg_amps = []
    e.undodgeable_turns = {}
    e.last_hit_was_crit = False
    e.lifesteal_pct = {}
    e.hp_regen_per_turn = {}
    e.revive_pool = {}
    return e, s


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. Dread Hades no longer empties the bar ─────────────────────")

ab = DH["abilities"][0]
check("Soul Reaper steals 4, not 20", ab["hades_stamina_steal"] == 4,
      ab.get("hades_stamina_steal"))
check("the description says 4 too — the card is what players read",
      "steal 4 of their stamina" in ab["description"], ab["description"][:90])
check("the singular and plural ability agree",
      DH["ability"]["hades_stamina_steal"] == ab["hades_stamina_steal"])
# The rest of Soul Reaper is untouched: only the stamina drain was the problem.
check("the 45 true damage is unchanged", ab["hades_drain_true_dmg"] == 45)
check("Cursed Ground is unchanged",
      ab["hades_curse_dmg"] == 20 and ab["hades_curse_turns"] == 3)

print(f"       a Special costs {STAMINA_COST[MOVE_SPECIAL]}, "
      f"an Attack {STAMINA_COST[MOVE_ATTACK]}")
worst = 0.0
for sta in (85, 100, 130, 250, 342):
    bar = max_stamina_for(sta)
    e, s = engine(DH, DH, bar)
    e._fire("on_special", "p", "e", DH, "special", "win", 0, 0, [])
    left = s.stamina_manager.stamina["e"]
    took = (bar - left) / bar * 100
    worst = max(worst, took)
    print(f"       stamina stat {sta:>3} | bar {bar:>5} | "
          f"{left:>5} left | took {took:>3.0f}% (was "
          f"{min(100, 20 / bar * 100):>3.0f}%)")
check("no opponent loses more than a third of their bar to one Special",
      worst < 33, f"{worst:.0f}%")
check("...and it still costs them at least one action",
      4 >= STAMINA_COST[MOVE_ATTACK], 4)

# The drain must never be able to zero somebody out on its own again.
e, s = engine(DH, DH, 16.0)
for _ in range(3):
    e._fire("on_special", "p", "e", DH, "special", "win", 0, 0, [])
check("three Specials in a row still leave them able to act",
      s.stamina_manager.stamina["e"] >= STAMINA_COST[MOVE_ATTACK],
      s.stamina_manager.stamina["e"])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. Guilty Upper — three hits, then the launch ────────────────")

names = [a["name"] for a in GL["abilities"]]
check("the ability is Guilty Upper", names == ["Guilty Upper"], names)
check("the singular mirrors it", GL["ability"]["name"] == "Guilty Upper")
check("it is written in the rules DSL, not legacy fields",
      bool(GL["abilities"][0].get("rules")))
# The old Guilty Verdict's CONDEMNED MODE gate did not survive conversion, so
# guaranteed_crit fired on every winning attack. Gone with the rewrite.
for dead in ("chain", "streak_threshold", "crit_chance", "condemned_special_boost"):
    check(f"the legacy `{dead}` field is gone", dead not in GL["abilities"][0])

e, s = engine()
rows = []
for i in range(1, 7):
    hp0 = s.hp["e"]
    st0 = s.stability_manager.v["e"]
    dmg, _t = e._fire("on_attack_hit", "p", "e", GL, "attack", "win", 100, 0, [])
    rows.append((dmg, hp0 - s.hp["e"], st0 - s.stability_manager.v["e"],
                 s.status.get_buff_bonus("p", "attack")))
for i, (dmg, burst, knock, atk) in enumerate(rows, 1):
    print(f"       hit {i}: +{atk:>2} atk | uppercut {burst:>2} | "
          f"knock {knock:>2.0f}")

check("each landed hit winds up +7 attack",
      [r[3] for r in rows[:3]] == [7, 14, 21], [r[3] for r in rows[:3]])
check("the wind-up caps at 3 stacks' worth per cycle",
      rows[3][3] == 28, rows[3][3])
check("the uppercut lands on the third hit", rows[2][1] == 40, rows[2][1])
check("...and not before it", rows[0][1] == 0 and rows[1][1] == 0,
      [r[1] for r in rows[:2]])
check("...and it repeats — the counter resets", rows[5][1] == 40, rows[5][1])

# This is the half that was silently wrong. `_run_ops` ignored `op["_if"]`
# entirely, so the launch fired on every single hit.
check("the launch lands ONLY with the uppercut",
      [r[2] for r in rows] == [0, 0, 25, 0, 0, 25], [r[2] for r in rows])
check("...knocking 25 stability off the opponent", rows[2][2] == 25, rows[2][2])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. op-level `_if` is honoured at all ─────────────────────────")
# The engine ignored this key. `legacy_convert` has emitted it since it was
# written, so the gate was decorative and the effect fired unconditionally.

e2, _s2 = engine()
gated = {"do": [{"op": "bonus_damage", "value": 50,
                 "_if": [{"cond": "counter_at_least", "name": "nope",
                          "value": 3}]}]}
dmg, _t = e2._run_ops(gated, "T", "p", "e", "attack", 100, 0, [])
check("an op whose gate fails does not run", dmg == 100, dmg)
e2.counters[("p", "nope")] = 3
dmg, _t = e2._run_ops(gated, "T", "p", "e", "attack", 100, 0, [])
check("...and runs once the gate passes", dmg == 150, dmg)
# An ungated op must be unaffected — this is the common case and a regression
# here would silently disable most of the roster.
plain = {"do": [{"op": "bonus_damage", "value": 50}]}
dmg, _t = e2._run_ops(plain, "T", "p", "e", "attack", 100, 0, [])
check("an ungated op still always runs", dmg == 150, dmg)
# All conditions must hold, not any.
both = {"do": [{"op": "bonus_damage", "value": 50, "_if": [
    {"cond": "counter_at_least", "name": "nope", "value": 3},
    {"cond": "counter_at_least", "name": "other", "value": 1}]}]}
dmg, _t = e2._run_ops(both, "T", "p", "e", "attack", 100, 0, [])
check("the gate is an AND, not an OR", dmg == 100, dmg)

# Ordering: the gate reads a counter that `counter_burst` resets. Reversed, the
# condition is always false and the uppercut lands damage with no launch.
burst_rule = next(r for r in GL["abilities"][0]["rules"]
                  if any(o.get("op") == "counter_burst" for o in r["do"]))
ops = [o["op"] for o in burst_rule["do"]]
check("the launch is evaluated BEFORE the burst resets the counter",
      ops.index("enemy_lose_stability") < ops.index("counter_burst"), ops)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 4. left-spin contact hits harder ─────────────────────────────")

check("Longinus still spins Left", GL["spin_direction"] == "Left")
e3, _ = engine(GL, GL)
same, _t = e3._fire("on_attack_hit", "p", "e", GL, "attack", "win", 100, 0, [])
e4, _ = engine(GL, RIGHT)
opp, _t = e4._fire("on_attack_hit", "p", "e", GL, "attack", "win", 100, 0, [])
print(f"       100 damage -> {same} same-spin, {opp} opposite-spin")
check("an opposite-spinning blade takes 20% more", opp == 120, opp)
check("...and a same-spinning one does not", same == 100, same)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5. Guilty Smash ──────────────────────────────────────────────")

sm = GL["special_move"]
check("the Special is Guilty Smash", sm["name"] == "Guilty Smash", sm["name"])
check("it deals 170", sm["total_damage"] == 170, sm["total_damage"])
check("hits x damage really is the stated total",
      sm["hits"] * sm["damage_per_hit"] == sm["total_damage"],
      (sm["hits"], sm["damage_per_hit"]))
check("it has flavour to print", bool(sm.get("flavour_texts")))

e5, s5 = engine()
before = s5.stability_manager.v["p"]
e5._fire("on_special", "p", "e", GL, "special", "win", 0, 0, [])
gained = s5.stability_manager.v["p"] - before
check("and it plants Longinus +20 stability", gained == 20, gained)
check("...on itself, not the opponent",
      s5.stability_manager.v["e"] == 100, s5.stability_manager.v["e"])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6. neither blade is dead in a boss fight ─────────────────────")

from cogs.battle.boss.blade_abilities import kit_for            # noqa: E402

gl_kit = kit_for(GL)
check("Guilty Longinus: the translator accepts the kit", gl_kit is not None)
check("Guilty Longinus: nothing falls through as dormant",
      not getattr(gl_kit, "dormant", []), getattr(gl_kit, "dormant", None))
check("...and its wind-up reaches a boss fight",
      gl_kit.flat_damage > 0 or gl_kit.dmg_amp > 0,
      (gl_kit.flat_damage, gl_kit.dmg_amp))

# Dread Hades' Soul Reaper is dormant in boss fights, and was BEFORE this
# change — its `hades_*` fields are a bespoke legacy shape that neither
# `_rules` nor the legacy path recognises, so the blade contributes nothing
# from its ability against a boss. Asserted as the state that exists rather
# than fixed: this change was asked to NERF Dread Hades, and waking its boss
# kit would be a buff pointing the other way. Recorded here so it is a known
# quantity instead of a surprise.
dh_kit = kit_for(DH)
check("Dread Hades: the translator accepts the kit", dh_kit is not None)
check("Dread Hades: Soul Reaper is dormant against bosses — pre-existing, "
      "not touched by this change",
      getattr(dh_kit, "dormant", []) == ["Soul Reaper"],
      getattr(dh_kit, "dormant", None))
check("...so nerfing the stamina drain changes nothing about boss fights",
      dh_kit.reduction == 0.0 and not dh_kit.true_damage,
      (dh_kit.reduction, dh_kit.true_damage))


print("\n" + "=" * 66)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 66)
sys.exit(1 if FAIL else 0)
