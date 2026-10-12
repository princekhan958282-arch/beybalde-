"""Level-100 stock HP targets and custom allocation compatibility."""
import copy
import unittest
from utils.character_registry import load_beys, REGISTRY
from utils import bey_levels as BL
from utils.loadout import bey_level_and_stats
from utils.hp_system import max_hp_for_blade
from utils.spin_mode import resolve
from cogs.battle.type_gimmicks import passive_stat_multiplier


class HpBalanceTests(unittest.TestCase):
    def assert_curve(self, blade):
        for iv in (0, BL.IV_MAX):
            before = 0
            for level in range(1, BL.MAX_LEVEL + 1):
                stats = BL.stats_at(blade, level, {'hp': iv})
                hp = int(max_hp_for_blade(dict(blade, stats=stats))
                         * passive_stat_multiplier(blade['type'], 'hp'))
                self.assertGreaterEqual(hp, before)
                before = hp
            self.assertGreaterEqual(hp, 2000)
            self.assertLessEqual(hp, 3000)

    def test_all_official_beys_and_mounted_forms(self):
        for blade in load_beys().values():
            with self.subTest(bey=blade['name']):
                self.assert_curve(blade)
                self.assertEqual(blade['stats']['hp'], blade['main_frame']['hp']
                                 + sum(REGISTRY.part(p)['stats']['hp']
                                       for p in blade['default_parts'].values()))
            for mode in blade.get('spin_modes', {}):
                with self.subTest(bey=blade['name'], mode=mode):
                    self.assert_curve(resolve({'spin_mode': {blade['name']: mode}}, blade))

    def test_every_legal_custom_hp_allocation(self):
        for bey_type in ('Attack', 'Defense', 'Stamina', 'Balance'):
            for hp in range(20, 336):
                blade = {'id': 'CUSTOM', 'name': 'Custom', 'type': bey_type,
                         'stats': {'hp': hp, 'attack': 20, 'defense': 20, 'stamina': 20}}
                original = copy.deepcopy(blade)
                for iv in (0, 10):
                    stats = BL.stats_at(blade, 100, {'hp': iv})
                    pool = int(stats['hp'] * 15 * passive_stat_multiplier(bey_type, 'hp'))
                    self.assertTrue(2000 <= pool <= 3000, (bey_type, hp, iv, pool))
                self.assertEqual(blade, original)
                self.assertEqual(bey_level_and_stats({}, blade)[1]['hp'],
                                 BL.stats_at(blade, 1, {})['hp'])


if __name__ == '__main__':
    unittest.main()
