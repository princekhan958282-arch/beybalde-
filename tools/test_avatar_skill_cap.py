"""Global skill cap, old saves, purchase ceiling and roster-wide scaling."""
import copy
import unittest
from cogs.avatar import avatar_collection as AC, avatar_progress as AP, avatar_levels as AL
from cogs.avatar.avatar_engine import avatar_engine
from cogs.avatar.avatar_scaling import scaled_card


class SkillCapTests(unittest.TestCase):
    def test_progression_is_linear_and_bounded(self):
        for level, expected in enumerate((1, 1.025, 1.05, 1.075, 1.1), 1):
            self.assertAlmostEqual(AL.skill_magnitude_mult(level), expected)
        self.assertEqual([AL.max_skill_level_for(i) for i in range(1, 6)], [2, 4, 5, 5, 5])
        self.assertAlmostEqual(AL.skill_magnitude_mult(1000), 1.1)

    def test_old_max_skills_cannot_buy_more_or_exceed_new_cap_on_any_avatar(self):
        avatar_engine.load()
        for card in avatar_engine.get_all_avatars():
            with self.subTest(avatar=card['id']):
                p = AC.migrate({'coins': 1000000}, [card['id']])
                slugs = [AP.slugify(s['name']) for s in card.get('skills', [])]
                entry = AP._ensure(p, card['id'])
                entry.update(level=5, skills=dict.fromkeys(slugs, 10))
                before = copy.deepcopy(p)
                capped = scaled_card(p, card)
                for slug in slugs:
                    self.assertEqual(AP.skill_level(p, card['id'], slug), 5)
                    with self.assertRaisesRegex(AP.PurchaseError, 'maximum, Lv5'):
                        AP.apply_skill_purchase(p, card['id'], slug)
                self.assertEqual(p, before)
                entry['skills'] = dict.fromkeys(slugs, 5)
                self.assertEqual(capped, scaled_card(p, card))


if __name__ == '__main__':
    unittest.main()
