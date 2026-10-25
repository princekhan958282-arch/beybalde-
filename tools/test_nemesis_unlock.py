"""Only a real PvP win against official Void Longinus unlocks Nemesis."""
import copy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from utils.boss_unlocks import NEMESIS_UNLOCK_KEY, nemesis_unlocked, record_pvp_unlock
from utils.database import get_beyblade
from utils.userstore import UserStore
from utils.snapshot import SECTIONS
from cogs.battle.boss import boss_battle as bb, boss_info
from tools.test_october_bey_roster import build, P, E


class NemesisUnlock(unittest.IsolatedAsyncioTestCase):
    async def finish_battle(self, *, won=True, draw=False, opponent='Void Longinus', npc=False, payout=True):
        session, _ = build('Dranzer')
        session.blades[E] = get_beyblade(opponent)
        session.payout = payout
        session.hp[P] = 100 if won else 0
        session.hp[E] = 100 if not won or draw else 0
        if draw:
            session.hp[P] = 100
        if npc:
            session.npc_controller = SimpleNamespace(key=E, profile={})
        saved = {}
        async def save(uid, profile):
            saved[uid] = saved[str(uid)] = copy.deepcopy(profile)
        with patch('cogs.battle.session.update_user', side_effect=save), \
             patch('cogs.battle.session.get_user', AsyncMock(side_effect=lambda uid: copy.deepcopy(session._profile_for(uid)))), \
             patch('cogs.battle.session.grant_xp', return_value=(1, 0, False)), \
             patch.object(session, '_release_skills', AsyncMock()):
            await session._end_battle()
            await session._end_battle()  # Completion is idempotent.
        return saved

    async def test_pvp_win_records_unlock_only_on_winner(self):
        saved = await self.finish_battle()
        self.assertTrue(nemesis_unlocked(saved[P]))
        self.assertFalse(nemesis_unlocked(saved[E]))
        self.assertEqual(saved[P]['wins'], 1)

    async def test_losses_draws_other_beys_and_npc_rounds_do_not_unlock(self):
        for config in ({'won': False}, {'draw': True}, {'opponent': 'Dranzer'}, {'npc': True}, {'npc': True, 'payout': False}):
            with self.subTest(config=config):
                saved = await self.finish_battle(**config)
                self.assertFalse(any(nemesis_unlocked(p) for p in saved.values()))

    async def test_equipping_void_or_old_boss_clears_do_not_unlock(self):
        cog = bb.BossCog(SimpleNamespace())
        profile = {'inventory': ['Void Longinus'], 'active_beyblade': 'Void Longinus',
                   'bosses_cleared': ['drakos', 'nemesis']}
        with patch.object(bb, 'get_user', AsyncMock(return_value=profile)), \
             patch.object(bb.bcopy, 'has_equipped_blade', AsyncMock(return_value=True)):
            ok, message = await cog._can_fight(1, 'nemesis')
            self.assertFalse(ok)
            self.assertIn('Void Longinus', message)
            self.assertIn('PvP', message)
            self.assertNotIn('Drakos', message)
            profile[NEMESIS_UNLOCK_KEY] = True
            self.assertTrue((await cog._can_fight(1, 'nemesis'))[0])
            self.assertTrue((await cog._can_fight(1, 'drakos'))[0])

    def test_only_official_bey_identity_counts_and_progress_survives_restart(self):
        profile = {'inventory': []}
        record_pvp_unlock(profile, {'id': 'CUSTOM', 'name': 'Void Longinus'})
        self.assertFalse(nemesis_unlocked(profile))
        record_pvp_unlock(profile, get_beyblade('Void Longinus'))
        record_pvp_unlock(profile, get_beyblade('Dranzer'))
        self.assertTrue(nemesis_unlocked(profile))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = directory + '/users.db'
            store = UserStore(path, directory + '/users.json')
            store.put_one('1', profile)
            restarted = UserStore(path, directory + '/users.json')
            self.assertTrue(nemesis_unlocked(restarted.get_one('1')))
        self.assertIn(NEMESIS_UNLOCK_KEY, SECTIONS['story'])

    async def test_roster_and_spawn_show_the_requirement(self):
        cog = bb.BossCog(SimpleNamespace())
        ctx = SimpleNamespace(author=SimpleNamespace(id=1), send=AsyncMock())
        with patch.object(bb, 'get_user', AsyncMock(return_value={'bosses_cleared': []})):
            await bb.BossCog.bosses.callback(cog, ctx)
        field = next(f for f in ctx.send.call_args.kwargs['embed'].fields if 'NEMESIS' in f.name)
        self.assertIn('🔒', field.name)
        self.assertIn('Balance', field.value)
        self.assertIn('Void Longinus', field.value)
        self.assertIn('PvP', field.value)
        encounter = bb.BossEncounterView(cog, 1, 'nemesis')
        self.assertIn('Void Longinus', encounter.embed().fields[0].value)
        encounter.stop()
        info = boss_info.build_embed(boss_info.REGISTRY['nemesis'])
        self.assertTrue(any('Void Longinus' in f.value for f in info.fields))


if __name__ == '__main__':
    unittest.main()
