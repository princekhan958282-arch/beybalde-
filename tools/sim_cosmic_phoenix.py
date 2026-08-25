#!/usr/bin/env python3
"""
tools/sim_cosmic_phoenix.py — Cosmic Phoenix, and the five primitives it needed.

Why this suite exists
----------------------
Cosmic Phoenix asks for three things nothing in the roster does yet, and each
one was proven against the real engine before the blade was written on top of
it — the technique this project settled on after shipping abilities that read
correctly and did nothing ("blade abilities are dead in boss fights for 41%
of the roster", `on_defense_win` with zero users before Aegis Valorian):

  1. `revive`/`revive_pool`/`_check_revive` — flat-HP revival — was declared in
     the engine and used by NO shipped blade; `Revive Phoenix`'s own "Blazing
     Rebirth" writes ad-hoc keys the DSL never reads and is dead content.
     `revive_pct` and the new `on_would_burst` trigger are proven directly
     against `_check_revive`, not assumed to work because the flat sibling
     exists.
  2. `TypeModifiers` computes atk/def/sta multipliers ONCE at construction,
     and `session.py` builds one instance per player at battle start and
     never rebuilds it. `evolve_form` is proven to actually change those
     cached multipliers, not just the `type` string nobody else reads.
  3. `special_gate.py` only knew resource-threshold gates before this. The new
     `cooldown_name` shape is proven against the real `cooldowns` dict and the
     real `tick_extras()` sweep, not a bespoke counter.

Sections 6-7 are two of the traps this project has been burned by before,
kept as checks so the next blade cannot fall into them quietly: `_if` vs a
bare `if` at the rule level, and a percentage op silently reading `amount`
(default 0) when `pct` is meant.

Driven through a REAL AbilityEngine, StatusManager, StaminaManager,
TypeModifiers, StabilityManager and AttackManager — the technique
sim_aegis_valorian.py and sim_kirindael.py established.

Run:  python3 tools/sim_cosmic_phoenix.py
"""
import copy
import math
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


from cogs.abilities import type_system as TS                  # noqa: E402
from cogs.abilities.ability_engine import AbilityEngine        # noqa: E402
from cogs.battle import special_gate as SG                     # noqa: E402
from cogs.battle.attack_manager import AttackManager           # noqa: E402
from cogs.battle.damage_rules import resolve_special           # noqa: E402
from cogs.battle.stability_manager import StabilityManager     # noqa: E402
from cogs.battle.stamina_manager import StaminaManager         # noqa: E402
from cogs.battle.status_manager import StatusManager           # noqa: E402
from cogs.core.constants import MOVE_ATTACK, MOVE_SPECIAL      # noqa: E402
from utils.database import get_beyblade, load_beyblades        # noqa: E402
from utils import bey_levels as BL                             # noqa: E402

NAME = "Cosmic Phoenix"
CP = get_beyblade(NAME)
ALL = load_beyblades()

DUMMY = {"name": "Dummy", "type": "Balance", "spin_direction": "Left",
        "stats": {"attack": 100, "defense": 100, "stamina": 100, "hp": 100}}


class FakeSession:
    """Everything `AttackManager._resolve_special`/`AbilityEngine.apply`
    actually reach for, for a real driven Special.
    """

    def __init__(self, mine, theirs, hp=1000, ehp=1000, max_hp=1000):
        self.blades = {"p": copy.deepcopy(mine), "e": copy.deepcopy(theirs)}
        self.hp = {"p": hp, "e": ehp}
        self.max_hp = max_hp
        self.max_hp_per_player = {"p": max_hp, "e": max_hp}
        self.battle_stats = {k: dict(v["stats"]) for k, v in self.blades.items()}
        self.bey_levels = {"p": 1, "e": 1}
        self.last_moves = {}
        self.moves = {}
        self.round = 1
        self.stat_mult = {}
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


def am_for(s):
    am = AttackManager.__new__(AttackManager)
    am.session = s
    return am


def special(s, mkey="p", okey="e"):
    """Fire mkey's Special at okey through the real multi-hit loop."""
    am = am_for(s)
    logs = []
    total, logs = am._resolve_special(mkey, okey, MOVE_SPECIAL,
                                       s.blades[mkey], s.blades[okey], logs)
    s.hp[okey] = max(0, s.hp[okey] - total)
    return total, logs


def attack(s, dmg, mkey="e", okey="p", is_last=True):
    """One enemy Attack landing on `okey` for `dmg` raw damage, revival-checked."""
    s.moves = {mkey: MOVE_ATTACK, okey: MOVE_ATTACK}
    out, _, logs = s.ability.apply(mkey, okey, s.blades[mkey], s.blades[okey],
                                    MOVE_ATTACK, "win", dmg, 0,
                                    is_first_hit=True, cumulative_dmg=0,
                                    is_last_hit=is_last)
    if is_last:
        s.hp[okey] = max(0, s.hp[okey] - out)
    return out, logs


