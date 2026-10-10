"""Boundary and pipeline regressions for all thirteen authored Burst Beys."""
import unittest
import math
from tools.test_nine_burst_beys import session, start, end, outgoing, NAMES, P, E
from tools import test_special_avatar_pipeline as pipeline
from utils.character_registry import REGISTRY
from cogs.battle.purification import effective_stats
from cogs.battle.special_gate import ready
from cogs.battle.damage_rules import resolve_special_hits

ALL_NAMES = NAMES + ('Black Especially', 'Grand Dragon', 'Heaven Pegasus', 'Hyperion Burn Cho Xceed')


class Boundaries(unittest.TestCase):
    def test_orbit_protection_does_not_leak_before_next_round_start(self):
        s, _, r, d = session('Circle Bahamut')
        d['orbit'] = 3
        start(s)
        self.assertEqual(r.stability_delta(P, -10), 0)
        end(s, 'charge')
        self.assertEqual(r.stability_delta(P, -10), 0)
        s.round += 1
        self.assertEqual(r.stability_delta(P, -10), -10)

    def test_momentum_cannot_create_damage_on_a_missed_attack(self):
        s, _, r, d = session('Ace Dragon')
        end(s, 'charge')
        self.assertEqual(outgoing(s, result='lose', amount=0), 0)
        self.assertGreater(outgoing(s), 100)

    def test_disabled_second_ability_stops_registered_effects(self):
        for name in ALL_NAMES:
            with self.subTest(name=name):
                s, b, r, d = session(name)
                second = [op['effect'] for rule in b['abilities'][1]['rules']
                          for op in rule.get('do', []) if op['op'] == 'burst_mechanic']
                first = [op['effect'] for rule in b['abilities'][0]['rules']
                         for op in rule.get('do', []) if op['op'] == 'burst_mechanic']
                s.ability.ability_2_disabled[P] = True
                for effect in second: self.assertFalse(r.has(P, effect))
                for effect in first: self.assertTrue(r.has(P, effect))

    def test_stack_stats_are_visible_to_special_and_ai_readers(self):
        for name, counter, scale, stats in (
                ('Silver Valkyrie', 'speed', .20, ('attack',)),
                ('Greatest Raphael', 'halo', .12, ('attack', 'defense'))):
            with self.subTest(name=name):
                s, _, r, d = session(name)
                baseline = effective_stats(s, P)
                d[counter] = 4
                boosted = effective_stats(s, P)
                for stat in stats:
                    self.assertAlmostEqual(boosted[stat], baseline[stat] + r.stat(P, stat) * scale)
                s.status.silenced_turns[P] = 1
                self.assertEqual(effective_stats(s, P), baseline)

    def test_guard_arms_reduce_hit_before_shield_and_consume_once(self):
        s, b, r, d = session('Bushin Ashura')
        d['arms'] = 3
        s.status.add_shield(P, 200)
        filt = s.ability.damage_filter
        result, _ = filt.defensive(E, P, s.blades[E], b, 'attack', 100, True)
        self.assertEqual(result, 0)
        self.assertEqual(s.status.get_shield(P), 121)
        self.assertEqual(d.get('arms', 0), 0)
        result, _ = filt.defensive(E, P, s.blades[E], b, 'attack', 100, True)
        self.assertEqual(result, 0)
        self.assertEqual(s.status.get_shield(P), 21)

    def test_avatar_rider_reduces_shield_damage_and_records_absorption(self):
        s, b, r, d = session('Circle Bahamut')
        d['orbit'] = 3
        s.status.add_shield(P, 200)
        raw = effective_stats(s, P)['defense']
        s.original_generation.rider(E, P, raw, 'Test rider', [])
        self.assertEqual(s.status.get_shield(P), 112)
        s, b, r, d = session('Arc Bahamut')
        r.shield(P, 10)
        raw = effective_stats(s, P)['defense']
        s.original_generation.rider(E, P, raw, 'Test rider', [])
        self.assertEqual(d['vault'], 20)
        self.assertEqual(d['barrier'], 0)
        self.assertTrue(any(x.get('source') == 'arc_barrier' for x in s.status.active_buffs[P]))


