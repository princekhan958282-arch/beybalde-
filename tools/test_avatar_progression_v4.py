"""V4 economy, transactional storage, feeding, recovery and battle regressions."""
import copy
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from cogs.avatar import avatar_collection as AC, avatar_config as C
from cogs.avatar import avatar_levels as AL, avatar_progress as AP
from cogs.avatar.avatar_engine import avatar_engine
from cogs.avatar.avatar_rewards import enqueue, decide, RewardView, AvatarRewards, present
from cogs.avatar.avatar_progression_ui import progression_embed, ProgressionView
from utils import database as DB
from utils.userstore import UserStore

AID = 'avatar_og_tyson'
OTHER = 'avatar_og_kai'


class V4Tests(unittest.TestCase):
    def setUp(self):
        avatar_engine.load()
        self.catalog = {c['id']: c for c in avatar_engine.get_all_avatars()}
        self.p = AC.migrate({'coins': 1000000, 'equipped_avatar': AID}, [AID])
        self.p['avatar_copies'].update({AID: 100, OTHER: 100})

    def test_exact_level_allocations_include_first_level(self):
        allocations = {'attack': (9,3,3), 'defense': (3,9,3), 'stamina': (3,3,9), 'balance': (5,5,5)}
        for kind, values in allocations.items():
            for level in range(1, 6):
                gain = AL.card_stat_bonus(kind, level)
                self.assertEqual(tuple(gain.values()), tuple(n * level for n in values))
                self.assertEqual(sum(gain.values()), 15 * level)
        self.assertEqual(sum(AL.card_stat_bonus('unknown', 100).values()), 75)
        AC.migrate(self.p)
        AC.migrate(self.p)
        self.assertEqual(AL.card_stat_bonus('attack', AP.card_level(self.p, AID)),
                         {'attack': 9, 'defense': 3, 'stamina': 3})

    def test_all_star_settings_and_cumulative_gains(self):
        approved = {2:1.,3:1.,4:1.,5:1.,6:.5,7:.25,8:.05}
        gains = [0,5,7,10,15,40,65,110,140,175,210,250,300,350,450]
        self.assertEqual(C.MAX_STARS,15)
        self.assertEqual(C.STAR_SUCCESS, approved)
        for star in range(1,16):
            AP._ensure(self.p,AID)['stars'] = star
            self.assertEqual(AC.stat_bonus(self.p,self.catalog[AID]),
                             dict.fromkeys(AL.STATS,sum(gains[:star])))
            if star > 1:
                self.assertEqual(C.STAR_COPY_COST[star],5*(star-1))
        self.assertEqual(C.star_stat_total(15), 2127)

    def test_risky_thresholds_fail_at_boundary_without_extra_spend(self):
        for target, chance in ((6,.5),(7,.25),(8,.05)):
            for roll, success in ((chance - .00001,True),(chance,False),(.99,False)):
                p = copy.deepcopy(self.p)
                AP._ensure(p,AID).update(stars=target-1,feeding=C.STAR_COPY_COST[target])
                result = AC.upgrade_star(p,AID,target,lambda:roll)
                self.assertEqual(result['success'],success)
                self.assertEqual(p['coins'],1000000)
                self.assertEqual(p['avatar_copies'][AID],100)
                if not success:
                    self.assertNotIn(AID,p['avatar_inventory'])
                    self.assertNotIn(AID,p['avatar']['cards'])
                    self.assertIsNone(p['equipped_avatar'])

    def test_pending_rates_block_roll_and_feeding_without_mutation(self):
        for source in range(8,15):
            AP._ensure(self.p,AID).update(stars=source,feeding=70)
            before=copy.deepcopy(self.p)
            roll=Mock()
            with self.assertRaisesRegex(AP.PurchaseError,'pending approval'):
                AC.upgrade_star(self.p,AID,source+1,roll)
            with self.assertRaisesRegex(AP.PurchaseError,'pending approval'):
                AC.feed(self.p,AID,70,{OTHER:1},self.catalog)
            roll.assert_not_called()
            self.assertEqual(self.p,before)

    def test_same_category_selected_copies_consumed_and_primary_preserved(self):
        AC.grant(self.p,OTHER)
        AP._ensure(self.p,OTHER).update(level=5,stars=7)
        AC.feed(self.p,AID,0,{AID:2,OTHER:3},self.catalog)
        self.assertEqual(AC.stages(self.p,AID),5)
        self.assertEqual(self.p['avatar_copies'][AID],98)
        self.assertEqual(self.p['avatar_copies'][OTHER],97)
        self.assertIn(OTHER,self.p['avatar_inventory'])
        self.assertEqual(AP.card_level(self.p,OTHER),5)
        AC.upgrade_star(self.p,AID,2,Mock(side_effect=AssertionError()))
        self.assertEqual(AC.stages(self.p,AID),0)

    def test_bad_feeds_do_not_consume_any_material(self):
        wrong = next(c['id'] for c in self.catalog.values() if AC.category(c)!=AC.category(self.catalog[AID]))
        self.p['avatar_copies'][wrong]=50
        for materials in ({wrong:5},{OTHER:101},{OTHER:0},{OTHER:-1},{OTHER:True},{OTHER:6},{'nope':1}):
            before=copy.deepcopy(self.p)
            with self.assertRaises(AP.PurchaseError):
                AC.feed(self.p,AID,0,materials,self.catalog)
            self.assertEqual(before,self.p)
        self.p['avatar_copies'][OTHER]=0
        self.assertNotIn(OTHER,AC.eligible_materials(self.p,AID,self.catalog))

    def test_stale_confirmation_cannot_destroy_reacquired_card(self):
        entry=AP._ensure(self.p,AID)
        generation=entry['generation']
        entry.update(stars=5,feeding=25)
        AC.upgrade_star(self.p,AID,6,lambda:1,generation=generation)
        AC.grant(self.p,AID)
        AP._ensure(self.p,AID).update(stars=5,feeding=25)
        with self.assertRaisesRegex(AP.PurchaseError,'different copy'):
            AC.upgrade_star(self.p,AID,6,lambda:1,generation=generation)
        self.assertIn(AID,self.p['avatar_inventory'])

    def test_legacy_feeding_migration_is_idempotent(self):
        p={'equipped_avatar':AID,'avatar':{'cards':{AID:{'level':4,'stars':7,
            'stages':3,'skills':{'storm':6},'spent':{'card':123}}}}}
        AC.migrate(p,[AID,AID,OTHER])
        before=copy.deepcopy(p)
        AC.migrate(p,[AID,AID])
        self.assertEqual(p,before)
        self.assertEqual(AC.stages(p,AID),3)
        self.assertEqual(AC.stars(p,AID),7)
        self.assertEqual(AP.card_level(p,AID),4)
        self.assertEqual(AP.skill_level(p,AID,'storm'),6)
        self.assertEqual(AC.spare_copies(p,AID),1)

    def test_new_duplicate_keep_and_sell_only_touch_reward(self):
        old=copy.deepcopy(self.p['avatar'])
        reward=enqueue(self.p,OTHER,reward_id='new',sell_value=7000,now=10)
        self.assertNotIn(OTHER,self.p['avatar_inventory'])
        decide(self.p,reward['id'],'keep',now=11)
        self.assertIn(OTHER,self.p['avatar_inventory'])
        enqueue(self.p,AID,reward_id='dupe',sell_value=7000,now=10)
        decide(self.p,'dupe','keep',now=11)
        self.assertEqual(AC.spare_copies(self.p,AID),101)
        self.assertEqual(self.p['coins'],1000000)
        enqueue(self.p,AID,reward_id='sell',sell_value=7000,now=10)
        decide(self.p,'sell','sell',now=11)
        decide(self.p,'sell','sell',now=11)
        decide(self.p,'sell','keep',now=11)
        self.assertEqual(self.p['coins'],1007000)
        self.assertEqual(self.p['avatar']['cards'][AID],old['cards'][AID])
        self.assertEqual(self.p['equipped_avatar'],AID)
        self.assertEqual(AC.spare_copies(self.p,AID),101)

    def test_multiple_rewards_idempotent_enqueue_and_timeout_keep(self):
        r=enqueue(self.p,AID,reward_id='one',sell_value=999,now=0)
        self.assertEqual(enqueue(self.p,AID,reward_id='one',sell_value=5,now=5),r)
        enqueue(self.p,AID,reward_id='two',sell_value=999,now=0)
        decide(self.p,'one','sell',now=C.REWARD_TIMEOUT)
        decide(self.p,'two','keep',now=20)
        self.assertEqual(AC.spare_copies(self.p,AID),102)
        self.assertEqual(self.p['coins'],1000000)
        self.assertEqual(self.p['avatar_rewards']['one']['state'],'keep')


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.temp.name)/'users.db')
        self.store=self.new_store()
        self.store.put_one('101',AC.migrate({'coins':0},[AID]))

    def new_store(self):
        return UserStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def mutate(self,store,fn):
        def apply(p):
            return p,fn(p)
        return store.mutate_one('101',apply)

    def test_concurrent_sqlite_connections_pay_once_and_restart_recovers(self):
        self.mutate(self.store,lambda p:enqueue(p,AID,reward_id='one',sell_value=12345))
        stores=[self.new_store(),self.new_store()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda store:self.mutate(store,lambda p:decide(p,'one','sell')),stores))
        restarted=self.new_store().get_one('101')
        self.assertEqual(restarted['coins'],12345)
        self.assertEqual(restarted['avatar_rewards']['one']['state'],'sell')
        self.assertEqual(AC.spare_copies(restarted,AID),0)

    def test_feeding_transaction_rollback_and_concurrent_spending(self):
        avatar_engine.load()
        catalog={c['id']:c for c in avatar_engine.get_all_avatars()}
        def seed(p):
            p['avatar_copies'][OTHER]=5
        self.mutate(self.store,seed)
        def feed(store):
            try:
                return self.mutate(store,lambda p:AC.feed(p,AID,0,{OTHER:5},catalog))
            except AP.PurchaseError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(feed,[self.new_store(),self.new_store()]))
        self.assertEqual(sum(r is not None for r in results),1)
        p=self.new_store().get_one('101')
        self.assertEqual(p['avatar_copies'][OTHER],0)
        self.assertEqual(AC.stages(p,AID),5)
        before=copy.deepcopy(p)
        def abort(p):
            p['coins']=123
            raise RuntimeError('abort')
        with self.assertRaises(RuntimeError):self.mutate(self.store,abort)
        self.assertEqual(self.store.get_one('101'),before)

    def test_stale_save_cannot_restore_pending_sell_or_currency(self):
        with patch.object(DB,'USER_STORE',self.store):
            DB._mutate_user_sync(101,lambda p:enqueue(p,AID,reward_id='one',sell_value=10))
            stale=DB._get_user_sync(101)
            DB._mutate_user_sync(101,lambda p:decide(p,'one','sell'))
            with self.assertRaisesRegex(ValueError,'Stale'):
                DB._update_user_sync(101,stale)
            p=DB._get_user_sync(101)
            self.assertEqual(p['coins'],10)
            self.assertEqual(p['avatar_rewards']['one']['state'],'sell')

    def test_explicit_restore_cannot_roll_back_revision_token(self):
        with patch.object(DB,'USER_STORE',self.store):
            for index in range(3):
                DB._mutate_user_sync(101,lambda p:enqueue(p,AID,reward_id=f'r{index}'))
            old=DB._get_user_sync(101)
            old['avatar_revision']=0
            def restore(p):
                p.clear()
                p.update(copy.deepcopy(old))
                p['avatar_copies'][AID]=2
            DB._mutate_user_sync(101,restore)
            self.assertEqual(DB._get_user_sync(101)['avatar_revision'],4)

    def test_mysql_transaction_contract_row_lock_commit_and_rollback(self):
        from utils.mysql_store import MySQLStore
        store=MySQLStore.__new__(MySQLStore)
        store.ensure_ready=Mock()
        connection=MagicMock()
        cursor=connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value={'data':'{"coins":0}'}
        store._conn=Mock(return_value=connection)
        store.put_one=Mock()
        result=store.mutate_one('101',lambda p:({'coins':12},'ok'))
        self.assertEqual(result,'ok')
        self.assertTrue(any('FOR UPDATE' in call.args[0] for call in cursor.execute.call_args_list))
        connection.begin.assert_called_once()
        connection.commit.assert_called_once()
        store.put_one.assert_called_once_with('101',{'coins':12},touch=True)
        with self.assertRaises(RuntimeError):
            store.mutate_one('101',Mock(side_effect=RuntimeError('abort')))
        connection.rollback.assert_called_once()


