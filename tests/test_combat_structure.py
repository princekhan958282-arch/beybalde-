"""Architecture regressions: shared arithmetic, turn boundary, authored controls."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import test_combat_rework as fixtures
from cogs.battle.type_gimmicks import TypeGimmickEngine
from cogs.battle import combat_rules
from cogs.battle.boss import boss_ai


class SharedRules(unittest.TestCase):
    def test_damage_and_healing_actual_caps(self):
        self.assertEqual(combat_rules.damage_hp(30, 100.9), (0, 30))
        self.assertEqual(combat_rules.damage_hp(100, 1.5, already_final=True), (98.5, 1.5))
        self.assertEqual(combat_rules.recover_hp(90, 100, 20), (100, 10))
        self.assertEqual(combat_rules.recover_hp(0, 100, 20), (0, 0))
        self.assertEqual(combat_rules.spend_resource(1, 20), 0)
        self.assertEqual(combat_rules.recover_resource(9, 10, 6), 10)
        self.assertEqual(combat_rules.recover_resource(20, 10, 6, preserve_reserve=True), 20)

    def test_cross_type_force_and_explicit_action_override(self):
        engine = TypeGimmickEngine({'p': 'Defense', 'e': 'Attack'}, rng=lambda: .99)
        engine.forced.add(('p', 'critical_strike'))
        engine.begin_round({'p': 'special', 'e': 'charge'})
        self.assertTrue(engine.states['p'].critical_strike_active)
        self.assertEqual(engine.critical('p', 'special', 100, []), 100)
        engine.register_op({'op': 'gimmick_force', 'gimmick': 'critical_strike',
                            'allow_actions': ['special'], 'turns': 1}, 'p', 'e', 'Override', [])
        self.assertEqual(engine.critical('p', 'special', 100, []), 200)
        self.assertEqual(engine.critical('p', 'burn', 100, []), 100)

    def test_timed_controls_refresh_and_suppression_wins(self):
        engine = TypeGimmickEngine({'p': 'Attack', 'e': 'Defense'}, rng=lambda: .99)
        engine.start_round()
        op = {'op': 'gimmick_chance', 'gimmick': 'critical_strike', 'value': 95, 'turns': 2}
        for _ in range(5):
            engine.register_op(op, 'p', 'e', 'Boost', [])
        self.assertEqual(len(engine.effects), 1)
        engine.begin_round({'p': 'attack', 'e': 'charge'}, started=True)
        self.assertEqual(engine.critical('p', 'attack', 100, []), 200)
        engine.register_op({'op': 'gimmick_suppress', 'gimmick': 'critical_strike', 'turns': 1}, 'p', 'e', 'Suppress', [])
        engine.register_op({'op': 'gimmick_force', 'gimmick': 'critical_strike'}, 'p', 'e', 'Force', [])
        self.assertEqual(engine.critical('p', 'attack', 100, []), 100)
        engine.end_round()
        engine.begin_round({'p': 'attack', 'e': 'charge'})
        self.assertTrue(engine.critical_applies('p', 'attack'))
        engine.end_round()
        engine.begin_round({'p': 'attack', 'e': 'charge'})
        self.assertFalse(engine.critical_applies('p', 'attack'))

    def test_morph_and_overdrive_bonuses_refresh(self):
        engine = TypeGimmickEngine({'p': 'Unknown', 'e': 'Defense'}, rng=lambda: .99)
        engine.start_round()
        for _ in range(3):
            for op in ({'op': 'gimmick_force', 'gimmick': 'adaptive_morph'},
                       {'op': 'gimmick_bonus', 'gimmick': 'adaptive_morph', 'bonus': 'stats', 'value': 5},
                       {'op': 'gimmick_force', 'gimmick': 'overdrive'},
                       {'op': 'gimmick_bonus', 'gimmick': 'overdrive', 'bonus': 'heal', 'value': 10}):
                engine.register_op(op, 'p', 'e', 'Bonus', [])
        self.assertAlmostEqual(engine.stat_multiplier('p'), 1.1)
        heal, stab, sp = engine.recovery('p', 200)
        self.assertAlmostEqual(heal, 220)
        self.assertEqual((stab, sp), (30, 6))

    def test_unknown_gimmicks_fail_loudly(self):
        engine = TypeGimmickEngine({'p': 'Attack'})
        with self.assertRaises(ValueError):
            engine.register_op({'op': 'gimmick_force', 'gimmick': 'typo'}, 'p', 'p', 'Bad', [])


class LiveBoundary(unittest.IsolatedAsyncioTestCase):
    def session(self, a='Unknown', b='Unknown'):
        return fixtures.RealRounds().session(a, b)

    def add_rules(self, s, key, ops, *, when='on_round_start', conditions=None, once=None):
        rule = {'when': when, 'do': ops}
        if conditions:
            rule['if'] = conditions
        if once:
            rule['once'] = once
        s.blades[key]['abilities'] = [{'name': 'Structural Control', 'rules': [rule]}]
        s.ability._compiled.clear()

    async def test_expired_defense_buff_same_in_both_orders(self):
        losses = []
        for attacker, defender in ((fixtures.P, fixtures.E), (fixtures.E, fixtures.P)):
            s = self.session()
            s.status.add_buff(defender, 'defense', 100, 1)
            before = s.hp[defender]
            await fixtures.turn(s, 'attack', 'attack')
            losses.append(before - s.hp[defender])
            self.assertEqual(s.status.get_buff_bonus(defender, 'defense'), 0)
        self.assertEqual(losses, [86, 86])

    async def test_one_turn_true_damage_same_in_both_orders(self):
        for attacker, defender in ((fixtures.P, fixtures.E), (fixtures.E, fixtures.P)):
            s = self.session()
            s.status.set_duration('true_damage_turns', attacker, 1)
            s.status.add_shield(defender, 500)
            before = s.hp[defender]
            await fixtures.turn(s, 'attack', 'attack')
            self.assertEqual(s.hp[defender], before)
            self.assertEqual(s.status.get_duration('true_damage_turns', attacker), 0)

    async def test_effect_granted_this_round_not_ticked_for_second_player(self):
        # A control grants a timed buff to its enemy during its own phase.
        # Both grants must retain their full duration regardless of seat.
        for caster, target in ((fixtures.P, fixtures.E), (fixtures.E, fixtures.P)):
            s = self.session()
            original = s.attack_manager.resolve_pair
            def wrapped(mkey, okey, *args):
                if mkey == caster:
                    s.status.add_buff(target, 'attack', 40, 2)
                return original(mkey, okey, *args)
            with patch.object(s.attack_manager, 'resolve_pair', side_effect=wrapped):
                await fixtures.turn(s, 'charge', 'charge')
            self.assertEqual(s.status.active_buffs[target][0]['rounds_left'], 2)

    async def test_authored_chance_and_enemy_suppression_before_roll(self):
        s = self.session('Attack', 'Unknown')
        self.add_rules(s, fixtures.P, [{'op': 'gimmick_chance', 'gimmick': 'critical_strike', 'value': 95}])
        before = s.hp[fixtures.E]
        await fixtures.turn(s, 'attack', 'charge')
        self.assertEqual(before - s.hp[fixtures.E], 283)
        s = self.session('Attack', 'Unknown')
        s.type_gimmicks.forced.add((fixtures.P, 'critical_strike'))
        self.add_rules(s, fixtures.E, [{'op': 'gimmick_suppress', 'gimmick': 'critical_strike', 'target': 'enemy'}])
        before = s.hp[fixtures.E]
        await fixtures.turn(s, 'attack', 'charge')
        self.assertEqual(before - s.hp[fixtures.E], 141)

    async def test_authored_cross_type_special_critical(self):
        normal, forced = self.session(), self.session()
        for s in (normal, forced):
            s.blades[fixtures.P]['special_move'] = {'hits': 1, 'damage_per_hit': 200}
            s.stamina_manager.gauge[fixtures.P] = 150
        self.add_rules(forced, fixtures.P, [{'op': 'gimmick_force', 'gimmick': 'critical_strike', 'allow_actions': ['special']}])
        before_n, before_f = normal.hp[fixtures.E], forced.hp[fixtures.E]
        await fixtures.turn(normal, 'special', 'charge')
        await fixtures.turn(forced, 'special', 'charge')
        self.assertEqual(before_f - forced.hp[fixtures.E], 2 * (before_n - normal.hp[fixtures.E]))

    async def test_authored_conditions_once_and_morph_stats(self):
        s = self.session()
        self.add_rules(s, fixtures.P, [{'op': 'gimmick_force', 'gimmick': 'adaptive_morph'}],
                       conditions=[{'cond': 'my_move_is', 'value': 'attack'}], once='battle')
        await fixtures.turn(s, 'charge', 'charge')
        self.assertFalse(s.type_gimmicks.states[fixtures.P].adaptive_morph_active)
        before = s.hp[fixtures.E]
        await fixtures.turn(s, 'attack', 'charge')
        self.assertEqual(before - s.hp[fixtures.E], 135)
        self.assertEqual(s.type_gimmicks.states[fixtures.P].adaptive_morph_rounds, 2)
        await fixtures.turn(s, 'attack', 'charge')
        self.assertEqual(s.type_gimmicks.states[fixtures.P].adaptive_morph_rounds, 1)

    async def test_story_projection_carries_controls_and_morph_without_double_stats(self):
        from cogs.story.story_ai import project
        from cogs.battle.purification import effective_stats
        s = self.session('Balance', 'Unknown')
        s.type_gimmicks.states[fixtures.P].adaptive_morph_rounds = 2
        s.type_gimmicks.start_round()
        s.type_gimmicks.register_op({'op': 'gimmick_bonus', 'gimmick': 'adaptive_morph',
                                    'bonus': 'stats', 'value': 5, 'turns': 2},
                                   fixtures.P, fixtures.E, 'Morph boost', [])
        s._sync_morph_hp(fixtures.P)
        snapshot = project(s, fixtures.P, fixtures.E)
        self.assertAlmostEqual(snapshot.eff_attack, effective_stats(s, fixtures.P)['attack'])
        self.assertEqual(snapshot.max_hp, s.max_hp_per_player[fixtures.P])
        self.assertEqual(snapshot.morph_rounds, 2)
        self.assertTrue(snapshot.gimmick_controls)
        snapshot.clone().gimmick_controls.clear()
        self.assertTrue(snapshot.gimmick_controls)


class BossBridge(unittest.TestCase):
    def fighter(self, kind='Unknown'):
        return fixtures.BossTests().fighter(kind)

    def test_boss_authored_force_controls_and_search_clone(self):
        a, b = self.fighter(), self.fighter('Defense')
        a.gimmick_rules = [{'when': 'on_round_start', 'once': 'battle',
                            'if': [{'cond': 'my_move_is', 'value': 'attack'}],
                            'do': [{'op': 'gimmick_force', 'gimmick': 'critical_strike', 'turns': 2}]}]
        b.gimmick_rules = [{'when': 'on_round_start', 'do': [{'op': 'gimmick_force', 'gimmick': 'kinetic_counter'}]}]
        with patch('random.random', return_value=.99):
            result = boss_ai.resolve(a, b, 'attack', 'defense')
        self.assertEqual(1000 - a.hp, 1000 - b.hp)
        self.assertGreater(result['kinetic_return_a'], 0)
        self.assertTrue(a.gimmick_once)
        self.assertTrue(a.gimmick_controls)
        clone = a.clone()
        clone.gimmick_controls.clear()
        self.assertTrue(a.gimmick_controls)
        with patch('random.random', side_effect=AssertionError('search rolled RNG')):
            boss_ai.resolve(a.clone(), b.clone(), 'attack', 'charge', simulate=True)

    def test_boss_kit_mitigation_precedes_exact_return_and_true_bypass(self):
        a, b = self.fighter('Attack'), self.fighter('Defense')
        b.incoming_reduction, b.incoming_flat_reduction = .5, 10
        with patch('random.random', return_value=0):
            report = boss_ai.resolve(a, b, 'attack', 'defense')
        self.assertEqual(1000 - b.hp, report['kinetic_return_a'])
        self.assertEqual(1000 - a.hp, report['kinetic_return_a'])
        a, b = self.fighter(), self.fighter('Defense')
        a.special_damage, a.special_true_damage = 200, True
        b.incoming_reduction, b.incoming_flat_reduction = .5, 100
        with patch('random.random', return_value=0):
            report = boss_ai.resolve(a, b, 'special', 'defense')
        self.assertEqual(report['dmg_to_b'], 200)

    def test_boss_offensive_bonuses_are_included_in_exact_return(self):
        a, b = self.fighter('Attack'), self.fighter('Defense')
        a.outgoing_amp, a.outgoing_flat = .5, 10
        with patch('random.random', return_value=0):
            report = boss_ai.resolve(a, b, 'attack', 'defense')
        self.assertEqual(report['kinetic_return_a'], 53)
        self.assertEqual(1000 - b.hp, 53)
        self.assertEqual(1000 - a.hp, 53)

    def test_boss_shield_effect_precedes_type_mitigation(self):
        from cogs.battle.boss.boss_abilities import BossState
        class ShieldState(BossState):
            def absorb(self, damage):
                self.seen = damage
                return max(0, damage - 10), False
        a, b = self.fighter('Attack'), self.fighter('Defense')
        b.state = ShieldState()
        raw = boss_ai._raw_damage(a, dst=b)
        soak = boss_ai.DEFENSE_SOAK * (1 + b.eff_defense / 200)
        expected = raw * max(.15, 1 - soak) * 2
        with patch('random.random', return_value=0):
            report = boss_ai.resolve(a, b, 'attack', 'defense')
        self.assertAlmostEqual(b.state.seen, expected)
        self.assertEqual(report['kinetic_return_a'], combat_rules.hp_damage((expected - 10) * .86))

    def test_boss_morph_bonus_and_expiry(self):
        a, b = self.fighter(), self.fighter()
        a.gimmick_rules = [{'when': 'on_round_start', 'once': 'battle', 'do': [
            {'op': 'gimmick_force', 'gimmick': 'adaptive_morph'},
            {'op': 'gimmick_bonus', 'gimmick': 'adaptive_morph', 'bonus': 'stats', 'value': 5}]}]
        with patch('random.random', return_value=.99):
            report = boss_ai.resolve(a, b, 'attack', 'charge')
        self.assertEqual(report['dmg_to_b'], 94)
        self.assertAlmostEqual(a.eff_attack, 315)  # bonus expires; Morph remains
        self.assertEqual(a.max_hp, 1050)

    def test_boss_hit_trigger_forces_another_types_critical(self):
        a, b = self.fighter('Unknown'), self.fighter('Unknown')
        a.gimmick_rules = [{'when': 'on_attack_hit', 'do': [
            {'op': 'gimmick_force', 'gimmick': 'critical_strike'}]}]
        with patch('random.random', return_value=.99):
            result = boss_ai.resolve(a, b, 'attack', 'charge')
        self.assertEqual(result['dmg_to_b'], 172)

    def test_fractional_actual_kinetic_return_is_not_rounded_again(self):
        a, b = self.fighter('Attack'), self.fighter('Defense')
        b.hp = 1.5
        with patch('random.random', return_value=0):
            result = boss_ai.resolve(a, b, 'attack', 'defense')
        self.assertEqual(result['kinetic_return_a'], 1.5)
        self.assertEqual(a.hp, 998.5)

    def test_live_boss_kit_does_not_refund_mitigation_or_add_second_reflect(self):
        from cogs.battle.boss import boss_battle as bb
        from cogs.battle.boss.blade_abilities import BladeKit
        player = SimpleNamespace(id=1, display_name='Player', mention='<@1>')
        actor = self.fighter('Defense')
        kit = BladeKit({})
        kit.reduction, kit.flat_reduction, kit.reflect = .5, 10, 999
        fight = bb.BossFight(player, 'argus', party=[player],
                             _fighters={1: actor}, _kits={1: kit}, _blades={1: {}})
        fight.boss.bey_type = 'Attack'
        before_boss, before_player = fight.boss.hp, actor.hp
        with patch('random.random', return_value=0), \
             patch.object(boss_ai, 'choose_move', return_value=('attack', {})):
            report = fight.step('defense')
        self.assertGreater(report['kinetic_return_a'], 0)
        self.assertEqual(before_player - actor.hp, report['kinetic_return_a'])
        self.assertEqual(before_boss - fight.boss.hp, report['kinetic_return_a'])


if __name__ == '__main__':
    unittest.main()
