"""Tests verifying bug audit fixes across all Beyblades and AbilityEngine:
1. All 170 Beyblades compile abilities and meet registry schema requirements.
2. Achilles CX Z Awakening trigger and damage amp op.
3. Phantom Nightmare extended effect schema (pct, cap) and lifesteal.
4. Abyss Drakon spin direction and executable rules.
5. Epsilon spin direction and Boss Breaker is_boss execution.
6. Noctilune spin direction.
7. Canonical spin directions on classic blades.
8. Boss Org blades burst height metadata.
9. AbilityEngine alias tolerance for damage_amp and lifesteal.
"""

import json
import os
import unittest
from pathlib import Path

from cogs.abilities.ability_engine import AbilityEngine
from cogs.abilities.extended_effects import validate as validate_extended_op
from cogs.battle.status_manager import StatusManager
from utils.character_registry import CharacterRegistry, REGISTRY

ROOT = Path(__file__).resolve().parents[1]


class DummySession:
    def __init__(self, foe_rarity="Rare", is_boss=False):
        self.hp = {"p": 2000, "e": 2000}
        self.max_hp_per_player = {"p": 2000, "e": 2000}
        self.max_hp = 2000
        self.moves = {"p": "attack", "e": "defense"}
        self.last_moves = {"p": "attack", "e": "defense"}
        self.round = 1
        self.is_boss = is_boss
        self.blades = {
            "p": {"name": "TestBlade", "spin_direction": "Right", "type": "Attack"},
            "e": {"name": "FoeBlade", "spin_direction": "Left", "type": "Defense", "rarity": foe_rarity}
        }
        self.status = StatusManager(self)
        self.stamina_manager = DummyStamina()


class DummyStamina:
    def __init__(self):
        self.stamina = {"p": 15.0, "e": 15.0}
        self.max_stamina = {"p": 15.0, "e": 15.0}
        self.drain_reduction = {}


