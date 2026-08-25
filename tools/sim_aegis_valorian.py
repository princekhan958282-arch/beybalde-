#!/usr/bin/env python3
"""
tools/sim_aegis_valorian.py — Aegis Valorian, and the four primitives it needed.

Why this suite exists
---------------------
Aegis Valorian was authored against triggers and ops that LOOKED sufficient and
were not. Four things were wrong, none of them raised, and every one of them
would have shipped a blade whose card reads correctly and whose ability does
nothing — the exact failure this project already paid for once ("blade
abilities are dead in boss fights for 41% of the roster"):

  1. `on_defense_win` had ZERO users in the whole roster. Royal Guard's entire
     stack engine hangs off it, so it was proven against the real damage
     resolver BEFORE the blade was written. (It does fire: Defense beating
     Attack returns matchup "win", which dispatches it.)

  2. `buff` had no percentage form. `{"op": "buff", "pct": 20}` fell through to
     `op.get("amount", val)`, val defaults to 0, and Champion's Resolve granted
     **+0 Attack** — silently, with a log line that cheerfully said "+0".

  3. Rule-level gates are spelled `if`; only OP-level gates use `_if`. A rule
     carrying `_if` has NO gate at all, so Royal Guard's damage cut applied on
     every incoming hit whether or not it had chosen Defense.

  4. `hp_below_pct` compares against a FRACTION despite its name. Writing the
     obvious `40` asked "is my HP below 4000%" — always true — so Champion's
     Resolve fired at full health.

Sections 2-5 are the behaviour. Sections 6-7 are those four traps, kept as
checks so the next blade cannot fall into them quietly.

Driven through a REAL AbilityEngine, StatusManager, DamageFilter and the REAL
damage resolver — the technique sim_artemis_roze.py established, with the
session surface widened to whatever `apply()` actually touches.

Run:  python3 tools/sim_aegis_valorian.py
"""
import json
import os
import random
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
from cogs.battle.damage_filter import DamageFilter            # noqa: E402
from cogs.battle.damage_rules import calc_damage              # noqa: E402
from cogs.battle.status_manager import StatusManager          # noqa: E402
from cogs.core.constants import MOVE_ATTACK, MOVE_DEFENSE     # noqa: E402
from utils.database import get_beyblade, load_beyblades       # noqa: E402
from utils import availability as AVAIL                       # noqa: E402
from utils import bey_levels as BL                            # noqa: E402
from utils import spin_mode as SM                             # noqa: E402

NAME = "Aegis Valorian"
AV = get_beyblade(NAME)
ALL = load_beyblades()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BALANCE = SM.resolve({SM.K_MODE: {NAME: "Balance"}}, AV)
DEFENCE = SM.resolve({SM.K_MODE: {NAME: "Defense"}}, AV)


class _Chain:
    def resolve(self, *a, **k):
        return []


class _Stamina:
    """Only what `stamina_cost_reduction` touches.

    Needed because that op reaches for `session.stamina_manager` inside a
    try/except — with no stamina manager it swallows the failure and the
    ability silently does nothing, which is precisely the shape of bug this
    suite exists to catch, so the harness has to provide one.
    """

    def __init__(self):
        self.stamina = {"p": 15.0, "e": 15.0}
        self.max_stamina = {"p": 15.0, "e": 15.0}
        self.gauge = {"p": 0, "e": 0}


class FakeSession:
    """Everything `AbilityEngine.apply()` actually reaches for.

    Wider than sim_artemis_roze's because Royal Guard runs on the DEFENDER's
    phase, which goes through DamageFilter — and that reads back through
    `session.ability`.
    """

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


def engine(blade, hp=1000, ehp=1000):
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
    e.damage_filter = DamageFilter(s)
    s.ability = e
    return e, s


def block(e, s, times=1):
    """Aegis presses Defense and the block lands."""
    s.moves = {"p": MOVE_DEFENSE, "e": MOVE_ATTACK}
    for _ in range(times):
        e.apply("p", "e", s.blades["p"], s.blades["e"], MOVE_DEFENSE, "win", 0, 0)


def incoming(e, s, dmg, my_move=MOVE_DEFENSE):
    """One enemy Attack landing on Aegis, who chose `my_move` this round."""
    s.moves = {"p": my_move, "e": MOVE_ATTACK}
    out, _, logs = e.apply("e", "p", s.blades["e"], s.blades["p"],
                           MOVE_ATTACK, "win", dmg, 0)
    return out, logs


