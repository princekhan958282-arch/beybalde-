"""Random encounter lifecycle, fixed IQ, original inventory and reward retries."""
import asyncio
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.battle.boss import boss_battle as bb, boss_originals as originals
from cogs.battle.boss import boss_ai as ai, boss_info
from cogs.spawn.spawn import _pick_random_beyblade, _roll_hidden_spawn
from utils.database import get_beyblade
from utils.character_registry import load_beys
from utils.bey_components import assemble
from tools import test_boss_flow as flow
from tools.test_boss_flow import member
from tools.test_october_bey_roster import build, P, E


def interaction(uid=1):
    i = MagicMock()
    i.user = member(uid)
    i.response.defer = AsyncMock()
    i.response.send_message = AsyncMock()
    i.response.is_done.return_value = True
    i.followup.send = AsyncMock()
    i.original_response = AsyncMock()
    i.edit_original_response = AsyncMock()
    return i


class Originals(unittest.TestCase):
    def test_definitions_full_stats_mechanics_and_component_totals(self):
        for key, prof in boss_info.REGISTRY.items():
            blade = get_beyblade(prof['name'])
            self.assertEqual(blade['boss_source'], key)
            self.assertTrue(blade['boss_only'])
            for stat in ('attack', 'defense', 'stamina'):
                self.assertEqual(blade['stats'][stat], prof[stat])
            self.assertEqual([a['name'] for a in blade['abilities']], [a['name'] for a in prof['abilities']])
            self.assertTrue(all(a.get('rules') for a in blade['abilities']))
            self.assertIsNotNone(blade['special_move'])
            profile = {'inventory': [blade['name']], 'active_beyblade': blade['name']}
            resolved, _ = assemble(profile, blade)
            self.assertEqual(resolved['stats'], blade['stats'])

    def test_originals_never_enter_wild_or_hidden_spawn_pools(self):
        blades = {b['name']: b for b in load_beys().values() if b.get('boss_only')}
        self.assertIsNone(_pick_random_beyblade(blades))
        for blade in blades.values():
            blade['hidden_drop_one_in'] = 1
        self.assertIsNone(_roll_hidden_spawn(blades))

    def test_reward_and_pending_collection_preserve_legacy_copies(self):
        old = [{'id': 'old-copy'}]
        profile = {'inventory': [], 'boss_copies': copy.deepcopy(old)}
        name, queued = originals.grant_original(profile, 'drakos')
        self.assertFalse(queued)
        self.assertEqual(profile['inventory'], [name])
        self.assertEqual(profile['boss_copies'], old)
        self.assertEqual(len(profile['part_instances']), 2)
        profile['inventory'] = [name] * 200
        reward, queued = originals.grant_original(profile, 'argus')
        self.assertTrue(queued)
        self.assertEqual(originals.claim_pending(profile), ([], 1))
        profile['inventory'].pop()
        self.assertEqual(originals.claim_pending(profile), ([reward], 0))
        self.assertEqual(originals.claim_pending(profile), ([], 0))
        self.assertEqual(profile['boss_copies'], old)

    def test_argus_and_lionheart_reward_abilities_execute(self):
        s, _ = build('Dranzer')
        blade = get_beyblade(boss_info.REGISTRY['argus']['name'])
        s.blades[P] = blade
        s.ability._fire('passive', P, E, blade, 'attack', 'win', 100, 0, [])
        self.assertEqual(s.ability.counters[(P, 'argus_eyes')], 1)
        self.assertGreater(s.status.get_buff_bonus(P, 'attack'), 0)
        self.assertEqual(s.ability.crit_chance_bonus[P], .1)
        s.hp[P] = 500
        s.ability._fire('passive', P, E, blade, 'attack', 'win', 100, 0, [])
        self.assertEqual(s.status.invulnerable_turns[P], 5)
        s.status.invulnerable_turns[P] = 0
        s.ability._fire('passive', P, E, blade, 'attack', 'win', 100, 0, [])
        self.assertEqual(s.status.invulnerable_turns[P], 0)  # Once per battle.
        s, _ = build('Dranzer')
        blade = get_beyblade(boss_info.REGISTRY['lionheart']['name'])
        s.blades[P] = blade
        dealt, taken = s.ability._fire('on_take_damage', P, E, blade, 'attack', 'loss', 100, 0, [])
        self.assertEqual((dealt, taken), (94, 45))
        self.assertEqual(s.ability.counters[(P, 'lionheart_reprisal')], 22)
        dealt, _ = s.ability._fire('on_special', P, E, blade, 'special', 'win', 100, 0, [])
        self.assertEqual(dealt, 122)
        self.assertEqual(s.ability.counters[(P, 'lionheart_reprisal')], 0)
        s.hp[P] = 500
        s.ability._fire('passive', P, E, blade, 'attack', 'win', 0, 0, [])
        self.assertEqual(s.hp[P], 700)


