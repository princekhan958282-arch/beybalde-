"""BEYCBOT template, router, command, purchase and durable-storage regressions."""
import asyncio
import copy
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from contextlib import asynccontextmanager
import unittest
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageChops
from utils import info_card_shop as card, info_card_router as router
from utils.info_card_cosmetics import (apply_info_card_purchase, equipped_info_theme,
                                      InfoCardCosmeticError)

BLADE = dict(name='Test Bey', rarity='Legendary', type='Attack', level=5, xp=145,
             stats=dict(hp=400, attack=300, defense=250, stamina=200),
             abilities=[dict(name='First Ability', description='First effect description.'),
                        dict(name='Second Ability', description='Second effect description.')],
             special=dict(name='Final Strike', description='A powerful special move.'))


class RendererTests(unittest.TestCase):
    def test_asset_and_working_directory(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                with patch.object(card, '_art', return_value=None):
                    output = card.render(BLADE)
            finally:
                os.chdir(original)
        rendered = Image.open(output).convert('RGB')
        template = Image.open(card.TEMPLATE).convert('RGB')
        self.assertEqual(rendered.size, (1536, 1152))
        # Decorative pixels remain the supplied asset, not a generated HUD.
        for box in [(0,0,1536,75), (64,685,244,808), (64,958,421,1082), (20,250,60,1050)]:
            self.assertIsNone(ImageChops.difference(rendered.crop(box), template.crop(box)).getbbox())

    def test_ability_counts_and_malformed_data(self):
        for raw in [None, [], BLADE['abilities'][:1], BLADE['abilities'] + [dict(name='Third', description='Third')],
                    'bad', {}, [None, BLADE['abilities'][0]], BLADE['abilities'] + [None],
                    [dict(name='No description'), BLADE['abilities'][0]]]:
            self.assertIsNone(card.render(dict(BLADE, abilities=raw)))
        legacy = copy.deepcopy(BLADE)
        del legacy['abilities']
        legacy['ability'], legacy['ability_2'] = BLADE['abilities']
        self.assertIsNotNone(card.render(legacy))
        with patch.object(card, 'TEMPLATE', Path('/missing/card.png')), self.assertLogs(card.log, level='WARNING'):
            self.assertIsNone(card.render(BLADE))

    def test_art_local_url_transparent_and_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)/'art.png'
            art = Image.new('RGBA', (240, 80))
            art.paste((255,0,0,255), (20,20,220,60))
            art.save(local)
            with patch.object(card.legacy, '_local_art_path', return_value=str(local)):
                result = card._art(BLADE)
                self.assertEqual(result.size, (526,105))
                buf = card.render(BLADE)
                rendered = Image.open(buf)
                self.assertEqual(rendered.getpixel((350,456))[:3], (255,0,0))
                template = Image.open(card.TEMPLATE)
                self.assertEqual(rendered.getpixel((100,290)), template.getpixel((100,290)))
            with patch.object(card.legacy, '_local_art_path', return_value=None), \
                 patch('urllib.request.urlopen', return_value=io.BytesIO(local.read_bytes())) as fetch:
                self.assertIsNotNone(card.render(dict(BLADE, image_url='https://cdn.discordapp.com/attachments/1/art.png')))
                self.assertEqual(fetch.call_args.args[0].full_url, 'https://cdn.discordapp.com/attachments/1/art.png')
            with patch.object(card.legacy, '_local_art_path', return_value=None), \
                 patch('urllib.request.urlopen', side_effect=OSError('broken URL')):
                self.assertIsNotNone(card.render(dict(BLADE, image_url='https://invalid/art.png')))
            with patch.object(card.legacy, '_local_art_path', return_value=None):
                self.assertIsNotNone(card.render(BLADE))

    def test_long_text_bounds_and_truncation(self):
        draw = ImageDrawSpy()
        for text in ['Short', 'Long name with many words '*50, 'X'*2000]:
            card._text(draw, (0,0,1180,60), text, 23, 18, 2)
            for xy, line, font in draw.drawn:
                self.assertLessEqual(draw.textlength(line,font=font), 1180)
                self.assertLessEqual(xy[1]+font.size, 60)
            if len(text)>100:
                self.assertTrue(draw.drawn[-1][1].endswith('…'))
            draw.drawn.clear()
        blade=copy.deepcopy(BLADE)
        blade['name']='Extremely long Bey name '*50
        for index, entry in enumerate(blade['abilities']+[blade['special']]):
            entry['name']=str(index)+'UnbrokenName'*100
            entry['description']='Very long description of the effect. '*100
        self.assertIsNotNone(card.render(blade))

    def test_exp_and_stats(self):
        with patch.object(card, '_text', wraps=card._text) as text, patch.object(card, '_art', return_value=None):
            card.render(BLADE)
            labels=[c.args[2] for c in text.call_args_list]
            self.assertIn('17 / 72',labels)
            for value in [400,300,250,200]: self.assertIn(value,labels)
            text.reset_mock()
            card.render(dict(BLADE, level=100))
            self.assertIn('MAX LEVEL',[c.args[2] for c in text.call_args_list])
            text.reset_mock()
            blade=dict(BLADE); del blade['xp']
            card.render(blade)
            self.assertIn('Progress unavailable',[c.args[2] for c in text.call_args_list])


