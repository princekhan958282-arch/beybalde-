"""Admin gift configuration survives redemption, recovery and Keep/Sell."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from tools import sim_story as H
from cogs.codes import redeem as R
from cogs.codes.builder import _AvatarModal
from cogs.avatar import avatar_engine, avatar_progress as AP, avatar_collection as AC
from cogs.avatar.avatar_rewards import enqueue, decide, reward_embed


class AvatarGiftTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        avatar_engine.load()
        self.card = avatar_engine.get_avatar('avatar_og_tyson')
        self.p = AC.migrate({'coins': 100, 'equipped_avatar': None}, [])

    async def test_parser_and_preview_support_star_and_level(self):
        rewards, error = R.parse_rewards('avatar:Tyson@5#15')
        self.assertIsNone(error)
        self.assertEqual(rewards[0]['stars'], 15)
        self.assertEqual(rewards[0]['level'], 5)
        self.assertIn('Lv.5 · 15★', R.describe(rewards))
        legacy, error = R.parse_rewards('avatar:Tyson')
        self.assertIsNone(error)
        self.assertEqual((legacy[0]['level'], legacy[0]['stars']), (1, 1))

    async def test_invalid_gift_progression_rejected(self):
        for spec in ('avatar:Tyson@0#2', 'avatar:Tyson@6#2', 'avatar:Tyson@5#16',
                     'avatar:Tyson@5#0', 'avatar:Tyson@x#5'):
            self.assertIsNotNone(R.parse_rewards(spec)[1], spec)
        for kwargs in ({'level': 6}, {'stars': 16}, {'level': True}):
            with self.assertRaises(AP.PurchaseError):
                enqueue(self.p, self.card['id'], **kwargs)

    async def test_modal_adds_progression_and_refreshes_original_panel(self):
        view = SimpleNamespace(guard=AsyncMock(return_value=True), refresh=AsyncMock(), rewards=[])
        modal = _AvatarModal(view)
        modal.name._value, modal.level._value, modal.stars._value = 'Tyson', '5', '8'
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await modal.on_submit(interaction)
        self.assertEqual(view.rewards, R.parse_rewards('avatar:Tyson@5#8')[0])
        view.refresh.assert_awaited_once_with(interaction)
        view.guard.return_value = False
        await modal.on_submit(interaction)
        self.assertEqual(len(view.rewards), 1)

    async def test_redeem_persists_progression_until_keep(self):
        H.seed_profile(777, avatar_inventory=[])
        rewards, _ = R.parse_rewards('avatar:Tyson@5#8')
        await R.grant(777, rewards, source='testgift', channel_id=88)
        p = await H.DB.get_user(777)
        reward = p['avatar_rewards']['code:testgift:0']
        self.assertEqual((reward['level'], reward['stars']), (5, 8))
        self.assertNotIn(self.card['id'], p['avatar_inventory'])
        self.assertIn('Lv.5 · 8★', reward_embed(p, reward).description)
        await H.DB.mutate_user(777, lambda profile: decide(profile, reward['id'], 'keep'))
        p = await H.DB.get_user(777)
        self.assertEqual(AP.card_level(p, self.card['id']), 5)
        self.assertEqual(AC.stars(p, self.card['id']), 8)
        await R.grant(777, rewards, source='testgift', channel_id=88)
        self.assertEqual(AC.spare_copies(await H.DB.get_user(777), self.card['id']), 0)

    async def test_duplicate_keeps_higher_progress_and_skills(self):
        aid = self.card['id']
        AC.grant(self.p, aid)
        AP._ensure(self.p, aid).update(level=5, stars=10, skills={'storm-burst': 7}, feeding=12)
        r = enqueue(self.p, aid, level=3, stars=8, now=100)
        decide(self.p, r['id'], 'keep', now=101)
        self.assertEqual((AP.card_level(self.p, aid), AC.stars(self.p, aid)), (5, 10))
        self.assertEqual(AP.card_entry(self.p, aid)['skills'], {'storm-burst': 7})
        self.assertEqual(AP.card_entry(self.p, aid)['feeding'], 12)
        self.assertEqual(AC.spare_copies(self.p, aid), 1)
        r = enqueue(self.p, aid, level=5, stars=15, now=100)
        decide(self.p, r['id'], 'keep', now=101)
        self.assertEqual(AC.stars(self.p, aid), 15)

    async def test_sell_never_grants_progression_and_is_idempotent(self):
        r = enqueue(self.p, self.card['id'], level=5, stars=15, sell_value=20, now=100)
        decide(self.p, r['id'], 'sell', now=101)
        decide(self.p, r['id'], 'keep', now=101)
        self.assertEqual(self.p['coins'], 120)
        self.assertNotIn(self.card['id'], self.p['avatar_inventory'])

    async def test_timeout_and_legacy_rewards(self):
        r = enqueue(self.p, self.card['id'], level=5, stars=8, now=100)
        decide(self.p, r['id'], 'sell', now=r['expires_at'])
        self.assertEqual(AC.stars(self.p, self.card['id']), 8)
        legacy = enqueue(self.p, self.card['id'], now=100)
        self.p['avatar_rewards'][legacy['id']].pop('level')
        self.p['avatar_rewards'][legacy['id']].pop('stars')
        decide(self.p, legacy['id'], 'keep', now=101)
        self.assertEqual(AC.stars(self.p, self.card['id']), 8)


if __name__ == '__main__':
    unittest.main()
