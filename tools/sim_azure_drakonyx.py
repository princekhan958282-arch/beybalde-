#!/usr/bin/env python3
"""
tools/sim_azure_drakonyx.py — Azure Drakonyx, and special_pierce_pct.

Why this suite exists
----------------------
Azure Drakonyx's kit reduces almost entirely to existing, already-proven ops
(`gain_counter`, `counter_at_least`, `bonus_damage_pct`, `enemy_debuff_pct`,
`reset_counter`, `heal_pct`, `buff`, `cleanse`, `enemy_hp_below_pct`). The one
genuinely new piece is `special_pierce_pct`: Celestial Dragon Breaker's DEF
pierce is CONDITIONAL ("only below 40% HP"), and the existing
`special_move.pierce_defense_pct` field (Phoenix Nova's mechanism) is read
ONCE before a Special's hit loop even starts — before `on_special` rules have
run — so an `_if` gate on it has no engine to evaluate. `special_pierce_pct`
is a one-shot op consumed per-hit, AFTER `ability.apply()` for that hit, so a
conditional gate on it is actually live by the time it's read.

Section 3 is the check that gates the whole Special design: prove the
pierce is OFF above 40% HP and ON below it, in a REAL driven hit — not
assumed because the `_if` syntax "looks right".

Driven through a REAL AbilityEngine, StatusManager, StaminaManager,
TypeModifiers, StabilityManager and AttackManager — the technique
sim_cosmic_phoenix.py established.

Run:  python3 tools/sim_azure_drakonyx.py
"""
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
from cogs.battle.attack_manager import AttackManager           # noqa: E402
from cogs.battle.damage_rules import resolve_special           # noqa: E402
from cogs.battle.stability_manager import StabilityManager     # noqa: E402
from cogs.battle.stamina_manager import StaminaManager         # noqa: E402
from cogs.battle.status_manager import StatusManager           # noqa: E402
from cogs.core.constants import MOVE_ATTACK, MOVE_SPECIAL      # noqa: E402
from utils.database import get_beyblade, load_beyblades        # noqa: E402
from utils import bey_levels as BL                             # noqa: E402

NAME = "Azure Drakonyx"
AD = get_beyblade(NAME)
ALL = load_beyblades()

DUMMY = {"name": "Dummy", "type": "Balance", "spin_direction": "Left",
        "stats": {"attack": 100, "defense": 100, "stamina": 100, "hp": 100}}


class FakeSession:
    def __init__(self, mine, theirs, hp=1000, ehp=1000, max_hp=1000):
        import copy
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
    am = am_for(s)
    total, logs = am._resolve_special(mkey, okey, MOVE_SPECIAL,
                                       s.blades[mkey], s.blades[okey], [])
    s.hp[okey] = max(0, s.hp[okey] - total)
    return total, logs


