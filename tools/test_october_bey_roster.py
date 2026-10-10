"""New stock Beys: component forms, real battle effects and independent rare rolls."""
import copy
import unittest
from unittest.mock import patch

from tools.sim_battle_ticks import BattleSession, FakeChannel, FakePlayer, get_beyblade
from utils.character_registry import REGISTRY, load_beys, load_parts
from utils.bey_components import assemble, reconcile
from utils.spin_mode import resolve, modes
from cogs.spawn.spawn import _roll_hidden_spawn, _pick_random_beyblade

NAMES = (
    'Golden Imperial Dragon', 'Black Valkyrie', 'Black Brave Valkyrie',
    'Ultimate Dark Valkyrie', 'Strike Longinus', 'Dragon Circle',
    'Legend Spriggan', 'Shelter Regulus', 'Abyss Fang', 'Deathscyth Longinus',
)
P, E = '1001', '1002'


def build(name, mode=None):
    blade = copy.deepcopy(get_beyblade(name))
    if mode:
        blade = resolve({'spin_mode': {name: mode}}, blade)
    enemy = copy.deepcopy(get_beyblade('Dranzer'))
    enemy.update(name='Test opponent', type='Attack', abilities=[], ability={})
    s = BattleSession(None, FakeChannel(), FakePlayer(1001, 'A'), FakePlayer(1002, 'B'),
                      blade, enemy, payout=False, spend_energy=False)
    for key in (P, E):
        s._prefetch[key] = {'profile': {'wins': 0, 'losses': 0, 'coins': 0, 'xp': 0, 'inventory': []}}
        s.max_hp_per_player[key] = 1000
        s.hp[key] = 200
        s.stability_manager.max[key] = 500
        s.stability_manager.stability[key] = 100
        s.stamina_manager.stamina[key] = 5
    s.last_moves = {P: 'attack', E: 'defense'}
    return s, blade


def snapshot(s):
    return copy.deepcopy((s.hp, s.status.snapshot(P), s.stability_manager.stability,
                          s.stamina_manager.stamina, s.ability.cooldowns))


class RosterData(unittest.TestCase):
    def test_new_identity_and_original_variants_remain_separate(self):
        self.assertEqual(len(load_beys()), 149)
        for i, name in enumerate(NAMES):
            b = REGISTRY.find_bey(name)
            self.assertEqual(b['id'], f'BB{139+i}')
            self.assertEqual(b['ability'], b['abilities'][0])
            self.assertEqual(len(b['abilities']), 2)
            self.assertTrue(b['image_url'].startswith('https://cdn.discordapp.com/attachments/'))
        self.assertNotEqual(get_beyblade('Ultimate Dark Valkyrie')['id'],
                            get_beyblade('Ultimate Valkyrie (Black Edition)')['id'])
        self.assertEqual({k: get_beyblade('Abyss Fang')['stats'][k]
                          for k in ('hp', 'attack', 'defense', 'stamina')},
                         {'hp': 157, 'attack': 180, 'defense': 20, 'stamina': 111})
        self.assertEqual({k: get_beyblade('Deathscyth Longinus')['stats'][k]
                          for k in ('hp', 'attack', 'defense', 'stamina')},
                         {'hp': 170, 'attack': 150, 'defense': 120, 'stamina': 100})

    def test_both_regulus_modes_survive_components_and_are_selectable(self):
        canonical = get_beyblade('Shelter Regulus')
        self.assertEqual(modes(canonical), ['Attack', 'Defense'])
        for mode, kind, attack, defense in [('Attack', 'Attack', 130, 75),
                                            ('Defense', 'Defense', 75, 130)]:
            p = {'inventory': ['Shelter Regulus'], 'active_beyblade': 'Shelter Regulus',
                 'spin_mode': {'Shelter Regulus': mode}}
            reconcile(p)
            mounted = resolve(p, canonical)
            equipped, _ = assemble(p, mounted)
            self.assertEqual(equipped['stats'], mounted['stats'])
            self.assertEqual(equipped['type'], kind)
            self.assertEqual(equipped['stats']['attack'], attack)
            self.assertEqual(equipped['stats']['defense'], defense)
            self.assertEqual(equipped['abilities'], canonical['spin_modes'][mode]['abilities'])
        self.assertEqual(resolve({}, canonical)['active_spin_mode'], 'Attack')

    def test_bundled_parts_are_owned_and_not_shop_stock(self):
        shop_ids = {p['id'] for p in load_parts(shop_only=True)}
        for name in NAMES:
            b = get_beyblade(name)
            p = {'inventory': [name], 'active_beyblade': name}
            reconcile(p)
            result, delta = assemble(p, b)
            self.assertEqual(result['stats'], b['stats'])
            self.assertFalse(any(delta.values()))
            for slot, ident in b['default_parts'].items():
                self.assertNotIn(ident, shop_ids)
                self.assertEqual(REGISTRY.part(ident)['bey_id'], b['id'])
                self.assertTrue(any(item['definition_id'] == ident for item in p['part_instances']))

    def test_deathscyth_independent_one_in_ten_thousand(self):
        b = get_beyblade('Deathscyth Longinus')
        self.assertEqual(b['rarity'], 'Mythic')
        self.assertEqual(b['hidden_drop_one_in'], 10000)
        self.assertFalse(b['booster_exclusive'])
        pool = {b['name']: b}
        with patch('cogs.spawn.spawn.random.randrange', return_value=0) as roll:
            self.assertEqual(_roll_hidden_spawn(pool), b)
            roll.assert_called_once_with(10000)
        with patch('cogs.spawn.spawn.random.randrange', return_value=1):
            self.assertIsNone(_roll_hidden_spawn(pool))
        self.assertIsNone(_pick_random_beyblade(pool))


