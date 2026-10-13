"""One applied pierce message and one actual HP-loss total per move."""
import unittest
from unittest.mock import patch
from tools.test_october_bey_roster import build, P, E
from cogs.battle.damage_rules import calc_damage


class BattleLogConsistency(unittest.TestCase):
    def battle(self, opponent_move='stamina', *, pierce=1, refresh=False):
        s, _ = build('Dranzer')
        for key, name in ((P, 'Aero Cinnamoroll'), (E, 'Victory Valkyrie')):
            s.blades[key]['name'] = name
            s.blades[key]['abilities'] = []
            s.blades[key]['ability'] = None
        if refresh:
            s.blades[P]['abilities'] = [{'name':'Cloud Soar Smash','rules':[
                {'when':'on_attack_win','do':[{'op':'ignore_defense','turns':1}]}]}]
        s.moves = {P:'attack', E:opponent_move}
        s.status.set_duration('ignore_defense_turns',P,pierce)
        s.type_gimmicks.rng = lambda: .99
        s.hp[E] = 1563
        s.max_hp_per_player[E] = 10000
        stats = {'level':100,'attack':1000,'defense':200,'stamina':100}
        with patch('cogs.abilities.ability_engine.random.random',return_value=.99):
            result = s.attack_manager.resolve_pair(P,E,'attack',opponent_move,
                s.blades[P],s.blades[E],stats,stats)
        return s, result

    def test_full_pierce_announced_once_against_each_opponent_move(self):
        for move in ('stamina','charge','attack','defense'):
            with self.subTest(move=move):
                s, (damage,counter,matchup,logs) = self.battle(move)
                self.assertEqual(sum('Defense Pierce' in line for line in logs),1)
                self.assertEqual(s.status.get_duration('ignore_defense_turns',P),0)
                self.assertGreater(damage,0)
                if move == 'defense':
                    self.assertEqual(counter,0)
                    self.assertIn('Counter NULLIFIED', '\n'.join(logs))

    def test_refresh_keeps_effect_without_repeating_applied_announcement(self):
        s, (_,_,_,logs) = self.battle(refresh=True)
        self.assertEqual(sum('Defense Pierce' in line for line in logs),1)
        self.assertEqual(s.status.get_duration('ignore_defense_turns',P),1)
        self.assertFalse(any('Cloud Soar Smash' in line for line in logs))
        s, (_,_,_,logs) = self.battle(pierce=0,refresh=True)
        self.assertEqual(sum('Defense Pierce granted' in line for line in logs),1)
        self.assertEqual(s.status.get_duration('ignore_defense_turns',P),1)

    def test_lethal_hit_summary_has_actual_hp_loss_and_no_projected_counter_total(self):
        s, (damage,counter,matchup,logs) = self.battle(refresh=True)
        # Keep this a lethal hit without depending on the old DEF=1 pierce bug.
        s.hp[E] = 153
        self.assertGreater(damage,153)
        hp_logs = s.attack_manager.apply_pair_results(P,E,s.blades[P],s.blades[E],
            'attack','stamina',damage,counter,matchup,0,0,'lose')
        self.assertEqual(s.attack_manager.committed_damage[P],153)
        self.assertEqual(s.hp[E],0)
        summary = '\n'.join(s.attack_manager.build_round_summary(*s.players,P,E,
            'attack','stamina',s.attack_manager.committed_damage[P],0,
            matchup,'lose',logs+hp_logs))
        self.assertIn('Dealt **153** damage',summary)
        self.assertEqual(summary.count('Defense Pierce'),1)
        self.assertNotIn('bonus damage applied',summary)
        self.assertNotIn('total:',summary)
        self.assertNotIn(str(damage),summary)

    def test_dodge_logs_no_phantom_damage_or_recovery_counter(self):
        s, (damage,counter,matchup,logs) = self.battle()
        with patch('cogs.battle.avatar_combat.absorb_incoming',return_value=(0,0,['Avatar dodge'])):
            hp_logs = s.attack_manager.apply_pair_results(P,E,s.blades[P],s.blades[E],
                'attack','stamina',damage,counter,matchup,0,0,'lose')
        self.assertEqual(s.attack_manager.committed_damage[P],0)
        self.assertEqual(s.hp[E],1563)
        self.assertFalse(any('bonus damage applied' in line for line in hp_logs))

    def test_full_pierce_bypasses_high_defense_denominator(self):
        blade = {'name': 'Excalibur Ascendant', 'stats': {'attack': 160}}
        attacker = {'level': 100, 'attack': 160, 'defense': 100, 'stamina': 100}
        normal_defender = {'level': 100, 'attack': 100, 'defense': 999, 'stamina': 100}
        pierced_defender = dict(normal_defender)
        pierced_defender['_ignore_defense'] = True

        normal, _, _, _ = calc_damage(
            'attack', attacker, normal_defender, blade, 'stamina')
        pierced, _, _, _ = calc_damage(
            'attack', attacker, pierced_defender, blade, 'stamina')
        no_def = dict(pierced_defender, defense=1)
        expected, _, _, _ = calc_damage(
            'attack', attacker, no_def, blade, 'stamina')

        self.assertGreater(pierced, normal)
        self.assertEqual(pierced, expected)

    def test_damage_math_still_applies_attack_advantage_once(self):
        s, _ = self.battle(pierce=0)
        stats = {'level':100,'attack':1000,'defense':200}
        damage,_,_,_ = calc_damage('attack',stats,stats,s.blades[P],'stamina')
        from cogs.battle.type_gimmicks import base_damage
        from cogs.battle.button_profile import move_power
        from cogs.battle.constants import ATTACK_VS_STAMINA_MULT
        raw = base_damage(100,move_power(s.blades[P],'attack'),1000,200)
        self.assertAlmostEqual(damage,raw*ATTACK_VS_STAMINA_MULT)


if __name__ == '__main__': unittest.main()
