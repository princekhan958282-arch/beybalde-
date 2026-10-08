"""Verify real Draciel records through live rounds and boss projections."""

def authored_open(*args, **kwargs):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from utils.character_registry import authored_open as open_registry
    return open_registry(*args, **kwargs)

import asyncio
import copy
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.sim_battle_ticks import BattleSession, FakeChannel, FakePlayer, get_beyblade
from cogs.battle.purification import effective_stats
from cogs.battle.special_gate import ready
from cogs.battle.boss.boss_ai import Fighter, resolve
from cogs.battle.boss.blade_abilities import BladeKit
from utils.availability import obtainable

P, E = '1001', '1002'


def build(v, reverse=False, hits=1):
    blade = get_beyblade('Draciel ' + v)
    enemy = copy.deepcopy(get_beyblade('Draciel F'))
    enemy.pop('draciel_kit')
    enemy.pop('special_requires')
    enemy['name'], enemy['type'] = 'Test attacker', 'Attack'
    enemy['abilities'], enemy['ability'] = [], {}
    enemy['special_move'] = {'name':'Test Special', 'hits':hits, 'damage_per_hit':100}
    mine, foe = (enemy, blade) if reverse else (blade, enemy)
    s = BattleSession(None, FakeChannel(), FakePlayer(1001, 'A'), FakePlayer(1002, 'B'), mine, foe, payout=False)
    for key in (P, E):
        s.hp[key] = s.max_hp_per_player[key] = 5000
        s.stamina_manager.stamina[key] = s.stamina_manager.cap_for(key)
        s.stability_manager.max[key] = s.stability_manager.stability[key] = 500
    return s, (E if reverse else P), (P if reverse else E)


async def turn(s, key, own, enemy):
    other = E if key == P else P
    s.moves = {key:own, other:enemy}
    for actor in (P, E):
        s.stamina_manager.stamina[actor] = s.stamina_manager.cap_for(actor)
        if s.moves[actor] == 'special': s.stamina_manager.gauge[actor] = 150
    with patch('random.random', return_value=.99):
        await s._resolve_round()
    assert not s.finished, 'synthetic battle ended unexpectedly'


def boss_fighter(v=None):
    blade = get_beyblade('Draciel '+v) if v else {'name':'Opponent','type':'Attack'}
    return Fighter(blade['name'], 5000, 5000, 100, 120, 100,
                   sp=15, sp_max=15, gauge=150, level=50,
                   bey_type=blade['type'], ability_blade=blade)