class AbilityBehavior(unittest.TestCase):
    def test_every_ability_changes_battle_state_or_damage(self):
        for name, mode in [(name, None) for name in NAMES] + [('Shelter Regulus', 'Defense')]:
            base = resolve({'spin_mode': {name: mode}}, get_beyblade(name)) if mode else get_beyblade(name)
            for ab in base['abilities']:
                with self.subTest(name=name, mode=mode, ability=ab['name']):
                    s, blade = build(name, mode)
                    # Isolate each authored ability through the actual battle engine.
                    blade['abilities'] = [copy.deepcopy(ab)]
                    s.ability._compiled.clear()
                    s.ability.once_fired.clear()
                    rule = ab['rules'][0]
                    before = snapshot(s)
                    logs = []
                    damage, _ = s.ability._fire(rule['when'], P, E, blade, 'attack', 'win', 100, 0, logs)
                    self.assertTrue(damage != 100 or snapshot(s) != before, logs)
                    after = snapshot(s)
                    if rule.get('once') == 'battle' or any(o['op'] == 'start_cooldown' for o in rule['do']):
                        logs = []
                        repeated, _ = s.ability._fire(rule['when'], P, E, blade, 'attack', 'win', 100, 0, logs)
                        self.assertEqual(repeated, 100)
                        self.assertEqual(snapshot(s), after)

    def test_conditional_offense_does_not_apply_without_its_condition(self):
        for name in ('Strike Longinus', 'Deathscyth Longinus'):
            s, b = build(name)
            s.hp[E] = 1000
            s.last_moves[E] = 'stamina'
            logs = []
            damage, _ = s.ability._fire('on_attack_hit', P, E, b, 'attack', 'win', 100, 0, logs)
            self.assertEqual(damage, 100)


class RealRoundBehavior(unittest.IsolatedAsyncioTestCase):
    async def test_new_beys_complete_real_attack_charge_and_special_rounds(self):
        for name, mode in [(n, None) for n in NAMES] + [('Shelter Regulus', 'Defense')]:
            with self.subTest(name=name, mode=mode):
                s, _ = build(name, mode)
                for key in (P, E):
                    s.hp[key] = s.max_hp_per_player[key] = 10000
                    s.stability_manager.max[key] = s.stability_manager.stability[key] = 5000
                for own in ('attack', 'charge', 'special'):
                    s.moves = {P: own, E: 'stamina'}
                    for key in (P, E):
                        s.stamina_manager.stamina[key] = s.stamina_manager.cap_for(key)
                        s.stamina_manager.gauge[key] = 150
                    with patch('random.random', return_value=.99):
                        await s._BattleSession__resolve_round_body()
                self.assertLess(s.hp[E], 10000)


if __name__ == '__main__':
    unittest.main()
