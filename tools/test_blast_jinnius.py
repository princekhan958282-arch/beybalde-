"""Blast Jinnius stock assembly and real shared ability/stability execution."""
import copy
import math
import unittest
from unittest.mock import patch
from tools.test_october_bey_roster import build, P, E
from utils.character_registry import REGISTRY, load_beys, load_parts
from utils.bey_components import reconcile, assemble
from cogs.battle.damage_rules import resolve_special_hits
from cogs.battle.special_gate import ready


class BlastJinnius(unittest.TestCase):
    def session(self):
        s,b=build('Blast Jinnius')
        s.stability_manager.max[P]=150
        s.stability_manager.stability[P]=100
        s.stability_manager.max[E]=100
        s.stability_manager.stability[E]=100
        return s,b

    def fire(self,s,b,when,move='defense',result='win'):
        return s.ability._fire(when,P,E,b,move,result,100,0,[])

    def test_identity_components_formula_and_hidden_parts(self):
        b=REGISTRY.find_bey('Blast Jinnius')
        self.assertEqual(b['id'],'BB164')
        self.assertEqual(b['type'],'Defense')
        self.assertEqual(b['rarity'],'Legendary')
        self.assertEqual(len(load_beys()),165)
        self.assertEqual(b['ability'],b['abilities'][0])
        self.assertEqual(len(b['abilities']),2)
        self.assertIn('Blast_Jinnius.png',b['image_url'])
        p={'inventory':[b['name']], 'active_beyblade':b['name']}
        reconcile(p); before=copy.deepcopy(p); reconcile(p)
        self.assertEqual(before,p)
        stock,delta=assemble(p,b)
        self.assertEqual(stock['stats'],b['stats']); self.assertFalse(any(delta.values()))
        shop={p['id'] for p in load_parts(shop_only=True)}
        for part in b['default_parts'].values(): self.assertNotIn(part,shop)
        self.assertEqual(resolve_special_hits(b),[math.ceil(140+.55*155)])

    def test_barrier_reduces_stability_loss_and_never_removes_minimum_cost(self):
        s,b=self.session()
        s.stability_manager._apply(P,-20)
        self.assertEqual(s.stability_manager.stability[P],85)
        s.stability_manager._apply(P,-1)
        self.assertEqual(s.stability_manager.stability[P],84)
        s.stability_manager._apply(E,-20)
        self.assertEqual(s.stability_manager.stability[E],80)

    def test_charge_recovery_cooldown_and_cap(self):
        s,b=self.session()
        self.fire(s,b,'on_charge','charge')
        self.assertEqual(s.stability_manager.stability[P],106)
        self.fire(s,b,'on_charge','charge')
        self.assertEqual(s.stability_manager.stability[P],106)
        for _ in range(2): s.ability.tick_extras()
        s.stability_manager.stability[P]=148
        self.fire(s,b,'on_charge','charge')
        self.assertEqual(s.stability_manager.stability[P],150)

    def test_pressure_only_on_defense_win_and_respects_cooldown(self):
        s,b=self.session()
        for when in ('on_defense_loss','on_attack_win','on_mirror'):
            self.fire(s,b,when)
        self.assertEqual(s.stability_manager.stability[E],100)
        self.fire(s,b,'on_defense_win')
        self.assertEqual(s.stability_manager.stability[E],92)
        self.assertEqual(s.stability_manager.stability[P],104)
        self.fire(s,b,'on_defense_win')
        self.assertEqual(s.stability_manager.stability[E],92)
        for _ in range(2): s.ability.tick_extras()
        self.fire(s,b,'on_defense_win')
        self.assertEqual(s.stability_manager.stability[E],84)

    def test_special_damage_stability_heal_and_four_round_gate(self):
        s,b=self.session()
        self.assertTrue(ready(s,P,b,150))
        with patch('cogs.abilities.ability_engine.random.random',return_value=.99), patch('cogs.battle.type_gimmicks.random.random',return_value=.99):
            damage,logs=s.attack_manager._resolve_special(P,E,'special',b,s.blades[E],[])
        self.assertGreater(damage,0)
        self.assertEqual(s.stability_manager.stability[E],78)
        self.assertEqual(s.stability_manager.stability[P],112)
        self.assertFalse(ready(s,P,b,150))
        self.fire(s,b,'on_special','special')
        self.assertEqual(s.stability_manager.stability[E],78)
        for _ in range(3): s.ability.tick_extras()
        self.assertFalse(ready(s,P,b,150))
        s.ability.tick_extras()
        self.assertTrue(ready(s,P,b,150))

    def test_special_threshold_is_relative_and_checked_before_base_hit(self):
        for maximum,start,expected in ((100,35,13),(100,34,4),(200,70,48),(200,69,39)):
            with self.subTest(maximum=maximum,start=start):
                s,b=self.session()
                s.stability_manager.max[E]=maximum
                s.stability_manager.stability[E]=start
                self.fire(s,b,'on_special','special')
                self.assertEqual(s.stability_manager.stability[E],expected)

    def test_special_resistance_and_ring_out(self):
        s,b=self.session()
        s.blades[E]['abilities']=[{'name':'Resistance','burst_resistance_pct':50,'rules':[]}]
        self.fire(s,b,'on_special','special')
        self.assertEqual(s.stability_manager.stability[E],89)
        s,b=self.session(); s.stability_manager.stability[E]=20
        self.fire(s,b,'on_special','special')
        self.assertEqual(s.stability_manager.stability[E],0)
        self.assertTrue(s.stability_manager.check_ring_out(E))


if __name__=='__main__': unittest.main()
