"""Isolated migration/equipment/battle regression tests; no Discord login.
Run: python -m unittest tools.test_character_components
"""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from utils.character_registry import ROOT, REGISTRY, CharacterRegistry, CORE_STATS, load_beys, load_avatars
from utils.bey_components import EquipmentError, assemble, compatible, equip, reconcile, select_instance
from utils import database as DB
from utils.userstore import UserStore
from utils.loadout import effective_blade
from cogs.economy.shop import PARTS_CATALOG, PurchaseError, apply_part_purchase, get_part_stat_deltas

BASELINE = json.loads((ROOT / 'docs/component_migration_baseline.json').read_text())

SKILL_CORRECTIONS = json.loads((ROOT / 'docs/avatar_skill_regression_baseline.json').read_text())['avatars']
DRANZER_ADDITIONS = {f'BB{132+i}': name for i, name in enumerate(
    ('Dranzer G', 'Black Dranzer', 'Dranzer F', 'Dranzer V', 'Dranzer V2', 'Dranzer GT', 'Dranzer MS'))}
OCTOBER_ADDITIONS = {f'BB{139+i}': name for i, name in enumerate(
    ('Golden Imperial Dragon', 'Black Valkyrie', 'Black Brave Valkyrie',
     'Ultimate Dark Valkyrie', 'Strike Longinus', 'Dragon Circle',
     'Legend Spriggan', 'Shelter Regulus', 'Abyss Fang', 'Deathscyth Longinus'))}
OCTOBER_ADDITIONS.update({'BB149': 'S,Dragon Killer', 'BB150': 'Black Legend'})

