#!/usr/bin/env python3
"""
tools/sim_astral_valkyrie.py — the two Astral Valkyrie versions.

Why this suite exists
----------------------
Between them the two kits needed exactly ONE new engine primitive, and the
interesting part is which one:

  * `reduce_damage_pct_turns` — genuinely new. `reduce_damage_pct` already
    existed but is instantaneous: it only touches the hit its own rule is
    resolving. "Take 15% less for 2 turns" cannot be said that way, because a
    blade would have to know in advance it was about to be attacked. Built as
    the deliberate mirror of `reflect_pct_turns` (same [pct, turns] shape,
    same refresh-never-stack rule, same sweep) so the two timed windows on the
    defensive side are one mechanism rather than two.

  * "the NEXT Attack gains +25% ATK" — NOT new, and worth saying why. The
    obvious candidate, `prime_bonus`, primes the next SPECIAL with a flat
    number. A counter armed on the defence and consumed on the next attack is
    the pattern Drakoryn's Iron Lock and Azure Drakonyx's Rampage already use,
    so Wing Reversal is authored, not coded.

  * "3 Special Charge + 3 Stamina" — also not new. Charge gives +50 gauge and
    the bar is 150, so three Charges IS a full gauge: the default, nothing to
    author. The 3 stamina is not the default 4.4, and that is what
    `button_profile` was built for — these are its first two customers, which
    makes this suite the first real end-to-end test of the opt-in seam.

Driven through a REAL AbilityEngine, StatusManager, StaminaManager,
TypeModifiers and StabilityManager — the technique every sim_<blade>.py here
uses (see sim_drakoryn.py, sim_radiant_valkyrie.py).

Run:  python3 tools/sim_astral_valkyrie.py
"""
import os
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


from cogs.abilities import type_system as TS                    # noqa: E402
from cogs.abilities.ability_engine import AbilityEngine          # noqa: E402
from cogs.battle import button_profile as BP                     # noqa: E402
from cogs.battle.attack_manager import AttackManager             # noqa: E402
from cogs.battle.stability_manager import StabilityManager       # noqa: E402
from cogs.battle.stamina_manager import STAMINA_COST             # noqa: E402
from cogs.battle.stamina_manager import StaminaManager           # noqa: E402
from cogs.battle.status_manager import StatusManager             # noqa: E402
from cogs.core.constants import (                                # noqa: E402
    GAUGE_PER_CHARGE, MOVE_ATTACK, MOVE_DEFENSE, MOVE_SPECIAL,
    SPECIAL_GAUGE_MAX,
)
from utils.database import get_beyblade, load_beyblades          # noqa: E402
from utils import bey_levels as BL                               # noqa: E402

STAR_NAME = "Astral Valkyrie — Starbreaker"
SKY_NAME  = "Astral Valkyrie — Skyrend"
STAR = get_beyblade(STAR_NAME)
SKY  = get_beyblade(SKY_NAME)
ALL  = load_beyblades()

DUMMY = {"name": "Dummy", "type": "Balance", "spin_direction": "Left",
        "stats": {"attack": 100, "defense": 100, "stamina": 100, "hp": 100}}


class Sess:
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


def move(s, mv, matchup, mkey="p", okey="e", dmg=0):
    s.last_moves = {mkey: mv, okey: mv}
    return s.ability.apply(mkey, okey, s.blades[mkey], s.blades[okey],
                           mv, matchup, dmg, 0,
                           is_first_hit=True, cumulative_dmg=0, is_last_hit=True)


def incoming(s, dmg, akey="e", dkey="p"):
    """A real hit landing ON `dkey`, so defensive windows are exercised."""
    s.last_moves = {akey: MOVE_ATTACK, dkey: MOVE_ATTACK}
    out, _, _ = s.ability.apply(akey, dkey, s.blades[akey], s.blades[dkey],
                                MOVE_ATTACK, "win", dmg, 0)
    return out


def special(s, mkey="p", okey="e"):
    am = AttackManager.__new__(AttackManager)
    am.session = s
    total, logs = am._resolve_special(mkey, okey, MOVE_SPECIAL,
                                      s.blades[mkey], s.blades[okey], [])
    return total, logs


