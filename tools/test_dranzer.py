"""Phoenix regression coverage using real PvP/Story rounds and boss exchanges.

Run: python -m unittest tools.test_dranzer
No Discord connection or production database is used.
"""
import copy
import unittest
from unittest.mock import patch

from tools.sim_battle_ticks import BattleSession, FakeChannel, FakePlayer, get_beyblade
from cogs.battle.dranzer import KINDS, SPECIAL_COOLDOWNS, version
from cogs.battle.purification import effective_stats
from cogs.battle.special_gate import ready
from cogs.battle.boss.boss_ai import Fighter, resolve
from cogs.battle.boss.blade_abilities import BladeKit
from utils.character_registry import REGISTRY, ROOT, load_beys

P, E = '1001', '1002'


def name(kind): return 'Black Dranzer' if kind == 'Black' else 'Dranzer ' + kind


def build(kind, reverse=False):
    blade = copy.deepcopy(get_beyblade(name(kind)))
    enemy = copy.deepcopy(get_beyblade('Dranzer'))
    enemy['name'], enemy['type'] = 'Test opponent', 'Attack'
    enemy['abilities'], enemy['ability'] = [], {}
    enemy['special_move'] = {'name': 'Test Special', 'hits': 1, 'damage_per_hit': 100}
    mine, foe = (enemy, blade) if reverse else (blade, enemy)
    s = BattleSession(None, FakeChannel(), FakePlayer(1001, 'A'), FakePlayer(1002, 'B'), mine, foe,
                      payout=False, spend_energy=False)
    for key in (P, E):
        s._prefetch[key] = {'profile': {'wins': 0, 'losses': 0, 'coins': 0, 'xp': 0, 'inventory': []}}
        s.hp[key] = s.max_hp_per_player[key] = 5000
        s.stability_manager.max[key] = s.stability_manager.stability[key] = 5000
        s.stamina_manager.stamina[key] = s.stamina_manager.cap_for(key)
    return s, (E if reverse else P), (P if reverse else E)


async def turn(s, key, own, enemy):
    other = E if key == P else P
    s.moves = {key: own, other: enemy}
    for actor in (P, E):
        s.stamina_manager.stamina[actor] = s.stamina_manager.cap_for(actor)
        if s.moves[actor] == 'special': s.stamina_manager.gauge[actor] = 150
    with patch('random.random', return_value=.99):
        # Call the real body directly so errors cannot be swallowed by the UI guard.
        await s._BattleSession__resolve_round_body()


def fighter(kind=None):
    blade = copy.deepcopy(get_beyblade(name(kind))) if kind else {'name': 'Opponent', 'type': 'Attack'}
    return Fighter(blade['name'], 5000, 5000, 100, 100, 100,
                   sp=15, sp_max=15, gauge=150, level=50, bey_type=blade['type'], ability_blade=blade)


class DranzerData(unittest.TestCase):
    def test_separate_records_preserve_base_and_components(self):
        base = get_beyblade('Dranzer')
        ids = []
        for kind in KINDS:
            blade = get_beyblade(name(kind))
            self.assertEqual(blade['stats'], base['stats'])
            self.assertEqual(blade['main_frame'], base['main_frame'])
            self.assertEqual(blade['rarity'], base['rarity'])
            self.assertEqual(blade['type'], 'Balance')
            self.assertEqual(len(blade['abilities']), 2)
            self.assertEqual(blade['ability'], blade['abilities'][0])
            self.assertTrue((ROOT / 'beys' / (name(kind).lower().replace(' ', '_') + '.json')).is_file())
            self.assertTrue(blade['image_url'].startswith('https://cdn.discordapp.com/'))
            self.assertEqual(blade['booster_exclusive'], kind == 'Black')
            for slot in ('disk', 'driver'):
                part = REGISTRY.part(blade['default_parts'][slot])
                self.assertEqual(part['bey_id'], blade['id'])
                self.assertEqual(part['stats'], REGISTRY.part(base['default_parts'][slot])['stats'])
            ids.append(blade['id'])
        self.assertEqual(len(set(ids)), 7)
        self.assertEqual(len(load_beys()), 139)
        self.assertIsNone(version(base))

    def test_black_mechanics_require_black_identity(self):
        copied = copy.deepcopy(get_beyblade('Black Dranzer'))
        copied['name'] = 'Another blade'
        self.assertIsNone(version(copied))
        for kind in KINDS - {'Black'}:
            self.assertNotIn('Dark Absorption', [a['name'] for a in get_beyblade(name(kind))['abilities']])


