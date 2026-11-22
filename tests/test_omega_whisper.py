"""
tests/test_omega_whisper.py
----------------------------
Comprehensive test suite verifying the Omega Whisper Exclusive Bit Beast card:
1. Avatar data loading, base stats (DEF: 165, STA: 69, ATK: 0, HP: 59), rarity, and type.
2. Bit Beast recognition and equip routing (is_bit_beast, equip_bit_beast).
3. Skill 1: Void Aegis (25⚡, 3 rounds, +25 Defense immediately, builds Shadow Ward stacks on hit).
4. Skill 2: Obsidian Barrier (50⚡, 2 rounds, 40% damage mitigation, negates enemy critical hit bonus damage).
5. Skill 3: Void Cataclysm (100⚡, deals 222 damage, cancels opponent attack buffs, restores 450 HP).
"""

import math
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch

from cogs.avatar.avatar_engine import AvatarEngine, avatar_engine
from cogs.avatar.avatar_utils import is_bit_beast
from cogs.battle.battle_skills import BattleSkillManager, PlayerSkillState


class TestOmegaWhisperData(unittest.TestCase):
    def setUp(self):
        self.engine = AvatarEngine()
        self.engine.load()

    def test_omega_whisper_card_exists_and_valid(self):
        card = self.engine.get_avatar("avatar_bb_omega_whisper")
        self.assertIsNotNone(card)
        self.assertEqual(card["name"], "Omega Whisper")
        self.assertEqual(card["rarity"], "Exclusive")
        self.assertEqual(card["type"], "defense")
        self.assertTrue(is_bit_beast(card))
        self.assertTrue(card.get("active_battle_skills"))

        # Base Stats (V1)
        bonuses = card["bonuses"]
        self.assertEqual(bonuses["attack_flat"], 0)
        self.assertEqual(bonuses["stamina_flat"], 69)
        self.assertEqual(bonuses["defence_flat"], 165)
        self.assertEqual(bonuses["hp_flat"], 90)

        # Skills
        skills = card["skills"]
        self.assertEqual(len(skills), 3)

        # Skill 1: Void Aegis | Energy: 25/100
        self.assertEqual(skills[0]["name"], "Void Aegis")
        self.assertEqual(skills[0]["cost"], 25)
        self.assertEqual(skills[0]["effect_type"], "void_aegis")
        self.assertEqual(skills[0]["defense"], 25)
        self.assertEqual(skills[0]["rounds"], 3)
        self.assertEqual(skills[0]["stack_defense"], 15)
        self.assertEqual(skills[0]["max_stacks"], 3)

        # Skill 2: Obsidian Barrier | Energy: 50/100
        self.assertEqual(skills[1]["name"], "Obsidian Barrier")
        self.assertEqual(skills[1]["cost"], 50)
        self.assertEqual(skills[1]["effect_type"], "obsidian_barrier")
        self.assertEqual(skills[1]["mitigate_pct"], 0.40)
        self.assertEqual(skills[1]["rounds"], 2)
        self.assertTrue(skills[1]["negate_crit"])

        # Skill 3: Void Cataclysm | Energy: 100/100
        self.assertEqual(skills[2]["name"], "Void Cataclysm")
        self.assertEqual(skills[2]["cost"], 100)
        self.assertEqual(skills[2]["effect_type"], "void_cataclysm")
        self.assertEqual(skills[2]["damage"], 222)
        self.assertEqual(skills[2]["heal"], 450)
        self.assertTrue(skills[2]["cancel_attack_buffs"])