class TestBeybladeBugAudit(unittest.TestCase):

    def test_01_all_beyblades_schema_and_spin_direction(self):
        """Every blade in beys/ must have valid id, name, type, and canonical spin_direction."""
        files = list((ROOT / "beys").glob("*.json"))
        self.assertGreaterEqual(len(files), 170)

        for path in files:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            bname = data.get("name", path.stem)
            self.assertIn("id", data, f"{bname} missing id")
            self.assertIn("name", data, f"{bname} missing name")
            self.assertIn("spin_direction", data, f"{bname} missing spin_direction")
            self.assertIn(
                data["spin_direction"],
                ("Right", "Left", "Dual"),
                f"{bname} has invalid or uncapitalized spin_direction: '{data.get('spin_direction')}'"
            )

    def test_02_achilles_cx_z_awakening(self):
        """Achilles CX should trigger Z Awakening at 3 Z stacks via turn_start and apply dmg_amp."""
        path = ROOT / "beys" / "achilles_cx.json"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        rules = data["abilities"][0]["rules"]
        awakening_rule = [r for r in rules if r.get("_name") == "Z Awakening"][0]
        self.assertEqual(awakening_rule["when"], "turn_start")
        self.assertEqual(awakening_rule["do"][0]["op"], "dmg_amp")

        # Test in engine
        sess = DummySession()
        sess.blades["p"] = data
        engine = AbilityEngine(sess)
        engine.counters[("p", "z_stack")] = 3

        logs = []
        engine._fire("turn_start", "p", "e", data, "attack", "neutral", 0, 0, logs)
        self.assertAlmostEqual(sess.status.dmg_amp_stacks.get("p", 0.0), 0.15, places=2)
        self.assertEqual(engine.counters.get(("p", "z_stack"), 0), 0)

    def test_03_phantom_nightmare_extended_effects_and_lifesteal(self):
        """Phantom Nightmare rules should have valid pct/cap fields and lifesteal_pct."""
        path = ROOT / "beys" / "phantom_nightmare.json"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        rules = data["abilities"][0]["rules"]
        for r in rules:
            self.assertNotEqual(r.get("when"), "on_turn")
            for op in r.get("do", []):
                cfg = validate_extended_op(op)
                self.assertIn("cap", cfg)
                self.assertIn("pct", cfg)

        # Lifesteal rule
        ls_rule = data["abilities"][1]["rules"][0]
        self.assertEqual(ls_rule["do"][0]["op"], "lifesteal_pct")

        sess = DummySession()
        sess.blades["p"] = data
        engine = AbilityEngine(sess)
        logs = []
        engine._fire("on_special", "p", "e", data, "special", "neutral", 100, 0, logs)
        self.assertEqual(engine.lifesteal_pct.get("p"), 25.0)

    def test_04_abyss_drakon_rules_and_spin(self):
        """Abyss Drakon must have Right spin and executable compiled ability rules."""
        path = ROOT / "beys" / "abyss_drakon.json"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["spin_direction"], "Right")
        sess = DummySession()
        engine = AbilityEngine(sess)
        rules = engine._rules_for(data, "p")
        self.assertGreater(len(rules), 0, "Abyss Drakon must compile at least 1 rule")

        # Test stamina drain
        logs = []
        engine._fire("on_stamina_win", "p", "e", data, "stamina", "win", 0, 0, logs)
        self.assertEqual(sess.stamina_manager.stamina["e"], 12.0)
        self.assertEqual(sess.stamina_manager.stamina["p"], 15.0)

    def test_05_epsilon_boss_breaker(self):
        """Epsilon must deal +25% damage in boss battles and gain attack stacks."""
        path = ROOT / "beys" / "epsilon.json"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["spin_direction"], "Right")

        # Against non-boss
        sess_normal = DummySession(foe_rarity="Rare", is_boss=False)
        sess_normal.blades["p"] = data
        engine_normal = AbilityEngine(sess_normal)
        logs = []
        engine_normal._fire("setup", "p", "e", data, "attack", "neutral", 0, 0, logs)
        self.assertEqual(sess_normal.status.dmg_amp_stacks.get("p", 0.0), 0.0)

        # Against Boss
        sess_boss = DummySession(foe_rarity="Boss", is_boss=True)
        sess_boss.blades["p"] = data
        engine_boss = AbilityEngine(sess_boss)
        logs = []
        engine_boss._fire("setup", "p", "e", data, "attack", "neutral", 0, 0, logs)
        self.assertAlmostEqual(sess_boss.status.dmg_amp_stacks.get("p", 0.0), 0.25, places=2)

    def test_06_boss_org_burst_height(self):
        """All boss organization blades must have valid burst_height metadata."""
        org_files = ("argus_prime_org.json", "aetherion_drakos_org.json",
                     "lionheart_sovereign_org.json", "nemesis_therion_org.json")
        for fn in org_files:
            path = ROOT / "beys" / fn
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("burst_height", data, f"{fn} must have burst_height")
            self.assertIn(data["burst_height"], ("Low", "Mid", "High"))

    def test_07_engine_alias_resilience(self):
        """AbilityEngine._run_ops should tolerate damage_amp and lifesteal op aliases."""
        sess = DummySession()
        engine = AbilityEngine(sess)

        # damage_amp alias
        blade_da = {
            "name": "AliasBey",
            "abilities": [{
                "name": "AliasAb",
                "rules": [{
                    "when": "turn_start",
                    "do": [{"op": "damage_amp", "value": 20}]
                }]
            }]
        }
        logs = []
        engine._fire("turn_start", "p", "e", blade_da, "attack", "neutral", 0, 0, logs)
        self.assertAlmostEqual(sess.status.dmg_amp_stacks.get("p", 0.0), 0.20, places=2)

        # lifesteal alias
        blade_ls = {
            "name": "AliasBey2",
            "abilities": [{
                "name": "AliasAb2",
                "rules": [{
                    "when": "turn_start",
                    "do": [{"op": "lifesteal", "percent": 30}]
                }]
            }]
        }
        engine._fire("turn_start", "p", "e", blade_ls, "attack", "neutral", 0, 0, logs)
        self.assertEqual(engine.lifesteal_pct.get("p"), 30.0)

    def test_08_classic_blades_capitalized_spin_direction(self):
        """Classic blades must have capitalized spin_direction 'Right'."""
        for fn in ("king_kerbeus.json", "rising_ragnaruk.json", "storm_spriggan.json", "victory_valkyrie.json"):
            path = ROOT / "beys" / fn
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["spin_direction"], "Right", f"{fn} must have Right spin")

    def test_09_sessionless_compilation_safety(self):
        """AbilityEngine instantiated via __new__ without a session must compile rules safely."""
        eng = AbilityEngine.__new__(AbilityEngine)
        files = list((ROOT / "beys").glob("*.json"))
        for p in files:
            with open(p, "r", encoding="utf-8") as f:
                blade = json.load(f)
            rules = eng._rules_for(blade, "p")
            self.assertIsInstance(rules, list)

    def test_10_tartarus_reaper_balance_numbers(self):
        """Tartarus Reaper Soul Judgement must match balanced spec: +25 buffs, 25 heal, 0.3 sta, 3 cap."""
        path = ROOT / "beys" / "tartarus_reaper.json"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        sj = [a for a in data["abilities"] if a.get("name") == "Soul Judgement"][0]
        self.assertIn("25", sj["description"])
        self.assertIn("three", sj["description"].lower())
        for r in sj["rules"]:
            for c in r.get("if", []):
                if c.get("cond") == "counter_below":
                    self.assertEqual(c["value"], 3)
            for op in r.get("do", []):
                if op.get("op") == "stacking_buff":
                    self.assertEqual(op["per_stack"], 25)
                    self.assertEqual(op["max"], 3)
                elif op.get("op") == "buff":
                    self.assertEqual(op["amount"], 25)
                elif op.get("op") == "heal":
                    self.assertEqual(op["value"], 25)
                elif op.get("op") == "gain_stamina":
                    self.assertEqual(op["value"], 0.3)



    def test_11_excalibur_ascendant_special_and_overdrive(self):
        """Excalibur Ascendant must have ignores_defense and true_damage on special, and true_damage_turns on Overdrive."""
        path = ROOT / "beys" / "excalibur_ascendant.json"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        sm = data["special_move"]
        self.assertTrue(sm.get("ignores_defense"))
        self.assertTrue(sm.get("true_damage"))
        self.assertNotIn("Draciel", sm.get("description", ""))
        self.assertIn("Excalibur Ascendant", sm.get("description", ""))

        od = [a for a in data["abilities"] if a.get("name") == "Internal Overdrive"][0]
        ops = od["rules"][0]["do"]
        self.assertTrue(any(op.get("op") == "true_damage_turns" for op in ops))
        self.assertTrue(any(op.get("op") == "ignore_defense" for op in ops))

        from cogs.battle.damage_rules import calc_damage
        atk_stats = {"level": 1, "attack": 160, "defense": 90, "stamina": 110}
        def_stats = {"level": 1, "attack": 50, "defense": 90, "stamina": 80, "_ignore_defense": True}
        dmg, counter, matchup, _ = calc_damage("attack", atk_stats, def_stats, data, "defense")
        self.assertEqual(matchup, "win")
        self.assertEqual(counter, 0)
        self.assertGreater(dmg, 0)

    def test_12_boss_base_compatibility_and_is_boss_isolation(self):
        """is_boss must identify boss base blades and not false-positive on standard Exclusive blades."""
        for fn in ("aetherion_drakos_org.json", "argus_prime_org.json", "lionheart_sovereign_org.json", "nemesis_therion_org.json"):
            with open(ROOT / "beys" / fn, "r", encoding="utf-8") as f:
                data = json.load(f)
            sess = DummySession()
            sess.blades["e"] = data
            engine = AbilityEngine(sess)
            self.assertTrue(engine._check({"cond": "is_boss"}, "p", "e", "attack", "neutral"), f"{fn} must be recognized as boss")

        for fn in ("excalibur_ascendant.json", "kirindael.json", "void_longinus.json", "tartarus_reaper.json"):
            with open(ROOT / "beys" / fn, "r", encoding="utf-8") as f:
                data = json.load(f)
            sess = DummySession()
            sess.blades["e"] = data
            engine = AbilityEngine(sess)
            self.assertFalse(engine._check({"cond": "is_boss"}, "p", "e", "attack", "neutral"), f"{fn} must not trigger is_boss")

    def test_13_boss_base_rules_and_counter_reset_buff_clearing(self):
        """reset_counter on stacking_buff must clear active buff bonuses in status manager."""
        with open(ROOT / "beys" / "aetherion_drakos_org.json", "r", encoding="utf-8") as f:
            drakos = json.load(f)
        sess = DummySession()
        sess.blades["p"] = drakos
        engine = AbilityEngine(sess)
        logs = []
        engine._fire("passive", "p", "e", drakos, "attack", "neutral", 0, 0, logs)
        self.assertEqual(sess.status.get_buff_bonus("p", "attack"), 4)
        engine._fire("on_take_damage", "p", "e", drakos, "attack", "loss", 100, 0, logs)
        self.assertEqual(engine.counters.get(("p", "stars")), 0)
        self.assertEqual(sess.status.get_buff_bonus("p", "attack"), 0, "Buff source must clear on counter reset")

    def test_14_copy_abilities_full_verification(self):
        """copy_abilities.verify must report 0 missing or mismatched boss mechanics across all boss profiles."""
        from cogs.battle.boss import copy_abilities
        self.assertEqual(copy_abilities.verify(), [])

    def test_15_boss_beys_combat_and_rolled_copies_compatibility(self):
        """Simulate combat triggers for all boss originals and rolled copies in AbilityEngine."""
        from cogs.battle.chain_handler import ChainHandler
        from cogs.battle.boss import boss_copy as bc, boss_info as binfo
        from unittest.mock import patch

        boss_files = [
            "aetherion_drakos_org.json",
            "argus_prime_org.json",
            "lionheart_sovereign_org.json",
            "nemesis_therion_org.json",
        ]

        with open(ROOT / "beys" / "abyss_drakon.json", "r", encoding="utf-8") as f:
            foe = json.load(f)

        for fn in boss_files:
            with open(ROOT / "beys" / fn, "r", encoding="utf-8") as f:
                boss = json.load(f)
            sess = DummySession()
            sess.chain_handler = ChainHandler(sess)
            sess.blades["p"] = boss
            sess.blades["e"] = foe
            engine = AbilityEngine(sess)
            sess.ability = engine

            # Setup & passive
            engine.setup("p", boss)
            with patch("random.random", return_value=0.1):
                logs = []
                engine._fire("passive", "p", "e", boss, "attack", "neutral", 0, 0, logs)
                self.assertIsInstance(logs, list)

            # Attack win
            dealt, taken, a_logs = engine.apply("p", "e", boss, foe, "attack", "win", 100, 0)
            self.assertGreater(dealt, 0)

            # Take damage
            t_logs = []
            engine._fire("on_take_damage", "p", "e", boss, "attack", "loss", 80, 0, t_logs)

            # Low HP & Special move
            sess.hp["p"] = 300
            engine._fire("on_low_hp", "p", "e", boss, "special", "win", 0, 0, logs)
            s_dealt, s_taken, s_logs = engine.apply("p", "e", boss, foe, "special", "win", 150, 0)
            self.assertGreater(s_dealt, 0)

        # Verify rolled copies across all 4 bosses and all grades execute without errors
        for key in ("drakos", "argus", "lionheart", "nemesis"):
            prof = binfo.REGISTRY[key]
            for grade in ("Flawed", "Standard", "Refined", "Pristine", "Flawless", "Perfect"):
                copy_data = bc.roll_copy(prof, forced_grade=grade)
                blade = bc.to_blade(copy_data, prof)
                sess = DummySession()
                sess.chain_handler = ChainHandler(sess)
                sess.blades["p"] = blade
                sess.blades["e"] = foe
                engine = AbilityEngine(sess)
                sess.ability = engine
                engine.setup("p", blade)
                dealt, _, _ = engine.apply("p", "e", blade, foe, "attack", "win", 100, 0)
                self.assertGreater(dealt, 0)

    def test_defense_move_does_not_deal_outgoing_attack_damage(self):
        from cogs.battle.constants import MOVE_ATTACK, MOVE_DEFENSE, MOVE_STAMINA, MOVE_CHARGE
        from cogs.battle.damage_rules import calc_damage
        with open(ROOT / "beys" / "prime_apocalypse.json", "r", encoding="utf-8") as f:
            apocalypse = json.load(f)
        with open(ROOT / "beys" / "fang_fenrir.json", "r", encoding="utf-8") as f:
            fenrir = json.load(f)

        atk_stats = apocalypse["stats"]
        def_stats = fenrir["stats"]

        # Defense vs Attack: 0 outgoing, 0 taken (counter hit is handled on Attack's pairing)
        d_vs_a = calc_damage(MOVE_DEFENSE, atk_stats, def_stats, apocalypse, MOVE_ATTACK)
        self.assertEqual(d_vs_a, (0, 0, "win", False))

        # Defense vs Defense: 0 damage dealt
        d_vs_d = calc_damage(MOVE_DEFENSE, atk_stats, def_stats, apocalypse, MOVE_DEFENSE)
        self.assertEqual(d_vs_d, (0, 0, "mirror", False))

        # Defense vs Stamina: 0 damage dealt, matchup is lose_grind for grind debuff
        d_vs_s = calc_damage(MOVE_DEFENSE, atk_stats, def_stats, apocalypse, MOVE_STAMINA)
        self.assertEqual(d_vs_s, (0, 0, "lose_grind", False))

        # Defense vs Charge: 0 damage dealt
        d_vs_c = calc_damage(MOVE_DEFENSE, atk_stats, def_stats, apocalypse, MOVE_CHARGE)
        self.assertEqual(d_vs_c, (0, 0, "mirror", False))

    def test_prime_apocalypse_weak_point_is_not_offensive_proc(self):
        from cogs.abilities.legacy_convert import legacy_convert
        with open(ROOT / "beys" / "prime_apocalypse.json", "r", encoding="utf-8") as f:
            apocalypse = json.load(f)
        rules = legacy_convert(apocalypse["ability"])
        # Must not produce bonus_damage_pct (50% bonus damage) from weak_point_hit_chance
        for rule in rules:
            for op in rule.get("do", []):
                self.assertNotEqual(op.get("op"), "bonus_damage_pct",
                                    "Prime Apocalypse should not gain offensive bonus damage from its weak point")
        # Should have its burst resistance / damage reduction
        has_reduce = any(any(op.get("op") == "reduce_damage_pct" for op in rule.get("do", [])) for rule in rules)
        self.assertTrue(has_reduce, "Prime Apocalypse should retain damage reduction from burst resistance")


if __name__ == "__main__":
    unittest.main()
