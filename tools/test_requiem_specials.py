"""Requiem forms through live rounds, including expiry and Special bypass."""
import copy
import unittest
from unittest.mock import patch
from dataclasses import replace
from tools.test_october_bey_roster import build, P, E
from cogs.battle.purification import effective_stats
from cogs.battle import requiem
from cogs.avatar import NULL_BONUSES


class RequiemSpecials(unittest.IsolatedAsyncioTestCase):
    def session(self, mode='Requiem Whip', reverse=False, level=100):
        s, _ = build('Spriggan Requiem')
        key, enemy = (E, P) if reverse else (P, E)
        if reverse:
            s.blades[P], s.blades[E] = s.blades[E], s.blades[P]
        blade = s.blades[key]
        blade['special_move'] = copy.deepcopy(blade['special_moves'][mode])
        blade['chosen_special'] = mode
        for k in (P, E):
            s.hp[k] = s.max_hp_per_player[k] = 10000
            s.stamina_manager.stamina[k] = 15
            s.stamina_manager.max_stamina[k] = 30
            s.stability_manager.stability[k] = s.stability_manager.max[k] = 1000
            s.bey_levels[k] = level
        s.type_gimmicks.rng = lambda: .99
        s.avatars_enabled = False
        return s, key, enemy

    async def turn(self, s, key, enemy, mine, theirs):
        s.moves = {key: mine, enemy: theirs}
        for k in (key, enemy):
            if s.moves[k] == 'special':
                s.stamina_manager.gauge[k] = 150
        with patch('random.random', return_value=.99):
            await s._BattleSession__resolve_round_body()

    async def test_whip_damage_conversion_three_rounds_and_restoration(self):
        for reverse in (False, True):
            for level in (1, 100):
                with self.subTest(reverse=reverse, level=level):
                    s, key, enemy = self.session(reverse=reverse, level=level)
                    raw = copy.deepcopy(s.blades[key]['stats'])
                    baseline = effective_stats(s, key)
                    await self.turn(s, key, enemy, 'special', 'charge')
                    self.assertGreater(s.attack_manager.committed_damage[key], 0)
                    for _ in range(3):
                        stats = effective_stats(s, key)
                        # Baseline can gain a low-HP buff, but this fixture
                        # stays healthy. Defensive stats remain available.
                        self.assertAlmostEqual(stats['attack'], sum(baseline[k]
                            for k in ('attack', 'defense', 'stamina')))
                        self.assertEqual(stats['defense'], baseline['defense'])
                        self.assertEqual(stats['stamina'], baseline['stamina'])
                        await self.turn(s, key, enemy, 'attack', 'charge')
                    self.assertFalse(requiem.active(s, key))
                    self.assertEqual(effective_stats(s, key)['attack'], baseline['attack'])
                    self.assertEqual(s.blades[key]['stats'], raw)

    async def test_zone_damages_then_protects_exactly_two_rounds_in_both_seats(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                s, key, enemy = self.session('Requiem Zone', reverse=reverse)
                before_cast = s.hp[key]
                await self.turn(s, key, enemy, 'special', 'attack')
                self.assertGreater(s.attack_manager.committed_damage[key], 0)
                self.assertLess(s.hp[key], before_cast)  # Starts after cast.
                for _ in range(2):
                    before = s.hp[key]
                    await self.turn(s, key, enemy, 'charge', 'attack')
                    self.assertEqual(s.attack_manager.committed_damage[enemy], 0)
                    self.assertGreaterEqual(s.hp[key], before)  # Passive heal allowed.
                await self.turn(s, key, enemy, 'charge', 'attack')
                self.assertGreater(s.attack_manager.committed_damage[enemy], 0)
                self.assertFalse(requiem.active(s, key))

    async def test_enemy_special_still_damages_zone(self):
        s, key, enemy = self.session('Requiem Zone')
        await self.turn(s, key, enemy, 'special', 'charge')
        self.assertTrue(requiem.active(s, key, 'zone'))
        s.blades[enemy]['special_move'] = {'hits': 3, 'damage_per_hit': 100}
        await self.turn(s, key, enemy, 'charge', 'special')
        self.assertGreater(s.attack_manager.committed_damage[enemy], 0)

    async def test_stamina_conversion_scale_cap_and_no_hp_healing(self):
        s, key, _ = self.session('Requiem Zone')
        requiem.cast(s, key, s.blades[key], [])
        s.round += 1
        s.stamina_manager.stamina[key] = 5
        before = s.hp[key]
        self.assertEqual(requiem.absorb(s, key, 'attack', 100, []), 0)
        self.assertEqual(s.stamina_manager.stamina[key], 10)
        requiem.absorb(s, key, 'attack', 100000, [])
        self.assertEqual(s.stamina_manager.stamina[key], 30)
        self.assertEqual(s.hp[key], before)
        self.assertEqual(requiem.absorb(s, key, 'special', 100, []), 100)
        self.assertEqual(requiem.absorb(s, key, 'burn', 100, []), 100)

    async def test_whip_recast_refreshes_without_stacking_or_mutating_registry(self):
        s, key, _ = self.session()
        requiem.cast(s, key, s.blades[key], [])
        s.round += 1
        first = effective_stats(s, key)
        # A new cast takes effect next round without retaining another copy.
        requiem.cast(s, key, s.blades[key], [])
        s.round += 1
        self.assertEqual(effective_stats(s, key), first)

    async def test_avatar_stats_are_converted_once_and_sessions_are_isolated(self):
        s, key, _ = self.session()
        s.avatar_bonuses[key] = replace(NULL_BONUSES, attack_percent=.15)
        before = effective_stats(s, key)
        requiem.cast(s, key, s.blades[key], [])
        s.round += 1
        for _ in range(3):
            self.assertAlmostEqual(effective_stats(s, key)['attack'],
                                   sum(before[k] for k in ('attack', 'defense', 'stamina')))
        other, other_key, _ = self.session()
        self.assertFalse(requiem.active(other, other_key))


if __name__ == '__main__':
    unittest.main()
