"""New combat contract, including real round and boss-resolver integration."""
import asyncio
import copy
import unittest
from unittest.mock import patch, AsyncMock
from dataclasses import replace
from types import SimpleNamespace

from cogs.battle.type_gimmicks import TypeGimmickEngine, base_damage, hp_damage
from cogs.battle.damage_rules import calc_damage, resolve_special
from cogs.battle.stamina_manager import StaminaManager, max_stamina_for
from cogs.battle.purification import effective_stats
from cogs.battle.boss import boss_ai
from tools.sim_tactical_pvp import build, P, E

async def turn(s, a, b):
    before = len(s.channel.sent)
    s.moves = {P: a, E: b}
    with patch('random.random', return_value=.99):
        # Call the body directly so failures propagate with their traceback.
        await s._BattleSession__resolve_round_body()
    return s.channel.sent[before:]


class FormulaTests(unittest.TestCase):
    def test_formula_equal_high_low_and_zero_defense(self):
        for attack, defense in ((300, 300), (400, 50), (50, 400), (300, 0), (300, .01)):
            with self.subTest(attack=attack, defense=defense):
                expected = ((42 * 100 * attack / max(1, defense)) / 50) + 2
                self.assertAlmostEqual(base_damage(100, 100, attack, defense), expected)
        self.assertEqual(base_damage(100, 100, 300, 300), 86)
        self.assertEqual(hp_damage(172.9), 172)

    def test_calc_uses_formula_without_old_shield_gate(self):
        result = calc_damage('attack', {'attack': 300, 'level': 100}, {'defense': 300}, {}, 'attack')
        self.assertEqual(result, (86, 0, 'mirror', False))

    def test_stat_conversion(self):
        for stat in (100, 200, 400, 0, 1200):
            blades = {'p': {'stats': {'stamina': stat}}}
            sm = StaminaManager(blades)
            self.assertEqual(sm.stamina['p'], min(stat / 20, max_stamina_for(stat)))
            self.assertEqual(sm.cap_for('p'), max_stamina_for(stat))

    def test_type_cost_composes_with_abilities(self):
        blade = {'stats': {'stamina': 200}, 'button_profile': {'special': {'stamina_cost': 5}}}
        normal = StaminaManager({'p': blade})
        blade = dict(blade, type='Stamina')
        stamina = StaminaManager({'p': blade})
        self.assertEqual(stamina.cost_for('p', 'special'), 4)
        stamina.drain_reduction['p'] = .25
        normal.drain_reduction['p'] = .25
        self.assertAlmostEqual(stamina.cost_for('p', 'special'), normal.cost_for('p', 'special') * .8)

    def test_gimmick_action_eligibility(self):
        for t, gimmick, valid in (('Attack', 'critical_strike', 'attack'), ('Defense', 'kinetic_counter', 'defense'), ('Stamina', 'overdrive', 'stamina')):
            for action in ('attack', 'defense', 'stamina', 'charge', 'special', 'counter', 'burn'):
                engine = TypeGimmickEngine({'p': t}, rng=lambda: 0)
                engine.begin_round({'p': action})
                self.assertEqual(getattr(engine.states['p'], gimmick + '_active'), action == valid)

    def test_five_percent_boundary_and_suppression(self):
        for roll, expected in ((.049999, True), (.05, False), (.99, False)):
            engine = TypeGimmickEngine({'p': 'Attack'}, rng=lambda: roll)
            engine.begin_round({'p': 'attack'})
            self.assertEqual(engine.states['p'].critical_strike_active, expected)
        engine.forced.add(('p', 'critical_strike'))
        engine.suppressed.add(('p', 'critical_strike'))
        engine.begin_round({'p': 'attack'})
        self.assertFalse(engine.states['p'].critical_strike_active)

    def test_kinetic_and_true_damage_contract(self):
        engine = TypeGimmickEngine({'a': 'Attack', 'd': 'Defense'}, rng=lambda: 0)
        engine.begin_round({'a': 'charge', 'd': 'defense'})
        self.assertEqual(engine.mitigate('a', 'd', 'attack', 200), 0)
        self.assertAlmostEqual(engine.mitigate('a', 'd', 'special', 200), 120.4)
        self.assertEqual(engine.mitigate('a', 'd', 'attack', 200, true_damage=True), 200)
        engine.begin_round({'a': 'attack', 'd': 'defense'})
        self.assertEqual(engine.critical('a', 'attack', 100, []), 200)
        self.assertEqual(engine.mitigate('a', 'd', 'attack', 200), 172)
        self.assertEqual(engine.critical('a', 'special', 100, []), 100)