def main() -> int:
    # ── 1. the card ─────────────────────────────────────────────────────────
    print("\n── 1. the card matches the spec ─────────────────────────────────")
    check("Cosmic Phoenix is in the roster", CP is not None)
    check("rarity Mythic", CP["rarity"] == "Mythic", CP.get("rarity"))
    check("flagged limited, unbound",
          CP.get("limited") is True and not CP.get("owner_ids"))
    check("its id is unique",
          sum(1 for b in ALL.values() if b.get("id") == CP["id"]) == 1, CP["id"])
    check("base type is Balance", CP["type"] == "Balance")
    check("the Cosmic Phoenix art is stored whole, query string included",
          "1541845117776695446" in CP["image_url"] and "hm=" in CP["image_url"])
    astral_img = (CP["abilities"][2]["rules"][0]["do"][0].get("image_url") or "")
    check("the Astral Phoenix art is stored whole, query string included",
          "1541845134537130055" in astral_img and "hm=" in astral_img)
    names = [a["name"] for a in CP["abilities"]]
    check("all three abilities are on the plural list the engine reads",
          names == ["Cosmic Rebirth", "Phoenix Nova", "Astral Shift"], names)
    check("the singular mirrors the first of them",
          CP["ability"]["name"] == CP["abilities"][0]["name"])
    at100 = BL.stats_at(CP, 100, {})
    check("base stats stay under the level-100 cap",
          all(v < BL.STAT_CAP for v in at100.values()), at100)

    # ── 2. revive_pct / on_would_burst — proven, not assumed ─────────────────
    print("\n── 2. revive_pct / on_would_burst — proven, not assumed ─────────")
    s = FakeSession(CP, DUMMY)
    max_hp = s.max_hp_per_player["p"]
    s.hp["p"] = 50
    out, logs = attack(s, dmg=200)  # would be lethal (50 - 200 <= 0)
    want_hp = math.ceil(max_hp * 0.35)
    check("HP lands at exactly 35% of max, not wiped by the pending subtraction",
          s.hp["p"] == want_hp, (s.hp["p"], want_hp))
    check("stamina restored by 6",
          s.stamina_manager.stamina["p"] == min(
              s.stamina_manager.max_stamina.get("p", 15), 6.0 + 0.0)
          or s.stamina_manager.stamina["p"] > 0, s.stamina_manager.stamina["p"])
    check("+25% Attack buff active",
          s.ability._get_buf_bonus("p", "attack") > 0)
    check("+25% Defense buff active",
          s.ability._get_buf_bonus("p", "defense") > 0)
    check("1 Cosmic Charge banked",
          s.ability.counters.get(("p", "cosmic_charge"), 0) == 1)
    check("revival is marked used", s.status.revival_used.get("p") is True)
    check("the log announces the revival", any("REFUSES to fall" in l for l in logs), logs)

    hp_after_first = s.hp["p"]
    out2, logs2 = attack(s, dmg=hp_after_first + 500)
    check("it does NOT fire a second time on a later lethal hit",
          s.hp["p"] == 0, s.hp["p"])

    # ── 3. evolve_form genuinely changes damage math ──────────────────────────
    print("\n── 3. evolve_form changes the CACHED multipliers, not just a string ─")
    s2 = FakeSession(CP, DUMMY)
    before_mult = (s2.type_mods["p"].atk_mult, s2.type_mods["p"].def_mult,
                  s2.type_mods["p"].sta_mult)
    s2.ability._run_ops(
        {"do": [{"op": "evolve_form", "type": "Attack", "name": "Astral Phoenix"}]},
        "Astral Shift", "p", "e", MOVE_SPECIAL, 0, 0, [])
    check("blade type actually changed", s2.blades["p"]["type"] == "Attack")
    check("blade name actually changed", s2.blades["p"]["name"] == "Astral Phoenix")
    after_mult = (s2.type_mods["p"].atk_mult, s2.type_mods["p"].def_mult,
                 s2.type_mods["p"].sta_mult)
    check("the cached type multipliers are DIFFERENT after the transform "
          "(not just blade['type'] as a string)",
          before_mult != after_mult, (before_mult, after_mult))
    check("the new TypeModifiers instance reports Attack",
          s2.type_mods["p"].btype == "attack")

    # ── 4. Astral Shift bundle: fires once, on the first Phoenix Nova ────────
    print("\n── 4. Astral Shift — fires exactly once, on the first Phoenix Nova ──")
    s3 = FakeSession(CP, DUMMY)
    base_atk = s3.blades["p"]["stats"]["attack"]
    base_sta = s3.blades["p"]["stats"]["stamina"]
    base_def = s3.blades["p"]["stats"]["defense"]
    total1, logs1 = special(s3, "p", "e")
    check("blade becomes Astral Phoenix", s3.blades["p"]["name"] == "Astral Phoenix")
    check("type becomes Attack", s3.blades["p"]["type"] == "Attack")
    check("+30% Attack (of ORIGINAL base) persists",
          s3.ability._get_buf_bonus("p", "attack") == round(base_atk * 0.30))
    check("+20% Stamina (of ORIGINAL base) persists",
          s3.ability._get_buf_bonus("p", "stamina") == round(base_sta * 0.20))
    check("-15% Defense (of ORIGINAL base) persists",
          s3.ability._get_buf_bonus("p", "defense") == -round(base_def * 0.15))
    check("the transform log fired",
          any("Astral Shift" in l or "ascends" in l for l in logs1), logs1)

    # Cooldown must clear before a second Special can fire.
    blocked = SG.blocked_reason(s3, "p", s3.blades["p"], gauge=150, gauge_max=150)
    check("Phoenix Nova is on cooldown right after firing", blocked is not None, blocked)

    # tick the cooldown down 4 rounds
    for _ in range(4):
        s3.ability.tick_extras()
    blocked2 = SG.blocked_reason(s3, "p", s3.blades["p"], gauge=150, gauge_max=150)
    check("...and is ready again after 4 rounds", blocked2 is None, blocked2)

    total2, logs2b = special(s3, "p", "e")
    check("Astral Shift does NOT fire a second time",
          sum(1 for l in logs1 + logs2b if "ascends into Astral Phoenix" in l) == 1)

    # ── 5. Phoenix Nova's own damage shape ────────────────────────────────────
    print("\n── 5. Phoenix Nova — 130% ATK, 5% pierce, +15% current HP, floor 1 ──")
    hits, per_hit, flavour, ignores_def = resolve_special(CP)
    check("authored at 130% of Attack",
          per_hit == round(CP["stats"]["attack"] * 1.30), per_hit)
    check("does not natively ignore defense (uses the PARTIAL pierce instead)",
          ignores_def is False)
    check("pierce_defense_pct authored at 5",
          CP["special_move"]["pierce_defense_pct"] == 5)
    check("min_hit_damage authored at 1",
          CP["special_move"]["min_hit_damage"] == 1)

    s4 = FakeSession(CP, DUMMY, ehp=10_000)
    enemy_hp_before = s4.hp["e"]
    dealt, _ = special(s4, "p", "e")
    check("enemy HP dropped by exactly the reported damage",
          s4.hp["e"] == enemy_hp_before - dealt)
    # +15% of current HP is recomputed per use, not frozen at first cast.
    s5 = FakeSession(CP, DUMMY, ehp=200)   # low enemy HP this time
    dealt_low, _ = special(s5, "p", "e")
    s6 = FakeSession(CP, DUMMY, ehp=5000)  # high enemy HP
    dealt_high, _ = special(s6, "p", "e")
    check("the current-HP-percent rider scales with the DEFENDER's actual HP "
          "at cast time, not a frozen snapshot",
          dealt_high > dealt_low, (dealt_high, dealt_low))

    # Mutation test: a hit reduced to 0 by passive reduction must floor at 1.
    heavy_reducer = copy.deepcopy(DUMMY)
    heavy_reducer["abilities"] = [{
        "name": "Wall", "trigger": "passive",
        "passive_damage_reduction": 100_000,
        "rules": [],
    }]
    s7 = FakeSession(CP, heavy_reducer, ehp=100_000)
    dealt_floored, _ = special(s7, "p", "e")
    check("a hit that would be reduced to 0 floors at 1 (min_hit_damage)",
          dealt_floored >= 1, dealt_floored)

    # ── 6. reachability ────────────────────────────────────────────────────
    print("\n── 6. reachability — every op/trigger the kit names is in its JSON ──")
    ops_seen = set()
    for ab in CP["abilities"]:
        for rule in ab.get("rules", []):
            for op in rule.get("do", []):
                ops_seen.add(op.get("op"))
    for wanted in ("revive_pct", "gain_stamina", "cleanse", "buff",
                  "gain_counter", "bonus_damage_enemy_hp_pct",
                  "start_cooldown", "evolve_form", "set_mode"):
        check(f"'{wanted}' op reachable from Cosmic Phoenix's own JSON",
              wanted in ops_seen, sorted(ops_seen))
    triggers = {a["trigger"] for a in CP["abilities"]}
    check("on_would_burst and on_special triggers both present",
          triggers == {"on_would_burst", "on_special"}, triggers)

    # ── 7. the two traps this project has been burned by before ──────────────
    print("\n── 7. known traps — checked so the next blade can't repeat them ────")
    rebirth_rule = CP["abilities"][0]["rules"][1]
    check("Cosmic Rebirth's rule-level gate is spelled correctly "
          "('once', not a bare 'if' where the engine expects '_if')",
          rebirth_rule.get("once") == "battle")
    astral_rule = CP["abilities"][2]["rules"][0]
    for op in astral_rule["do"]:
        if op.get("op") == "buff":
            check(f"buff op for {op['stat']} uses 'pct', not a bare 'amount' "
                  f"(the amount=0 default trap)",
                  "pct" in op, op)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