def digest(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

def profile():
    return {'user_id': '701', 'inventory': ['Dranzer', 'Dranzer'], 'active_beyblade': 'Dranzer',
            'coins': 10000, 'parts': ['Destroy Driver', 'Atomic Driver', 'Over Disk', 'Nexus Disk'],
            'equipped_parts': [], 'bey_progress': {}, 'xp': 0}

class MigrationTests(unittest.TestCase):
    def test_every_record_and_every_original_field_is_preserved(self):
        for kind, entries in (('bey', load_beys().values()), ('avatar', load_avatars()['avatars'])):
            baseline = BASELINE['beys' if kind == 'bey' else 'avatars']
            additions = {**DRANZER_ADDITIONS, **OCTOBER_ADDITIONS} if kind == 'bey' else {}
            self.assertEqual({v['id'] for v in entries}, set(baseline) | set(additions))
            for value in entries:
                if value['id'] in additions:
                    self.assertEqual(value['name'], additions[value['id']])
                    continue  # New kits have dedicated behavioral/stock-data coverage in test_dranzer.
                expected = baseline[value['id']]
                # Includes complete abilities, transformations, skills, images,
                # shop metadata, cooldowns and every other original field.
                with self.subTest(kind=kind, id=value['id'], name=value['name']):
                    correction = SKILL_CORRECTIONS.get(value['id']) if kind == 'avatar' else None
                    expected_hash = expected['sha256']
                    if correction:
                        self.assertEqual(correction['migration_sha256'], expected_hash)
                        self.assertEqual(correction['name'], value['name'])
                        expected_hash = correction['sha256']
                    self.assertEqual(digest({field: value[field] for field in expected['fields']}), expected_hash)
        for folder, number in (('beys', 151), ('avatars', 54), ('parts/disks', 169), ('parts/drivers', 168)):
            self.assertEqual(len(list((ROOT / folder).glob('*.json'))), number)
        for folder in ('beys', 'avatars'):
            self.assertFalse(any(p.is_dir() for p in (ROOT / folder).iterdir()))
        self.assertFalse((ROOT / 'data/beyblades.json').exists())
        self.assertFalse((ROOT / 'cogs/avatar/avatar_data.json').exists())

    def test_skill_corrections_reference_existing_avatar_records(self):
        self.assertTrue(SKILL_CORRECTIONS)
        self.assertLessEqual(set(SKILL_CORRECTIONS), set(BASELINE['avatars']))

    def test_parts_preserve_verified_prices_and_tradeoffs(self):
        catalog = {p['name']: p for p in PARTS_CATALOG}
        for old in BASELINE['parts']:
            new = catalog[old['name']]
            self.assertEqual({k: new[k] for k in old}, old)
            if old['type'] != 'ring':
                expected = dict.fromkeys(CORE_STATS, 0)
                expected[old['stat']] += old['bonus']
                if old.get('penalty_stat'):
                    expected[old['penalty_stat']] -= old.get('penalty', 0)
                for stat, value in old.get('penalties', {}).items():
                    expected[stat] -= value
                self.assertEqual(new['stats'], expected)
                self.assertEqual(get_part_stat_deltas([new['name']]), {s: n for s, n in expected.items() if n})

    def test_defaults_preserve_every_legacy_statline(self):
        for blade in load_beys().values():
            assembled, delta = assemble({}, blade)
            self.assertEqual(assembled['stats'], blade['stats'])
            self.assertEqual(delta, dict.fromkeys(CORE_STATS, 0))

    def test_ids_names_aliases_and_private_copies(self):
        blade = DB.get_beyblade('Dranzer')
        self.assertEqual(DB.get_beyblade(blade['id'])['name'], 'Dranzer')
        self.assertEqual(DB.get_beyblade('dRaNzEr')['id'], blade['id'])
        blade['stats']['attack'] = -999
        self.assertNotEqual(DB.get_beyblade('Dranzer')['stats']['attack'], -999)

    def test_bad_reload_is_rejected_without_overriding_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'avatars').mkdir()
            source = copy.deepcopy(load_avatars()['avatars'][0])
            (root / 'avatars/a.json').write_text(json.dumps(source))
            registry = CharacterRegistry(root)
            before = copy.deepcopy(registry.load('avatar'))
            (root / 'avatars/b.json').write_text(json.dumps(source))
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                registry.load('avatar', reload=True)
            self.assertEqual(registry.load('avatar'), before)
            (root / 'avatars/b.json').write_text('{ malformed')
            with self.assertRaisesRegex(ValueError, 'b.json'):
                registry.load('avatar', reload=True)

    def test_missing_core_stat_and_nested_character_files_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'parts/drivers').mkdir(parents=True)
            part = copy.deepcopy(REGISTRY.part('Destroy Driver'))
            del part['stats']['hp']
            (root / 'parts/drivers/bad.json').write_text(json.dumps(part))
            with self.assertRaisesRegex(ValueError, 'hp'):
                CharacterRegistry(root).load('driver')
            (root / 'avatars/type').mkdir(parents=True)
            (root / 'avatars/a.json').write_text(json.dumps(load_avatars()['avatars'][0]))
            (root / 'avatars/type/b.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'subfolders'):
                CharacterRegistry(root).load('avatar')

class EquipmentTests(unittest.TestCase):
    def setUp(self):
        self.profile = profile()
        reconcile(self.profile)
        self.blade = DB.get_beyblade('Dranzer')

    def test_replacement_and_repeated_equip_do_not_stack(self):
        equip(self.profile, 'Destroy Driver')
        equip(self.profile, 'Nexus Disk')
        first, _ = assemble(self.profile, self.blade)
        for _ in range(5):
            equip(self.profile, 'Nexus Disk')
            equip(self.profile, 'Destroy Driver')
        self.assertEqual(assemble(self.profile, self.blade)[0]['stats'], first['stats'])
        equip(self.profile, 'Atomic Driver')
        changed, delta = assemble(self.profile, self.blade)
        self.assertEqual(changed['stats'], dict(self.blade['stats'], **{s: self.blade['main_frame'][s] + REGISTRY.part('Nexus Disk')['stats'][s] + REGISTRY.part('Atomic Driver')['stats'][s] for s in CORE_STATS}))
        self.assertEqual(changed['stats']['attack'], self.blade['main_frame']['attack'] + 10)
        equip(self.profile, 'Over Disk')
        _, delta = assemble(self.profile, self.blade)
        self.assertEqual(delta, {s: REGISTRY.part('Over Disk')['stats'][s] + REGISTRY.part('Atomic Driver')['stats'][s] - sum(REGISTRY.part(self.blade['default_parts'][slot])['stats'][s] for slot in ('disk','driver')) for s in CORE_STATS})
        self.assertEqual(self.blade, DB.get_beyblade('Dranzer'))

    def test_every_component_contributes_independently(self):
        # Synthetic fixture verifies HP as well: shipped parts currently have
        # no HP bonuses. This fixture never changes production balance data.
        custom_disk = copy.deepcopy(REGISTRY.part('Over Disk'))
        custom_disk['stats'] = {'hp': 3, 'attack': 5, 'defense': 7, 'stamina': 11}
        custom_driver = copy.deepcopy(REGISTRY.part('Destroy Driver'))
        custom_driver['stats'] = {'hp': 2, 'attack': -2, 'defense': 1, 'stamina': -1}
        original = REGISTRY.part
        def lookup(value):
            for p in (custom_disk, custom_driver):
                if value in (p['id'], p['name']): return p
            return original(value)
        with patch.object(REGISTRY, 'part', side_effect=lookup):
            equip(self.profile, 'Over Disk')
            equip(self.profile, 'Destroy Driver')
            assembled, delta = assemble(self.profile, self.blade)
        for stat in CORE_STATS:
            self.assertEqual(assembled['stats'][stat], self.blade['main_frame'][stat] + custom_disk['stats'][stat] + custom_driver['stats'][stat])

    def test_two_copies_have_distinct_builds_and_exclusive_parts(self):
        equip(self.profile, 'Destroy Driver')
        first, second = self.profile['bey_instances']
        select_instance(self.profile, second['instance_id'])
        previous = second['parts']['driver']
        result = equip(self.profile, 'Destroy Driver')
        self.assertEqual(result['swapped_instance_id'], first['instance_id'])
        self.assertEqual(first['parts']['driver'], previous)
        equip(self.profile, 'Atomic Driver')
        self.assertNotEqual(first['parts'], second['parts'])
        select_instance(self.profile, first['instance_id'])
        equip(self.profile, 'Destroy Driver')
        self.assertIn('Destroy Driver', self.profile['equipped_parts'])
        self.assertEqual(assemble(self.profile, self.blade)[1]['attack'], 10 - REGISTRY.part(self.blade['default_parts']['driver'])['stats']['attack'])
        select_instance(self.profile, second['instance_id'])
        self.assertEqual(assemble(self.profile, self.blade)[1]['defense'], 12 - REGISTRY.part(self.blade['default_parts']['driver'])['stats']['defense'])

    def test_unowned_incompatible_and_removed_copy_rejected(self):
        with self.assertRaisesRegex(EquipmentError, 'not own'):
            equip(self.profile, 'Hyper Driver')
        part = REGISTRY.part('Destroy Driver')
        with patch.dict(part['compatibility'], {'types': ['Stamina']}):
            with self.assertRaisesRegex(EquipmentError, 'incompatible'):
                equip(self.profile, 'Destroy Driver')
        with self.assertRaises(EquipmentError):
            select_instance(self.profile, 'missing')

    def test_legacy_loadout_migrates_once_and_selling_releases_part(self):
        p = profile()
        p['parts'].append('Slash Ring')
        p['equipped_parts'] = ['Slash Ring', 'Destroy Driver', 'Nexus Disk']
        reconcile(p)
        snapshot = copy.deepcopy(p)
        reconcile(p)
        self.assertEqual(p, snapshot)
        from utils.bey_components import definition_for
        self.assertEqual({slot: definition_for(p, ident)['id'] for slot, ident in p['bey_instances'][0]['parts'].items()}, {'driver': 'destroy_driver', 'disk': 'nexus_disk'})
        self.assertEqual(set(p['bey_instances'][1]['parts']), {'disk','driver'})
        assembled, _ = assemble(p, self.blade)
        self.assertEqual(assembled['stats']['attack'], self.blade['main_frame']['attack'] + 29)
        p['parts'].remove('Destroy Driver')
        reconcile(p)
        self.assertEqual(definition_for(p, p['bey_instances'][0]['parts']['driver'])['id'], self.blade['default_parts']['driver'])

    def test_copy_removal_releases_equipment_and_preserves_remaining_id(self):
        equip(self.profile, 'Destroy Driver')
        remaining_id = self.profile['bey_instances'][1]['instance_id']
        self.profile['inventory'].remove('Dranzer')
        reconcile(self.profile)
        self.assertEqual(self.profile['bey_instances'][0]['instance_id'], remaining_id)
        self.assertEqual(set(self.profile['bey_instances'][0]['parts']), {'disk','driver'})
        self.assertIn('Destroy Driver', self.profile['parts'])

class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = UserStore(str(root / 'users.db'), str(root / 'users.json'))
        self.patch = patch.object(DB, 'USER_STORE', self.store)
        self.patch.start()
        await DB.update_user(701, profile())
    async def asyncTearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    async def test_restart_keeps_builds_and_all_existing_progress(self):
        await DB.mutate_user(701, lambda p: equip(p, 'Destroy Driver'))
        before = await DB.get_user(701)
        restarted = UserStore(self.store.db_path, str(Path(self.temp.name) / "users.json"))
        with patch.object(DB, 'USER_STORE', restarted):
            self.assertEqual(await DB.get_user(701), before)

    async def test_atomic_purchase_grants_part_and_charges_once(self):
        part = REGISTRY.part('Hyper Driver')
        results = await asyncio.gather(*(DB.mutate_user(701, lambda p: apply_part_purchase(p, part['name'])) for _ in range(2)), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, PurchaseError) for r in results), 1)
        p = await DB.get_user(701)
        self.assertEqual(p['coins'], 10000 - part['price'])
        self.assertEqual(p['parts'].count(part['name']), 1)
        original = copy.deepcopy(p)
        with self.assertRaises(EquipmentError):
            await DB.mutate_user(701, lambda p: equip(p, 'Omega Driver'))
        self.assertEqual(await DB.get_user(701), original)

    async def test_shared_loadout_uses_selected_build_and_remains_a_snapshot(self):
        await DB.mutate_user(701, lambda p: equip(p, 'Destroy Driver'))
        blade = DB.get_beyblade('Dranzer')
        p = await DB.get_user(701)
        first, _, _ = await effective_blade(701, profile=p, blade=blade, include_avatar=False)
        self.assertEqual(first['stats']['attack'], blade['stats']['attack'] + 10 - REGISTRY.part(blade['default_parts']['driver'])['stats']['attack'])
        await DB.mutate_user(701, lambda p: equip(p, 'Atomic Driver'))
        second, _, _ = await effective_blade(701, blade=blade, include_avatar=False)
        self.assertEqual(first['stats']['attack'], blade['stats']['attack'] + 10 - REGISTRY.part(blade['default_parts']['driver'])['stats']['attack'])
        self.assertEqual(second['stats']['attack'], blade['stats']['attack'] - REGISTRY.part(blade['default_parts']['driver'])['stats']['attack'])
        self.assertNotEqual(first['component_snapshot']['parts'], second['component_snapshot']['parts'])

    async def test_custom_bey_keeps_legacy_equipment_and_authored_roster_untouched(self):
        custom = {'id': 'custom_701', 'name': 'Custom Test', 'stats': {'hp': 100, 'attack': 100, 'defense': 100, 'stamina': 100, 'special': 100}, 'abilities': []}
        p = profile()
        p.update(active_beyblade=custom['name'], active_custom_bey=True, custom_bey=custom, equipped_parts=['Destroy Driver'])
        before = copy.deepcopy(custom)
        effective, _, _ = await effective_blade(701, profile=p, blade=custom, include_avatar=False)
        self.assertEqual(effective['stats']['attack'], 110)
        self.assertEqual(custom, before)
        self.assertIsNone(REGISTRY.find_bey(custom['name']))

if __name__ == '__main__':
    unittest.main()
