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
from utils.bey_components import equip, select_instance, assemble
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
            bonus = (20 if stat == 'attack' else 0) - sum(REGISTRY.part(self.raw['default_parts'][slot])['stats'][stat] for slot in ('disk','driver'))
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
        self.assertEqual(s.part_deltas['701']['attack'], 20 - sum(REGISTRY.part(self.raw['default_parts'][slot])['stats']['attack'] for slot in ('disk','driver')))
        self.assertEqual(s.blades['701']['component_snapshot'], snapshot)
        later = await self.pvp()
        self.assertNotEqual(s.battle_stats, later.battle_stats)

    async def test_boss_uses_same_current_components(self):
        fighter, blade = await BB._player_fighter(701)
        self.assertEqual(fighter.attack, self.raw['main_frame']['attack'] + 20)
        self.assertEqual(fighter.defense, self.raw['main_frame']['defense'])
        self.assertEqual(fighter.stamina_stat, self.raw['main_frame']['stamina'])
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
        stock_hp = sum(REGISTRY.part(self.raw['default_parts'][slot])['stats']['hp'] for slot in ('disk','driver'))
        with patch.dict(driver['stats'], hp=stock_hp+13):
            boosted = await self.pvp()
            boss_boosted, _ = await BB._player_fighter(701)
        from utils.hp_system import max_hp_for_blade
        import math
        passive = passive_stat_multiplier(self.raw['type'], 'hp')
        self.assertEqual(boosted.hp['701'], math.floor((max_hp_for_blade(self.raw) + 13 * 15) * passive))
        mult = await DB.get_stat_multiplier(701, self.raw['name'])
        self.assertEqual(boss_boosted.hp, math.floor((BB.BASE_PLAYER_HP + 13) * mult * passive))
        self.assertEqual(boss_base.hp, math.floor((BB.BASE_PLAYER_HP - stock_hp) * mult * passive))
        self.assertLess(baseline.hp['701'], boosted.hp['701'])

    async def test_level_growth_and_equipment_replacement_are_applied_once(self):
        from utils import bey_levels as BL
        await DB.mutate_user(701, lambda p:p.update(bey_progress={'Dranzer':{'xp':BL.xp_for_level(30),'ivs':{}}}))
        p=await DB.get_user(701)
        expected=BL.stats_at(self.raw,30,{})
        delta=assemble(p,self.raw)[1]
        session=await self.pvp()
        for stat in ('attack','defense','stamina'):
            self.assertEqual(session.battle_stats['701'][stat],(expected[stat]+delta[stat])*passive_stat_multiplier(self.raw['type'],stat))

    async def test_inventory_buttons_select_exact_copy_and_validate_parts(self):
        view = InventoryView(self.p1, self.p1)
        # The actual view caches/IDs drive the actual inventory button callbacks.
        await view._load_cache()
        self.assertEqual(len({i['instance_id'] for i in view._cache['bey']}), 2)
        second = view._cache['bey'][1]
        await DB.mutate_user(701, lambda p: select_instance(p, second['instance_id']))
        before = await DB.get_user(701)
        first_driver = before['bey_instances'][0]['parts']['driver']
        second_driver = before['bey_instances'][1]['parts']['driver']
        await view._toggle_part('Destroy Driver')
        after = await DB.get_user(701)
        self.assertEqual(after['bey_instances'][0]['parts']['driver'], second_driver)
        self.assertEqual(after['bey_instances'][1]['parts']['driver'], first_driver)
        self.assertEqual(after['part_instances'], before['part_instances'])
        await view._toggle_part('Atomic Driver')
        await view._load_cache()
        self.assertFalse(view._cache['bey'][0]['equipped'])
        self.assertTrue(view._cache['bey'][1]['equipped'])
        await view._toggle_part('Atomic Driver')
        self.assertIn('Atomic Driver', (await DB.get_user(701))['equipped_parts'])

    async def test_equippart_command_replaces_part_through_atomic_adapter(self):
        ctx = SimpleNamespace(author=self.p1, send=AsyncMock())
        shop = ShopCog(None)
        await ShopCog.equippart.callback(shop,ctx,part_name='Atomic Driver')
        p = await DB.get_user(701)
        self.assertEqual(set(p['equipped_parts']), {'Nexus Disk','Atomic Driver'})
        self.assertIn('equipped', ctx.send.call_args.args[0])
        await ShopCog.unequippart.callback(shop,ctx,part_name='Atomic Driver')
        self.assertEqual(set((await DB.get_user(701))['equipped_parts']), {'Nexus Disk', REGISTRY.part(self.raw['default_parts']['driver'])['name']})

    async def test_level_and_parts_hp_convert_once_without_a_base_pool(self):
        from utils import bey_levels as BL
        from utils.loadout import effective_blade
        import math
        await DB.mutate_user(701, lambda p:p.update(bey_progress={'Dranzer':{'xp':BL.xp_for_level(30),'ivs':{}}}))
        effective, _, _ = await effective_blade(701, blade=self.raw, include_avatar=False)
        s = await self.pvp()
        expected = math.floor(effective['stats']['hp'] * 15 * passive_stat_multiplier(self.raw['type'], 'hp'))
        self.assertEqual(s.hp['701'], expected)
        self.assertEqual(s.max_hp_per_player['701'], expected)

    async def test_story_npc_levelled_hp_is_not_added_twice(self):
        from utils.hp_system import max_hp_for_blade
        import math
        player, _ = await player_blade(701)
        npc_blade, gain = H.levelled('Draciel F')
        self.assertGreater(gain, 0)
        s, _, npc, _ = await H.build_session(self.p1, player, npc_blade, gain, 'elite', spend_energy=False)
        pool = int(s.avatar_bonuses[str(npc.id)].apply_hp_bonus(max_hp_for_blade(npc_blade)))
        expected = math.floor(pool * passive_stat_multiplier(npc_blade['type'], 'hp'))
        self.assertEqual(s.hp[str(npc.id)], expected)

    async def test_avatar_hp_applies_once_after_conversion_and_card_matches(self):
        from cogs.avatar import avatar_engine, AvatarBonuses
        from utils.loadout import battle_pool, effective_blade
        import math
        bonus = AvatarBonuses(hp_flat=50, hp_percent=0.10)
        effective, _, _ = await effective_blade(701, blade=self.raw, include_avatar=False)
        with patch.object(avatar_engine, 'get_battle_bonuses', new=AsyncMock(return_value=bonus)):
            s = await self.pvp()
            displayed = await battle_pool(701, self.raw)
        expected = math.floor(int((effective['stats']['hp'] * 15 + 50) * 1.10)
                              * passive_stat_multiplier(self.raw['type'], 'hp'))
        self.assertEqual(s.hp['701'], expected)
        self.assertEqual(displayed, expected)

    async def test_owned_card_does_not_apply_level_and_parts_twice(self):
        from utils import bey_levels as BL
        from utils.loadout import effective_blade, battle_pool
        await DB.mutate_user(701, lambda p:p.update(bey_progress={'Dranzer':{'xp':BL.xp_for_level(30),'ivs':{}}}))
        card, _, _ = await effective_blade(701, blade=self.raw)
        session = await self.pvp()
        self.assertEqual(await battle_pool(701, card), session.max_hp_per_player['701'])


