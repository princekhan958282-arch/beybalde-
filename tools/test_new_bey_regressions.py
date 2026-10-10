"""Boundary and pipeline regressions for all thirteen authored Burst Beys."""
import unittest
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
