#!/usr/bin/env python3
"""
tools/sim_kirindael.py — Kirindael, and the second Special resource.

What is genuinely new here
--------------------------
Every other blade in the roster fires its Special on one condition: the gauge
is full. Kirindael needs a full gauge AND 60 Purifier Charge, a resource its
own abilities build. That is not a bigger number, it is a second gate, and it
had to be added to every place that asks "can this side use its Special" —
the SPECIAL button, the League opponent's move search, and the battle card
that has to explain why a full gauge still will not fire.

Three mechanics had no representation at all before this blade:

  * `debuff_ward` — `debuff_immune` is a permanent on/off flag. Horn of Purity
    is dormant, wakes on the FIRST debuff, covers for 2 rounds, pays out, and
    goes dormant again. A boolean cannot hold that.
  * `purification_domain` — a four-round field, replacing the old damage zone.
    It owns conversion, marks, temporary stability and enemy penalties.
  * `gain_counter` — a plain numeric resource. `stacking_buff` always steps by
    one and grants a stat with it; Purifier Charge moves in twenties and
    grants nothing directly.

The suite drives a REAL AbilityEngine, StatusManager and DamageFilter, and
takes the real damage resolver's word for what a clash is.

Run:  python3 tools/sim_kirindael.py
"""
import json
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
from cogs.battle import special_gate as SG                    # noqa: E402
from cogs.battle.damage_filter import DamageFilter            # noqa: E402
from cogs.battle.damage_rules import calc_damage, resolve_special  # noqa: E402
from cogs.battle.status_manager import StatusManager          # noqa: E402
from cogs.core.constants import (MOVE_ATTACK, SPECIAL_GAUGE_MAX)  # noqa: E402
from utils.database import get_beyblade, load_beyblades       # noqa: E402
from utils import bey_levels as BL                            # noqa: E402

NAME = "Kirindael"
K = get_beyblade(NAME)
ALL = load_beyblades()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAIN = get_beyblade("Dead Phoenix")      # a blade with no second resource


class _Chain:
    def resolve(self, *a, **k):
        return []


class _Stamina:
    def __init__(self):
        self.gauge = {"p": 0, "e": 0}
        self.stamina = {"p": 15.0, "e": 15.0}
        self.max_stamina = {"p": 15.0, "e": 15.0}


class FakeSession:
    def __init__(self, blade, hp=1000, ehp=1000):
        self.blades = {"p": blade, "e": blade}
        self.hp = {"p": hp, "e": ehp}
        self.max_hp_per_player = {"p": 1000, "e": 1000}
        self.max_hp = 1000
        self.last_moves = {}
        self.moves = {}
        self.round = 1
        self.status = StatusManager(self)
        self.status_manager = self.status
        self.chain_handler = _Chain()
        self.stamina_manager = _Stamina()
        self.ability = None


def engine(blade=K, hp=1000, ehp=1000):
    s = FakeSession(blade, hp, ehp)
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
    e.ward_cfg = {}
    e.ward_turns = {}
    e.zones = []
    e.damage_filter = DamageFilter(s)
    s.ability = e
    return e, s


def armed():
    """An engine with Kirindael's setup rule already fired, as a battle would."""
    e, s = engine()
    e._fire("setup", "p", "e", K, "passive", "", 0, 0, [])
    return e, s


def curse(e, stat="attack", amount=20):
    """The opponent aims a debuff at Kirindael."""
    logs = []
    e._run_ops({"do": [{"op": "enemy_debuff", "stat": stat,
                        "amount": amount, "turns": 3}]},
               "Enemy Curse", "e", "p", MOVE_ATTACK, 0, 0, logs)
    return logs


def charge(e):
    return e.counters.get(("p", "purifier_charge"), 0)


