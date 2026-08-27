#!/usr/bin/env python3
"""
tools/sim_button_effects.py — the button rework, and the promise it makes.

Why this suite exists
----------------------
The five battle buttons were thin. Charge was a skip-turn that ZERO blades in
the roster reacted to (0 `on_charge_*` rules against 28 `on_attack_hit` and 35
`on_special`), and it was actively punished — `damage_rules.py` hands an
attacker its best multiplier in the game, 1.5x, against a Charge. Stability was
a cliff rather than a resource: `check_ring_out` is `stability <= 0`, so 99/100
played identically to 1/100 and then 0 instantly killed. Special cost the same
150 gauge for all 113 blades. Attack and Defense cost a flat 2.2 stamina each,
so stacking a stat cost nothing anywhere in the economy.

The rework adds real mechanics to all of that. The hard part is not adding
them — it is adding them without silently moving 113 shipped blades that
players already own and have levelled.

Section 1 is that promise, and it is the reason this file exists
----------------------------------------------------------------
Every blade with no `button_profile` block must resolve to EXACTLY the
constant it used before this system existed, for every button, every field.
Section 1 asserts that over the whole roster rather than spot-checking, and it
is deliberately written to be able to fail: `tools/sim_button_effects.py
--mutate` breaks one default on purpose and the suite must go red. A
neutrality proof that cannot fail is not proving anything, and this project has
shipped that mistake before (`sim_panels.py` had 244 passing checks sitting on
top of a dead code path).

The defaults in `button_profile._DEFAULTS` are IMPORTED from
`cogs.core.constants`, never retyped, so "the default equals the current
constant" is true by construction. Section 1 verifies the property anyway —
construction arguments are how the retyped-constant bug gets reintroduced by
someone who "simplified" the import away later.

Run:  python3 tools/sim_button_effects.py
      python3 tools/sim_button_effects.py --mutate   # must FAIL
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0
MUTATE = "--mutate" in sys.argv


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {detail}")


import types as _t                                              # noqa: E402

from cogs.abilities import ability_engine as AE                  # noqa: E402
from cogs.abilities import type_system as TS                     # noqa: E402
from cogs.abilities.ability_engine import AbilityEngine          # noqa: E402
from cogs.battle import button_profile as BP                    # noqa: E402
from cogs.battle import special_gate as SG                      # noqa: E402
from cogs.battle.attack_manager import AttackManager             # noqa: E402
from cogs.battle.session import BattleSession                    # noqa: E402
from cogs.battle.stability_manager import StabilityManager       # noqa: E402
from cogs.battle.stamina_manager import StaminaManager           # noqa: E402
from cogs.battle.status_manager import StatusManager             # noqa: E402
from cogs.core import constants as C                            # noqa: E402
from utils.database import load_beyblades                       # noqa: E402

ALL = load_beyblades()


class _FakeSession:
    """Only the surface special_gate actually touches.

    Deliberately hand-built rather than a real BattleSession: these checks are
    about the gate's own arithmetic (what it charges, what it refuses, what it
    floors), and a real session would drag in blade art, avatars and a Discord
    channel to prove that 120 - 90 == 30.
    """

    def __init__(self):
        self.stamina_manager = type("_SM", (), {"gauge": {"p": 0, "e": 0}})()
        self.ability = type("_AB", (), {"counters": {}, "cooldowns": {}})()
        self.stability_manager = _FakeStability()


class _FakeStability:
    """Mirrors StabilityManager's _apply contract: clamp to [0, max]."""

    def __init__(self):
        self.stability = {"p": 100, "e": 100}
        self.max = {"p": 100, "e": 100}

    def _apply(self, key, delta):
        cur = self.stability.get(key, 0)
        self.stability[key] = max(0, min(self.max.get(key, 100), cur + delta))
        return []


DUMMY = {"name": "Dummy", "type": "Balance", "spin_direction": "Left",
        "stats": {"attack": 100, "defense": 100, "stamina": 100, "hp": 100}}


