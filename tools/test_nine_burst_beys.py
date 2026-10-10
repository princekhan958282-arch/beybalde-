"""Authored roster integrity and stateful effects through shared battle hooks."""
import copy
import unittest
from unittest.mock import patch
from tools.test_october_bey_roster import build, P, E
from utils.character_registry import REGISTRY, load_parts
from utils.bey_components import reconcile, assemble
from cogs.battle.special_gate import ready
from cogs.battle.damage_rules import resolve_special_hits

NAMES = ('Silver Valkyrie', 'Arc Bahamut', 'Circle Bahamut', 'Judgement Joker',
         'Jambo Jamunter', 'Greatest Raphael', 'Bushin Ashura', 'Orb Engaard', 'Ace Dragon')


def session(name):
    s, b = build(name)
    s.round = 1
    s.hp[P] = 1000
    return s, b, s.ability.burst, s.ability.burst.data(P)


def end(s, move, enemy='stamina', result='win'):
    s.ability.tactical.round_end(P, E, move, enemy, result, [])


def start(s, move='attack', enemy='stamina'):
    stats, opponent = copy.deepcopy(s.battle_stats[P]), copy.deepcopy(s.battle_stats[E])
    s.ability.tactical.round_start(P, E, move, enemy, stats, opponent, [])
    return stats, opponent


def outgoing(s, move='attack', result='win', amount=100, first=True):
    return s.ability.tactical.before_damage(P, E, move, result, amount, [], first)


class RosterIntegrity(unittest.TestCase):
    def test_records_and_bundled_components(self):
        stock = {x['id'] for x in load_parts(shop_only=True)}
        for i, name in enumerate(NAMES, 149):
            b = REGISTRY.find_bey(name)
            self.assertEqual(b['id'], f'BB{i}')
            self.assertEqual(len(b['abilities']), 2)
            self.assertEqual(b['ability'], b['abilities'][0])
            self.assertIn('cdn.discordapp.com', b['image_url'])
            p = {'inventory': [name], 'active_beyblade': name}
            reconcile(p)
            mounted, delta = assemble(p, b)
            self.assertEqual(mounted['stats'], b['stats'])
            self.assertFalse(any(delta.values()))
            for slot, ident in b['default_parts'].items():
                self.assertNotIn(ident, stock)
                self.assertEqual(REGISTRY.part(ident)['bey_id'], b['id'])
        self.assertEqual(REGISTRY.find_bey('Cricle Bahamut')['name'], 'Circle Bahamut')

    def test_special_formula_and_cooldown_gate(self):
        for name in NAMES:
            s, b, r, d = session(name)
            sm = b['special_move']
            hits = resolve_special_hits(b, effective_stats=s.battle_stats[P])
            expected = round(sm['damage_formula']['base'] + sum(
                sm['damage_formula'].get(k, 0) * s.battle_stats[P].get(k, 0)
                for k in ('attack', 'defense', 'stamina')) + .499999)
            self.assertEqual(hits, [expected] * sm['hits'])
            self.assertTrue(ready(s, P, b, 150))
            damage, _ = s.ability._fire('on_special', P, E, b, 'special', 'win', 100, 0, [])
            self.assertFalse(ready(s, P, b, 150))
            self.assertEqual(s.ability.cooldowns[(P, 'burst_finisher')], 4)