def main() -> int:
    # ── 1. the cards ────────────────────────────────────────────────────────
    print("\n── 1. both cards match the spec ─────────────────────────────────")
    for name, b, bid, stats in (
        (STAR_NAME, STAR, "BB114",
         {"attack": 152, "defense": 96, "stamina": 88, "special": 148, "hp": 150}),
        (SKY_NAME, SKY, "BB115",
         {"attack": 128, "defense": 104, "stamina": 132, "special": 140, "hp": 145}),
    ):
        check(f"{name} is in the roster", b is not None)
        check(f"{name}: id {bid}, unique",
              b["id"] == bid
              and sum(1 for x in ALL.values() if x.get("id") == bid) == 1)
        check(f"{name}: Legendary, limited, unbound",
              b["rarity"] == "Legendary" and b.get("limited") is True
              and not b.get("owner_ids"))
        check(f"{name}: stats as agreed", b["stats"] == stats, b["stats"])
        at100 = BL.stats_at(b, 100, {})
        check(f"{name}: under the level-100 cap",
              all(v <= BL.STAT_CAP for v in at100.values()), at100)
        check(f"{name}: three abilities on the plural list the engine reads",
              len(b["abilities"]) == 3
              and b["ability"]["name"] == b["abilities"][0]["name"])

    check("Starbreaker keeps its own art, query string included",
          "1542557825836781578" in STAR["image_url"] and "hm=" in STAR["image_url"])
    check("Skyrend keeps its own — two versions, two images",
          "1542557816047542332" in SKY["image_url"]
          and SKY["image_url"] != STAR["image_url"])

    # ── 2. the Special's cost, in the game's own units ──────────────────────
    print("\n── 2. '3 Special Charge + 3 Stamina', in existing units ─────────")
    # Not a new resource: Charge gives +50 and the bar is 150, so three
    # Charges is exactly a full gauge. Authoring anything here would have
    # invented a second currency for a cost the game already expresses.
    check("three Charge presses fill the bar exactly — so '3 Special Charge' "
          "IS the default full gauge",
          GAUGE_PER_CHARGE * 3 == SPECIAL_GAUGE_MAX,
          (GAUGE_PER_CHARGE, SPECIAL_GAUGE_MAX))
    for name, b in ((STAR_NAME, STAR), (SKY_NAME, SKY)):
        check(f"{name}: Special still costs the full gauge",
              BP.special_gauge_cost(b) == SPECIAL_GAUGE_MAX)
        sm = StaminaManager({"p": b})
        check(f"{name}: ...and 3 stamina, not the default "
              f"{STAMINA_COST[MOVE_SPECIAL]}",
              sm.cost_for("p", MOVE_SPECIAL) == 3.0,
              sm.cost_for("p", MOVE_SPECIAL))
        # The seam must not leak into the other buttons.
        check(f"{name}: Attack/Defense are NOT re-priced by that block",
              BP.stamina_cost(b, MOVE_ATTACK, 2.2) == 2.2
              and BP.stamina_cost(b, MOVE_DEFENSE, 2.2) == 2.2)

    # ── 3. Starfall Edge — every 3rd Attack, not the 1st or 2nd ─────────────
    print("\n── 3. Starfall Edge — every 3rd Attack ──────────────────────────")
    s = Sess(STAR, DUMMY)
    atk = STAR["stats"]["attack"]
    bonus = round(atk * 0.35)
    s.stability_manager.stability["p"] -= 10        # room to show the +2
    stab0 = s.stability_manager.stability["p"]

    o1, _, _ = move(s, MOVE_ATTACK, "win", dmg=100)
    check("1st Attack: no bonus", o1 == 100, o1)
    o2, _, _ = move(s, MOVE_ATTACK, "win", dmg=100)
    check("2nd Attack: still none", o2 == 100, o2)
    check("...and no Stability yet either",
          s.stability_manager.stability["p"] == stab0)
    o3, _, _ = move(s, MOVE_ATTACK, "win", dmg=100)
    check(f"3rd Attack: +35% of ATK ({bonus})", o3 == 100 + bonus, o3)
    check("...and +2 Stability on the same trigger",
          s.stability_manager.stability["p"] == stab0 + 2,
          (s.stability_manager.stability["p"], stab0))
    o4, _, _ = move(s, MOVE_ATTACK, "win", dmg=100)
    check("4th Attack: counter reset, no bonus", o4 == 100, o4)
    move(s, MOVE_ATTACK, "win", dmg=100)
    o6, _, _ = move(s, MOVE_ATTACK, "win", dmg=100)
    check("6th Attack: it comes round again", o6 == 100 + bonus, o6)

    # ── 4. Astral Guard — once, below half, and it really expires ──────────
    print("\n── 4. Astral Guard — once per battle, below 50% HP ──────────────")
    s2 = Sess(STAR, DUMMY)
    dbase = s2.blades["p"]["stats"]["defense"]

    s2.hp["p"] = 700                                  # 70%, above the line
    move(s2, MOVE_ATTACK, "win", mkey="p", dmg=0)
    check("above 50% HP it does not fire",
          s2.ability._get_buf_bonus("p", "defense") == 0
          and not s2.ability.resist_windows.get("p"))

    s2.hp["p"] = 400                                  # 40%
    move(s2, MOVE_ATTACK, "win", mkey="p", dmg=0)
    check("below 50%: +20% Defense",
          s2.ability._get_buf_bonus("p", "defense") == round(dbase * 0.20),
          s2.ability._get_buf_bonus("p", "defense"))
    check("...and a 2-turn damage-reduction window opens",
          s2.ability.resist_windows.get("p") == [15.0, 2],
          s2.ability.resist_windows.get("p"))

    # The window has to bite on a REAL incoming hit.
    got = incoming(s2, 100)
    check("an incoming 100 is cut to 85 while braced", got == 85, got)

    # Once per battle: drop lower and it must stay silent.
    s2.ability.resist_windows.pop("p", None)
    s2.hp["p"] = 100
    move(s2, MOVE_ATTACK, "win", mkey="p", dmg=0)
    check("it cannot fire a second time in the same battle",
          not s2.ability.resist_windows.get("p"),
          s2.ability.resist_windows.get("p"))

    # ...and the window expires on the sweep rather than lasting forever.
    s3 = Sess(STAR, DUMMY)
    s3.hp["p"] = 400
    move(s3, MOVE_ATTACK, "win", mkey="p", dmg=0)
    check("armed for 2", s3.ability.resist_windows["p"][1] == 2)
    s3.ability.tick_dmg_amps()
    check("after 1 sweep: still up, 1 turn left",
          s3.ability.resist_windows["p"][1] == 1)
    check("...and still mitigating", incoming(s3, 100) == 85)
    s3.ability.tick_dmg_amps()
    check("after 2 sweeps: gone — a timed window that never expires is a "
          "permanent buff wearing a duration",
          not s3.ability.resist_windows.get("p"),
          s3.ability.resist_windows.get("p"))
    check("...and a hit lands in full again", incoming(s3, 100) == 100)

    # Refresh, never stack.
    s4 = Sess(STAR, DUMMY)
    s4.ability._run_ops({"do": [{"op": "reduce_damage_pct_turns",
                                 "value": 15, "turns": 2}]}, "T", "p", "e",
                        "", 0, 0, [])
    s4.ability._run_ops({"do": [{"op": "reduce_damage_pct_turns",
                                 "value": 15, "turns": 2}]}, "T", "p", "e",
                        "", 0, 0, [])
    check("two overlapping 15% windows stay 15%, not 28% — refreshed, never "
          "stacked", s4.ability.resist_windows["p"][0] == 15.0,
          s4.ability.resist_windows["p"])
    s4.ability._run_ops({"do": [{"op": "reduce_damage_pct_turns",
                                 "value": 400, "turns": 2}]}, "T", "p", "e",
                        "", 0, 0, [])
    check("an absurd value is capped short of total immunity",
          s4.ability.resist_windows["p"][0] == 90.0,
          s4.ability.resist_windows["p"])

    # ── 5. Starbreaker Impact ───────────────────────────────────────────────
    print("\n── 5. Starbreaker Impact — 180% ATK, 25% pierce, +15% ATK ───────")
    # Two different opponents, because the two claims need different
    # conditions and using one for both is how a check passes for the wrong
    # reason. The raw 180% is only readable with NO mitigation in the way; the
    # pierce is only observable when there IS some.
    ATK_FOE = {"name": "Foe", "type": "Attack", "spin_direction": "Left",
              "stats": {"attack": 100, "defense": 100, "stamina": 100, "hp": 100}}
    s5 = Sess(STAR, ATK_FOE, ehp=100000)
    dealt, _ = special(s5, "p", "e")
    check("against an un-mitigated target it deals exactly 180% of Attack",
          dealt == round(atk * 1.80), (dealt, round(atk * 1.80)))
    check("...and leaves +15% Attack behind",
          s5.ability._get_buf_bonus("p", "attack") == round(atk * 0.15),
          s5.ability._get_buf_bonus("p", "attack"))

    # Balance's mitigation is never suppressed, so there is something to cut.
    # An Attack-vs-Attack mirror switches BOTH sides' type bonuses off, which
    # would leave the pierce nothing to do and the check green regardless.
    import copy as _copy
    s5b = Sess(STAR, DUMMY, ehp=100000)
    pierced, plogs = special(s5b, "p", "e")
    check("against a mitigating target the pierce fires",
          any("cuts through" in l and "25" in l for l in plogs), plogs)

    blunt_blade = _copy.deepcopy(STAR)
    for _ab in blunt_blade["abilities"]:
        for _r in _ab["rules"]:
            _r["do"] = [o for o in _r["do"] if o.get("op") != "special_pierce_pct"]
    blunt_blade["ability"] = blunt_blade["abilities"][0]
    s5c = Sess(blunt_blade, DUMMY, ehp=100000)
    blunt, _ = special(s5c, "p", "e")
    check(f"...and it is worth real damage — {pierced} pierced vs {blunt} blunt",
          pierced > blunt, (pierced, blunt))
    check("...while mitigation still applies at all (the pierce is 25%, "
          "not immunity)",
          pierced < dealt, (pierced, dealt))

    # ── 6. Sky Cutter — a real 25% chance, not always and not never ────────
    print("\n── 6. Sky Cutter — 25% of Attacks cut deeper ────────────────────")
    import random
    sky_atk = SKY["stats"]["attack"]
    sky_bonus = round(sky_atk * 0.30)
    random.seed(20260827)
    s6 = Sess(SKY, DUMMY)
    outs = [move(s6, MOVE_ATTACK, "win", dmg=100)[0] for _ in range(400)]
    procs = sum(1 for o in outs if o > 100)
    check(f"it procs sometimes and not always ({procs}/400)",
          0 < procs < 400, procs)
    check(f"...at roughly a quarter of the time ({100*procs/400:.1f}%)",
          0.15 < procs / 400 < 0.35, procs / 400)
    check("a proc is worth exactly +30% of Attack",
          all(o in (100, 100 + sky_bonus) for o in outs),
          sorted(set(outs)))

    # ── 7. Wing Reversal — the NEXT attack, once ───────────────────────────
    print("\n── 7. Wing Reversal — a block becomes a wind-up ─────────────────")
    s7 = Sess(SKY, DUMMY)
    # Sky Cutter's 25% would muddy an exact number, so read the counter and
    # measure the release against a run where it was never armed.
    o_base, _, _ = move(s7, MOVE_ATTACK, "win", dmg=100)
    check("no reversal banked to begin with",
          s7.ability.counters.get(("p", "wing_reversal"), 0) == 0)

    s8 = Sess(SKY, DUMMY)
    move(s8, MOVE_DEFENSE, "win")
    check("a successful Defense banks the reversal",
          s8.ability.counters.get(("p", "wing_reversal")) == 1)
    move(s8, MOVE_DEFENSE, "win")
    check("blocking twice does not stack it — it is the NEXT attack, not the "
          "next two", s8.ability.counters.get(("p", "wing_reversal")) == 1)

    random.seed(7)
    out_rel, _, _ = move(s8, MOVE_ATTACK, "win", dmg=100)
    check("the next Attack is boosted by at least +25% of Attack",
          out_rel >= 100 + round(sky_atk * 0.25), out_rel)
    check("...and the bank is spent",
          s8.ability.counters.get(("p", "wing_reversal")) == 0)

    # ── 8. Skyrend Crash ────────────────────────────────────────────────────
    print("\n── 8. Skyrend Crash — it plants itself ──────────────────────────")
    s9 = Sess(SKY, DUMMY)
    s9.stability_manager.stability["p"] -= 10
    before = s9.stability_manager.stability["p"]
    dealt9, _ = special(s9, "p", "e")
    check("it restores exactly 3 Stability",
          s9.stability_manager.stability["p"] == before + 3,
          (s9.stability_manager.stability["p"], before))
    # `non_damage` zeroes the AUTHORED damage, and Skyrend Crash adds none
    # back — so what lands is whatever the engine floors an empty hit to.
    # `TypeModifiers.apply_defense` floors at 1 (type_system.py:369), so
    # against a defender whose mitigation is live this chips 1 rather than 0.
    # Asserting 0 would be asserting something the engine deliberately does
    # not do; 1 chip on a Special whose entire point is the Stability restore
    # is harmless, and pinning it here means a future change to that floor
    # shows up as a decision rather than a surprise.
    check("...and deals no meaningful damage — its point is the Stability",
          SKY["special_move"].get("non_damage") is True and dealt9 <= 1, dealt9)

    # ── 9. reachability ────────────────────────────────────────────────────
    print("\n── 9. every op the two kits name is in their own JSON ───────────")
    for name, b, wanted in (
        (STAR_NAME, STAR, ("gain_counter", "bonus_damage_stat", "gain_stability",
                           "reset_counter", "buff", "reduce_damage_pct_turns",
                           "special_pierce_pct")),
        (SKY_NAME, SKY, ("bonus_damage_stat", "gain_counter", "reset_counter",
                         "gain_stability")),
    ):
        ops = {o.get("op") for ab in b["abilities"] for r in ab["rules"]
               for o in r.get("do", [])}
        for w in wanted:
            check(f"{name}: '{w}' reachable from its own JSON", w in ops,
                  sorted(ops))
    check("Sky Cutter's 25% is a rule-level `chance`, not a hand-rolled random",
          any(r.get("chance") == 0.25
              for ab in SKY["abilities"] for r in ab["rules"]))
    check("Astral Guard is gated `once: battle` — the only thing stopping it "
          "re-firing every round below half HP",
          any(r.get("once") == "battle"
              for ab in STAR["abilities"] for r in ab["rules"]))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