class DracielPvp(unittest.IsolatedAsyncioTestCase):
    async def test_roster_and_regular_acquisition(self):
        ids = []
        for v, rarity in [('F','Common'),('V','Epic'),('V2','Legendary'),('G','Mythic'),('MS','Ultimate')]:
            blade = get_beyblade('Draciel '+v)
            self.assertEqual(blade['rarity'], rarity)
            self.assertEqual(blade['type'], 'Defense')
            self.assertTrue(obtainable(blade))
            self.assertIn('Draciel', blade['ability']['name'])
            self.assertIn('Draciel', blade['special_move']['name'])
            self.assertTrue(blade['image_url'].startswith('https://cdn.discordapp.com/'))
            ids.append(blade['id'])
        self.assertEqual(len(set(ids)), 5)
        with authored_open('bey') as source:
            all_blades = json.load(source)
        self.assertEqual(len({b['id'] for b in all_blades.values()}), len(all_blades))

    async def test_f_shield_is_simultaneous_expires_and_cooldown(self):
        results = []
        for reverse in (False, True):
            s, key, other = build('F', reverse)
            await turn(s, key, 'special', 'attack')
            shield = s.ability.draciel.active(key)['shield']
            self.assertGreater(shield, 0)
            self.assertLess(shield, 180)
            self.assertFalse(ready(s,key,s.blades[key],150))
            results.append((s.hp[key], shield))
            await turn(s,key,'charge','charge')
            self.assertFalse(s.ability.draciel.active(key))
            for _ in range(3): await turn(s,key,'charge','charge')
            self.assertTrue(ready(s,key,s.blades[key],150))
        self.assertEqual(results[0], results[1])

    async def test_f_foundation_cooldown_and_resource_caps(self):
        s,key,other = build('F')
        s.hp[key] = 4500
        await turn(s,key,'defense','attack')
        self.assertEqual(s.ability.draciel.states[key]['ability_ready'], 3)
        await turn(s,key,'defense','attack')
        self.assertEqual(s.ability.draciel.states[key]['ability_ready'], 3)
        await turn(s,key,'defense','attack')
        self.assertEqual(s.ability.draciel.states[key]['ability_ready'], 5)
        self.assertLessEqual(s.hp[key],s.max_hp_per_player[key])

    async def test_f_shield_protects_incoming_stability_only(self):
        s,key,other=build('F')
        await turn(s,key,'special','charge')
        before=s.stability_manager.stability[key]
        s.stability_manager._apply(key,-10)
        self.assertEqual(before-s.stability_manager.stability[key],6)
        before=s.stability_manager.stability[key]
        s.stability_manager._apply(key,-10,action=True)
        self.assertEqual(before-s.stability_manager.stability[key],10)

    async def test_v_rebound_debuff_and_expiry(self):
        s,key,other=build('V')
        baseline=effective_stats(s,other)['attack']
        await turn(s,key,'defense','attack')
        self.assertAlmostEqual(effective_stats(s,other)['attack'],baseline*.90)
        self.assertLess(s.hp[other],5000)
        await turn(s,key,'charge','charge')
        self.assertAlmostEqual(effective_stats(s,other)['attack'],baseline)

    async def test_v_wall_retaliates_on_actual_damage(self):
        s,key,other=build('V')
        await turn(s,key,'special','special')
        self.assertLess(s.hp[other],5000) # no direct damage from defensive Special
        self.assertGreater(s.ability.draciel.hits[(other,key,'special')],0)

    async def test_v2_multi_hit_stacks_once_and_caps(self):
        s,key,other=build('V2',hits=5)
        baseline=effective_stats(s,key)['defense']
        for i in range(5):
            await turn(s,key,'charge','special')
            self.assertEqual(s.ability.draciel.states[key]['shell'],min(4,i+1))
        self.assertAlmostEqual(effective_stats(s,key)['defense'],baseline*1.24)

    async def test_v2_wall_banks_and_fires_only_on_expiry(self):
        s,key,other=build('V2')
        await turn(s,key,'special','attack')
        self.assertEqual(s.hp[other],5000)
        self.assertGreater(s.ability.draciel.active(key)['stored'],0)
        await turn(s,key,'charge','charge')
        self.assertLess(s.hp[other],5000)
        old=s.hp[other]
        await turn(s,key,'charge','charge')
        self.assertEqual(s.hp[other],old)

    async def test_g_pressure_cost_preview_consumes_once(self):
        s,key,other=build('G')
        normal=s.stamina_manager.cost_for(other,'attack')
        await turn(s,key,'defense','attack')
        self.assertAlmostEqual(s.stamina_manager.cost_for(other,'attack'),normal+.4)
        await turn(s,key,'charge','charge')
        self.assertAlmostEqual(s.stamina_manager.cost_for(other,'attack'),normal)

    async def test_g_field_buff_surcharge_and_expiry_slam(self):
        s,key,other=build('G')
        baseline=effective_stats(s,key)['defense']
        normal=s.stamina_manager.cost_for(other,'attack')
        await turn(s,key,'special','charge')
        self.assertAlmostEqual(effective_stats(s,key)['defense'],baseline*1.20)
        self.assertAlmostEqual(s.stamina_manager.cost_for(other,'attack'),normal+.6)
        await turn(s,key,'charge','charge')
        self.assertLess(s.hp[other],5000)
        self.assertAlmostEqual(effective_stats(s,key)['defense'],baseline)
        self.assertAlmostEqual(s.stamina_manager.cost_for(other,'attack'),normal)

    async def test_ms_cleanses_one_def_debuff_and_reverse_guard(self):
        s,key,other=build('MS',hits=3)
        s.status.add_buff(key,'defense',-10,5)
        s.status.add_buff(key,'attack',-10,5)
        s.stability_manager.stability[key]=400
        await turn(s,key,'special','special')
        self.assertFalse(any(b['stat']=='defense' and b['amount']<0 for b in s.status.active_buffs[key]))
        self.assertTrue(any(b['stat']=='attack' for b in s.status.active_buffs[key]))
        self.assertEqual(s.stability_manager.stability[key],406)
        self.assertEqual(s.ability.draciel.states[key]['reverse_round'],2)
        await turn(s,key,'charge','attack')
        self.assertTrue(s.ability.draciel.states[key]['reverse_used'])
        self.assertEqual(s.ability.draciel.states[key]['reverse_ready'],4)

    async def test_zero_damage_does_not_stack_and_true_damage_bypasses_shield(self):
        untouched,owner,foe=build('V2')
        await turn(untouched,owner,'special','charge')
        self.assertEqual(untouched.ability.draciel.states[owner].get('shell',0),0)
        s,key,other=build('F')
        await turn(s,key,'special','charge')
        effect=s.ability.draciel.active(key)
        before=effect['shield']
        s.blades[other]['special_move']['true_damage']=True
        await turn(s,key,'charge','special')
        self.assertEqual(s.ability.draciel.states[key].get('shell',0),0)
        self.assertLess(s.hp[key],5000)
        # The true Special left the F shield untouched until its timed expiry.
        self.assertFalse(s.ability.draciel.active(key))
        self.assertEqual(effect['shield'],before)

    async def test_story_projection_keeps_live_stats_costs_and_isolates_state(self):
        from cogs.story.story_ai import project
        s,key,other=build('G')
        await turn(s,key,'special','charge')
        mine,foe=project(s,key,other),project(s,other,key)
        self.assertAlmostEqual(mine.eff_defense,effective_stats(s,key)['defense'])
        self.assertAlmostEqual(foe.cost_for('attack'),s.stamina_manager.cost_for(other,'attack'))
        self.assertFalse(mine.can('special'))
        old=copy.deepcopy(s.ability.draciel.states)
        resolve(mine,foe,'charge','attack',simulate=True)
        self.assertEqual(s.ability.draciel.states,old)

    async def test_kinetic_exact_return_gets_no_second_draciel_counter(self):
        s,key,other=build('V')
        s.moves={key:'defense',other:'attack'}
        # Force both via the existing control operations.
        s.blades[key]['abilities'][0]['rules']=[{'when':'on_round_start','do':[{'op':'gimmick_force','gimmick':'kinetic_counter'}]}]
        s.blades[other]['abilities']=[{'name':'Forced Crit','rules':[{'when':'on_round_start','do':[{'op':'gimmick_force','gimmick':'critical_strike'}]}]}]
        s.ability._compiled.clear()
        await turn(s,key,'defense','attack')
        self.assertEqual(5000-s.hp[key],5000-s.hp[other])