class LiveSpecials(unittest.IsolatedAsyncioTestCase):
    async def test_all_silenced_specials_still_start_cooldown(self):
        helper = pipeline.SpecialPipelineTests()
        for name in ALL_NAMES:
            with self.subTest(name=name):
                s = await helper.build(REGISTRY.find_bey(name))
                s.status.silenced_turns['101'] = 2
                self.assertTrue(s.status.is_silenced('101'))
                helper.resolve(s)
                self.assertEqual(s.ability.cooldowns.get(('101', 'burst_finisher')), 4)
                self.assertFalse(ready(s, '101', s.blades['101'], 150))

    async def test_stacked_specials_use_live_stats_and_consume_stacks(self):
        helper = pipeline.SpecialPipelineTests()
        for name, counter, rider in (('Silver Valkyrie', 'speed', 48),
                                     ('Greatest Raphael', 'halo', 0)):
            with self.subTest(name=name):
                s = await helper.build(REGISTRY.find_bey(name))
                state = s.ability.burst.data('101')
                state[counter] = 4
                expected = sum(resolve_special_hits(s.blades['101'],
                    effective_stats=effective_stats(s, '101'))) + rider
                self.assertEqual(helper.resolve(s)[0], expected)
                self.assertEqual(state.get(counter, 0), 0)


class AuthoredSpecialEffects(unittest.IsolatedAsyncioTestCase):
    async def test_every_special_damage_and_rider_with_second_ability_enabled_or_disabled(self):
        """Use the actual multi-hit resolver; a Special survives ability-2 disable."""
        helper = pipeline.SpecialPipelineTests()
        for disabled in (False, True):
            for name in ALL_NAMES:
                with self.subTest(name=name, ability_2_disabled=disabled):
                    s = await helper.build(REGISTRY.find_bey(name))
                    key, enemy = '101', '102'
                    d = s.ability.burst.data(key)
                    r = s.ability.burst
                    s.ability.ability_2_disabled[key] = disabled
                    s.stability_manager.stability[key] = s.stability_manager.max[key] - 100
                    before_stability = s.stability_manager.stability[key]
                    s.stamina_manager.stamina[key] = 1
                    s.hp[key] = s.max_hp_per_player[key] - 400
                    before_hp = s.hp[key]
                    d.update(speed=4, red=2, black=2, halo=4, spheres=3, pursuit=3, feathers=3, heat=4)
                    s.ability.burst.data(enemy)['eclipse_until'] = s.round + 2
                    r.buff(enemy, 'attack', -8, 2, f'heavy_landing:{key}')
                    landing = next(b for b in s.status.active_buffs[enemy]
                                   if b.get('source') == f'heavy_landing:{key}')
                    if name == 'Grand Dragon': s.status.add_shield(enemy, 20)
                    formula = sum(resolve_special_hits(s.blades[key], effective_stats=effective_stats(s, key)))
                    bonus = {'Silver Valkyrie': 48, 'Judgement Joker': 30,
                             'Black Especially': 35, 'Hyperion Burn Cho Xceed': 72}.get(name, 0)
                    self.assertEqual(helper.resolve(s)[0], formula + bonus)
                    self.assertEqual(s.ability.cooldowns[(key, 'burst_finisher')], 4)
                    if name == 'Silver Valkyrie': self.assertEqual(d.get('speed', 0), 0)
                    elif name == 'Arc Bahamut':
                        self.assertEqual(s.status.get_shield(key), math.ceil(s.max_hp_per_player[key] * .20))
                    elif name == 'Circle Bahamut':
                        self.assertEqual(d.get('orbit'), 2)
                        self.assertEqual(s.stability_manager.stability[key], before_stability + 12)
                    elif name == 'Judgement Joker':
                        self.assertEqual((d.get('red', 0), d.get('black', 0)), (0, 0))
                        self.assertEqual(s.status.get_shield(key), math.ceil(s.max_hp_per_player[key] * .10))
                    elif name == 'Jail Jormungand':
                        self.assertEqual(landing['rounds_left'], 4)
                        self.assertEqual(s.stability_manager.stability[key], before_stability + 8)
                    elif name == 'Greatest Raphael':
                        self.assertEqual(s.hp[key], before_hp + math.ceil(s.max_hp_per_player[key] * .12))
                        self.assertEqual(d.get('halo', 0), 0)
                    elif name == 'Orb Engaard':
                        self.assertEqual(d['sphere_reduction'], 24)
                        self.assertEqual(d['sphere_until'], s.round + 1)
                        self.assertEqual(d.get('spheres', 0), 0)
                    elif name == 'Ace Dragon': self.assertEqual(d.get('pursuit', 0), 0)
                    elif name == 'Black Especially':
                        self.assertNotIn('eclipse_until', r.data(enemy))
                    elif name == 'Grand Dragon': self.assertEqual(s.status.get_shield(enemy), 0)
                    elif name == 'Heaven Pegasus':
                        self.assertEqual(s.stamina_manager.stamina[key], 4)
                        self.assertEqual(s.stability_manager.stability[key], before_stability + 12)
                        self.assertEqual(d.get('feathers', 0), 0)
                    elif name == 'Hyperion Burn Cho Xceed': self.assertEqual(d.get('heat', 0), 0)

    async def test_all_specials_keep_avatar_amplification_with_loaded_state(self):
        helper = pipeline.SpecialPipelineTests()
        for name in ALL_NAMES:
            with self.subTest(name=name):
                plain = await helper.build(REGISTRY.find_bey(name))
                boosted = await helper.build(REGISTRY.find_bey(name), 'avatar_x003')
                for s in (plain, boosted):
                    s.ability.burst.data('101').update(speed=4, halo=4, red=2,
                        black=2, spheres=3, pursuit=3, feathers=3, heat=4)
                self.assertGreater(helper.resolve(boosted)[0], helper.resolve(plain)[0])

    async def test_raphael_reversal_triggers_from_burn_and_avatar_rider(self):
        from unittest.mock import patch
        for source in ('burn', 'avatar'):
            with self.subTest(source=source):
                s, b, r, d = session('Greatest Raphael')
                s.hp[P] = 410
                s.status.add_buff(P, 'attack', -10, 4)
                if source == 'burn':
                    s.status.apply_burn(P, {'burn_damage_per_turn': 20, 'burn_duration': 2})
                    s.moves = {P: 'charge', E: 'charge'}
                    with patch('random.random', return_value=.99):
                        await s._BattleSession__resolve_round_body()
                    self.assertEqual(s.status.burn_duration[P], 1)
                else:
                    s.original_generation.rider(E, P, effective_stats(s, P)['defense'] * .3, 'Test rider', [])
                self.assertTrue(d.get('reversed'))
                self.assertFalse(any(b.get('amount', 0) < 0 for b in s.status.active_buffs[P]))


