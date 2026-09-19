#!/usr/bin/env python3
"""Regression tests through the live ability, status, damage and cost managers."""
import copy
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.sim_cosmic_phoenix import FakeSession, DUMMY, am_for
from cogs.abilities.extended_effects import OPS
from cogs.battle.boss.blade_abilities import kit_for
from cogs.battle import special_gate


def session(ops=None):
    blade = copy.deepcopy(DUMMY)
    blade['name'] = 'Effect Test'
    if ops:
        # Exercise the same JSON roundtrip used by saved ability definitions.
        blade['abilities'] = json.loads(json.dumps([{'name': 'Test', 'rules': [
            {'when': 'setup', 'do': ops}]}]))
    s = FakeSession(blade, DUMMY)
    s.players = [SimpleNamespace(display_name='P'), SimpleNamespace(display_name='E')]
    return s


def run(s, op, key='p', other='e'):
    logs = []
    s.ability._run_ops({'do': [op]}, 'Test', key, other, 'attack', 0, 0, logs, 'win')
    return logs


def hit(s, damage=100, key='p', other='e', move='attack'):
    dealt, taken, logs = s.ability.apply(key, other, s.blades[key], s.blades[other], move, 'win', damage, 0)
    logs += am_for(s).apply_pair_results(key, other, s.blades[key], s.blades[other],
                                       move, 'charge', dealt, 0, 'win', 0, 0, 'mirror')
    return logs


