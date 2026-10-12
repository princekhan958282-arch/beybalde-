"""Noctilune's authored kit through real rule and round pipelines."""
import math
import unittest
from unittest.mock import patch
from tools.test_october_bey_roster import build, P, E
from utils.database import get_beyblade
from cogs.battle.damage_rules import resolve_special
from cogs.battle.purification import effective_stats

PETALS='noctilune_petals'
BLOOMS='noctilune_blooms'


class NoctiluneRules(unittest.TestCase):
    def battle(self):
        s,b=build('Noctilune')
        s.type_gimmicks.rng=lambda:.99
        return s,b

    def lose(self,s,b):
        return s.ability._fire('on_any_loss',P,E,b,'attack','lose',100,0,[])

    def test_each_loss_gains_one_shared_stack_and_both_stats(self):
        s,b=self.battle()
        for n in range(1,5):
            self.lose(s,b)
            self.assertEqual(s.ability.counters[P,PETALS],n)
            for stat in ('attack','defense'):
                self.assertEqual(s.status.get_buff_bonus(P,stat),n*round(b['stats'][stat]*.04))
        self.assertEqual(b['ability'],b['abilities'][0])

    def test_only_losing_clashes_gain_petals(self):
        for result,expected in (('win',0),('mirror',0),('lose',1),('lose_grind',1)):
            with self.subTest(result=result):
                s,b=self.battle()
                s.moves={P:'defense',E:'stamina'}
                with patch('cogs.abilities.ability_engine.random.random',return_value=.99):
                    s.ability.apply(P,E,b,s.blades[E],'defense',result,0,0)
                self.assertEqual(s.ability.counters.get((P,PETALS),0),expected)

    def test_bloom_twice_heals_cleanses_and_removes_only_petal_bonuses(self):
        s,b=self.battle();s.hp[P]=100
        s.status.add_buff(P,'attack',7,99,source='unrelated')
        for cycle in (1,2):
            s.status.add_buff(P,'defense',-10,5)
            for _ in range(5): self.lose(s,b)
            self.assertEqual(s.ability.counters[P,PETALS],0)
            self.assertEqual(s.ability.counters[P,BLOOMS],cycle)
            self.assertEqual(s.hp[P],100+cycle*150)
            self.assertEqual(s.status.get_buff_bonus(P,'attack'),7)
            self.assertEqual(s.status.get_buff_bonus(P,'defense'),0)
        for _ in range(12): self.lose(s,b)
        self.assertEqual(s.ability.counters[P,PETALS],5)
        self.assertEqual(s.ability.counters[P,BLOOMS],2)
        self.assertEqual(s.hp[P],400)
        self.assertEqual(s.status.get_buff_bonus(P,'attack'),7+5*round(108*.04))
        self.assertEqual(s.status.get_buff_bonus(P,'defense'),5*round(112*.04))

    def test_bloom_cleanses_exactly_one_negative_effect(self):
        s,b=self.battle()
        s.status.add_buff(P,'attack',-10,5)
        s.status.add_buff(P,'defense',-10,5)
        for _ in range(5):self.lose(s,b)
        negative=[x for x in s.status.active_buffs[P] if x['amount']<0]
        self.assertEqual(len(negative),1)

    def test_special_uses_live_attack_stat_formula(self):
        _,b=self.battle()
        for attack in (108,400,700):
            self.assertEqual(resolve_special(b,1000,{'attack':attack})[1],math.ceil(150+.45*attack))

    def test_special_bonus_retains_petals_and_pierces_only_at_five(self):
        for petals in range(6):
            s,b=self.battle()
            s.ability.counters[P,PETALS]=petals
            damage,_=s.ability._fire('on_special',P,E,b,'special','win',100,0,[])
            self.assertEqual(damage,100+6*petals)
            self.assertEqual(s.ability.counters[P,PETALS],petals)
            self.assertEqual(s.ability.special_pierce_pct.get(P,0),20 if petals==5 else 0)

    def test_real_special_hit_uses_formula_bonus_and_conditional_partial_pierce(self):
        from cogs.battle.type_gimmicks import TypeGimmickEngine
        for petals in range(6):
            s,b=self.battle()
            s.blades[E]['type']='Defense'
            s.type_gimmicks=TypeGimmickEngine({P:'Balance',E:'Defense'},rng=lambda:.99)
            s.moves={P:'special',E:'stamina'}
            s.ability.counters[P,PETALS]=petals
            stats=effective_stats(s,P)
            base=math.ceil(150+.45*stats['attack'])
            boosted=base+(math.ceil(base*petals*.06) if petals else 0)
            reduction=.14*(.8 if petals==5 else 1)
            with patch('cogs.abilities.ability_engine.random.random',return_value=.99):
                hit,logs=s.attack_manager._resolve_special(P,E,'special',b,s.blades[E],[])
            self.assertAlmostEqual(hit,boosted*(1-reduction))
            self.assertEqual(s.ability.counters[P,PETALS],petals)
            self.assertEqual(s.ability.special_pierce_pct.get(P,0),0)
            self.assertEqual(any('20%' in line and 'Defense mitigation' in line for line in logs),petals==5)

    def test_existing_spending_operations_still_consume_by_default(self):
        s,b=self.battle();s.ability.counters[P,'test']=3
        s.ability._run_ops({'do':[{'op':'consume_stack_damage_pct','name':'test','per_stack':10}]},
            'Test',P,E,'special',100,0,[])
        self.assertEqual(s.ability.counters[P,'test'],0)


class NoctiluneRounds(unittest.IsolatedAsyncioTestCase):
    async def test_real_losses_gain_one_petal_and_raise_next_round_stats(self):
        from tools import sim_story as H
        H.seed_profile(101);H.seed_profile(102)
        s=await H.BattleSession.create(None,H.FakeChannel(),H.FakePlayer(101,'Noct'),
            H.FakePlayer(102,'Dragoon'),get_beyblade('Noctilune'),get_beyblade('Dragoon Fantom'),
            payout=False,avatars_enabled=False)
        s.type_gimmicks.rng=lambda:.99
        s.hp=dict.fromkeys(s.hp,10000);s.max_hp_per_player=dict(s.hp)
        base=effective_stats(s,'101')
        for n in range(1,4):
            s.moves={'101':'stamina','102':'attack'}
            with patch('cogs.abilities.ability_engine.random.random',return_value=.99):
                await s._resolve_round()
            self.assertEqual(s.ability.counters['101',PETALS],n)
        live=effective_stats(s,'101')
        self.assertGreater(live['attack'],base['attack'])
        self.assertGreater(live['defense'],base['defense'])


if __name__=='__main__':unittest.main()
