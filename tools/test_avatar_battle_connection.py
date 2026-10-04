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
                    ranked=False):
        avatar_engine.load()
        self.p1 = H.FakePlayer(101, "Avatar player")
        self.p2 = H.FakePlayer(102, "Opponent")
        profile = dict(equipped_avatar=aid, avatar_skill={aid: slot},
                       avatar_energy=energy)
        if locked is not None:
            profile[AS.K_LOCKED] = locked
        H.seed_profile(101, **profile)
        H.seed_profile(102)
        blade = {"name": "Connection test", "type": "Balance",
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


if __name__ == "__main__":
    unittest.main()