class Encounters(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_iq_at_all_bosses_and_tiers(self):
        for key in bb.BOSSES:
            for tier in bb.btiers.ORDERED:
                fight = flow.BossFlowTests().make_fight()
                fight.key, fight.cfg, fight.tier = key, bb.BOSSES[key], tier
                with patch.object(ai, 'choose_move', return_value=(ai.MOVE_ATTACK, {})) as choose:
                    fight.step(ai.MOVE_ATTACK)
                self.assertEqual(ai.iq_for(choose.call_args.kwargs['difficulty']), 2)
                self.assertEqual(ai.iq_for(fight.card_state()['difficulty']), 2)
                self.assertIn('IQ 2', dict(bb.lobby_card_state(key, tier=tier)['stats'])['Boss AI'])

    async def test_probability_one_per_guild_and_send_failure_cleanup(self):
        cog = bb.BossCog(SimpleNamespace())
        channel = SimpleNamespace(guild=SimpleNamespace(id=10), send=AsyncMock())
        with patch.object(bb.random, 'random', return_value=0):
            await asyncio.gather(cog.on_beycord_spawn_cycle(channel), cog.on_beycord_spawn_cycle(channel))
        self.assertEqual(channel.send.await_count, 1)
        view = cog._encounters[10]
        view.stop()
        cog._encounters.clear()
        channel.send.side_effect = RuntimeError('send failed')
        with patch.object(bb.random, 'random', return_value=0), self.assertLogs(bb.log, level='ERROR'):
            await cog.on_beycord_spawn_cycle(channel)
        self.assertFalse(cog._encounters)
        channel.send.reset_mock(side_effect=True)
        with patch.object(bb.random, 'random', return_value=1):
            await cog.on_beycord_spawn_cycle(channel)
        channel.send.assert_not_awaited()

    async def test_first_challenger_wins_and_ineligible_click_does_not_consume(self):
        cog = bb.BossCog(SimpleNamespace())
        view = bb.BossEncounterView(cog, 10, 'drakos')
        cog._encounters[10] = view
        i1, i2 = interaction(1), interaction(2)
        with patch.object(cog, '_can_fight', AsyncMock(return_value=(False, 'Equip a Bey'))):
            await view.challenge.callback(i1)
        self.assertIs(cog._encounters[10], view)
        self.assertFalse(view.claimed)
        with patch.object(cog, '_can_fight', AsyncMock(return_value=(True, ''))), \
             patch.object(bb.BossLobbyView, 'refresh', AsyncMock()):
            await asyncio.gather(view.challenge.callback(i1), view.challenge.callback(i2))
        self.assertTrue(view.claimed)
        self.assertNotIn(10, cog._encounters)
        self.assertEqual(len(cog._active), 1)

    async def test_failed_lobby_creation_releases_player_and_keeps_encounter(self):
        cog = bb.BossCog(SimpleNamespace())
        view = bb.BossEncounterView(cog, 10, 'drakos')
        cog._encounters[10] = view
        with patch.object(cog, '_can_fight', AsyncMock(return_value=(True, ''))), \
             patch.object(bb.BossLobbyView, 'refresh', AsyncMock(side_effect=RuntimeError('edit failed'))):
            with self.assertRaises(RuntimeError):
                await view.challenge.callback(interaction())
        self.assertFalse(cog._active)
        self.assertIs(cog._encounters[10], view)
        view.stop()

    async def test_expiry_disables_challenge_and_rejects_late_click(self):
        cog = bb.BossCog(SimpleNamespace())
        view = bb.BossEncounterView(cog, 10, 'drakos')
        cog._encounters[10] = view
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertFalse(cog._encounters)
        self.assertTrue(view.children[0].disabled)
        i = interaction()
        await view.challenge.callback(i)
        i.response.send_message.assert_awaited_once()
        view.stop()

    async def test_coop_original_rewards_are_idempotent_and_never_roll_copies(self):
        fight = flow.BossFlowTests().make_fight()
        fight.result = 'win'
        profiles = {m.id: {'inventory': [], 'coins': 0, 'boss_copies': [{'id': 'legacy'}]} for m in fight.party}
        async def mutate(uid, fn):
            return fn(profiles[uid])
        cog = bb.BossCog(SimpleNamespace())
        channel = SimpleNamespace(send=AsyncMock())
        with patch.object(bb, 'mutate_user', side_effect=mutate), \
             patch.object(bb, 'grant_xp'), \
             patch.object(bb.casino_wallet, 'credit', AsyncMock()), \
             patch.object(bb.gemini, 'say_with_deadline', AsyncMock(return_value='Defeated')), \
             patch.object(bb.bcopy, 'roll_copy', side_effect=AssertionError('No copies')):
            await cog.finish(fight, channel)
            await cog.finish(fight, channel)
        for profile in profiles.values():
            self.assertEqual(profile['inventory'], [boss_info.REGISTRY['drakos']['name']])
            self.assertEqual(profile['boss_copies'], [{'id': 'legacy'}])
            self.assertEqual(len(profile['boss_reward_receipts']), 1)

    async def test_old_manual_commands_and_aliases_are_removed(self):
        cog = bb.BossCog(SimpleNamespace())
        names = {n for c in cog.get_commands() for n in [c.name, *c.aliases]}
        self.assertTrue({'boss', 'bossfight', 'raidboss'}.isdisjoint(names))
        self.assertTrue({'bosses', 'bossinfo', 'bossrewards', 'copies'}.issubset(names))


if __name__ == '__main__':
    unittest.main()
