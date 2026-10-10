"""Four approved kits: committed hits, shield-only damage, healing and Heat."""
import copy
import math
import unittest
from unittest.mock import patch
from tools.test_nine_burst_beys import session, end, outgoing, P, E
from utils.character_registry import REGISTRY, load_parts
from utils.bey_components import assemble, reconcile
from cogs.battle.purification import heal_amount
from cogs.battle.damage_rules import resolve_special_hits
from cogs.battle.special_gate import ready

NAMES = ('Black Especially', 'Grand Dragon', 'Heaven Pegasus', 'Hyperion Burn Cho Xceed')


def hit(s, amount=100, move='attack'):
    s.ability.tactical.committed(P, E, move, amount, [])


def shield_hit(s, move='attack', damage=100):
    return s.ability.damage_filter._step4_shield(P, E, s.blades[E], move, damage, True, [])


class Roster(unittest.TestCase):
    def test_identity_types_formulas_and_owned_components(self):
        shop = {p['id'] for p in load_parts(shop_only=True)}
        for i, (name, typ) in enumerate(zip(NAMES, ('Balance', 'Attack', 'Stamina', 'Attack')), 160):
            b = REGISTRY.find_bey(name)
            self.assertEqual(b['id'], f'BB{i}')
            self.assertEqual(b['type'], typ)
            self.assertEqual(len(b['abilities']), 2)
            self.assertEqual(b['ability'], b['abilities'][0])
            p = {'inventory': [name], 'active_beyblade': name}
            reconcile(p)
            mounted, delta = assemble(p, b)
            self.assertEqual(mounted['stats'], b['stats'])
            self.assertFalse(any(delta.values()))
            for ident in b['default_parts'].values():
                self.assertNotIn(ident, shop)
                self.assertEqual(REGISTRY.part(ident)['bey_id'], b['id'])
            formula = b['special_move']['damage_formula']
            expected = math.ceil(formula['base'] + sum(formula.get(k, 0) * b['stats'][k]
                                                       for k in ('attack', 'defense', 'stamina')))
            self.assertEqual(resolve_special_hits(b), [expected] * b['special_move']['hits'])


