#!/usr/bin/env python3
"""Targeted ;ainfo render and Discord attachment checks; no live Discord needed."""

def authored_open(*args, **kwargs):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from utils.character_registry import authored_open as open_registry
    return open_registry(*args, **kwargs)

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
from discord.ext.commands.view import StringView

CARDS = json.loads(authored_open('avatar').getvalue())['avatars']
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

    def test_school_blader_always_on_stats_match_engine(self):
        from cogs.avatar import avatar_skills as AS
        from cogs.avatar.avatar_utils import build_avatar_embed
        shu = next(c for c in CARDS if c['id'] == 'avatar_s108')
        self.assertEqual(C.permanent_bonuses(shu), AS.bonuses_for(shu, 0))
        bonuses = C.permanent_bonuses(shu)
        self.assertEqual(bonuses['attack_flat'], 45)
        self.assertEqual(bonuses['attack_percent'], .2)
        self.assertEqual(bonuses['defence_flat'], 40)
        self.assertEqual(bonuses['stamina_flat'], 43)
        embed = build_avatar_embed(shu, compact=True)
        self.assertTrue(any('ATK' in f.value and '+45' in f.value for f in embed.fields))

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
    async def test_discord_parser_accepts_empty_argument(self):
        ctx = SimpleNamespace(message=SimpleNamespace(attachments=[]), view=StringView(''))
        await AvatarShop.avatar_info._parse_arguments(ctx)
        self.assertEqual(ctx.kwargs, {'query': None})

    async def test_no_query_displays_current_equipped_avatar(self):
        fake = SimpleNamespace(bot=None, _resolve_avatar_query=lambda q:self.fail('Should resolve equipped ID directly'),
                               _get_owned_avatar_ids=lambda user:[YUKI['id']],
                               _get_equipped_id=AsyncMock(return_value=YUKI['id']))
        ctx = SimpleNamespace(author=SimpleNamespace(id=123), send=AsyncMock())
        with patch('utils.database.get_user', AsyncMock(return_value={})), patch.object(C, 'render_avatar_info_card', return_value=None):
            await AvatarShop.avatar_info.callback(fake, ctx)
        kwargs = ctx.send.call_args.kwargs
        self.assertIn('Yuki', kwargs['embed'].title)
        self.assertIsInstance(kwargs['view'], AvatarSkillsView)
        fake._get_equipped_id.assert_awaited_once_with(123)
        self.assertFalse(AvatarShop.avatar_info.clean_params['query'].required)

    async def test_no_equipped_or_invalid_id_gives_guidance(self):
        for equipped in [None, '', 'removed-avatar-id', []]:
            fake = SimpleNamespace(bot=None, _get_equipped_id=AsyncMock(return_value=equipped))
            ctx = SimpleNamespace(author=SimpleNamespace(id=123), send=AsyncMock())
            await AvatarShop.avatar_info.callback(fake, ctx)
            text = ctx.send.call_args.args[0]
            self.assertIn(';equipavatar', text)
            self.assertIn(';ainfo <name or id>', text)

    async def test_named_lookup_preserved_with_no_equipped_avatar(self):
        fake = SimpleNamespace(bot=None, _resolve_avatar_query=lambda q:YUKI,
                               _get_owned_avatar_ids=lambda user:[],
                               _get_equipped_id=AsyncMock(return_value=None))
        ctx = SimpleNamespace(author=SimpleNamespace(id=123), send=AsyncMock())
        with patch.object(C, 'render_avatar_info_card', return_value=None):
            await AvatarShop.avatar_info.callback(fake, ctx, query='Yuki')
        self.assertIn('Yuki', ctx.send.call_args.kwargs['embed'].title)

    async def test_attachment_and_existing_skill_controls(self):
        fake = SimpleNamespace(bot=None, _resolve_avatar_query=lambda q:YUKI,
                               _get_owned_avatar_ids=lambda user:[YUKI['id']],
                               _get_equipped_id=AsyncMock(return_value=YUKI['id']))
        ctx = SimpleNamespace(author=SimpleNamespace(id=123), send=AsyncMock())
        profile = {'avatar': {'cards': {YUKI['id']: {'level': 3, 'skills': {}}}}}
        with patch('utils.database.get_user', AsyncMock(return_value=profile)), patch.object(C, '_art', return_value=None):
            await AvatarShop.avatar_info.callback(fake, ctx, query='Yuki')
        kwargs = ctx.send.call_args.kwargs
        self.assertEqual(kwargs['file'].filename, 'ainfo.jpg')
        self.assertIn('embed', kwargs)
        self.assertTrue(any(getattr(child, 'label', '') == 'Level Up' for child in kwargs['view'].children))
        self.assertTrue(any(child.label == 'Details' for child in kwargs['view'].children))
        self.assertIsInstance(kwargs['view'], AvatarSkillsView)
        self.assertTrue(any(child.label == 'Skills' for child in kwargs['view'].children))
        self.assertIn('ainfo', AvatarShop.avatar_info.aliases)

    async def test_render_failure_keeps_existing_embed_and_button(self):
        fake = SimpleNamespace(bot=None, _resolve_avatar_query=lambda q:YUKI,
                               _get_owned_avatar_ids=lambda user:[],
                               _get_equipped_id=AsyncMock(return_value=None))
        ctx = SimpleNamespace(author=SimpleNamespace(id=123), send=AsyncMock())
        with patch.object(C, 'render_avatar_info_card', return_value=None):
            await AvatarShop.avatar_info.callback(fake, ctx, query='Yuki')
        kwargs = ctx.send.call_args.kwargs
        self.assertNotIn('file', kwargs)
        self.assertEqual(kwargs['embed'].thumbnail.url, YUKI['image'])
        self.assertIsInstance(kwargs['view'], AvatarSkillsView)

class ArtworkRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        C.clear_cache()

    async def test_unsigned_url_recovers_matching_attachment_and_caches(self):
        source = 'https://cdn.discordapp.com/attachments/123/456/avatar.png'
        fresh = source+'?ex=ffffffff&is=abc&hm=signed'
        async def history(**kwargs):
            self.assertEqual(kwargs['around'].id, 456)
            self.assertEqual(kwargs['limit'], 20)
            yield SimpleNamespace(attachments=[SimpleNamespace(id=999,url='other'), SimpleNamespace(id=456,url=fresh)])
        channel = SimpleNamespace(history=history)
        bot = SimpleNamespace(get_channel=lambda cid:channel)
        self.assertEqual(await C.resolve_avatar_image_url(bot, source), fresh)
        self.assertEqual(await C.resolve_avatar_image_url(None, source), fresh)

    async def test_valid_signed_url_is_unchanged(self):
        source = 'https://media.discordapp.net/attachments/123/456/avatar.png?ex=ffffffff&hm=signed'
        self.assertEqual(await C.resolve_avatar_image_url(None, source), source)

    async def test_expired_url_missing_channel_and_non_discord_fallback(self):
        source = 'https://cdn.discordapp.com/attachments/123/456/avatar.png?ex=1&hm=expired'
        bot = SimpleNamespace(get_channel=lambda cid:None, fetch_channel=AsyncMock(side_effect=PermissionError()))
        self.assertEqual(await C.resolve_avatar_image_url(bot, source), source)
        for other in ['assets/avatars/example.png', 'https://example.org/avatar.png', None]:
            self.assertEqual(await C.resolve_avatar_image_url(None, other), other)

    async def test_details_button_preserves_original_info_ephemerally(self):
        from cogs.avatar.avatar_utils import build_avatar_embed
        embed = build_avatar_embed(YUKI)
        view = AvatarSkillsView(YUKI, details_embed=embed)
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await view.show_details.callback(interaction)
        interaction.response.send_message.assert_awaited_once_with(embed=embed, ephemeral=True)

if __name__ == '__main__':
    unittest.main()
