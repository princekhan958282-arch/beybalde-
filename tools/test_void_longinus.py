"""Longinus Strike stability balance in real battle rule and Special pipelines."""
import copy
import unittest
from unittest.mock import patch

from tools.test_october_bey_roster import build, P, E
from utils.database import get_beyblade


class VoidLonginusStability(unittest.TestCase):
    def battle(self, level, kind, move):
        s, blade = build('Void Longinus')
        s.bey_levels[P] = level
        s.blades[E]['type'] = kind
        s.last_moves = {P: 'attack', E: move}
        s.type_gimmicks.rng = lambda: .99
        return s, blade

    def test_normal_and_each_special_hit_cover_level_type_and_move_matrix(self):
        for level in (1, 99, 100):
            base, bonus, hp_bonus = (15, 10, 30) if level == 100 else (8, 5, 20)
            for kind in ('Attack', 'Defense', 'Stamina', 'Balance'):
                for move in ('attack', 'defense', 'charge', 'stamina', 'special', None):
                    expected = base + (bonus if kind == 'Defense' or move in ('attack', 'defense', 'charge') else 0)
                    for special in (False, True):
                        with self.subTest(level=level, kind=kind, move=move, special=special):
                            s, blade = self.battle(level, kind, move)
                            for hit in range(2 if special else 1):
                                before = s.stability_manager.stability[E]
                                if special:
                                    damage, _ = s.ability.process_hit_proc(P, blade, E, 90)
                                    self.assertEqual(damage, 90 + hp_bonus)
                                else:
                                    damage, _ = s.ability._fire('on_attack_hit', P, E, blade, 'attack', 'win', 100, 0, [])
                                    self.assertEqual(damage, 100 + hp_bonus)
                                self.assertEqual(before - s.stability_manager.stability[E], expected)
                            self.assertEqual(s.stability_manager.stability[P], 100)

    def test_real_void_crash_applies_two_hits_against_noctilune_and_defense(self):
        for level in (99, 100):
            for opponent in ('Noctilune', 'Draciel G'):
                for move in ('attack', 'defense', 'charge', 'stamina', 'special'):
                    with self.subTest(level=level, opponent=opponent, move=move):
                        s, blade = self.battle(level, 'Balance', move)
                        s.blades[E] = copy.deepcopy(get_beyblade(opponent))
                        s.moves = {P: 'special', E: move}
                        s.last_moves[P] = 'special'
                        s.hp[E] = s.max_hp_per_player[E] = 10000
                        base, bonus = (15, 10) if level == 100 else (8, 5)
                        expected = base + (bonus if s.blades[E]['type'] == 'Defense' or move in ('attack', 'defense', 'charge') else 0)
                        with patch('cogs.abilities.ability_engine.random.random', return_value=.99):
                            damage, _ = s.attack_manager._resolve_special(P, E, 'special', blade, s.blades[E], [])
                        self.assertGreater(damage, 0)
                        self.assertEqual(100 - s.stability_manager.stability[E], 2 * expected)


if __name__ == '__main__':
    unittest.main()
