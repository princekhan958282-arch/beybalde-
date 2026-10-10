"""Exercise the ;ainfo action menu and clickable requirement feedback."""
import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from cogs.avatar import avatar_collection as AC, avatar_config as C, avatar_progress as AP
from cogs.avatar.avatar_engine import avatar_engine
from cogs.avatar import avatar_progression_ui as UI

AID = 'avatar_og_tyson'


class ActionMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        avatar_engine.load()
        self.card = avatar_engine.get_avatar(AID)
        self.profile = AC.migrate({'coins': 0, 'equipped_avatar': AID}, [AID])
        self.get_patch = patch.object(UI, 'get_user', AsyncMock(side_effect=lambda _: self.profile))
        self.get_patch.start()

        async def mutate(owner, fn):
            return fn(self.profile)

        self.mutate_patch = patch.object(UI, 'mutate_user', AsyncMock(side_effect=mutate))
        self.mutate_patch.start()
        self.addCleanup(self.get_patch.stop)
        self.addCleanup(self.mutate_patch.stop)
        self.view = UI.ProgressionView(101, self.card, self.profile)
        self.interaction = SimpleNamespace(
            user=SimpleNamespace(id=101),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        )

    def labels(self):
        return [item.label for item in self.view.children if isinstance(item, discord.ui.Button)]

    async def choose(self, action):
        select = next(item for item in self.view.children if isinstance(item, UI.ActionSelect))
        select._values = [action]
        await select.callback(self.interaction)

    async def test_initial_view_and_switches_replace_controls(self):
        self.assertEqual(self.labels(), ['Details'])
        self.assertEqual(len(self.view.children), 2)
        select = self.view.children[1]
        self.assertEqual([o.label for o in select.options], ['Level Up', 'Skills', 'Star Up / Feed'])
        for action, labels in [('level', ['Details', 'Level Up']),
                               ('skills', ['Details', 'View Skill', 'Skill Level Up']),
                               ('star', ['Details', 'Feed (0/5)']),
                               ('level', ['Details', 'Level Up'])]:
            await self.choose(action)
            self.assertEqual(self.labels(), labels)
            self.assertTrue(all(not c.disabled for c in self.view.children))
            self.assertEqual(any(isinstance(c, UI.SkillSelect) for c in self.view.children), action == 'skills')
            menu = next(c for c in self.view.children if isinstance(c, UI.ActionSelect))
            self.assertEqual([o.value for o in menu.options if o.default], [action])
            # Discord serialization: buttons share row 0, selects have their own rows.
            rows = self.view.to_components()
            self.assertEqual(len(rows), 3 if action == 'skills' else 2)
            self.assertEqual([c['type'] for c in rows[0]['components']], [2] * len(labels))

    async def test_level_click_explains_coins_and_maximum_without_spending(self):
        await self.choose('level')
        before = copy.deepcopy(self.profile)
        await self.view.level_up.callback(self.interaction)
        text = self.interaction.followup.send.call_args.args[0]
        self.assertIn('coins', text)
        self.assertIn('short', text)
        self.assertEqual(self.profile, before)
        AP._ensure(self.profile, AID)['level'] = C.MAX_CARD_LEVEL
        self.view.configure(self.profile)
        self.assertFalse(self.view.level_up.disabled)
        await self.view.level_up.callback(self.interaction)
        self.assertIn('maximum', self.interaction.followup.send.call_args.args[0])

    async def test_skill_picker_drives_view_and_upgrade_feedback(self):
        await self.choose('skills')
        picker = next(c for c in self.view.children if isinstance(c, UI.SkillSelect))
        picker._values = ['2']
        await picker.callback(self.interaction)
        await self.view.view_skill.callback(self.interaction)
        embed = self.interaction.response.send_message.call_args.kwargs['embed']
        self.assertIn(self.card['skills'][1]['name'], embed.title)
        self.assertEqual(embed.description, self.card['skills'][1]['description'])
        AP._ensure(self.profile, AID)['skills'][AP.slugify(self.card['skills'][1]['name'])] = 2
        await self.view.skill_up.callback(self.interaction)
        self.assertIn('raise the avatar', self.interaction.followup.send.call_args.args[0])
        self.assertFalse(self.view.skill_up.disabled)
        AP._ensure(self.profile, AID)['level'] = 2
        await self.view.skill_up.callback(self.interaction)
        self.assertIn('short', self.interaction.followup.send.call_args.args[0])

    async def test_combined_button_explains_missing_materials_then_opens_feed(self):
        await self.choose('star')
        self.assertFalse(self.view.star_feed.disabled)
        await self.view.star_feed.callback(self.interaction)
        self.assertIn('No eligible same-category spare cards',
                      self.interaction.response.send_message.call_args.args[0])
        self.profile['avatar_copies'][AID] = 5
        await self.view.star_feed.callback(self.interaction)
        selection = self.interaction.response.send_message.call_args.kwargs['view']
        self.assertIsInstance(selection, UI.FeedSelection)
        self.assertEqual(selection.expected, 0)
        self.assertEqual(selection.materials, {AID: 5})

    async def test_fresh_feed_state_routes_to_risky_confirmation(self):
        await self.choose('star')
        AP._ensure(self.profile, AID).update(stars=5, feeding=25)
        # No configure here: callback must notice feeding done in another view.
        await self.view.star_feed.callback(self.interaction)
        confirmation = self.interaction.response.send_message.call_args.kwargs['view']
        self.assertIsInstance(confirmation, UI.StarConfirm)
        self.assertEqual(confirmation.target, 6)
        self.assertEqual(confirmation.timeout, 30)
        self.assertIn('PERMANENTLY LOSE', confirmation.embed(self.profile).description)
        self.assertEqual(self.labels(), ['Details', 'Try 6★'])

    async def test_pending_and_max_stars_remain_clickable_and_explain(self):
        await self.choose('star')
        for star, reason in [(8, 'pending approval'), (15, 'already at 15★')]:
            AP._ensure(self.profile, AID).update(stars=star, feeding=0)
            self.view.configure(self.profile)
            before = copy.deepcopy(self.profile)
            self.assertFalse(self.view.star_feed.disabled)
            await self.view.star_feed.callback(self.interaction)
            self.assertIn(reason, self.interaction.response.send_message.call_args.args[0])
            self.assertEqual(before, self.profile)

    async def test_upgrades_keep_selection_and_transition_feed_to_star(self):
        await self.choose('level')
        self.profile['coins'] = 1000000
        await self.view.level_up.callback(self.interaction)
        self.assertEqual(AP.card_level(self.profile, AID), 2)
        self.assertEqual(self.labels(), ['Details', 'Level Up'])
        await self.choose('skills')
        self.view.slot = 2
        await self.view.skill_up.callback(self.interaction)
        self.assertEqual(AP.skill_level(self.profile, AID, AP.slugify(self.card['skills'][1]['name'])), 2)
        await self.choose('star')
        self.profile['avatar_copies'][AID] = 5
        await self.view.apply(self.interaction, lambda p: AC.feed(p, AID, 0, {AID: 5}, {AID: self.card}))
        self.assertEqual(self.labels(), ['Details', 'Star Up'])
        self.assertEqual(AC.stages(self.profile, AID), 5)

    async def test_owner_expiry_and_lost_avatar_protection(self):
        self.interaction.user.id = 102
        self.assertFalse(await self.view.interaction_check(self.interaction))
        self.interaction.user.id = 101
        self.assertTrue(await self.view.interaction_check(self.interaction))
        self.view.deadline = time.monotonic() - 1
        self.assertFalse(await self.view.interaction_check(self.interaction))
        self.view.configure(self.profile)
        self.assertTrue(all(c.disabled for c in self.view.children))
        other = UI.ProgressionView(101, self.card, self.profile)
        self.profile['avatar_inventory'] = []
        other.configure(self.profile)
        self.assertTrue(other.closed)
        self.assertTrue(all(c.disabled for c in other.children))

    async def test_avatar_without_skills_has_no_empty_picker(self):
        card = dict(self.card, skills=[])
        view = UI.ProgressionView(101, card, self.profile)
        menu = next(c for c in view.children if isinstance(c, UI.ActionSelect))
        self.assertEqual([o.value for o in menu.options], ['level', 'star'])


if __name__ == '__main__':
    unittest.main()
