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


from cogs.battle import button_profile as BP                    # noqa: E402
from cogs.core import constants as C                            # noqa: E402
from utils.database import load_beyblades                       # noqa: E402

ALL = load_beyblades()

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

    print(f"\n{PASS} passed, {FAIL} failed")
    if MUTATE:
        # Inverted on purpose: with a broken default, a red run is the pass.
        print("MUTATION RUN — a FAILURE above is the expected, correct result.")
        return 0 if FAIL else 1
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
