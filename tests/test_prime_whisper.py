"""Tests for Prime Whisper:
1. Schema, stats, main_frame, default components (45 / 27.5 / 27.5), and spin direction.
2. Hex Plating: 15% physical damage reduction, 20% chance to drain stamina.
3. Demonic Gaze: Stacking buff on taking damage (+5 Special per hit, up to 5 stacks).
4. Gilded Purge: Defense-scaling Special Move applying 10% enemy Attack debuff for 2 turns.
5. Corruption (Awakened Dark Mode): Sub-40% HP awakening with +100 HP heal, +100 ATK, +80 SP,
   20% lifesteal, 2x stamina cost, and sprite swap.
6. Session Intercept Flow: Combat loop yield / awakening evaluation.
"""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from cogs.abilities.ability_engine import AbilityEngine
from cogs.battle.constants import MOVE_ATTACK, MOVE_DEFENSE, MOVE_SPECIAL
from cogs.battle.status_manager import StatusManager
from utils.character_registry import REGISTRY

ROOT = Path(__file__).resolve().parents[1]


class DummyStamina:
    def __init__(self, stamina=15.0, max_stamina=15.0):
        self.stamina = {"p": float(stamina), "e": float(stamina)}
        self.max_stamina = {"p": float(max_stamina), "e": float(max_stamina)}
        self.gauge = {"p": 0.0, "e": 0.0}
        self.drain_reduction = {}
        self.cost_increase = {}
        self.cost_increase_flat = {}
        self.cost_increase_moves = {}


class DummyChainHandler:
    def resolve(self, *args, **kwargs):
        return []


class DummySession:
    def __init__(self, blade_p, blade_e):
        self.hp = {"p": 1500, "e": 1500}
        self.max_hp_per_player = {"p": 1500, "e": 1500}
        self.max_hp = 1500
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


