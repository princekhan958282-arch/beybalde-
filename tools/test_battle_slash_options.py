"""Slash options and match-local normalized stats/disabled avatars."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from tools import sim_story as H
import discord
from discord.ext import commands
from cogs.battle.battle import BattleCog
from cogs.ui.panels import PanelCommands
from cogs.avatar import avatar_engine
from cogs.avatar import avatar_skills as AS
from utils import bey_levels as BL
from utils.loadout import bey_level_and_stats
from utils.hp_system import max_hp_for_blade
from cogs.battle.type_gimmicks import passive_stat_multiplier


class BattleOptions(unittest.IsolatedAsyncioTestCase):
    async def build(self, level=None, avatars=True):
        avatar_engine.load()
        self.blade = H.get_beyblade('Dranzer')
        self.profile = H.seed_profile(101, blade='Dranzer', equipped_avatar='avatar_og_tyson',
            avatar_inventory=['avatar_og_tyson'], avatar_energy=77,
            bey_progress={'Dranzer': {'xp': BL.xp_for_level(70), 'ivs': {}}})
        H.seed_profile(102, blade='Dranzer', equipped_avatar='avatar_x003',
            avatar_inventory=['avatar_x003'])
        await H.DB.get_user(101)
        await H.DB.get_user(102)
        self.before = copy.deepcopy(H.STORE.data)
        return await H.BattleSession.create(None, H.FakeChannel(), H.FakePlayer(101, 'A'),
            H.FakePlayer(102, 'B'), self.blade, copy.deepcopy(self.blade),
            payout=False, bey_level=level, avatars_enabled=avatars)

    async def test_slash_forwards_options(self):
        cog = BattleCog(SimpleNamespace())
        interaction, player = object(), object()
        with patch('cogs.ui.invoke.run_prefix_command', new=AsyncMock()) as run:
            await BattleCog.battle_challenge.callback(cog, interaction, player, 50, False)
        run.assert_awaited_once_with(interaction, cog.battle, bot=cog.bot,
                                    visibility='public', opponent=player,
                                    mode='casual', bey_level=50, avatars=False)
        options = {p.name: p for p in BattleCog.battle_challenge.parameters}
        self.assertEqual(options['bey_level'].min_value, 1)
        self.assertEqual(options['bey_level'].max_value, 100)

    async def test_battle_registers_without_panels_and_has_no_duplicate(self):
        async with commands.Bot(command_prefix=';', intents=discord.Intents.none()) as bot:
            await bot.add_cog(BattleCog(bot))
            self.assertIsNotNone(bot.tree.get_command('battle'))
            await bot.add_cog(PanelCommands(bot))
            self.assertEqual(sum(c.name == 'battle' for c in bot.tree.get_commands()), 1)

    async def test_fixed_levels_apply_stats_hp_special_and_formula_level(self):
        for level in (1, 50, 100):
            session = await self.build(level, False)
            self.assertEqual(session.bey_levels, {'101': level, '102': level})
            for pid in ('101', '102'):
                snapshot = copy.deepcopy(self.before[pid])
                snapshot.setdefault('bey_progress', {}).setdefault('Dranzer', {})['xp'] = BL.xp_for_level(level)
                _, stats = bey_level_and_stats(snapshot, self.blade)
                for stat in ('attack', 'defense', 'stamina'):
                    self.assertAlmostEqual(session.battle_stats[pid][stat],
                        stats[stat] * passive_stat_multiplier(self.blade['type'], stat))
                expected_blade = copy.deepcopy(self.blade)
                expected_blade['stats'] = stats
                expected_blade['level'] = level
                self.assertEqual(session.max_hp_per_player[pid],
                    int(max_hp_for_blade(expected_blade) * passive_stat_multiplier(self.blade['type'], 'hp')))
                self.assertEqual(session.special_stats[pid], stats.get('special', 0))
            self.assertEqual(H.STORE.data, self.before)

    async def test_avatar_off_removes_all_effects_without_spending_energy(self):
        with patch.object(AS, 'begin_battle_for', new=AsyncMock()) as begin:
            session = await self.build(50, False)
            await session._release_skills()
        begin.assert_not_awaited()
        self.assertEqual(session.avatar_cards, {'101': {}, '102': {}})
        self.assertFalse(session.original_generation.states)
        self.assertTrue(all(not b.has_any_bonus for b in session.avatar_bonuses.values()))
        self.assertEqual(H.STORE.data, self.before)

    async def test_defaults_keep_real_levels_and_avatars(self):
        session = await self.build()
        self.assertEqual(session.bey_levels['101'], 70)
        self.assertEqual(session.bey_levels['102'], 1)
        self.assertTrue(session.avatar_cards['101'])
        self.assertTrue(session.avatar_bonuses['101'].has_any_bonus)

    async def test_invalid_fixed_level_rejected(self):
        for level in (0, 101, True):
            with self.assertRaises(ValueError):
                await self.build(level, False)


if __name__ == '__main__':
    unittest.main()