class _RealSession:
    """A real engine stack, for the checks that are about behavior not arithmetic.

    Sections 8-9 assert things that only mean something end to end — that a
    banked stack survives to the next move, that a multi-hit Special spends it
    once, that a rule firing on on_stability_break can actually save the
    blade. Those need the real AbilityEngine, StabilityManager and
    DamageFilter, not a stand-in that would happily agree with a broken one.
    """

    def __init__(self, mine, theirs, hp=1000, ehp=1000, max_hp=1000):
        import copy
        self.blades = {"p": copy.deepcopy(mine), "e": copy.deepcopy(theirs)}
        self.hp = {"p": hp, "e": ehp}
        self.max_hp = max_hp
        self.max_hp_per_player = {"p": max_hp, "e": max_hp}
        self.battle_stats = {k: dict(v["stats"]) for k, v in self.blades.items()}
        self.bey_levels = {"p": 1, "e": 1}
        self.last_moves, self.moves, self.stat_mult = {}, {}, {}
        self.round = 1
        self.status = StatusManager(self)
        self.status_manager = self.status
        self.stamina_manager = StaminaManager(self.blades)
        self.type_mods = {k: TS.TypeModifiers(v, stats=self.battle_stats[k])
                          for k, v in self.blades.items()}
        self.stability_manager = StabilityManager(self.blades, self.type_mods)
        self.chain_handler = _t.SimpleNamespace(resolve=lambda *a, **k: [])
        self.ability = AbilityEngine(self)
        for _k in self.blades:
            self.ability.setup(_k, self.blades[_k])

    # The real BattleSession method under test, lifted verbatim in behavior:
    # sections 9's whole point is that the guard RE-READS after firing.
    _ring_out_guard = BattleSession._ring_out_guard


def _am(session):
    am = AttackManager.__new__(AttackManager)
    am.session = session
    return am


def _charge(am, s, mkey, okey):
    """Drive one real Charge through resolve_pair's non-combat branch."""
    s.last_moves = {mkey: C.MOVE_CHARGE, okey: C.MOVE_CHARGE}
    _, _, _, logs = am.resolve_pair(
        mkey, okey, C.MOVE_CHARGE, C.MOVE_CHARGE,
        s.blades[mkey], s.blades[okey],
        s.battle_stats[mkey], s.battle_stats[okey])
    return logs


def _attack(s, mkey, okey, dmg=100):
    s.last_moves = {mkey: C.MOVE_ATTACK, okey: C.MOVE_ATTACK}
    return s.ability.apply(mkey, okey, s.blades[mkey], s.blades[okey],
                           C.MOVE_ATTACK, "win", dmg, 0,
                           is_first_hit=True, cumulative_dmg=0,
                           is_last_hit=True)

if MUTATE:
    # Break exactly one neutral default and nothing else. If section 1 still
    # passes after this, it is not actually checking the roster.
    BP._DEFAULTS["attack"]["gauge"] = 50
    print("\n!! MUTATION ACTIVE: attack gauge default forced to 50 !!")


