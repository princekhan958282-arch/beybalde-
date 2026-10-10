"""Skill displays stay readable and agree with battle scaling after upgrades."""
import copy
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.avatar import avatar_collection as AC, avatar_progress as AP
from cogs.avatar.avatar_engine import avatar_engine
from cogs.avatar.avatar_scaling import scaled_card
from cogs.avatar.avatar_skill_display import skill_description, skill_preview
from cogs.avatar.avatar_utils import build_avatar_embed
from cogs.avatar import avatar_progression_ui as UI


class SkillDisplayTests(unittest.TestCase):
    def setUp(self):
        avatar_engine.load()
        self.card = avatar_engine.get_avatar('avatar_l003')

    def test_zero_two_values_match_battle_at_every_level(self):
        before = copy.deepcopy(self.card)
        for level in range(1, 11):
            profile = AC.migrate({}, [self.card['id']])
            AP._ensure(profile, self.card['id']).update(level=5, skills={
                AP.slugify(s['name']): level for s in self.card['skills']})
            battle = scaled_card(profile, self.card)
            for slot, key in [(1, 'attack_percent'), (2, 'charge_percent'), (3, 'special_move_percent')]:
                expected = f"{battle['skills'][slot - 1]['bonuses'][key] * 100:.2f}".rstrip('0').rstrip('.')
                self.assertIn(expected + '%', skill_description(self.card, slot, level))
        self.assertEqual(self.card, before)

    def test_details_and_preview_show_effect_and_selected_stats(self):
        levels = {AP.slugify('Partner Sync'): 3}
        embed = build_avatar_embed(self.card, owned=True, level=3,
                                   skill_levels=levels, active_skill_slot=1)
        skills = next(f.value for f in embed.fields if 'Skills' in f.name)
        self.assertIn('27.84%', skills)
        self.assertIn('+4 ATK / +4 DEF / +4 STM', skills)
        self.assertNotIn('Attack increased by 24%', skills)
        preview = skill_preview(self.card, 1, 3, 4)
        self.assertIn('27.84%', preview)
        self.assertIn('29.76%', preview)
        self.assertNotIn('×', preview)

    def test_rule_effects_scale_but_gates_chances_and_counters_do_not(self):
        daigo = next(c for c in avatar_engine.get_all_avatars() if c['name'] == 'Daigo Kurogami')
        self.assertIn('29% more damage', skill_description(daigo, 2, 3))
        self.assertIn('below 40% HP', skill_description(daigo, 2, 3))
        self.assertEqual(skill_description(daigo, 3, 3), daigo['skills'][2]['description'])
        ken = next(c for c in avatar_engine.get_all_avatars() if c['name'] == 'Ken Midori')
        text = skill_description(ken, 2, 3)
        self.assertIn('40% chance', text)
        self.assertIn('drain 1.74', text)

    def test_special_flags_and_integer_effects_remain_readable(self):
        dyrroth = next(c for c in avatar_engine.get_all_avatars() if c['name'] == 'Dyrroth')
        self.assertIn('Every 3 hits', skill_description(dyrroth, 1, 3))
        self.assertIn('23.2%', skill_description(dyrroth, 1, 3))
        self.assertIn('46.4–88.16%', skill_description(dyrroth, 2, 3))
        self.assertIn('first 3 rounds', skill_description(dyrroth, 2, 3))
        self.assertIn('128.76%', skill_description(dyrroth, 3, 3))
        self.assertIn('full Attack stat', skill_description(dyrroth, 3, 3))

    def test_roster_descriptions_do_not_mutate_battle_data(self):
        for card in avatar_engine.get_all_avatars():
            before = copy.deepcopy(card)
            for slot in range(1, len(card.get('skills', [])) + 1):
                for level in (1, 3, 10):
                    self.assertTrue(skill_description(card, slot, level))
            self.assertEqual(card, before)


class SkillDisplayUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        avatar_engine.load()
        self.card = avatar_engine.get_avatar('avatar_l003')
        aid = self.card['id']
        self.profile = AC.migrate({'coins': 1000000}, [aid])
        AP._ensure(self.profile, aid).update(level=3, skills={AP.slugify('Partner Sync'): 3})
        self.view = UI.ProgressionView(101, self.card, self.profile)
        self.interaction = SimpleNamespace(user=SimpleNamespace(id=101),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))
        self.read = patch.object(UI, 'get_user', AsyncMock(side_effect=lambda _: self.profile))
        self.read.start()
        self.addCleanup(self.read.stop)

    async def test_view_skill_and_upgrade_receipt_show_real_effects(self):
        await self.view.view_skill.callback(self.interaction)
        embed = self.interaction.response.send_message.call_args.kwargs['embed']
        self.assertIn('27.84%', embed.description)
        self.assertIn('29.76%', embed.fields[-1].value)
        async def mutate(owner, fn):
            return fn(self.profile)
        with patch.object(UI, 'mutate_user', AsyncMock(side_effect=mutate)):
            await self.view.skill_up.callback(self.interaction)
        text = self.interaction.followup.send.call_args.args[0]
        self.assertIn('Lv3 → Lv4', text)
        self.assertIn('27.84%', text)
        self.assertIn('29.76%', text)
        self.assertNotIn('×', text)
        self.assertIn('29.76%', next(f.value for f in self.view.details_embed.fields if 'Skills' in f.name))

    async def test_repeated_refreshes_clear_old_panel_and_preserve_fallback(self):
        self.view.message = SimpleNamespace(edit=AsyncMock())
        with patch('utils.avatar_info_card.resolve_avatar_image_url', AsyncMock(return_value=None)), \
             patch('utils.avatar_info_card.render_avatar_info_card', side_effect=lambda *a, **k: io.BytesIO(b'image')):
            for action in ('level', 'skills', 'star'):
                self.view.action = action
                await self.view.refresh_message()
                kwargs = self.view.message.edit.call_args.kwargs
                self.assertIsNone(kwargs['embed'])
                self.assertIsNone(kwargs['content'])
                self.assertEqual(kwargs['attachments'][0].filename, 'ainfo.jpg')
        with patch('utils.avatar_info_card.resolve_avatar_image_url', AsyncMock(return_value=None)), \
             patch('utils.avatar_info_card.render_avatar_info_card', return_value=None):
            await self.view.refresh_message()
            kwargs = self.view.message.edit.call_args.kwargs
            self.assertEqual(kwargs['attachments'], [])
            text = '\n'.join(f.value for f in kwargs['embed'].fields)
            self.assertIn('27.84%', text)
            self.assertNotIn('×', text)


if __name__ == '__main__':
    unittest.main()
