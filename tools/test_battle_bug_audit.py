"""Audit regressions: one-round Custom effects, real healing and max-level damage."""
import unittest
from unittest.mock import patch
from tools.test_october_bey_roster import build, P, E
from tools.test_custom_bey_v2 import blade
from cogs.battle.purification import effective_stats


class RecoveryAndDuration(unittest.TestCase):
    def test_normal_heal_never_revives_or_reduces_over_max_health(self):
        for hp, expected in ((0,0),(-10,-10),(995,1000),(1100,1100),(100,112)):
            with self.subTest(hp=hp):
                s,_ = build('Dranzer')
                s.hp[P] = hp
                logs=[]
                s.ability._heal(P,12,logs,'Heal')
                self.assertEqual(s.hp[P],expected)
                self.assertEqual(bool(logs),expected>hp)

    def test_explicit_revival_still_arms_and_fires_once(self):
        s,_ = build('Dranzer')
        b = blade(['second_wind'])
        s.blades[P] = b
        s.ability._fire('turn_start',P,E,b,'attack','win',0,0,[])
        s.hp[P] = 20
        self.assertTrue(s.ability._check_revive(P,b,50))
        s.ability.dranzer.commit_damage(P,50,[])
        self.assertEqual(s.hp[P],100)
        self.assertTrue(s.status.revival_used[P])
        self.assertFalse(s.ability._check_revive(P,b,1000))

    def test_custom_defense_weaken_affects_next_snapshot_and_then_expires(self):
        s,_ = build('Dranzer')
        b = blade(['ignore_defense'])
        s.blades[P] = b
        baseline = effective_stats(s,E)['defense']
        s.ability._fire('on_attack_win',P,E,b,'attack','win',100,0,[])
        # Live sessions tick durations before taking next round's stat snapshot.
        s.ability.damage_filter._step1_tick(E,[])
        weakened = effective_stats(s,E)['defense']
        self.assertLess(weakened,baseline)
        self.assertEqual(s.status.get_duration('ignore_defense_turns',P),0)
        s.ability.damage_filter._step1_tick(E,[])
        self.assertEqual(effective_stats(s,E)['defense'],baseline)


class MaxLevelDamage(unittest.IsolatedAsyncioTestCase):
    async def session(self, level, move):
        from tools import sim_story as H
        from utils import bey_levels as BL
        from cogs.battle.battle import _apply_parts
        blades=[]
        for uid,name in ((101,'Dragoon Fantom'),(102,'Victory Valkyrie')):
            raw=H.get_beyblade(name)
            p=H.seed_profile(uid,blade=name,inventory=[name],bey_progress={name:{
                'xp':BL.xp_for_level(level),'ivs':dict.fromkeys(BL.STATS,0)}})
            blades.append(_apply_parts(raw,p))
        s=await H.BattleSession.create(None,H.FakeChannel(),H.FakePlayer(101,'A'),
            H.FakePlayer(102,'B'),*blades,payout=False,avatars_enabled=False)
        s.type_gimmicks.rng=lambda:.99
        s.moves={'101':'attack','102':move}
        a,e=effective_stats(s,'101'),effective_stats(s,'102')
        with patch('cogs.abilities.ability_engine.random.random',return_value=.99):
            hit,*_=s.attack_manager.resolve_pair('101','102','attack',move,
                s.blades['101'],s.blades['102'],a,e)
        return s,a,e,hit

    async def test_full_level_dragoon_uses_owned_growth_and_authored_smash_bonus(self):
        low,*_,low_hit=await self.session(1,'stamina')
        full,a,e,full_hit=await self.session(100,'stamina')
        self.assertEqual(a['level'],100)
        self.assertGreater(a['attack'],low.battle_stats['101']['attack'])
        self.assertGreater(full_hit,low_hit*10)
        from cogs.battle.damage_rules import calc_damage
        raw,*_=calc_damage('attack',a,e,full.blades['101'],'stamina')
        self.assertGreater(full_hit,raw)  # Dragon Smash's authored 18% amp applies.
        self.assertGreater(full_hit,270)  # No universal 170–270 damage cap.

    async def test_defense_weaken_changes_real_next_round_damage(self):
        from tools import sim_story as H
        results=[]
        for keys in ([],['ignore_defense']):
            H.seed_profile(101)
            H.seed_profile(102)
            enemy=H.get_beyblade('Dranzer')
            enemy['abilities']=[]
            enemy['ability']=None
            s=await H.BattleSession.create(None,H.FakeChannel(),H.FakePlayer(101,'A'),
                H.FakePlayer(102,'B'),blade(keys),enemy,payout=False,avatars_enabled=False)
            s.type_gimmicks.rng=lambda:.99
            damage=[]
            for _ in range(3):
                s.moves={'101':'attack','102':'charge'}
                with patch('cogs.abilities.ability_engine.random.random',return_value=.99):
                    await s._resolve_round()
                damage.append(s.attack_manager.committed_damage['101'])
            results.append(damage)
        baseline,weakened=results
        self.assertEqual(weakened[0],baseline[0])
        self.assertGreater(weakened[1],baseline[1])
        self.assertEqual(weakened[2],baseline[2])

    async def test_defense_button_reduces_damage_without_losing_level_scaling(self):
        _,a,_,attack=await self.session(100,'attack')
        _,d,_,guard=await self.session(100,'defense')
        self.assertEqual(a['attack'],d['attack'])
        self.assertGreater(guard,0)
        self.assertLess(guard,attack)


if __name__=='__main__': unittest.main()
