"""All avatars, including legacy bonus data, contribute zero maximum HP."""
import copy
import unittest
from cogs.avatar import avatar_skills as AS
from cogs.avatar.avatar_engine import avatar_engine, AvatarBonuses
from cogs.avatar.avatar_utils import format_bonuses_summary


class AvatarNoHP(unittest.TestCase):
    def test_entire_roster_has_zero_hp_in_every_skill_slot(self):
        avatar_engine.load()
        for card in avatar_engine.get_all_avatars():
            original=copy.deepcopy(card)
            for slot in (0,1,2,3):
                with self.subTest(avatar=card['name'],slot=slot):
                    block=AS.bonuses_for(card,slot)
                    self.assertEqual((block['hp_flat'],block['hp_percent']),(0,0))
                    bonus=avatar_engine.bonuses_from_block(block)
                    self.assertEqual((bonus.hp_flat,bonus.hp_percent),(0,0))
                    self.assertEqual(bonus.apply_hp_bonus(2000),2000)
            self.assertEqual(card,original)

    def test_legacy_and_future_hp_bonus_inputs_are_inert(self):
        card={'bonuses':{'hp_flat':100000,'hp_percent':1},'skills':[
            {'name':'HP skill','bonuses':{'hp_flat':100000,'hp_percent':1}}]}
        self.assertEqual(AS.bonuses_for(card,1)['hp_percent'],0)
        bonus=avatar_engine.bonuses_from_block(card['bonuses'])
        self.assertEqual((bonus.hp_flat,bonus.hp_percent),(0,0))
        legacy=AvatarBonuses(hp_flat=100000,hp_percent=1)
        self.assertFalse(legacy.has_any_bonus)
        for hp in (0,100,2000):self.assertEqual(legacy.apply_hp_bonus(hp),hp)

    def test_shop_summary_hides_hp_and_keeps_other_bonuses(self):
        text=format_bonuses_summary({'hp_flat':100000,'hp_percent':1,'attack_percent':.2})
        self.assertNotIn('HP',text)
        self.assertIn('ATK',text)
        self.assertIn('20%',text)


if __name__=='__main__':unittest.main()
