#!/usr/bin/env python3
"""Exercise tactical effects through real BattleSession one-on-one rounds."""
import asyncio
import copy
import math
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.sim_battle_ticks import BattleSession, FakeChannel, FakePlayer, get_beyblade
from cogs.battle import button_profile
from cogs.abilities.tactical_effects import OPS, TYPES

ATTACK, DEFENSE, STAMINA, CHARGE, SPECIAL = "attack", "defense", "stamina", "charge", "special"
P, E = "1001", "1002"


def build(*effects, blade_type="Balance", enemy_type="Balance", additional_rules=None):
    base = get_beyblade("Void Longinus")
    mine, foe = copy.deepcopy(base), copy.deepcopy(base)
    mine["name"], foe["name"] = "Tactical Tester", "Round Opponent"
    mine["type"], foe["type"] = blade_type, enemy_type
    mine["abilities"] = [{"name": "Tactical", "rules": [
        {"when": "setup", "do": [{"op": op} for op in effects]},
        *(additional_rules or []),
    ]}]
    foe["abilities"] = []
    s = BattleSession(None, FakeChannel(), FakePlayer(1001, "A"),
                      FakePlayer(1002, "B"), mine, foe, payout=False)
    original_send = s.channel.send
    async def send_with_args(*args, **kwargs):
        message = await original_send(*args, **kwargs)
        s.channel.sent[-1]["args"] = args
        return message
    s.channel.send = send_with_args
    assert all(s.ability.tactical.has(P, op) for op in effects)
    return s


async def turn(s, a, b):
    assert not s.finished, "battle finished before the requested round"
    sm = s.stamina_manager
    for key, move in ((P, a), (E, b)):
        # Keep the long synthetic match alive so each operation can be checked
        # across several rounds. Emergency Reserve's first use keeps its
        # deliberately low Stamina and is tested separately.
        reserve = s.ability.tactical.has(key, "emergency_reserve")
        unused = not s.ability.tactical.data(key, "emergency_reserve").get("used")
        if sm.stamina[key] < sm.cost_for(key, move) + .25 and not (reserve and unused):
            sm.stamina[key] = sm.cap_for(key)
    before = len(s.channel.sent)
    s.moves = {P: a, E: b}
    with patch("random.random", return_value=.99):
        await s._resolve_round()
    sent = s.channel.sent[before:]
    for msg in sent:
        for text in msg.get("args", ()):
            if isinstance(text, str) and "Battle Error" in text:
                raise AssertionError(text)
    return "\n".join(str(getattr(m.get("embed"), "description", "") or "") for m in sent)