class DranzerRuntimeTests(unittest.TestCase):
    def test_second_ability_disable_gates_absorption_rebirth_and_overheat(self):
        for kind in ('Black', 'V2', 'GT', 'MS'):
            s, key, other = build(kind)
            r = s.ability.dranzer
            s.status.ability_2_disabled[key] = True
            baseline = effective_stats(s, key)['attack']
            if kind == 'Black':
                self.assertEqual(r.terminal(key, 100, []), 100)
                self.assertFalse(r.states[key].get('energy'))
            elif kind == 'V2':
                s.hp[key] = 50
                r.terminal(key, 100, [])
                self.assertEqual(s.hp[key], 0)
            elif kind == 'GT':
                r.states[key].update(turbo=3, turbo_until=8)
                self.assertEqual(r.outgoing(key, 'attack', 100), 100)
                self.assertEqual(r.cost(key, 'attack', 2), 1.7)
            else:
                s.hp[key] = 1000
                self.assertAlmostEqual(effective_stats(s, key)['attack'], baseline)

    def test_black_burn_and_rule_damage_do_not_lifesteal_or_steal(self):
        s, key, other = build('Black')
        r = s.ability.dranzer
        s.status.burn_stacks[key], s.status.burn_dmg[key], s.status.burn_duration[key] = 1, 100, 1
        s.status.tick_burn(key, s.blades[key])
        self.assertEqual(s.hp[key], 4915)
        self.assertEqual(r.states[key]['energy'], 15)
        s.blades[other]['abilities'] = [{'name': 'Terminal damage', 'rules': [
            {'when': 'turn_start', 'do': [{'op': 'true_damage', 'value': 100}]}]}]
        s.ability._compiled.clear()
        s.ability._fire('turn_start', other, key, s.blades[other], 'charge', 'mirror', 0, 0, [])
        self.assertEqual(s.hp[key], 4830)
        self.assertEqual(r.states[key]['energy'], 30)
        self.assertEqual(s.hp[other], 5000)
        self.assertFalse(r.states[key].get('transfers'))

    def test_special_heal_and_burn_survive_ability_silence(self):
        for kind in ('Black', 'F'):
            s, key, other = build(kind)
            r = s.ability.dranzer
            s.status.silenced_turns[key] = 3
            s.hp[key] = 4000
            r.committed(key, other, 'special', 100)
            r.end({key: 'special', other: 'charge'}, [])
            if kind == 'Black':
                self.assertEqual(s.hp[key], 4030)
                self.assertFalse(r.states[key].get('transfers'))
            else:
                self.assertEqual(len(r.states[key]['burns']), 1)

    def test_twin_wings_terminal_hit_has_cooldown_and_no_recursive_proc(self):
        s, key, other = build('V2')
        r = s.ability.dranzer
        for round_no, expected in ((1, 4978), (2, 4978), (3, 4978), (4, 4956)):
            s.round = round_no
            r.begin({key: 'attack', other: 'charge'}, [])
            r.committed(key, other, 'attack', 100)
            r.end({key: 'attack', other: 'charge'}, [])
            self.assertEqual(s.hp[other], expected)
            self.assertEqual(r.hits[(key, other, 'attack')], 100)

    def test_v_removes_temporary_phoenix_def_buff(self):
        s, key, other = build('V')
        s.blades[other] = copy.deepcopy(get_beyblade('Dranzer G'))
        r = s.ability.dranzer
        r.states[other]['rotation_until'] = 3
        r.begin({key: 'special', other: 'charge'}, [])
        self.assertNotIn('rotation_until', r.states[other])

    def test_gravity_stacks_cap_and_special_consumes_snapshot(self):
        s, key, other = build('G')
        r = s.ability.dranzer
        base = effective_stats(s, key)['attack']
        for round_no in range(1, 9):
            s.round = round_no
            r.begin({key: 'attack', other: 'charge'}, [])
            r.committed(key, other, 'attack', 10)
            r.end({key: 'attack', other: 'charge'}, [])
        self.assertEqual(r.states[key]['gravity'], 5)
        self.assertAlmostEqual(effective_stats(s, key)['attack'], base * 1.20)
        s.round += 1
        r.begin({key: 'special', other: 'charge'}, [])
        self.assertEqual(r.states[key]['gravity'], 0)
        expected = (145 + .65 * base * 1.20 + 60) * 100 / (effective_stats(s, other)['defense'] * .88)
        self.assertAlmostEqual(r.special_damage(key, other), expected)

    def test_rotation_missing_hp_cooldown_and_expiry(self):
        s, key, other = build('G')
        r = s.ability.dranzer
        s.hp[key] = 3000
        base = effective_stats(s, key)['defense']
        r.end({key: 'stamina', other: 'charge'}, [])
        self.assertEqual(s.hp[key], 3120)
        self.assertAlmostEqual(effective_stats(s, key)['defense'], base * 1.10)
        s.round = 2
        r.end({key: 'stamina', other: 'charge'}, [])
        self.assertEqual(s.hp[key], 3120)
        s.round = 4
        self.assertAlmostEqual(effective_stats(s, key)['defense'], base)
        s.round = 5
        r.end({key: 'stamina', other: 'charge'}, [])
        self.assertEqual(s.hp[key], 3232)

    def test_black_absorption_capacity_and_non_recursive_terminal(self):
        s, key, other = build('Black')
        r = s.ability.dranzer
        self.assertEqual(r.terminal(key, 100, []), 85)
        self.assertEqual(r.states[key]['energy'], 15)
        self.assertEqual(s.hp[other], 5000)
        r.states[key]['energy'] = 1745
        self.assertEqual(r.terminal(key, 100, []), 95)
        self.assertEqual(r.states[key]['energy'], 1750)
        self.assertEqual(r.terminal(key, 100, []), 100)
        self.assertFalse(r.states[key].get('transfers'))

    def test_black_lifesteal_uses_actual_and_special_adds_thirty_percent(self):
        for move, healing in (('attack', 20), ('special', 50)):
            s, key, other = build('Black')
            r = s.ability.dranzer
            s.hp[key] = 4000
            r.committed(key, other, move, 100)
            r.end({key: move, other: 'charge'}, [])
            self.assertEqual(s.hp[key], 4000 + healing)
            self.assertTrue(r.states[key]['transfers'])

    def test_black_steals_equal_current_stats_expires_and_caps(self):
        s, key, other = build('Black')
        r = s.ability.dranzer
        mine, foe = effective_stats(s, key), effective_stats(s, other)
        r.steal(key, other)
        for stat in ('attack', 'defense'):
            self.assertAlmostEqual(effective_stats(s, key)[stat] - mine[stat], foe[stat] * .04)
            self.assertAlmostEqual(foe[stat] - effective_stats(s, other)[stat], foe[stat] * .04)
        for _ in range(12): r.steal(key, other)
        for stat in ('attack', 'defense'):
            self.assertAlmostEqual(effective_stats(s, other)[stat], foe[stat] * .84)
        s.round = 5
        self.assertAlmostEqual(effective_stats(s, key)['attack'], mine['attack'])
        self.assertAlmostEqual(effective_stats(s, other)['attack'], foe['attack'])

    def test_black_special_consumes_energy_and_preserves_new_incoming(self):
        s, key, other = build('Black')
        r = s.ability.dranzer
        r.states[key]['energy'] = 300
        r.begin({key: 'special', other: 'charge'}, [])
        self.assertEqual(r.states[key]['energy'], 0)
        expected = (190 + .80 * effective_stats(s, key)['attack'] + 300) * 100 / (effective_stats(s, other)['defense'] * .80)
        self.assertAlmostEqual(r.special_damage(key, other), expected)
        r.terminal(key, 100, [])
        self.assertEqual(r.states[key]['energy'], 15)

    def test_f_burn_cap_independent_expiry_and_special_ticks(self):
        s, key, other = build('F')
        r = s.ability.dranzer
        tick = int(effective_stats(s, key)['attack'] * .12)
        r.committed(key, other, 'attack', 10)
        r.end({key: 'attack', other: 'charge'}, [])
        self.assertEqual(s.hp[other], 5000 - tick)
        s.round = 2
        r.begin({key: 'special', other: 'charge'}, [])
        r.end({key: 'special', other: 'charge'}, [])
        self.assertEqual(s.hp[other], 5000 - 4 * tick)  # immediate old tick + two end ticks
        self.assertEqual(len(r.states[key]['burns']), 1)
        s.round = 3
        r.end({key: 'charge', other: 'charge'}, [])
        self.assertFalse(r.states[key]['burns'])
        r.burn(key, other); r.burn(key, other); r.burn(key, other)
        self.assertEqual(len(r.states[key]['burns']), 2)

    def test_f_resistance_all_hits_resource_cap_and_cooldown(self):
        s, key, other = build('F')
        r = s.ability.dranzer
        s.stamina_manager.stamina[key] = 1
        for _ in range(3): self.assertEqual(r.mitigate(key, 'special', 100, []), 85)
        self.assertEqual(s.stamina_manager.stamina[key], 3)
        s.round = 2
        self.assertEqual(r.mitigate(key, 'special', 100, []), 100)
        s.round = 5
        self.assertEqual(r.mitigate(key, 'special', 100, []), 85)

    def test_v_attack_stacks_reset_and_guard_once(self):
        s, key, other = build('V')
        r = s.ability.dranzer
        base = effective_stats(s, key)['defense']
        for round_no in range(1, 5):
            s.round = round_no
            r.begin({key: 'attack', other: 'charge'}, [])
            r.committed(key, other, 'attack', 10)
            r.end({key: 'attack', other: 'charge'}, [])
        self.assertEqual(r.outgoing(key, 'attack', 100), 121)
        s.hp[key] = 2200
        r.after_incoming(key, [])
        self.assertAlmostEqual(effective_stats(s, key)['defense'], base * 1.25)
        self.assertEqual(r.mitigate(key, 'attack', 100, []), 90)
        s.round += 3
        r.begin({key: 'defense', other: 'charge'}, [])
        self.assertEqual(r.states[key]['volcano'], 0)
        self.assertEqual(r.mitigate(key, 'attack', 100, []), 100)

    def test_v_special_removes_only_strongest_removable_def_buff(self):
        s, key, other = build('V')
        s.status.add_buff(other, 'defense', 15, 3)
        s.status.add_buff(other, 'defense', 30, 3)
        s.status.add_buff(other, 'defense', 40, 99)
        s.status.add_buff(other, 'attack', 50, 3)
        s.ability.dranzer.begin({key: 'special', other: 'charge'}, [])
        self.assertEqual(s.status.get_buff_bonus(other, 'defense'), 55)
        self.assertEqual(s.status.get_buff_bonus(other, 'attack'), 50)

    def test_v2_rebirth_cleanse_once_and_later_hit_can_kill(self):
        s, key, other = build('V2')
        r = s.ability.dranzer
        s.hp[key] = 50
        s.status.add_buff(key, 'attack', -20, 3)
        r.terminal(key, 100, [])
        self.assertEqual(s.hp[key], 1000)
        self.assertEqual(s.status.get_buff_bonus(key, 'attack'), 0)
        self.assertTrue(r.states[key]['rebirth_used'])
        r.terminal(key, 1200, [])
        self.assertEqual(s.hp[key], 0)

    def test_v2_double_hit_shred_expires(self):
        s, key, other = build('V2')
        r = s.ability.dranzer
        r.begin({key: 'special', other: 'charge'}, [])
        first = r.special_damage(key, other, 0)
        r.special_landed(key, other, 0, first)
        self.assertAlmostEqual(r.special_damage(key, other, 1), first / .88)
        s.round = 4
        self.assertAlmostEqual(r.special_damage(key, other, 1), first)

    def test_gt_stacks_expire_overheat_cost_and_special_discount(self):
        s, key, other = build('GT')
        r = s.ability.dranzer
        base = effective_stats(s, key)['attack']
        for round_no in range(1, 4):
            s.round = round_no
            r.begin({key: 'charge', other: 'charge'}, [])
            r.end({key: 'charge', other: 'charge'}, [])
        self.assertEqual(r.states[key]['turbo'], 3)
        self.assertAlmostEqual(effective_stats(s, key)['attack'], base * 1.18)
        self.assertAlmostEqual(r.outgoing(key, 'attack', 100), 118)
        self.assertEqual(r.cost(key, 'attack', 2), 2.55)
        s.round = 4
        r.begin({key: 'special', other: 'charge'}, [])
        self.assertEqual(r.cost(key, 'special', 4), 3.4)
        self.assertEqual(r.states[key]['turbo'], 0)
        expected = (165 + .70 * base * 1.18) * 1.36 * 100 / (effective_stats(s, other)['defense'] * .82)
        self.assertAlmostEqual(r.special_damage(key, other), expected)
        r.states[key].update(turbo=3, turbo_until=6)
        s.round = 7
        r.begin({key: 'attack', other: 'charge'}, [])
        self.assertEqual(r.states[key]['turbo'], 0)

    def test_ms_dynamic_stat_tiers_healing_recalculates_and_special_cap(self):
        s, key, other = build('MS')
        r = s.ability.dranzer
        baseline = effective_stats(s, key)
        for hp, tier in ((5000, 0), (4000, 1), (3000, 2), (1000, 4), (4500, 0)):
            s.hp[key] = hp
            self.assertAlmostEqual(effective_stats(s, key)['attack'], baseline['attack'] * (1 + .05 * tier))
            self.assertAlmostEqual(effective_stats(s, key)['defense'], baseline['defense'] * (1 + .04 * tier))
        s.hp[key] = 100
        r.begin({key: 'special', other: 'charge'}, [])
        expected = (175 + .75 * baseline['attack'] * 1.20) * 1.35 * 100 / (effective_stats(s, other)['defense'] * .80)
        self.assertAlmostEqual(r.special_damage(key, other), expected)
        self.assertAlmostEqual(r.attack_stats(key, {'defense': 100})['defense'], 90)
        self.assertGreater(r.attack_bonus(key, 100), 100)


