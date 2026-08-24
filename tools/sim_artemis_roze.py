#!/usr/bin/env python3
"""
tools/sim_artemis_roze.py — Artemis Roze, and the six ops written for her.

Why this suite exists
----------------------
Every mechanic Artemis Roze's kit asked for was checked against the engine
BEFORE it was authored (see the AskUserQuestion rounds this session), because
guessing wrong on a brand-new primitive and finding out from a live battle
log is exactly the failure this house style exists to prevent (see
sim_heavens_ring.py's own docstring — 41% of the roster once sat exactly
there). Six things had no home in the engine at all:

  cleanse                       nothing removed a status; "reach 5 stacks,
                                 cleanse 1 negative effect" had no verb
  stack_scaled_lifesteal_pct    lifesteal that TRACKS a counter (and can fall
                                 back down when it's spent) — the existing
                                 lifesteal_pct op only ever grows, forever
  consume_stack_damage_pct      a stack payoff that SPENDS the stacks, unlike
                                 stacking_buff's permanent per-stack grant
  consume_stack_burst_enemy_hp_pct  a finisher scaled to the ENEMY's current
                                 hp per stack — deliberately swingier than any
                                 other burst in the roster (all of which scale
                                 off the attacker's own stats), by explicit
                                 design choice
  set_mode(turns=...)           a transform that reverts ITSELF and can carry
                                 a finale — every existing mode-based blade
                                 was a permanent stance swap
  enemy_debuffed_stat /         "is the enemy already bound" and "is this
  not_on_cooldown               ability still cooling down" — beys have no
                                 player-activated ability slot other than the
                                 Special, so "Active, 2-turn cooldown" had to
                                 be built as an automatic trigger with an
                                 internal gate instead

Driven through a REAL AbilityEngine and a REAL StatusManager (not a stand-in
for cleanse/burn/silence — those exist nowhere but there), on a minimal fake
session, exactly the technique sim_heavens_ring.py established.

Run:  python3 tools/sim_artemis_roze.py
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


from cogs.abilities.ability_engine import AbilityEngine       # noqa: E402
from cogs.battle.status_manager import StatusManager          # noqa: E402
from utils.database import get_beyblade, load_beyblades       # noqa: E402
from utils import bey_levels as BL                            # noqa: E402

NAME = "Artemis Roze"
AR = get_beyblade(NAME)
ALL = load_beyblades()


class FakeSession:
    def __init__(self, hp=1000, ehp=1000):
        self.status = None            # filled in by engine()
        self.blades = {"p": AR, "e": AR}
        self.hp = {"p": hp, "e": ehp}
        self.max_hp_per_player = {"p": 2000, "e": 2000}
        self.max_hp = 2000
        self.last_moves = {}
        self.round = 1


def engine(hp=1000, ehp=1000):
    """A real AbilityEngine + a real StatusManager, on a minimal session —
    everything cleanse/burn/silence/buffs touch is the actual production
    code, not a stand-in that could quietly diverge from it."""
    s = FakeSession(hp, ehp)
    s.status = StatusManager(s)
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
    e.timed_modes = []
    e.cooldowns = {}
    return e, s


def main() -> int:
    # ── 1. the card matches the spec ────────────────────────────────────────
    print("\n── 1. the card matches the spec ─────────────────────────────────")

    check("Artemis Roze is in the roster", AR is not None)
    check("rarity Mythic", AR["rarity"] == "Mythic", AR.get("rarity"))
    check("type Attack — switched from Defense per the follow-up request",
          AR["type"] == "Attack", AR.get("type"))
    check("its id is unique",
          sum(1 for b in ALL.values() if b.get("id") == AR["id"]) == 1, AR["id"])
    check("the image is the one supplied",
          "1541405667586478120/1785516907259.png" in AR["image_url"])

    names = [a["name"] for a in AR["abilities"]]
    check("all three abilities are on the plural list the engine reads",
          names == ["Prismatic Rebirth", "Ribbon Entangle",
                    "Flowering Metamorphosis"], names)
    check("the singular mirrors the first of them",
          AR["ability"]["name"] == AR["abilities"][0]["name"])

    sm = AR["special_move"]
    check("hits x damage really is the stated total",
          sm["hits"] * sm["damage_per_hit"] == sm["total_damage"],
          (sm["hits"], sm["damage_per_hit"], sm["total_damage"]))
    check("it has flavour to print", bool(sm.get("flavour_texts")))

    at100 = BL.stats_at(AR, 100, {})
    check("no stat is pinned to the level-100 cap (that's a roster-wide "
          "sanity rule sim_levels.py enforces — this blade must not trip it, "
          "even after the +50 Attack buff)",
          all(v < BL.STAT_CAP for v in at100.values()), at100)
    check("the roster-wide 'almost nothing pinned to the cap' gate still "
          "holds for attack — sim_levels.py enforces capped <= 2 roster-wide, "
          "and she must not push it past that",
          sum(1 for b in ALL.values()
              if BL.stats_at(b, 100, {}).get("attack") == BL.STAT_CAP) <= 2)
    check("attack is 155 — buffed +50 per the follow-up request",
          AR["stats"]["attack"] == 155, AR["stats"]["attack"])
    check("defense is 70 — lowered again (150 -> 90 -> 70) across three "
          "follow-up requests",
          AR["stats"]["defense"] == 70, AR["stats"]["defense"])

    # ── 1b. card_theme — the per-blade colour override ─────────────────────
    print("\n── 1b. her card uses its own palette, not just Mythic red ──────")

    from utils import info_card as IC
    from utils import info_card_pillow as ICP

    theme = AR.get("card_theme")
    check("she carries a card_theme override",
          isinstance(theme, dict) and {"accent", "glow", "tint"} <= theme.keys(),
          theme)

    resolved = IC.theme_for(AR)
    check("theme_for() returns HER colours, not the shared Mythic ones",
          resolved == theme and resolved != IC._RARITY_THEME["Mythic"], resolved)

    other = get_beyblade("Dead Phoenix")
    check("a blade with no override still gets its plain rarity theme — the "
          "override cannot leak onto a blade that never asked for one",
          IC.theme_for(other) == IC._RARITY_THEME["Mythic"], IC.theme_for(other))

    html = IC.build_html(AR)
    check("the HTML/Playwright card actually renders her accent colour",
          theme["accent"] in html, theme["accent"])
    check("...and the glow and tint too",
          theme["glow"] in html and theme["tint"] in html, None)

    buf = ICP._render(AR, {})
    check("the Pillow fallback card renders without raising",
          buf is not None and len(buf.getvalue()) > 0,
          len(buf.getvalue()) if buf else None)
    # The bug this caught before shipping: info_card_pillow.py used to read
    # `_RARITY_THEME[rarity]` directly, bypassing theme_for() entirely — a
    # blade's own card_theme was honoured by the HTML renderer and silently
    # ignored by the Pillow one, so which palette you saw depended on which
    # renderer happened to run, not on the blade.
    pillow_src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "utils/info_card_pillow.py"), encoding="utf-8").read()
    check("the Pillow renderer goes through theme_for(), not a raw rarity "
          "lookup that would skip card_theme",
          "theme_for(blade)" in pillow_src
          and "_RARITY_THEME.get(rarity" not in pillow_src, None)

    # ── 1c. player exclusive — every route is shut ──────────────────────────
    print("\n── 1c. player exclusive — every route is shut ──────────────────")

    from utils.availability import obtainable                # noqa: E402
    import cogs.economy.shop as SHOP                          # noqa: E402
    import cogs.spawn.spawn as SPAWN                          # noqa: E402
    from cogs.tournament.tournament import draft_pool         # noqa: E402
    import random as _random

    OWNER = 1273889267986468885

    check("nobody in general can obtain it", not obtainable(AR))
    check("the named owner can", obtainable(AR, OWNER))
    check("...and nobody else can", not obtainable(AR, 956773141265391676))
    check("it is flagged limited", AR.get("limited") is True)
    check("...and bound to exactly one id", AR.get("owner_ids") == [OWNER],
          AR.get("owner_ids"))

    _random.seed(11)
    spawned = sum(1 for _ in range(50_000)
                  if (SPAWN._pick_random_beyblade(ALL) or {}).get("name") == NAME)
    check("50,000 wild spawns produce none", spawned == 0, spawned)
    check("it is not in the booster pack pool",
          not any(b.get("name") == NAME for b in SHOP._load_booster_pool()))
    check("...nor the booster hidden pool",
          not any(b.get("name") == NAME for b in SHOP._hidden_drop_pool()))
    check("...nor a tournament draft",
          not any(b.get("name") == NAME for b in draft_pool()))

    # ── 2. Prismatic Rebirth — Petal Layer ──────────────────────────────────
    print("\n── 2. Prismatic Rebirth — stack, heal, cleanse at 5 ─────────────")

    # Each stack rounds its OWN +8% independently (stacking_buff computes
    # `per` fresh every call) rather than rounding a running total once, so
    # the expected value is `round(defense * 0.08) * i`, not
    # `round(defense * 0.08 * i)` — the two diverge as soon as a single
    # stack's rounding remainder would have crossed an integer boundary.
    per_stack_def = round(AR["stats"]["defense"] * 0.08)
    e, s = engine()
    for i in range(1, 5):
        dmg, _t = e._fire("on_defend", "p", "e", AR, "attack", "lose", 0, 0, [])
        check(f"stack {i}: defense grows +8%",
              e.st.get_buff_bonus("p", "defense") == per_stack_def * i,
              e.st.get_buff_bonus("p", "defense"))
        check(f"stack {i}: lifesteal is {i*5}%",
              e.lifesteal_pct["p"] == i * 5, e.lifesteal_pct["p"])

    check("nothing has been cleansed yet — she isn't at 5 stacks",
          e.counters[("p", "petal_layer")] == 4)

    # Plant a real burn through the real StatusManager, then take the 5th hit.
    e.st.apply_burn("p", {"name": "test burn", "burn_damage_per_turn": 10,
                          "burn_duration": 3, "max_burn_stacks": 1})
    check("the burn actually landed", e.st.burn_stacks.get("p", 0) == 1)
    e._fire("on_defend", "p", "e", AR, "attack", "lose", 0, 0, [])
    check("reaching the 5th stack cleanses the burn",
          e.st.burn_stacks.get("p", 0) == 0, e.st.burn_stacks.get("p"))
    check("...and the 5th stack's defense/lifesteal still landed",
          e.counters[("p", "petal_layer")] == 5
          and e.lifesteal_pct["p"] == 25, e.lifesteal_pct.get("p"))

    # One more hit past the cap: no 6th stack, and — the mutation this guards
    # against — cleanse does NOT fire again just because the counter still
    # reads >= 5. It only fires on the TRANSITION into 5, once.
    e.st.apply_burn("p", {"name": "test burn 2", "burn_damage_per_turn": 10,
                          "burn_duration": 3, "max_burn_stacks": 1})
    e._fire("on_defend", "p", "e", AR, "attack", "lose", 0, 0, [])
    check("the stack caps at 5, not 6",
          e.counters[("p", "petal_layer")] == 5)
    check("...and cleanse does not re-fire just for STAYING at 5",
          e.st.burn_stacks.get("p", 0) == 1, e.st.burn_stacks.get("p"))

    # ── 3. Ribbon Entangle — bind, consume, cooldown ────────────────────────
    print("\n── 3. Ribbon Entangle — bind, consume stacks, cooldown ──────────")

    e, s = engine()
    for _ in range(3):
        e._fire("on_defend", "p", "e", AR, "attack", "lose", 0, 0, [])
    check("3 Petal Layers banked before the first Attack",
          e.counters[("p", "petal_layer")] == 3)

    dmg, _t = e._fire("on_attack_hit", "p", "e", AR, "attack", "win", 100, 0, [])
    check("3 stacks at 15% each adds +45 damage (100 -> 145)",
          dmg == 145, dmg)
    check("the stacks are SPENT, not kept",
          e.counters[("p", "petal_layer")] == 0)
    base_atk = AR["stats"]["attack"]
    base_sta = AR["stats"]["stamina"]
    check("the enemy's Attack is cut by 30% of THEIR base stat",
          e.st.get_buff_bonus("e", "attack") == -round(base_atk * 0.30),
          e.st.get_buff_bonus("e", "attack"))
    check("...and Stamina the same way",
          e.st.get_buff_bonus("e", "stamina") == -round(base_sta * 0.30),
          e.st.get_buff_bonus("e", "stamina"))
    check("the cooldown is now armed",
          e.cooldowns[("p", "ribbon")] == 2, e.cooldowns.get(("p", "ribbon")))

    # Immediately again, with fresh stacks banked: cooldown blocks it outright.
    for _ in range(2):
        e._fire("on_defend", "p", "e", AR, "attack", "lose", 0, 0, [])
    dmg2, _t = e._fire("on_attack_hit", "p", "e", AR, "attack", "win", 100, 0, [])
    check("on cooldown, the SAME Attack does not fire it again",
          dmg2 == 100, dmg2)
    check("...and the banked stacks were not touched",
          e.counters[("p", "petal_layer")] == 2)

    # Tick the cooldown down, then fire on an already-bound enemy.
    e.tick_extras()
    e.tick_extras()
    check("two ticks clear the 2-turn cooldown",
          e.cooldowns.get(("p", "ribbon"), 0) == 0)
    dmg3, _t = e._fire("on_attack_hit", "p", "e", AR, "attack", "win", 100, 0, [])
    # 2 banked layers (30%) + the already-bound 50% bonus, applied in that
    # order: 100 -> +50% (bound) = 150 -> +30% of 150 = 195.
    check("an already-Bound target takes the extra 50%, THEN the stack bonus",
          dmg3 == 195, dmg3)

    # ── 4. Flowering Metamorphosis — Blooming State ─────────────────────────
    print("\n── 4. Flowering Metamorphosis — transform, finale ───────────────")

    e, s = engine(hp=1000, ehp=1000)
    e._fire("on_special", "p", "e", AR, "special", "win", 0, 0, [])
    check("Blooming State engages", e.modes.get("p") == "blooming")
    check("it is scheduled to revert in 3 turns",
          len(e.timed_modes) == 1 and e.timed_modes[0]["turns"] == 3,
          e.timed_modes)
    check("the 40% transform lifesteal is live immediately — not waiting "
          "for the next on_defend",
          e.lifesteal_pct["p"] == 40, e.lifesteal_pct.get("p"))

    e._fire("on_defend", "p", "e", AR, "attack", "lose", 0, 0, [])
    check("one opponent attack grants 2 Petal Layers while Blooming, not 1",
          e.counters[("p", "petal_layer")] == 2, e.counters[("p", "petal_layer")])
    check("lifesteal reflects both the stack (10%) and the transform (+40%)",
          e.lifesteal_pct["p"] == 50, e.lifesteal_pct["p"])

    dmg, _t = e._fire("on_attack_hit", "p", "e", AR, "attack", "win", 100, 0, [])
    check("her own Attack lands roughly doubled while Blooming",
          dmg == 200, dmg)
    # Ribbon Entangle also fires here (it isn't gated off by Blooming — the
    # bind is still worth applying) but must NOT eat the banked layers: they
    # belong to the finale. This is the actual bug this suite caught before
    # shipping — the fix is the `mode_is_not: blooming` gate on Ribbon's
    # consume_stack_damage_pct op, not a change to this test.
    check("Ribbon Entangle still binds during Blooming...",
          e.st.get_buff_bonus("e", "attack") < 0)
    check("...but does NOT spend the Petal Layers meant for the finale",
          e.counters[("p", "petal_layer")] == 2, e.counters[("p", "petal_layer")])

    # End of Blooming: the finale must read the enemy's CURRENT hp at the
    # moment it fires, not a value captured earlier — proven by changing it
    # in between banking the stacks and the transform actually expiring.
    s.hp["e"] = 400
    e.tick_extras()
    e.tick_extras()
    check("still Blooming after 2 of 3 ticks", e.modes.get("p") == "blooming")
    before_hp = s.hp["e"]
    e.tick_extras()
    check("Blooming reverts on the 3rd tick", e.modes.get("p") == "")
    # 2 layers x 20% of the CURRENT 400 hp = 160.
    check("the finale burns 20% of the enemy's CURRENT hp per layer (2 -> 160)",
          before_hp - s.hp["e"] == 160, before_hp - s.hp["e"])
    check("the layers are spent by the finale",
          e.counters[("p", "petal_layer")] == 0)

    # ── 5. the ops are safe in isolation ────────────────────────────────────
    print("\n── 5. the new ops are safe on their own ──────────────────────────")

    e, s = engine()
    dmg0, _t = e._fire("on_attack_hit", "p", "e", AR, "attack", "win", 50, 0, [])
    check("consume_stack_damage_pct with nothing banked adds nothing",
          dmg0 == 50, dmg0)

    e2, s2 = engine()
    cleared = e2.st.cleanse_one("p")
    check("cleanse_one on a clean target returns None, not an error",
          cleared is None, cleared)
    e2.st.silence("p", 2)
    cleared2 = e2.st.cleanse_one("p")
    check("cleanse priority: silence is cleared when there's no burn",
          cleared2 == "silence" and not e2.st.is_silenced("p"), cleared2)

    unknown_grade_safe = True
    try:
        e2._run_ops({"do": [{"op": "not_a_real_op", "value": 1}]},
                    "test", "p", "e", "attack", 10, 0, [])
    except Exception as exc:                                  # noqa: BLE001
        unknown_grade_safe = False
        print("       unexpected:", exc)
    check("an unknown op is skipped, not a crash (forward-compat)",
          unknown_grade_safe)

    # ── 6. reachable from the actual card, not just this suite ──────────────
    print("\n── 6. every new op is reachable from Artemis Roze's own JSON ────")

    import json
    blob = json.dumps(AR)
    for op in ("cleanse", "stack_scaled_lifesteal_pct", "consume_stack_damage_pct",
              "consume_stack_burst_enemy_hp_pct", "start_cooldown"):
        check(f'"{op}" is used in her own rules, not invented for this test',
              f'"op": "{op}"' in blob, None)
    for cond in ("counter_at_least", "counter_below", "enemy_debuffed_stat",
                "not_on_cooldown", "mode_is", "mode_is_not"):
        check(f'"{cond}" is used in her own rules',
              f'"cond": "{cond}"' in blob, None)
    check('set_mode carries "turns" — the timed-transform path, not the '
          "old permanent-switch one",
          '"turns": 3' in blob, None)

    engine_src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "cogs/abilities/ability_engine.py"), encoding="utf-8").read()
    check("tick_extras exists on the engine",
          "def tick_extras(self)" in engine_src, None)
    session_src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "cogs/battle/session.py"), encoding="utf-8").read()
    check("tick_extras is actually called every round, not just defined",
          "self.ability.tick_extras()" in session_src, None)

    # ── 7. the roster still holds together ──────────────────────────────────
    print("\n── 7. the roster still compiles ──────────────────────────────────")
    broken = []
    eng, _ = engine()
    for name, blade in ALL.items():
        try:
            eng._rules_for(blade, "p")
        except Exception as exc:                              # noqa: BLE001
            broken.append(f"{name}: {exc}")
    check("every blade in the roster still compiles its abilities",
          not broken, broken)

    print("\n" + "=" * 66)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