class RealRounds(unittest.IsolatedAsyncioTestCase):
    def session(self, a='Attack', b='Defense'):
        s = build(blade_type=a, enemy_type=b)
        for key in (P, E):
            s._prefetch[key] = {'profile': {'wins': 0, 'losses': 0, 'coins': 0, 'xp': 0}}
            s.stamina_manager.max_stamina[key] = 20
            s.stamina_manager.stamina[key] = 20
            s.bey_levels[key] = 100
            s.blades[key]['stats'].update(attack=300, defense=300, stamina=200)
        return s

    async def test_attack_passive_current_buffs_and_debuffs(self):
        s = self.session()
        self.assertAlmostEqual(effective_stats(s, P)['attack'], 330)
        s.status.add_buff(P, 'attack', 40, 3)
        s.status.add_buff(E, 'defense', -50, 3)
        self.assertAlmostEqual(effective_stats(s, P)['attack'], 374)
        self.assertEqual(effective_stats(s, E)['defense'], 250)
        before = s.hp[E]
        await turn(s, 'attack', 'charge')
        self.assertLess(s.hp[E], before)
        # Stored data is still the base data, never the boosted values.
        self.assertEqual(s.blades[P]['stats']['attack'], 300)

    async def test_crit_doubles_real_attack(self):
        normal, critical = self.session('Attack', 'Unknown'), self.session('Attack', 'Unknown')
        critical.type_gimmicks.forced.add((P, 'critical_strike'))
        n, c = normal.hp[E], critical.hp[E]
        await turn(normal, 'attack', 'charge')
        await turn(critical, 'attack', 'charge')
        self.assertLessEqual(abs((c - critical.hp[E]) - 2 * (n - normal.hp[E])), 1)

    async def test_defense_passive_unconditional(self):
        defense, neutral = self.session('Unknown', 'Defense'), self.session('Unknown', 'Unknown')
        before_d, before_n = defense.hp[E], neutral.hp[E]
        await turn(defense, 'attack', 'charge')
        await turn(neutral, 'attack', 'charge')
        self.assertEqual(before_d - defense.hp[E], hp_damage((before_n - neutral.hp[E]) * .86))

    async def test_kinetic_nullifies_without_consuming_shield(self):
        s = self.session()
        s.type_gimmicks.forced.add((E, 'kinetic_counter'))
        s.status.add_shield(E, 50)
        before = s.hp[E]
        await turn(s, 'attack', 'defense')
        self.assertEqual(s.hp[E], before)
        self.assertEqual(s.status.get_shield(E), 50)

    async def test_exact_critical_return_with_low_hp_and_reflect(self):
        s = self.session()
        s.hp[E] = 30
        s.ability.reflect_windows[E] = (100, 3)
        s.type_gimmicks.forced.update({(P, 'critical_strike'), (E, 'kinetic_counter')})
        before = s.hp[P]
        await turn(s, 'attack', 'defense')
        self.assertEqual(s.hp[E], 0)
        self.assertEqual(before - s.hp[P], 30)
        self.assertEqual(s.counter_damage_events, [{'kind': 'counter', 'target': P, 'amount': 30}])
        self.assertTrue(s.finished)

    async def test_both_player_orders_return_same_damage(self):
        for attacker, defender, actions in ((P, E, ('attack', 'defense')), (E, P, ('defense', 'attack'))):
            s = self.session('Attack', 'Defense') if attacker == P else self.session('Defense', 'Attack')
            s.type_gimmicks.forced.update({(attacker, 'critical_strike'), (defender, 'kinetic_counter')})
            a, d = s.hp[attacker], s.hp[defender]
            await turn(s, *actions)
            self.assertEqual(a - s.hp[attacker], d - s.hp[defender])

    async def test_special_reduction_and_no_type_critical(self):
        normal, kinetic = self.session(), self.session()
        kinetic.type_gimmicks.forced.add((E, 'kinetic_counter'))
        for s in (normal, kinetic):
            s.blades[P]['special_move'] = {'hits': 1, 'damage_per_hit': 200}
            s.special_stats[P] = s.blades[P]['stats']['special']
            s.stamina_manager.gauge[P] = 150
        n, k = normal.hp[E], kinetic.hp[E]
        await turn(normal, 'special', 'defense')
        await turn(kinetic, 'special', 'defense')
        self.assertEqual(n - normal.hp[E], 172)
        self.assertEqual(k - kinetic.hp[E], 120)
        self.assertFalse(kinetic.type_gimmicks.states[P].critical_strike_active)

    async def test_true_damage_special_bypasses_type_and_kinetic(self):
        s = self.session()
        s.blades[P]['special_move'] = {'hits': 1, 'damage_per_hit': 200, 'true_damage': True}
        s.special_stats[P] = s.blades[P]['stats']['special']
        s.stamina_manager.gauge[P] = 150
        s.type_gimmicks.forced.add((E, 'kinetic_counter'))
        s.status.add_shield(E, 100)
        before = s.hp[E]
        await turn(s, 'special', 'defense')
        self.assertEqual(before - s.hp[E], 200)

    async def test_avatar_special_riders_receive_type_mitigation(self):
        from cogs.avatar import NULL_BONUSES
        s = self.session()
        s.avatar_bonuses[P] = replace(NULL_BONUSES, special_move_percent=.25,
                                    special_move_flat=20, ult_adds_attack_stat=True)
        s.blades[P]['special_move'] = {'hits': 1, 'damage_per_hit': 200}
        s.special_stats[P] = s.blades[P]['stats']['special']
        s.stamina_manager.gauge[P] = 150
        before = s.hp[E]
        await turn(s, 'special', 'defense')
        self.assertEqual(before - s.hp[E], 520)

    async def test_kinetic_exact_return_after_avatar_mitigation(self):
        s = self.session()
        s.type_gimmicks.forced.update({(P, 'critical_strike'), (E, 'kinetic_counter')})
        before_a, before_d = s.hp[P], s.hp[E]
        with patch('cogs.battle.avatar_combat.absorb_incoming',
                   side_effect=lambda session, d, a, damage: (damage / 2, 99 if damage > 0 else 0, [])):
            await turn(s, 'attack', 'defense')
        self.assertEqual(before_a - s.hp[P], before_d - s.hp[E])

    async def test_button_validation(self):
        s = self.session()
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(response=response)
        s.stamina_manager.stamina[P] = 0
        await s.submit_move(interaction, s.players[0], 'attack')
        self.assertIsNone(s.moves[P])
        self.assertIn('Battle Stamina', response.send_message.call_args.args[0])
        await s.submit_move(interaction, s.players[0], 'special')
        self.assertIsNone(s.moves[P])
        await s.submit_move(interaction, s.players[0], 'stamina')
        self.assertEqual(s.moves[P], 'stamina')
        await s.submit_move(interaction, s.players[0], 'charge')
        self.assertEqual(s.moves[P], 'stamina')

    async def test_form_change_updates_gimmick_identity(self):
        s = self.session('Balance', 'Unknown')
        rule = {'do': [{'op': 'evolve_form', 'type': 'Attack'}]}
        s.ability._run_ops(rule, 'Form', P, E, 'attack', 0, 0, [])
        self.assertEqual(s.type_gimmicks.types[P], 'attack')
        self.assertAlmostEqual(effective_stats(s, P)['attack'], 330)

    async def test_normal_and_stamina_healing_and_caps(self):
        for t, expected in (('Unknown', 100), ('Stamina', 140)):
            s = self.session(t, 'Unknown')
            s.hp[P] = s.max_hp_per_player[P] - 400
            s.stability_manager.stability[P] = 50
            before = s.hp[P]
            await turn(s, 'stamina', 'charge')
            self.assertEqual(s.hp[P] - before, expected)
            self.assertEqual(s.stability_manager.stability[P], 58)
            self.assertLessEqual(s.stamina[P], s.stamina_manager.cap_for(P))
            await turn(s, 'stamina', 'charge')
            await turn(s, 'stamina', 'charge')
            await turn(s, 'stamina', 'charge')
            self.assertEqual(s.hp[P], s.max_hp_per_player[P])

    async def test_overdrive_replaces_normal_and_caps(self):
        s = self.session('Stamina', 'Unknown')
        s.type_gimmicks.forced.add((P, 'overdrive'))
        s.hp[P] -= 400
        s.stamina_manager.stamina[P] = 0
        s.stamina_manager.max_stamina[P] = 10
        s.stability_manager.stability[P] = 50
        before = s.hp[P]
        await turn(s, 'stamina', 'charge')
        self.assertEqual(s.hp[P] - before, 200)
        self.assertEqual(s.stamina[P], 6)
        self.assertEqual(s.stability_manager.stability[P], 80)
        await turn(s, 'stamina', 'charge')
        self.assertEqual(s.stamina[P], 10)
        self.assertEqual(s.stability_manager.stability[P], s.stability_manager.max[P])

    async def test_balance_morph_refresh_and_expiry(self):
        s = self.session('Balance', 'Unknown')
        self.assertAlmostEqual(effective_stats(s, P)['attack'], 309.9)
        self.assertAlmostEqual(effective_stats(s, P)['defense'], 309.9)
        self.assertAlmostEqual(effective_stats(s, P)['stamina'], 206.6)
        engine = s.type_gimmicks
        engine.forced.add((P, 'adaptive_morph'))
        for _ in range(4):
            await turn(s, 'charge', 'charge')
            self.assertAlmostEqual(effective_stats(s, P)['attack'], 325.395)
        engine.forced.clear()
        await turn(s, 'charge', 'charge')
        await turn(s, 'charge', 'charge')
        self.assertAlmostEqual(effective_stats(s, P)['attack'], 309.9)
        self.assertEqual(s.max_hp_per_player[P], s.base_max_hp[P])

    async def test_finish_ringout_spin_burst_draw(self):
        for kind in ('ringout', 'survival', 'burst', 'draw'):
            s = self.session('Balance', 'Balance')
            if kind == 'ringout':
                s.stability_manager.stability[P] = 0
            elif kind == 'survival':
                s.stamina_manager.max_stamina[P] = 0
                s.stamina_manager.stamina[P] = 0
            elif kind == 'burst':
                s.hp[P] = 1
            else:
                s.hp[P] = s.hp[E] = 0
            await turn(s, 'charge' if kind == 'survival' else 'attack', 'attack')
            self.assertTrue(s.finished, kind)
            if kind == 'draw':
                self.assertIsNone(s.winner_id)
            else:
                self.assertEqual(s.finish_for(P), kind)

    async def test_real_ability_stat_effect_reaches_formula_once(self):
        buffed, plain = self.session('Unknown', 'Unknown'), self.session('Unknown', 'Unknown')
        buffed.status.add_buff(P, 'attack', 100, 3)
        b, p = buffed.hp[E], plain.hp[E]
        await turn(buffed, 'attack', 'attack')
        await turn(plain, 'attack', 'attack')
        self.assertEqual(b - buffed.hp[E], 114)
        self.assertEqual(p - plain.hp[E], 86)


