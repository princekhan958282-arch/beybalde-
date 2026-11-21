"""Unit tests for Cerberus (BB172), Killing Frenzy, Unleash the Hound, and Triple Bite."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from utils.character_registry import REGISTRY, load_beys, load_parts
from utils import bey_levels as BL
from utils.hp_system import max_hp_for_blade
from utils.spin_mode import resolve, is_dual, modes
from cogs.battle.type_gimmicks import passive_stat_multiplier
from cogs.abilities.ability_engine import AbilityEngine
from cogs.battle.attack_manager import AttackManager
from cogs.battle.status_manager import StatusManager
from cogs.core.constants import MOVE_ATTACK, MOVE_DEFENSE, MOVE_SPECIAL, MOVE_STAMINA


class DummySession:
    """Mock BattleSession for testing Cerberus mechanics."""
    def __init__(self, blade_p1, blade_p2):
        self.blades = {"p1": blade_p1, "p2": blade_p2}
        self.battle_stats = {
            "p1": dict(blade_p1.get("stats", {})),
            "p2": dict(blade_p2.get("stats", {})),
        }
        self.hp = {
            "p1": blade_p1.get("stats", {}).get("hp", 80),
            "p2": blade_p2.get("stats", {}).get("hp", 80),
        }
        self.gauge = {"p1": 100, "p2": 100}
        self.status = StatusManager(self)
        self.ability = AbilityEngine(self)
        self.round = 1
        self.chain_handler = MagicMock()
        self.stability_manager = MagicMock()
        self.stability_manager.apply_clash_penalty.return_value = []
        self.stability_manager.apply_attack_cost.return_value = []
        self.stability_manager.apply_counter_penalty.return_value = []
        self.stability_manager.is_effects_active.return_value = False
        self.stamina_manager = MagicMock()
        self.defense_manager = MagicMock()
        self.defense_manager.preprocess_defender_stats.side_effect = lambda mkey, okey, mblade, oblade, ostats, **kw: (ostats, [])
        self.type_mods = {}
        self.type_gimmicks = None
        self.defer_landed_hit_effects = False


class CerberusTests(unittest.TestCase):
    def setUp(self):
        self.blade = REGISTRY.find_bey("Cerberus")
        self.assertIsNotNone(self.blade, "Cerberus (BB172) must be present in REGISTRY")

    def test_identity_and_schema(self):
        self.assertEqual(self.blade["id"], "BB172")
        self.assertEqual(self.blade["name"], "Cerberus")
        self.assertEqual(self.blade["rarity"], "Exclusive")
        self.assertEqual(self.blade["type"], "Balance")
        self.assertEqual(self.blade["spin_direction"], "Dual")
        self.assertTrue(self.blade["dual_spin"])
        self.assertEqual(self.blade["default_mode"], "Right")
        self.assertEqual(self.blade["creator"], "AXIS#Z73DAX \u201cBeymaster1\u201d")
        self.assertEqual(self.blade["complexity"], 5)

    def test_stats_and_component_split(self):
        stats = self.blade["stats"]
        self.assertEqual(stats["hp"], 80)
        self.assertEqual(stats["attack"], 170)
        self.assertEqual(stats["defense"], 80)
        self.assertEqual(stats["stamina"], 75)
        self.assertEqual(stats["special"], 0)

        # 45 / 27.5 / 27.5 split verification
        mf = self.blade["main_frame"]
        disk = REGISTRY.part(self.blade["default_parts"]["disk"])
        driver = REGISTRY.part(self.blade["default_parts"]["driver"])

        self.assertEqual(mf["hp"] + disk["stats"]["hp"] + driver["stats"]["hp"], 80)
        self.assertEqual(mf["attack"] + disk["stats"]["attack"] + driver["stats"]["attack"], 170)
        self.assertEqual(mf["defense"] + disk["stats"]["defense"] + driver["stats"]["defense"], 80)
        self.assertEqual(mf["stamina"] + disk["stats"]["stamina"] + driver["stats"]["stamina"], 75)

    def test_hp_balance_bounds(self):
        for iv in (0, BL.IV_MAX):
            before = 0
            for lvl in range(1, BL.MAX_LEVEL + 1):
                lvl_stats = BL.stats_at(self.blade, lvl, {"hp": iv})
                hp = int(max_hp_for_blade(dict(self.blade, stats=lvl_stats))
                         * passive_stat_multiplier(self.blade["type"], "hp"))
                self.assertGreaterEqual(hp, before)
                before = hp
            self.assertTrue(2000 <= hp <= 3000, f"Cerberus Lv100 HP ({hp}) out of bounds")

    def test_dual_spin_mode_toggling(self):
        self.assertTrue(is_dual(self.blade))
        avail = modes(self.blade)
        self.assertEqual(avail, ["Right", "Left"])

        right_form = resolve({"spin_mode": {"Cerberus": "Right"}}, self.blade)
        self.assertEqual(right_form["spin_direction"], "Right")
        self.assertEqual(right_form["stats"]["attack"], 170)

        left_form = resolve({"spin_mode": {"Cerberus": "Left"}}, self.blade)
        self.assertEqual(left_form["spin_direction"], "Left")
        self.assertEqual(left_form["stats"]["attack"], 170)

    def test_killing_frenzy_stacking_and_crit_bonus(self):
        dummy_opp = {"id": "BB001", "name": "Opponent", "type": "Attack", "stats": {"hp": 100, "attack": 100, "defense": 100, "stamina": 100, "special": 100}}
        session = DummySession(self.blade, dummy_opp)
        engine = session.ability

        rule = {
            "do": [
                {
                    "op": "gain_obsession_stack",
                    "max_stacks": 5,
                    "duration": 3,
                    "crit_per_stack": 0.10
                }
            ]
        }
        logs = []

        # Stack 1
        engine._run_ops(rule, "Killing Frenzy", "p1", "p2", MOVE_ATTACK, 50, 0, logs)
        self.assertEqual(engine.obsession_stacks["p1"], 1)
        self.assertEqual(engine.obsession_timer["p1"], 3)
        self.assertAlmostEqual(engine.crit_chance_bonus["p1"], 0.10)

        # Stack 2 & 3
        engine._run_ops(rule, "Killing Frenzy", "p1", "p2", MOVE_ATTACK, 50, 0, logs)
        engine._run_ops(rule, "Killing Frenzy", "p1", "p2", MOVE_ATTACK, 50, 0, logs)
        self.assertEqual(engine.obsession_stacks["p1"], 3)
        self.assertAlmostEqual(engine.crit_chance_bonus["p1"], 0.30)

        # Stack 4, 5, and beyond max cap (5)
        engine._run_ops(rule, "Killing Frenzy", "p1", "p2", MOVE_ATTACK, 50, 0, logs)
        engine._run_ops(rule, "Killing Frenzy", "p1", "p2", MOVE_ATTACK, 50, 0, logs)
        engine._run_ops(rule, "Killing Frenzy", "p1", "p2", MOVE_ATTACK, 50, 0, logs)
        self.assertEqual(engine.obsession_stacks["p1"], 5)
        self.assertAlmostEqual(engine.crit_chance_bonus["p1"], 0.50)

    def test_killing_frenzy_timer_decay_and_expiry(self):
        dummy_opp = {"id": "BB001", "name": "Opponent", "type": "Attack", "stats": {"hp": 100, "attack": 100, "defense": 100, "stamina": 100, "special": 100}}
        session = DummySession(self.blade, dummy_opp)
        engine = session.ability

        engine.obsession_stacks["p1"] = 3
        engine.obsession_timer["p1"] = 3
        engine.crit_chance_bonus["p1"] = 0.30

        # Round 1 end
        engine.tick_extras()
        self.assertEqual(engine.obsession_stacks["p1"], 3)
        self.assertEqual(engine.obsession_timer["p1"], 2)

        # Round 2 end
        engine.tick_extras()
        self.assertEqual(engine.obsession_stacks["p1"], 3)
        self.assertEqual(engine.obsession_timer["p1"], 1)

        # Round 3 end -> expired
        logs = engine.tick_extras()
        self.assertEqual(engine.obsession_stacks["p1"], 0)
        self.assertNotIn("p1", engine.obsession_timer)
        self.assertEqual(engine.crit_chance_bonus["p1"], 0.0)
        self.assertTrue(any("expired" in l for l in logs))

    def test_unleash_the_hound_and_triple_bite(self):
        dummy_opp = {"id": "BB001", "name": "Opponent", "type": "Attack", "stats": {"hp": 100, "attack": 100, "defense": 100, "stamina": 100, "special": 100}}
        session = DummySession(self.blade, dummy_opp)
        engine = session.ability
        am = AttackManager(session)

        # Special Move activates Unleash the Hound
        rule = {"do": [{"op": "unleash_hound", "duration": 6}]}
        logs = []
        engine._run_ops(rule, "Unleash the Hound", "p1", "p2", MOVE_SPECIAL, 0, 0, logs)
        self.assertTrue(engine.unleash_hound_active["p1"])
        self.assertEqual(engine.clone_duration["p1"], 6)

        # Attack with clones active triggers 3-hit clone assault (2.7x)
        # and multi-hit clone strikes build Obsession stacks
        p1_stats = {"level": 1, "attack": 170, "defense": 80, "stamina": 75, "hp": 80}
        p2_stats = {"level": 1, "attack": 100, "defense": 80, "stamina": 100, "hp": 100}

        dmg_dealt, dmg_taken, matchup, attack_logs = am.resolve_pair(
            "p1", "p2",
            MOVE_ATTACK, MOVE_STAMINA,
            self.blade, dummy_opp,
            p1_stats, p2_stats
        )
        # Should be amplified by 2.7x
        self.assertGreater(dmg_dealt, 0)
        self.assertTrue(any("Triple Bite" in l for l in attack_logs))
        self.assertTrue(any("Shadow Clone 1" in l for l in attack_logs))
        self.assertTrue(any("Shadow Clone 2" in l for l in attack_logs))

        # Each hit triggered Killing Frenzy -> should have 3 Obsession stacks!
        self.assertEqual(engine.obsession_stacks["p1"], 3)
        self.assertAlmostEqual(engine.crit_chance_bonus["p1"], 0.30)

        # Counter response test: when Cerberus defends with clones active
        dmg_dealt_2, counter_taken, _, def_logs = am.resolve_pair(
            "p2", "p1",
            MOVE_ATTACK, MOVE_DEFENSE,
            dummy_opp, self.blade,
            p2_stats, p1_stats
        )
        self.assertGreater(counter_taken, 0)
        self.assertTrue(any("Triple Bite" in l and "counter" in l for l in def_logs))


if __name__ == "__main__":
    unittest.main()
