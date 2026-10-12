"""Silence must gate DSL dispatch, including calls outside offensive apply()."""
import copy
import unittest
from tools.test_october_bey_roster import build, P, E


class SilenceDispatch(unittest.TestCase):
    def fixture(self, trigger, ops, once=None):
        s, blade = build('Dranzer')
        blade = copy.deepcopy(blade)
        rule = {'when': trigger, 'do': ops}
        if once:
            rule['once'] = once
        blade['abilities'] = [{'name': 'Silence fixture', 'rules': [rule]}]
        blade['ability'] = blade['abilities'][0]
        s.blades[P] = blade
        s.ability._compiled.clear()
        s.damage_status_round = s.round
        s.status.silenced_turns[P] = 3
        return s, blade

    def test_turn_start_and_round_start_are_suppressed(self):
        for trigger in ('turn_start', 'turn_end', 'on_round_start'):
            with self.subTest(trigger=trigger):
                s, blade = self.fixture(trigger, [{'op': 'true_damage', 'value': 20}])
                s.ability._fire(trigger, P, E, blade, 'attack', 'win', 0, 0, [])
                self.assertEqual(s.hp[E], 200)

    def test_charge_dispatch_is_suppressed(self):
        s, blade = self.fixture('on_charge', [{'op': 'true_damage', 'value': 20}])
        s.attack_manager.resolve_pair(P, E, 'charge', 'attack', blade, s.blades[E],
                                     blade['stats'], s.blades[E]['stats'])
        self.assertEqual(s.hp[E], 200)

    def test_defender_reactive_rules_are_suppressed(self):
        for trigger in ('on_defend', 'on_take_damage'):
            with self.subTest(trigger=trigger):
                s, blade = self.fixture(trigger, [{'op': 'reflect_pct', 'value': 50}])
                damage, counter = s.ability._fire_defensive(
                    P, E, blade, 'attack', 'win', 100, 0, [])
                self.assertEqual((damage, counter), (100, 0))

    def test_silence_does_not_consume_once_per_battle_rule(self):
        s, blade = self.fixture('turn_start', [{'op': 'true_damage', 'value': 20}], 'battle')
        s.ability._fire('turn_start', P, E, blade, '', '', 0, 0, [])
        self.assertEqual(s.hp[E], 200)
        self.assertFalse(s.ability.once_fired)
        s.status.silenced_turns[P] = 0
        s.ability._fire('turn_start', P, E, blade, '', '', 0, 0, [])
        self.assertEqual(s.hp[E], 180)
        s.ability._fire('turn_start', P, E, blade, '', '', 0, 0, [])
        self.assertEqual(s.hp[E], 180)

    def test_setup_can_initialize_state_while_silenced(self):
        s, blade = self.fixture('setup', [{'op': 'shield', 'value': 30}])
        s.ability.setup(P, blade)
        self.assertEqual(s.status.get_shield(P), 30)


if __name__ == '__main__':
    unittest.main()
