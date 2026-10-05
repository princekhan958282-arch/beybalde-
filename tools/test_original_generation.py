"""Original Generation integration regressions. Run with python tools/test_original_generation.py."""
import asyncio
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import sim_story as H
from cogs.avatar.avatar_engine import avatar_engine
from cogs.avatar.avatar_utils import validate_avatar_data, build_avatar_embed
from cogs.avatar import avatar_skills as AS
from cogs.avatar.avatar_shop import AvatarShop, PACK_POOL, PACK_PRICE, PACK_PULLS, _pull_from_pool
from cogs.battle.purification import effective_stats, heal_amount
from cogs.battle.original_generation_ui import SkillPicker


class OriginalGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def build(self, character='tyson', enemy=None):
        avatar_engine.load()
        H.seed_profile(101, equipped_avatar='avatar_og_'+character, avatar_energy=0)
        H.seed_profile(102, equipped_avatar='avatar_og_'+enemy if enemy else None)
        blade = dict(name='Test Bey', type='Balance', stats=dict(attack=200, defense=100, stamina=300, hp=2000, special=100),
                     abilities=[], special_move=dict(name='Test Special', damage=120))
        s = await H.BattleSession.create(None, H.FakeChannel(), H.FakePlayer(101,'Player'), H.FakePlayer(102,'Enemy'),
                                         blade, copy.deepcopy(blade), ranked=True, payout=False)
        s.type_gimmicks.rng = lambda: .99
        s.original_generation.rng = lambda: .99
        s.stamina_manager.stamina = {k:30 for k in s.blades}
        return s

    def activate(self, s, slot, move='attack', enemy_move='attack'):
        s.moves={'101':None,'102':None}
        s.original_generation.states['101'].energy=100
        self.assertIsNone(s.original_generation.select('101',slot))
        s.moves={'101':move,'102':enemy_move}
        logs=[]
        s.original_generation.begin_round(logs)
        return s.original_generation,logs

    def test_data_and_shop(self):
        avatar_engine.load()
        cards=[a for a in avatar_engine.get_all_avatars() if a.get('active_battle_skills')]
        self.assertEqual(len(cards),9)
        for card in cards:
            self.assertEqual(validate_avatar_data(card),(True,''))
            self.assertEqual(sum(card['bonuses'][k] for k in ('hp_flat','attack_flat','defence_flat','stamina_flat')),60)
            self.assertEqual([sk['energy_cost'] for sk in card['skills']],[40,60,100])
            self.assertEqual(len(card['skills']),3)
            e=build_avatar_embed(card)
            self.assertIn('Active battle skills',str(e.to_dict()))
        pool=AvatarShop(None)._build_rarity_map(PACK_POOL['original'])
        self.assertEqual(len(pool['Original Generation']),9)
        for _ in range(30):
            self.assertIn(_pull_from_pool('original',PACK_POOL['original'],pool)['id'],{c['id'] for c in cards})
        self.assertEqual(PACK_PRICE['original'],100000)
        self.assertEqual(PACK_PULLS['original'],1)
        from cogs.ui.main_shop import _avatar_embed
        field=next(f for f in _avatar_embed().fields if 'Original Generation' in f.name)
        self.assertNotIn('Slot 2',field.value)
        self.assertIn('Original Generation',field.value)

    async def test_no_persistent_energy_spent_and_all_stats(self):
        s=await self.build()
        self.assertEqual(s.original_generation.states['101'].energy,0)
        self.assertEqual(s.avatar_bonuses['101'].attack_flat,25)
        self.assertEqual(s.avatar_bonuses['101'].stamina_flat,15)
        self.assertEqual(s.avatar_bonuses['101'].defence_flat,5)
        self.assertEqual(s.avatar_bonuses['101'].hp_flat,15)
        self.assertEqual(AS.energy(await H.DB.get_user(101)),0)
        self.assertEqual(AS.active_slot({},s.avatar_cards['101']),0)

    async def test_original_pack_no_refund_and_persistent_cooldown(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from cogs.avatar.avatar_shop import ORIGINAL_PACK_COOLDOWN
        avatar_engine.load()
        profile={'coins':1000000}
        async def mutate(uid,fn):
            return fn(profile)
        shop=AvatarShop(None)
        shop._get_player_coins=AsyncMock(side_effect=lambda uid:profile['coins'])
        async def deduct(uid,amount):
            profile['coins']-=amount
        shop._deduct_coins=AsyncMock(side_effect=deduct)
        shop._add_coins=AsyncMock()
        shop._player_owns=Mock(return_value=True)
        shop._add_to_inventory=Mock()
        ctx=SimpleNamespace(author=SimpleNamespace(id=101),send=AsyncMock())
        with patch('utils.database.get_user',AsyncMock(return_value=profile)), \
             patch('utils.database.mutate_user',side_effect=mutate), \
             patch('cogs.avatar.avatar_shop.time.time',return_value=200000):
            await AvatarShop.buy_pack.callback(shop,ctx,'og')
        self.assertEqual(profile['coins'],950000)
        self.assertEqual(profile['original_generation_pack_bought_at'],200000)
        shop._add_coins.assert_not_awaited()
        self.assertIn('No refund',ctx.send.call_args.kwargs['embed'].description)
        restarted=AvatarShop(None)
        restarted._deduct_coins=AsyncMock()
        with patch('utils.database.get_user',AsyncMock(return_value=profile)), \
             patch('cogs.avatar.avatar_shop.time.time',return_value=200000+ORIGINAL_PACK_COOLDOWN-1):
            await AvatarShop.buy_pack.callback(restarted,ctx,'original_generation')
        restarted._deduct_coins.assert_not_awaited()
        self.assertIn('cooldown',ctx.send.call_args.args[0])
        with patch('utils.database.get_user',AsyncMock(return_value=profile)), \
             patch('utils.database.mutate_user',side_effect=mutate), \
             patch('cogs.avatar.avatar_shop.time.time',return_value=200000+ORIGINAL_PACK_COOLDOWN):
            await AvatarShop.buy_pack.callback(shop,ctx,'original')
        self.assertEqual(profile['coins'],850000)
        self.assertEqual(profile['original_generation_pack_bought_at'],200000+ORIGINAL_PACK_COOLDOWN)
        profile={'coins':0}
        with patch('utils.database.get_user',AsyncMock(return_value=profile)), \
             patch('cogs.avatar.avatar_shop.time.time',return_value=1000000):
            await AvatarShop.buy_pack.callback(shop,ctx,'original')
        self.assertNotIn('original_generation_pack_bought_at',profile)
        self.assertEqual(shop._deduct_coins.await_count,2)
        with patch('utils.database.get_user',AsyncMock(return_value=profile)):
            await AvatarShop.avatar_packs.callback(shop,ctx)
        field=next(f for f in ctx.send.call_args.kwargs['embed'].fields if 'Original Generation' in f.name)
        self.assertIn('0%',field.value)
        self.assertIn('48 hours',field.value)
        _,refund=await shop._resolve_pull(101,{'id':'common_test','name':'Common Test','rarity':'Common'},75000)
        self.assertEqual(refund,7500)
        shop._add_coins.assert_awaited_once_with(101,7500)

    async def test_selection_cooldown_and_ultimate(self):
        s=await self.build();og=s.original_generation
        self.assertIn('Need',og.select('101',1))
        og.states['101'].energy=100
        self.assertIsNone(og.select('101',1))
        self.assertIsNotNone(og.select('101',2))
        s.moves={'101':'attack','102':'attack'};og.begin_round([])
        self.assertEqual(og.states['101'].energy,60)
        s.round+=1;s.moves={'101':None,'102':None}
        self.assertIn('Ready',og.select('101',1))
        s.round=4;og.states['101'].energy=100
        self.assertIsNone(og.select('101',3))
        s.moves={'101':'special','102':'attack'};og.begin_round([])
        s.round=5;s.moves={'101':None,'102':None}
        self.assertIn('already',og.reason('101',3))
        view=SkillPicker(s,'101');self.assertTrue(view.children[2].disabled)

    async def test_tyson_tornado_exact_two_ticks_crit_and_surcharge(self):
        s=await self.build();og,logs=self.activate(s,3,'special')
        self.assertEqual(og.cost('102','stamina',0),0)
        rolls=iter([.43,.44]);og.rng=lambda:next(rolls)
        atk=og.attack('101');before=s.hp['102']
        og.end_round(logs)
        first=before-s.hp['102'];self.assertGreater(first,0)
        self.assertTrue(any('CRITICAL' in l for l in logs))
        s.round=2;og.begin_round([])
        self.assertEqual(og.cost('102','stamina',0),.7)
        before=s.hp['102'];og.end_round(logs);second=before-s.hp['102']
        self.assertAlmostEqual(first,second*2,delta=2)
        s.round=3;before=s.hp['102'];og.begin_round([]);og.end_round(logs)
        self.assertEqual(s.hp['102'],before)
        self.assertEqual(og.cost('102','attack',2),2)
        self.assertEqual(og.states['101'].energy,60)
        self.assertEqual(s.avatar_bonuses['101'].crit_percent,0)

    async def test_tyson_requires_special_no_charge(self):
        s=await self.build();og,logs=self.activate(s,3,'attack')
        self.assertEqual(og.states['101'].energy,100)
        self.assertFalse(og.states['101'].ultimate_used)
        self.assertFalse(og.effect('101','tornado'))

    async def test_tyson_recovery_and_streak(self):
        s=await self.build();og,logs=self.activate(s,1)
        s.hp['101']-=200;og.committed('102','101','attack',200,logs);og.end_round(logs)
        self.assertEqual(s.hp['101'],s.max_hp_per_player['101']-150)
        s.round=4;og,logs=self.activate(s,2)
        og.states['101'].streak=3
        self.assertEqual(og.before_damage('101','102','attack',100,logs),130)
        s.hp['101']=0;og.heal('101',500,logs,'test');self.assertEqual(s.hp['101'],0)

    async def test_kai_focus_scorch_and_buff_removal(self):
        s=await self.build('kai');s.og_previous_moves={'102':'attack'}
        og,logs=self.activate(s,1)
        self.assertEqual(og.preprocess('101','102',{'defense':100})['defense'],75)
        s.round=4;og,logs=self.activate(s,2);og.committed('101','102','attack',10,logs)
        before=s.hp['102'];og.end_round(logs);self.assertEqual(before,s.hp['102'])
        s.round=5;og.begin_round([]);before=s.hp['102'];og.end_round(logs);self.assertEqual(before-s.hp['102'],8)
        s.round=8;s.status.add_buff('102','defense',20,3)
        og,logs=self.activate(s,3)
        self.assertEqual(og.preprocess('101','102',{'defense':120})['defense'],100)
        self.assertEqual(og.before_damage('101','102','attack',100,logs),125)
        self.assertEqual(s.status.get_buff_bonus('102','defense'),0)

    async def test_ray_combo_split_and_shield_bypass(self):
        s=await self.build('ray');og,logs=self.activate(s,1)
        before=s.stamina['101']=10
        damage,logs=s.ability.damage_filter.defensive('101','102',s.blades['101'],s.blades['102'],'attack',90)
        self.assertEqual(damage,90)
        og.committed('101','102','attack',damage,logs)
        self.assertEqual(s.stamina['101'],10.4)
        s.round=4;og.states['101'].previous='defense';s.stability_manager.stability['101']=80
        og,logs=self.activate(s,2)
        self.assertEqual(og.stat_multiplier('101','attack'),1.15)
        self.assertEqual(s.stability_manager.stability['101'],83)
        s.round=8;og,logs=self.activate(s,3,enemy_move='defense');s.status.add_shield('102',100)
        out,bypass=og.shield('101','102',80,logs)
        self.assertEqual((out,bypass),(30,50))
        before=s.stability_manager.stability['102'];og.before_damage('101','102','attack',100,logs)
        self.assertEqual(before-s.stability_manager.stability['102'],5)

    async def test_max_shield_guard_recovery_and_stability_only(self):
        s=await self.build('max');og,logs=self.activate(s,1)
        amount=int(s.max_hp_per_player['101']*.10)
        out,bypass=og.shield('102','101',amount+20,logs)
        self.assertEqual(out,20)
        s.round=4;og,logs=self.activate(s,2,'defense')
        s.hp['101']-=200;og.guard_saved['101']=100;og.end_round(logs)
        self.assertEqual(s.hp['101'],s.max_hp_per_player['101']-200)
        s.round=5;og.begin_round([]);og.guard_saved['101']=100;og.end_round(logs)
        self.assertEqual(s.hp['101'],s.max_hp_per_player['101']-140)
        s.round=8;og,logs=self.activate(s,3)
        s.stability_manager.stability['101']=2
        s.stability_manager._apply('101',-10)
        self.assertEqual(s.stability_manager.stability['101'],9)
        s.stability_manager._apply('101',-20);self.assertEqual(s.stability_manager.stability['101'],0)

    async def test_mariah_first_hit_wound_and_ambush(self):
        s=await self.build('mariah');og,logs=self.activate(s,1)
        self.assertEqual(og.mitigate('102','101','special',100,logs),65)
        self.assertEqual(og.mitigate('102','101','special',100,logs),100)
        s.round=4;og,logs=self.activate(s,2);og.committed('101','102','attack',10,logs)
        self.assertEqual(heal_amount(s,'102',100),60)
        s.round=6;self.assertEqual(heal_amount(s,'102',100),100)
        s.round=8;og,logs=self.activate(s,3,enemy_move='stamina')
        self.assertEqual(og.before_damage('101','102','attack',100,logs),140)
        self.assertFalse(og.effect('101','ambush'))

    async def test_daichi_stability_and_scaling(self):
        s=await self.build('daichi');s.stability_manager.stability['101']=80
        og,logs=self.activate(s,1);self.assertEqual(s.stability_manager.stability['101'],86)
        s.round=4;og,logs=self.activate(s,2)
        self.assertEqual(og.cost('101','attack',2),2.3)
        og.committed('101','102','attack',10,logs)
        self.assertEqual(s.stability_manager.stability['102'],96)
        s.round=8;og,logs=self.activate(s,3);s.stability_manager.stability['102']=40
        self.assertEqual(og.before_damage('101','102','attack',100,logs),135)

    async def test_lee_mark_counter_and_seal(self):
        s=await self.build('lee','kai');og,logs=self.activate(s,1)
        og.committed('102','101','attack',10,logs)
        self.assertEqual(len(og.retaliation),1)
        og.committed('102','101','attack',10,logs);self.assertEqual(len(og.retaliation),1)
        before=s.hp['102'];og.end_round(logs);self.assertLess(s.hp['102'],before)
        s.round=4;og,logs=self.activate(s,2,'defense');og.committed('102','101','attack',200,logs)
        self.assertEqual(og.retaliation[0][2],40)
        s.round=8;og.states['102'].last_skill=2;og,logs=self.activate(s,3)
        s.round=9;s.moves={'101':None,'102':None};og.states['102'].energy=100
        self.assertIn('sealed',og.reason('102',2));self.assertIsNone(og.reason('102',1))
        s.round=10;self.assertIsNone(og.reason('102',2))

    async def test_gary_incoming_only_defense_lock_and_rage(self):
        s=await self.build('gary');og,logs=self.activate(s,1)
        before=s.hp['101'];s.stability_manager._apply('101',-10)
        self.assertEqual(s.hp['101'],before-15)
        before=s.hp['101'];s.stability_manager._action_cost('101',-10,'attack')
        self.assertEqual(s.hp['101'],before)
        s.round=4;og,logs=self.activate(s,2)
        self.assertEqual(og.before_damage('101','102','attack',100,logs),135)
        s.round=5;self.assertIsNotNone(og.blocked_move('101','defense'))
        s.round=6;self.assertIsNone(og.blocked_move('101','defense'))
        s.round=8;og,logs=self.activate(s,3)
        self.assertEqual(og.states['101'].rage,2)
        self.assertEqual(og.before_damage('101','102','attack',100,logs),115)
        s.round=9;s.moves={'101':'stamina','102':'attack'};og.begin_round([])
        self.assertEqual(og.states['101'].rage,0)

    async def test_kevin_denies_energy_and_punishes_repeats(self):
        s=await self.build('kevin','tyson');og,logs=self.activate(s,2)
        og.committed('101','102','attack',10,logs);og.end_round(logs)
        self.assertEqual(og.states['102'].energy,0)
        s.round=5;s.og_previous_moves={'102':'attack'};og,logs=self.activate(s,3)
        self.assertEqual(og.cost('102','attack',2),2.4)
        self.assertEqual(og.cost('102','defense',2),2)
        s.round=7;self.assertEqual(og.cost('102','attack',2),2)

    async def test_every_skill_through_real_round(self):
        for ch in ('tyson','kai','ray','max','mariah','daichi','lee','gary','kevin'):
            for slot in (1,2,3):
                with self.subTest(character=ch,slot=slot):
                    s=await self.build(ch)
                    s.original_generation.states['101'].energy=100
                    self.assertIsNone(s.original_generation.select('101',slot))
                    s.moves={'101':'special' if ch=='tyson' and slot==3 else 'attack','102':'defense'}
                    s.stamina_manager.gauge['101']=150
                    await s._resolve_round()
                    self.assertEqual(s.round,2)
                    self.assertEqual(s.original_generation.states['101'].energy,120-(40,60,100)[slot-1])
                    self.assertEqual(AS.energy(await H.DB.get_user(101)),0)

    def test_boss_skills_and_search_clone_isolation(self):
        from cogs.battle.boss import boss_ai as ai
        from cogs.battle.original_generation import SkillState
        avatar_engine.load()
        for ch in ('tyson','kai','ray','max','mariah','daichi','lee','gary','kevin'):
            for slot in (1,2,3):
                with self.subTest(character=ch,slot=slot):
                    a=ai.Fighter('Boss',2000,2000,120,100,200,sp=30,sp_max=30,bey_type='Balance')
                    b=ai.Fighter('Player',2000,2000,200,100,300,sp=30,sp_max=30,bey_type='Balance')
                    b.avatar_card=avatar_engine.get_avatar('avatar_og_'+ch)
                    b.avatar_combat_state={'states':{'b':SkillState(energy=100,pending=slot)}}
                    move='special' if ch=='tyson' and slot==3 else 'attack'
                    b.gauge=150
                    snapshot=copy.deepcopy(b.avatar_combat_state)
                    ai.resolve(a.clone(),b.clone(),'attack',move,simulate=True)
                    self.assertEqual(b.avatar_combat_state,snapshot)
                    ai.resolve(a,b,'attack',move)
                    self.assertEqual(b.avatar_combat_state['states']['b'].energy,120-(40,60,100)[slot-1])
                    self.assertEqual(b.combat_round,1)
                    self.assertEqual(b.avatar_stat_multipliers,{})

    def test_boss_tornado_crit_timers_and_energy(self):
        from cogs.battle.boss import boss_ai as ai
        from cogs.battle.original_generation import SkillState
        a=ai.Fighter('Boss',2000,2000,100,100,200,sp=30,sp_max=30,bey_type='Balance')
        b=ai.Fighter('Player',2000,2000,200,100,300,sp=30,sp_max=30,bey_type='Balance')
        avatar_engine.load();b.avatar_card=avatar_engine.get_avatar('avatar_og_tyson')
        b.avatar_combat_state={'states':{'b':SkillState(energy=100,pending=3)}}
        b.gauge=150
        with patch('cogs.battle.original_generation.random.random',return_value=0):
            first=ai.resolve(a,b,'charge','special')
            second=ai.resolve(a,b,'stamina','charge')
            third=ai.resolve(a,b,'stamina','charge')
        self.assertTrue(any('CRITICAL' in l for l in first['gimmicks']))
        self.assertTrue(any('Galaxy Turbo Twister' in l for l in second['gimmicks']))
        self.assertFalse(any('Galaxy Turbo Twister' in l for l in third['gimmicks']))
        self.assertEqual(b.avatar_combat_state['states']['b'].energy,60)

    def test_boss_tornado_respects_last_round_of_aegis(self):
        from cogs.battle.boss import boss_ai as ai
        from cogs.battle.boss.argus import ArgusState
        from cogs.battle.original_generation import SkillState
        avatar_engine.load()
        a=ai.Fighter('Boss',2000,2000,100,100,200,sp=30,sp_max=30,bey_type='Balance')
        b=ai.Fighter('Player',2000,2000,200,100,300,sp=30,sp_max=30,bey_type='Balance')
        a.state=ArgusState(aegis_turns=1)
        b.avatar_card=avatar_engine.get_avatar('avatar_og_tyson')
        b.avatar_combat_state={'states':{'b':SkillState(energy=100,pending=3)}}
        b.gauge=150
        baseline_a,baseline_b=a.clone(),b.clone()
        baseline_b.avatar_combat_state['states']['b'].pending=0
        with patch('cogs.battle.original_generation.random.random',return_value=0):
            ai.resolve(baseline_a,baseline_b,'attack','special')
            ai.resolve(a,b,'attack','special')
            self.assertEqual(a.hp,baseline_a.hp)
            self.assertEqual(a.state.aegis_turns,0)
            ai.resolve(a,b,'attack','charge')
            ai.resolve(baseline_a,baseline_b,'attack','charge')
        self.assertLess(a.hp,baseline_a.hp)

    async def test_actual_guard_feint_and_stamina_surcharge(self):
        s=await self.build('kevin');og,logs=self.activate(s,1,enemy_move='defense')
        k,o='101','102';stats,enemy=effective_stats(s,k),effective_stats(s,o)
        from cogs.battle.damage_rules import calc_damage
        base=calc_damage('attack',stats,enemy,s.blades[k],'defense')[0]
        dmg,_,_,_=s.attack_manager.resolve_pair(k,o,'attack','defense',s.blades[k],s.blades[o],stats,enemy)
        self.assertGreater(dmg,base)
        s=await self.build('tyson');og,logs=self.activate(s,3,'special')
        s.round=2;s.moves={'101':'attack','102':'stamina'};og.begin_round([])
        self.assertEqual(s.stamina_manager.cost_for('102','stamina'),.7)
        before=s.stamina['102'];s.stamina_manager.deduct_cost('102','stamina')
        self.assertAlmostEqual(before-s.stamina['102'],.7)


    async def test_private_skill_controls_reject_wrong_owner_and_stale_round(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        s=await self.build('tyson')
        og=s.original_generation;og.states['101'].energy=100
        view=SkillPicker(s,'101')
        def interaction(uid):
            return SimpleNamespace(user=SimpleNamespace(id=uid),response=SimpleNamespace(
                send_message=AsyncMock(),edit_message=AsyncMock()))
        wrong=interaction(102);await view.children[0].callback(wrong)
        self.assertEqual(og.states['101'].pending,0)
        self.assertTrue(wrong.response.send_message.called)
        stale=interaction(101);s.round+=1;await view.children[0].callback(stale)
        self.assertEqual(og.states['101'].pending,0)
        fresh=SkillPicker(s,'101');good=interaction(101);await fresh.children[0].callback(good)
        self.assertEqual(og.states['101'].pending,1)
        duplicate=interaction(101);await fresh.children[1].callback(duplicate)
        self.assertEqual(og.states['101'].pending,1)
        self.assertTrue(duplicate.response.send_message.called)
        self.assertEqual(og.states['101'].energy,100)

    async def test_battle_panel_and_boss_skill_button(self):
        from types import SimpleNamespace
        from cogs.battle.session import _InChannelControlPanel
        from cogs.battle.boss.boss_battle import BossView
        from cogs.battle.boss.boss_ai import Fighter
        s=await self.build('tyson')
        panel=_InChannelControlPanel(s)
        self.assertTrue(any(c.label=='Avatar Skill' for c in panel.children))
        s.original_generation.states={}
        self.assertFalse(any(c.label=='Avatar Skill' for c in _InChannelControlPanel(s).children))
        b=Fighter('Player',2000,2000,100,100,100)
        b.avatar_card=avatar_engine.get_avatar('avatar_og_tyson')
        f=SimpleNamespace(foe=b,finished=False)
        view=BossView(None,f)
        self.assertTrue(any(c.label=='Avatar Skill' for c in view.children))

    async def test_legacy_skill_seal_expires_without_removing_card_stats(self):
        from cogs.avatar.avatar_engine import AvatarBonuses
        s=await self.build('lee')
        s.avatar_cards['102']=avatar_engine.get_avatar('avatar_x002')
        s.avatar_skill_slots['102']=1
        s.avatar_bonuses['102']=AvatarBonuses(attack_percent=.3,hp_percent=.12)
        og,logs=self.activate(s,3)
        self.assertEqual(s.avatar_bonuses['102'].attack_percent,0)
        self.assertEqual(s.avatar_bonuses['102'].hp_percent,.12)
        s.round=3;s.moves={'101':'attack','102':'attack'};og.begin_round([])
        self.assertEqual(s.avatar_bonuses['102'].attack_percent,.3)

    def test_fixed_skill_upgrades_do_not_charge_coins(self):
        from cogs.avatar.avatar_progress import quote_skill,apply_skill_purchase,PurchaseError
        p={'coins':1000000}
        q=quote_skill(p,'avatar_og_tyson','galaxy-turbo-twister')
        self.assertEqual(q['cost'],0)
        self.assertTrue(q['blocked'])
        with self.assertRaises(PurchaseError):apply_skill_purchase(p,'avatar_og_tyson','galaxy-turbo-twister')
        self.assertEqual(p['coins'],1000000)


if __name__=='__main__':
    unittest.main()