class DranzerPvp(unittest.IsolatedAsyncioTestCase):
    async def test_all_specials_damage_and_use_gauge_with_cooldowns(self):
        for kind in sorted(KINDS):
            with self.subTest(kind=kind):
                s, key, other = build(kind)
                await turn(s, key, 'special', 'charge')
                self.assertLess(s.hp[other], 5000)
                self.assertEqual(s.stamina_manager.gauge[key], 0)
                self.assertFalse(ready(s, key, s.blades[key], 150))
                for _ in range(SPECIAL_COOLDOWNS[kind]): await turn(s, key, 'charge', 'charge')
                self.assertTrue(ready(s, key, s.blades[key], 150))

    async def test_each_attack_ability_in_real_round(self):
        for kind in sorted(KINDS):
            with self.subTest(kind=kind):
                s, key, other = build(kind)
                await turn(s, key, 'attack', 'charge')
                self.assertLess(s.hp[other], 5000)
                state = s.ability.dranzer.states[key]
                if kind == 'G': self.assertEqual(state['gravity'], 1)
                if kind == 'Black': self.assertTrue(state['transfers'])
                if kind == 'F': self.assertEqual(len(state['burns']), 1)
                if kind == 'V': self.assertEqual(state['volcano'], 1)
                if kind == 'V2': self.assertEqual(state['wing_ready'], 4)

    async def test_no_attack_proc_when_invulnerable(self):
        for kind in ('G', 'Black', 'F', 'V', 'V2'):
            s, key, other = build(kind)
            s.status.set_invulnerable(other, 3)
            await turn(s, key, 'attack', 'charge')
            self.assertEqual(s.hp[other], 5000)
            state = s.ability.dranzer.states[key]
            self.assertFalse(any(state.get(f) for f in ('gravity', 'transfers', 'burns', 'volcano', 'wing_ready')))

    async def test_black_actual_overkill_heal_and_position_symmetry(self):
        results = []
        for reverse in (False, True):
            s, key, other = build('Black', reverse)
            s.hp[key], s.hp[other] = 4000, 10
            await turn(s, key, 'attack', 'charge')
            results.append(s.hp[key])
            self.assertEqual(s.hp[key], 4002)
        self.assertEqual(results[0], results[1])

    async def test_story_projection_stats_costs_cooldowns_and_clone_isolation(self):
        from cogs.story.story_ai import project
        for kind in ('G', 'Black', 'GT', 'MS', 'V2'):
            s, key, other = build(kind)
            state = s.ability.dranzer.states[key]
            state.update(gravity=3, turbo=3, turbo_until=9, special_ready=4)
            if kind == 'Black': s.ability.dranzer.steal(key, other)
            s.hp[key] = 2200
            mine, foe = project(s, key, other), project(s, other, key)
            for stat in ('attack', 'defense'):
                self.assertAlmostEqual(getattr(mine, 'eff_' + stat), effective_stats(s, key)[stat])
            self.assertAlmostEqual(mine.cost_for('attack'), s.stamina_manager.cost_for(key, 'attack'), places=2)
            self.assertFalse(mine.can('special'))
            old = copy.deepcopy(s.ability.dranzer.states)
            resolve(mine, foe, 'attack', 'charge', simulate=True)
            self.assertEqual(s.ability.dranzer.states, old)