def attack(s, mkey="p", okey="e", dmg=100, is_last=True):
    """One Attack from mkey landing on okey for `dmg` raw damage."""
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
    check("Azure Drakonyx is in the roster", AD is not None)
    check("rarity Mythic", AD["rarity"] == "Mythic", AD.get("rarity"))
    check("flagged limited, unbound",
          AD.get("limited") is True and not AD.get("owner_ids"))
    check("its id is unique",
          sum(1 for b in ALL.values() if b.get("id") == AD["id"]) == 1, AD["id"])
    check("the supplied art is stored whole, query string included",
          "1541882880584843304" in AD["image_url"] and "hm=" in AD["image_url"])
    names = [a["name"] for a in AD["abilities"]]
    check("all three abilities are on the plural list the engine reads",
          names == ["Dragon's Rampage", "Azure Rebirth", "Celestial Dragon Breaker"],
          names)
    check("the singular mirrors the first of them",
          AD["ability"]["name"] == AD["abilities"][0]["name"])
    at100 = BL.stats_at(AD, 100, {})
    check("base stats stay under the level-100 cap",
          all(v < BL.STAT_CAP for v in at100.values()), at100)

    # ── 2. Dragon's Rampage — every 3rd attack, not the 1st/2nd/4th ─────────
    print("\n── 2. Dragon's Rampage — procs on exactly the 3rd attack ────────")
    s = FakeSession(AD, DUMMY)
    base_def = s.blades["e"]["stats"]["defense"]

    out1, logs1 = attack(s, dmg=100)
    check("1st attack: no bonus damage", out1 == 100, out1)
    check("1st attack: no DEF debuff yet",
          s.ability._get_buf_bonus("e", "defense") == 0)

    out2, logs2 = attack(s, dmg=100)
    check("2nd attack: still no bonus damage", out2 == 100, out2)

    out3, logs3 = attack(s, dmg=100)
    check("3rd attack: +25% bonus damage", out3 == 125, out3)
    check("3rd attack: enemy DEF cut by 10% of THEIR base, for 2 turns",
          s.ability._get_buf_bonus("e", "defense") == -round(base_def * 0.10),
          s.ability._get_buf_bonus("e", "defense"))
    check("the proc is logged",
          any("Rampage" in l for l in logs3) or any("+" in l for l in logs3), logs3)

    out4, logs4 = attack(s, dmg=100)
    check("4th attack: counter reset — no bonus damage",
          out4 == 100, out4)

    out5, _ = attack(s, dmg=100)
    out6, _ = attack(s, dmg=100)
    check("5th attack: still building, no bonus", out5 == 100, out5)
    check("6th attack: procs again", out6 == 125, out6)

    # ── 3. Celestial Dragon Breaker — conditional pierce, proven live ───────
    print("\n── 3. Celestial Dragon Breaker — 180% ATK, conditional +30%/pierce ──")
    hits, per_hit, flavour, ignores_def = resolve_special(AD)
    check("authored at 180% of Attack",
          per_hit == round(AD["stats"]["attack"] * 1.80), per_hit)
    check("does not natively ignore defense (the pierce is conditional)",
          ignores_def is False)

    # Above 40% HP: no bonus, no pierce.
    s2 = FakeSession(AD, DUMMY, ehp=1000)   # enemy at full HP
    dealt_full, logs_full = special(s2, "p", "e")
    check("enemy above 40% HP: no +30% damage rider",
          "+30%" not in " ".join(logs_full) and
          not any("cuts through" in l for l in logs_full), logs_full)

    # Below 40% HP: both the damage rider AND the pierce apply.
    s3 = FakeSession(AD, DUMMY, ehp=300)    # enemy well under 40% of 1000... use max_hp=300 too
    s3.max_hp_per_player["e"] = 1000
    s3.hp["e"] = 300                        # 30% of max — below the 40% gate
    dealt_low, logs_low = special(s3, "p", "e")
    check("enemy below 40% HP: the pierce fires",
          any("cuts through" in l for l in logs_low), logs_low)
    check("...cutting through exactly 25% of their Defense mitigation",
          any("25%" in l for l in logs_low), logs_low)

    # Damage comparison at equal starting conditions (both attacking the same
    # DEF-having target), gated purely by the HP threshold — proves the
    # +30% rider is genuinely conditional, not a flat always-on bonus.
    dfoe = dict(DUMMY)
    dfoe["stats"] = dict(DUMMY["stats"])
    s4 = FakeSession(AD, dfoe, ehp=1000)
    s4.max_hp_per_player["e"] = 1000
    dealt_above, _ = special(s4, "p", "e")
    s5 = FakeSession(AD, dfoe, ehp=1000)
    s5.max_hp_per_player["e"] = 1000
    s5.hp["e"] = 350   # 35% — below the 40% gate
    dealt_below, _ = special(s5, "p", "e")
    check("the +30% rider only applies below the 40% HP gate",
          dealt_below > dealt_above, (dealt_below, dealt_above))

    # ── 4. Azure Rebirth — once, only below 30% HP, exact numbers ───────────
    print("\n── 4. Azure Rebirth — once per battle, below 30% HP ─────────────")
    # Threshold triggers (on_low_hp included) fire off the MOVER's own HP —
    # confirmed by reading attack_manager.resolve_pair, called once per round
    # with EACH side as mkey — so Azure Drakonyx itself must be `mkey` here,
    # not the side being hit. `dmg=0` keeps this focused on the threshold
    # check alone, not on any particular move's damage.
    s6 = FakeSession(AD, DUMMY)
    max_hp = s6.max_hp_per_player["p"]

    # Above the 30% line: must NOT fire.
    s6.hp["p"] = 500   # 50%, not below 30%
    attack(s6, mkey="p", okey="e", dmg=0)
    check("above 30% HP: Azure Rebirth does not fire",
          s6.ability._get_buf_bonus("p", "defense") == 0)

    # Cross below 30%: must fire exactly once, with the exact numbers.
    s6.hp["p"] = 250   # 25%
    hp_before = s6.hp["p"]
    _, rebirth_logs = attack(s6, mkey="p", okey="e", dmg=0)
    want_heal = math.ceil(max_hp * 0.20)
    check("HP restored by exactly 20% of max",
          s6.hp["p"] == min(max_hp, hp_before + want_heal),
          (s6.hp["p"], hp_before + want_heal))
    check("+15% Defense for 2 turns",
          s6.ability._get_buf_bonus("p", "defense")
          == round(s6.blades["p"]["stats"]["defense"] * 0.15))
    check("the revival log fired",
          any("Azure Rebirth" in l for l in rebirth_logs), rebirth_logs)

    # Plant a real temporary debuff, then trigger it again while still under
    # 30% — must NOT fire twice, but the debuff should stay (nothing to
    # cleanse a second time from an ability that never re-fires).
    s6.status.add_buff("p", "attack", -5, 3)
    s6.hp["p"] = 100
    attack(s6, mkey="p", okey="e", dmg=0)
    check("does NOT fire a second time in the same battle",
          s6.ability._get_buf_bonus("p", "attack") == -5,
          s6.ability._get_buf_bonus("p", "attack"))

    # Cleanse-on-trigger, tested fresh (below 30% with a real debuff present).
    s7 = FakeSession(AD, DUMMY)
    s7.status.add_buff("p", "stamina", -8, 3)
    s7.hp["p"] = 200
    attack(s7, mkey="p", okey="e", dmg=0)
    check("a genuine negative status IS cleansed when Azure Rebirth fires",
          s7.ability._get_buf_bonus("p", "stamina") >= 0,
          s7.ability._get_buf_bonus("p", "stamina"))

    # ── 5. reachability ────────────────────────────────────────────────────
    print("\n── 5. reachability — every op/trigger the kit names is in its JSON ──")
    ops_seen = set()
    for ab in AD["abilities"]:
        for rule in ab.get("rules", []):
            for op in rule.get("do", []):
                ops_seen.add(op.get("op"))
    for wanted in ("gain_counter", "bonus_damage_pct", "enemy_debuff_pct",
                  "reset_counter", "heal_pct", "buff", "cleanse",
                  "special_pierce_pct"):
        check(f"'{wanted}' op reachable from Azure Drakonyx's own JSON",
              wanted in ops_seen, sorted(ops_seen))
    triggers = {a["trigger"] for a in AD["abilities"]}
    check("on_attack_hit, on_low_hp and on_special triggers all present",
          triggers == {"on_attack_hit", "on_low_hp", "on_special"}, triggers)

    # ── 6. known traps ───────────────────────────────────────────────────────
    print("\n── 6. known traps — checked so this blade can't repeat them ────")
    rebirth_rule = AD["abilities"][1]["rules"][0]
    check("Azure Rebirth's HP gate is spelled 'if' at rule level "
          "(not '_if', which only op-level gates use)",
          "if" in rebirth_rule and rebirth_rule["if"][0]["cond"] == "hp_below_pct")
    check("...and it's a FRACTION (0.3), not a bare 30 (the pct-vs-fraction trap)",
          rebirth_rule["if"][0]["value"] == 0.3)
    rebirth_buff = next(op for op in rebirth_rule["do"] if op.get("op") == "buff")
    check("Azure Rebirth's Defense buff uses 'pct', not a bare 'amount'",
          "pct" in rebirth_buff, rebirth_buff)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