class HpConversionTests(unittest.TestCase):
    def test_conversion_has_no_type_band_clamp_or_base_hp(self):
        from utils.hp_system import max_hp_for_blade
        for stat in (1, 80, 100, 139, 500):
            with self.subTest(hp=stat):
                self.assertEqual(max_hp_for_blade({'type':'Attack', 'stats':{'hp':stat}}), stat * 15)

    def test_missing_invalid_and_zero_hp_are_safe(self):
        from utils.hp_system import max_hp_for_blade
        self.assertEqual(max_hp_for_blade(None), 1500)
        self.assertEqual(max_hp_for_blade({'stats':{'hp':0}}), 1)
        for invalid in ('bad', float('nan'), float('inf')):
            self.assertGreater(max_hp_for_blade({'stats':{'hp':invalid}}), 0)

    def test_missing_special_scales_with_blades_hp_pool(self):
        from cogs.battle.damage_rules import resolve_special
        self.assertEqual(resolve_special({'stats':{'hp':100}})[1], 900)
        self.assertEqual(resolve_special({'stats':{'hp':200}})[1], 1800)

    def test_chain_healing_uses_fighters_actual_maximum(self):
        from cogs.battle.chain_handler import ChainHandler
        from cogs.battle.status_manager import StatusManager
        for maximum in (750, 6000):
            blade = {'name':'Test', 'stats':{'hp':maximum//15}}
            session = SimpleNamespace(hp={'a':maximum-10, 'b':100},
                                      max_hp_per_player={'a':maximum})
            session.status = StatusManager(session)
            # The chain's shared healing adapter does not need a battle engine
            # to verify the percentage and cap calculation.
            with patch('cogs.battle.purification.heal_amount', side_effect=lambda s,k,n:n):
                chain = ChainHandler(session)
                chain._apply_effect({'effect':'heal_pct','value':0.1},'a','b',blade,'Test',0)
            self.assertEqual(session.hp['a'], maximum)


if __name__=='__main__': unittest.main()