class DranzerBoss(unittest.TestCase):
    def test_boss_reflection_does_not_trigger_black_offensive_effects(self):
        a, b = fighter('Black'), fighter()
        b.reflected_flat = 100
        result = resolve(a, b, 'attack', 'charge', simulate=True)
        self.assertGreater(result['ability_reflect_b'], 0)
        self.assertGreater(a.dranzer_state['energy'], 0)
        self.assertNotIn('transfers', b.dranzer_state.get('statuses', {}))

    def test_all_specials_and_cooldowns(self):
        for kind in sorted(KINDS):
            with self.subTest(kind=kind):
                a, b = fighter(kind), fighter()
                resolve(a, b, 'special', 'charge', simulate=True)
                self.assertLess(b.hp, 5000)
                self.assertEqual(a.gauge, 0)
                self.assertFalse(a.can('special'))
                self.assertFalse(BladeKit(a.ability_blade).unsupported())
                for _ in range(SPECIAL_COOLDOWNS[kind]): resolve(a, b, 'charge', 'charge', simulate=True)
                a.gauge = 150
                self.assertTrue(a.can('special'))

    def test_black_transfer_absorption_and_search_clone_isolation(self):
        a, b = fighter('Black'), fighter()
        a.hp = 4000
        resolve(a, b, 'attack', 'attack', simulate=True)
        self.assertGreater(a.dranzer_state['energy'], 0)
        self.assertTrue(a.dranzer_state['transfers'])
        saved = copy.deepcopy(a.dranzer_state)
        resolve(a.clone(), b.clone(), 'special', 'attack', simulate=True)
        self.assertEqual(a.dranzer_state, saved)

    def test_boss_gt_does_not_refund_stamina_cost(self):
        a, b = fighter('GT'), fighter()
        a.dranzer_state.update(turbo=3, turbo_until=8)
        expected = a.sp - a.cost_for('attack')
        resolve(a, b, 'attack', 'charge', simulate=True)
        # Normal regen is allowed, but it cannot restore the pre-cost reservoir.
        self.assertLess(a.sp, 15)
        self.assertGreaterEqual(a.sp, expected)

    def test_boss_rebirth_second_hit_and_round_clock(self):
        a, b = fighter('V2'), fighter('V2')
        a.hp = 10
        resolve(a, b, 'charge', 'special', simulate=True)
        self.assertTrue(a.dranzer_state['rebirth_used'])
        self.assertLess(a.hp, a.max_hp * .20)
        c, d = fighter('G'), fighter()
        d.combat_round = 8
        resolve(c, d, 'special', 'charge', simulate=True)
        self.assertEqual(c.phoenix_round, 10)
        self.assertFalse(c.can('special'))


if __name__ == '__main__': unittest.main(verbosity=2)
