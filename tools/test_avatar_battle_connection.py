"""Regression checks against real sessions and an in-memory profile store.

Run: python tools/test_avatar_battle_connection.py
"""
import asyncio
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import sim_story as H
from cogs.avatar import avatar_skills as AS
from cogs.avatar.avatar_engine import avatar_engine
from cogs.battle.purification import effective_stats
from cogs.battle.type_gimmicks import passive_stat_multiplier


class AvatarConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def build(self, aid=None, slot=1, energy=100, locked=None,
                    ranked=False, blade=None):
        avatar_engine.load()
        self.p1 = H.FakePlayer(101, "Avatar player")
        self.p2 = H.FakePlayer(102, "Opponent")
        profile = dict(equipped_avatar=aid, avatar_skill={aid: slot},
                       avatar_energy=energy)
        if locked is not None:
            profile[AS.K_LOCKED] = locked
        H.seed_profile(101, **profile)
        H.seed_profile(102)
        blade = copy.deepcopy(blade) if blade is not None else {"name": "Connection test", "type": "Balance",
                 "stats": {"attack": 100, "defense": 100,
                           "stamina": 100, "special": 100, "hp": 100},
                 "abilities": []}
        return await H.BattleSession.create(
            None, H.FakeChannel(), self.p1, self.p2,
            blade, copy.deepcopy(blade), ranked=ranked, payout=False)

    async def test_changed_pick_replaces_stale_lock(self):
        s = await self.build("avatar_x002", slot=1, locked=2)
        self.assertEqual(s.skill_commit["101"]["slot"], 1)
        self.assertEqual(s.avatar_bonuses["101"].attack_percent, .30)
        self.assertEqual(s.avatar_bonuses["101"].crit_percent, 0)
        self.assertEqual(effective_stats(s, "101")["attack"], 130 * passive_stat_multiplier("Balance", "attack"))

    async def test_exhausted_energy_removes_bonus_skill(self):
        s = await self.build("avatar_x002", slot=1, energy=0, ranked=True)
        self.assertFalse(s.skill_commit["101"]["afforded"])
        self.assertEqual(s.avatar_bonuses["101"].attack_percent, 0)
        self.assertEqual(effective_stats(s, "101")["attack"], 100 * passive_stat_multiplier("Balance", "attack"))

    async def test_exhausted_energy_removes_rule_skills(self):
        s = await self.build("avatar_s107", energy=0, ranked=True)
        self.assertEqual(s.avatar_skill_slots["101"], 0)
        self.assertEqual(s.ability._avatar_rules_for("101"), [])
        # The card's permanent statline is retained.
        self.assertEqual(s.avatar_bonuses["101"].attack_flat, 52)

    async def test_rule_skill_changes_actual_damage(self):
        s = await self.build("avatar_s107", slot=1)
        s.moves = {"101": "attack", "102": "stamina"}
        s.type_gimmicks.rng = lambda: .99
        s.type_gimmicks.start_round()
        s.type_gimmicks.begin_round(s.moves, started=True)
        with patch("cogs.abilities.ability_engine.random.random", return_value=0):
            base, _, _ = s.ability.apply("101", "102", s.blades["101"],
                                        s.blades["102"], "attack", "win", 100, 0)
        self.assertGreater(base, 100)
        self.assertEqual({r["_name"] for _, r in
                          s.ability._avatar_rules_for("101")},
                         {avatar_engine.get_avatar("avatar_s107")["skills"][0]["name"]})

    async def test_charge_bonus_reaches_gauge_and_cap(self):
        s = await self.build("avatar_r002", slot=3)
        av = s.avatar_bonuses["101"]
        s.stamina_manager.add_gauge("101", "charge")
        self.assertEqual(s.stamina_manager.gauge["101"], av.apply_charge_bonus(50))
        s.stamina_manager.gauge["101"] = 149
        s.stamina_manager.add_gauge("101", "charge")
        self.assertEqual(s.stamina_manager.gauge["101"], 150)
        s.stamina_manager.gauge["101"] = 0
        s.stamina_manager.add_gauge("101", "attack")
        self.assertEqual(s.stamina_manager.gauge["101"], 15)

    async def test_avatar_crit_refunds_gauge(self):
        card = avatar_engine.get_avatar("avatar_x002")
        slot = next(i for i, sk in enumerate(card["skills"], 1)
                    if sk.get("bonuses", {}).get("gauge_on_crit"))
        s = await self.build(card["id"], slot=slot)
        s.moves = {"101": "attack", "102": "stamina"}
        s.type_gimmicks.rng = lambda: .99
        s.type_gimmicks.start_round()
        s.type_gimmicks.begin_round(s.moves, started=True)
        with patch("cogs.battle.avatar_combat.random.random", return_value=0):
            damage, _, logs = s.ability.apply(
                "101", "102", s.blades["101"], s.blades["102"],
                "attack", "win", 100, 0)
        self.assertEqual(damage, 200)
        self.assertEqual(s.stamina_manager.gauge["101"], 50)
        self.assertTrue(any("critical hit charges" in line for line in logs))

    async def test_natural_crit_also_refunds_gauge(self):
        s = await self.build("avatar_x002", slot=2)
        s.blades["101"]["type"] = "Attack"
        from cogs.battle.type_gimmicks import TypeGimmickEngine
        s.type_gimmicks = TypeGimmickEngine(
            {"101": "Attack", "102": "Balance"}, rng=lambda: 0)
        s.moves = {"101": "attack", "102": "stamina"}
        s.type_gimmicks.start_round()
        s.type_gimmicks.begin_round(s.moves, started=True)
        s.ability.apply("101", "102", s.blades["101"], s.blades["102"],
                        "attack", "win", 100, 0)
        self.assertEqual(s.stamina_manager.gauge["101"], 50)

    async def test_no_avatar_keeps_base_stats_and_gauge(self):
        s = await self.build()
        self.assertEqual(effective_stats(s, "101")["attack"], 100 * passive_stat_multiplier("Balance", "attack"))
        self.assertEqual(s.ability._avatar_rules_for("101"), [])
        s.stamina_manager.add_gauge("101", "charge")
        self.assertEqual(s.stamina_manager.gauge["101"], 50)

    async def test_dyrroth_special_bonus_applied_once_in_pvp(self):
        s = await self.build("avatar_x003", slot=3)
        # Use an authored two-hit Special to catch both double scaling and
        # adding the full Attack rider on every hit.
        s.blades["101"]["special_move"] = {"hits": 2, "damage_per_hit": 100,
                                             "ignores_defense": True}
        damage, _ = s.attack_manager._resolve_special(
            "101", "102", "special", s.blades["101"], s.blades["102"], [])
        atk = effective_stats(s, "101")["attack"]
        self.assertEqual(damage, round(2 * 211 + atk))

    async def test_dyrroth_formula_special_bonus_in_pvp(self):
        s = await self.build("avatar_x003", slot=3)
        s.blades["101"]["special_move"] = {
            "damage_formula": {"base": 100}, "ignores_defense": True}
        damage, _ = s.attack_manager._resolve_special(
            "101", "102", "special", s.blades["101"], s.blades["102"], [])
        self.assertEqual(damage, round(211 + effective_stats(s, "101")["attack"]))

    async def test_xeno_generated_special_receives_dyrroth_bonus(self):
        for stacks in (0, 3):
            damages = []
            attacks = []
            for aid in (None, 'avatar_x003'):
                s = await self.build(aid, slot=3, blade=H.get_beyblade('Xeno Xcalius'))
                s.type_gimmicks.rng = lambda: .99
                s.moves = {'101': 'special', '102': 'charge'}
                s.ability.counters[('101', 'xeno_stack')] = stacks
                damage, logs = s.attack_manager._resolve_special(
                    '101', '102', 'special', s.blades['101'], s.blades['102'], [])
                damages.append(damage)
                attacks.append(effective_stats(s, '101')['attack'])
                if aid:
                    self.assertTrue(any('full Attack stat added' in line for line in logs))
            self.assertGreater(damages[1], damages[0], (stacks, damages))
            self.assertEqual(damages[1], round(round(damages[0] * 2.11) + attacks[1]))

    async def test_reported_1456_damage_is_not_unchanged(self):
        results = []
        for aid in (None, 'avatar_x003'):
            s = await self.build(aid, slot=3, blade=H.get_beyblade('Xeno Xcalius'))
            # Calibrated fixture: Xeno's 200% ATK DSL yields 1456 damage.
            s.battle_stats['101']['attack'] = 728
            live = {key: effective_stats(s, key) for key in s.blades}
            live['101']['attack'] = 728
            s.effective_stats_for = lambda key: live[key]
            damage, _ = s.attack_manager._resolve_special(
                '101', '102', 'special', s.blades['101'], s.blades['102'], [])
            results.append(damage)
        self.assertEqual(results, [1456, 3800])

    async def test_generated_special_rider_once_across_hits(self):
        s = await self.build('avatar_x003', slot=3)
        blade = s.blades['101']
        blade['special_move'] = {'non_damage': True, 'hits': 2, 'damage_per_hit': 0}
        blade['abilities'] = [{'name': 'Generated hits', 'rules': [
            {'when': 'passive', 'do': [{'op': 'bonus_damage', 'value': 100}]}]}]
        blade['name'] = 'Generated Special fixture'
        s.ability._compiled.clear()
        s.avatar_bonuses['101'].special_move_flat = 10
        damage, logs = s.attack_manager._resolve_special(
            '101', '102', 'special', blade, s.blades['102'], [])
        self.assertEqual(damage, round(110 * 2.11) + round(100 * 2.11) +
                         round(effective_stats(s, '101')['attack']))
        self.assertEqual(sum('full Attack stat added' in l for l in logs), 1)

    async def test_support_only_special_remains_zero_damage(self):
        s = await self.build('avatar_x003', slot=3, blade=H.get_beyblade('Deep Caynox'))
        damage, logs = s.attack_manager._resolve_special(
            '101', '102', 'special', s.blades['101'], s.blades['102'], [])
        self.assertEqual(damage, 0)
        self.assertFalse(any('full Attack stat added' in l for l in logs))

    async def test_other_rule_generated_special_is_boosted(self):
        outcomes = []
        for aid in (None, 'avatar_x003'):
            s = await self.build(aid, slot=3, blade=H.get_beyblade('Astral Valkyrie Starbreaker'))
            damage, _ = s.attack_manager._resolve_special(
                '101', '102', 'special', s.blades['101'], s.blades['102'], [])
            outcomes.append(damage)
        self.assertGreater(outcomes[1], outcomes[0])

    async def test_dyrroth_special_bonus_reaches_boss_hp_and_ai(self):
        from cogs.battle.boss import boss_battle as BB, boss_ai as AI
        for special_stat in (0, 100):
            with self.subTest(special_stat=special_stat):
                s = await self.build("avatar_x003", slot=3)
                blade = copy.deepcopy(s.blades["101"])
                blade["stats"]["special"] = special_stat
                with patch.object(BB.bcopy, "equipped_blade",
                                  return_value=(blade, None)):
                    boosted, _ = await BB._player_fighter(101)
                baseline = boosted.clone()
                baseline.avatar_bonuses = None
                baseline.special_mult = 1
                target = AI.Fighter("Boss", 100000, 100000, 100, 100, 100,
                                    special_atk_pct=3.9)
                baseline.gauge = 150
                with patch('cogs.battle.type_gimmicks.random.random', return_value=.99):
                    base_report = AI.resolve(target.clone(), baseline.clone(),
                                             "charge", "special")
                raw = AI._raw_damage(baseline, True)
                expected = round(raw * 2.11) + round(boosted.eff_attack)
                self.assertEqual(AI._raw_damage(boosted, True), expected)
                boosted.gauge = 150
                before = target.hp
                with patch('cogs.battle.type_gimmicks.random.random', return_value=.99):
                    report = AI.resolve(target, boosted, "charge", "special")
                self.assertEqual(before - target.hp, report["dmg_to_a"])
                self.assertEqual(report["dmg_to_a"],
                                 int(expected * AI.PLAYER_SPECIAL_VS_BOSS))
                self.assertGreater(report["dmg_to_a"], base_report["dmg_to_a"])
                self.assertTrue(any("full Attack stat added" in line
                                    for line in report["gimmicks"]))
                clone = baseline.clone()
                clone.avatar_bonuses = copy.deepcopy(boosted.avatar_bonuses)
                self.assertEqual(AI._raw_damage(clone, True), expected)

    async def test_boss_other_skill_does_not_grant_dyrroth_ultimate(self):
        from cogs.battle.boss import boss_battle as BB, boss_ai as AI
        s = await self.build("avatar_x003", slot=1)
        with patch.object(BB.bcopy, "equipped_blade",
                          return_value=(s.blades["101"], None)):
            fighter, _ = await BB._player_fighter(101)
        plain = fighter.clone()
        plain.avatar_bonuses = None
        self.assertEqual(AI._raw_damage(fighter, True), AI._raw_damage(plain, True))

    async def test_boss_authored_special_uses_bonus_once(self):
        from cogs.battle.boss import boss_ai as AI
        s = await self.build("avatar_x003", slot=3)
        fighter = AI.Fighter("Player", 10000, 10000, 100, 100, 100,
                             special_damage=1456,
                             avatar_bonuses=s.avatar_bonuses["101"])
        self.assertEqual(AI._raw_damage(fighter, True), round(1456 * 2.11) + 100)
        self.assertEqual(AI._raw_damage(fighter.clone(), True),
                         AI._raw_damage(fighter, True))


if __name__ == "__main__":
    unittest.main()
