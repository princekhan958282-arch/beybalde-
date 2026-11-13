"""
tests/test_blader_bit_beast_skills.py
--------------------------------------
Comprehensive test suite verifying the new Blader and Bit Beast Battle Skill system:
1. Blader energy initialization (100) and Bit Beast energy initialization (0).
2. Bit Beast turn-by-turn automatic charging (+15/turn up to 100 max cap).
3. Idempotent once-per-turn charging (no duplicate charging on re-renders).
4. Blader skill validation, immediate deduction, and unaffordable prevention.
5. Bit Beast skill validation, immediate deduction, and unaffordable prevention.
6. Prevention of negative energy and prevention of duplicate activations in the same round.
7. Independent activation of both Blader and Bit Beast skills in the same round.
8. UI Picker views disabling unaffordable buttons.
9. Energy reset when starting a new battle session.
10. End-to-end combat integration and battle log emission in BattleSession.
"""

import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.battle.battle_skills import (
    BattleSkillManager,
    PlayerSkillState,
    BLADER_START_ENERGY,
    BIT_BEAST_START_ENERGY,
    BIT_BEAST_CHARGE_PER_TURN,
    MAX_ENERGY,
    STANDARD_SKILL_COSTS,
    DEFAULT_BIT_BEAST_SKILLS,
    DEFAULT_BLADER_SKILLS,
)
from cogs.battle.skill_views import BladerSkillPicker, BitBeastSkillPicker
from tools.sim_battle_ticks import build_pvp_session, play_round, FakeChannel, FakePlayer, get_beyblade


class TestPlayerSkillState(unittest.TestCase):
    def test_default_initialization(self):
        st = PlayerSkillState()
        self.assertEqual(st.blader_energy, 100)
        self.assertEqual(st.bit_beast_energy, 0)
        self.assertEqual(st.last_charged_turn, 0)
        self.assertIsNone(st.blader_pending)
        self.assertIsNone(st.bit_beast_pending)
        self.assertFalse(st.blader_energy_deducted)

    def test_backward_compatible_properties(self):
        st = PlayerSkillState(energy=80, pending=2)
        self.assertEqual(st.energy, 80)
        self.assertEqual(st.blader_energy, 80)
        self.assertEqual(st.pending, 2)
        self.assertEqual(st.blader_pending, 2)

        st.energy = 55
        self.assertEqual(st.energy, 55)
        self.assertEqual(st.blader_energy, 55)

        st.energy = 150  # Cap check
        self.assertEqual(st.blader_energy, 100)

        st.energy = -10  # Min check
        self.assertEqual(st.blader_energy, 0)


class TestBitBeastCharging(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(
            round=1,
            players=[SimpleNamespace(id=101), SimpleNamespace(id=102)],
            blades={"101": {"type": "Attack", "name": "Dragoon"}, "102": {"type": "Defense", "name": "Draciel"}},
            avatar_cards={},
            hp={"101": 100, "102": 100},
            max_hp_per_player={"101": 100, "102": 100},
            max_hp=100,
            finished=False,
            moves={},
        )
        self.sm = BattleSkillManager(self.session)

    def test_turn_by_turn_charging(self):
        # Initial turn 0: energy is 0
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 0)
        self.assertEqual(self.sm.get_bit_beast_energy("102"), 0)

        # Turn 1 -> 15
        self.sm.on_turn_start(1)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 15)
        self.assertEqual(self.sm.get_bit_beast_energy("102"), 15)

        # Turn 2 -> 30
        self.sm.on_turn_start(2)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 30)

        # Turn 3 -> 45
        self.sm.on_turn_start(3)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 45)

        # Turn 4 -> 60
        self.sm.on_turn_start(4)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 60)

        # Turn 5 -> 75
        self.sm.on_turn_start(5)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 75)

        # Turn 6 -> 90
        self.sm.on_turn_start(6)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 90)

        # Turn 7 -> 100 (capped at 100, not 105)
        self.sm.on_turn_start(7)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 100)

        # Turn 8+ -> remains 100
        self.sm.on_turn_start(8)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 100)

    def test_idempotent_charging_strictly_once_per_turn(self):
        # Calling on_turn_start multiple times in the same turn must only charge +15 once
        self.sm.on_turn_start(1)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 15)

        self.sm.on_turn_start(1)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 15)

        self.sm.on_turn_start(1)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 15)

        # Moving to Turn 2 charges once
        self.sm.on_turn_start(2)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 30)
        self.sm.on_turn_start(2)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 30)


