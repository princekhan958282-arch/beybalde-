"""All avatars and bit beasts have HP within their designated type bands."""
import copy
import unittest
from cogs.avatar import avatar_skills as AS
from cogs.avatar.avatar_engine import avatar_engine, AvatarBonuses
from cogs.avatar.avatar_utils import format_bonuses_summary
from utils.hp_system import HP_PER_STAT


class AvatarNoHP(unittest.TestCase):
    def test_entire_roster_has_valid_hp_in_every_skill_slot(self):
        avatar_engine.load()
        bands = {
            'attack': (50, 80),
            'defense': (70, 90),
            'stamina': (100, 110),
            'balance': (80, 100),
        }
        for card in avatar_engine.get_all_avatars():
            original = copy.deepcopy(card)
            t = card['type'].lower()
            lo, hi = bands[t]
            hp = card['bonuses']['hp_flat']
            self.assertTrue(lo <= hp <= hi, f"{card['name']} ({t}): {hp} not in [{lo}, {hi}]")
            for slot in (0, 1, 2, 3):
                with self.subTest(avatar=card['name'], slot=slot):
                    block = AS.bonuses_for(card, slot)
                    self.assertEqual(block['hp_flat'], hp)
                    bonus = avatar_engine.bonuses_from_block(block)
                    self.assertEqual(bonus.hp_flat, hp)
                    expected_hp = 2000 + hp * HP_PER_STAT + (2000 * block.get('hp_percent', 0.0))
                    self.assertEqual(bonus.apply_hp_bonus(2000), expected_hp)
            self.assertEqual(card, original)

    def test_bit_beasts_hp_matches_type_bands(self):
        avatar_engine.load()
        bb_expected = {
            'avatar_bb_dragoon': (50, 80),
            'avatar_bb_wonder_valkyrie': (50, 80),
            'avatar_bb_omega_whisper': (70, 90),
            'avatar_bb_dranzer': (80, 100),
            'avatar_bb_driger': (80, 100),
        }
        for bb_id, (lo, hi) in bb_expected.items():
            card = avatar_engine.get_avatar(bb_id)
            self.assertIsNotNone(card)
            hp = card['bonuses']['hp_flat']
            self.assertTrue(lo <= hp <= hi, f"{card['name']}: {hp} not in [{lo}, {hi}]")

    def test_shop_summary_displays_hp_and_keeps_other_bonuses(self):
        text = format_bonuses_summary({'hp_flat': 70, 'hp_percent': 0.05, 'attack_percent': .2})
        self.assertIn('HP', text)
        self.assertIn('+70', text)
        self.assertIn('ATK', text)
        self.assertIn('+20%', text)


if __name__ == '__main__':
    unittest.main()
