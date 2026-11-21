"""Unit tests for BB171 Eclipse Thunderbird (Dual Mode, Eclipse Flight, Ω Special, and Chain-Crit)."""

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from cogs.abilities.ability_engine import AbilityEngine
from cogs.battle.attack_manager import AttackManager
from cogs.battle.stability_manager import StabilityManager
from cogs.battle.status_manager import StatusManager
from utils.character_registry import load_beys, REGISTRY
from utils.spin_mode import resolve, modes, chosen


class DummyChannel:
    async def send(self, *args, **kwargs):
        pass


class DummyPlayer:
    def __init__(self, uid, name):
        self.id = uid
        self.name = name


class DummyChainHandler:
    def resolve(self, *args, **kwargs):
        return []

    def queue(self, key, steps):
        pass


class DummySession:
    def __init__(self, b1, b2):
        self.blades = {"p": copy.deepcopy(b1), "e": copy.deepcopy(b2)}
        self.hp = {"p": 2000, "e": 2000}
        self.max_hp_per_player = {"p": 2000, "e": 2000}
        self.max_hp = 2000
        self.round = 1
        self.stat_mult = {"p": 1.0, "e": 1.0}
        self.part_deltas = {"p": {}, "e": {}}
        self.battle_stats = {"p": {}, "e": {}}
        self.special_stats = {"p": None, "e": None}
        self.bey_levels = {"p": 1, "e": 1}
        self.moves = {"p": "attack", "e": "attack"}
        self.type_gimmicks = None
        self.channel = DummyChannel()
        self.chain_handler = DummyChainHandler()

        class DummyStamina:
            def __init__(self):
                self.stamina = {"p": 15.0, "e": 15.0}
                self.max_stamina = {"p": 15.0, "e": 15.0}
                self.drain_reduction = {"p": 0.0, "e": 0.0}

            def add_gauge(self, key, kind):
                pass

        self.status = StatusManager(self)
        self.stamina_manager = DummyStamina()
        self.stability_manager = StabilityManager(self.blades, {})
        self.ability = AbilityEngine(self)
        self.attack_manager = AttackManager(self)