class TestPrimeWhisper(unittest.TestCase):

    def setUp(self):
        self.blade_path = ROOT / "beys" / "prime_whisper.json"
        with open(self.blade_path, "r", encoding="utf-8") as f:
            self.blade = json.load(f)

        self.foe_blade = {
            "name": "Target Dummy",
            "spin_direction": "Left",
            "type": "Attack",
            "rarity": "Legendary",
            "stats": {"hp": 150, "attack": 120, "defense": 100, "stamina": 100, "special": 100},
            "abilities": [],
        }

    def test_01_schema_and_component_split(self):
        """Prime Whisper must strictly adhere to schema, stats, and 45/27.5/27.5 split."""
        self.assertEqual(self.blade["id"], "BB170")
        self.assertEqual(self.blade["name"], "Prime Whisper")
        self.assertEqual(self.blade["type"], "Defense")
        self.assertEqual(self.blade["spin_direction"], "Right")
        self.assertEqual(self.blade["rarity"], "Mythic")

        # Stats validation
        stats = self.blade["stats"]
        self.assertEqual(stats["hp"], 150)
        self.assertEqual(stats["attack"], 120)
        self.assertEqual(stats["defense"], 140)
        self.assertEqual(stats["stamina"], 90)
        self.assertEqual(stats["special"], 100)

        # Parts existence and split validation
        disk_ref = self.blade["default_parts"]["disk"]
        driver_ref = self.blade["default_parts"]["driver"]
        disk = REGISTRY.part(disk_ref)
        driver = REGISTRY.part(driver_ref)
        self.assertIsNotNone(disk, f"Disk {disk_ref} must be registered")
        self.assertIsNotNone(driver, f"Driver {driver_ref} must be registered")

        mf = self.blade["main_frame"]
        for stat in ("hp", "attack", "defense", "stamina"):
            total = mf[stat] + disk["stats"][stat] + driver["stats"][stat]
            self.assertEqual(total, stats[stat], f"Component sum mismatch for {stat}")

    def test_02_hex_plating_damage_reduction_and_drain(self):
        """Hex Plating reduces incoming physical damage by 15% and has 20% stamina drain chance."""
        sess = DummySession(self.blade, self.foe_blade)
        engine = AbilityEngine(sess)
        sess.ability = engine

        # 1. Test 15% incoming damage reduction
        # Enemy deals 100 incoming damage to Prime Whisper ('p')
        logs = []
        dmg_dealt, dmg_taken = engine._fire(
            "on_take_damage", "p", "e", self.blade, MOVE_DEFENSE, "lose", 100, 0, logs
        )
        # 15% of 100 is 15 -> dmg_dealt reduced to 85
        self.assertEqual(dmg_dealt, 85)
        self.assertTrue(any("Hex Plating" in l and "reduced by 15%" in l for l in logs))

        # 2. Test 20% stamina drain proc
        sess.stamina_manager.stamina["e"] = 10.0
        sess.stamina_manager.stamina["p"] = 5.0
        logs_drain = []
        with patch("random.random", return_value=0.1):  # force proc (< 0.20)
            engine._fire(
                "on_take_damage", "p", "e", self.blade, MOVE_DEFENSE, "lose", 50, 0, logs_drain
            )
        # Enemy lost 1.5 stamina, Prime Whisper gained 1.5
        self.assertEqual(sess.stamina_manager.stamina["e"], 8.5)
        self.assertEqual(sess.stamina_manager.stamina["p"], 6.5)
        self.assertTrue(any("drained 1.5 stamina" in l for l in logs_drain))

    def test_03_demonic_gaze_stacking(self):
        """Demonic Gaze stacks +5 Special up to 5 times (+25 total) upon taking damage."""
        sess = DummySession(self.blade, self.foe_blade)
        engine = AbilityEngine(sess)
        sess.ability = engine

        # Simulate 6 hits of incoming damage
        for i in range(1, 7):
            logs = []
            engine._fire("on_take_damage", "p", "e", self.blade, MOVE_DEFENSE, "lose", 20, 0, logs)
            expected_stacks = min(5, i)
            self.assertEqual(engine.counters.get(("p", "demonic_gaze_stacks")), expected_stacks)
            self.assertEqual(sess.status.get_buff_bonus("p", "special"), expected_stacks * 5)

        # Total buff bonus capped at +25 Special
        self.assertEqual(sess.status.get_buff_bonus("p", "special"), 25)

    def test_04_gilded_purge_special_debuff(self):
        """Gilded Purge applies a 10% enemy Attack reduction for 2 turns."""
        sess = DummySession(self.blade, self.foe_blade)
        engine = AbilityEngine(sess)
        sess.ability = engine

        sm = self.blade["special_move"]
        self.assertEqual(sm["name"], "Gilded Purge")
        self.assertEqual(sm["damage_formula"]["defense"], 1.2)

        # Fire on_special
        logs = []
        engine._fire("on_special", "p", "e", self.blade, MOVE_SPECIAL, "win", 100, 0, logs)

        # Foe's base attack is 120 -> 10% reduction = -12 Attack
        self.assertEqual(sess.status.get_buff_bonus("e", "attack"), -12)
        self.assertTrue(any("enemy attack -12 (10%)" in l.lower() for l in logs))

    def test_05_corruption_awakened_dark_mode(self):
        """Corruption triggers when HP < 40%, heals 100 HP, boosts ATK +100 / SP +80, gives 20% lifesteal, and 2x stamina cost."""
        sess = DummySession(self.blade, self.foe_blade)
        engine = AbilityEngine(sess)
        sess.ability = engine

        # 1. At 50% HP (750 / 1500), Corruption should not trigger
        sess.hp["p"] = 750
        logs = []
        engine._fire("on_low_hp", "p", "e", self.blade, MOVE_ATTACK, "neutral", 0, 0, logs)
        self.assertFalse(self.blade.get("is_awakened", False))
        self.assertEqual(len(logs), 0)

        # 2. At 35% HP (525 / 1500 < 40%), Corruption triggers
        sess.hp["p"] = 525
        engine._fire("on_low_hp", "p", "e", self.blade, MOVE_ATTACK, "neutral", 0, 0, logs)

        # Heals +100 HP: 525 + 100 = 625
        self.assertEqual(sess.hp["p"], 625)

        # Attack +100 buff, Special +80 buff
        self.assertEqual(sess.status.get_buff_bonus("p", "attack"), 100)
        self.assertEqual(sess.status.get_buff_bonus("p", "special"), 80)

        # 20% lifesteal enabled
        self.assertEqual(engine.lifesteal_pct.get("p"), 20)

        # 2x stamina cost increase (+100%)
        self.assertEqual(sess.stamina_manager.cost_increase.get("p"), 1.0)

        # Image swapped to Awakened Dark Mode
        self.assertEqual(self.blade["image_url"], self.blade["awakened_image_url"])
        self.assertEqual(self.blade["name"], "Prime Whisper (Awakened Dark Mode)")

        # Verify logs
        log_text = " ".join(logs)
        self.assertIn("Corruption", log_text)
        self.assertIn("awakens in a new form", log_text)

    def test_06_session_intercept_headless_execution(self):
        """BattleSession._check_corruption_intercept triggers awakening when HP <= 40%."""
        from cogs.battle.session import BattleSession
        p1 = SimpleNamespace(id=1001, display_name="BladerOne", mention="<@1001>")
        p2 = SimpleNamespace(id=1002, display_name="BladerTwo", mention="<@1002>")

        # Create session with Prime Whisper on p1
        blade_copy = json.loads(json.dumps(self.blade))
        session = BattleSession.__new__(BattleSession)
        session.players = (p1, p2)
        session.channel = None  # Headless test mode
        session.blades = {"1001": blade_copy, "1002": dict(self.foe_blade)}
        session.hp = {"1001": 500, "1002": 1500}  # 500 / 1500 = 33.3% (< 40%)
        session.max_hp_per_player = {"1001": 1500, "1002": 1500}
        session.status = StatusManager(session)
        session.stamina_manager = DummyStamina()
        session.ability = AbilityEngine(session)
        session._is_npc = lambda p: False

        round_log = []
        import asyncio
        asyncio.run(session._check_corruption_intercept(round_log))

        # Check that awakening occurred
        self.assertTrue(blade_copy.get("is_awakened"))
        self.assertEqual(blade_copy["image_url"], self.blade["awakened_image_url"])
        self.assertEqual(session.hp["1001"], 600)  # 500 + 100 heal
        self.assertEqual(session.status.get_buff_bonus("1001", "attack"), 100)
        self.assertEqual(session.status.get_buff_bonus("1001", "special"), 80)
        self.assertEqual(session.ability.lifesteal_pct.get("1001"), 20)
        self.assertEqual(session.stamina_manager.cost_increase.get("1001"), 1.0)
        self.assertTrue(any("awakens into **Dark Mode**" in l for l in round_log))


if __name__ == "__main__":
    unittest.main()
