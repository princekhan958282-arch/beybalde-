#!/usr/bin/env python3
"""Live-manager regression coverage for Lightning Purifier, including Lv100."""
import copy
import math
import os
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.sim_cosmic_phoenix import FakeSession, am_for, DUMMY
from cogs.battle import purification as P
from cogs.battle.damage_rules import resolve_special
from cogs.battle import special_gate
from utils.database import get_beyblade
from utils import bey_levels as BL


def card(name, level=100):
    b = copy.deepcopy(get_beyblade(name))
    b['stats'] = BL.stats_at(b, level, {})
    return b


def session(enemy=None):
    return FakeSession(card('Kirindael'), enemy or DUMMY, max_hp=10000,
                       hp=10000, ehp=10000)


def cast(s, who='p', foe='e'):
    s.stamina_manager.gauge[who] = 150
    s.ability.counters[(who, 'purifier_charge')] = 60
    assert special_gate.ready(s, who, s.blades[who], 150, 150)
    damage, logs = am_for(s)._resolve_special(who, foe, 'special', s.blades[who], s.blades[foe], [])
    s.hp[foe] = max(0, s.hp[foe]-damage)
    special_gate.spend(s, who, s.blades[who])
    return damage, logs


class DomainTests(unittest.TestCase):
    def test_level100_live_special_pipeline(self):
        s = session()
        s.type_mods = {}  # Isolate the raw formula from type mitigation.
        expected = math.ceil(145 + .4*s.blades['p']['stats']['attack'] + .5*s.blades['p']['stats']['defense'] + .8*s.blades['p']['stats']['stamina'])
        damage, _ = cast(s)
        self.assertEqual(damage, expected)
        self.assertEqual(s.hp['e'], 10000-expected)
        self.assertEqual(s.stamina_manager.gauge['p'], 0)
        self.assertEqual(s.ability.counters[('p','purifier_charge')], 0)
        self.assertEqual(P.active(s, 'p')['turns'], 4)

    def test_silenced_cast_cleanses_and_opens(self):
        s = session()
        s.status.silence('p', 5)
        s.status.add_buff('p','defense',-40,4)
        cast(s)
        self.assertFalse(s.status.is_silenced('p'))
        self.assertTrue(P.active(s,'p'))
        self.assertFalse(s.status.active_buffs['p'])

    def test_blocked_cast_still_opens(self):
        s = session()
        s.status.set_invulnerable('e', 5)
        damage, _ = cast(s)
        self.assertEqual(damage, 0)
        self.assertTrue(P.active(s,'p'))

    def test_enemy_special_counts_once_per_action(self):
        s = session()
        cast(s)
        foe = s.blades['e']
        for i in range(3):
            s.ability.apply('e','p',foe,s.blades['p'],'special','win',10,0,
                            is_first_hit=i == 0, is_last_hit=i == 2)
        self.assertEqual(P.active(s,'p')['marks'],1)
        s.ability.tick_extras()
        before = s.hp['e']
        s.ability.apply('e','p',foe,s.blades['p'],'special','win',10,0)
        self.assertEqual(P.active(s,'p')['marks'],2)
        self.assertLess(s.hp['e'],before)

    def test_avatar_skill_rules_deduplicate(self):
        s=session(); cast(s)
        s.avatar_cards={'e':{'id':'regression','skills':[{'name':'Test Skill','rules':[
            {'when':'on_attack_win','do':[{'op':'log','text':'first rule'}]},
            {'when':'on_attack_win','do':[{'op':'log','text':'second rule'}]}]}]}}
        for _ in range(2):
            s.ability.apply('e','p',s.blades['e'],s.blades['p'],'attack','win',10,0)
        self.assertEqual(P.active(s,'p')['marks'],1)
        s.ability.tick_extras()
        s.ability.apply('e','p',s.blades['e'],s.blades['p'],'attack','win',10,0)
        self.assertTrue(P.active(s,'p')['judged'])

    def test_burn_silence_and_self_tradeoff(self):
        s=session(); cast(s)
        s.status.apply_burn('p',{'burn_damage_per_turn':20,'burn_duration':3})
        s.status.silence('p',4)
        s.ability._run_ops({'do':[{'op':'buff','stat':'defense','amount':-20,'turns':99}]},
                          'Self tradeoff','p','e','attack',0,0,[])
        self.assertEqual(s.status.burn_dmg['p'],10)
        self.assertEqual(s.status.silenced_turns['p'],2)
        self.assertEqual(P.active(s,'p')['conversion'],10)
        self.assertEqual(s.status.active_buffs['p'][-1]['amount'],-20)

    def test_all_healing_paths(self):
        s=session(); cast(s)
        s.hp['e']=1000
        s.ability._heal('e',100,[],'Test')
        self.assertEqual(s.hp['e'],1075)
        s.ability.lifesteal_pct['e']=100
        s.ability.apply('e','p',s.blades['e'],s.blades['p'],'attack','win',100,0)
        self.assertEqual(s.hp['e'],1150)
        baseline=session()
        baseline.hp['e']=1000
        baseline.stamina_manager.apply_stamina_action('e',baseline.hp,None,max_hp=10000)
        expected=math.floor((baseline.hp['e']-1000)*.75)
        s.hp['e']=1000
        s.stamina_manager.apply_stamina_action('e',s.hp,None,max_hp=10000)
        self.assertEqual(s.hp['e']-1000,expected)

    def test_expiry_recast_and_mirror(self):
        s=session(card('Kirindael'));cast(s)
        cast(s,'e','p')
        self.assertEqual(len(P.domains(s)),2)
        expected=round(s.blades['p']['stats']['attack']*.15)
        self.assertEqual(s.status.get_buff_bonus('p','attack'),expected)
        before=s.stability_manager.max['p']
        cast(s)
        self.assertEqual(s.stability_manager.max['p'],before)
        for _ in range(4):s.ability.tick_extras()
        self.assertFalse(P.domains(s))
        self.assertEqual(s.status.get_buff_bonus('p','attack'),0)
        self.assertEqual(s.stability_manager.max['p'],100)

    def test_no_bonus_from_blocked_action(self):
        s=session();cast(s)
        s.status.add_buff('p','stamina',-20,5)
        s.status.set_invulnerable('e',5)
        damage,_,_=s.ability.apply('p','e',s.blades['p'],s.blades['e'],'attack','win',100,0)
        self.assertEqual(damage,0)
        self.assertEqual(P.active(s,'p')['conversion'],5)

    def test_real_opponents_at_level100(self):
        for name in ('Cosmic Phoenix','Azure Drakonyx'):
            with self.subTest(enemy=name):
                s=session(card(name))
                with patch('random.random',return_value=1.0):
                    damage,_=cast(s)
                    self.assertGreater(damage,0)
                    for _ in range(4):
                        s.ability.apply('e','p',s.blades['e'],s.blades['p'],'attack','win',100,0)
                        s.ability.tick_extras()
                    self.assertFalse(P.domains(s))
                    self.assertEqual(s.stability_manager.max['p'],100)

    def test_judgment_honors_cosmic_revive(self):
        s=session(card('Cosmic Phoenix'));cast(s)
        s.hp['e']=1
        P.mark(s,'e',('special',),[])
        P.mark(s,'e',('skill','test'),[])
        self.assertTrue(s.status.revival_used['e'])
        self.assertEqual(s.hp['e'],math.floor(math.ceil(10000*.35)*.75))

    def test_multihit_conversion_covers_entire_action(self):
        s=session();cast(s)
        s.status.add_buff('p','stamina',-20,5)
        self.assertEqual(P.amplify(s,'p',100,[],first=True,last=False),105)
        self.assertEqual(P.amplify(s,'p',100,[],first=False,last=True),105)
        self.assertEqual(P.amplify(s,'p',100,[]),100)

    def test_formula_does_not_double_scale_mastery(self):
        s=session();s.type_mods={};s.stat_mult['p']=1.2
        expected=resolve_special(s.blades['p'],effective_stats=P.effective_stats(s,'p'))[1]
        damage,_=cast(s)
        self.assertEqual(damage,expected)

    def test_domain_defense_is_not_counted_twice(self):
        from cogs.battle.defense_manager import DefenseManager
        s=session();cast(s)
        s.defense_manager=DefenseManager(s)
        stats=P.effective_stats(s,'p')
        adjusted,_=s.defense_manager.preprocess_defender_stats(
            'e','p',s.blades['e'],s.blades['p'],stats)
        self.assertEqual(adjusted['defense'],stats['defense'])

    def test_non_damage_override_wins(self):
        b=card('Kirindael');b['special_move']['non_damage']=True
        self.assertEqual(resolve_special(b)[1],0)


if __name__=='__main__':unittest.main(verbosity=2)