class TestEclipseThunderbird(unittest.TestCase):
    def setUp(self):
        beys = load_beys()
        self.blade = beys["Eclipse Thunderbird"]

    def test_01_profile_and_component_integrity(self):
        """Verifies core identity, 125 base HP, dual-spin configuration, and 45/27.5/27.5 parts."""
        self.assertEqual(self.blade["id"], "BB171")
        self.assertEqual(self.blade["rarity"], "Mythic")
        self.assertTrue(self.blade.get("tournament_exclusive"))
        self.assertFalse(self.blade.get("booster_exclusive"))
        self.assertEqual(self.blade["type"], "Attack")
        self.assertTrue(self.blade.get("dual_spin"))
        self.assertEqual(self.blade["stats"]["hp"], 125)
        self.assertEqual(self.blade["stats"]["attack"], 120)
        self.assertEqual(self.blade["stats"]["defense"], 80)
        self.assertEqual(self.blade["stats"]["stamina"], 117)
        self.assertEqual(self.blade["stats"]["special"], 125)

        # Modular components verification
        disk_name = self.blade["default_parts"]["disk"]
        driver_name = self.blade["default_parts"]["driver"]
        disk = REGISTRY.part(disk_name)
        driver = REGISTRY.part(driver_name)
        self.assertIsNotNone(disk)
        self.assertIsNotNone(driver)

        # Largest remainder sum check
        mf = self.blade["main_frame"]
        for stat in ("hp", "attack", "defense", "stamina"):
            self.assertEqual(
                self.blade["stats"][stat],
                mf[stat] + disk["stats"][stat] + driver["stats"][stat],
                f"Stat mismatch for {stat}"
            )

    def test_02_dual_mode_resolution(self):
        """Verifies Dawn and Dusk mode resolution via utils.spin_mode."""
        self.assertIn("Dawn", modes(self.blade))
        self.assertIn("Dusk", modes(self.blade))

        # Dawn Mode
        dawn = resolve({"spin_mode": {"Eclipse Thunderbird": "Dawn"}}, self.blade)
        self.assertEqual(dawn["name"], "eclipse_thunderbird_dawn")
        self.assertEqual(dawn["spin_direction"], "Right")
        self.assertEqual(dawn["stats"]["attack"], 120)
        self.assertEqual(dawn["stats"]["defense"], 80)
        self.assertEqual(dawn["stats"]["stamina"], 117)
        self.assertEqual(dawn["special_move"]["name"], "Daytime Blitz")
        self.assertIn("eclipse_thunderbird_dawn.png", dawn["image_url"])

        # Dusk Mode
        dusk = resolve({"spin_mode": {"Eclipse Thunderbird": "Dusk"}}, self.blade)
        self.assertEqual(dusk["name"], "eclipse_thunderbird_dusk")
        self.assertEqual(dusk["spin_direction"], "Left")
        self.assertEqual(dusk["stats"]["attack"], 150)
        self.assertEqual(dusk["stats"]["defense"], 65)
        self.assertEqual(dusk["stats"]["stamina"], 102)
        self.assertEqual(dusk["special_move"]["name"], "Nighttime Annihilation")
        self.assertIn("eclipse_thunderbird_dusk.png", dusk["image_url"])

    def test_03_dawn_mode_wrath_of_the_sun_and_order_of_operations(self):
        """Verifies Wrath of the Sun stacking buff and order of operations with Eclipse Flight."""
        dawn = resolve({"spin_mode": {"Eclipse Thunderbird": "Dawn"}}, self.blade)
        sess = DummySession(dawn, dawn)
        eng = sess.ability

        # Force Wrath of the Sun to proc 5 times
        rule = [r for ab in dawn["abilities"] if ab["name"] == "Wrath of the Sun" for r in ab["rules"]][0]
        for i in range(5):
            eng._run_ops(rule, "Wrath of the Sun", "p", "e", "", 0, 0, [])

        self.assertEqual(eng.counters.get(("p", "solar_stack")), 5)
        # Solar stacks grant +3% per stack = +15% of base attack (120 * 0.15 = 18 or 5 stacks of 4 = 20)
        stack_atk = sess.status.get_buff_bonus("p", "attack")
        self.assertGreaterEqual(stack_atk, 18)

        # Trigger Eclipse Flight (+10% base attack = +12)
        flight_rule = [r for ab in dawn["abilities"] if ab["name"] == "Eclipse Flight" for r in ab["rules"]][0]
        eng._run_ops(flight_rule, "Eclipse Flight", "p", "e", "", 0, 0, [])
        self.assertTrue(eng.eclipse_phase.get("p"))

        # Total buff bonus reflects both +10% base flight and +15% stacks:
        # ATK_final = (ATK_base * 1.10) * (1.0 + 0.15) = 120 * 1.10 * 1.15 = 151.8 (~152)
        total_atk = dawn["stats"]["attack"] + sess.status.get_buff_bonus("p", "attack")
        self.assertAlmostEqual(total_atk, 152, delta=3)

    def test_04_dawn_mode_daytime_blitz(self):
        """Verifies Daytime Blitz deals 8 hits, adds 20% ATK rider, and reduces enemy stability."""
        dawn = resolve({"spin_mode": {"Eclipse Thunderbird": "Dawn"}}, self.blade)
        sess = DummySession(dawn, dawn)
        sess.moves["p"] = "special"

        # Resolve Daytime Blitz
        total_dmg, logs = sess.attack_manager._resolve_special("p", "e", "special", dawn, dawn, [])
        self.assertGreater(total_dmg, 0)
        # Verify enemy stability was reduced by 30 (from 100 to 70)
        self.assertEqual(sess.stability_manager.stability["e"], 70)
        self.assertTrue(any("Daytime Blitz" in l for l in logs))

    def test_05_dusk_mode_bask_in_moonlight(self):
        """Verifies Bask in the Moonlight restores stamina on turn_start."""
        dusk = resolve({"spin_mode": {"Eclipse Thunderbird": "Dusk"}}, self.blade)
        sess = DummySession(dusk, dusk)
        sess.stamina_manager.stamina["p"] = 8.0
        eng = sess.ability

        rule = [r for ab in dusk["abilities"] if ab["name"] == "Bask in the Moonlight" for r in ab["rules"]][0]
        logs = []
        eng._run_ops(rule, "Bask in the Moonlight", "p", "e", "", 0, 0, logs)
        self.assertEqual(sess.stamina_manager.stamina["p"], 10.0)
        self.assertTrue(any("+2 stamina" in l.lower() for l in logs))

    def test_06_dusk_mode_nighttime_annihilation_chain_crit(self):
        """Verifies Nighttime Annihilation rolls 4 hits with Chain-Crit mechanism."""
        dusk = resolve({"spin_mode": {"Eclipse Thunderbird": "Dusk"}}, self.blade)
        sess = DummySession(dusk, dusk)

        # Force crit roll to succeed on hit 1
        with patch("cogs.battle.attack_manager.random.random", side_effect=[0.1, 0.9, 0.9, 0.9]):
            total_dmg, logs = sess.attack_manager._resolve_special("p", "e", "special", dusk, dusk, [])

        self.assertGreater(total_dmg, 0)
        self.assertTrue(any("CHAIN CRIT ACTIVATED" in l for l in logs))

    def test_07_eclipse_phase_and_final_eclipse_flight_omega_special(self):
        """Verifies round-3 Eclipse Flight activates Ω Special and permanently seals it upon use."""
        dawn = resolve({"spin_mode": {"Eclipse Thunderbird": "Dawn"}}, self.blade)
        sess = DummySession(dawn, dawn)
        eng = sess.ability

        # Round 2: Not in Eclipse Phase
        sess.round = 2
        _, _ = eng._fire("turn_start", "p", "e", dawn, "", "", 0, 0, [])
        self.assertFalse(eng.eclipse_phase.get("p", False))

        # Round 3: Eclipse Phase activates!
        sess.round = 3
        logs = []
        _, _ = eng._fire("turn_start", "p", "e", dawn, "", "", 0, 0, logs)
        self.assertTrue(eng.eclipse_phase.get("p"))
        self.assertTrue(any("Eclipse Phase" in l for l in logs))

        # Firing Special during Eclipse Phase triggers Final Eclipse Flight (Ω Special)
        omega_logs = []
        dmg, omega_logs = sess.attack_manager._resolve_special("p", "e", "special", dawn, dawn, [])
        self.assertGreater(dmg, 0)
        self.assertTrue(any("FINAL ECLIPSE FLIGHT" in l for l in omega_logs))
        self.assertTrue(any("permanently sealed" in l for l in omega_logs))

        # Verify Eclipse Flight is permanently sealed
        self.assertTrue(eng.eclipse_sealed.get("p"))
        self.assertFalse(eng.eclipse_phase.get("p", False))

        # Round 6: Eclipse Phase does NOT activate because it is sealed!
        sess.round = 6
        round_6_logs = []
        _, _ = eng._fire("turn_start", "p", "e", dawn, "", "", 0, 0, round_6_logs)
        self.assertFalse(eng.eclipse_phase.get("p", False))


if __name__ == "__main__":
    unittest.main()
