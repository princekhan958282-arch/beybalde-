"""Tests for Galactic Astral:
1. Blade schema, stats, main_frame, default components, and spin direction.
2. Nebula Resonance: 25% proc on physical Attack hit -> +30% ATK bonus damage, +120 HP heal, -1.0 stamina.
3. Event Horizon: Triggers when HP < 30% -> Defense drops to 0, +88 ATK transferred permanently, once per battle.
4. Galactic Rift Slash: 4-hit Special move (total 163) applying permanent 20% Defense Shatter debuff on final hit.
"""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from cogs.abilities.ability_engine import AbilityEngine
from cogs.battle.constants import MOVE_ATTACK, MOVE_SPECIAL, MOVE_DEFENSE
from cogs.battle.status_manager import StatusManager
from utils.character_registry import REGISTRY, load_beys

ROOT = Path(__file__).resolve().parents[1]


class DummyStamina:
    def __init__(self, stamina=15.0, max_stamina=15.0):
        self.stamina = {"p": float(stamina), "e": float(stamina)}
        self.max_stamina = {"p": float(max_stamina), "e": float(max_stamina)}
        self.gauge = {"p": 0.0, "e": 0.0}
        self.drain_reduction = {}


class DummyChainHandler:
    def resolve(self, *args, **kwargs):
        return []


class DummySession:
    def __init__(self, blade_p, blade_e):
        self.hp = {"p": 1440, "e": 1440}
        self.max_hp_per_player = {"p": 1440, "e": 1440}
        self.max_hp = 1440
        self.moves = {"p": "attack", "e": "defense"}
        self.last_moves = {"p": "attack", "e": "defense"}
        self.round = 1
        self.is_boss = False
        self.blades = {"p": blade_p, "e": blade_e}
        self.battle_stats = {
            "p": dict(blade_p["stats"]),
            "e": dict(blade_e["stats"]),
        }
        self.status = StatusManager(self)
        self.stamina_manager = DummyStamina()
        self.chain_handler = DummyChainHandler()
        self.ability = None