def main() -> int:
    # ── 1. the card ─────────────────────────────────────────────────────────
    print("\n── 1. the card matches the spec ─────────────────────────────────")
    check("Kirindael is in the roster", K is not None)
    check("rarity Exclusive", K["rarity"] == "Exclusive", K.get("rarity"))
    check("Balance type", K["type"] == "Balance", K.get("type"))
    check("Dual Spin", K["spin_direction"] == "Dual", K.get("spin_direction"))
    check("its id is unique",
          sum(1 for b in ALL.values() if b.get("id") == K["id"]) == 1, K["id"])
    check("the supplied art is stored whole, query string included",
          "1541737368514732062/IMG_1439.jpg" in K["image_url"]
          and "hm=" in K["image_url"])

    st = K["stats"]
    check("ATK 123, DEF 103, STA 113, HP 123 exactly as supplied",
          (st["attack"], st["defense"], st["stamina"], st["hp"])
          == (123, 103, 113, 123), st)
    check("those four total 462, the number on the card",
          st["attack"] + st["defense"] + st["stamina"] + st["hp"] == 462)
    # `special` was NOT in the supplied spec. The game requires all five and it
    # drives Special scaling, so it is an authored assumption — checked here so
    # it is visible rather than buried.
    check("special is 160 — the fifth stat the spec omitted, chosen to land "
          "the blade inside the Exclusive band",
          st["special"] == 160, st.get("special"))
    others = [sum(b["stats"].values()) for n, b in ALL.items()
              if b.get("rarity") == "Exclusive" and n != NAME]
    check("...and the resulting total sits with the other Exclusives",
          min(others) - 30 <= sum(st.values()) <= max(others) + 10,
          (sum(st.values()), min(others), max(others)))

    at100 = BL.stats_at(K, 100, {})
    check("no stat is pinned to the level-100 cap",
          all(v < BL.STAT_CAP for v in at100.values()), at100)

    names = [a["name"] for a in K["abilities"]]
    check("all three abilities are on the plural list the engine reads",
          names == ["Horn of Purity", "Spark Rush", "Lightning Purifier"],
          names)
    check("the singular mirrors the first of them",
          K["ability"]["name"] == K["abilities"][0]["name"])
    e, _ = engine()
    check("its rules compile", len(e._rules_for(K, "p")) == 4)

    # ── 2. the second Special gate ──────────────────────────────────────────
    print("\n── 2. two resources, not one bigger number ──────────────────────")
    check("the blade declares a second requirement",
          SG.requirement(K) == {"counter": "purifier_charge", "value": 60,
                                "cooldown_name": "", "label": "Purifier Charge",
                                "emoji": "⚡"},
          SG.requirement(K))
    check("every other blade declares none", SG.requirement(PLAIN) is None)

    e, s = engine()
    check("an empty gauge is refused first, and says so",
          "gauge" in (SG.blocked_reason(s, "p", K, 0, SPECIAL_GAUGE_MAX) or ""))
    msg = SG.blocked_reason(s, "p", K, SPECIAL_GAUGE_MAX, SPECIAL_GAUGE_MAX)
    check("a FULL gauge with no charge is still refused — the second gate is "
          "real", msg is not None, msg)
    check("...and the refusal names the resource, so a locked button explains "
          "itself", "Purifier Charge" in (msg or ""), msg)
    e.counters[("p", "purifier_charge")] = 59
    check("59 of 60 is still not enough",
          SG.blocked_reason(s, "p", K, SPECIAL_GAUGE_MAX, SPECIAL_GAUGE_MAX))
    e.counters[("p", "purifier_charge")] = 60
    check("both full unlocks it",
          SG.ready(s, "p", K, SPECIAL_GAUGE_MAX, SPECIAL_GAUGE_MAX))
    check("an ordinary blade is never held back by a gate it never declared",
          SG.ready(s, "p", PLAIN, SPECIAL_GAUGE_MAX, SPECIAL_GAUGE_MAX))
    check("a malformed requirement fails OPEN, not into an unusable Special",
          SG.requirement({"special_requires": {"counter": "", "value": "x"}})
          is None)

    # ── 3. Horn of Purity ───────────────────────────────────────────────────
    print("\n── 3. Horn of Purity — purify, bank, cover, repeat ──────────────")
    e, s = engine()
    check("the ward is dormant until setup runs", not e.ward_cfg)
    e, s = armed()
    check("setup arms it before the first debuff can arrive", bool(e.ward_cfg))

    logs = curse(e)
    check("the first debuff is purified rather than applied",
          s.status.get_buff_bonus("p", "attack") == 0,
          s.status.get_buff_bonus("p", "attack"))
    check("...and it says so", any("purified" in ln for ln in logs), logs)
    check("purifying banks +25 Purifier Charge", charge(e) == 25, charge(e))
    check("cover stands for 2 rounds", e.ward_turns.get("p") == 2)

    logs = curse(e, "defense")
    check("a second debuff inside the window is also blocked",
          s.status.get_buff_bonus("p", "defense") == 0)
    check("...but does NOT pay out again — the charge cannot be farmed by "
          "spamming debuffs", charge(e) == 25, charge(e))

    t1 = e.tick_extras()
    check("after one round the cover is still up",
          e.ward_turns.get("p") == 1 and not any("fades" in x for x in t1))
    t2 = e.tick_extras()
    check("after two it lapses, and announces that it can purify again",
          "p" not in e.ward_turns and any("purify again" in x for x in t2), t2)

    curse(e)
    check("the next debuff re-arms it and pays out again",
          charge(e) == 50 and e.ward_turns.get("p") == 2, charge(e))

    # A blade with no ward must be unaffected by any of this.
    e2, s2 = engine(PLAIN)
    curse(e2)
    check("a blade with no ward still takes its debuffs normally",
          s2.status.get_buff_bonus("p", "attack") < 0,
          s2.status.get_buff_bonus("p", "attack"))

    # ── 4. Spark Rush ───────────────────────────────────────────────────────
    print("\n── 4. Spark Rush — the clash, because there is no clash winner ──")
    _, _, matchup, _ = calc_damage(MOVE_ATTACK, K["stats"], K["stats"],
                                   K, MOVE_ATTACK)
    check("the REAL resolver calls Attack-vs-Attack a MIRROR for both sides — "
          "no winner exists to trigger on", matchup == "mirror", matchup)

    e, s = armed()
    dmg, _, logs = e.apply("p", "e", K, K, MOVE_ATTACK, "mirror", 100, 0)
    check("a clash sparks: +25% damage (100 -> 125)", dmg == 125, dmg)
    check("...and banks +25 Purifier Charge", charge(e) == 25, charge(e))
    e, s = armed()
    e.apply("p", "e", K, K, MOVE_ATTACK, "win", 100, 0)
    check("winning an ordinary Attack still builds relaunch momentum",
          charge(e) == 10, charge(e))

    # ── 5. reaching 60 the way the blade actually does ──────────────────────
    print("\n── 5. the new 60-charge gate ───────────────────────────────────")
    e, s = armed()
    for _ in range(2):
        curse(e)
        e.tick_extras()
        e.tick_extras()
    check("two purifications bank 50", charge(e) == 50, charge(e))
    s.stamina_manager.gauge["p"] = SPECIAL_GAUGE_MAX
    check("50 charge is still below the Special gate",
          not SG.ready(s, "p", K, SPECIAL_GAUGE_MAX, SPECIAL_GAUGE_MAX))
    e.apply("p", "e", K, K, MOVE_ATTACK, "win", 100, 0)
    check("one ordinary Attack win lifts it exactly to 60",
          charge(e) == 60, charge(e))
    check("60 charge plus a full gauge unlocks Lightning Purifier",
          SG.ready(s, "p", K, SPECIAL_GAUGE_MAX, SPECIAL_GAUGE_MAX))
    for _ in range(5):
        e.apply("p", "e", K, K, MOVE_ATTACK, "mirror", 100, 0)
    check("the counter still keeps its existing 100-point storage cap",
          charge(e) == 100, charge(e))

    # ── 6. Lightning Purifier ───────────────────────────────────────────────
    from cogs.battle import purification as P
    from cogs.battle.stability_manager import StabilityManager
    import math
    print("\n── 6. Lightning Purifier — damage and four-round Domain ───────")
    hits, dph, flav, _ = resolve_special(K, K["stats"]["special"])
    expected = math.ceil(145 + .4*K["stats"]["attack"] + .5*K["stats"]["defense"] + .8*K["stats"]["stamina"])
    check("formula resolves the three stats exactly once", hits == 1 and dph == expected)
    check("formula ignores legacy special-stat scaling", resolve_special(K, 99999)[1] == expected)
    e, s = armed()
    s.stability_manager = StabilityManager(s.blades, {})
    s.status.add_buff("p", "attack", -20, 4)
    s.status.apply_burn("p", {"burn_damage_per_turn": 20, "burn_duration": 3})
    e.counters[("p", "purifier_charge")] = 60
    dmg, _, logs = e.apply("p", "e", K, K, "special", "win", dph, 0)
    check("the Domain opens for four rounds", P.active(s, "p")["turns"] == 4)
    check("casting clears debuffs and burn", not s.status.active_buffs["p"] and not s.status.burn_stacks["p"])
    check("casting spends Purifier Charge", charge(e) == 0)
    check("no old damage zone survives", not e.zones)
    check("temporary stability raises bar and capacity", s.stability_manager.max["p"] == 115 and s.stability_manager.stability["p"] == 115)
    check("owner ATK and enemy DEF apply", s.status.get_buff_bonus("p", "attack") == round(K["stats"]["attack"]*.2) and s.status.get_buff_bonus("e", "defense") == -round(K["stats"]["defense"]*.05))
    curse(e, "stamina", 20)
    check("Domain overrides Horn immunity and halves debuffs", s.status.get_buff_bonus("p", "stamina") == -10)
    check("conversion banks five percent", P.active(s, "p")["conversion"] == 5)
    for _ in range(4):
        curse(e, "stamina", 20)
    check("conversion caps at fifteen", P.active(s, "p")["conversion"] == 15)
    check("a blocked hit preserves conversion", P.amplify(s, "p", 0, []) == 0 and P.active(s, "p")["conversion"] == 15)
    check("successful damage spends conversion once", P.amplify(s, "p", 100, []) == 115 and P.amplify(s, "p", 100, []) == 100)
    check("healing is reduced once", P.heal_amount(s, "e", 100) == 75 and P.heal_amount(s, "p", 100) == 100)
    s.stability_manager.apply_attack_cost("p", True)
    s.stability_manager.apply_attack_cost("e", True)
    check("actual action costs apply -25%/+30%", s.stability_manager.stability["p"] == 107 and s.stability_manager.stability["e"] == 87)
    hp = s.hp["e"]
    P.mark(s, "e", ("special",), logs)
    P.mark(s, "e", ("special",), logs)
    check("multi-hit special grants only one mark", P.active(s, "p")["marks"] == 1 and s.hp["e"] == hp)
    P.mark(s, "e", ("skill", "test"), logs)
    judgment = math.ceil(40 + .15 * P.effective_stats(s, "p")["attack"])
    check("second mark deals scaled true damage", s.hp["e"] == hp-judgment)
    P.mark(s, "e", ("skill", "another"), logs)
    check("judgment only fires once per Domain", s.hp["e"] == hp-judgment)
    for _ in range(3):
        e.tick_extras()
    check("Domain survives first three round ends", P.active(s, "p")["turns"] == 1)
    e.tick_extras()
    check("fourth round cleans up all Domain state", not P.active(s, "p") and s.stability_manager.max["p"] == 100 and s.stability_manager.stability["p"] == 92)
    check("enemy stat and healing penalties expire", s.status.get_buff_bonus("e", "defense") == 0 and P.heal_amount(s, "e", 100) == 100)

    # ── 7. the primitives on their own ──────────────────────────────────────
    print("\n── 7. the new primitives, in isolation ──────────────────────────")
    e, s = engine()
    e._run_ops({"do": [{"op": "gain_counter", "name": "c", "amount": 30,
                        "max": 50}]}, "T", "p", "e", "attack", 0, 0, [])
    e._run_ops({"do": [{"op": "gain_counter", "name": "c", "amount": 30,
                        "max": 50}]}, "T", "p", "e", "attack", 0, 0, [])
    check("gain_counter accumulates and respects its cap",
          e.counters[("p", "c")] == 50, e.counters[("p", "c")])
    e._run_ops({"do": [{"op": "gain_counter", "name": "c", "amount": -70,
                        "max": 50}]}, "T", "p", "e", "attack", 0, 0, [])
    check("...and never goes negative", e.counters[("p", "c")] == 0)

    e, s = engine()
    e._run_ops({"do": [{"op": "create_zone", "turns": 3, "hits": 3,
                        "dmg": 10, "mult": 1.0}]},
               "Z", "p", "e", "special", 0, 0, [])
    for _ in range(3):
        e.tick_extras()
    check("a zone with hits == turns strikes every round and totals right",
          1000 - s.hp["e"] == 30, 1000 - s.hp["e"])

    e, s = engine()
    e._run_ops({"do": [{"op": "create_zone", "turns": 5, "hits": 2,
                        "dmg": 33, "mult": 1.0}]},
               "Z", "p", "e", "special", 0, 0, [])
    for _ in range(5):
        e.tick_extras()
    check("an awkward split still sums to the stated total (2 x 33 = 66)",
          1000 - s.hp["e"] == 66, 1000 - s.hp["e"])
    check("...and the zone is gone afterwards", not e.zones)

    # Plain permanent immunity has to keep working exactly as it did.
    e, s = engine(PLAIN)
    e._run_ops({"do": [{"op": "debuff_immune"}]}, "T", "p", "e",
               "attack", 0, 0, [])
    curse(e)
    check("the original permanent debuff_immune is untouched by the ward",
          s.status.get_buff_bonus("p", "attack") == 0)

    # ── 8. it is actually wired in ──────────────────────────────────────────
    print("\n── 8. every gate consults the same rule ─────────────────────────")
    sess_src = open(os.path.join(ROOT, "cogs/battle/session.py"),
                    encoding="utf-8").read()
    check("the SPECIAL button asks special_gate, not its own gauge check",
          "special_gate.blocked_reason" in sess_src
          and "gauge < SPECIAL_GAUGE_MAX" not in sess_src)
    check("the button surfaces the reason instead of a bare refusal",
          "_special_block" in sess_src)
    check("the battle card shows the second resource, so a locked Special is "
          "explainable", "special_gate.progress" in sess_src)
    story_src = open(os.path.join(ROOT, "cogs/story/story_ai.py"),
                     encoding="utf-8").read()
    check("the League opponent is held to the same gate as the player",
          "special_gate.ready" in story_src)
    eng_src = open(os.path.join(ROOT, "cogs/abilities/ability_engine.py"),
                   encoding="utf-8").read()
    for op in ("debuff_ward", "create_zone", "gain_counter"):
        check(f'"{op}" is an op the engine dispatches',
              f'kind == "{op}"' in eng_src)
    # The colon matters: the docstring on _debuff_blocked quotes the old
    # pattern to explain why it was replaced, so a bare substring search finds
    # its own explanation and reports a bug that is not there.
    check("all three debuff paths go through ONE ward check",
          eng_src.count("_debuff_blocked(") >= 4
          and "if self.debuff_immune.get(okey):" not in eng_src
          and "not self.debuff_immune.get(okey):" not in eng_src)
    check("zones are ticked every round, not merely created",
          "_tick_zones()" in eng_src and "def _tick_zones" in eng_src)

    blob = json.dumps(K)
    for token in ("debuff_ward", "purification_domain", "gain_counter",
                  "special_requires", "on_attack_mirror", "reset_counter"):
        check(f'"{token}" is used in Kirindael\'s own record',
              f'"{token}"' in blob)

    # ── 9. the roster still compiles ────────────────────────────────────────
    print("\n── 9. the roster still compiles ─────────────────────────────────")
    e, _ = engine()
    broken = []
    for n, b in ALL.items():
        try:
            e._rules_for(b, "p")
        except Exception as exc:                                 # noqa: BLE001
            broken.append((n, str(exc)[:60]))
    check("every blade in the roster still compiles its abilities",
          not broken, broken[:3])
    # Cosmic Phoenix deliberately reuses `special_requires` too (its
    # cooldown-style shape rather than Kirindael's counter-threshold one) —
    # this only needs to catch a blade picking the block up BY ACCIDENT.
    check("no OTHER blade accidentally picked up a second Special gate",
          set(n for n, b in ALL.items() if SG.requirement(b))
          == {NAME, "Cosmic Phoenix"},
          [n for n, b in ALL.items() if SG.requirement(b)])

    print(f"\n{'='*66}\n  {PASS} passed, {FAIL} failed\n{'='*66}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