class BossTests(unittest.TestCase):
    def fighter(self, t='', hp=1000):
        return boss_ai.Fighter('test', hp, hp, 300, 300, 200,
                               sp=10, sp_max=10, level=100, bey_type=t)

    def test_boss_normal_formula_and_passive(self):
        a, b = self.fighter('Attack'), self.fighter('Defense')
        self.assertAlmostEqual(boss_ai._raw_damage(a, dst=b), 94.4)
        with patch('random.random', return_value=.99):
            result = boss_ai.resolve(a, b, 'attack', 'charge')
        self.assertEqual(result['dmg_to_b'], 81)

    def test_boss_critical_counter_and_clone(self):
        a, b = self.fighter('Attack'), self.fighter('Defense')
        with patch('random.random', return_value=0):
            result = boss_ai.resolve(a, b, 'attack', 'defense')
        self.assertEqual(1000 - a.hp, 1000 - b.hp)
        self.assertEqual(result['kinetic_return_a'], 1000 - b.hp)
        clone = a.clone()
        self.assertEqual(clone.level, a.level)
        self.assertEqual(clone.bey_type, a.bey_type)
        self.assertEqual(clone.morph_rounds, a.morph_rounds)

    def test_boss_overdrive(self):
        a, b = self.fighter('Stamina'), self.fighter()
        a.hp, a.sp, a.stability = 600, 0, 40
        with patch('random.random', return_value=0):
            result = boss_ai.resolve(a, b, 'stamina', 'charge')
        self.assertEqual(a.hp, 800)
        self.assertEqual(a.sp, 6)
        self.assertEqual(a.stability, 70)

    def test_boss_heals_after_damage_in_both_player_orders(self):
        for healer_first in (True, False):
            healer, attacker = self.fighter(), self.fighter()
            actors = (healer, attacker) if healer_first else (attacker, healer)
            moves = ('stamina', 'attack') if healer_first else ('attack', 'stamina')
            with patch('random.random', return_value=.99):
                result = boss_ai.resolve(*actors, *moves)
            tag = 'a' if healer_first else 'b'
            damage = result['dmg_to_' + tag]
            self.assertGreater(damage, 0)
            self.assertEqual(result['heal_' + tag], min(100, damage))
            self.assertEqual(healer.hp, 1000 - damage + min(100, damage))

    def test_dead_boss_healer_does_not_recover_or_report_healing(self):
        healer, attacker = self.fighter('Stamina'), self.fighter()
        healer.hp, healer.stability, healer.sp = 1, 40, 0
        with patch('random.random', return_value=0):
            result = boss_ai.resolve(attacker, healer, 'attack', 'stamina')
        self.assertEqual(healer.hp, 0)
        self.assertEqual(result['heal_b'], 0)
        self.assertEqual(healer.healed_total, 0)
        self.assertEqual(healer.stability, 40)
        self.assertEqual(healer.sp, 0)

    def test_boss_overdrive_heals_current_turn_damage_with_caps(self):
        healer, attacker = self.fighter('Stamina'), self.fighter()
        healer.stability, healer.sp = 95, 9
        with patch('random.random', return_value=0):
            result = boss_ai.resolve(attacker, healer, 'attack', 'stamina')
        self.assertEqual(result['heal_b'], result['dmg_to_b'])
        self.assertEqual(healer.hp, healer.max_hp)
        self.assertEqual(healer.sp, healer.sp_max)
        self.assertEqual(healer.stability, healer.max_stability)

    def test_named_boss_special_preserves_player_healing_report(self):
        from cogs.battle.boss import boss_battle as bb, argus
        from cogs.battle.boss.blade_abilities import BladeKit
        player = SimpleNamespace(id=1, display_name='Player', mention='<@1>')
        actor = self.fighter('Stamina', hp=10000)
        actor.hp = 8000
        fight = bb.BossFight(player, 'argus', party=[player],
                             _fighters={1: actor}, _kits={1: BladeKit({})},
                             _blades={1: {}})
        with patch('random.random', return_value=.99):
            result = fight._fire_special('watchfire', 'stamina', argus)
        self.assertEqual(result['heal_b'], 140)
        self.assertEqual(actor.hp, 8000 - result['dmg_to_b'] + 140)
        # Authored drain logs only the HP it actually restores at the cap.
        fight.boss.hp = fight.boss.max_hp - 10
        with patch.object(argus, 'special_damage', return_value=(0, {'drain': 500})), \
             patch('random.random', return_value=.99):
            result = fight._fire_special('watchfire', 'charge', argus)
        self.assertEqual(result['heal_a'], 10)
        self.assertEqual(fight.boss.hp, fight.boss.max_hp)

    def test_named_boss_special_uses_shared_kinetic_pipeline(self):
        from cogs.battle.boss import boss_battle as bb
        from cogs.battle.boss import argus as argus
        from cogs.battle.boss.blade_abilities import BladeKit
        player = SimpleNamespace(id=1, display_name='Player', mention='<@1>')
        def fight():
            actor = self.fighter('Defense', hp=10000)
            return bb.BossFight(player, 'argus', party=[player],
                                _fighters={1: actor}, _kits={1: BladeKit({})},
                                _blades={1: {}})
        normal, kinetic = fight(), fight()
        with patch('random.random', return_value=.99):
            ordinary = normal._fire_special('watchfire', 'defense', argus)
        with patch('random.random', return_value=0):
            countered = kinetic._fire_special('watchfire', 'defense', argus)
        self.assertLessEqual(abs(countered['dmg_to_b'] - ordinary['dmg_to_b'] * .7), 1)
        self.assertTrue(countered['gimmicks'])

    def test_search_does_not_roll_live_gimmicks(self):
        a, b = self.fighter('Attack'), self.fighter('Defense')
        with patch('random.random', side_effect=AssertionError('search rolled RNG')):
            boss_ai.resolve(a.clone(), b.clone(), 'attack', 'defense', simulate=True)
        self.assertEqual(a.hp, 1000)