@unittest.skipUnless(os.environ.get('AVATAR_V4_TEST_MYSQL_URL'), 'Live MySQL URL not configured')
class LiveMySQLTransactionTests(TransactionTests):
    """CI uses an isolated MySQL 8 service, never the production MYSQL_URL."""
    def new_store(self):
        from utils.mysql_store import MySQLStore
        return MySQLStore(os.environ['AVATAR_V4_TEST_MYSQL_URL'])


class RecoveryUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        avatar_engine.load()
        self.temp=tempfile.TemporaryDirectory()
        self.store=UserStore(str(Path(self.temp.name)/'users.db'))
        self.store.put_one('101',AC.migrate({'coins':0},[AID]))
        self.patch=patch.object(DB,'USER_STORE',self.store)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    async def test_reward_buttons_owner_keep_sell_and_double_click(self):
        r=await DB.mutate_user(101,lambda p:enqueue(p,AID,sell_value=10))
        view=RewardView(101,r)
        interaction=SimpleNamespace(user=SimpleNamespace(id=102),response=SimpleNamespace(
            send_message=AsyncMock(),edit_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction))
        interaction.user.id=101
        self.assertTrue(await view.interaction_check(interaction))
        await view.children[1].callback(interaction)
        await view.children[0].callback(interaction)
        self.assertEqual((await DB.get_user(101))['coins'],10)
        self.assertTrue(all(c.disabled for c in view.children))
        r=await DB.mutate_user(101,lambda p:enqueue(p,AID,sell_value=10))
        await RewardView(101,r).children[0].callback(interaction)
        self.assertEqual(AC.spare_copies(await DB.get_user(101),AID),1)

    async def test_worker_restart_reattaches_views_and_keeps_expired_rewards(self):
        r1=await DB.mutate_user(101,lambda p:enqueue(p,AID,reward_id='active'))
        await DB.mutate_user(101,lambda p:enqueue(p,OTHER,reward_id='expired',now=0))
        await DB.mutate_user(101,lambda p:p['avatar_rewards']['active'].update(message_id=123,channel_id=456))
        bot=SimpleNamespace(get_channel=Mock(return_value=SimpleNamespace()),add_view=Mock())
        cog=AvatarRewards(bot)
        await cog.recover()
        bot.add_view.assert_called_once()
        self.assertEqual(bot.add_view.call_args.kwargs['message_id'],123)
        p=await DB.get_user(101)
        self.assertIn(OTHER,p['avatar_inventory'])
        self.assertEqual(p['avatar_rewards']['expired']['state'],'keep')
        await cog.recover()
        bot.add_view.assert_called_once()
        # New process/cog still registers the saved persistent view.
        await AvatarRewards(bot).recover()
        self.assertEqual(bot.add_view.call_count,2)

    async def test_delivery_message_metadata_survives_restart(self):
        r=await DB.mutate_user(101,lambda p:enqueue(p,AID))
        destination=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123,channel=SimpleNamespace(id=456))))
        await present(SimpleNamespace(),101,r,destination)
        p=await DB.get_user(101)
        self.assertEqual(p['avatar_rewards'][r['id']]['message_id'],123)
        self.assertEqual(p['avatar_rewards'][r['id']]['channel_id'],456)
        self.assertEqual(p['avatar_rewards'][r['id']]['state'],'pending')

    async def test_code_story_and_admin_rewards_all_queue_decisions(self):
        from cogs.codes.redeem import grant
        from cogs.story.story_cog import StoryCog
        from cogs.admin.actions import ActionCtx, _giveavatar
        rewards=[{'kind':'avatar','value':AID}, {'kind':'avatar','value':AID}]
        await grant(101,rewards,source='test',channel_id=456)
        await grant(101,rewards,source='test',channel_id=456)
        p=await DB.get_user(101)
        self.assertEqual(len(p['avatar_rewards']),2)
        self.assertEqual(AC.spare_copies(p,AID),0)
        cog=StoryCog(SimpleNamespace())
        self.assertIsNotNone(cog._award_blader(101))
        self.assertIsNone(cog._award_blader(101))
        ctx=ActionCtx(target_id=101,invoker_id=999,text='Tyson',channel=SimpleNamespace(id=456))
        result=await _giveavatar(ctx)
        self.assertTrue(result.ok)
        p=await DB.get_user(101)
        self.assertEqual(len(p['avatar_rewards']),4)
        self.assertEqual(AC.spare_copies(p,AID),0)
        self.assertEqual(p['coins'],0)

    async def test_pack_purchase_retry_and_keep_do_not_refund(self):
        from cogs.avatar.avatar_shop import AvatarShop
        await DB.mutate_user(101,lambda p:p.update(coins=1000000))
        shop=AvatarShop(SimpleNamespace())
        ctx=SimpleNamespace(author=SimpleNamespace(id=101),message=SimpleNamespace(id=123),
                            channel=SimpleNamespace(id=456),send=AsyncMock())
        with patch('cogs.avatar.avatar_rewards.present',AsyncMock()):
            await shop.buy_pack.callback(shop,ctx,'common')
            await shop.buy_pack.callback(shop,ctx,'common')
        p=await DB.get_user(101)
        self.assertEqual(p['coins'],925000)
        self.assertEqual(len(p['avatar_rewards']),1)
        reward=next(iter(p['avatar_rewards'].values()))
        await DB.mutate_user(101,lambda p:decide(p,reward['id'],'keep'))
        self.assertEqual((await DB.get_user(101))['coins'],925000)

    async def test_all_15_star_details_and_pending_controls(self):
        p=await DB.get_user(101)
        card=avatar_engine.get_avatar(AID)
        for star in range(1,16):
            AP._ensure(p,AID)['stars']=star
            view=ProgressionView(101,card,p)
            self.assertIn(f'{star}★',progression_embed(p,card).title)
            if star>=8:
                self.assertTrue(view.star_up.disabled)
                self.assertTrue(view.feed_stage.disabled)
            self.assertEqual(len([c for c in view.children if hasattr(c,'label')]),6)
        p['avatar_inventory']=[]
        view.configure(p)
        self.assertTrue(view.closed)
        self.assertTrue(all(c.disabled for c in view.children))