def main() -> int:
    # ── 1. the card ─────────────────────────────────────────────────────────
    print("\n── 1. the card matches the spec ─────────────────────────────────")
    check("Aegis Valorian is in the roster", AV is not None)
    check("rarity Ultimate", AV["rarity"] == "Ultimate", AV.get("rarity"))
    check("its id is unique",
          sum(1 for b in ALL.values() if b.get("id") == AV["id"]) == 1, AV["id"])
    check("the supplied art is stored whole, query string included",
          "1541620473287286875/Aegis_Valorian.png" in AV["image_url"]
          and "hm=" in AV["image_url"])
    names = [a["name"] for a in AV["abilities"]]
    check("all three abilities are on the plural list the engine reads",
          names == ["Royal Guard", "Champion's Resolve", "Valorian Judgment"],
          names)
    # The recurring trap: the engine runs `abilities`; the singular `ability`
    # is display only. A blade authored with the singular alone has an ability
    # that never fires.
    check("the singular mirrors the first of them",
          AV["ability"]["name"] == AV["abilities"][0]["name"])

    # ── 1b. two forms, two stat lines ───────────────────────────────────────
    print("\n── 1b. two forms, and they are genuinely different ──────────────")
    check("it is flagged dual", SM.is_dual(AV))
    check("both forms are offered", SM.modes(AV) == ["Balance", "Defense"],
          SM.modes(AV))
    check("Balance is the default form", SM.chosen({}, AV) == "Balance")
    check("the forms have DIFFERENT stats — the whole point of the switch",
          BALANCE["stats"] != DEFENCE["stats"])
    check("Guardian form is Balance type", BALANCE["type"] == "Balance")
    check("Bulwark form is Defense type", DEFENCE["type"] == "Defense")
    check("Bulwark trades Attack for Defense",
          DEFENCE["stats"]["defense"] > BALANCE["stats"]["defense"]
          and DEFENCE["stats"]["attack"] < BALANCE["stats"]["attack"])
    check("each form has its own labelled button",
          SM.label(AV, "Balance") != SM.label(AV, "Defense")
          and "Guardian" in SM.label(AV, "Balance")
          and "Bulwark" in SM.label(AV, "Defense"))
    # spin_mode.py's own rule: the top-level record must stay a complete,
    # valid blade for every reader that has never heard of dual mode.
    check("the top-level record mirrors the default form",
          AV["stats"] == BALANCE["stats"] and AV["type"] == BALANCE["type"])
    check("an unknown stored form falls back rather than breaking",
          SM.chosen({SM.K_MODE: {NAME: "Nonsense"}}, AV) == "Balance")

    # sim_levels.py only cap-checks a blade's TOP-LEVEL stats, so a mode block
    # can breach the ceiling with nothing to catch it. Checked here per form.
    for mode, form in (("Balance", BALANCE), ("Defense", DEFENCE)):
        at100 = BL.stats_at(form, 100, {})
        check(f"{mode} form stays under the level-100 cap "
              f"(sim_levels only checks the top-level block)",
              all(v < BL.STAT_CAP for v in at100.values()), at100)

    # ── 2. the trigger the whole kit hangs off ──────────────────────────────
    print("\n── 2. on_defense_win — proven, not assumed ──────────────────────")
    st = BALANCE["stats"]
    _, _, matchup, _ = calc_damage(MOVE_DEFENSE, st, st, BALANCE, MOVE_ATTACK)
    check("the REAL resolver calls a landed block a 'win' for the defender",
          matchup == "win", matchup)
    e, s = engine(BALANCE)
    block(e, s)
    check("...and that dispatches on_defense_win, which NO other blade in the "
          "roster exercises",
          e.counters.get(("p", "guard_stack")) == 1,
          e.counters.get(("p", "guard_stack")))

    # ── 3. Royal Guard ──────────────────────────────────────────────────────
    print("\n── 3. Royal Guard — soften, then bank ───────────────────────────")
    e, s = engine(BALANCE)
    took, logs = incoming(e, s, 100, my_move=MOVE_DEFENSE)
    check("choosing Defense cuts an incoming hit by 15% (100 -> 85)",
          took == 85, took)
    check("...and says so", any("reduced by 15" in ln for ln in logs))

    e, s = engine(BALANCE)
    took, logs = incoming(e, s, 100, my_move=MOVE_ATTACK)
    check("NOT choosing Defense cuts nothing — the gate is real",
          took == 100 and not any("reduced by 15" in ln for ln in logs), took)

    e, s = engine(BALANCE)
    per = round(BALANCE["stats"]["defense"] * 0.05)
    for i in (1, 2, 3):
        block(e, s)
        check(f"block {i} banks a Guard Stack worth 5% Defense (+{per})",
              e.counters.get(("p", "guard_stack")) == i
              and s.status.get_buff_bonus("p", "defense") == per * i,
              (e.counters.get(("p", "guard_stack")),
               s.status.get_buff_bonus("p", "defense")))
    block(e, s, 3)
    check("the stack caps at 3, however many blocks land",
          e.counters.get(("p", "guard_stack")) == 3
          and s.status.get_buff_bonus("p", "defense") == per * 3)

    e, s = engine(BALANCE)
    s.moves = {"p": MOVE_DEFENSE, "e": MOVE_ATTACK}
    e.apply("p", "e", BALANCE, BALANCE, MOVE_DEFENSE, "lose", 0, 0)
    check("a block that does NOT land banks nothing",
          e.counters.get(("p", "guard_stack"), 0) == 0)

    # ── 4. Champion's Resolve ───────────────────────────────────────────────
    print("\n── 4. Champion's Resolve — once, and only when it's bad ─────────")
    want_atk = round(BALANCE["stats"]["attack"] * 0.20)
    for hp, label, expect in ((1000, "full health", 0), (410, "41% HP", 0),
                              (390, "39% HP", want_atk)):
        e, s = engine(BALANCE, hp=hp)
        s.moves = {"p": MOVE_ATTACK, "e": MOVE_ATTACK}
        e.apply("p", "e", BALANCE, BALANCE, MOVE_ATTACK, "win", 0, 0)
        got = s.status.get_buff_bonus("p", "attack")
        check(f"at {label}: Attack {'+%d' % expect if expect else 'unchanged'}",
              got == expect, got)

    e, s = engine(BALANCE, hp=390)
    s.moves = {"p": MOVE_ATTACK, "e": MOVE_ATTACK}
    e.apply("p", "e", BALANCE, BALANCE, MOVE_ATTACK, "win", 0, 0)
    check("it also cuts stamina spent by 20%",
          abs(getattr(s.stamina_manager, "drain_reduction", {}).get("p", 0)
              - 0.20) < 1e-9,
          getattr(s.stamina_manager, "drain_reduction", None))
    before = s.status.get_buff_bonus("p", "attack")
    s.hp["p"] = 900
    e.apply("p", "e", BALANCE, BALANCE, MOVE_ATTACK, "win", 0, 0)
    s.hp["p"] = 100
    e.apply("p", "e", BALANCE, BALANCE, MOVE_ATTACK, "win", 0, 0)
    check("once per battle really means once — healing up and dropping again "
          "does not stack a second copy",
          s.status.get_buff_bonus("p", "attack") == before,
          (before, s.status.get_buff_bonus("p", "attack")))

    # ── 5. Valorian Judgment ────────────────────────────────────────────────
    print("\n── 5. Valorian Judgment — 180/210/240/280%, then spend ──────────")
    for form, fname in ((BALANCE, "Guardian"), (DEFENCE, "Bulwark")):
        atk = form["stats"]["attack"]
        base = form["special_move"]["damage_per_hit"]
        check(f"{fname}: the Special is authored at 180% of ITS OWN Attack "
              f"({base} vs {atk})",
              abs(base / atk - 1.80) < 0.02, base / atk)
        check(f"{fname}: it is a single hit, so the stack bonus lands on the "
              f"whole move", form["special_move"]["hits"] == 1)
        check(f"{fname}: hits x damage is the stated total",
              form["special_move"]["hits"]
              * form["special_move"]["damage_per_hit"]
              == form["special_move"]["total_damage"])

    atk = BALANCE["stats"]["attack"]
    base = BALANCE["special_move"]["damage_per_hit"]
    for stacks, want in ((0, 1.80), (1, 2.10), (2, 2.40), (3, 2.80)):
        e, s = engine(BALANCE)
        block(e, s, stacks)
        banked = s.status.get_buff_bonus("p", "defense")
        s.moves = {"p": "special", "e": MOVE_ATTACK}
        dmg, _, _ = e.apply("p", "e", BALANCE, BALANCE, "special", "win", base, 0)
        check(f"{stacks} stacks -> {want*100:.0f}% of Attack "
              f"(got {dmg / atk * 100:.1f}%)",
              abs(dmg / atk - want) < 0.03, dmg)
        check(f"...and the {stacks} stacks are SPENT — counter zeroed and the "
              f"Defense they granted ({banked}) handed back",
              e.counters.get(("p", "guard_stack")) == 0
              and s.status.get_buff_bonus("p", "defense") == 0,
              (e.counters.get(("p", "guard_stack")),
               s.status.get_buff_bonus("p", "defense")))

    # ── 6. spend_stacks, precisely ──────────────────────────────────────────
    print("\n── 6. spend_stacks takes back its own, and nothing else ─────────")
    e, s = engine(BALANCE)
    block(e, s, 3)
    s.status.add_buff("p", "defense", 50, 99)          # a part / avatar bonus
    check("an unrelated Defense buff coexists with the stacks",
          s.status.get_buff_bonus("p", "defense") == per * 3 + 50)
    logs = []
    e._run_ops({"do": [{"op": "spend_stacks", "name": "guard_stack",
                        "label": "Guard Stack"}]},
               "Valorian Judgment", "p", "e", "special", 0, 0, logs)
    check("spending takes back ONLY what the stacks granted — a stat-wide "
          "clear would have eaten the other buff too",
          s.status.get_buff_bonus("p", "defense") == 50,
          s.status.get_buff_bonus("p", "defense"))
    check("...and it says how many were spent",
          any("spent 3" in ln for ln in logs), logs)
    logs = []
    e._run_ops({"do": [{"op": "spend_stacks", "name": "guard_stack"}]},
               "Valorian Judgment", "p", "e", "special", 0, 0, logs)
    check("spending nothing is a quiet no-op, not a crash or a false claim",
          s.status.get_buff_bonus("p", "defense") == 50 and not logs)
    check("clear_source with no tag refuses to clear everything",
          s.status.clear_source("p", "") == 0
          and s.status.get_buff_bonus("p", "defense") == 50)

    # ── 7. the four traps, kept shut ────────────────────────────────────────
    print("\n── 7. the traps that nearly shipped this blade broken ───────────")
    e, s = engine(BALANCE)
    logs = []
    e._run_ops({"do": [{"op": "buff", "stat": "attack", "pct": 20,
                        "turns": 99}]},
               "T", "p", "e", "attack", 0, 0, logs)
    check("`buff` understands a percentage — it used to silently grant +0",
          s.status.get_buff_bonus("p", "attack") == want_atk,
          s.status.get_buff_bonus("p", "attack"))
    e, s = engine(BALANCE)
    e._run_ops({"do": [{"op": "buff", "stat": "attack", "amount": 7,
                        "turns": 99}]},
               "T", "p", "e", "attack", 0, 0, [])
    check("...and a flat amount still works exactly as before",
          s.status.get_buff_bonus("p", "attack") == 7)

    # Rule-level gates are `if`; `_if` is op-level only and is IGNORED on a
    # rule, which turns the gate into decoration.
    rule_if = [(n, a["name"]) for n, b in ALL.items()
               for a in (b.get("abilities") or [])
               for r in (a.get("rules") or []) if "_if" in r]
    check("no rule in the roster uses the ignored `_if` key at rule level",
          not rule_if, rule_if[:3])

    # `hp_below_pct` compares a FRACTION. Authoring the obvious `40` meant
    # "below 4000%", which is always true.
    e, s = engine(BALANCE, hp=1000)
    check("an hp threshold written as a percent is read as one, not as "
          "'always true'",
          not e._check({"cond": "hp_below_pct", "value": 40},
                       "p", "e", "attack", "win"))
    check("...and the fraction form the roster already uses still works",
          not e._check({"cond": "hp_below_pct", "value": 0.4},
                       "p", "e", "attack", "win"))
    s.hp["p"] = 300
    check("...both agree when the blade really is hurt",
          e._check({"cond": "hp_below_pct", "value": 40}, "p", "e", "attack", "win")
          and e._check({"cond": "hp_below_pct", "value": 0.4},
                       "p", "e", "attack", "win"))

    # `my_move_is` vs `move_is` on the defensive phase.
    e, s = engine(BALANCE)
    s.moves = {"p": MOVE_DEFENSE, "e": MOVE_ATTACK}
    check("`my_move_is` reads THIS side's choice, not the phase's move",
          e._check({"cond": "my_move_is", "value": "defense"},
                   "p", "e", MOVE_ATTACK, "win")
          and not e._check({"cond": "move_is", "value": "defense"},
                           "p", "e", MOVE_ATTACK, "win"))
    delattr(s, "moves")
    check("...and it falls back to the phase move when a session has no "
          "moves table, so older harnesses keep working",
          e._check({"cond": "my_move_is", "value": "attack"},
                   "p", "e", MOVE_ATTACK, "win"))

    # ── 8. limited, but bound to nobody ─────────────────────────────────────
    print("\n── 8. limited — out of every pool, owned by no one ──────────────")
    check("it is flagged limited", AV.get("limited") is True)
    check("it is bound to NOBODY — no owner_ids", not AV.get("owner_ids"))
    check("nobody can obtain it", not AVAIL.obtainable(AV))
    check("...not even a specific player, since it is not owner-bound",
          not AVAIL.obtainable(AV, 1273889267986468885))
    check("its acquisition window is what shuts it — `limited` alone is only "
          "a display flag", not AVAIL.is_available(AV))

    import cogs.economy.shop as SHOP                            # noqa: E402
    import cogs.spawn.spawn as SPAWN                            # noqa: E402
    from cogs.tournament.tournament import draft_pool           # noqa: E402

    random.seed(19)
    spawned = sum(1 for _ in range(50_000)
                  if (SPAWN._pick_random_beyblade(ALL) or {}).get("name") == NAME)
    check("50,000 wild spawns produce none", spawned == 0, spawned)
    check("it is not in the booster pack pool",
          not any(b.get("name") == NAME for b in SHOP._load_booster_pool()))
    check("...nor the booster hidden pool",
          not any(b.get("name") == NAME for b in SHOP._hidden_drop_pool()))
    check("...nor a tournament draft",
          not any(b.get("name") == NAME for b in draft_pool()))
    # The point of "limited but unbound": you hand it out yourself. Neither
    # gift codes nor admin grants consult obtainable(), so they still can.
    callers = os.popen(
        f"grep -rn 'obtainable(' --include=*.py {ROOT}/cogs "
        f"| grep -v pycache | grep -v 'def obtainable'").read()
    check("no gift-code or admin-grant path gates on obtainable(), so it can "
          "still be handed out by hand",
          "codes/" not in callers and "admin/" not in callers, callers[:200])

    # ── 9. reachability ─────────────────────────────────────────────────────
    print("\n── 9. every primitive is reached from its own JSON ──────────────")
    blob = json.dumps(AV)
    for token in ("on_defense_win", "spend_stacks", "my_move_is",
                  "reduce_damage_pct", "stacking_buff", "per_stack_pct",
                  "stamina_cost_reduction", "counter_at_least",
                  "counter_below", "bonus_damage_pct"):
        check(f'"{token}" is used in its own rules, not invented for this test',
              f'"{token}"' in blob)
    eng_src = open(os.path.join(ROOT, "cogs/abilities/ability_engine.py"),
                   encoding="utf-8").read()
    check("spend_stacks is a real op the engine dispatches",
          'kind == "spend_stacks"' in eng_src)
    check("my_move_is is a real condition the engine evaluates",
          'c == "my_move_is"' in eng_src)
    sm_src = open(os.path.join(ROOT, "cogs/battle/status_manager.py"),
                  encoding="utf-8").read()
    check("StatusManager can tag and revoke a buff by source",
          "def clear_source" in sm_src and "source: str" in sm_src)
    check("stacking_buff tags what it grants, or spending could not undo it",
          'source=f"stack:{cname}"' in eng_src)

    # ── 10. the roster still compiles ───────────────────────────────────────
    print("\n── 10. the roster still compiles ────────────────────────────────")
    e, _ = engine(BALANCE)
    broken = []
    for n, b in ALL.items():
        try:
            e._rules_for(b, "p")
        except Exception as exc:                                 # noqa: BLE001
            broken.append((n, str(exc)[:60]))
    check("every blade in the roster still compiles its abilities",
          not broken, broken[:3])

    print(f"\n{'='*66}\n  {PASS} passed, {FAIL} failed\n{'='*66}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