class NewBeyMatchups(unittest.IsolatedAsyncioTestCase):
    async def test_all_169_ordered_matchups_complete_live_actions_and_specials(self):
        import copy
        from unittest.mock import patch
        H = pipeline.H
        for left in ALL_NAMES:
            for right in ALL_NAMES:
                with self.subTest(left=left, right=right):
                    H.seed_profile(101)
                    H.seed_profile(102)
                    s = await H.BattleSession.create(None, H.FakeChannel(),
                        H.FakePlayer(101, 'Left'), H.FakePlayer(102, 'Right'),
                        copy.deepcopy(REGISTRY.find_bey(left)),
                        copy.deepcopy(REGISTRY.find_bey(right)), ranked=False, payout=False)
                    s.type_gimmicks.rng = lambda: .99
                    for key in ('101', '102'):
                        s.hp[key] = s.max_hp_per_player[key] = 100000
                        s.stability_manager.max[key] = s.stability_manager.stability[key] = 50000
                    for own, enemy in zip(('attack', 'defense', 'stamina', 'charge', 'special'),
                                          ('stamina', 'attack', 'defense', 'charge', 'special')):
                        s.moves = {'101': own, '102': enemy}
                        for key in ('101', '102'):
                            s.stamina_manager.stamina[key] = s.stamina_manager.cap_for(key)
                            s.stamina_manager.gauge[key] = 150
                        with patch('random.random', return_value=.99):
                            await s._BattleSession__resolve_round_body()
                        for key in ('101', '102'):
                            self.assertTrue(0 <= s.hp[key] <= s.max_hp_per_player[key])
                            self.assertTrue(0 <= s.stamina_manager.stamina[key] <= s.stamina_manager.cap_for(key))
                            self.assertGreaterEqual(s.status.get_shield(key), 0)
                    for key in ('101', '102'):
                        self.assertFalse(ready(s, key, s.blades[key], 150))