class TestOmegaWhisperCombatSkills(unittest.TestCase):
    def setUp(self):
        card = avatar_engine.get_avatar("avatar_bb_omega_whisper")
        self.card = card
        self.session = SimpleNamespace(
            round=1,
            players=[SimpleNamespace(id=101), SimpleNamespace(id=102)],
            blades={
                "101": {"type": "Defense", "name": "Omega Whisper"},
                "102": {"type": "Attack", "name": "Valkyrie"},
            },
            avatar_cards={"101": card, "102": {}},
            bit_beast_cards={"101": card, "102": {}},
            hp={"101": 200, "102": 500},
            max_hp_per_player={"101": 800, "102": 800},
            max_hp=800,
            finished=False,
            moves={"101": "defense", "102": "attack"},
            status=SimpleNamespace(
                active_buffs={},
                add_buff=lambda key, stat, amt, dur, source=None, hostile=False: (
                    self.session.status.active_buffs.setdefault(key, []).append(
                        {"stat": stat, "amount": amt, "rounds_left": dur, "source": source}
                    )
                ),
            ),
        )
        self.sm = BattleSkillManager(self.session)

    def test_bit_beast_skills_retrieval(self):
        skills = self.sm.get_bit_beast_skills("101")
        self.assertEqual(len(skills), 3)
        self.assertEqual(skills[0]["name"], "Void Aegis")
        self.assertEqual(skills[0]["cost"], 25)
        self.assertEqual(skills[1]["name"], "Obsidian Barrier")
        self.assertEqual(skills[1]["cost"], 50)
        self.assertEqual(skills[2]["name"], "Void Cataclysm")
        self.assertEqual(skills[2]["cost"], 100)

    def test_skill1_void_aegis(self):
        # Give 25 energy to 101
        st = self.sm._state_for("101")
        st.bit_beast_energy = 25

        ok, msg = self.sm.select_bit_beast_skill("101", 1)
        self.assertTrue(ok)
        self.assertEqual(st.bit_beast_energy, 0)

        logs = []
        self.sm.begin_round(logs)

        # Check Void Aegis effect was placed
        effect = self.sm.effect("101", "void_aegis")
        self.assertIsNotNone(effect)
        self.assertEqual(effect["stacks"], 0)
        self.assertEqual(effect["max_stacks"], 3)
        self.assertEqual(effect["base_def"], 25)

        # Verify immediate +25 Defense buff added to status manager
        buffs = self.session.status.active_buffs.get("101", [])
        def_buffs = [b for b in buffs if b["stat"] == "defense" and b["source"] == "void_aegis"]
        self.assertEqual(len(def_buffs), 1)
        self.assertEqual(def_buffs[0]["amount"], 25)

        # Simulate absorbing 3 incoming hits
        # Hit 1
        self.sm.committed("102", "101", "attack", 50, logs)
        self.assertEqual(effect["stacks"], 1)
        shadow_buffs = [b for b in buffs if "shadow_ward" in str(b.get("source"))]
        self.assertEqual(len(shadow_buffs), 1)
        self.assertEqual(shadow_buffs[0]["amount"], 15)

        # Hit 2
        self.sm.committed("102", "101", "attack", 40, logs)
        self.assertEqual(effect["stacks"], 2)
        shadow_buffs = [b for b in buffs if "shadow_ward" in str(b.get("source"))]
        self.assertEqual(len(shadow_buffs), 2)

        # Hit 3
        self.sm.committed("102", "101", "attack", 30, logs)
        self.assertEqual(effect["stacks"], 3)
        shadow_buffs = [b for b in buffs if "shadow_ward" in str(b.get("source"))]
        self.assertEqual(len(shadow_buffs), 3)

        # Hit 4 (should be capped at max 3 stacks)
        self.sm.committed("102", "101", "attack", 20, logs)
        self.assertEqual(effect["stacks"], 3)
        shadow_buffs = [b for b in buffs if "shadow_ward" in str(b.get("source"))]
        self.assertEqual(len(shadow_buffs), 3)

    def test_skill2_obsidian_barrier(self):
        st = self.sm._state_for("101")
        st.bit_beast_energy = 50

        ok, msg = self.sm.select_bit_beast_skill("101", 2)
        self.assertTrue(ok)
        self.assertEqual(st.bit_beast_energy, 0)

        logs = []
        self.sm.begin_round(logs)

        effect = self.sm.effect("101", "obsidian_barrier")
        self.assertIsNotNone(effect)
        self.assertEqual(effect["pct"], 0.40)
        self.assertTrue(effect["negate_crit"])

        # Test normal damage mitigation (100 dmg reduced by 40% -> 60)
        mitigated = self.sm.mitigate("102", "101", "attack", 100, logs)
        self.assertEqual(mitigated, 60)

        # Test critical hit negation + 40% mitigation
        # With enemy crit (200 dmg, crit mult 2.0 -> base 100 -> reduced by 40% -> 60)
        self.session.ability = SimpleNamespace(last_hit_was_crit=True)
        mitigated_crit = self.sm.mitigate("102", "101", "attack", 200, logs)
        self.assertEqual(mitigated_crit, 60)

    def test_skill3_void_cataclysm(self):
        st = self.sm._state_for("101")
        st.bit_beast_energy = 100

        # Opponent has attack buffs
        self.session.status.active_buffs["102"] = [
            {"stat": "attack", "amount": 35, "rounds_left": 2},
            {"stat": "defense", "amount": 20, "rounds_left": 2},
            {"stat": "attack", "amount": 15, "rounds_left": 1},
        ]
        self.session.hp["102"] = 500
        self.session.hp["101"] = 200  # Will be healed +450 -> 650

        ok, msg = self.sm.select_bit_beast_skill("101", 3)
        self.assertTrue(ok)
        self.assertEqual(st.bit_beast_energy, 0)

        logs = []
        self.sm.begin_round(logs)

        # 1. Opponent took 222 damage (500 - 222 = 278)
        self.assertEqual(self.session.hp["102"], 278)

        # 2. Opponent's attack buffs were cancelled (defense buff remains)
        opp_buffs = self.session.status.active_buffs["102"]
        opp_atk_buffs = [b for b in opp_buffs if b["stat"] == "attack"]
        self.assertEqual(len(opp_atk_buffs), 0)
        opp_def_buffs = [b for b in opp_buffs if b["stat"] == "defense"]
        self.assertEqual(len(opp_def_buffs), 1)

        # 3. Wielder restored 450 HP (200 + 450 = 650)
        self.assertEqual(self.session.hp["101"], 650)


if __name__ == "__main__":
    unittest.main()