class BattleV4Tests(unittest.IsolatedAsyncioTestCase):
    async def build(self, mode, level, star):
        from tools import sim_story as H
        from cogs.battle.boss import boss_battle
        from cogs.story.story_match import NPCFighter
        from cogs.story.story_ai import LeagueOpponent
        avatar_engine.load()
        p=H.seed_profile(101, blade='Void Longinus', equipped_avatar=AID,
                         avatar_inventory=[AID], avatar_energy=100)
        AC.migrate(p)
        AP._ensure(p,AID).update(level=level,stars=star)
        H.STORE.data['101']=copy.deepcopy(p)
        H.seed_profile(102)
        blade=H.get_beyblade('Void Longinus')
        if mode=='boss':
            return (await boss_battle._player_fighter(101))[0]
        player=H.FakePlayer(101,'Player')
        enemy=H.FakePlayer(102,'Opponent')
        kwargs={}
        if mode=='story':
            enemy=NPCFighter(1,blade['name'])
            kwargs={'npc_controller':LeagueOpponent(enemy,blade,difficulty='elite',level=1,avatar_id=None),
                    'spend_energy':True}
        return await H.BattleSession.create(None,H.FakeChannel(),player,enemy,
            blade,copy.deepcopy(blade),ranked=False,payout=False,**kwargs)

    async def test_star_and_level_apply_once_in_pvp_story_and_boss(self):
        for mode in ('pvp','story','boss'):
            with self.subTest(mode=mode):
                base=await self.build(mode,1,1)
                progressed=await self.build(mode,5,8)
                b0=base.avatar_bonuses if mode=='boss' else base.avatar_bonuses['101']
                b1=progressed.avatar_bonuses if mode=='boss' else progressed.avatar_bonuses['101']
                self.assertEqual(b1.attack_flat-b0.attack_flat,288)
                self.assertEqual(b1.defence_flat-b0.defence_flat,264)
                self.assertEqual(b1.stamina_flat-b0.stamina_flat,264)
                if mode=='boss':
                    self.assertEqual(progressed.attack-base.attack,288)
                    self.assertEqual(progressed.defense-base.defense,264)
                    self.assertEqual(progressed.stamina_stat-base.stamina_stat,264)
                else:
                    for key,gain in [('attack',288),('defense',264),('stamina',264)]:
                        from cogs.battle.type_gimmicks import passive_stat_multiplier
                        expected = gain * passive_stat_multiplier(progressed.blades['101']['type'], key)
                        self.assertAlmostEqual(progressed.battle_stats['101'][key]-base.battle_stats['101'][key],expected)


if __name__ == '__main__':
    unittest.main()