class TestGalacticAstral(unittest.TestCase):

    def setUp(self):
        self.blade_path = ROOT / "beys" / "galactic_astral.json"
        with open(self.blade_path, "r", encoding="utf-8") as f:
            self.blade = json.load(f)

        self.foe_blade = {
            "name": "Target Dummy",
            "spin_direction": "Left",
            "type": "Defense",
            "rarity": "Legendary",
            "stats": {"hp": 150, "attack": 100, "defense": 100, "stamina": 100, "special": 100},
            "abilities": [],
        }

    def test_01_schema_and_components(self):
        """Galactic Astral must meet all authored requirements and component split invariants."""
        self.assertEqual(self.blade["id"], "BB169")
        self.assertEqual(self.blade["name"], "Galactic Astral")
        self.assertIn("Galactic astral", self.blade.get("aliases", []))
        self.assertEqual(self.blade["type"], "Attack")
        self.assertEqual(self.blade["spin_direction"], "Right")
        self.assertEqual(self.blade["rarity"], "Mythic")
        self.assertTrue(self.blade.get("tournament_exclusive"))
        self.assertFalse(self.blade.get("booster_exclusive"))

        # Stats validation
        stats = self.blade["stats"]
        self.assertEqual(stats["hp"], 144)
        self.assertEqual(stats["attack"], 176)
        self.assertEqual(stats["defense"], 88)
        self.assertEqual(stats["stamina"], 120)
        self.assertEqual(stats["special"], 163)

        # Registry and component check
        disk_ref = self.blade["default_parts"]["disk"]
        driver_ref = self.blade["default_parts"]["driver"]
        disk = REGISTRY.part(disk_ref)
        driver = REGISTRY.part(driver_ref)
        self.assertIsNotNone(disk, f"Disk {disk_ref} must be registered")
        self.assertIsNotNone(driver, f"Driver {driver_ref} must be registered")

        mf = self.blade["main_frame"]
        for stat in ("hp", "attack", "defense", "stamina"):
            total = mf[stat] + disk["stats"][stat] + driver["stats"][stat]
            self.assertEqual(total, stats[stat], f"Stat sum mismatch for {stat}")

    def test_02_nebula_resonance(self):
        """Nebula Resonance procs on Attack hit, dealing 30% ATK damage, healing 120 HP, and consuming 1 stamina."""
        sess = DummySession(self.blade, self.foe_blade)
        engine = AbilityEngine(sess)
        sess.ability = engine

        # Set HP to 500 so heal of 120 is visible
        sess.hp["p"] = 500
        sess.stamina_manager.stamina["p"] = 10.0

        logs = []
        # Force chance check to succeed
        with patch("random.random", return_value=0.1):
            dmg_dealt, dmg_taken = engine._fire(
                "on_attack_hit", "p", "e", self.blade, MOVE_ATTACK, "win", 50, 0, logs
            )

        # 30% of 176 ATK = 52.8 -> 53 bonus damage
        expected_bonus = round(176 * 0.3)
        self.assertEqual(dmg_dealt, 50 + expected_bonus)

        # Heals 120 HP: 500 + 120 = 620
        self.assertEqual(sess.hp["p"], 620)

        # Consumes 1.0 stamina: 10.0 - 1.0 = 9.0
        self.assertEqual(sess.stamina_manager.stamina["p"], 9.0)

        # Check logs
        log_text = " ".join(logs)
        self.assertIn("Nebula Resonance", log_text)
        self.assertIn("healed", log_text.lower())
        self.assertIn("stamina", log_text.lower())

    def test_03_event_horizon_threshold(self):
        """Event Horizon triggers below 30% HP, drops Defense to 0, transfers 88 to Attack, fires once."""
        sess = DummySession(self.blade, self.foe_blade)
        engine = AbilityEngine(sess)
        sess.ability = engine

        # 1. At 50% HP (720 / 1440), Event Horizon should NOT trigger
        sess.hp["p"] = 720
        logs = []
        engine._fire("on_low_hp", "p", "e", self.blade, MOVE_ATTACK, "neutral", 0, 0, logs)
        self.assertEqual(sess.status.get_buff_bonus("p", "attack"), 0)
        self.assertEqual(sess.status.get_buff_bonus("p", "defense"), 0)
        self.assertEqual(len(logs), 0)

        # 2. At 25% HP (360 / 1440 < 30%), Event Horizon MUST trigger
        sess.hp["p"] = 360
        logs = []
        engine._fire("on_low_hp", "p", "e", self.blade, MOVE_ATTACK, "neutral", 0, 0, logs)

        # Defense drops to 0 (-88 buff), Attack gains +88
        self.assertEqual(sess.status.get_buff_bonus("p", "defense"), -88)
        self.assertEqual(sess.status.get_buff_bonus("p", "attack"), 88)

        log_text = " ".join(logs)
        self.assertIn("Event Horizon", log_text)
        self.assertIn("Defense drops to 0", log_text)
        self.assertIn("+88 transferred to Attack", log_text)

        # 3. Should NOT fire a second time (once: battle)
        logs2 = []
        engine._fire("on_low_hp", "p", "e", self.blade, MOVE_ATTACK, "neutral", 0, 0, logs2)
        self.assertEqual(len(logs2), 0)
        # Buff amounts must remain unchanged
        self.assertEqual(sess.status.get_buff_bonus("p", "defense"), -88)
        self.assertEqual(sess.status.get_buff_bonus("p", "attack"), 88)

    def test_04_galactic_rift_slash_shatter(self):
        """Galactic Rift Slash applies Shatter debuff on final hit, permanently reducing enemy DEF by 20%."""
        sess = DummySession(self.blade, self.foe_blade)
        engine = AbilityEngine(sess)
        sess.ability = engine

        sm = self.blade["special_move"]
        self.assertEqual(sm["name"], "Galactic Rift Slash")
        self.assertEqual(sm["hits"], 4)
        self.assertEqual(sm["total_damage"], 163)
        self.assertEqual(sum(sm["damage_per_hit"]), 163)

        # Test firing on_special_last_hit
        logs = []
        engine._fire("on_special_last_hit", "p", "e", self.blade, MOVE_SPECIAL, "win", 43, 0, logs)

        # Foe's base defense is 100 -> 20% reduction = -20 DEF
        self.assertEqual(sess.status.get_buff_bonus("e", "defense"), -20)
        self.assertEqual(sess.status.shatter_stacks.get("e", 0), 1)

        log_text = " ".join(logs)
        self.assertIn("Shatter", log_text)
        self.assertIn("enemy defense -20 (20%) (permanent)!", log_text)

    def test_05_full_special_barrage_application(self):
        """In a full 4-hit Special resolution via engine.apply, Shatter applies on the last hit."""
        sess = DummySession(self.blade, self.foe_blade)
        engine = AbilityEngine(sess)
        sess.ability = engine

        # Hits 0, 1, 2 should NOT trigger on_special_last_hit
        for hit_n in range(3):
            _, _, logs = engine.apply(
                "p", "e", self.blade, self.foe_blade, MOVE_SPECIAL, "win", 40, 0,
                is_first_hit=(hit_n == 0),
                is_last_hit=False
            )
            # Enemy defense has not yet been shattered
            self.assertEqual(sess.status.get_buff_bonus("e", "defense"), 0)

        # Hit 3 (is_last_hit=True) triggers Shatter
        _, _, logs = engine.apply(
            "p", "e", self.blade, self.foe_blade, MOVE_SPECIAL, "win", 43, 0,
            is_first_hit=False,
            is_last_hit=True
        )
        self.assertEqual(sess.status.get_buff_bonus("e", "defense"), -20)
        self.assertEqual(sess.status.shatter_stacks.get("e", 0), 1)
        self.assertTrue(any("Shatter" in l for l in logs))


if __name__ == "__main__":
    unittest.main()