class Effects(unittest.TestCase):
    def test_shadow_adaptation_refreshes_each_stat_independently(self):
        s, b, r, d = session(NAMES[0])
        end(s, 'attack', 'defense', 'lose'); end(s, 'attack', 'defense', 'lose')
        end(s, 'defense', 'stamina', 'lose')
        buffs = s.status.active_buffs[P]
        self.assertEqual(len(buffs), 2)
        self.assertEqual({x['stat'] for x in buffs}, {'attack', 'defense'})
        for _ in range(2):
            s.status.tick_buffs(P, [])
            self.assertEqual(len(s.status.active_buffs[P]), 2)
        s.status.tick_buffs(P, [])
        self.assertEqual(s.status.active_buffs[P], [])

    def test_seal_requires_charge_and_committed_attack_and_expires(self):
        s, b, r, d = session(NAMES[0])
        hit(s)
        self.assertEqual(heal_amount(s, E, 100), 100)
        end(s, 'charge')
        hit(s, 0); hit(s, 100, 'special')
        self.assertTrue(d['seal_ready'])
        hit(s)
        self.assertEqual(heal_amount(s, E, 100), 75)
        end(s, 'charge'); hit(s)
        self.assertEqual(heal_amount(s, E, 100), 75)  # refresh, never stack
        s.hp[E] = 100
        r.data(E)['halo'] = 4
        r.special(E, P, 'halo_finisher', 100, [])
        self.assertEqual(s.hp[E], 190)  # Raphael's Special also respects the seal
        self.assertEqual(r.special(P, E, 'eclipse_finisher', 100, []), 135)
        self.assertEqual(heal_amount(s, E, 100), 100)
        end(s, 'charge'); hit(s)
        s.round += 3
        self.assertEqual(heal_amount(s, E, 100), 100)

    def test_seal_respects_debuff_immunity_and_stamina_healing_path(self):
        s, b, r, d = session(NAMES[0])
        s.ability.debuff_immune[E] = True
        end(s, 'charge'); hit(s)
        self.assertEqual(heal_amount(s, E, 100), 100)
        s.ability.debuff_immune[E] = False
        end(s, 'charge'); hit(s)
        s.hp[E] = 100
        sm = s.stamina_manager
        base_heal, _, _ = s.type_gimmicks.recovery(E, sm._sta_stat(E))
        sm.apply_stamina_action(E, s.hp, s.type_mods[E], max_hp=1000, gimmicks=s.type_gimmicks)
        self.assertEqual(s.hp[E] - 100, math.floor(base_heal * .75))

    def test_excavation_never_overflows_extra_damage_into_hp(self):
        s, b, r, d = session(NAMES[1])
        for shield, through, remaining in ((50, 50, 0), (120, 0, 0), (200, 0, 50)):
            s.status.shield_hp[E] = shield
            d['breakthrough'] = False
            self.assertEqual(shield_hit(s), through)
            self.assertEqual(s.status.get_shield(E), remaining)
            self.assertEqual(d.get('breakthrough', False), remaining == 0)
        s.status.shield_hp[E] = 120
        d['breakthrough'] = False
        self.assertEqual(outgoing(s), 100)
        self.assertEqual(shield_hit(s), 0)
        self.assertEqual(outgoing(s), 125)
        self.assertEqual(outgoing(s), 100)

    def test_crusher_shield_only_rider_precedes_normal_damage_once(self):
        s, b, r, d = session(NAMES[1])
        s.status.shield_hp[E] = 100
        r.special(P, E, 'crusher_finisher', 100, [])
        self.assertEqual(shield_hit(s, 'special'), 40)
        self.assertEqual(s.status.get_shield(E), 0)
        self.assertEqual(r.shield_bonus(P, 'special', 100, True), 0)
        r.special(P, E, 'crusher_finisher', 100, [])
        s.round += 1
        self.assertEqual(r.shield_bonus(P, 'special', 100, True), 0)

    def test_feathers_cap_heal_only_stamina_action_and_special_consumes(self):
        s, b, r, d = session(NAMES[2])
        for _ in range(5): end(s, 'stamina', 'defense')
        self.assertEqual(d['feathers'], 3)
        self.assertAlmostEqual(r.action_heal(P, 100), 115)
        self.assertEqual(heal_amount(s, P, 100), 100)
        s.hp[P] = 100
        sm = s.stamina_manager
        base_heal, _, _ = s.type_gimmicks.recovery(P, sm._sta_stat(P))
        sm.apply_stamina_action(P, s.hp, s.type_mods[P], max_hp=1000, gimmicks=s.type_gimmicks)
        self.assertEqual(s.hp[P] - 100, math.floor(base_heal * 1.15))
        s.stamina_manager.stamina[P] = 1
        before = s.stability_manager.stability[P]
        r.special(P, E, 'ascension_finisher', 100, [])
        self.assertEqual(s.stamina_manager.stamina[P], 4)
        self.assertEqual(s.stability_manager.stability[P], before + 12)
        self.assertEqual(r.action_heal(P, 100), 100)

    def test_slipstream_charge_only_single_use_available_stamina_and_expiry(self):
        s, b, r, d = session(NAMES[2])
        end(s, 'stamina', 'defense')
        s.stamina_manager.stamina[P] = 1
        s.stamina_manager.stamina[E] = .3
        end(s, 'charge')
        self.assertAlmostEqual(s.stamina_manager.stamina[P], 1.3)
        self.assertEqual(s.stamina_manager.stamina[E], 0)
        s.stamina_manager.stamina[E] = 5
        end(s, 'charge')
        self.assertEqual(s.stamina_manager.stamina[E], 5)
        end(s, 'stamina', 'defense'); s.round += 3
        end(s, 'charge')
        self.assertEqual(s.stamina_manager.stamina[E], 5)

    def test_heat_committed_attacks_only_cap_and_stamina_loss(self):
        s, b, r, d = session(NAMES[3])
        hit(s, 0); hit(s, 100, 'special')
        self.assertEqual(d.get('heat', 0), 0)
        for _ in range(6): hit(s)
        self.assertEqual(d['heat'], 4)
        end(s, 'stamina', 'defense')
        self.assertEqual(d['heat'], 3)
        self.assertEqual(outgoing(s), 100)

    def test_ignition_cost_successful_hit_consumption_no_special_and_expiry(self):
        s, b, r, d = session(NAMES[3])
        d['heat'] = 3
        end(s, 'charge')
        self.assertEqual(d['heat'], 1)
        self.assertEqual(outgoing(s, 'special'), 100)
        self.assertEqual(outgoing(s), 130)
        hit(s, 0)
        self.assertEqual(outgoing(s), 130)
        hit(s)
        self.assertEqual(outgoing(s), 100)
        d['heat'] = 2; end(s, 'charge'); s.round += 3
        self.assertEqual(outgoing(s), 100)

    def test_burn_consumes_heat_and_adds_same_bonus_to_all_three_hits(self):
        s, b, r, d = session(NAMES[3])
        d['heat'] = 4
        self.assertEqual(r.special(P, E, 'burn_finisher', 100, []), 124)
        self.assertEqual(outgoing(s, 'special', first=False), 124)
        self.assertEqual(outgoing(s, 'special', first=False), 124)
        self.assertEqual(d.get('heat', 0), 0)
        self.assertEqual(outgoing(s), 100)
        s.round += 1
        self.assertEqual(outgoing(s, 'special', first=False), 100)


class LiveRounds(unittest.IsolatedAsyncioTestCase):
    async def test_heat_bonus_reaches_every_actual_special_hit(self):
        from tools import test_special_avatar_pipeline as pipeline
        fixture = pipeline.SpecialPipelineTests()
        blade = REGISTRY.find_bey(NAMES[3])
        plain = await fixture.build(blade)
        heated = await fixture.build(blade)
        heated.ability.burst.data('101')['heat'] = 4
        raw, _, _ = fixture.resolve(plain)
        enhanced, _, _ = fixture.resolve(heated)
        self.assertEqual(enhanced - raw, 72)
        self.assertEqual(heated.ability.burst.data('101').get('heat', 0), 0)

    async def test_each_kit_completes_live_rounds_and_gated_special(self):
        for name in NAMES:
            with self.subTest(name=name):
                s, b, r, d = session(name)
                for key in (P, E):
                    s.hp[key] = s.max_hp_per_player[key] = 10000
                    s.stability_manager.max[key] = s.stability_manager.stability[key] = 5000
                for move in ('attack', 'stamina', 'charge', 'special'):
                    s.moves = {P: move, E: 'defense' if move == 'stamina' else 'stamina'}
                    for key in (P, E):
                        s.stamina_manager.stamina[key] = s.stamina_manager.cap_for(key)
                        s.stamina_manager.gauge[key] = 150
                    with patch('random.random', return_value=.99):
                        await s._BattleSession__resolve_round_body()
                self.assertLess(s.hp[E], 10000)
                self.assertFalse(ready(s, P, b, 150))


if __name__ == '__main__':
    unittest.main()
