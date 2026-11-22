"""Unit tests for Trypio/Garuda nerfs, Continuous Defense surcharge, and Defense Breaker."""
import copy
import unittest
from unittest.mock import patch, AsyncMock
from tools.sim_battle_ticks import BattleSession, FakeChannel, FakePlayer, get_beyblade
from cogs.battle.constants import MOVE_ATTACK, MOVE_DEFENSE, MOVE_STAMINA, MOVE_CHARGE


class TestDefenseBreakerAndNerfs(unittest.IsolatedAsyncioTestCase):
    def _create_session(self, name1="Trypio", name2="Eternal Blaze Garuda"):
        blade1 = copy.deepcopy(get_beyblade(name1))
        blade2 = copy.deepcopy(get_beyblade(name2))
        ch = FakeChannel()
        p1 = FakePlayer(1001, "Player1")
        p2 = FakePlayer(1002, "Player2")
        s = BattleSession(
            bot=None, channel=ch, p1=p1, p2=p2,
            blade1=blade1, blade2=blade2,
            payout=False, avatars_enabled=False
        )
        for key in ("1001", "1002"):
            s._prefetch[key] = {
                "profile": {"wins": 0, "losses": 0, "coins": 0, "xp": 0, "inventory": []}
            }
            s.hp[key] = s.max_hp_per_player[key] = 2500
            s.bey_levels[key] = 100
            s.stability_manager.stability[key] = s.stability_manager.max[key] = 1000
            s.stamina_manager.stamina[key] = 10.0
        return s

    async def test_trypio_healing_nerf_to_112(self):
        """Trypio's stamina action heal is capped at 112 HP (previously ~198 HP)."""
        s = self._create_session("Trypio", "Eternal Blaze Garuda")
        k1, k2 = "1001", "1002"
        s.hp[k1] = 1000
        # Normal stamina stat at level 100 is > 200 (283), which would heal 198 without cap
        heal, _, _ = s.type_gimmicks.recovery(k1, 283)
        self.assertEqual(heal, 112, "Trypio recovery() should be capped at 112 HP")

        # Set effective stamina stat to 283
        s.stamina_manager._eff = {k1: {"stamina": 283}, k2: {"stamina": 283}}

        # Test in stamina_manager.apply_stamina_action
        hp_dict = {k1: 1000}
        logs = s.stamina_manager.apply_stamina_action(
            k1, hp_dict, s.type_mods.get(k1), attacked=False, max_hp=2500, gimmicks=s.type_gimmicks
        )
        # Should have healed exactly 112 HP (1000 + 112 = 1112)
        self.assertEqual(hp_dict[k1], 1112)
        self.assertTrue(any("112 HP" in l for l in logs))

        # Non-Trypio beyblade should still heal normal 70% of stamina stat
        heal_other, _, _ = s.type_gimmicks.recovery(k2, 283)
        self.assertAlmostEqual(heal_other, 283 * 0.70, delta=1)
        self.assertGreater(heal_other, 190)

    async def test_continuous_defense_surcharge(self):
        """Continuous defense increases stamina cost: 2nd round +1.0, 3rd +1.5, 4th +2.0, etc."""
        s = self._create_session("Victory Valkyrie", "Aegis Valorian")
        k1, k2 = "1001", "1002"
        sm = s.stamina_manager

        base_def_cost = sm.cost_for(k1, MOVE_DEFENSE)

        # Round 1: streak = 0 -> base cost
        self.assertEqual(sm.consecutive_defense.get(k1, 0), 0)
        self.assertEqual(sm.cost_for(k1, MOVE_DEFENSE), base_def_cost)

        # Execute Round 1 with Defense
        s.moves = {k1: MOVE_DEFENSE, k2: MOVE_DEFENSE}
        await s._BattleSession__resolve_round_body()

        # After Round 1: streak should be 1
        self.assertEqual(sm.consecutive_defense.get(k1), 1)
        # Round 2 cost: base + 1.0
        self.assertAlmostEqual(sm.cost_for(k1, MOVE_DEFENSE), base_def_cost + 1.0, places=2)

        # Execute Round 2 with Defense
        s.moves = {k1: MOVE_DEFENSE, k2: MOVE_DEFENSE}
        await s._BattleSession__resolve_round_body()

        # After Round 2: streak should be 2
        self.assertEqual(sm.consecutive_defense.get(k1), 2)
        # Round 3 cost: base + 1.5
        self.assertAlmostEqual(sm.cost_for(k1, MOVE_DEFENSE), base_def_cost + 1.5, places=2)

        # Execute Round 3 with Defense
        s.moves = {k1: MOVE_DEFENSE, k2: MOVE_DEFENSE}
        await s._BattleSession__resolve_round_body()

        # After Round 3: streak should be 3
        self.assertEqual(sm.consecutive_defense.get(k1), 3)
        # Round 4 cost: base + 2.0
        self.assertAlmostEqual(sm.cost_for(k1, MOVE_DEFENSE), base_def_cost + 2.0, places=2)

        # Execute Round 4 with Attack (switching moves resets streak)
        s.moves = {k1: MOVE_ATTACK, k2: MOVE_DEFENSE}
        await s._BattleSession__resolve_round_body()

        # Streak should now reset to 0
        self.assertEqual(sm.consecutive_defense.get(k1), 0)
        self.assertEqual(sm.cost_for(k1, MOVE_DEFENSE), base_def_cost)

    def _get_round_desc(self, s):
        for item in reversed(s.channel.sent):
            emb = item.get("embed")
            if emb and emb.description:
                return emb.description
        return ""

    async def test_garuda_passive_stamina_canceled_when_hit(self):
        """Garuda's Eternal Flame & Iron Wing Veil stamina gains are canceled if Garuda was hit."""
        s = self._create_session("Victory Valkyrie", "Eternal Blaze Garuda")
        k_valt, k_garuda = "1001", "1002"
        sm = s.stamina_manager

        # Case 1: Garuda defends, Valt attacks -> Garuda is hit!
        sm.stamina[k_garuda] = 10.0
        s.moves = {k_valt: MOVE_ATTACK, k_garuda: MOVE_DEFENSE}
        with patch("random.random", return_value=0.99):
            await s._BattleSession__resolve_round_body()

        # Check logs for cancellation message
        desc = self._get_round_desc(s)
        found_ef_cancel = any("Eternal Flame" in l and "stamina recovery canceled" in l for l in desc.split("\n"))
        self.assertTrue(found_ef_cancel, "Eternal Flame cancellation should be logged when hit")

        # Case 2: Garuda defends, Valt uses Stamina -> Garuda is NOT hit!
        sm.stamina[k_garuda] = 10.0
        s.channel.sent.clear()
        s.moves = {k_valt: MOVE_STAMINA, k_garuda: MOVE_DEFENSE}
        with patch("random.random", return_value=0.99):
            await s._BattleSession__resolve_round_body()

        desc = self._get_round_desc(s)
        self.assertNotIn("stamina recovery canceled", desc)

    async def test_trypio_passive_stamina_canceled_when_hit(self):
        """Trypio's Updraft Glide passive stamina gain is canceled if Trypio was hit."""
        s = self._create_session("Trypio", "Victory Valkyrie")
        k_trypio, k_valt = "1001", "1002"
        sm = s.stamina_manager

        # Case 1: Trypio defends, Valt attacks -> Trypio is hit!
        sm.stamina[k_trypio] = 10.0
        s.moves = {k_trypio: MOVE_DEFENSE, k_valt: MOVE_ATTACK}
        with patch("random.random", return_value=0.99):
            await s._BattleSession__resolve_round_body()

        desc = self._get_round_desc(s)
        found_trypio_cancel = any("Updraft Glide" in l and "stamina recovery canceled" in l for l in desc.split("\n"))
        self.assertTrue(found_trypio_cancel, "Updraft Glide cancellation should be logged when hit")

        # Case 2: Trypio defends, Valt uses Stamina -> Trypio is NOT hit!
        sm.stamina[k_trypio] = 10.0
        s.channel.sent.clear()
        s.moves = {k_trypio: MOVE_DEFENSE, k_valt: MOVE_STAMINA}
        with patch("random.random", return_value=0.99):
            await s._BattleSession__resolve_round_body()

        desc = self._get_round_desc(s)
        self.assertNotIn("stamina recovery canceled", desc)

    async def test_defense_breaker_mechanic(self):
        """Defense Breaker triggers on 3rd consecutive Attack vs Defense clash (50% base chance)."""
        s = self._create_session("Victory Valkyrie", "Aegis Valorian")
        k_atk, k_def = "1001", "1002"

        # Turn 1: Attack vs Defense -> streak becomes 1
        s.moves = {k_atk: MOVE_ATTACK, k_def: MOVE_DEFENSE}
        with patch("random.random", return_value=0.10):  # Even if low roll, no proc on turn 1
            await s._BattleSession__resolve_round_body()
        self.assertEqual(s.atk_vs_def_streak.get((k_atk, k_def)), 1)
        self.assertEqual(s.defense_locked_turns.get(k_def, 0), 0)

        # Turn 2: Attack vs Defense -> streak becomes 2
        s.moves = {k_atk: MOVE_ATTACK, k_def: MOVE_DEFENSE}
        with patch("random.random", return_value=0.10):  # No proc on turn 2
            await s._BattleSession__resolve_round_body()
        self.assertEqual(s.atk_vs_def_streak.get((k_atk, k_def)), 2)
        self.assertEqual(s.defense_locked_turns.get(k_def, 0), 0)

        # Turn 3: Attack vs Defense -> streak becomes 3 -> 50% chance!
        # Roll 0.40 (< 0.50) -> PROC Guard Break!
        s.moves = {k_atk: MOVE_ATTACK, k_def: MOVE_DEFENSE}
        with patch("random.random", return_value=0.40):
            await s._BattleSession__resolve_round_body()

        # Streak resets
        self.assertEqual(s.atk_vs_def_streak.get((k_atk, k_def)), 0)
        # Defense locked for next turn
        self.assertEqual(s.defense_locked_turns.get(k_def), 1)

        # Check Guard Break in logs
        desc = self._get_round_desc(s)
        self.assertTrue(any("GUARD BREAK!" in l for l in desc.split("\n")))
        # Check -15% defense debuff applied
        def_buff = sum(b["amount"] for b in s.status.active_buffs.get(k_def, []) if b["stat"] == "defense")
        self.assertLess(def_buff, 0, "Defender should have received a Defense debuff (-15%)")

        # Turn 4: Defender attempts to use Defense -> blocked!
        mock_interaction = AsyncMock()
        mock_interaction.user.id = 1002
        await s.submit_move(mock_interaction, mock_interaction.user, MOVE_DEFENSE)
        mock_interaction.response.send_message.assert_called_with(
            "🛡️ **Defense is broken!** You cannot use Defense this turn.", ephemeral=True
        )

        # Defender uses Stamina instead on Turn 4
        s.moves = {k_atk: MOVE_ATTACK, k_def: MOVE_STAMINA}
        await s._BattleSession__resolve_round_body()

        # After Turn 4 finishes, lockout decrements to 0
        self.assertEqual(s.defense_locked_turns.get(k_def), 0)

    async def test_defense_breaker_scaling_chance(self):
        """If turn 3 fails (50%), turn 4 clash scales to 55% chance."""
        s = self._create_session("Victory Valkyrie", "Aegis Valorian")
        k_atk, k_def = "1001", "1002"

        # Turn 1
        s.moves = {k_atk: MOVE_ATTACK, k_def: MOVE_DEFENSE}
        await s._BattleSession__resolve_round_body()

        # Turn 2
        s.moves = {k_atk: MOVE_ATTACK, k_def: MOVE_DEFENSE}
        await s._BattleSession__resolve_round_body()

        # Turn 3: 50% chance, roll 0.52 (fails to proc)
        s.moves = {k_atk: MOVE_ATTACK, k_def: MOVE_DEFENSE}
        with patch("random.random", return_value=0.52):
            await s._BattleSession__resolve_round_body()
        self.assertEqual(s.atk_vs_def_streak.get((k_atk, k_def)), 3)
        self.assertEqual(s.defense_locked_turns.get(k_def, 0), 0)

        # Turn 4: 55% chance, roll 0.53 (< 0.55, procs!)
        s.moves = {k_atk: MOVE_ATTACK, k_def: MOVE_DEFENSE}
        with patch("random.random", return_value=0.53):
            await s._BattleSession__resolve_round_body()
        self.assertEqual(s.defense_locked_turns.get(k_def), 1)
        self.assertEqual(s.atk_vs_def_streak.get((k_atk, k_def)), 0)


if __name__ == "__main__":
    unittest.main()
