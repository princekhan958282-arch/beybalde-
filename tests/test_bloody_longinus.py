"""End-to-end tests for BB001 Bloody Longinus and new AbilityEngine hooks."""
import copy
import math
import unittest
from dataclasses import replace
from unittest.mock import patch

from cogs.avatar import NULL_BONUSES
from cogs.battle.purification import effective_stats
from tools.test_repeated_battle_audit import round_trace, session
from utils.character_registry import load_beys


class BloodyLonginusTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_and_dual_block_sync(self):
        beys = load_beys()
        blade = beys["Bloody Longinus"]
        self.assertEqual(blade["id"], "BB001")
        self.assertEqual(blade["ability"], blade["abilities"][0])
        self.assertEqual(blade["special_move"]["total_damage"], 200)
        self.assertEqual(blade["special_move"]["damage_per_hit"], 200)

    async def test_squall_stacking_proc_and_fifth_hit_reset_in_both_seats(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                s, a, e = await session("Bloody Longinus", "Victory Valkyrie", level=1, reverse=reverse)
                s.hp[a] = s.max_hp_per_player[a] = 5000
                s.hp[e] = s.max_hp_per_player[e] = 5000

                # No Left-spin bonuses vs Right-spin Victory Valkyrie
                self.assertEqual(s.status.get_buff_bonus(a, "defense"), 0)
                self.assertEqual(s.status.get_buff_bonus(a, "stamina"), 0)
                self.assertEqual(s.ability.recoil_flat_mod.get(a, 0), 0)

                # Win 1 (no proc with roll=0.99) vs Win 1 (proc with roll=0.1)
                t_no_proc = await round_trace(s, a, e, "attack", "charge", roll=0.99)
                self.assertEqual(s.ability.counters[a, "squall"], 1)
                self.assertEqual(s.ability.counters[a, "squall_cycle"], 1)
                self.assertEqual(s.status.get_buff_bonus(a, "attack"), 14)

                # Verify 40% self-recoil on damage dealt
                expected_recoil = math.ceil(t_no_proc["damage"][a] * 0.40)
                self.assertEqual(t_no_proc["hp_before"][a] - t_no_proc["hp_after"][a], expected_recoil)

                # Wins 2..4 build up to 4 stacks (+56 ATK)
                for expected_stack in (2, 3, 4):
                    await round_trace(s, a, e, "attack", "charge", roll=0.99)
                    self.assertEqual(s.ability.counters[a, "squall"], expected_stack)
                    self.assertEqual(s.ability.counters[a, "squall_cycle"], expected_stack)
                    self.assertEqual(s.status.get_buff_bonus(a, "attack"), expected_stack * 14)

                # Win 5 triggers 150% + guaranteed crit finisher, resets cycle cleanly, does NOT immediately rebuild
                t5 = await round_trace(s, a, e, "attack", "charge", roll=0.1)
                self.assertIn("FOUR-DRAGON UPPER (5th Hit)", t5["logs"])
                self.assertEqual(s.ability.counters[a, "squall"], 0)
                self.assertEqual(s.ability.counters[a, "squall_cycle"], 0)
                self.assertEqual(s.status.get_buff_bonus(a, "attack"), 0)
                self.assertEqual(s.ability.guaranteed_crit_turns.get(a, 0), 0)

                # Win 6 starts next cycle cleanly at 1 stack (+14 ATK)
                await round_trace(s, a, e, "attack", "charge", roll=0.99)
                self.assertEqual(s.ability.counters[a, "squall"], 1)
                self.assertEqual(s.ability.counters[a, "squall_cycle"], 1)
                self.assertEqual(s.status.get_buff_bonus(a, "attack"), 14)

    async def test_40_pct_atk_proc_adds_bonus_damage(self):
        s1, a1, e1 = await session("Bloody Longinus", "Victory Valkyrie", level=1)
        s2, a2, e2 = await session("Bloody Longinus", "Victory Valkyrie", level=1)
        for s, a, e in ((s1, a1, e1), (s2, a2, e2)):
            s.hp[a] = s.max_hp_per_player[a] = 5000
            s.hp[e] = s.max_hp_per_player[e] = 5000

        t_miss = await round_trace(s1, a1, e1, "attack", "charge", roll=0.99)
        atk_stat = s2.battle_stats[a2]["attack"]
        t_proc = await round_trace(s2, a2, e2, "attack", "charge", roll=0.1)
        expected_bonus = int(round(atk_stat * 0.5))
        self.assertGreater(t_proc["damage"][a2], t_miss["damage"][a1])
        self.assertAlmostEqual(t_proc["damage"][a2] - t_miss["damage"][a1], expected_bonus, delta=2)

    async def test_left_spin_clash_bonuses_drain_and_recoil_reduction(self):
        s, a, e = await session("Bloody Longinus", "Void Longinus", level=1)
        s.hp[a] = s.max_hp_per_player[a] = 5000
        s.hp[e] = s.max_hp_per_player[e] = 5000
        self.assertEqual(s.status.get_buff_bonus(a, "stamina"), 15)
        self.assertEqual(s.status.get_buff_bonus(a, "defense"), 8)
        self.assertEqual(s.ability.recoil_flat_mod.get(a), -7)

        sta_enemy_before = s.stamina_manager.stamina[e]
        t = await round_trace(s, a, e, "attack", "charge", roll=0.99)
        raw_recoil = math.ceil(t["damage"][a] * 0.40)
        expected_recoil = max(0, raw_recoil - 7)
        self.assertEqual(t["hp_before"][a] - t["hp_after"][a], expected_recoil)
        self.assertLess(s.stamina_manager.stamina[e], sta_enemy_before)
        self.assertIn("drained 0.3 stamina", t["logs"])

    async def test_dodged_or_shielded_hit_deals_zero_recoil(self):
        s, a, e = await session("Bloody Longinus", "Victory Valkyrie", level=1)
        s.hp[a] = s.max_hp_per_player[a] = 5000
        s.avatar_bonuses[e] = replace(NULL_BONUSES, dodge_chance=1)
        t = await round_trace(s, a, e, "attack", "charge", roll=0.0)
        self.assertEqual(t["damage"][a], 0)
        self.assertEqual(t["hp_after"][a], t["hp_before"][a])

    async def test_dragon_scream_consumes_four_stacks_with_special_crit_and_pierce(self):
        # Against Defense-type Blast Jinnius to verify 50% Special DEF ignore + 200% + crit
        s_unstacked, a1, e1 = await session("Bloody Longinus", "Blast Jinnius", level=1)
        s_stacked, a2, e2 = await session("Bloody Longinus", "Blast Jinnius", level=1)
        for s, a, e in ((s_unstacked, a1, e1), (s_stacked, a2, e2)):
            s.hp[a] = s.max_hp_per_player[a] = 5000
            s.hp[e] = s.max_hp_per_player[e] = 5000

        t_base = await round_trace(s_unstacked, a1, e1, "special", "charge", roll=0.99)
        # Blast Jinnius has 14% Defense passive mitigation: ceil/floor(200 * 0.86) = 172
        self.assertEqual(t_base["damage"][a1], 172)

        for _ in range(4):
            await round_trace(s_stacked, a2, e2, "attack", "charge", roll=0.99)
        self.assertEqual(s_stacked.ability.counters[a2, "squall"], 4)

        t_awakened = await round_trace(s_stacked, a2, e2, "special", "charge", roll=0.99)
        # (200 base + 56 squall ATK buff) = 256 -> +100% = 512 -> x2.0 Special crit = 1024
        # -> 50% of Blast Jinnius's 14% Defense mitigation = 7% -> floor(1024 * 0.93) = 952
        self.assertEqual(t_awakened["damage"][a2], 952)
        self.assertIn("AWAKENED DRAGON SCREAM", t_awakened["logs"])
        self.assertEqual(s_stacked.ability.counters[a2, "squall"], 0)
        self.assertEqual(s_stacked.ability.counters[a2, "squall_cycle"], 0)
        self.assertEqual(s_stacked.status.get_buff_bonus(a2, "attack"), 0)
        # Special crit must not leak onto the next Attack
        self.assertEqual(s_stacked.ability.guaranteed_crit_turns.get(a2, 0), 0)

    async def test_excalibur_ascendant_overdrive_does_not_crit_its_own_special(self):
        s, a, e = await session("Excalibur Ascendant", "Victory Valkyrie", level=1)
        s.hp[a] = s.max_hp_per_player[a] = 5000
        s.hp[e] = s.max_hp_per_player[e] = 5000
        t = await round_trace(s, a, e, "special", "charge", roll=0.99)
        # Excalibur Triple Strike is 3x50 = 150 true damage; Overdrive arms 4 Attack turns AFTER Special
        self.assertEqual(t["damage"][a], 150)
        self.assertEqual(s.ability.guaranteed_crit_turns.get(a, 0), 4)


if __name__ == "__main__":
    unittest.main()