class TestBladerSkillValidationAndDeduction(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(
            round=1,
            players=[SimpleNamespace(id=101), SimpleNamespace(id=102)],
            blades={"101": {"type": "Attack"}, "102": {"type": "Defense"}},
            avatar_cards={"101": {"name": "Tyson", "character": "tyson", "skills": [
                {"name": "Skill 1", "energy_cost": 25},
                {"name": "Skill 2", "energy_cost": 50},
                {"name": "Skill 3", "energy_cost": 75},
            ]}},
            hp={"101": 100, "102": 100},
            max_hp_per_player={"101": 100, "102": 100},
            max_hp=100,
            finished=False,
            moves={},
        )
        self.sm = BattleSkillManager(self.session)

    def test_initial_blader_energy(self):
        self.assertEqual(self.sm.get_blader_energy("101"), 100)

    def test_immediate_deduction_and_affordability(self):
        # Slot 1 costs 25: 100 -> 75
        can, reason = self.sm.can_activate_blader_skill("101", 1)
        self.assertTrue(can)
        self.assertIsNone(reason)

        ok, msg = self.sm.select_blader_skill("101", 1)
        self.assertTrue(ok)
        self.assertEqual(self.sm.get_blader_energy("101"), 75)

        # Cannot activate a second Blader skill in the same round
        can2, reason2 = self.sm.can_activate_blader_skill("101", 1)
        self.assertFalse(can2)
        self.assertIn("already selected", reason2)

    def test_insufficient_energy_prevention(self):
        st = self.sm._state_for("101")
        st.blader_energy = 20  # less than 25

        can, reason = self.sm.can_activate_blader_skill("101", 1)
        self.assertFalse(can)
        self.assertIn("Need 25⚡", reason)

        ok, msg = self.sm.select_blader_skill("101", 1)
        self.assertFalse(ok)
        self.assertEqual(st.blader_energy, 20)  # Energy unchanged

    def test_no_double_deduction_on_round_begin(self):
        # Select Blader skill 1 (cost 25)
        ok, _ = self.sm.select_blader_skill("101", 1)
        self.assertTrue(ok)
        self.assertEqual(self.sm.get_blader_energy("101"), 75)

        # Begin round
        logs = []
        self.sm.begin_round(logs)
        # Blader energy must remain 75, NOT double-deducted to 50!
        self.assertEqual(self.sm.get_blader_energy("101"), 75)


class TestBitBeastSkillValidationAndDeduction(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(
            round=1,
            players=[SimpleNamespace(id=101), SimpleNamespace(id=102)],
            blades={"101": {"type": "Attack", "name": "Dragoon V"}, "102": {"type": "Defense", "name": "Draciel V"}},
            avatar_cards={},
            hp={"101": 100, "102": 100},
            max_hp_per_player={"101": 100, "102": 100},
            max_hp=100,
            finished=False,
            moves={},
        )
        self.sm = BattleSkillManager(self.session)

    def test_turn_1_all_bit_beast_skills_disabled(self):
        self.sm.on_turn_start(1)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 15)

        # Skills cost 25, 50, 75. At 15 energy, none can be activated.
        for slot in (1, 2, 3):
            can, reason = self.sm.can_activate_bit_beast_skill("101", slot)
            self.assertFalse(can)
            self.assertIn("charging +15/turn", reason)

    def test_turn_2_affords_skill_1_only(self):
        self.sm.on_turn_start(1)
        self.sm.on_turn_start(2)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 30)

        # Skill 1 (25) is affordable
        can1, _ = self.sm.can_activate_bit_beast_skill("101", 1)
        self.assertTrue(can1)

        # Skill 2 (50) and 3 (75) are unaffordable
        can2, _ = self.sm.can_activate_bit_beast_skill("101", 2)
        can3, _ = self.sm.can_activate_bit_beast_skill("101", 3)
        self.assertFalse(can2)
        self.assertFalse(can3)

        # Immediate deduction: 30 -> 5
        ok, msg = self.sm.select_bit_beast_skill("101", 1)
        self.assertTrue(ok)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 5)

        # Duplicate in same round prevented
        can_again, reason_again = self.sm.can_activate_bit_beast_skill("101", 1)
        self.assertFalse(can_again)
        self.assertIn("already selected", reason_again)

    def test_independent_coexistence_with_blader_skill(self):
        # Charge to 60 energy
        for turn in range(1, 5):
            self.sm.on_turn_start(turn)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 60)
        self.assertEqual(self.sm.get_blader_energy("101"), 100)

        # Player activates BOTH Blader Skill 1 (cost 25) AND Bit Beast Skill 2 (cost 50)
        ok1, _ = self.sm.select_blader_skill("101", 1)
        ok2, _ = self.sm.select_bit_beast_skill("101", 2)

        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertEqual(self.sm.get_blader_energy("101"), 75)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 10)

        # Resolve round
        logs = []
        self.sm.begin_round(logs)
        self.assertTrue(any("Dragoon V: Storm Surge" in l for l in logs))


