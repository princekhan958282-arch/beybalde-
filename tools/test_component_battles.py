"""Real battle/UI adapters with the existing in-memory Story harness.
Run separately: python tools/test_component_battles.py
"""
import asyncio
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import sim_story as H
from utils import database as DB
from utils.bey_components import equip, select_instance
from utils.character_registry import REGISTRY
from cogs.battle.battle import _apply_parts
from cogs.battle.boss import boss_battle as BB
from cogs.story.story_cog import player_blade
from cogs.battle.type_gimmicks import passive_stat_multiplier
from cogs.ui.inventory_ui import InventoryView
from cogs.economy.shop import ShopCog

class ComponentBattleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        H.seed_profile(701, 'Dranzer', inventory=['Dranzer', 'Dranzer'], parts=['Destroy Driver','Atomic Driver','Nexus Disk','Over Disk'])
        H.seed_profile(702, 'Draciel F', inventory=['Draciel F'])
        await DB.mutate_user(701, lambda p: equip(p, 'Destroy Driver'))
        await DB.mutate_user(701, lambda p: equip(p, 'Nexus Disk'))
        self.raw = DB.get_beyblade('Dranzer')
        self.enemy = DB.get_beyblade('Draciel F')
        self.p1, self.p2 = H.FakePlayer(701,'Player'), H.FakePlayer(702,'Enemy')

    async def pvp(self, ranked=False):
        p = await DB.get_user(701)
        blade = _apply_parts(copy.deepcopy(self.raw), p)
        return await H.BattleSession.create(None, H.FakeChannel(), self.p1, self.p2, blade, self.enemy, ranked=ranked, payout=False)

    def assert_stats(self, s):
        for stat in ('attack','defense','stamina'):
            bonus = 20 if stat == 'attack' else 0
            expected = (self.raw['stats'][stat] + bonus) * passive_stat_multiplier(self.raw['type'], stat)
            self.assertEqual(s.battle_stats['701'][stat], expected)

    async def test_pvp_and_ranked_tournament_session_use_build_once(self):
        self.assert_stats(await self.pvp())
        self.assert_stats(await self.pvp(ranked=True))

    async def test_active_session_does_not_follow_future_equipment_changes(self):
        s = await self.pvp()
        before = copy.deepcopy(s.battle_stats)
        snapshot = copy.deepcopy(s.blades['701']['component_snapshot'])
        await DB.mutate_user(701, lambda p: equip(p, 'Atomic Driver'))
        self.assertEqual(s.battle_stats, before)
        self.assertEqual(s.part_deltas['701']['attack'], 20)
        self.assertEqual(s.blades['701']['component_snapshot'], snapshot)
        later = await self.pvp()
        self.assertNotEqual(s.battle_stats, later.battle_stats)

    async def test_boss_uses_same_current_components(self):
        fighter, blade = await BB._player_fighter(701)
        self.assertEqual(fighter.attack, self.raw['stats']['attack'] + 20)
        self.assertEqual(fighter.defense, self.raw['stats']['defense'])
        self.assertEqual(fighter.stamina_stat, self.raw['stats']['stamina'])
        before = copy.deepcopy(blade)
        await DB.mutate_user(701, lambda p: equip(p, 'Atomic Driver'))
        self.assertEqual(blade, before)

    async def test_story_uses_build_and_npc_uses_no_player_parts(self):
        player, _ = await player_blade(701)
        npc_blade, gain = H.levelled('Draciel F')
        s, _, npc, _ = await H.build_session(self.p1, player, npc_blade, gain, 'elite', spend_energy=False)
        self.assert_stats(s)
        self.assertEqual(s.part_deltas[str(npc.id)], {})

    async def test_hp_contribution_reaches_pvp_and_boss_pools(self):
        baseline = await self.pvp()
        boss_base, _ = await BB._player_fighter(701)
        driver = REGISTRY.part('Destroy Driver')
        with patch.dict(driver['stats'], hp=13):
            boosted = await self.pvp()
            boss_boosted, _ = await BB._player_fighter(701)
        from utils.hp_system import max_hp_for_blade
        import math
        passive = passive_stat_multiplier(self.raw['type'], 'hp')
        self.assertEqual(boosted.hp['701'], math.floor((max_hp_for_blade(self.raw) + 13) * passive))
        self.assertEqual(boss_boosted.hp - boss_base.hp, 13)

    async def test_inventory_buttons_select_exact_copy_and_validate_parts(self):
        view = InventoryView(self.p1, self.p1)
        # The actual view caches/IDs drive the actual inventory button callbacks.
        await view._load_cache()
        self.assertEqual(len({i['instance_id'] for i in view._cache['bey']}), 2)
        second = view._cache['bey'][1]
        await DB.mutate_user(701, lambda p: select_instance(p, second['instance_id']))
        with self.assertRaisesRegex(ValueError, 'another Bey copy'):
            await view._toggle_part('Destroy Driver')
        await view._toggle_part('Atomic Driver')
        await view._load_cache()
        self.assertFalse(view._cache['bey'][0]['equipped'])
        self.assertTrue(view._cache['bey'][1]['equipped'])
        await view._toggle_part('Atomic Driver')
        self.assertNotIn('Atomic Driver', (await DB.get_user(701))['equipped_parts'])

    async def test_equippart_command_replaces_part_through_atomic_adapter(self):
        ctx = SimpleNamespace(author=self.p1, send=AsyncMock())
        shop = ShopCog(None)
        await ShopCog.equippart.callback(shop,ctx,part_name='Atomic Driver')
        p = await DB.get_user(701)
        self.assertEqual(set(p['equipped_parts']), {'Nexus Disk','Atomic Driver'})
        self.assertIn('equipped', ctx.send.call_args.args[0])
        await ShopCog.unequippart.callback(shop,ctx,part_name='Atomic Driver')
        self.assertEqual((await DB.get_user(701))['equipped_parts'], ['Nexus Disk'])

if __name__=='__main__': unittest.main()
