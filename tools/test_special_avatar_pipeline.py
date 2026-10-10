"""Check avatar Special amplification through real roster damage paths."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import sim_story as H
from cogs.avatar.avatar_engine import avatar_engine
from cogs.battle.purification import effective_stats
from utils.character_registry import load_beys


class SpecialPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def build(self, blade, aid=None):
        self.assertIsNotNone(blade)
        avatar_engine.load()
        H.seed_profile(101, equipped_avatar=aid, avatar_skill={aid: 3}, avatar_energy=100)
        H.seed_profile(102)
        target = {'name': 'Special audit target', 'type': 'Balance',
                  'stats': {'hp': 10000, 'attack': 100, 'defense': 100, 'stamina': 100},
                  'abilities': []}
        s = await H.BattleSession.create(None, H.FakeChannel(),
            H.FakePlayer(101, 'Attacker'), H.FakePlayer(102, 'Target'),
            copy.deepcopy(blade), target, ranked=False, payout=False)
        s.type_gimmicks.rng = lambda: .99
        s.moves = {'101': 'special', '102': 'charge'}
        return s

    def resolve(self, s):
        before = s.hp['102']
        with patch('cogs.abilities.ability_engine.random.random', return_value=.99), patch('cogs.battle.type_gimmicks.random.random', return_value=.99):
            damage, logs = s.attack_manager._resolve_special(
                '101', '102', 'special', s.blades['101'], s.blades['102'], [])
        return damage, before - s.hp['102'], logs

    async def test_native_damage_plus_dsl_damage_all_amplified_once(self):
        blade = {'name': 'Mixed Special fixture', 'type': 'Balance',
            'stats': {'hp': 100, 'attack': 100, 'defense': 100, 'stamina': 100},
            'special_move': {'hits': 1, 'damage_per_hit': 100},
            'abilities': [{'name': 'Special extra damage', 'rules': [
                {'when': 'on_special', 'do': [{'op': 'bonus_damage', 'value': 100}]}]}]}
        plain = await self.build(blade)
        boosted = await self.build(blade, 'avatar_x003')
        base, _, _ = self.resolve(plain)
        damage, _, logs = self.resolve(boosted)
        self.assertEqual(base, 200)
        self.assertEqual(damage, round(200 * 2.11) + round(effective_stats(boosted, '101')['attack']))
        self.assertEqual(sum('full Attack stat added' in l for l in logs), 1)

    async def test_special_true_damage_scales_but_unrelated_proc_does_not(self):
        blade = {'name': 'Special true damage fixture', 'type': 'Balance',
            'stats': {'hp': 100, 'attack': 100, 'defense': 100, 'stamina': 100},
            'special_move': {'hits': 1, 'damage_per_hit': 100},
            'abilities': [{'name': 'True damage effects', 'rules': [
                {'when': 'on_special', 'do': [{'op': 'true_damage', 'value': 100},
                    {'op': 'true_damage_stat_pct', 'stat': 'attack', 'scale': 1}]},
                {'when': 'passive', 'do': [{'op': 'true_damage', 'value': 50}]}]}]}
        plain = await self.build(blade)
        boosted = await self.build(blade, 'avatar_x003')
        _, direct, _ = self.resolve(plain)
        _, amplified, logs = self.resolve(boosted)
        stat = boosted.battle_stats['101']['attack']
        # The passive 50 is independent even when fired on a Special turn.
        self.assertEqual(direct, 150 + round(plain.battle_stats['101']['attack']))
        self.assertEqual(amplified, 50 + 211 + round(round(stat) * 2.11))
        self.assertEqual(sum('Special true damage' in l for l in logs), 2)

    async def test_eudora_extra_hits_copy_generated_special_payload(self):
        for name in ('Xeno Xcalius', 'Astral Valkyrie — Starbreaker', 'Drakoryn'):
            blade = H.get_beyblade(name)
            plain = await self.build(blade)
            boosted = await self.build(blade, 'avatar_mlbb004')
            raw, _, _ = self.resolve(plain)
            damage, _, logs = self.resolve(boosted)
            if name == 'Xeno Xcalius':
                self.assertEqual(damage, round(raw * effective_stats(boosted, '101')['attack'] / effective_stats(plain, '101')['attack']) * 3)
                # Its once-per-battle rule cannot replay an old extra-hit payload.
                self.assertEqual(self.resolve(boosted)[0], 0)
            else:
                self.assertGreater(damage, raw)
            self.assertTrue(any('3-hit barrage' in l for l in logs))
        support = await self.build(H.get_beyblade('Deep Caynox'), 'avatar_mlbb004')
        self.assertEqual(self.resolve(support)[0], 0)

    async def test_entire_roster_specials_have_no_missing_avatar_amplification(self):
        blades = list(load_beys().values())
        self.assertEqual(len(blades), 164)
        for blade in blades:
            with self.subTest(blade=blade['name']):
                plain = await self.build(blade)
                boosted = await self.build(blade, 'avatar_x003')
                raw, direct, _ = self.resolve(plain)
                damage, direct_boosted, logs = self.resolve(boosted)
                if raw > 0:
                    self.assertGreater(damage, raw)
                    self.assertEqual(sum('full Attack stat added' in l for l in logs), 1)
                elif direct == 0:
                    self.assertEqual(damage, 0)
                if direct > 0:
                    self.assertGreaterEqual(direct_boosted, direct)


if __name__ == '__main__':
    unittest.main()