def main() -> int:
    # ── 1. the neutrality proof ─────────────────────────────────────────────
    print("\n── 1. every blade without a button_profile is UNCHANGED ─────────")
    plain = [(n, b) for n, b in ALL.items() if not BP.has_profile(b)]
    check(f"most of the roster has opted out and must not move "
          f"({len(plain)}/{len(ALL)} blades)",
          len(plain) >= len(ALL) - 5, len(plain))

    # Every field, every button, every opted-out blade — against the constant
    # each one used before button_profile existed.
    expected_gauge = {
        "attack":  C.GAUGE_PER_ATTACK,
        "defense": C.GAUGE_PER_DEFENSE,
        "stamina": C.GAUGE_PER_STAMINA,
        "charge":  C.GAUGE_PER_CHARGE,
    }
    bad_gauge, bad_special, bad_charge, bad_tiers, bad_stam = [], [], [], [], []
    for name, blade in plain:
        for source, want in expected_gauge.items():
            if BP.gauge_gain(blade, source) != want:
                bad_gauge.append((name, source, BP.gauge_gain(blade, source), want))
        if BP.special_gauge_cost(blade) != C.SPECIAL_GAUGE_MAX:
            bad_special.append((name, BP.special_gauge_cost(blade)))
        if BP.special_stability_cost(blade) != 0:
            bad_special.append((name, "stab", BP.special_stability_cost(blade)))
        cfg = BP.charge_cfg(blade)
        if cfg["max_stacks"] != 0 or cfg["per_stack_pct"] != 0 \
                or cfg["stability_per_stack"] != 0:
            bad_charge.append((name, cfg))
        if BP.stability_tiers(blade) != ():
            bad_tiers.append((name, BP.stability_tiers(blade)))
        if BP.stamina_cost(blade, C.MOVE_ATTACK, 2.2) != 2.2:
            bad_stam.append((name, BP.stamina_cost(blade, C.MOVE_ATTACK, 2.2)))

    check("gauge gain is the old constant for every opted-out blade, "
          "on all four buttons", not bad_gauge, bad_gauge[:4])
    check("Special still costs the full gauge bar, and no stability",
          not bad_special, bad_special[:4])
    check("Charge banks NO stacks — the whole stack system is inert "
          "until a blade opts in", not bad_charge, bad_charge[:4])
    check("no stability tiers, so the bar keeps its all-or-nothing behavior",
          not bad_tiers, bad_tiers[:4])
    check("the flat stamina cost passes through untouched",
          not bad_stam, bad_stam[:4])
    check("...and none of them counts as reworked",
          not any(BP.rework_active(b) for _, b in plain))

    # ── 2. the module contract ──────────────────────────────────────────────
    print("\n── 2. authored profiles merge per-KEY, not per-block ────────────")
    partial = {"name": "Partial",
               "button_profile": {"special": {"gauge_cost": 90}}}
    check("an authored field takes effect", BP.special_gauge_cost(partial) == 90)
    check("...and every field it did NOT mention keeps today's number — a "
          "blade wanting a cheap Special must not silently lose its gauge gain",
          BP.gauge_gain(partial, "attack") == C.GAUGE_PER_ATTACK
          and BP.special_stability_cost(partial) == 0)
    check("opting in at all flags the blade as reworked",
          BP.has_profile(partial) and BP.rework_active(partial))

    print("\n── 3. malformed data degrades, never raises ─────────────────────")
    # This is hand-edited JSON. A blade whose Attack button raises mid-battle
    # is a far worse outcome than one quietly playing by the standard numbers.
    for label, bad in (
        ("profile is a string",      {"button_profile": "nope"}),
        ("a block is null",          {"button_profile": {"special": None}}),
        ("a block is a list",        {"button_profile": {"charge": [1, 2]}}),
        ("a value is not a number",  {"button_profile": {"special":
                                                         {"gauge_cost": "abc"}}}),
        ("tiers are malformed",      {"button_profile": {"stability":
                                                         {"tiers": "bad"}}}),
        ("blade is None",            None),
        ("blade is empty",           {}),
    ):
        try:
            ok = (BP.special_gauge_cost(bad) == C.SPECIAL_GAUGE_MAX
                  and BP.charge_cfg(bad)["max_stacks"] == 0
                  and BP.gauge_gain(bad, "attack") == C.GAUGE_PER_ATTACK
                  and BP.stability_tiers(bad) == ())
        except Exception as exc:                             # noqa: BLE001
            ok = False
            print(f"       raised: {exc!r}")
        check(f"{label} → falls back to defaults", ok)

    print("\n── 4. values that would invert a drawback are clamped ───────────")
    # A Special that GIVES stability, or costs nothing to fire, is not a
    # tuning value — it is the drawback silently becoming a bonus.
    free = {"button_profile": {"special": {"gauge_cost": 0}}}
    check("a 0-cost Special is floored to 1 — the gauge economy is the only "
          "thing pacing Specials at all",
          BP.special_gauge_cost(free) == 1, BP.special_gauge_cost(free))
    gainer = {"button_profile": {"special": {"stability_cost": -5}}}
    check("a negative stability COST cannot become a stability gain",
          BP.special_stability_cost(gainer) == 0)
    negstack = {"button_profile": {"charge": {"max_stacks": -3,
                                              "per_stack_pct": -20}}}
    cfg = BP.charge_cfg(negstack)
    check("negative charge stacks/percentages clamp to zero",
          cfg["max_stacks"] == 0 and cfg["per_stack_pct"] == 0.0, cfg)

    print("\n── 5. tier ordering is normalised, not trusted ──────────────────")
    # Tiers are matched highest-fraction-first. Authored out of order they
    # would match the wrong band, so the module sorts rather than assuming.
    unordered = {"button_profile": {"stability": {"tiers": [
        [0.15, {"incoming_mult": 1.30}], [0.30, {"incoming_mult": 1.15}]]}}}
    tiers = BP.stability_tiers(unordered)
    check("tiers come back sorted high→low regardless of authored order",
          [t[0] for t in tiers] == [0.30, 0.15], tiers)

    # ── 6. per-blade Special cost (Part 5) ──────────────────────────────────
    print("\n── 6. a Special can now be cheaper or dearer than everyone's ────")
    plain_blade = plain[0][1] if plain else {}
    cheap = {"name": "Cheap", "button_profile": {"special": {"gauge_cost": 90}}}
    dear  = {"name": "Dear",  "button_profile": {"special": {"gauge_cost": 150}}}

    sess = _FakeSession()
    check("an opted-out blade still needs the full bar",
          SG.gauge_cost(plain_blade) == C.SPECIAL_GAUGE_MAX)
    check("a cheap Special is READY at 100 gauge, where the default is not",
          SG.blocked_reason(sess, "p", cheap, 100) is None
          and SG.blocked_reason(sess, "p", plain_blade, 100) is not None)
    check("...and is still blocked below its own cost",
          SG.blocked_reason(sess, "p", cheap, 89) is not None)
    check("the block message quotes the blade's OWN cost, not 150 — a player "
          "told '89/150' when the real bar is 90 cannot act on that",
          "/90" in (SG.blocked_reason(sess, "p", cheap, 89) or ""),
          SG.blocked_reason(sess, "p", cheap, 89))

    # The whole point of a cheap Special: the change stays on the bar.
    # consume_gauge()'s hard zero would confiscate it.
    sess.stamina_manager.gauge["p"] = 120
    spent = SG.spend(sess, "p", cheap)
    check("spend() deducts exactly the blade's cost",
          spent == 90, spent)
    check("...leaving the change on the gauge instead of zeroing it",
          sess.stamina_manager.gauge["p"] == 30,
          sess.stamina_manager.gauge["p"])

    sess.stamina_manager.gauge["p"] = 150
    SG.spend(sess, "p", dear)
    check("a full-cost Special still empties the bar exactly as before",
          sess.stamina_manager.gauge["p"] == 0)

    sess.stamina_manager.gauge["p"] = 20
    SG.spend(sess, "p", cheap)
    check("spending more than is banked floors at 0, never negative",
          sess.stamina_manager.gauge["p"] == 0)

    # spend() also resets the extra counter — both halves of "pay for the
    # Special" in one call, so they cannot drift apart.
    kiri = {"name": "K", "special_requires": {"counter": "purifier_charge",
                                              "value": 100}}
    sess.stamina_manager.gauge["p"] = 150
    sess.ability.counters[("p", "purifier_charge")] = 100
    SG.spend(sess, "p", kiri)
    check("...and still zeroes a second resource in the same call",
          sess.ability.counters[("p", "purifier_charge")] == 0)

    print("\n── 7. a Special can never ring out its own user ─────────────────")
    # The 0 default is a real fix, not an oversight: Specials used to cost
    # -10 stability and blades could kill themselves casting.
    risky = {"name": "Risky",
             "button_profile": {"special": {"stability_cost": 40}}}
    sess2 = _FakeSession()
    sess2.stability_manager.stability["p"] = 100
    SG.apply_stability_cost(sess2, "p", risky)
    check("an authored stability cost is really paid",
          sess2.stability_manager.stability["p"] == 60,
          sess2.stability_manager.stability["p"])

    sess2.stability_manager.stability["p"] = 25
    SG.apply_stability_cost(sess2, "p", risky)
    check("...but is clamped to leave 1 standing — a drawback must not be a "
          "suicide button",
          sess2.stability_manager.stability["p"] == 1,
          sess2.stability_manager.stability["p"])

    sess2.stability_manager.stability["p"] = 1
    SG.apply_stability_cost(sess2, "p", risky)
    check("at 1 stability it costs nothing at all rather than ringing out",
          sess2.stability_manager.stability["p"] == 1)

    sess2.stability_manager.stability["p"] = 100
    SG.apply_stability_cost(sess2, "p", plain_blade)
    check("an opted-out blade's Special still costs ZERO stability",
          sess2.stability_manager.stability["p"] == 100)

    # ── 8. Charge stacks, driven through the real engine (Part 2) ───────────
    print("\n── 8. Charge is no longer a skip-turn ───────────────────────────")
    CHARGER = {
        "name": "Charger", "type": "Attack", "spin_direction": "Right",
        "stats": {"attack": 100, "defense": 100, "stamina": 100, "hp": 100},
        "button_profile": {"charge": {"max_stacks": 3, "per_stack_pct": 10,
                                      "lost_on_hit": True,
                                      "stability_per_stack": 2}},
    }
    s = _RealSession(CHARGER, DUMMY)
    am = _am(s)
    eng = s.ability

    # Drop below the ceiling first: starting stability doubles as the max, so
    # a blade at full would have its +2 clamped away and the check would pass
    # or fail for the wrong reason.
    s.stability_manager.stability["p"] -= 10
    stab0 = s.stability_manager.stability["p"]
    _charge(am, s, "p", "e")
    check("charging banks a stack",
          eng.counters.get(("p", "charge_stack")) == 1)
    check("...and steadies the blade by stability_per_stack",
          s.stability_manager.stability["p"] == stab0 + 2,
          (s.stability_manager.stability["p"], stab0))

    _charge(am, s, "p", "e"); _charge(am, s, "p", "e")
    check("stacks accumulate to the authored cap",
          eng.counters.get(("p", "charge_stack")) == 3)
    _charge(am, s, "p", "e")
    check("...and stop there rather than growing forever",
          eng.counters.get(("p", "charge_stack")) == 3)

    # Cash them in: 3 stacks x 10% = +30% on the next Attack.
    out, _, _ = _attack(s, "p", "e", dmg=100)
    check("the bank is released into the next Attack (+30% on 3 stacks)",
          out == 130, out)
    check("...and is spent, not kept",
          eng.counters.get(("p", "charge_stack")) == 0)

    out2, _, _ = _attack(s, "p", "e", dmg=100)
    check("a second Attack with an empty bank is plain damage again",
          out2 == 100, out2)

    # The gamble: getting hit mid-charge knocks the bank loose.
    s2 = _RealSession(CHARGER, DUMMY)
    _charge(_am(s2), s2, "p", "e")
    _charge(_am(s2), s2, "p", "e")
    check("two stacks banked", s2.ability.counters.get(("p", "charge_stack")) == 2)
    _attack(s2, "e", "p", dmg=60)          # the DUMMY hits the charger
    check("being hit while charging knocks the whole bank loose — this is "
          "what keeps Charge a gamble rather than free value",
          s2.ability.counters.get(("p", "charge_stack")) == 0,
          s2.ability.counters.get(("p", "charge_stack")))

    # A multi-hit Special must spend the bank ONCE, not per hit.
    s3 = _RealSession(CHARGER, DUMMY)
    for _ in range(3):
        _charge(_am(s3), s3, "p", "e")
    spent_logs = []
    s3.ability.apply(
        "p", "e", s3.blades["p"], s3.blades["e"], C.MOVE_SPECIAL, "win",
        100, 0, is_first_hit=True, cumulative_dmg=0, is_last_hit=False)
    mid = s3.ability.counters.get(("p", "charge_stack"))
    s3.ability.apply(
        "p", "e", s3.blades["p"], s3.blades["e"], C.MOVE_SPECIAL, "win",
        100, 0, is_first_hit=False, cumulative_dmg=100, is_last_hit=True)
    check("a multi-hit Special spends the bank once, on the first hit only — "
          "per-hit spending is the trap that already caught the duration tick",
          mid == 0 and s3.ability.counters.get(("p", "charge_stack")) == 0,
          (mid, s3.ability.counters.get(("p", "charge_stack")), spent_logs))

    # And the whole system stays inert for an opted-out blade.
    s4 = _RealSession(dict(DUMMY, name="Plain"), DUMMY)
    _charge(_am(s4), s4, "p", "e")
    check("an opted-out blade banks NOTHING — Charge behaves exactly as it "
          "always did for the other 113 blades",
          s4.ability.counters.get(("p", "charge_stack"), 0) == 0)
    out_plain, _, _ = _attack(s4, "p", "e", dmg=100)
    check("...and its Attack is unmodified", out_plain == 100, out_plain)

    # ── 9. the new triggers actually fire (Part 4) ──────────────────────────
    print("\n── 9. the new triggers are reachable ────────────────────────────")
    check("all five are registered in TRIGGERS",
          {"on_charge", "on_low_stability", "on_stability_break",
           "on_gauge_full"} <= AE.TRIGGERS, AE.TRIGGERS)

    # on_charge is the headline: on_charge_win/_loss are UNREACHABLE because
    # calc_damage returns "mirror" for every charge, so this hook is the only
    # honest way for a blade to react to charging at all.
    REACTOR = {
        "name": "Reactor", "type": "Attack", "spin_direction": "Right",
        "stats": {"attack": 100, "defense": 100, "stamina": 100, "hp": 100},
        "abilities": [{"name": "Spark", "trigger": "on_charge", "rules": [
            {"when": "on_charge", "do": [
                {"op": "log", "text": "SPARK-FIRED"}], "_name": "Spark"}]}],
    }
    s5 = _RealSession(REACTOR, DUMMY)
    logs5 = _charge(_am(s5), s5, "p", "e")
    check("on_charge fires on a real Charge — the trigger no blade could use "
          "before, because only on_charge_mirror was ever reachable",
          any("SPARK-FIRED" in l for l in logs5), logs5)

    # on_stability_break is a genuine last chance, not a notification.
    SAVER = {
        "name": "Saver", "type": "Attack", "spin_direction": "Right",
        "stats": {"attack": 100, "defense": 100, "stamina": 100, "hp": 100},
        "abilities": [{"name": "Cling", "trigger": "on_stability_break",
                       "rules": [{"when": "on_stability_break", "do": [
                           {"op": "gain_stability", "value": 20}],
                           "_name": "Cling"}]}],
    }
    s6 = _RealSession(SAVER, DUMMY)
    s6.stability_manager.stability["p"] = 0
    survived = not s6._ring_out_guard("p", [])
    check("a rule that restores stability on on_stability_break actually "
          "SURVIVES the ring-out — the guard re-reads the bar after firing",
          survived and s6.stability_manager.stability["p"] > 0,
          s6.stability_manager.stability["p"])

    s7 = _RealSession(dict(DUMMY, name="NoSave"), DUMMY)
    s7.stability_manager.stability["p"] = 0
    check("...and a blade with no such rule still rings out normally",
          s7._ring_out_guard("p", []))

    print(f"\n{PASS} passed, {FAIL} failed")
    if MUTATE:
        # Inverted on purpose: with a broken default, a red run is the pass.
        print("MUTATION RUN — a FAILURE above is the expected, correct result.")
        return 0 if FAIL else 1
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