class Mechanics(unittest.TestCase):
    def test_silver_stack_cap_loss_edge_and_finisher(self):
        s, b, r, d = session('Silver Valkyrie')
        for _ in range(6): end(s, 'attack')
        self.assertEqual(d['speed'], 4)
        stats, enemy = start(s)
        self.assertAlmostEqual(enemy['defense'], s.battle_stats[E]['defense'] * .8)
        self.assertAlmostEqual(stats['attack'], s.battle_stats[P]['attack'] * 1.2)
        end(s, 'attack', result='lose')
        self.assertEqual(d['speed'], 3)
        dmg, _ = s.ability._fire('on_special', P, E, b, 'special', 'win', 100, 0, [])
        self.assertEqual(dmg, 136)
        self.assertNotIn('speed', d)

    def test_arc_storage_break_replacement_and_expiry_preserve_other_shields(self):
        s, b, r, d = session('Arc Bahamut')
        # setup shield uses original battle maximum, before the test sets 1000.
        initial = d['barrier']
        s.status.absorb_shield(P, initial)
        s.ability.tactical.shield_absorbed(E, P, initial, 0, [])
        self.assertTrue(any(x['source'] == 'arc_barrier' for x in s.status.active_buffs[P]))
        r.absorbed(P, 1000, [])
        self.assertEqual(d['vault'], 60)
        self.assertEqual(outgoing(s, 'defense'), 160)
        self.assertEqual(outgoing(s, 'defense'), 100)
        s.status.add_shield(P, 17)
        r.special(P, E, 'fortress_finisher', 100, [])
        self.assertEqual(s.status.get_shield(P), 217)
        end(s, 'charge')
        self.assertEqual(s.status.get_shield(P), 217)
        s.round = 2
        end(s, 'charge')
        self.assertEqual(s.status.get_shield(P), 17)

    def test_circle_immunity_excludes_action_cost_and_consumes_stacks(self):
        s, b, r, d = session('Circle Bahamut')
        for _ in range(3): end(s, 'defense', 'attack')
        start(s, 'defense', 'attack')
        self.assertEqual(s.ability.tactical.mitigate(P, E, 'attack', 100, []), 88)
        before = s.stability_manager.stability[P]
        s.stability_manager._apply(P, -20)
        self.assertEqual(s.stability_manager.stability[P], before)
        s.stability_manager._apply(P, -6, action=True)
        self.assertLess(s.stability_manager.stability[P], before)
        end(s, 'charge')
        self.assertEqual(d['orbit'], 0)
        r.special(P, E, 'orbit_finisher', 100, [])
        self.assertEqual(d['orbit'], 2)

    def test_joker_token_consumption_only_normal_attack(self):
        s, b, r, d = session('Judgement Joker')
        end(s, 'attack'); end(s, 'defense', 'attack')
        self.assertEqual(outgoing(s, 'special'), 100)
        self.assertEqual((d['red'], d['black']), (1, 1))
        self.assertEqual(outgoing(s), 115)
        self.assertEqual((d['red'], d['black']), (0, 0))
        self.assertEqual(s.status.get_shield(P), 80)
        d.update(red=2, black=2)
        self.assertEqual(r.special(P, E, 'verdict_finisher', 100, []), 130)
        self.assertEqual(s.status.get_shield(P), 180)

    def test_heavy_landing_refresh_and_special_extension(self):
        s, b, r, d = session('Jambo Jamunter')
        end(s, 'defense', 'attack'); end(s, 'defense', 'attack')
        buffs = [x for x in s.status.active_buffs[E] if x['source'].startswith('heavy_landing')]
        self.assertEqual(len(buffs), 1)
        self.assertEqual(buffs[0]['rounds_left'], 3)
        r.special(P, E, 'hammer_finisher', 100, [])
        self.assertEqual(buffs[0]['rounds_left'], 4)
        self.assertGreater(outgoing(s, 'defense'), 100)
        s.hp[P] = 600
        self.assertEqual(outgoing(s, 'defense'), 100)

    def test_raphael_halo_heal_reversal_once(self):
        s, b, r, d = session('Greatest Raphael')
        for _ in range(8): end(s, 'charge')
        self.assertEqual(d['halo'], 4)
        stats, _ = start(s)
        self.assertAlmostEqual(stats['attack'], s.battle_stats[P]['attack'] * 1.12)
        s.hp[P] = 300
        s.status.add_buff(P, 'attack', -10, 4)
        r.committed(P, [])
        self.assertEqual(s.status.active_buffs[P], [])
        self.assertEqual(r.incoming(P, 'special', 100, []), 80)
        r.special(P, E, 'halo_finisher', 100, [])
        self.assertEqual(s.hp[P], 420)
        s.round = 4
        r.committed(P, [])
        self.assertEqual(r.incoming(P, 'attack', 100, []), 100)

    def test_ashura_arms_and_three_distinct_actions(self):
        s, b, r, d = session('Bushin Ashura')
        for _ in range(3): end(s, 'defense', 'attack')
        self.assertEqual(r.incoming(P, 'special', 100, []), 100)
        self.assertEqual(r.incoming(P, 'attack', 100, []), 79)
        self.assertEqual(d.get('arms', 0), 0)
        end(s, 'special')  # interrupt the preceding repeated-Defense sequence
        end(s, 'attack'); end(s, 'charge'); end(s, 'stamina')
        self.assertTrue(any(x['source'] == 'bushin_discipline' for x in s.status.active_buffs[P]))
        self.assertEqual(d['sequence'], [])

    def test_orb_third_attack_and_sphere_next_round_only(self):
        s, b, r, d = session('Orb Engaard')
        for round_n in range(1, 4):
            s.round = round_n
            start(s, 'defense', 'attack')
            self.assertEqual(r.incoming(P, 'attack', 100, []), 70 if round_n == 3 else 100)
        start(s, 'defense', 'special')
        self.assertEqual(d['attacks'], 3)
        for _ in range(3): end(s, 'defense', 'defense', 'mirror')
        self.assertEqual(d['spheres'], 3)
        r.special(P, E, 'sphere_finisher', 100, [])
        self.assertEqual(r.incoming(P, 'special', 100, []), 100)
        s.round = 4
        self.assertEqual(r.incoming(P, 'special', 100, []), 76)
        s.round = 5
        self.assertEqual(r.incoming(P, 'special', 100, []), 100)

    def test_ace_loss_stacks_charge_consumption_expiry_and_special_pierce(self):
        s, b, r, d = session('Ace Dragon')
        for _ in range(5): end(s, 'attack', 'defense', 'lose')
        self.assertEqual(d['pursuit'], 3)
        self.assertEqual(outgoing(s), 130)
        end(s, 'charge')
        self.assertEqual(outgoing(s, 'special'), 100)
        self.assertGreater(outgoing(s), 100)
        self.assertEqual(outgoing(s), 100)
        end(s, 'charge'); s.round += 3
        self.assertEqual(outgoing(s), 100)
        d['pursuit'] = 3
        r.special(P, E, 'pursuit_finisher', 100, [])
        self.assertEqual(s.ability.special_pierce_pct[P], 15)

    def test_state_isolation_and_silence(self):
        s, b, r, d = session('Ace Dragon')
        d['pursuit'] = 3
        s.status.silenced_turns[P] = 2
        self.assertEqual(outgoing(s), 100)
        self.assertEqual(d['pursuit'], 3)
        other, _, runtime, state = session('Ace Dragon')
        self.assertNotIn('pursuit', state)
        self.assertNotIn(E, r.effects)


class LiveRounds(unittest.IsolatedAsyncioTestCase):
    async def test_every_new_bey_completes_real_rounds_and_special(self):
        for name in NAMES:
            with self.subTest(name=name):
                s, b, r, d = session(name)
                for key in (P, E):
                    s.hp[key] = s.max_hp_per_player[key] = 10000
                    s.stability_manager.max[key] = s.stability_manager.stability[key] = 5000
                for own in ('defense', 'charge', 'attack', 'special'):
                    s.moves = {P: own, E: 'stamina'}
                    for key in (P, E):
                        s.stamina_manager.stamina[key] = s.stamina_manager.cap_for(key)
                        s.stamina_manager.gauge[key] = 150
                    with patch('random.random', return_value=.99):
                        await s._BattleSession__resolve_round_body()
                self.assertLess(s.hp[E], 10000)
                self.assertFalse(ready(s, P, b, 150))
                self.assertEqual(s.ability.cooldowns[(P, 'burst_finisher')], 3)


if __name__ == '__main__':
    unittest.main()
