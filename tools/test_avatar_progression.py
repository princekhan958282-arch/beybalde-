"""Run: python -m unittest tools.test_avatar_progression. No Discord login."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from concurrent.futures import ThreadPoolExecutor

from utils import database as DB
from utils.userstore import UserStore
from cogs.avatar import avatar_progress as AP, avatar_config as C
from cogs.avatar import avatar_collection as AC
from cogs.avatar.avatar_engine import avatar_engine
from cogs.avatar.avatar_scaling import scaled_card
from cogs.avatar.avatar_progression_ui import ProgressionView, StarConfirm

AID = 'avatar_og_tyson'


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.legacy = self.root / 'avatars.json'
        self.legacy.write_text(json.dumps({'101': [AID]}))
        self.store = UserStore(str(self.root / 'users.db'), str(self.root / 'users.json'))
        self.patches = [patch.object(DB, 'USER_STORE', self.store), patch.object(DB, 'AVATARS_PATH', str(self.legacy))]
        for p in self.patches: p.start()
        self.store.put_one('101', {'user_id': '101', 'coins': 1000000, 'equipped_avatar': AID})

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def mutate(self, fn):
        return DB._mutate_user_sync(101, fn)

    def read(self):
        return DB._get_user_sync(101)

    def seed_tier(self, stars, stages=0, copies=20):
        def seed(p):
            AP._ensure(p, AID).update(stars=stars, stages=stages)
            p['avatar_copies'][AID] = copies
        self.mutate(seed)

    def test_legacy_defaults_and_existing_levels_survive(self):
        p = self.read()
        self.assertEqual(p['avatar_inventory'], [AID])
        self.assertEqual((AP.card_level(p, AID), AC.stars(p, AID), AC.stages(p, AID)), (1, 1, 0))
        self.assertEqual(AP.skill_level(p, AID, 'storm'), 1)
        self.mutate(lambda p: AP._ensure(p, AID).update(level=4, skills={'storm': 6}))
        self.assertEqual(AP.card_level(self.read(), AID), 4)
        self.assertEqual(AP.skill_level(self.read(), AID, 'storm'), 6)

    def test_duplicate_copy_is_separate_from_owned_equipped_card(self):
        self.assertFalse(DB.add_avatar_to_inventory(101, AID))
        self.assertEqual(AC.spare_copies(self.read(), AID), 1)
        self.mutate(lambda p: AC.upgrade_star(p, AID, 2, Mock(side_effect=AssertionError('Safe upgrades never roll'))))
        self.assertEqual(DB.get_avatar_inventory(101), [AID])
        self.assertEqual(self.read()['equipped_avatar'], AID)
        self.assertEqual(AC.spare_copies(self.read(), AID), 0)

    def test_own_card_is_never_consumed_as_duplicate(self):
        with self.assertRaises(AP.PurchaseError):
            self.mutate(lambda p: AC.upgrade_star(p, AID, 2, Mock()))
        self.assertEqual(DB.get_avatar_inventory(101), [AID])

    def test_all_safe_tiers_and_stage_gate(self):
        self.seed_tier(1)
        roll = Mock(side_effect=AssertionError('Safe upgrade rolled'))
        for target in range(2, 6):
            self.assertTrue(self.mutate(lambda p: AC.upgrade_star(p, AID, target, roll))['success'])
        with self.assertRaisesRegex(AP.PurchaseError, 'five stages'):
            self.mutate(lambda p: AC.upgrade_star(p, AID, 6, roll))
        for stage in range(C.STAGE_COUNT):
            self.mutate(lambda p: AC.feed(p, AID, stage))
        self.assertEqual(AC.stages(self.read(), AID), 5)
        with self.assertRaises(AP.PurchaseError): self.mutate(lambda p: AC.feed(p, AID, 5))

    def test_success_at_six_and_seven_and_max_rejection(self):
        self.seed_tier(5, 5)
        for target in (6, 7):
            self.assertTrue(self.mutate(lambda p: AC.upgrade_star(p, AID, target, lambda: 0))['success'])
        with self.assertRaises(AP.PurchaseError): self.mutate(lambda p: AC.upgrade_star(p, AID, 8, lambda: 0))
        self.assertEqual(AC.stars(self.read(), AID), 7)

    def test_loss_is_durable_and_old_profile_cannot_resurrect(self):
        for source, target in ((5, 6), (6, 7)):
            DB.add_avatar_to_inventory(101, AID)
            self.seed_tier(source, 5)
            old = self.read()
            result = self.mutate(lambda p: AC.upgrade_star(p, AID, target, lambda: 1))
            self.assertFalse(result['success'])
            self.assertNotIn(AID, DB.get_avatar_inventory(101))
            p = self.read()
            self.assertIsNone(p['equipped_avatar'])
            self.assertNotIn(AID, p['avatar']['cards'])
            self.assertEqual(p['avatar_copies'][AID], 20 - C.STAR_COPY_COST[target])
            DB._update_user_sync(101, old)
            restarted = UserStore(str(self.root / 'users.db'), str(self.root / 'users.json'))
            with patch.object(DB, 'USER_STORE', restarted):
                self.assertNotIn(AID, DB.get_avatar_inventory(101))
                self.assertIsNone(self.read()['equipped_avatar'])

    def test_transaction_error_abandons_copy_consumption(self):
        self.seed_tier(5, 5)
        before = self.read()
        with self.assertRaises(RuntimeError):
            self.mutate(lambda p: AC.upgrade_star(p, AID, 6, Mock(side_effect=RuntimeError('roll error'))))
        after = self.read()
        self.assertEqual(before['avatar_copies'], after['avatar_copies'])
        self.assertEqual(before['avatar'], after['avatar'])

    def test_concurrent_same_attempt_rolls_once(self):
        self.seed_tier(5, 5)
        roll = Mock(return_value=0)
        def attempt():
            try: return self.mutate(lambda p: AC.upgrade_star(p, AID, 6, roll))
            except AP.PurchaseError: return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        roll.assert_called_once()
        self.assertEqual(AC.spare_copies(self.read(), AID), 15)

    def test_snapshot_restores_new_progress_and_counts_live_inventory(self):
        from utils import snapshot as SN
        self.seed_tier(5, 5)
        saved = self.read()
        snap = {'profiles': {'101': copy.deepcopy(saved)}, 'files': {}}
        self.mutate(lambda p: AC.upgrade_star(p, AID, 6, lambda: 1))
        loss = self.read()
        self.assertEqual(SN.describe({'profiles': {'101': loss}, 'files': {'avatars': {'101': [AID]}}})['avatars'], 0)
        asyncio.run(SN.restore(snap, sections=('avatars',)))
        self.assertEqual(AC.stars(self.read(), AID), 5)
        self.assertEqual(self.read()['avatar_copies'][AID], 20)
        self.mutate(lambda p: AC.upgrade_star(p, AID, 6, lambda: 1))
        asyncio.run(SN.restore(snap))
        self.assertIn(AID, DB.get_avatar_inventory(101))

    def test_level_and_star_stats_reach_battle_bonuses(self):
        avatar_engine.load()
        before = asyncio.run(avatar_engine.get_battle_bonuses(101))
        self.mutate(lambda p: AP.apply_card_purchase(p, AID))
        self.seed_tier(3)
        after = asyncio.run(avatar_engine.get_battle_bonuses(101))
        growth = C.GROWTH[avatar_engine.get_avatar(AID)['type']]
        self.assertEqual(after.attack_flat - before.attack_flat, growth['attack'] + C.STAR_STAT_GAIN['attack'] * 2)
        self.assertEqual(after.defence_flat - before.defence_flat, growth['defense'] + C.STAR_STAT_GAIN['defense'] * 2)

    def test_cost_max_and_ownership_revalidated(self):
        self.mutate(lambda p: AP.apply_card_purchase(p, AID))
        self.assertEqual(AP.card_level(self.read(), AID), 2)
        self.assertEqual(self.read()['coins'], 1000000 - C.CARD_LEVEL_COSTS[0])
        self.mutate(lambda p: p.update(coins=0))
        with self.assertRaises(AP.PurchaseError): self.mutate(lambda p: AP.apply_card_purchase(p, AID))
        self.seed_tier(5, 5)
        self.mutate(lambda p: AC.upgrade_star(p, AID, 6, lambda: 1))
        for fn in (AP.apply_card_purchase, AP.apply_reset):
            with self.assertRaises(AP.PurchaseError): self.mutate(lambda p: fn(p, AID))
        with self.assertRaises(ValueError): DB._set_equipped_avatar_sync(101, AID)


class ViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        avatar_engine.load()
        self.card = avatar_engine.get_avatar(AID)
        self.profile = AC.migrate({'coins': 1000000}, [AID])
        AP._ensure(self.profile, AID).update(stars=5, stages=5)
        self.profile['avatar_copies'][AID] = 10
        self.parent = ProgressionView(101, self.card, self.profile)
        self.parent.refresh_message = AsyncMock()
        self.interaction = SimpleNamespace(user=SimpleNamespace(id=101),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())

    async def test_owner_warning_cancel_and_timeout_never_roll(self):
        with patch('cogs.avatar.avatar_progression_ui.mutate_user', AsyncMock()) as mutate:
            v = StarConfirm(101, self.card, self.profile, self.parent)
            text = v.embed(self.profile).description
            self.assertIn('PERMANENTLY LOSE', text)
            self.assertIn('50%', text)
            self.assertEqual(v.timeout, 30)
            self.interaction.user.id = 102
            self.assertFalse(await v.interaction_check(self.interaction))
            self.interaction.user.id = 101
            await v.cancel.callback(self.interaction)
            v2 = StarConfirm(101, self.card, self.profile, self.parent)
            await v2.on_timeout()
            mutate.assert_not_awaited()
            self.assertTrue(all(b.disabled for b in v.children))
            self.assertTrue(v2.closed)
        AP._ensure(self.profile, AID)['stars'] = 6
        v = StarConfirm(101, self.card, self.profile, self.parent)
        self.assertIn('highest and riskiest', v.embed(self.profile).description)
        self.assertIn('25%', v.embed(self.profile).description)

    async def test_confirmation_disables_before_transaction_and_blocks_double_click(self):
        v = StarConfirm(101, self.card, self.profile, self.parent)
        async def mutate(uid, fn):
            self.assertTrue(v.closed)
            self.assertTrue(all(b.disabled for b in v.children))
            return fn(self.profile)
        with patch('cogs.avatar.avatar_progression_ui.mutate_user', side_effect=mutate) as call, patch('random.random', return_value=0):
            await v.confirm.callback(self.interaction)
            await v.confirm.callback(self.interaction)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(AC.stars(self.profile, AID), 6)

    async def test_disabled_buttons_respect_balance_caps_and_gate(self):
        AP._ensure(self.profile, AID).update(level=C.MAX_CARD_LEVEL, stages=4, skills={
            AP.slugify(s['name']): C.MAX_SKILL_LEVEL for s in self.card['skills']})
        self.profile['coins'] = 0
        self.parent.configure(self.profile)
        self.assertTrue(self.parent.level_up.disabled)
        self.assertTrue(self.parent.skill_up.disabled)
        self.assertTrue(self.parent.star_up.disabled)
        self.profile['avatar_copies'][AID] = 0
        self.parent.configure(self.profile)
        self.assertTrue(self.parent.feed_stage.disabled)

    async def test_level_skill_and_feed_buttons_save_and_refresh(self):
        self.interaction.response.defer = AsyncMock()
        async def mutate(uid, fn):
            return fn(self.profile)
        with patch('cogs.avatar.avatar_progression_ui.get_user', AsyncMock(return_value=self.profile)), \
             patch('cogs.avatar.avatar_progression_ui.mutate_user', side_effect=mutate):
            await self.parent.level_up.callback(self.interaction)
            self.assertEqual(AP.card_level(self.profile, AID), 2)
            self.parent.slot = 2
            await self.parent.skill_up.callback(self.interaction)
            slug = AP.slugify(self.card['skills'][1]['name'])
            self.assertEqual(AP.skill_level(self.profile, AID, slug), 2)
            AP._ensure(self.profile, AID)['stages'] = 0
            await self.parent.feed_stage.callback(self.interaction)
            self.assertEqual(AC.stages(self.profile, AID), 1)
        self.assertEqual(self.parent.refresh_message.await_count, 3)
        self.assertFalse(self.parent.busy)

    async def test_star_button_opens_confirmation_and_details_skills_work(self):
        self.interaction.original_response = AsyncMock()
        with patch('cogs.avatar.avatar_progression_ui.get_user', AsyncMock(return_value=self.profile)):
            await self.parent.star_up.callback(self.interaction)
        self.assertIsInstance(self.interaction.response.send_message.call_args.kwargs['view'], StarConfirm)
        await self.parent.show_details.callback(self.interaction)
        self.assertEqual(self.interaction.response.send_message.call_args.kwargs['embed'], self.parent.details_embed)
        await self.parent.show_skills.callback(self.interaction)
        self.assertEqual(len(self.interaction.response.send_message.call_args.kwargs['embed'].fields), 3)

    async def test_refresh_replaces_image_with_current_progress_and_clears_after_loss(self):
        from cogs.avatar.avatar_progression_ui import SkillSelect
        import io
        view = ProgressionView(101, self.card, self.profile)
        view.message = SimpleNamespace(edit=AsyncMock())
        AP._ensure(self.profile, AID).update(level=3, stars=4)
        with patch('cogs.avatar.avatar_progression_ui.get_user', AsyncMock(return_value=self.profile)), \
             patch('utils.avatar_info_card.resolve_avatar_image_url', AsyncMock(return_value=None)), \
             patch('utils.avatar_info_card.render_avatar_info_card', return_value=io.BytesIO(b'image')) as render:
            await view.refresh_message()
            self.assertEqual(render.call_args.kwargs['stars'], 4)
            self.assertEqual(render.call_args.kwargs['level'], 3)
            self.assertEqual(view.message.edit.call_args.kwargs['attachments'][0].filename, 'ainfo.jpg')
            self.assertIsNone(view.message.edit.call_args.kwargs['embed'])
            select = next(c for c in view.children if isinstance(c, SkillSelect))
            select._values = ['3']
            self.interaction.response.defer = AsyncMock()
            await select.callback(self.interaction)
            self.assertEqual(view.slot, 3)
            self.profile['avatar_inventory'].remove(AID)
            await view.refresh_message()
            self.assertEqual(view.message.edit.call_args.kwargs['attachments'], [])
            self.assertTrue(view.level_up.disabled)

    async def test_failed_profile_read_releases_busy_guard_and_timeout_disables_all(self):
        self.interaction.response.defer = AsyncMock()
        with patch('cogs.avatar.avatar_progression_ui.get_user', AsyncMock(side_effect=RuntimeError('db unavailable'))):
            with self.assertRaises(RuntimeError):
                await self.parent.level_up.callback(self.interaction)
        self.assertFalse(self.parent.busy)
        await self.parent.on_timeout()
        self.assertTrue(all(c.disabled for c in self.parent.children))

    async def test_bonus_and_rule_scaling_preserves_gates_and_shared_data(self):
        card = avatar_engine.get_avatar('avatar_s103')
        before = copy.deepcopy(card)
        aid = card['id']
        p = AC.migrate({}, [aid])
        AP._ensure(p, aid).update(level=5, skills={AP.slugify(s['name']): 10 for s in card['skills']})
        scaled = scaled_card(p, card)
        self.assertEqual(card, before)
        ops = [op for s in scaled['skills'] for r in s.get('rules', []) for op in r.get('do', [])]
        self.assertEqual(next(o['value'] for o in ops if o['op'] == 'add_counter'), 1)
        self.assertEqual(next(o['value'] for o in ops if o['op'] == 'prime_crit'), 1)
        self.assertGreater(next(o['value'] for o in ops if o['op'] == 'bonus_damage'), 22)


if __name__ == '__main__':
    unittest.main()
