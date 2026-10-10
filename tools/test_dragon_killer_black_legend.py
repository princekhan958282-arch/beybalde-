"""Mode selection, stock equipment and actual Dragon Killer/Black Legend effects."""
import unittest
from unittest.mock import patch

from tools.test_october_bey_roster import build, P, E
from utils.character_registry import REGISTRY, load_beys, load_parts
from utils.bey_components import assemble, reconcile
from utils.spin_mode import resolve, modes
from cogs.economy.profile import SpinModeView


class StockData(unittest.TestCase):
    def test_id_aliases_and_rarity(self):
        killer = REGISTRY.find_bey('S,Dragon Killer')
        self.assertEqual(killer['id'], 'BB149')
        self.assertEqual(killer['rarity'], 'Ultimate')
        self.assertEqual(killer['type'], 'Attack')
        self.assertEqual(REGISTRY.find_bey('s dragon killer'), killer)
        legend = REGISTRY.find_bey('Black Legend')
        self.assertEqual(legend['id'], 'BB150')
        self.assertEqual(legend['rarity'], 'Legendary')
        self.assertEqual(len(load_beys()), 164)

    def test_mode_picker_and_equipment_preserve_both_forms(self):
        b = REGISTRY.find_bey('Black Legend')
        self.assertEqual(modes(b), ['Attack', 'Counter'])
        for mode, kind, atk, defense in [('Attack', 'Attack', 155, 80),
                                         ('Counter', 'Defense', 80, 155)]:
            with self.subTest(mode=mode):
                p = {'inventory': [b['name']], 'active_beyblade': b['name'],
                     'spin_mode': {b['name']: mode}}
                reconcile(p)
                mounted = resolve(p, b)
                assembled, delta = assemble(p, mounted)
                self.assertEqual(assembled['type'], kind)
                self.assertEqual(assembled['stats']['attack'], atk)
                self.assertEqual(assembled['stats']['defense'], defense)
                self.assertEqual(assembled['stats'], mounted['stats'])
                self.assertEqual(assembled['abilities'], b['spin_modes'][mode]['abilities'])
                self.assertEqual(assembled['special_move'], b['spin_modes'][mode]['special_move'])
                self.assertFalse(any(delta.values()))
        self.assertEqual(resolve({}, b)['active_spin_mode'], 'Attack')

    def test_bundled_parts_not_in_shop(self):
        shop = {p['id'] for p in load_parts(shop_only=True)}
        for name in ('S,Dragon Killer', 'Black Legend'):
            b = REGISTRY.find_bey(name)
            self.assertEqual(b['ability'], b['abilities'][0])
            assembled, delta = assemble({'inventory': [name], 'active_beyblade': name}, b)
            self.assertEqual(assembled['stats'], b['stats'])
            self.assertFalse(any(delta.values()))
            for slot, ref in b['default_parts'].items():
                part = REGISTRY.part(ref)
                self.assertEqual(part['bey_id'], b['id'])
                self.assertNotIn(ref, shop)
                self.assertFalse(part['tradable'])


class CombatEffects(unittest.TestCase):
    def test_attack_bonus_is_real_and_mode_specific(self):
        for name, mode, scale in [('S,Dragon Killer', None, .12),
                                  ('Black Legend', 'Attack', .10),
                                  ('Black Legend', 'Counter', 0)]:
            s, b = build(name, mode)
            logs = []
            actual, _ = s.ability._fire('on_attack_hit', P, E, b, 'attack', 'win', 100, 0, logs)
            self.assertEqual(actual, 100 + round(s.battle_stats[P]['attack'] * scale))

    def test_counter_only_reflects_normal_attack_while_defending(self):
        for mode, own, incoming, expected in [('Counter', 'defense', 'attack', 20),
                                              ('Counter', 'attack', 'attack', 0),
                                              ('Counter', 'defense', 'special', 0),
                                              ('Attack', 'defense', 'attack', 0)]:
            with self.subTest(mode=mode, own=own, incoming=incoming):
                s, b = build('Black Legend', mode)
                s.moves = {P: own, E: incoming}
                dealt, reflected = s.ability._fire_defensive(P, E, b, incoming, 'win', 100, 0, [])
                self.assertEqual(dealt, 100)
                self.assertEqual(reflected, expected)

    def test_counter_renewal_heals_on_defense_win(self):
        s, b = build('Black Legend', 'Counter')
        before = s.hp[P]
        s.ability._fire('on_defense_win', P, E, b, 'defense', 'win', 0, 0, [])
        self.assertEqual(s.hp[P] - before, 60)

    def test_black_rally_once_and_attack_mode_only(self):
        s, b = build('Black Legend', 'Attack')
        s.ability._fire('on_low_hp', P, E, b, 'attack', 'win', 0, 0, [])
        self.assertEqual(s.status.get_buff_bonus(P, 'attack'), 23)
        s.ability._fire('on_low_hp', P, E, b, 'attack', 'win', 0, 0, [])
        self.assertEqual(s.status.get_buff_bonus(P, 'attack'), 23)
        s, b = build('Black Legend', 'Counter')
        s.ability._fire('on_low_hp', P, E, b, 'defense', 'win', 0, 0, [])
        self.assertEqual(s.status.get_buff_bonus(P, 'attack'), 0)

    def test_dragon_slayer_bonus_fires_once_across_three_special_hits(self):
        s, b = build('S,Dragon Killer')
        s.moves = {P: 'special', E: 'charge'}
        s.hp[E] = s.max_hp_per_player[E] = 10000
        with patch('random.random', return_value=.99):
            _, logs = s.attack_manager._resolve_special(P, E, 'special', b, s.blades[E], [])
        self.assertEqual(sum("enemy's current HP" in line for line in logs), 1)
        self.assertTrue(any('+800 damage' in line for line in logs), logs)


class RealRounds(unittest.IsolatedAsyncioTestCase):
    async def test_info_buttons_show_counter_and_attack(self):
        view = SpinModeView(None, REGISTRY.find_bey('Black Legend'), None, active='Attack')
        self.assertEqual([item.label for item in view.children], ['Attack Mode', 'Counter Mode'])
        self.assertTrue(view.children[0].disabled)
        self.assertFalse(view.children[1].disabled)
        view.stop()

    async def test_real_rounds_complete_for_both_beys_and_modes(self):
        for name, mode, own in [('S,Dragon Killer', None, 'attack'),
                                ('Black Legend', 'Attack', 'attack'),
                                ('Black Legend', 'Counter', 'defense')]:
            with self.subTest(name=name, mode=mode):
                s, _ = build(name, mode)
                for key in (P, E):
                    s.hp[key] = s.max_hp_per_player[key] = 10000
                    s.stability_manager.max[key] = s.stability_manager.stability[key] = 5000
                for move in (own, 'special'):
                    s.moves = {P: move, E: 'attack' if own == 'defense' else 'stamina'}
                    for key in (P, E):
                        s.stamina_manager.stamina[key] = s.stamina_manager.cap_for(key)
                        s.stamina_manager.gauge[key] = 150
                    with patch('random.random', return_value=.99):
                        await s._BattleSession__resolve_round_body()
                self.assertLess(s.hp[E], 10000)


if __name__ == '__main__':
    unittest.main()