class PvpRounds(unittest.IsolatedAsyncioTestCase):
    async def test_registered_effects_survive_an_actual_round(self):
        for op in OPS:
            with self.subTest(op=op):
                s = build(op, blade_type=TYPES.get(op, "Balance"))
                await turn(s, DEFENSE, ATTACK)
                self.assertEqual(s.round, 2)
                self.assertTrue(s.ability.tactical.has(P, op))

    async def test_sacrificial_guard_one_on_one(self):
        s = build("sacrificial_guard")
        before = s.hp[P]
        log = await turn(s, DEFENSE, ATTACK)
        self.assertIn("Sacrificial Guard", log)
        self.assertEqual(s.status.get_shield(P), 50)
        self.assertLess(s.hp[P], before)
        await turn(s, STAMINA, ATTACK)
        self.assertLess(s.status.get_shield(P), 50)

    async def test_pattern_pressure_guard_fracture_and_stagger(self):
        s = build("pattern_reader", "pressure_gauge", "guard_fracture", "staggered_rhythm")
        await turn(s, DEFENSE, ATTACK)
        log = await turn(s, CHARGE, ATTACK)
        self.assertIn("Pattern Reader", log)
        before = s.stamina_manager.gauge[P]
        await turn(s, DEFENSE, ATTACK)
        self.assertGreaterEqual(s.stamina_manager.gauge[P], before + 20)
        self.assertTrue(s.ability.tactical.data(P, "guard_fracture").get("ready"))
        self.assertGreater(s.ability.tactical.data(P, "pressure_gauge").get("stacks", 0), 0)
        log = await turn(s, ATTACK, DEFENSE)
        self.assertIn("Guard Fracture", log)
        self.assertGreaterEqual(s.ability.tactical.data(P, "pressure_gauge").get("stacks", 0), 1)

    async def test_recoil_second_wind_and_lasting_guard(self):
        s = build("recoil_engine", "second_wind", "lasting_guard")
        s.max_hp_per_player[P] = 80
        s.hp[P] = 33
        before = s.stamina_manager.stamina[P]
        await turn(s, DEFENSE, ATTACK)
        self.assertTrue(s.ability.tactical.data(P, "lasting_guard").get("active"))
        self.assertTrue(s.ability.tactical.data(P, "recoil_engine").get("ready"))
        s.hp[P] = 80
        log = await turn(s, STAMINA, ATTACK)
        self.assertIn("Lasting Guard", log)
        await turn(s, ATTACK, STAMINA)
        self.assertFalse(s.ability.tactical.data(P, "recoil_engine").get("ready"))
        self.assertTrue(s.ability.tactical.data(P, "second_wind").get("used"))

    async def test_charge_overflow_shield_momentum_and_exposed_core(self):
        s = build("charge_overflow", "shield_momentum", "exposed_core")
        s.stamina_manager.gauge[P] = 145
        await turn(s, CHARGE, STAMINA)
        self.assertEqual(s.stamina_manager.gauge[P], 150)
        self.assertGreater(s.status.get_shield(P), 0)
        s.stamina_manager.gauge[P] = 0
        s.status.add_shield(P, 100)
        await turn(s, STAMINA, ATTACK)
        self.assertGreater(s.stamina_manager.gauge[P], 0)
        s.status.add_shield(E, 10)
        await turn(s, ATTACK, STAMINA)
        self.assertTrue(s.ability.tactical.data(P, "exposed_core").get("ready"))
        log = await turn(s, ATTACK, DEFENSE)
        self.assertIn("Exposed Core", log)

    async def test_opening_rising_finisher_and_measured_strike(self):
        s = build("opening_gambit", "rising_stakes", "finishers_mark", "measured_strike")
        await turn(s, ATTACK, DEFENSE)
        self.assertTrue(s.ability.tactical.data(P, "opening_gambit").get("used"))
        self.assertGreater(s.ability.tactical.data(P, "rising_stakes").get("stacks", 0), 0)
        self.assertGreater(s.stamina_manager.gauge[P], 0)
        s.hp[E] = s.max_hp_per_player[E] = 5000
        for _ in range(3):
            await turn(s, ATTACK, STAMINA)
        self.assertEqual(s.ability.tactical.data(P, "rising_stakes").get("stacks", 0), 0)
        self.assertGreaterEqual(s.ability.tactical.data(P, "finishers_mark").get("marks", 0), 3)
        s.stamina_manager.gauge[P] = 150
        await turn(s, SPECIAL, DEFENSE)
        self.assertLess(s.ability.tactical.data(P, "finishers_mark").get("marks", 0), 3)

    async def test_reserve_timing_tempo_comeback_and_final_rotation(self):
        s = build("emergency_reserve", "perfect_timing", "battle_tempo",
                  "comeback_circuit", "final_rotation")
        sm = s.stamina_manager
        sm.stamina[P] = 1
        s.hp[P] = s.max_hp_per_player[P] // 2
        await turn(s, ATTACK, STAMINA)
        self.assertTrue(s.ability.tactical.data(P, "emergency_reserve").get("used"))
        self.assertGreater(sm.stamina[P], 0)
        self.assertTrue(s.ability.tactical.data(P, "comeback_circuit").get("active"))
        for _ in range(3):
            await turn(s, DEFENSE, DEFENSE)
        before = sm.gauge[P]
        sm.gauge[P] = 0
        await turn(s, CHARGE, STAMINA)
        self.assertGreaterEqual(sm.gauge[P], 70)
        sm.gauge[P] = 150
        await turn(s, SPECIAL, DEFENSE)
        self.assertGreaterEqual(sm.gauge[P], 30)
        s.round = 8
        sm.gauge[P] = 0
        await turn(s, DEFENSE, ATTACK)
        self.assertTrue(s.ability.tactical.data(P, "final_rotation").get("used"))
        self.assertGreater(sm.gauge[P], 0)

    async def test_type_effects_and_stability_anchor(self):
        attack = build("precision_window", blade_type="Attack")
        await turn(attack, ATTACK, STAMINA)
        self.assertTrue(attack.ability.tactical.data(P, "precision_window").get("ready"))
        await turn(attack, ATTACK, DEFENSE)
        self.assertFalse(attack.ability.tactical.data(P, "precision_window").get("ready"))

        stamina = build("spin_siphon", blade_type="Stamina")
        before_stamina = stamina.stamina_manager.stamina[E]
        await turn(stamina, STAMINA, DEFENSE)
        self.assertIn("Spin Siphon", "\n".join(str(getattr(m.get("embed"), "description", "")) for m in stamina.channel.sent))
        self.assertLess(stamina.stamina_manager.stamina[E], before_stamina)

        defense = build("stability_anchor", blade_type="Defense")
        defense.stability_manager.stability[P] = 1
        await turn(defense, ATTACK, STAMINA)
        self.assertTrue(defense.ability.tactical.data(P, "stability_anchor").get("used"))
        self.assertEqual(defense.stability_manager.stability[P], 1)

    async def test_clean_break_counterweight_and_shield_protection(self):
        s = build("clean_break", "counterweight", additional_rules=[
            {"when": "on_attack_hit", "do": [{"op": "cleanse"}]},
        ])
        s.status.add_buff(P, "attack", -10, 3)
        s.battle_stats[E]["attack"] = s.battle_stats[P]["attack"] + 100
        log = await turn(s, ATTACK, STAMINA)
        self.assertIn("cleansed", log)
        self.assertGreaterEqual(s.ability.tactical.data(P, "clean_break").get("through", -1), 1)
        self.assertEqual(s.status.get_buff_bonus(P, "attack"), 0)

    async def test_counterweight_changes_real_defense_result(self):
        guarded, plain = build("counterweight"), build()
        for s in (guarded, plain):
            s.blades[E]["stats"]["attack"] += 100
        await turn(guarded, DEFENSE, ATTACK)
        await turn(plain, DEFENSE, ATTACK)
        self.assertLess(guarded.hp[E], plain.hp[E])

    async def test_measured_strike_reduces_followup_real_hit(self):
        measured, plain = build("measured_strike"), build()
        for s in (measured, plain):
            s.max_hp_per_player[E] = 60
            s.hp[E] = 2101
        await turn(measured, ATTACK, DEFENSE)
        await turn(plain, ATTACK, DEFENSE)
        self.assertTrue(measured.ability.tactical.data(P, "measured_strike").get("ready"))
        await turn(measured, ATTACK, DEFENSE)
        await turn(plain, ATTACK, DEFENSE)
        self.assertGreater(measured.hp[E], plain.hp[E])

    async def test_exposed_core_pierces_real_special_defense(self):
        exposed = build("exposed_core", blade_type="Attack", enemy_type="Defense")
        plain = build(blade_type="Attack", enemy_type="Defense")
        for s in (exposed, plain):
            s.status.add_shield(E, 10)
            await turn(s, ATTACK, STAMINA)
            s.stamina_manager.gauge[P] = 150
        self.assertTrue(exposed.ability.tactical.data(P, "exposed_core").get("ready"))
        log = await turn(exposed, SPECIAL, DEFENSE)
        await turn(plain, SPECIAL, DEFENSE)
        self.assertIn("Exposed Core", log)
        self.assertLess(exposed.hp[E], plain.hp[E])


if __name__ == "__main__":
    unittest.main(verbosity=2)