class EffectsTests(unittest.TestCase):
    def test_storage_actual_damage_and_release_nonrecursive(self):
        s = session([{'op': 'damage_storage', 'name': 'bank', 'pct': 50, 'cap': 40, 'turns': 5}])
        s.status.add_shield('p', 40)
        hit(s, 100, 'e', 'p')
        bank = s.ability.extended.entries('p', 'damage_storage')[0]
        self.assertEqual(bank['bank'], 30)
        self.assertEqual(s.hp['p'], 940)
        run(s, {'op': 'damage_storage', 'pct': 100, 'cap': 100}, 'e', 'p')
        before = s.hp['e']
        run(s, {'op': 'release_storage', 'name': 'bank'})
        self.assertEqual(s.hp['e'], before - 30)
        self.assertEqual(bank['bank'], 0)
        self.assertEqual(s.ability.extended.entries('e', 'damage_storage')[0]['bank'], 0)

    def test_storage_overkill_and_cap(self):
        s = session([{'op': 'damage_storage', 'pct': 100, 'cap': 40}])
        s.hp['p'] = 15
        hit(s, 200, 'e', 'p')
        self.assertEqual(s.ability.extended.entries('p', 'damage_storage')[0]['bank'], 15)

    def test_conversion_numeric_binary_and_next_action(self):
        s = session([{'op': 'debuff_conversion', 'prevent_pct': 50, 'gain_pct': 5, 'max_pct': 15, 'turns': 5}])
        s.status.add_buff('p', 'defense', -20, 4)
        s.status.silence('p', 3)
        s.status.apply_burn('p', {'burn_damage_per_turn': 20, 'burn_duration': 3})
        self.assertEqual(s.status.get_buff_bonus('p', 'defense'), -10)
        self.assertEqual(s.status.silenced_turns['p'], 2)
        self.assertEqual(s.status.burn_dmg['p'], 10)
        c = s.ability.extended.entries('p', 'debuff_conversion')[0]
        self.assertEqual(c['bank'], 15)
        s.status.silenced_turns['p'] = 0
        hit(s)
        self.assertEqual(s.hp['e'], 885)
        self.assertEqual(c['bank'], 0)

    def test_conversion_no_self_tradeoff_or_rounding_reward(self):
        s = session([{'op': 'debuff_conversion'}])
        s.status.add_buff('p', 'defense', -20, 3, hostile=False)
        s.status.silence('p', 1)
        self.assertEqual(s.ability.extended.entries('p', 'debuff_conversion')[0]['bank'], 0)

    def test_conversion_blocked_and_avatar_dodge_not_spent(self):
        s = session([{'op': 'debuff_conversion', 'turns': 5}])
        s.status.add_buff('p', 'defense', -20, 3)
        c = s.ability.extended.entries('p', 'debuff_conversion')[0]
        s.status.set_invulnerable('e', 3)
        hit(s)
        self.assertEqual(c['bank'], 5)
        s.status.invulnerable_turns['e'] = 0
        with patch('cogs.battle.avatar_combat.absorb_incoming', return_value=(0, 0, [])):
            hit(s)
        self.assertEqual(c['bank'], 5)
        hit(s)
        self.assertEqual(s.hp['e'], 895)
        self.assertEqual(c['bank'], 0)

    def test_conversion_multihit_once(self):
        s = session([{'op': 'debuff_conversion'}])
        s.status.add_buff('p', 'defense', -20, 3)
        total = 0
        for i in range(3):
            d, _, _ = s.ability.apply('p', 'e', s.blades['p'], s.blades['e'], 'special', 'win', 100, 0,
                                      is_first_hit=i == 0, is_last_hit=i == 2, cumulative_dmg=total)
            total += d
        am_for(s).apply_pair_results('p', 'e', s.blades['p'], s.blades['e'], 'special', 'charge', total, 0, 'win', 0, 0, 'mirror')
        self.assertEqual(s.hp['e'], 685)
        self.assertEqual(s.ability.extended.entries('p', 'debuff_conversion')[0]['bank'], 0)

    def test_suppression_expires_without_destroying_buffs(self):
        s = session()
        s.status.add_buff('e', 'attack', 40, 5)
        s.status.add_buff('e', 'attack', -10, 5)
        run(s, {'op': 'buff_suppression', 'target': 'enemy', 'pct': 50, 'turns': 1})
        self.assertEqual(s.status.get_buff_bonus('e', 'attack'), 10)
        s.ability.tick_extras()
        self.assertEqual(s.status.get_buff_bonus('e', 'attack'), 30)

    def test_resource_conversion_atomic_and_caps(self):
        s = session()
        op = {'op': 'resource_conversion', 'from': 'gauge', 'to': 'stability', 'amount': 10, 'rate': .5}
        s.stamina_manager.gauge['p'] = 9
        s.stability_manager.stability['p'] -= 10
        before = s.stability_manager.stability['p']
        run(s, op)
        self.assertEqual(s.stamina_manager.gauge['p'], 9)
        self.assertEqual(s.stability_manager.stability['p'], before)
        s.stamina_manager.gauge['p'] = 10
        run(s, op)
        self.assertEqual(s.stamina_manager.gauge['p'], 0)
        self.assertEqual(s.stability_manager.stability['p'], before + 5)
        s.stability_manager.stability['p'] = s.stability_manager.max['p']
        s.stamina_manager.gauge['p'] = 10
        run(s, op)
        self.assertEqual(s.stamina_manager.gauge['p'], 10)

    def test_echo_delay_actual_damage_and_no_feedback(self):
        s = session([{'op': 'delayed_echo', 'pct': 50, 'cap': 40, 'moves': ['attack'], 'delay': 1, 'turns': 5}])
        s.status.add_shield('e', 60)
        hit(s)
        self.assertEqual(s.hp['e'], 960)
        self.assertEqual(len(s.ability.extended.echoes), 1)
        s.ability.tick_extras()
        self.assertEqual(s.hp['e'], 960)
        s.ability.tick_extras()
        self.assertEqual(s.hp['e'], 940)
        self.assertFalse(s.ability.extended.echoes)
        s.ability.tick_extras()
        self.assertEqual(s.hp['e'], 940)

    def test_echo_shield_invulnerability_and_dead_owner(self):
        for protection in ('shield', 'invulnerability', 'dead'):
            with self.subTest(protection=protection):
                s = session([{'op': 'delayed_echo', 'cap': 40, 'moves': ['attack']}])
                hit(s)
                if protection == 'shield':
                    s.status.add_shield('e', 100)
                elif protection == 'invulnerability':
                    s.status.set_invulnerable('e', 4)
                else:
                    s.hp['p'] = 0
                s.ability.tick_extras(); s.ability.tick_extras()
                self.assertEqual(s.hp['e'], 900)

    def test_echo_revive(self):
        s = session([{'op': 'delayed_echo', 'pct': 100, 'cap': 100, 'moves': ['attack']}])
        hit(s)
        s.hp['e'] = 10
        s.ability.revive_pool['e'] = 100
        s.ability.tick_extras(); s.ability.tick_extras()
        self.assertEqual(s.hp['e'], 100)
        self.assertTrue(s.status.revival_used['e'])

    def test_marks_threshold_consumption_and_nested_guard(self):
        s = session()
        op = {'op': 'threshold_mark', 'name': 'judgment', 'target': 'enemy', 'threshold': 2,
              'do': [{'op': 'enemy_debuff', 'stat': 'defense', 'value': 10, 'turns': 3}]}
        run(s, op)
        self.assertEqual(s.status.get_buff_bonus('e', 'defense'), 0)
        run(s, op)
        self.assertEqual(s.status.get_buff_bonus('e', 'defense'), -10)
        self.assertFalse(s.ability.extended.marks)
        op['do'] = [{'op': 'threshold_mark'}]
        self.assertTrue(any('invalid' in l for l in run(s, op)))
        self.assertFalse(s.ability.extended.marks)

    def test_cost_quote_deduction_and_expiry(self):
        s = session()
        sm = s.stamina_manager
        base = sm.cost_for('p', 'attack')
        run(s, {'op': 'action_cost_modifier', 'resource': 'stamina', 'moves': ['attack'], 'pct': -25, 'turns': 1})
        expected = round(base * .75, 2)
        self.assertEqual(sm.cost_for('p', 'attack'), expected)
        before = sm.stamina['p']
        sm.deduct_cost('p', 'attack')
        self.assertAlmostEqual(sm.stamina['p'], before - expected)
        self.assertEqual(sm.cost_for('p', 'stamina'), 0)
        s.ability.tick_extras()
        self.assertEqual(sm.cost_for('p', 'attack'), base)

    def test_stability_cost_does_not_change_enemy_damage(self):
        s = session()
        sm = s.stability_manager
        run(s, {'op': 'action_cost_modifier', 'resource': 'stability', 'moves': ['attack'], 'pct': 50})
        before = sm.stability['p']
        sm.apply_attack_cost('p', hit=True)
        self.assertEqual(sm.stability['p'], before - 15)
        before = sm.stability['p']
        sm._apply('p', -10)
        self.assertEqual(sm.stability['p'], before - 10)

    def test_cooldown_real_special_gate(self):
        s = session()
        s.blades['p']['special_move'] = {'requirement': {'cooldown_name': 'example'}}
        s.ability.cooldowns[('p', 'example')] = 3
        run(s, {'op': 'cooldown_shift', 'name': 'example', 'rounds': -2})
        self.assertEqual(special_gate.cooldown_left(s, 'p', 'example'), 1)
        run(s, {'op': 'cooldown_shift', 'name': 'example', 'rounds': -99})
        self.assertEqual(special_gate.cooldown_left(s, 'p', 'example'), 0)
        run(s, {'op': 'cooldown_shift', 'name': 'unknown', 'rounds': 10})
        self.assertNotIn(('p', 'unknown'), s.ability.cooldowns)

    def test_shatter_overflow_and_no_shield(self):
        s = session([{'op': 'shield_shatter', 'pct': 100}])
        s.status.add_shield('e', 50)
        hit(s, 40)
        self.assertEqual(s.status.get_shield('e'), 0)
        self.assertEqual(s.hp['e'], 985)  # 25 normal damage spent breaking 50 shield
        hit(s, 40)
        self.assertEqual(s.hp['e'], 945)

    def test_transfer_resistance_and_remaining_duration(self):
        s = session()
        s.status.add_buff('p', 'defense', -20, 3)
        s.status.add_buff('p', 'attack', -40, 99, hostile=False)
        s.ability.debuff_immune['e'] = True
        run(s, {'op': 'effect_transfer'})
        self.assertEqual(s.status.get_buff_bonus('p', 'defense'), -20)
        s.ability.debuff_immune['e'] = False
        run(s, {'op': 'effect_transfer'})
        self.assertEqual(s.status.get_buff_bonus('p', 'defense'), 0)
        self.assertEqual(s.status.get_buff_bonus('e', 'defense'), -20)
        self.assertEqual(s.status.active_buffs['e'][0]['rounds_left'], 3)
        self.assertEqual(s.status.get_buff_bonus('p', 'attack'), -40)

    def test_transfer_full_prevention_keeps_source(self):
        s = session()
        s.status.add_buff('p', 'defense', -20, 3)
        run(s, {'op': 'debuff_conversion', 'prevent_pct': 100}, 'e', 'p')
        run(s, {'op': 'effect_transfer'})
        self.assertEqual(s.status.get_buff_bonus('p', 'defense'), -20)
        self.assertEqual(s.status.get_buff_bonus('e', 'defense'), 0)

    def test_invalid_configs_and_snapshot_isolation(self):
        s = session()
        invalid = [
            {'op': 'damage_storage', 'cap': float('nan')},
            {'op': 'delayed_echo', 'cap': -2},
            {'op': 'buff_suppression', 'turns': 0},
            {'op': 'action_cost_modifier', 'resource': 'hp'},
            {'op': 'resource_conversion', 'from': 'stamina', 'to': 'stamina', 'amount': 1, 'rate': 1},
            {'op': 'effect_transfer', 'count': -1},
            {'op': 'shield_shatter', 'pct': float('inf')},
        ]
        for op in invalid:
            self.assertTrue(any('invalid' in l for l in run(s, op)))
        self.assertFalse(s.ability.extended.windows)
        run(s, {'op': 'damage_storage', 'cap': 40})
        snap = s.status.snapshot('p')['extended_effects']
        snap['windows'][0]['bank'] = 999
        self.assertEqual(s.ability.extended.entries('p', 'damage_storage')[0]['bank'], 0)
        self.assertFalse(session().ability.extended.windows)

    def test_refresh_does_not_duplicate_storage(self):
        s = session([{'op': 'damage_storage', 'name': 'bank', 'pct': 50, 'cap': 40}])
        hit(s, 40, 'e', 'p')
        run(s, {'op': 'damage_storage', 'name': 'bank', 'cap': 40, 'turns': 4})
        entries = s.ability.extended.entries('p', 'damage_storage')
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['bank'], 20)
        self.assertEqual(entries[0]['turns'], 4)

    def test_named_windows_are_caster_scoped(self):
        s = session()
        run(s, {'op': 'buff_suppression', 'target': 'enemy', 'name': 'same', 'pct': 20})
        run(s, {'op': 'buff_suppression', 'name': 'same', 'pct': 40}, 'e', 'p')
        self.assertEqual(len(s.ability.extended.entries('e', 'buff_suppression')), 2)
        s.status.add_buff('e', 'attack', 100, 4)
        self.assertEqual(s.status.get_buff_bonus('e', 'attack'), 60)

    def test_marks_expire_and_immunity_blocks_hostile_cost(self):
        s = session()
        run(s, {'op': 'threshold_mark', 'target': 'enemy', 'turns': 1})
        s.ability.tick_extras()
        self.assertFalse(s.ability.extended.marks)
        s.ability.debuff_immune['e'] = True
        before = s.stamina_manager.cost_for('e', 'attack')
        run(s, {'op': 'action_cost_modifier', 'target': 'enemy', 'resource': 'stamina', 'pct': 50})
        self.assertEqual(s.stamina_manager.cost_for('e', 'attack'), before)

    def test_transfer_burn_and_silence(self):
        s = session()
        s.status.silence('p', 3)
        s.status.apply_burn('p', {'burn_damage_per_turn': 20, 'burn_duration': 4})
        run(s, {'op': 'effect_transfer', 'count': 2})
        self.assertEqual(s.status.silenced_turns['e'], 3)
        self.assertEqual(s.status.silenced_turns['p'], 0)
        self.assertEqual(s.status.burn_dmg['e'], 20)
        self.assertEqual(s.status.burn_duration['e'], 4)
        self.assertEqual(s.status.burn_stacks['p'], 0)

    def test_boss_partial_compatibility_diagnostic(self):
        for kind in OPS:
            blade = copy.deepcopy(DUMMY)
            blade['abilities'] = [{'name': 'Mixed', 'rules': [{'when': 'passive', 'do': [
                {'op': 'buff', 'stat': 'attack', 'amount': 10}, {'op': kind}]}]}]
            self.assertTrue(any(kind in line for line in kit_for(blade).unsupported()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
