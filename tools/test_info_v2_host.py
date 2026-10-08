"""Container-font portability and actual renderer diagnostics."""
import asyncio
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageFont
from utils import info_card_v2 as v2, info_card_router as router

BLADE={'name':'Container Test','type':'Attack','rarity':'Common',
       'stats':{'hp':100,'attack':100,'defense':100,'stamina':100},
       'abilities':[{'name':'One','description':'An ability description.'}],
       'special_move':{'name':'Test Special','description':'Special description.'}}

class BitmapFont:
    def __init__(self): self.font=ImageFont.load_default()
    def __getattr__(self,name):
        if name=='size': raise AttributeError('bitmap font has no size')
        return getattr(self.font,name)

class HostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        v2._FONT_CACHE.clear(); v2.clear_cache()
    async def test_container_uses_bundled_font_without_system_fonts(self):
        real=ImageFont.truetype
        def font(path,*args,**kwargs):
            if not str(path).endswith('/assets/ui/beycbot_font.ttf'):
                raise OSError('no system fonts')
            return real(path,*args,**kwargs)
        with patch.object(v2.ImageFont,'truetype',side_effect=font), patch.object(v2,'_art',return_value=None):
            for bold in [True,False]:
                self.assertEqual(v2._font(25,bold).size,25)
            buf=await router.render_info_card(BLADE)
            self.assertEqual(Image.open(buf).width,900)
            self.assertEqual(router.last_engine,'v2/pillow')
            self.assertEqual(router.last_render_error,'')
            await router.render_info_card(BLADE)
            self.assertEqual(router.last_engine,'v2/cache')
    async def test_bitmap_fallback_has_no_size_but_still_renders(self):
        bitmap=BitmapFont()
        with patch.object(v2.ImageFont,'truetype',side_effect=OSError('no fonts')), \
             patch.object(v2.ImageFont,'load_default',return_value=bitmap), \
             patch.object(v2,'_art',return_value=None):
            self.assertIsNotNone(await v2.render_info_card(BLADE))
            self.assertEqual(v2.last_engine,'v2/pillow')
    async def test_reports_actual_fallback_error(self):
        with patch.object(v2,'render_info_card_pillow',side_effect=RuntimeError('forced V2 failure')), \
             patch.object(v2.legacy,'render_info_card',AsyncMock(return_value=io.BytesIO(b'legacy'))), \
             patch.object(v2.legacy,'last_engine','pillow'), self.assertLogs(v2.log,level='WARNING'):
            await router.render_info_card(BLADE)
        self.assertEqual(router.last_engine,'legacy/pillow')
        self.assertEqual(router.last_render_error,'RuntimeError: forced V2 failure')
    async def test_admin_debug_reports_selection_error_and_attachment_format(self):
        from cogs.admin.actions import _carddebug
        buf=io.BytesIO(); Image.new('RGB',(900,100)).save(buf,'JPEG'); buf.seek(0); buf.name='bey_info_v2.jpg'
        with patch.object(router,'render_info_card',AsyncMock(return_value=buf)), \
             patch.object(router,'last_engine','v2/pillow'), \
             patch.object(router,'last_render_error',''), \
             patch.object(router.legacy,'last_playwright_error',''):
            result=await _carddebug(None)
        self.assertIn('v2/pillow',result.message)
        self.assertIn('selected:',result.message)
        self.assertTrue(result.file.filename.endswith('.jpg'))
        result.file.close()

if __name__=='__main__': unittest.main()
