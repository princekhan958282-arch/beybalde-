"""Physical component conservation, migration and transactions (isolated SQLite)."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from utils import database as DB
from utils.character_registry import CORE_STATS, REGISTRY, load_beys, load_parts
from utils.bey_components import (EquipmentError, assemble, reconcile, equip, owned_parts,
                                 select_instance, detach_bey, attach_bey, definition_for)
from utils.userstore import UserStore
from cogs.economy.shop import ShopCog, MarketplaceCog, PARTS_CATALOG, apply_part_purchase, PurchaseError
from cogs.extras.trade import TradeView


def profile(*names):
    return {'inventory': list(names), 'active_beyblade': names[0] if names else None,
            'parts': [], 'equipped_parts': [], 'coins': 100000, 'xp': 123,
            'bey_progress': {'Dranzer': {'level': 10, 'xp': 42}}}


def ids(p):
    return {i['instance_id'] for i in p['part_instances']}


def invariant(test, p):
    reconcile(p)
    assigned = [ident for e in p['bey_instances'] if REGISTRY.find_bey(e['name']) for ident in e['parts'].values()]
    test.assertEqual(len(assigned), len(set(assigned)))
    test.assertTrue(set(assigned) <= ids(p))
    for e in p['bey_instances']:
        if REGISTRY.find_bey(e['name']):
            test.assertEqual(set(e['parts']), {'disk', 'driver'})
            for slot, ident in e['parts'].items():
                test.assertEqual(definition_for(p, ident)['category'], slot)


class PhysicalTests(unittest.TestCase):
    def test_all_stock_totals_rounding_prices_and_acquisitions(self):
        beys = load_beys()
        self.assertEqual(len(beys), 165)
        default = [p for p in load_parts() if p.get('source') == 'beyblade_default']
        self.assertEqual(len(default), len(beys) * 2)
        self.assertEqual(len({p['name'] for p in default}), len(beys) * 2)
        self.assertEqual(len(PARTS_CATALOG), 51)
        for blade in beys.values():
            with self.subTest(blade=blade['id']):
                p = profile(blade['name'], blade['name'])
                reconcile(p)
                self.assertEqual(len(ids(p)), 4)
                before = copy.deepcopy(p)
                reconcile(p)
                self.assertEqual(p, before)
                result, delta = assemble(p, blade)
                for stat in CORE_STATS:
                    self.assertEqual(result['stats'][stat], blade['stats'][stat])
                    values = [blade['main_frame'][stat]] + [REGISTRY.part(blade['default_parts'][slot])['stats'][stat] for slot in ('disk','driver')]
                    for value, weight in zip(values, (0.45, 0.275, 0.275)):
                        self.assertLessEqual(abs(value-blade['stats'][stat]*weight), 1)
                invariant(self, p)
        for part in default:
            self.assertNotIn('price', part)
            self.assertFalse(part['shop_available'])
            self.assertFalse(part['tradable'])
            for value in (part['id'], part['name']):
                p = profile()
                before = copy.deepcopy(p)
                with self.assertRaisesRegex(PurchaseError, 'cannot be purchased'):
                    apply_part_purchase(p, value)
                self.assertEqual(p, before)

    def test_mounted_forms_keep_stock_stats_and_intrinsic_effects(self):
        from utils.spin_mode import resolve
        for blade in load_beys().values():
            for mode in blade.get('spin_modes', {}):
                p=profile(blade['name'])
                p['spin_mode']={blade['name']:mode}
                reconcile(p)
                mounted=resolve(p,copy.deepcopy(blade))
                assembled,_=assemble(p,mounted)
                self.assertEqual(assembled['stats'],mounted['stats'])
                for field in ('abilities','special_move'):
                    self.assertEqual(assembled.get(field),mounted.get(field))

    def test_switching_conserves_items_and_other_slot(self):
        p = profile('Dranzer', 'Draciel F')
        p['parts'] = ['Nexus Disk', 'Destroy Driver']
        reconcile(p)
        first, second = p['bey_instances']
        original = ids(p)
        old_disk, old_driver = first['parts']['disk'], first['parts']['driver']
        for _ in range(20):
            equip(p, 'Nexus Disk')
            self.assertEqual(first['parts']['driver'], old_driver)
            self.assertIsNone(next(i for i in owned_parts(p) if i['instance_id']==old_disk)['equipped_on'])
            select_instance(p, second['instance_id'])
            equip(p, old_disk)
            invariant(self, p)
            equip(p, second['bundled_parts']['disk'])
            select_instance(p, first['instance_id'])
            equip(p, old_disk)
            equip(p, 'Destroy Driver')
            self.assertEqual(first['parts']['disk'], old_disk)
            equip(p, old_driver)
        self.assertEqual(ids(p), original)
        self.assertEqual(assemble(p, DB.get_beyblade('Dranzer'))[0]['stats'], DB.get_beyblade('Dranzer')['stats'])

    def test_equipped_parts_swap_without_duplication_or_empty_slots(self):
        p = profile('Dranzer', 'Dranzer')
        reconcile(p)
        first, second = p['bey_instances']
        disk = first['parts']['disk']
        other_disk = second['parts']['disk']
        drivers = [e['parts']['driver'] for e in (first, second)]
        original = ids(p)
        with self.assertRaisesRegex(EquipmentError, 'always'):
            equip(p, disk, remove=True)
        select_instance(p, second['instance_id'])
        result = equip(p, disk)
        self.assertEqual(result['swapped_instance_id'], first['instance_id'])
        self.assertEqual(first['parts']['disk'], other_disk)
        self.assertEqual(second['parts']['disk'], disk)
        self.assertEqual([e['parts']['driver'] for e in (first, second)], drivers)
        self.assertEqual(ids(p), original)
        before = copy.deepcopy(p)
        self.assertTrue(equip(p, disk)['unchanged'])
        self.assertEqual(p, before)
        invariant(self, p)

    def test_name_equip_swaps_attached_part_and_spare_equip_releases_old_part(self):
        p = profile('Dranzer', 'Draciel F')
        p['parts'] = ['Destroy Driver']
        reconcile(p)
        first, second = p['bey_instances']
        equip(p, 'Destroy Driver')
        select_instance(p, second['instance_id'])
        old_driver = second['parts']['driver']
        result = equip(p, 'Destroy Driver')
        self.assertEqual(first['parts']['driver'], old_driver)
        self.assertEqual(result['swapped_instance_id'], first['instance_id'])
        before = copy.deepcopy(p)
        with self.assertRaisesRegex(EquipmentError, 'stock part is equipped'):
            equip(p, 'Destroy Driver', remove=True)
        self.assertEqual(p, before)
        result = equip(p, second['bundled_parts']['driver'])
        self.assertEqual(result['swapped_instance_id'], first['instance_id'])
        select_instance(p, first['instance_id'])
        result = equip(p, first['bundled_parts']['driver'])
        self.assertIsNone(result['swapped_with'])
        self.assertIsNone(next(i for i in owned_parts(p) if i['instance_id'] == result['previous'])['equipped_on'])
        invariant(self, p)

    def test_swap_rejects_reverse_incompatibility_without_changing_slots(self):
        p = profile('Dranzer', 'Draciel F')
        reconcile(p)
        first, second = p['bey_instances']
        before = copy.deepcopy(p)
        real_compatible = __import__('utils.bey_components', fromlist=['compatible']).compatible
        def limited(part, blade):
            return blade['name'] != second['name'] and real_compatible(part, blade)
        with patch('utils.bey_components.compatible', side_effect=limited):
            # Reconciliation needs the existing loadout accepted; restrict only
            # the reverse validation after it has finished.
            with patch('utils.bey_components.reconcile'):
                with self.assertRaisesRegex(EquipmentError, 'Cannot swap'):
                    equip(p, second['parts']['disk'])
        self.assertEqual(p, before)
        invariant(self, p)

    def test_version_one_migrates_purchased_configuration_and_progress(self):
        p = profile('Dranzer', 'Dranzer')
        p.update(parts=['Nexus Disk','Destroy Driver'], component_equipment_version=1,
            bey_instances=[{'name':'Dranzer','instance_id':'first','parts':{'disk':'nexus_disk','driver':'destroy_driver'}},
                           {'name':'Dranzer','instance_id':'second','parts':{}}], active_bey_instance='first')
        progress = copy.deepcopy(p['bey_progress'])
        reconcile(p)
        self.assertEqual({slot:definition_for(p,i)['id'] for slot,i in p['bey_instances'][0]['parts'].items()}, {'disk':'nexus_disk','driver':'destroy_driver'})
        self.assertEqual(len(ids(p)), 6)
        self.assertEqual(p['bey_progress'], progress)
        self.assertEqual(p['xp'],123)
        before = copy.deepcopy(p)
        reconcile(p)
        self.assertEqual(p,before)

    def test_attached_parts_transfer_available_parts_stay_and_return_is_idempotent(self):
        p,q = profile('Dranzer'),profile('Draciel F')
        p['parts']=['Nexus Disk']
        reconcile(p);reconcile(q)
        equip(p,'Nexus Disk')
        available = p['bey_instances'][0]['bundled_parts']['disk']
        union=ids(p)|ids(q)
        bey_id=p['bey_instances'][0]['instance_id']
        bundle=detach_bey(p,'Dranzer')
        attach_bey(q,bundle)
        self.assertEqual(ids(p)|ids(q),union)
        self.assertFalse(ids(p)&ids(q))
        self.assertIn(available,ids(p))
        self.assertIn('Nexus Disk',q['parts'])
        self.assertNotIn('Nexus Disk',p['parts'])
        self.assertIn(bey_id,{e['instance_id'] for e in q['bey_instances']})
        with self.assertRaises(EquipmentError): attach_bey(q,bundle)
        attach_bey(p,detach_bey(q,'Dranzer'))
        self.assertEqual(ids(p)|ids(q),union)
        invariant(self,p);invariant(self,q)


class TransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=UserStore(str(Path(self.temp.name)/'users.db'),str(Path(self.temp.name)/'users.json'))
        self.patch=patch.object(DB,'USER_STORE',self.store);self.patch.start()
        await DB.update_user(1, profile('Dranzer','Dranzer'))
        await DB.update_user(2, profile('Draciel F'))
        self.shop=ShopCog(None)
    async def asyncTearDown(self):
        self.patch.stop();self.temp.cleanup()
    def ctx(self,uid):
        return SimpleNamespace(author=SimpleNamespace(id=uid,mention=f'<@{uid}>',display_name=str(uid)),send=AsyncMock())

    async def test_restart_and_concurrent_acquisition_grant_exactly_once(self):
        results=await asyncio.gather(*(asyncio.to_thread(DB.add_beyblade_to_inventory,1,'Dranzer') for _ in range(10)))
        self.assertTrue(all(results))
        p=await DB.get_user(1)
        self.assertEqual(len(p['bey_instances']),12)
        self.assertEqual(len(ids(p)),24)
        with patch.object(DB,'USER_STORE',UserStore(self.store.db_path,str(Path(self.temp.name)/'users.json'))):
            self.assertEqual(await DB.get_user(1),p)

    async def test_actual_trade_command_moves_copy_and_components(self):
        a,b=await DB.get_user(1),await DB.get_user(2)
        union=ids(a)|ids(b)
        member=lambda uid:SimpleNamespace(id=uid,display_name=str(uid))
        cog=SimpleNamespace(release=lambda *args:None)
        trade=TradeView(cog,member(1),member(2));trade.offers={1:'Dranzer',2:'Draciel F'}
        with patch('cogs.extras.trade._log_trade'):
            await trade.complete_trade()
        a,b=await DB.get_user(1),await DB.get_user(2)
        self.assertEqual(ids(a)|ids(b),union)
        self.assertFalse(ids(a)&ids(b))
        self.assertEqual(a['inventory'],['Dranzer','Draciel F'])
        invariant(self,a);invariant(self,b)

    async def test_marketplace_escrow_cancel_and_purchase_preserve_components(self):
        await DB.mutate_user(1, lambda p:p.update(active_beyblade=None))
        original=await DB.get_user(1)
        await MarketplaceCog.sellbey.callback(MarketplaceCog(None),self.ctx(1),args='Dranzer 1000')
        escrow=await DB.get_user(1)
        listing=escrow['marketplace_listings'][0]
        self.assertEqual(len(listing['component_bundle']['part_instances']),2)
        await MarketplaceCog.cancellisting.callback(MarketplaceCog(None),self.ctx(1),bey_name='Dranzer')
        restored=await DB.get_user(1)
        self.assertEqual(ids(restored),ids(original))
        self.assertEqual({e['instance_id'] for e in restored['bey_instances']},{e['instance_id'] for e in original['bey_instances']})
        await MarketplaceCog.sellbey.callback(MarketplaceCog(None),self.ctx(1),args='Dranzer 1000')
        buyer=await DB.get_user(2)
        await MarketplaceCog.buybey.callback(MarketplaceCog(None),self.ctx(2),SimpleNamespace(id=1,bot=False,mention='<@1>',display_name='seller'),bey_name='Dranzer')
        seller,newbuyer=await DB.get_user(1),await DB.get_user(2)
        self.assertEqual(ids(seller)|ids(newbuyer),ids(original)|ids(buyer))
        self.assertFalse(ids(seller)&ids(newbuyer))
        self.assertEqual(newbuyer['coins'],buyer['coins']-1000)
        self.assertEqual(seller['coins'],original['coins']+950)
        self.assertFalse(seller['marketplace_listings'])
        invariant(self,seller);invariant(self,newbuyer)

    async def test_transfer_conflict_rolls_back_both_profiles(self):
        for uid in (1,2):
            await DB.mutate_user(uid, lambda p:apply_part_purchase(p,'Nexus Disk'))
        await DB.mutate_user(1,lambda p:equip(p,'Nexus Disk'))
        before={str(uid):await DB.get_user(uid) for uid in (1,2)}
        def transfer(profiles):
            attach_bey(profiles['2'],detach_bey(profiles['1'],'Dranzer'))
        with self.assertRaises(EquipmentError): await DB.mutate_users([1,2],transfer)
        self.assertEqual({str(uid):await DB.get_user(uid) for uid in (1,2)},before)

    async def test_shared_reward_writes_and_bulk_legacy_migration(self):
        p=profile('Dranzer')
        DB.save_users({'3':p})
        migrated=await DB.get_user(3)
        self.assertEqual(len(ids(migrated)),2)
        await DB.mutate_user(3,lambda p:p['inventory'].extend(['Draciel F','Dranzer']))
        rewards=await DB.get_user(3)
        self.assertEqual(len(ids(rewards)),6)
        self.assertEqual(rewards['bey_progress'],p['bey_progress'])
        before=copy.deepcopy(rewards)
        await DB.update_user(3,rewards)
        self.assertEqual(await DB.get_user(3),before)

    async def test_selling_purchased_part_restores_stock_and_preserves_selected_copy(self):
        await DB.mutate_user(1,lambda p:apply_part_purchase(p,'Nexus Disk'))
        await DB.mutate_user(1,lambda p:equip(p,'Nexus Disk'))
        p=await DB.get_user(1)
        await DB.mutate_user(1,lambda p:select_instance(p,p['bey_instances'][1]['instance_id']))
        before=await DB.get_user(1)
        await ShopCog.sell.callback(self.shop,self.ctx(1),part_name='Nexus Disk')
        after=await DB.get_user(1)
        self.assertEqual(after['active_bey_instance'],before['active_bey_instance'])
        self.assertNotIn('Nexus Disk',after['parts'])
        self.assertEqual(len(ids(after)),4)
        invariant(self,after)

    async def test_legacy_bey_snapshot_restores_then_migrates_without_stale_parts(self):
        from utils.snapshot import restore
        old=profile('Draciel F')
        await restore({'profiles':{'1':old}},sections=['beys'])
        restored=await DB.get_user(1)
        self.assertEqual(restored['inventory'],['Draciel F'])
        self.assertEqual(len(ids(restored)),2)
        invariant(self,restored)

    async def test_default_purchase_and_sale_commands_do_not_change_balance(self):
        p=await DB.get_user(1)
        name=REGISTRY.part(DB.get_beyblade('Dranzer')['default_parts']['disk'])['name']
        await ShopCog.buy.callback(self.shop,self.ctx(1),item_name=name)
        await ShopCog.sell.callback(self.shop,self.ctx(1),part_name=name)
        self.assertEqual(await DB.get_user(1),p)
        await ShopCog.myparts.callback(self.shop,self.ctx(1))


if __name__=='__main__': unittest.main()
