"""Rename compatibility and preservation of owned BB155 copies."""
import copy
import unittest
from utils.character_registry import REGISTRY
from utils.bey_components import reconcile, assemble

OLD = 'Jambo Jamunter'
NEW = 'Jail Jormungand'


class Rename(unittest.TestCase):
    def test_name_type_aliases_and_parts(self):
        blade = REGISTRY.find_bey(NEW)
        self.assertEqual(blade['id'], 'BB155')
        self.assertEqual(blade['type'], 'Stamina')
        for name in (OLD, 'Jumbo Jormuntor', 'Jumbo Jormungand'):
            self.assertEqual(REGISTRY.find_bey(name)['name'], NEW)
        for slot, ident in blade['default_parts'].items():
            part = REGISTRY.part(ident)
            self.assertTrue(part['name'].startswith(NEW))
            self.assertEqual(REGISTRY.part(part['name'].replace(NEW, OLD))['id'], ident)

    def test_owned_instances_progress_and_equipment_survive(self):
        profile = {'inventory': [OLD, OLD], 'active_beyblade': OLD,
                   'bey_progress': {OLD: {'xp': 12345, 'ivs': {'attack': 11}}}}
        reconcile(profile)
        instances = copy.deepcopy(profile['bey_instances'])
        parts = copy.deepcopy(profile['part_instances'])
        self.assertEqual(profile['inventory'], [NEW, NEW])
        self.assertEqual(profile['active_beyblade'], NEW)
        self.assertEqual(profile['bey_progress'][NEW], {'xp': 12345, 'ivs': {'attack': 11}})
        self.assertNotIn(OLD, profile['bey_progress'])
        mounted, delta = assemble(profile, REGISTRY.find_bey(NEW))
        self.assertEqual(mounted['type'], 'Stamina')
        self.assertEqual(mounted['stats'], REGISTRY.find_bey(NEW)['stats'])
        self.assertFalse(any(delta.values()))
        reconcile(profile)
        self.assertEqual(instances, profile['bey_instances'])
        self.assertEqual(parts, profile['part_instances'])

    def test_preexisting_instance_ids_and_dictionary_inventory_survive(self):
        profile = {'inventory': [{'name': NEW, 'serial': 'owned-copy'}], 'active_beyblade': NEW}
        reconcile(profile)
        ident = profile['active_bey_instance']
        parts = copy.deepcopy(profile['part_instances'])
        profile['inventory'][0]['name'] = OLD
        profile['active_beyblade'] = OLD
        profile['bey_instances'][0]['name'] = OLD
        reconcile(profile)
        self.assertEqual(profile['active_bey_instance'], ident)
        self.assertEqual(profile['bey_instances'][0]['name'], NEW)
        self.assertEqual(profile['inventory'][0], {'name': NEW, 'serial': 'owned-copy'})
        self.assertEqual(profile['part_instances'], parts)

    def test_colliding_progress_keeps_highest_and_archives_other_record(self):
        profile = {'inventory': [OLD, NEW], 'bey_progress': {
            OLD: {'xp': 1000, 'ivs': {'attack': 4}}, NEW: {'xp': 100, 'ivs': {'attack': 7}}}}
        reconcile(profile)
        self.assertEqual(profile['bey_progress'][NEW]['xp'], 1000)
        self.assertEqual(profile['bey_rename_archive'][NEW]['ivs']['attack'], 7)
        before = copy.deepcopy(profile)
        reconcile(profile)
        self.assertEqual(profile, before)


if __name__ == '__main__': unittest.main()