class ImageDrawSpy:
    def __init__(self):
        from PIL import ImageDraw
        self.draw=ImageDraw.Draw(Image.new('RGB',(1536,1152)))
        self.drawn=[]
    def textlength(self,*a,**kw): return self.draw.textlength(*a,**kw)
    def text(self,xy,line,font,**kw): self.drawn.append((xy,line,font))


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from utils import database as db
        from utils.userstore import UserStore
        self.db=db
        self.tmp=tempfile.TemporaryDirectory()
        self.store=UserStore(str(Path(self.tmp.name)/'users.db'),str(Path(self.tmp.name)/'absent.json'))
        self.patcher=patch.object(db,'USER_STORE',self.store); self.patcher.start()
        self.store.put_one('1',dict(coins=100000, inventory=['keep'], stats={'attack':123}))
    async def asyncTearDown(self):
        self.patcher.stop(); self.tmp.cleanup()

    async def test_purchase_atomicity_persistence_and_no_bonuses(self):
        results=await asyncio.gather(*[self.db.mutate_user(1,lambda p: apply_info_card_purchase(p,'beycbot_2ability')) for _ in range(10)],return_exceptions=True)
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        self.assertEqual(sum(isinstance(r,InfoCardCosmeticError) for r in results),9)
        from utils.userstore import UserStore
        reopened=UserStore(str(Path(self.tmp.name)/'users.db'),str(Path(self.tmp.name)/'absent.json'))
        p=reopened.get_one('1')
        self.assertEqual(p['coins'],33000)
        self.assertEqual(equipped_info_theme(p),'beycbot_2ability')
        self.assertEqual(p['stats'],{'attack':123})
        self.assertEqual(p['inventory'],['keep'])
        for coins in [66999,0,'bad',None]:
            p={'coins':coins}; before=copy.deepcopy(p)
            with self.assertRaises(InfoCardCosmeticError): apply_info_card_purchase(p,'beycbot_2ability')
            self.assertEqual(p,before)
        self.assertEqual(equipped_info_theme({'equipped_info_card_theme':'beycbot_2ability'}),'default')

    async def test_storage_failure_and_exact_balance(self):
        before=self.store.get_one('1')
        with patch.object(self.store,'put_one',side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                await self.db.mutate_user(1,lambda p: apply_info_card_purchase(p,'beycbot_2ability'))
        self.assertEqual(self.store.get_one('1'),before)
        p={'coins':67000}
        self.assertEqual(apply_info_card_purchase(p,'beycbot_2ability')['spent'],67000)
        self.assertEqual(p['coins'],0)

    async def test_router_and_default_fallback(self):
        with patch.object(router.v2,'render_info_card',AsyncMock(return_value='default')):
            self.assertEqual(await router.render_info_card(BLADE),'default')
            self.assertIsInstance(await router.render_info_card(BLADE,info_theme='beycbot_2ability'),io.BytesIO)
            for abilities in [None,'bad',BLADE['abilities'][:1],BLADE['abilities']+[dict(name='Three',description='Three')]]:
                self.assertEqual(await router.render_info_card(dict(BLADE,abilities=abilities),info_theme='beycbot_2ability'),'default')

    async def test_shop_ui_and_callback(self):
        from cogs.ui.main_shop import MainShopView, SECTION_PROFILE, _profiles_embed
        embed=_profiles_embed()
        self.assertIn('67,000',str(embed.to_dict()))
        view=MainShopView(1); view.section=SECTION_PROFILE; view._build_buttons()
        self.assertTrue(any('BEYCBOT' in (b.label or '') and '67,000' in b.label for b in view.children))
        interaction=SimpleNamespace(user=SimpleNamespace(id=1),response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        await view._buy_info_card(interaction)
        self.assertEqual(self.store.get_one('1')['coins'],33000)
        await view._buy_info_card(interaction)
        self.assertEqual(self.store.get_one('1')['coins'],33000)
        view.stop()

    async def test_info_sends_template_with_player_xp(self):
        from cogs.economy.profile import ProfileCog, _with_viewer_level
        p=self.store.get_one('1'); apply_info_card_purchase(p,'beycbot_2ability'); p['bey_progress']={BLADE['name']:{'xp':145,'ivs':{}}}; self.store.put_one('1',p)
        leveled=await _with_viewer_level(1,BLADE)
        self.assertEqual((leveled['level'],leveled['xp']),(5,145))
        @asynccontextmanager
        async def typing(): yield
        ctx=SimpleNamespace(author=SimpleNamespace(id=1),typing=typing,send=AsyncMock())
        blade=dict(BLADE); del blade['xp']
        with patch.object(card,'_text',wraps=card._text) as text:
            await ProfileCog(None)._send_bey_card(ctx,blade)
            self.assertIn('17 / 72',[c.args[2] for c in text.call_args_list])
        file=ctx.send.call_args.kwargs['file']
        self.assertEqual(Image.open(file.fp).size,(1536,1152)); file.close()


if __name__=='__main__': unittest.main()
