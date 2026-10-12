"""Repeated defenses must not turn full pierce into a DEF=1 damage spike."""
import copy
import unittest
from unittest.mock import patch
from tools.sim_battle_ticks import BattleSession, FakeChannel, FakePlayer, get_beyblade
from cogs.battle.purification import effective_stats


class PierceSpike(unittest.IsolatedAsyncioTestCase):
    async def test_three_defenses_then_attack_in_both_player_orders(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                names = ['Wind Knight', 'Spriggan Requiem']
                if reverse:
                    names.reverse()
                blades = [copy.deepcopy(get_beyblade(name)) for name in names]
                for blade in blades:
                    blade['stats'].update(attack=700, defense=700, hp=10000, stamina=400)
                s = BattleSession(None, FakeChannel(), FakePlayer(1001, 'A'),
                    FakePlayer(1002, 'B'), *blades, payout=False, avatars_enabled=False)
                for key in ('1001', '1002'):
                    s._prefetch[key] = {'profile': {'wins': 0, 'losses': 0, 'coins': 0,
                                                   'xp': 0, 'inventory': []}}
                    s.hp[key] = s.max_hp_per_player[key] = 10000
                    s.bey_levels[key] = 100
                    s.stability_manager.stability[key] = s.stability_manager.max[key] = 1000
                    s.stamina_manager.stamina[key] = 100
                knight = '1002' if reverse else '1001'
                opponent = '1001' if reverse else '1002'
                s.type_gimmicks.rng = lambda: .99
                for _ in range(3):
                    s.moves = {knight: 'defense', opponent: 'attack'}
                    with patch('random.random', return_value=.99):
                        await s._BattleSession__resolve_round_body()
                self.assertEqual(s.status.get_duration('ignore_defense_turns', knight), 1)
                self.assertEqual(s.status.get_buff_bonus(knight, 'attack'), 35)
                s.moves = {knight: 'attack', opponent: 'charge'}
                with patch('random.random', return_value=.99):
                    await s._BattleSession__resolve_round_body()
                damage = s.attack_manager.committed_damage[knight]
                self.assertGreater(damage, 0)
                self.assertLess(damage, 500)
                self.assertGreater(s.hp[opponent], 9000)
                self.assertEqual(s.status.get_duration('ignore_defense_turns', knight), 0)

    async def test_full_pierce_preserves_favorable_ratio_and_removes_defensive_advantage(self):
        from tools.test_october_bey_roster import build, P, E
        for attack, defense, expected in ((700, 1400, 700), (700, 200, 200),
                                          (100, 10000, 100)):
            with self.subTest(attack=attack, defense=defense):
                s, blade = build('Wind Knight')
                s.status.set_duration('ignore_defense_turns', P, 1)
                original = {'defense': defense}
                adjusted, _ = s.defense_manager.preprocess_defender_stats(
                    P, E, blade, s.blades[E], original, {'attack': attack})
                self.assertEqual(adjusted['defense'], expected)
                self.assertEqual(original['defense'], defense)


if __name__ == '__main__':
    unittest.main()
