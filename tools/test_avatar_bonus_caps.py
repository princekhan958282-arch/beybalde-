"""Avatar progression must increase damage consistently until the Bey cap."""
import copy
import unittest
from tools import sim_story as H
from cogs.avatar.avatar_engine import avatar_engine, AvatarBonuses
from cogs.avatar import avatar_progress as AP
from cogs.avatar.avatar_scaling import scaled_card
from cogs.avatar.avatar_skill_display import skill_description
from utils.loadout import effective_blade
from cogs.battle.purification import effective_stats
from cogs.battle.type_gimmicks import passive_stat_multiplier
from cogs.battle.damage_rules import calc_damage


class BonusCaps(unittest.IsolatedAsyncioTestCase):
    async def build(self, level=1, star=1, skill=1, attack=1000):
        avatar_engine.load()
        card = avatar_engine.get_avatar('avatar_x003')
        p = H.seed_profile(101, equipped_avatar=card['id'], avatar_skill={card['id']: 1},
                           avatar_inventory=[card['id']])
        AP._ensure(p, card['id']).update(level=level, stars=star,
            skills={AP.slugify(card['skills'][0]['name']): skill})
        H.STORE.data['101'] = copy.deepcopy(p)
        H.seed_profile(102)
        blade = {'name': 'Cap fixture', 'type': 'Balance', 'abilities': [],
                 'stats': {'attack': attack, 'defense': 1000, 'stamina': 1000, 'hp': 100}}
        s = await H.BattleSession.create(None, H.FakeChannel(), H.FakePlayer(101, 'A'),
            H.FakePlayer(102, 'B'), blade, copy.deepcopy(blade), payout=False)
        return s, p, card

    async def test_each_progression_source_increases_damage_below_cap(self):
        baseline = (await self.build())[0]
        base_atk = effective_stats(baseline, '101')['attack']
        for args in ({'level': 2}, {'star': 2}, {'skill': 2}):
            s, _, _ = await self.build(**args)
            atk = effective_stats(s, '101')['attack']
            def damage(attack):
                return calc_damage('attack', {'attack': attack, 'level': 100},
                                   {'defense': 100}, s.blades['101'], 'stamina')[0]
            self.assertGreater(damage(atk), damage(base_atk))
            if 'skill' in args:
                self.assertGreater(s.avatar_bonuses['101'].nth_hit_bonus(3, atk),
                                   baseline.avatar_bonuses['101'].nth_hit_bonus(3, base_atk))

    async def test_max_progression_card_and_battle_agree_at_cap(self):
        s, p, _ = await self.build(level=5, star=15, skill=10, attack=100)
        blade, breakdown, _ = await effective_blade(101, p, s.blades['101'])
        self.assertEqual(blade['stats']['attack'], 180)
        self.assertEqual(breakdown['attack']['avatar'], 80)
        self.assertEqual(effective_stats(s, '101')['attack'],
                         180 * passive_stat_multiplier('Balance', 'attack'))
        self.assertEqual(p['avatar']['cards']['avatar_x003']['stars'], 15)

    async def test_skill_upgrade_preserves_hit_interval_and_rounds(self):
        _, p, card = await self.build(level=5, skill=10)
        for skill in card['skills']:
            p['avatar']['cards'][card['id']]['skills'][AP.slugify(skill['name'])] = 10
        upgraded = scaled_card(p, card)
        self.assertEqual(upgraded['skills'][0]['bonuses']['nth_hit_interval'], 3)
        self.assertEqual(upgraded['skills'][1]['bonuses']['defence_break_rounds'], 3)
        self.assertEqual(upgraded['skills'][1]['bonuses']['defence_break_max'], 1)
        self.assertIn('Every 3 hits', skill_description(card, 1, 10))

    async def test_flat_gains_are_added_once_and_cap_scales_with_bey(self):
        av = AvatarBonuses(attack_flat=20, attack_percent=.3,
                           defence_flat=10000, stamina_flat=10000)
        self.assertEqual(av.apply_attack_bonus(100), 150)
        for base in (0, 10, 100, 1000):
            self.assertEqual(av.apply_defence_bonus(base), 2 * base)
            self.assertEqual(av.apply_stamina_bonus(base), 2 * base)

    async def test_each_roster_avatar_uses_its_own_cap(self):
        avatar_engine.load()
        caps = set()
        for card in avatar_engine.get_all_avatars():
            cap = card['stat_bonus_cap']
            self.assertGreater(cap, 0)
            self.assertLessEqual(cap, 1)
            caps.add(cap)
            profile = H.seed_profile(101, equipped_avatar=card['id'],
                                     avatar_inventory=[card['id']])
            AP._ensure(profile, card['id']).update(level=5, stars=15)
            H.STORE.data['101'] = copy.deepcopy(profile)
            av = await avatar_engine.get_battle_bonuses(101)
            self.assertEqual(av.stat_bonus_cap, cap, card['name'])
            for method in ('apply_attack_bonus', 'apply_defence_bonus', 'apply_stamina_bonus'):
                self.assertAlmostEqual(getattr(av, method)(100), 100 * (1 + cap), msg=card['name'])
            av.hp_flat = 100000
            self.assertAlmostEqual(av.apply_hp_bonus(2000), 2000 * (1 + cap))
        self.assertGreater(len(caps), 1)
        self.assertEqual(AvatarBonuses(attack_flat=10000, stat_bonus_cap=12).apply_attack_bonus(100), 200)

    async def test_hp_flat_and_percent_share_cap(self):
        self.assertEqual(AvatarBonuses(hp_flat=100, hp_percent=.1).apply_hp_bonus(2000), 2300)
        for pool in (0, 100, 2000):
            self.assertEqual(AvatarBonuses(hp_flat=10000, hp_percent=.5).apply_hp_bonus(pool), 2 * pool)
            self.assertEqual(AvatarBonuses(hp_percent=12).apply_hp_bonus(pool), 2 * pool)

    async def test_hp_cap_matches_pvp_boss_and_display(self):
        from unittest.mock import AsyncMock, patch
        from cogs.battle.boss import boss_battle as BB
        from utils.loadout import battle_pool
        with patch.object(avatar_engine, 'get_battle_bonuses',
                          new=AsyncMock(return_value=AvatarBonuses())):
            base, _, _ = await self.build()
            plain_boss, _ = await BB._player_fighter(101)
        with patch.object(avatar_engine, 'get_battle_bonuses',
                          new=AsyncMock(return_value=AvatarBonuses(hp_flat=100000))):
            capped, _, _ = await self.build()
            shown = await battle_pool(101, capped.blades['101'])
            boss, _ = await BB._player_fighter(101)
        self.assertAlmostEqual(capped.max_hp_per_player['101'], 2 * base.max_hp_per_player['101'], delta=1)
        self.assertEqual(shown, capped.max_hp_per_player['101'])
        self.assertEqual(boss.max_hp, 2 * plain_boss.max_hp)


if __name__ == '__main__':
    unittest.main()
