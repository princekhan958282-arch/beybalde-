"""Info card labels, default lookup and selected-copy regression tests."""
import copy
import unittest
from unittest.mock import AsyncMock, patch
from PIL import ImageDraw

from utils import database as DB
from utils.character_registry import REGISTRY
from utils.bey_components import reconcile, equip, select_instance, assemble
from utils.info_card_parts import card_parts, parts_slots
from utils import info_card_v2 as V2, info_card_legacy as LEGACY
from cogs.economy.profile import _viewer_parts
from cogs.ui.inventory_ui import InventoryView


def profile():
    p={'inventory':['Noctilune','Noctilune'],'active_beyblade':'Noctilune',
       'parts':['Nexus Disk','Destroy Driver'],'equipped_parts':[]}
    reconcile(p)
    return p


class InfoComponentTests(unittest.IsolatedAsyncioTestCase):
    def test_every_official_bey_has_displayable_defaults(self):
        for blade in REGISTRY.load('bey').values():
            slots=parts_slots(blade)
            self.assertEqual([s['slot'] for s in slots],['BLADE','DISK','DRIVER'])
            for slot,value in zip(('disk','driver'),slots[1:]):
                self.assertEqual(value['value'],REGISTRY.part(blade['default_parts'][slot])['name'])
            self.assertEqual(LEGACY._parts_slots(blade,None),slots)

    async def test_viewer_lookup_includes_bundled_non_shop_parts(self):
        p=profile();blade=DB.get_beyblade('Noctilune')
        with patch('cogs.economy.profile.get_user',AsyncMock(return_value=p)):
            self.assertEqual(await _viewer_parts(1,blade),card_parts(blade))
            equip(p,'Nexus Disk');equip(p,'Destroy Driver')
            self.assertEqual(await _viewer_parts(1,blade),{'disk':'Nexus Disk','driver':'Destroy Driver'})
            other=DB.get_beyblade('Dranzer')
            self.assertEqual(await _viewer_parts(1,other),card_parts(other))

    def test_duplicate_selection_and_inventory_card_use_exact_copy(self):
        p=profile();blade=DB.get_beyblade('Noctilune')
        first,second=p['bey_instances']
        equip(p,'Nexus Disk')
        view=object.__new__(InventoryView)
        view.detail={'instance_id':first['instance_id']}
        from utils.bey_components import owned_parts
        view._cache={'part':[{'ptype':x['category'],'name':x['name'],'equipped_on':x['equipped_on']} for x in owned_parts(p)]}
        select_instance(p,second['instance_id'])
        self.assertEqual(view._card_parts()['disk'],'Nexus Disk')
        self.assertEqual(card_parts(blade,profile=p),card_parts(blade))

    def test_snapshot_retains_names_after_equipment_switch(self):
        p=profile();blade=DB.get_beyblade('Noctilune')
        equip(p,'Nexus Disk')
        frozen,_=assemble(p,blade)
        equip(p,p['bey_instances'][0]['bundled_parts']['disk'])
        self.assertEqual(card_parts(frozen)['disk'],'Nexus Disk')
        self.assertNotEqual(card_parts(blade,profile=p)['disk'],'Nexus Disk')

    def test_old_argument_names_remain_compatible_but_labels_are_updated(self):
        slots=parts_slots({'name':'Custom'}, {'ratchet':'Old Disk','bit':'Old Driver'})
        self.assertEqual(slots[1:], [{'slot':'DISK','value':'Old Disk'},{'slot':'DRIVER','value':'Old Driver'}])
        self.assertEqual(card_parts({'name':'Custom'})['disk'],None)

    def test_actual_v2_render_draws_updated_labels_and_default_names(self):
        blade=DB.get_beyblade('Noctilune')
        texts=[]
        original=ImageDraw.ImageDraw.text
        def record(draw,xy,text,*args,**kwargs):
            texts.append(str(text));return original(draw,xy,text,*args,**kwargs)
        with patch.object(V2,'_art',return_value=None),patch.object(ImageDraw.ImageDraw,'text',record):
            result=V2.render_info_card_pillow(blade)
        self.assertIsNotNone(result)
        self.assertIn('DISK:',texts);self.assertIn('DRIVER:',texts)
        self.assertNotIn('RATCHET:',texts);self.assertNotIn('BIT:',texts)
        self.assertTrue(any('Harmony' in t for t in texts))
        self.assertTrue(any('Pivot' in t for t in texts))

if __name__=='__main__': unittest.main()
