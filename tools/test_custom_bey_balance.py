"""Balance enforcement for saved Customs and repeated battle activations."""
import copy
import unittest
from unittest.mock import AsyncMock, patch
from utils.custom_bey import balanced_definition, ABILITY_COOLDOWN, ABILITY_PRESETS
from tools.test_custom_bey_v2 import blade
from tools.test_october_bey_roster import build as battle, P, E


class CustomBalance(unittest.TestCase):
    def fire(self, s, b):
        rule = b['abilities'][0]
        return s.ability._fire(rule['trigger'], P, E, b, 'attack', 'win', 100, 0, [])

    def test_old_saved_presets_refresh_without_mutating_identity_stats_or_profile(self):
        for key in ABILITY_PRESETS:
            with self.subTest(key=key):
                b = blade([key])
                b['approval_status'] = 'approved'
                b['submitted_guild_id'] = 42
                b['abilities'][0]['name'] = 'Player Named Ability'
                b['abilities'][0]['rules'][0]['do'][0] = {'op': 'bonus_damage', 'value': 9999}
                b['custom_meta']['rules_version'] = 2
                before = copy.deepcopy(b)
                fixed = balanced_definition(b)
                self.assertEqual(b, before)
                self.assertEqual(fixed['stats'], before['stats'])
                self.assertEqual(fixed['approval_status'], 'approved')
                self.assertEqual(fixed['abilities'][0]['name'], 'Player Named Ability')
                self.assertEqual(fixed['abilities'][0]['rules'][0]['do'][0], ABILITY_PRESETS[key]['op'])
                self.assertEqual(balanced_definition(fixed), fixed)
                s, _ = battle('Dranzer')
                s.blades[P] = b
                self.fire(s, b)
                # Engine compilation enforces current rules even if a caller
                # bypasses the equipped-blade resolver.
                self.assertEqual(s.ability._rules_for(b)[0][1]['do'][0], ABILITY_PRESETS[key]['op'])

    def test_full_defense_bypass_is_removed(self):
        s, _ = battle('Dranzer')
        b = blade(['ignore_defense'])
        s.blades[P] = b
        self.fire(s, b)
        self.assertEqual(s.status.get_duration('ignore_defense_turns', P), 0)
        self.assertLess(s.status.get_buff_bonus(E, 'defense'), 0)

    def test_same_named_designs_have_independent_compiled_rules(self):
        s, _ = battle('Dranzer')
        a, b = blade(['true_damage']), blade(['heal'])
        self.assertEqual(a['name'], b['name'])
        self.assertEqual(s.ability._rules_for(a)[0][1]['do'][0]['op'], 'true_damage')
        self.assertEqual(s.ability._rules_for(b)[0][1]['do'][0]['op'], 'heal')

    def test_repeated_charges_and_shields_stay_bounded(self):
        for effect, expected in [('special_charge',25), ('extra_special_hit',25), ('hit_charge',12), ('max_hp_barrier',100), ('hp_barrier',100), ('shield',100)]:
            with self.subTest(effect=effect):
                s, _ = battle('Dranzer')
                b = blade([effect], ['turn_start'])
                s.blades[P] = b
                s.hp[P] = 1000
                for _ in range(50):
                    s.ability.cooldowns.clear()  # simulate separated activations
                    self.fire(s, b)
                value = (s.ability.primed_bonus.get(P,0) if effect in ('special_charge','extra_special_hit') else
                         s.ability.primed_hit_bonus.get(P,0) if effect == 'hit_charge' else s.status.get_shield(P))
                self.assertEqual(value, expected)
                self.assertEqual(s.ability.extra_special_hits.get(P,0),0)
                self.assertEqual(s.ability.cooldowns[(P, f'custom_{effect}')], ABILITY_COOLDOWN)


class EquippedBalance(unittest.IsolatedAsyncioTestCase):
    async def test_saved_custom_battles_use_updated_definition(self):
        from cogs.battle.boss.boss_copy import equipped_blade
        b = blade(['extra_special_hit'])
        b['abilities'][0]['rules'][0]['do'][0] = {'op':'bonus_special_hits','hits':1}
        b['custom_meta']['rules_version'] = 2
        profile = {'active_custom_bey':True,'custom_bey':b}
        before = copy.deepcopy(profile)
        with patch('cogs.battle.boss.boss_copy.get_user',AsyncMock(return_value=profile)):
            result, instance = await equipped_blade(123)
        self.assertIsNone(instance)
        self.assertEqual(result['abilities'][0]['rules'][0]['do'][0]['op'],'prime_bonus')
        self.assertEqual(profile,before)


if __name__ == '__main__': unittest.main()
