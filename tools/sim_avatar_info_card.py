#!/usr/bin/env python3
"""Targeted ;ainfo render and Discord attachment checks; no live Discord needed."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
from utils import avatar_info_card as C
from cogs.avatar.avatar_shop import AvatarShop, AvatarSkillsView

CARDS = json.loads((Path(__file__).resolve().parents[1] / 'cogs/avatar/avatar_data.json').read_text())['avatars']
YUKI = CARDS[0]

class CardTests(unittest.TestCase):
    def setUp(self):
        C.clear_cache()

    def test_all_cards_and_states_render_without_mutation(self):
        original = copy.deepcopy(CARDS)
        with patch.object(C, '_art', return_value=None):
            for card in CARDS:
                for owned, level in [(False, 1), (True, 1), (True, 5)]:
                    with self.subTest(card=card['id'], owned=owned, level=level):
                        buf = C.render_avatar_info_card(card, owned=owned, level=level)
                        self.assertIsNotNone(buf)
                        with Image.open(buf) as im:
                            self.assertEqual(im.size, C.SIZE)
                            self.assertEqual(im.format, 'JPEG')
        self.assertEqual(CARDS, original)

    def test_permanent_bonuses_match_growth_and_exclude_skills(self):
        bonuses = C.permanent_bonuses(YUKI, owned=True, level=3)
        self.assertEqual(bonuses, {'hp_flat': 0, 'hp_percent': .03, 'attack_flat': 10, 'defence_flat': 24, 'stamina_flat': 12})
        self.assertEqual(C.permanent_bonuses(YUKI), {'hp_flat': 0, 'hp_percent': .03})
        passive = {**YUKI, 'skills': []}  # Compatibility with cards without skills
        self.assertEqual(C.permanent_bonuses(passive), passive['bonuses'])

    def test_drawn_values_keep_plus_sign_and_skill_costs(self):
        drawn = []
        original = ImageDraw.ImageDraw.text
        def record(draw, xy, text, *args, **kwargs):
            drawn.append(text)
            return original(draw, xy, text, *args, **kwargs)
        with patch.object(C, '_art', return_value=None), patch.object(ImageDraw.ImageDraw, 'text', record):
            self.assertIsNotNone(C.render_avatar_info_card(YUKI, owned=True, level=3, active_skill_slot=1))
        for value in ['+10', '+24', '+12', '+3%', 'Steadfast Guard', 'Second Wind', 'Calm Foundation', '25', '50', '75']:
            self.assertIn(value, drawn)
        self.assertNotIn(YUKI['description'], drawn)

    def test_corrupt_fields_and_long_names(self):
        with patch.object(C, '_art', return_value=None):
            for corrupt in [None, [], {'steadfast-guard': 'oops'}, {'steadfast-guard': {}}, {'steadfast-guard': float('inf')}]:
                self.assertIsNotNone(C.render_avatar_info_card(YUKI, owned=True, skill_levels=corrupt))
            for bonus in [None, [], {'attack_flat': 'bad', 'hp_percent': float('nan')}]:
                card = {**YUKI, 'name': 'Very Long Name ' * 80, 'bonuses': bonus, 'skills': [None, {}, {'name': None}]}
                self.assertIsNotNone(C.render_avatar_info_card(card, owned=True))

    def test_missing_frame_returns_embed_fallback_signal(self):
        with patch.object(C, 'FRAME_PATH', '/does-not-exist'), self.assertLogs(C.log, level='ERROR'):
            self.assertIsNone(C.render_avatar_info_card(YUKI))

    def test_portrait_aspect_ratio_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'portrait.png')
            Image.new('RGB', (100, 400), 'red').save(path)
            art = C._art(path)
            self.assertIsNotNone(art)
            self.assertEqual(art.height, C.ART_BOX[3]-C.ART_BOX[1])
            self.assertAlmostEqual(art.width / art.height, .25, places=2)
            self.assertIsNot(art, C._art(path))
            self.assertIsNone(C._art('https://discord.com/channels/not-an-image'))

class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_and_existing_skill_controls(self):
        fake = SimpleNamespace(_resolve_avatar_query=lambda q:YUKI,
                               _get_owned_avatar_ids=lambda user:[YUKI['id']],
                               _get_equipped_id=AsyncMock(return_value=YUKI['id']))
        ctx = SimpleNamespace(author=SimpleNamespace(id=123), send=AsyncMock())
        profile = {'avatar': {'cards': {YUKI['id']: {'level': 3, 'skills': {}}}}}
        with patch('utils.database.get_user', AsyncMock(return_value=profile)), patch.object(C, '_art', return_value=None):
            await AvatarShop.avatar_info.callback(fake, ctx, query='Yuki')
        kwargs = ctx.send.call_args.kwargs
        self.assertEqual(kwargs['file'].filename, 'ainfo.jpg')
        self.assertEqual(kwargs['embed'].image.url, 'attachment://ainfo.jpg')
        self.assertIsInstance(kwargs['view'], AvatarSkillsView)
        self.assertTrue(any(child.label == 'Skills' for child in kwargs['view'].children))
        self.assertIn('ainfo', AvatarShop.avatar_info.aliases)

    async def test_render_failure_keeps_existing_embed_and_button(self):
        fake = SimpleNamespace(_resolve_avatar_query=lambda q:YUKI,
                               _get_owned_avatar_ids=lambda user:[],
                               _get_equipped_id=AsyncMock(return_value=None))
        ctx = SimpleNamespace(author=SimpleNamespace(id=123), send=AsyncMock())
        with patch.object(C, 'render_avatar_info_card', return_value=None):
            await AvatarShop.avatar_info.callback(fake, ctx, query='Yuki')
        kwargs = ctx.send.call_args.kwargs
        self.assertNotIn('file', kwargs)
        self.assertEqual(kwargs['embed'].thumbnail.url, YUKI['image'])
        self.assertIsInstance(kwargs['view'], AvatarSkillsView)

if __name__ == '__main__':
    unittest.main()
