"""Defense versus Special through the live PvP/Story damage pipeline."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import sim_story as H


class SpecialDefenseTests(unittest.IsolatedAsyncioTestCase):
    async def build(self, hits=1, damage=100, *, abilities=None, **special):
        H.seed_profile(101)
        H.seed_profile(102)
        blade = {'name':'Guard test Special', 'type':'Balance',
                 'stats':{'hp':1000,'attack':100,'defense':100,'stamina':100},
                 'abilities':abilities or [], 'special_move':{'hits':hits,'damage_per_hit':damage, **special}}
        target = {'name':'Guard test target','type':'Balance',
                  'stats':{'hp':1000,'attack':100,'defense':100,'stamina':100}, 'abilities':[]}
        session = await H.BattleSession.create(None,H.FakeChannel(),
            H.FakePlayer(101,'Attacker'),H.FakePlayer(102,'Defender'),blade,target,payout=False)
        session.type_gimmicks.rng = lambda: .99
        session.moves = {'101':'special','102':'defense'}
        return session

    def resolve(self, session, attacker='101', defender='102'):
        with patch('cogs.abilities.ability_engine.random.random', return_value=.99):
            return session.attack_manager._resolve_special(attacker,defender,'special',
                session.blades[attacker],session.blades[defender],[])

    async def test_single_hit_reduced_by_sixty_percent(self):
        s = await self.build()
        damage, logs = self.resolve(s)
        self.assertEqual(damage,40)
        self.assertEqual(sum('Defense vs Special' in log for log in logs),1)

    async def test_each_hit_and_extra_hit_are_reduced(self):
        s = await self.build(3,[100,75,25])
        s.ability.extra_special_hits['101'] = 1
        damage, logs = self.resolve(s)
        self.assertEqual(damage,40+30+10+26)
        self.assertEqual(sum('Defense vs Special' in log for log in logs),1)

    async def test_other_buttons_and_later_round_do_not_guard(self):
        for move in ('attack','charge','stamina','special'):
            s = await self.build()
            s.moves['102'] = move
            self.assertEqual(self.resolve(s)[0],100)
        s = await self.build()
        self.assertEqual(self.resolve(s)[0],40)
        s.moves['102'] = 'charge'
        self.assertEqual(self.resolve(s)[0],100)

    async def test_ignores_defense_special_still_respects_button_guard(self):
        s = await self.build(ignores_defense=True)
        self.assertEqual(self.resolve(s)[0],40)

    async def test_true_damage_special_and_active_true_buff_bypass_guard(self):
        s = await self.build(true_damage=True)
        damage, logs = self.resolve(s)
        self.assertEqual(damage,100)
        self.assertFalse(any('Defense vs Special' in log for log in logs))
        s = await self.build()
        s.status.set_duration('true_damage_turns','101',2)
        self.assertEqual(self.resolve(s)[0],100)

    async def test_authored_floor_does_not_cancel_button_guard(self):
        s = await self.build(damage=10,min_hit_damage=100)
        self.assertEqual(self.resolve(s)[0],40)

    async def test_minimum_hit_does_not_restore_shield_absorbed_damage(self):
        for shield, expected in ((20,20),(40,0),(100,0)):
            with self.subTest(shield=shield):
                s = await self.build(damage=100,min_hit_damage=100)
                s.status.shield_hp['102'] = shield
                self.assertEqual(self.resolve(s)[0],expected)

    async def test_minimum_hit_does_not_break_invulnerability(self):
        s = await self.build(damage=100,min_hit_damage=100)
        s.status.invulnerable_turns['102'] = 2
        self.assertEqual(self.resolve(s)[0],0)

    async def test_multi_hit_floor_consumes_shield_without_recreating_damage(self):
        s = await self.build(hits=3,damage=10,min_hit_damage=100)
        s.status.shield_hp['102'] = 60
        self.assertEqual(self.resolve(s)[0],60)
        self.assertEqual(s.status.shield_hp.get('102',0),0)

    async def test_minimum_hit_does_not_override_evasion(self):
        s = await self.build(damage=100,min_hit_damage=100)
        with patch.object(s.ability.damage_filter,'_step3b_evasion',return_value=(True,['EVADED'])):
            self.assertEqual(self.resolve(s)[0],0)

    async def test_zero_damage_special_is_not_turned_into_a_hit(self):
        s = await self.build(damage=0,min_hit_damage=100,non_damage=True)
        self.assertEqual(self.resolve(s)[0],0)

    async def test_generated_damage_is_guarded_but_direct_true_damage_bypasses(self):
        s = await self.build(abilities=[{'name':'Special payload', 'rules':[
            {'when':'on_special','do':[{'op':'bonus_damage','value':100},
                                       {'op':'true_damage','value':100}]},
            {'when':'passive','do':[{'op':'true_damage','value':50}]}]}])
        before = s.hp['102']
        self.assertEqual(self.resolve(s)[0],80)
        self.assertEqual(before-s.hp['102'],150)

    async def test_full_round_commits_guarded_damage(self):
        s = await self.build()
        before = s.hp['102']
        s.stamina_manager.gauge['101'] = 100
        with patch('cogs.abilities.ability_engine.random.random', return_value=.99):
            await s._resolve_round()
        self.assertEqual(before-s.hp['102'],40)

    async def test_swapping_player_positions_keeps_guard(self):
        s = await self.build()
        s.blades['102']['special_move'] = copy.deepcopy(s.blades['101']['special_move'])
        s.moves = {'101':'defense','102':'special'}
        self.assertEqual(self.resolve(s,'102','101')[0],40)


if __name__ == '__main__':
    unittest.main()