class TestSkillViews(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(
            round=1,
            players=[SimpleNamespace(id=101), SimpleNamespace(id=102)],
            blades={"101": {"type": "Attack", "name": "Dragoon"}, "102": {"type": "Defense", "name": "Draciel"}},
            avatar_cards={},
            hp={"101": 100, "102": 100},
            max_hp_per_player={"101": 100, "102": 100},
            max_hp=100,
            finished=False,
            moves={},
        )
        self.sm = BattleSkillManager(self.session)
        self.session.skills_manager = self.sm

    def test_blader_view_initial_state(self):
        view = BladerSkillPicker(self.session, "101")
        # Player has 100 energy, so skills costing 25, 50, 75 are all enabled
        buttons = [item for item in view.children if hasattr(item, "disabled")]
        self.assertEqual(len(buttons), 3)
        self.assertFalse(buttons[0].disabled)
        self.assertFalse(buttons[1].disabled)
        self.assertFalse(buttons[2].disabled)

    def test_bit_beast_view_initial_state(self):
        # Bit Beast has 0 energy initially
        view = BitBeastSkillPicker(self.session, "101")
        buttons = [item for item in view.children if hasattr(item, "disabled")]
        self.assertEqual(len(buttons), 3)
        # All buttons must be disabled because 0 < 25
        self.assertTrue(buttons[0].disabled)
        self.assertTrue(buttons[1].disabled)
        self.assertTrue(buttons[2].disabled)

    def test_bit_beast_view_after_charging(self):
        # Charge to Turn 2 (30 energy)
        self.sm.on_turn_start(1)
        self.sm.on_turn_start(2)
        self.assertEqual(self.sm.get_bit_beast_energy("101"), 30)

        view = BitBeastSkillPicker(self.session, "101")
        buttons = [item for item in view.children if hasattr(item, "disabled")]
        # Button 1 (cost 25) enabled, Button 2 (cost 50) disabled, Button 3 (cost 75) disabled
        self.assertFalse(buttons[0].disabled)
        self.assertTrue(buttons[1].disabled)
        self.assertTrue(buttons[2].disabled)


class TestLiveBattleSessionIntegration(unittest.TestCase):
    def test_live_session_energy_charging_and_reset(self):
        s, ch, pa, pb = build_pvp_session()
        ka, kb = str(pa.id), str(pb.id)

        self.assertIsNotNone(s.skills_manager)
        # Starting energies
        self.assertEqual(s.skills_manager.get_blader_energy(ka), 100)
        self.assertEqual(s.skills_manager.get_bit_beast_energy(ka), 0)

        # Turn 1 start charging
        s.skills_manager.on_turn_start(1)
        self.assertEqual(s.skills_manager.get_bit_beast_energy(ka), 15)

        # A brand new session resets energy to fresh baseline
        s2, ch2, pa2, pb2 = build_pvp_session()
        ka2 = str(pa2.id)
        self.assertEqual(s2.skills_manager.get_blader_energy(ka2), 100)
        self.assertEqual(s2.skills_manager.get_bit_beast_energy(ka2), 0)

    def test_live_round_resolution_with_skills(self):
        s, ch, pa, pb = build_pvp_session()
        ka, kb = str(pa.id), str(pb.id)

        # Advance to Turn 2 so Bit Beast has 30 energy
        s.skills_manager.on_turn_start(1)
        s.round = 2
        s.skills_manager.on_turn_start(2)
        self.assertEqual(s.skills_manager.get_bit_beast_energy(ka), 30)
        self.assertEqual(s.skills_manager.get_blader_energy(ka), 100)

        # Player A activates Blader Skill 1 (25 cost) and Bit Beast Skill 1 (25 cost)
        ok1, _ = s.skills_manager.select_blader_skill(ka, 1)
        ok2, _ = s.skills_manager.select_bit_beast_skill(ka, 1)
        self.assertTrue(ok1)
        self.assertTrue(ok2)

        self.assertEqual(s.skills_manager.get_blader_energy(ka), 75)
        self.assertEqual(s.skills_manager.get_bit_beast_energy(ka), 5)

        # Play round with moves: Player A attacks, Player B defends
        asyncio.run(play_round(s, "attack", "defense"))

        # Verify round resolved and battle progressed
        self.assertGreaterEqual(s.round, 3)
        # Verify Player A blader energy remains 75 (not double deducted)
        self.assertEqual(s.skills_manager.get_blader_energy(ka), 75)
        # Bit Beast energy should have charged for round 3 (5 + 15 = 20)
        self.assertEqual(s.skills_manager.get_bit_beast_energy(ka), 20)


class TestNewBitBeastAvatarsAndShop(unittest.TestCase):
    def setUp(self):
        from cogs.avatar.avatar_engine import avatar_engine
        avatar_engine.load()
        self.engine = avatar_engine

    def test_all_four_bit_beasts_exist_and_validate(self):
        names = ["Wonder Valkyrie", "Dragoon", "Driger", "Dranzer"]
        avatars_by_name = {a["name"]: a for a in self.engine.get_all_avatars()}
        for name in names:
            self.assertIn(name, avatars_by_name, f"{name} should be registered in avatar_engine")
            card = avatars_by_name[name]
            self.assertEqual(card["rarity"], "Bit Beast")
            self.assertTrue(card.get("active_battle_skills"))
            skills = card.get("skills", [])
            self.assertEqual(len(skills), 3, f"{name} should have exactly 3 skills")
            self.assertEqual(skills[0]["cost"], 25)
            self.assertEqual(skills[1]["cost"], 50)
            self.assertEqual(skills[2]["cost"], 75)

    def test_bit_beast_pack_in_shop(self):
        from cogs.avatar.avatar_shop import (
            PACK_POOL,
            PACK_GUARANTEE,
            PACK_PRICE,
            PACK_DISPLAY,
            PACK_EMOJI,
            PACK_PULLS,
            DUPE_REFUND_RATE,
        )
        self.assertIn("bit_beast", PACK_POOL)
        self.assertEqual(PACK_POOL["bit_beast"], ["Bit Beast"])
        self.assertEqual(PACK_GUARANTEE["bit_beast"], "Bit Beast")
        self.assertGreater(PACK_PRICE["bit_beast"], 0)
        self.assertEqual(PACK_DISPLAY["bit_beast"], "Bit Beast")
        self.assertEqual(PACK_EMOJI["bit_beast"], "🐉")
        self.assertEqual(PACK_PULLS["bit_beast"], 1)
        self.assertIn("Bit Beast", DUPE_REFUND_RATE)

    def test_bit_beast_signature_skills_retrieval(self):
        # 1. Equipped Wonder Valkyrie card
        card_wv = self.engine.get_avatar("avatar_bb_wonder_valkyrie")
        session_wv = SimpleNamespace(
            round=1,
            players=[SimpleNamespace(id=101), SimpleNamespace(id=102)],
            blades={"101": {"type": "Attack", "name": "Brave Valkyrie"}, "102": {"type": "Defense"}},
            avatar_cards={"101": card_wv, "102": {}},
            hp={"101": 100, "102": 100},
            max_hp_per_player={"101": 100, "102": 100},
            max_hp=100,
            finished=False,
            moves={},
        )
        sm_wv = BattleSkillManager(session_wv)
        bb_skills = sm_wv.get_bit_beast_skills("101")
        self.assertEqual([s["name"] for s in bb_skills], ["Wonder Attack", "Wonder Reboot", "Valkyrie Winged Storm"])
        blader_skills = sm_wv.get_blader_skills("101")
        self.assertEqual([s["name"] for s in blader_skills], ["Blader Focus", "Tactical Guard", "Blader's Awakening"])

        # 2. Equipped Dragoon card
        card_dr = self.engine.get_avatar("avatar_bb_dragoon")
        session_dr = SimpleNamespace(
            round=1,
            players=[SimpleNamespace(id=101), SimpleNamespace(id=102)],
            blades={"101": {"type": "Attack", "name": "Dragoon Fantom"}, "102": {"type": "Defense"}},
            avatar_cards={"101": card_dr, "102": {}},
            hp={"101": 100, "102": 100},
            max_hp_per_player={"101": 100, "102": 100},
            max_hp=100,
            finished=False,
            moves={},
        )
        sm_dr = BattleSkillManager(session_dr)
        bb_skills_dr = sm_dr.get_bit_beast_skills("101")
        self.assertEqual([s["name"] for s in bb_skills_dr], ["Hyper Tornado", "Storm Surge", "Galaxy Storm"])

        # 3. Driger
        card_dg = self.engine.get_avatar("avatar_bb_driger")
        session_dg = SimpleNamespace(
            round=1,
            players=[SimpleNamespace(id=101), SimpleNamespace(id=102)],
            blades={"101": {"type": "Balance", "name": "Driger V"}, "102": {"type": "Defense"}},
            avatar_cards={"101": card_dg, "102": {}},
            hp={"101": 100, "102": 100},
            max_hp_per_player={"101": 100, "102": 100},
            max_hp=100,
            finished=False,
            moves={},
        )
        sm_dg = BattleSkillManager(session_dg)
        bb_skills_dg = sm_dg.get_bit_beast_skills("101")
        self.assertEqual([s["name"] for s in bb_skills_dg], ["Tiger Claw", "Vulcan Claw", "Thunder Slash Claw"])

        # 4. Dranzer
        card_dz = self.engine.get_avatar("avatar_bb_dranzer")
        session_dz = SimpleNamespace(
            round=1,
            players=[SimpleNamespace(id=101), SimpleNamespace(id=102)],
            blades={"101": {"type": "Balance", "name": "Dranzer Spiral"}, "102": {"type": "Defense"}},
            avatar_cards={"101": card_dz, "102": {}},
            hp={"101": 100, "102": 100},
            max_hp_per_player={"101": 100, "102": 100},
            max_hp=100,
            finished=False,
            moves={},
        )
        sm_dz = BattleSkillManager(session_dz)
        bb_skills_dz = sm_dz.get_bit_beast_skills("101")
        self.assertEqual([s["name"] for s in bb_skills_dz], ["Flame Saber", "Dranzer Rebirth", "Plasma Saber"])

    def test_bit_beast_combat_effects(self):
        card_dz = self.engine.get_avatar("avatar_bb_dranzer")
        session = SimpleNamespace(
            round=1,
            players=[SimpleNamespace(id=101), SimpleNamespace(id=102)],
            blades={"101": {"type": "Balance", "name": "Dranzer"}, "102": {"type": "Defense"}},
            avatar_cards={"101": card_dz, "102": {}},
            hp={"101": 50, "102": 100},
            max_hp_per_player={"101": 100, "102": 100},
            max_hp=100,
            finished=False,
            moves={"101": "attack", "102": "defense"},
        )
        sm = BattleSkillManager(session)
        # Advance turn so Dranzer has 60 energy
        for t in range(1, 5):
            sm.on_turn_start(t)
        self.assertEqual(sm.get_bit_beast_energy("101"), 60)

        # Select Dranzer Rebirth (cost 50: 40 shield and 25 heal)
        ok, msg = sm.select_bit_beast_skill("101", 2)
        self.assertTrue(ok)
        self.assertEqual(sm.get_bit_beast_energy("101"), 10)

        logs = []
        sm.begin_round(logs)

        # Check heal landed: 50 + 25 = 75
        self.assertEqual(session.hp["101"], 75)
        # Check shield landed: 40 HP
        shield_effect = sm.effect("101", "shield")
        self.assertIsNotNone(shield_effect)
        self.assertEqual(shield_effect.get("hp"), 40)


if __name__ == "__main__":
    unittest.main()
