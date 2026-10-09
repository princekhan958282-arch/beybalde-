"""Functional checks for every authored avatar skill (no Discord connection).

Run: python tools/test_avatar_roster.py
Original Generation's 27 interactive skills are covered by
python tools/test_original_generation.py; this suite pins their roster coverage.
"""
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.test_avatar_battle_connection import AvatarConnectionTests
from cogs.avatar.avatar_engine import avatar_engine
from cogs.battle import avatar_combat as AVC
from cogs.battle.purification import effective_stats
from cogs.battle.boss import boss_ai as AI, boss_battle as BB
from cogs.battle.boss.avatar_bonus_adapter import BonusBridge

avatar_engine.load()
CARDS=avatar_engine.get_all_avatars()

class AvatarRosterTests(AvatarConnectionTests):
    async def test_eudora_extra_hits_change_boss_hp(self):
        for hits in (1, 2, 5):
            s=await self.build('avatar_mlbb004',slot=3)
            s.blades['101']['special_move']={'hits':hits,'damage_per_hit':100}
            with patch.object(BB.bcopy,'equipped_blade',return_value=(s.blades['101'],None)):
                actor,_=await BB._player_fighter(101)
            actor.gauge=150;plain=actor.clone();plain.avatar_bonuses=None
            enemy=AI.Fighter('Boss',100000,100000,100,100,100)
            with patch('cogs.battle.type_gimmicks.random.random',return_value=.99):
                base=AI.resolve(enemy.clone(),plain,'charge','special')['dmg_to_a']
                report=AI.resolve(enemy.clone(),actor,'charge','special')
            self.assertGreater(report['dmg_to_a'],base)
            self.assertTrue(any('bonus hit' in line for line in report['gimmicks']))

    async def test_school_damage_reductions_are_incoming(self):
        for aid,slot,expected in [('avatar_s102',1,78),('avatar_s101',3,82),
                                  ('avatar_s108',3,80)]:
            s=await self.build(aid,slot=slot)
            s.moves={'101':'defense','102':'attack'}
            s.hp['101']=s.max_hp_per_player['101']*.2
            s.stamina_manager.stamina['101']=1
            damage,_=s.ability._fire_defensive('101','102',s.blades['101'],
                                               'attack','lose',100,0,[])
            self.assertEqual(damage,expected)
        s=await self.build('avatar_s102',slot=1)
        s.moves={'101':'attack','102':'attack'}
        damage,_=s.ability._fire_defensive('101','102',s.blades['101'],
                                          'attack','mirror',100,0,[])
        self.assertEqual(damage,100)

    async def test_valt_loss_removes_stat_stacks(self):
        s=await self.build('avatar_s107',slot=2)
        s.ability._fire('on_attack_win','101','102',s.blades['101'],
                        'attack','win',100,0,[])
        self.assertEqual(s.status.get_buff_bonus('101','attack'),14)
        s.ability._fire('on_attack_loss','101','102',s.blades['101'],
                        'attack','lose',100,0,[])
        self.assertEqual(s.status.get_buff_bonus('101','attack'),0)

    async def test_primed_response_applies_to_next_attack_once(self):
        s=await self.build('avatar_s105',slot=1)
        s.moves={'101':'defense','102':'attack'}
        s.ability.apply('101','102',s.blades['101'],s.blades['102'],
                         'defense','win',0,0)
        s.moves={'101':'attack','102':'charge'}
        first,_,_=s.ability.apply('101','102',s.blades['101'],s.blades['102'],
                                  'attack','win',100,0)
        second,_,_=s.ability.apply('101','102',s.blades['101'],s.blades['102'],
                                   'attack','win',100,0)
        self.assertEqual((first,second),(130,100))

    async def test_death_spiral_arms_fourth_attack(self):
        s=await self.build('avatar_s103',slot=3)
        s.moves={'101':'attack','102':'stamina'}
        with patch('cogs.abilities.ability_engine.random.random',return_value=.99):
            damage=[s.ability.apply('101','102',s.blades['101'],s.blades['102'],
                                    'attack','win',100,0)[0] for _ in range(4)]
        self.assertEqual(damage,[100,100,100,200])

    async def test_boss_rule_stacks_and_ai_snapshot_isolation(self):
        s=await self.build('avatar_s107',slot=2)
        with patch.object(BB.bcopy,'equipped_blade',return_value=(s.blades['101'],None)):
            actor,_=await BB._player_fighter(101)
        actor.sp=20;actor.sp_max=20
        enemy=AI.Fighter('Boss',100000,100000,100,100,100,sp=20,sp_max=20)
        before=copy.deepcopy(actor.avatar_rule_state)
        with patch('cogs.battle.type_gimmicks.random.random',return_value=.99):
            AI.resolve(enemy.clone(),actor.clone(),'stamina','attack',simulate=True)
            self.assertEqual(actor.avatar_rule_state,before)
            AI.resolve(enemy.clone(),actor,'stamina','attack')
            self.assertEqual(actor.avatar_rule_stats['attack'],14)
            AI.resolve(enemy.clone(),actor,'stamina','attack')
            self.assertEqual(actor.avatar_rule_stats['attack'],28)
            AI.resolve(enemy.clone(),actor,'defense','attack')
            self.assertEqual(actor.avatar_rule_stats['attack'],0)

    def test_complete_skill_roster(self):
        self.assertEqual(len(CARDS), 54)
        self.assertEqual(sum(len(a.get('skills', [])) for a in CARDS), 162)
        self.assertEqual(sum(len(a['skills']) for a in CARDS if a.get('active_battle_skills')), 27)
        for card in CARDS:
            for skill in card['skills']:
                self.assertTrue(skill.get('bonuses') or skill.get('rules') or card.get('active_battle_skills'),
                                (card['name'],skill['name']))

    async def bonus_skill(self, card, slot, skill):
        s=await self.build(card['id'], slot=slot)
        key,other='101','102'
        av=s.avatar_bonuses[key]
        bonus=skill['bonuses']
        stats=effective_stats(s,key)
        for stat,flat,pct in (('attack','attack_flat','attack_percent'),
                              ('defense','defence_flat','defence_percent'),
                              ('stamina','stamina_flat','stamina_percent')):
            if bonus.get(flat) or bonus.get(pct):
                s.avatar_bonuses[key]=None
                self.assertGreater(stats[stat],effective_stats(s,key)[stat])
                s.avatar_bonuses[key]=av
        if bonus.get('stability_percent') or bonus.get('stability_flat'):
            self.assertGreater(s.stability_manager.max[key],100)
        if bonus.get('charge_percent') or bonus.get('charge_flat'):
            s.stamina_manager.add_gauge(key,'charge')
            self.assertGreater(s.stamina_manager.gauge[key],50)
        if any(bonus.get(k) for k in ('special_move_percent','special_move_flat','ult_adds_attack_stat','multi_hit_power_double','multi_hit_extra_hits')):
            s.blades[key]['special_move']={'hits':2,'damage_per_hit':100,'ignores_defense':True}
            dmg,_=s.attack_manager._resolve_special(key,other,'special',s.blades[key],s.blades[other],[])
            self.assertGreater(dmg,200)
            if bonus.get('multi_hit_extra_hits'):
                s.blades[key]['special_move']['hits']=1
                single,_=s.attack_manager._resolve_special(key,other,'special',s.blades[key],s.blades[other],[])
                self.assertEqual(single,300)
        s.moves={key:'attack',other:'charge'}
        if bonus.get('crit_percent'):
            s.type_gimmicks.rng=lambda:.99
            s.type_gimmicks.start_round();s.type_gimmicks.begin_round(s.moves,started=True)
            with patch('cogs.battle.avatar_combat.random.random',return_value=0):
                dmg,_,_=s.ability.apply(key,other,s.blades[key],s.blades[other],'attack','win',100,0)
            self.assertEqual(dmg,200)
            if bonus.get('gauge_on_crit'):
                self.assertGreater(s.stamina_manager.gauge[key],0)
        if bonus.get('dodge_chance'):
            with patch('cogs.battle.avatar_combat.random.random',return_value=0):
                through,_,_=AVC.absorb_incoming(s,key,other,100)
            self.assertEqual(through,0)
        if bonus.get('resistance_damage_percent'):
            with patch('cogs.battle.avatar_combat.random.random',return_value=.99):
                through,_,_=AVC.absorb_incoming(s,key,other,100)
            self.assertLess(through,100)
        if bonus.get('counter_chance'):
            s.moves={key:'defense',other:'attack'}
            with patch('cogs.battle.avatar_combat.random.random',return_value=0):
                _,counter,_=AVC.absorb_incoming(s,key,other,100)
            self.assertGreater(counter,0)
        if bonus.get('resistance_status_chance'):
            with patch('cogs.battle.avatar_combat.random.random',return_value=0):
                _,_=s.ability._run_ops({'do':[{'op':'burn','value':10,'turns':2}]},
                                      'Test burn',other,key,'attack',100,0,[],'win')
            self.assertFalse(s.status.burn_stacks.get(key))
        if bonus.get('defence_break_rounds'):
            broken,_=AVC.apply_defence_break(s,key,{'defense':100})
            self.assertLess(broken['defense'],100)
            s.round=av.defence_break_rounds+1
            expired,_=AVC.apply_defence_break(s,key,{'defense':100})
            self.assertEqual(expired['defense'],100)
        if bonus.get('nth_hit_interval'):
            values=[AVC.nth_hit_bonus(s,key,100)[0] for _ in range(av.nth_hit_interval)]
            self.assertEqual(values[:-1],[0]*(av.nth_hit_interval-1))
            self.assertGreater(values[-1],0)
        if bonus.get('immortal_rounds'):
            s.hp[key]=5
            capped,_=AVC.guard_lethal(s,key,100)
            self.assertEqual(capped,4)
            s.round+=av.immortal_rounds
            expired,_=AVC.guard_lethal(s,key,100)
            self.assertEqual(expired,100)

        # The actual boss player builder must retain the active slice, and
        # shared boss hooks must change its resources or damage too.
        with patch.object(BB.bcopy,'equipped_blade',return_value=(s.blades[key],None)):
            fighter,_=await BB._player_fighter(101)
        plain=fighter.clone();plain.avatar_bonuses=None
        enemy=AI.Fighter('Boss',100000,100000,100,100,100,sp=20,sp_max=20)
        bridge=BonusBridge({'a':enemy,'b':fighter},{'a':'attack','b':'defense'})
        if bonus.get('charge_percent') or bonus.get('charge_flat'):
            charged=fighter.clone();AI.resolve(enemy.clone(),charged,'charge','charge')
            self.assertGreater(charged.gauge,50)
        if bonus.get('stability_percent') or bonus.get('stability_flat'):
            self.assertGreater(fighter.max_stability,100)
        if any(bonus.get(k) for k in ('special_move_percent','special_move_flat','ult_adds_attack_stat','multi_hit_power_double','multi_hit_extra_hits')):
            self.assertGreater(AI._raw_damage(fighter,True),AI._raw_damage(plain,True))
        if bonus.get('crit_percent'):
            with patch('cogs.battle.avatar_combat.random.random',return_value=0):
                self.assertEqual(bridge.offensive('b','attack',100),200)
            if bonus.get('gauge_on_crit'):self.assertGreater(bridge.crit_refunds['b'],0)
        if bonus.get('dodge_chance') or bonus.get('counter_chance') or bonus.get('resistance_damage_percent'):
            with patch('cogs.battle.avatar_combat.random.random',return_value=0 if bonus.get('dodge_chance') or bonus.get('counter_chance') else .99):
                through,counter=bridge.incoming('b','a',100)
            if bonus.get('counter_chance'): self.assertGreater(counter,0)
            if bonus.get('dodge_chance') or bonus.get('resistance_damage_percent'):self.assertLess(through,100)
        if bonus.get('nth_hit_interval'):
            outputs=[bridge.offensive('b','attack',100) for _ in range(av.nth_hit_interval)]
            self.assertGreater(outputs[-1],100)
            bridge.finish();self.assertEqual(fighter.avatar_bonus_state['hits'],av.nth_hit_interval)
        if bonus.get('immortal_rounds'):
            fighter.hp=5
            self.assertEqual(bridge.lethal('b',100),4)
            bridge.finish();self.assertIn('immortal',fighter.avatar_bonus_state)
        if bonus.get('defence_break_rounds'):
            self.assertGreater(AI._raw_damage(fighter,dst=enemy),AI._raw_damage(plain,dst=enemy))
        if bonus.get('resistance_status_chance'):
            state=SimpleNamespace(freeze_turns=0)
            boss=SimpleNamespace(state=state,eff_attack=100,special_damage=None,
                                 special_true_damage=False,special_ignores_defense=False,
                                 hp=1000,max_hp=1000,gauge=150)
            fight=SimpleNamespace(boss=boss,foe=fighter)
            module=SimpleNamespace(
                SPECIALS={'freeze':{'ultimate':False,'name':'Freeze'}},
                special_damage=lambda *args:(100,{'freeze':2}))
            with patch.object(BB.ai,'resolve',return_value={
                    'dmg_to_a':0,'dmg_to_b':100,'heal_a':0}), \
                 patch('cogs.battle.avatar_combat.random.random',return_value=0):
                report=BB.BossFight._fire_special(fight,'freeze','attack',module)
            self.assertEqual(state.freeze_turns,0)
            self.assertTrue(any('resisted freeze' in l for l in report['gimmicks']))

    async def rule_skill(self, card, slot, skill):
        s=await self.build(card['id'],slot=slot)
        key,other='101','102'
        s.hp[key]=s.max_hp_per_player[key]*.2
        s.hp[other]=s.max_hp_per_player[other]*.2
        s.stamina_manager.stamina[key]=1
        s.stability_manager.stability[key]=30
        s.stamina_manager.gauge[key]=150
        s.move_counts={key:{'attack':3,'defense':1,'stamina':1},other:{'attack':3}}
        s.victory_points={key:0,other:1}
        s.moves={key:'attack',other:'stamina'}
        with patch('cogs.abilities.ability_engine.random.random',return_value=0):
            for rule in skill['rules']:
                for cond in rule.get('if',[]):
                    if cond['cond']=='counter_at_least':s.ability.counters[(key,cond['name'])]=int(cond['value'])
                    if cond['cond']=='stability_above_pct':s.stability_manager.stability[key]=s.stability_manager.max[key]
                    if cond['cond']=='my_move_is':s.moves[key]=cond['value']
                    if cond['cond']=='hp_above_pct':s.hp[key]=s.max_hp_per_player[key]
                    if cond['cond']=='hp_below_pct':s.hp[key]=s.max_hp_per_player[key]*.2
                    if cond['cond']=='incoming_move_is':s.moves[other]=cond['value']
                    if cond['cond']=='enemy_most_used_move_is':s.move_counts[other]={cond['value']:3}
                event=rule['when']
                move='defense' if 'defense' in event else 'stamina' if 'stamina' in event else 'attack'
                with patch.object(s.ability,'_run_ops',wraps=s.ability._run_ops) as run:
                    s.ability._fire(event,key,other,s.blades[key],move,'win',100,0,[])
                self.assertTrue(run.called,(card['name'],slot,event,rule))
        # Rule-based cards must reach the same interpreter in the boss path.
        with patch.object(BB.bcopy,'equipped_blade',return_value=(s.blades[key],None)):
            fighter,_=await BB._player_fighter(101)
        self.assertEqual(fighter.avatar_skill_slot,slot)
        self.assertEqual(fighter.avatar_card['id'],card['id'])
        enemy=AI.Fighter('Boss',100000,100000,100,100,100,sp=20,sp_max=20)
        for move in ('attack','defense','stamina','charge','special'):
            actor=fighter.clone();actor.gauge=150;actor.sp=20;actor.sp_max=20
            with patch('cogs.abilities.ability_engine.random.random',return_value=0):
                AI.resolve(enemy.clone(),actor,'attack',move)
            self.assertTrue(actor.avatar_rule_state.get('moves'))

for card in CARDS:
    for slot,skill in enumerate(card.get('skills',[]),1):
        if card.get('active_battle_skills'):continue
        async def test(self, card=card,slot=slot,skill=skill):
            await (self.bonus_skill(card,slot,skill) if skill.get('bonuses')
                   else self.rule_skill(card,slot,skill))
        name='test_roster_'+card['id']+'_'+str(slot)
        setattr(AvatarRosterTests,name,test)

if __name__=='__main__':unittest.main()