class DracielBoss(unittest.TestCase):
    def test_each_special_runs_and_expires(self):
        for v in ('F','V','V2','G','MS'):
            with self.subTest(v=v):
                a,b=boss_fighter(),boss_fighter(v)
                report=resolve(a,b,'attack','special',simulate=True)
                self.assertEqual(report['dmg_to_a'],0)
                self.assertIn('special',b.draciel_state)
                self.assertFalse(b.can('special'))
                self.assertFalse(BladeKit(b.ability_blade).unsupported())
                report=resolve(a,b,'charge','charge',simulate=True)
                self.assertNotIn('special',b.draciel_state)
                if v in ('V2','G'): self.assertLess(a.hp,5000)
                for _ in range(3): resolve(a,b,'charge','charge',simulate=True)
                b.gauge=150
                self.assertTrue(b.can('special'))

    def test_clone_isolates_all_draciel_state(self):
        a,b=boss_fighter(),boss_fighter('V2')
        resolve(a,b,'attack','special',simulate=True)
        old=copy.deepcopy(b.draciel_state)
        resolve(a.clone(),b.clone(),'attack','charge',simulate=True)
        self.assertEqual(b.draciel_state,old)

    def test_pressure_preview_and_stat_stacks(self):
        a,b=boss_fighter(),boss_fighter('G')
        normal=a.cost_for('attack')
        resolve(a,b,'attack','defense',simulate=True)
        self.assertAlmostEqual(a.cost_for('attack'),normal+.4)
        resolve(a,b,'charge','charge',simulate=True)
        self.assertAlmostEqual(a.cost_for('attack'),normal)
        a,b=boss_fighter(),boss_fighter('V2')
        baseline=b.eff_defense
        for _ in range(5): resolve(a,b,'attack','charge',simulate=True)
        self.assertEqual(b.draciel_state['shell'],4)
        self.assertAlmostEqual(b.eff_defense,baseline*1.24)

    def test_both_walls_have_same_result_in_either_position(self):
        for v in ('F','V2','MS'):
            a,b=boss_fighter(),boss_fighter(v)
            report=resolve(a,b,'attack','special',simulate=True)
            c,d=boss_fighter(v),boss_fighter()
            reversed_report=resolve(c,d,'special','attack',simulate=True)
            self.assertEqual(report['dmg_to_b'],reversed_report['dmg_to_a'])

    def test_boss_clock_handles_party_actor_with_fewer_exchanges(self):
        a,b=boss_fighter(),boss_fighter('G')
        a.combat_round=8
        resolve(a,b,'attack','special',simulate=True)
        self.assertEqual(b.draciel_round,10)
        self.assertFalse(b.can('special'))
        for _ in range(4): resolve(a,b,'charge','charge',simulate=True)
        b.gauge=150
        self.assertTrue(b.can('special'))


if __name__=='__main__': unittest.main(verbosity=2)
