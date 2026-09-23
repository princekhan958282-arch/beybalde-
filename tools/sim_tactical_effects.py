#!/usr/bin/env python3
"""Focused live-manager checks for opt-in tactical ops."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.sim_cosmic_phoenix import DUMMY, FakeSession, am_for
from cogs.abilities.tactical_effects import OPS, TYPES
from cogs.battle import special_gate
from cogs.battle.boss.blade_abilities import kit_for


def make(*names, type_name="Balance", enemy_names=()):
    def blade(effects, btype, name):
        result = copy.deepcopy(DUMMY)
        result["name"] = name
        result["type"] = btype
        result["abilities"] = [{"name": "Tactical", "rules": [
            {"when": "setup", "do": [{"op": name} for name in effects]}]}]
        return result
    s = FakeSession(blade(names, type_name, "Tactical Owner"),
                    blade(enemy_names, "Balance", "Tactical Opponent"))
    return s, s.ability.tactical


def round_(s, move="attack", enemy="defense", match="win", damage=0):
    t = s.ability.tactical
    s.last_moves = {"p": move, "e": enemy}
    p = {"attack": 100, "defense": 100}
    e = {"attack": 100, "defense": 100}
    logs = []
    t.round_start("p", "e", move, enemy, p, e, logs)
    t.round_start("e", "p", enemy, move, e, p, logs)
    if damage:
        dealt, _, hitlog = s.ability.apply("p", "e", s.blades["p"], s.blades["e"], move, match, damage, 0)
        logs += hitlog
        logs += am_for(s).apply_pair_results("p", "e", s.blades["p"], s.blades["e"], move, enemy, dealt, 0, match, 0, 0, "lose")
    t.round_end("p", "e", move, enemy, match, logs)
    t.round_end("e", "p", enemy, move, "lose", logs)
    s.round += 1
    return logs


class TacticalTests(unittest.TestCase):
    def test_all_25_are_registered_and_type_locked(self):
        self.assertEqual(len(OPS), 25)
        for op in OPS:
            with self.subTest(op=op):
                s, t = make(op, type_name=TYPES.get(op, "Balance"))
                self.assertTrue(t.has("p", op))
                self.assertFalse(t.has("e", op))
                self.assertTrue(any(op in line for line in kit_for(s.blades["p"]).unsupported()))
        for op in TYPES:
            self.assertFalse(make(op)[1].has("p", op))

    def test_round_history_and_one_time_grants(self):
        s, t = make("pattern_reader", "pressure_gauge", "staggered_rhythm",
                    "battle_tempo", "final_rotation")
        round_(s, "attack", "attack")
        self.assertEqual(t.data("p", "pressure_gauge")["stacks"], 1)
        logs = round_(s, "defense", "attack")
        self.assertTrue(any("Pattern Reader" in line for line in logs) is False)
        self.assertEqual(t.data("p", "pattern_reader").get("against"), None)
        round_(s, "attack", "attack")
        self.assertEqual(s.stamina_manager.gauge["p"], 20)
        s.round = 8
        round_(s, "charge", "charge")
        self.assertTrue(t.data("p", "final_rotation")["used"])

    def test_shield_overflow_momentum_and_exposure(self):
        s, t = make("charge_overflow", "shield_momentum", "exposed_core")
        sm = s.stamina_manager
        sm.gauge["p"] = 145
        sm.add_gauge("p", "charge")
        self.assertEqual(sm.gauge["p"], 150)
        self.assertGreater(s.status.get_shield("p"), 0)
        s.status.add_shield("p", 40)
        before = sm.gauge["p"]
        t.shield_absorbed("e", "p", 25, 0, [])
        self.assertGreaterEqual(sm.gauge["p"], before)
        t.shield_absorbed("p", "e", 10, 5, [])
        self.assertTrue(t.data("p", "exposed_core")["ready"])

    def test_reserve_anchor_and_second_wind(self):
        s, t = make("emergency_reserve", "second_wind", type_name="Defense")
        t.register({"op": "stability_anchor"}, "p", [])
        sm = s.stamina_manager
        sm.stamina["p"] = 1
        old = s.hp["p"]
        sm.deduct_cost("p", "attack")
        self.assertLess(s.hp["p"], old)
        self.assertTrue(t.data("p", "emergency_reserve")["used"])
        stab = s.stability_manager
        stab.stability["p"] = 2
        stab._apply("p", -20)
        self.assertEqual(stab.stability["p"], 1)
        stab._apply("p", -20)
        self.assertEqual(stab.stability["p"], 0)
        s.hp["p"] = 290
        sm.stamina["p"] = 0
        t.committed("e", "p", "attack", 10, [])
        self.assertGreater(sm.stamina["p"], 0)

    def test_special_refund_and_team_hook(self):
        s, t = make("perfect_timing", "sacrificial_guard")
        sm = s.stamina_manager
        sm.gauge["p"] = special_gate.gauge_cost(s.blades["p"])
        t.round_start("p", "e", "special", "attack", {"attack": 100, "defense": 100},
                      {"attack": 100, "defense": 100}, [])
        spent = special_gate.spend(s, "p", s.blades["p"])
        self.assertEqual(sm.gauge["p"], spent // 5)
        self.assertEqual(t.redirect_ally_damage("p", "e", 40), (40, 0))
        self.assertEqual(t.redirect_ally_damage("p", "e", 40, protection_active=True), (15, 25))

    def test_rising_stakes_marks_spin_and_type(self):
        s, t = make("spin_siphon", "finishers_mark", "rising_stakes", type_name="Stamina")
        old = s.stamina_manager.stamina["e"]
        t.round_end("p", "e", "stamina", "defense", "win", [])
        self.assertEqual(s.stamina_manager.stamina["e"], old - 2)
        for _ in range(4):
            t.round_end("p", "e", "defense", "attack", "lose", [])
        self.assertEqual(t.data("p", "rising_stakes")["stacks"], 4)
        self.assertEqual(t.before_damage("p", "e", "attack", "win", 100, []), 120)
        for _ in range(3):
            t.committed("p", "e", "attack", 10, [])
        self.assertEqual(t.before_damage("p", "e", "special", "win", 100, []), 125)

    def test_pattern_guard_and_cleansing_use_actual_damage_path(self):
        s, t = make("pattern_reader", "lasting_guard", "clean_break")
        t.previous["e"] = "attack"
        t.data("p", "lasting_guard")["active"] = True
        t.round_start("p", "e", "defense", "attack",
                      {"attack": 100, "defense": 100},
                      {"attack": 100, "defense": 100}, [])
        self.assertEqual(t.mitigate("p", "e", "attack", 100, []), 66)
        t.cleansed("p", "burn")
        self.assertEqual(t.mitigate("p", "e", "attack", 100, []), 60)
        t.round_end("p", "e", "defense", "attack", "loss", [])
        s.round += 2
        self.assertEqual(t.mitigate("p", "e", "attack", 100, []), 100)

    def test_damage_windows_and_opponent_defense_are_bounded(self):
        s, t = make("guard_fracture", "exposed_core", "counterweight",
                    "opening_gambit", "recoil_engine", "measured_strike")
        t.round_end("p", "e", "defense", "attack", "win", [])
        t.round_end("p", "e", "defense", "attack", "win", [])
        t.data("p", "exposed_core")["ready"] = True
        stats, enemy = {"attack": 100, "defense": 100}, {"attack": 400, "defense": 100}
        t.round_start("p", "e", "attack", "defense", stats, enemy, [])
        self.assertEqual(stats["defense"], 120)
        self.assertEqual(enemy["defense"], 76.5)
        self.assertEqual(t.before_damage("p", "e", "attack", "win", 100, []), 120)
        t.committed("e", "p", "attack", 210, [])
        t.committed("p", "e", "attack", 260, [])
        self.assertEqual(t.before_damage("p", "e", "attack", "win", 100, []), 98)
        # 100 * 1.15 * .85 = 97.75, rounded upward.
        self.assertEqual(t.before_damage("p", "e", "attack", "win", 100, []), 100)

    def test_precision_is_consumed_on_next_attack(self):
        s, t = make("precision_window", type_name="Attack")
        t.round_end("p", "e", "attack", "stamina", "win", [])
        self.assertEqual(t.crit_bonus("p", "attack"), .10)
        self.assertEqual(t.crit_bonus("p", "attack"), 0)

    def test_comeback_and_battle_tempo_gauge_cap(self):
        s, t = make("comeback_circuit", "battle_tempo", "shield_momentum")
        sm = s.stamina_manager
        s.hp["p"] = 500
        sm.stamina["p"] = 2
        t.round_start("p", "e", "attack", "defense", {"attack": 100, "defense": 100},
                      {"attack": 100, "defense": 100}, [])
        self.assertTrue(t.data("p", "comeback_circuit")["active"])
        from cogs.battle.button_profile import gauge_gain
        base = gauge_gain(s.blades["p"], "attack")
        sm.add_gauge("p", "attack")
        self.assertEqual(sm.gauge["p"], __import__("math").ceil(base * 1.12))
        sm.gauge["p"] = 0
        t.data("p", "battle_tempo")["skipped"] = 3
        t.round_start("p", "e", "charge", "attack", {"attack": 100, "defense": 100},
                      {"attack": 100, "defense": 100}, [])
        self.assertEqual(sm.gauge["p"], 20)
        t.shield_absorbed("e", "p", 1000, 0, [])
        self.assertEqual(sm.gauge["p"], 35)
        t.shield_absorbed("e", "p", 1000, 0, [])
        self.assertEqual(sm.gauge["p"], 35)

    def test_one_time_reserve_and_final_rotation_decision(self):
        s, t = make("emergency_reserve", "final_rotation")
        sm = s.stamina_manager
        sm.stamina["p"] = 1
        cost = sm.cost_for("p", "attack")
        self.assertTrue(sm.can_afford("p", "attack"))
        sm.deduct_cost("p", "attack")
        self.assertEqual(sm.stamina["p"], 0)
        self.assertFalse(sm.can_afford("p", "attack"))
        s.round = 8
        sm.stamina["p"] = sm.cap_for("p") - 20
        t.round_start("p", "e", "attack", "attack", {"attack": 100, "defense": 100},
                      {"attack": 100, "defense": 100}, [])
        self.assertAlmostEqual(sm.stamina["p"], sm.cap_for("p") - 5)
        t.round_start("p", "e", "attack", "attack", {"attack": 100, "defense": 100},
                      {"attack": 100, "defense": 100}, [])
        self.assertAlmostEqual(sm.stamina["p"], sm.cap_for("p") - 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
