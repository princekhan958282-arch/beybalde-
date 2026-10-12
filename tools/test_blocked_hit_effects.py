"""Screenshot regressions: blocked critical rewards, defense logs and recoil."""
import unittest
from dataclasses import replace
from unittest.mock import patch
from cogs.avatar import NULL_BONUSES
from cogs.battle.type_gimmicks import TypeGimmickEngine
from tools.test_october_bey_roster import build, P, E


class BlockedHits(unittest.TestCase):
    def critical(self, *, kinetic=False, shield=0, reduction=0):
        s, blade = build('Spriggan Requiem')
        blade['abilities'] = []
        blade['ability'] = None
        s.blades[E]['abilities'] = ([{'name': 'Full guard', 'rules': [
            {'when': 'on_take_damage', 'do': [
                {'op': 'reduce_damage_flat', 'value': reduction}]}]}] if reduction else [])
        s.ability._compiled.clear()
        s.moves = {P: 'attack', E: 'defense' if kinetic else 'charge'}
        s.last_moves = dict(s.moves)
        s.damage_status_round = s.round
        s.avatar_bonuses[P] = replace(NULL_BONUSES, crit_percent=1, gauge_on_crit=55)
        s.type_gimmicks = TypeGimmickEngine({P: 'Balance', E: 'Defense' if kinetic else 'Balance'}, rng=lambda: .99)
        if kinetic:
            s.type_gimmicks.forced.add((E, 'kinetic_counter'))
        s.type_gimmicks.begin_round(s.moves)
        if shield:
            s.status.add_shield(E, shield)
        with patch('cogs.battle.avatar_combat.random.random', return_value=0):
            damage, counter, lines = s.ability.apply(P, E, blade, s.blades[E],
                                                   'attack', 'lose' if kinetic else 'win', 100, 0)
        return s, damage, counter, lines

    def commit(self, s, damage, counter=0):
        return s.attack_manager.apply_pair_results(P, E, s.blades[P], s.blades[E],
            'attack', s.moves[E], damage, counter, 'lose', 0, 0, 'win')

    def test_blocked_critical_does_not_reward_gauge(self):
        for cfg in ({'kinetic': True}, {'shield': 1000}, {'reduction': 1000}):
            with self.subTest(cfg=cfg):
                s, damage, counter, lines = self.critical(**cfg)
                self.assertEqual(damage, 0)
                logs = self.commit(s, damage, counter)
                self.assertEqual(s.stamina_manager.gauge.get(P, 0), 0)
                self.assertFalse(any('critical hit charges' in line for line in lines + logs))

    def test_final_avatar_dodge_does_not_reward_gauge(self):
        s, damage, counter, lines = self.critical()
        with patch('cogs.battle.avatar_combat.absorb_incoming', return_value=(0, 0, [])):
            logs = self.commit(s, damage, counter)
        self.assertEqual(s.attack_manager.committed_damage[P], 0)
        self.assertEqual(s.stamina_manager.gauge.get(P, 0), 0)
        self.assertFalse(any('critical hit charges' in line for line in lines + logs))

    def test_landed_critical_rewards_only_at_commit_and_only_once(self):
        s, damage, counter, lines = self.critical()
        self.assertGreater(damage, 0)
        self.assertEqual(s.stamina_manager.gauge.get(P, 0), 0)
        logs = self.commit(s, damage, counter)
        self.assertEqual(s.stamina_manager.gauge[P], 55)
        self.assertEqual(sum('critical hit charges' in line for line in logs), 1)
        self.commit(s, 0)
        self.assertEqual(s.stamina_manager.gauge[P], 55)

    def test_wind_knight_recoil_does_not_arm_permanent_reflect(self):
        s, blade = build('Wind Knight')
        before = s.hp[E]
        s.ability._fire('on_defense_win', P, E, blade, 'defense', 'win', 0, 0, [])
        logs = s.chain_handler.resolve(P, blade, E, 0)
        self.assertEqual(before - s.hp[E], 20)
        self.assertEqual(s.ability.post_rebirth_reflect.get(P, 0), 0)
        self.assertFalse(any('every hit' in line for line in logs))
        self.assertEqual(s.status.get_buff_bonus(P, 'attack'), 0)
        for _ in range(2):
            s.ability._fire('on_defense_win', P, E, blade, 'defense', 'win', 0, 0, [])
            s.chain_handler.resolve(P, blade, E, 0)
        self.assertEqual(s.status.get_buff_bonus(P, 'attack'), 35)
        self.assertEqual(s.status.get_duration('ignore_defense_turns', P), 1)
        self.assertEqual(s.ability.post_rebirth_reflect.get(P, 0), 0)

    def test_passive_requiem_heal_still_works_on_blocked_turn(self):
        s, blade = build('Spriggan Requiem')
        s.ability.setup(P, blade)
        s.ability._fire('passive', P, E, blade, 'attack', 'lose', 0, 0, [])
        before = s.hp[P]
        s.ability.apply_dot_tick_extras(P, blade)
        self.assertEqual(s.hp[P] - before, 8)

    def test_defense_summary_reports_guard_instead_of_blocked_attack(self):
        s, _ = build('Wind Knight')
        lines = s.attack_manager.build_round_summary(*s.players, P, E,
            'defense', 'attack', 0, 0, 'win', 'lose', [])
        text = '\n'.join(lines)
        own = text.split('Test opponent used')[0]
        self.assertIn('guard', own.lower())
        self.assertNotIn('But it was blocked!', own)


if __name__ == '__main__':
    unittest.main()
