"""Unit tests for manual tournament battle execution and listener progression."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from cogs.tournament.tournament import (
    ActiveV2Tournament,
    TournamentCog,
    TournamentMatchView,
    AdvancePlayerView
)
from cogs.tournament.models import Match, MatchState, Mode
from cogs.tournament.drafts import DraftConfig
from cogs.tournament.registration import Registration, RegistrationPanel


class TestTournamentManualBattle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Mock()
        self.bot.is_closed = Mock(return_value=True)
        self.bot.is_owner = AsyncMock(return_value=False)
        self.bot.loop = asyncio.get_event_loop()
        self.cog = TournamentCog(self.bot)
        self.guild = Mock()
        self.guild.id = 123
        self.guild.me = Mock()
        self.channel = Mock()
        self.channel.id = 456
        self.channel.guild = self.guild
        self.channel.send = AsyncMock(return_value=Mock(edit=AsyncMock()))
        self.channel.create_thread = AsyncMock(side_effect=Exception("no threads"))
        self.bot.get_guild = Mock(return_value=self.guild)
        self.bot.get_channel = Mock(return_value=self.channel)

        self.config = DraftConfig(name="Grand Championship", slots=4)
        self.reg = Registration(
            guild_id=self.guild.id,
            host_id=999,
            channel_id=self.channel.id,
            config=self.config,
            entrants=[1, 2, 3, 4],
            status="running"
        )
        self.panel = RegistrationPanel(self.cog, self.reg)

    async def test_active_tournaments_initialized(self):
        self.assertIsInstance(self.cog.active_tournaments, dict)
        self.assertEqual(len(self.cog.active_tournaments), 0)

    async def test_match_view_permissions(self):
        match = Match(player_a=1, player_b=2, round_no=1)
        active = ActiveV2Tournament(
            guild_id=self.guild.id,
            reg=self.reg,
            panel=self.panel,
            mode=Mode.SINGLE.value,
            matches=[match],
            blades={1: "Dranzer S", 2: "Dragoon S"},
            channel=self.channel,
            guild=self.guild
        )
        view = TournamentMatchView(self.cog, active, match, None, None)

        # Stranger clicks Battle Now
        interaction = Mock()
        interaction.user = Mock()
        interaction.user.id = 555
        interaction.user.roles = []
        interaction.response = Mock()
        interaction.response.send_message = AsyncMock()

        await TournamentMatchView.btn_battle(view, interaction, view.btn_battle)
        interaction.response.send_message.assert_awaited_once()
        self.assertIn("Only match participants", interaction.response.send_message.call_args[0][0])

        # Stranger clicks Forfeit
        interaction.response.send_message.reset_mock()
        await TournamentMatchView.btn_forfeit(view, interaction, view.btn_forfeit)
        interaction.response.send_message.assert_awaited_once()
        self.assertIn("Only match participants can forfeit", interaction.response.send_message.call_args[0][0])

        # Stranger clicks Advance Player
        interaction.response.send_message.reset_mock()
        await TournamentMatchView.btn_advance(view, interaction, view.btn_advance)
        interaction.response.send_message.assert_awaited_once()
        self.assertIn("Only the tournament host", interaction.response.send_message.call_args[0][0])

    async def test_forfeit_advances_opponent(self):
        match = Match(player_a=1, player_b=2, round_no=1)
        active = ActiveV2Tournament(
            guild_id=self.guild.id,
            reg=self.reg,
            panel=self.panel,
            mode=Mode.SINGLE.value,
            matches=[match],
            blades={1: "Dranzer S", 2: "Dragoon S"},
            channel=self.channel,
            guild=self.guild
        )
        self.cog.active_tournaments[self.guild.id] = active
        active.match_events[match.id] = asyncio.Event()

        view = TournamentMatchView(self.cog, active, match, None, None)
        interaction = Mock()
        interaction.user = Mock()
        interaction.user.id = 1  # Player 1 forfeits
        interaction.user.roles = []
        interaction.response = Mock()
        interaction.response.send_message = AsyncMock()

        await TournamentMatchView.btn_forfeit(view, interaction, view.btn_forfeit)

        # Match should be completed with Player 2 as winner
        self.assertEqual(match.winner, 2)
        self.assertEqual(match.loser, 1)
        self.assertEqual(match.state, MatchState.COMPLETED.value)
        self.assertTrue(active.match_events[match.id].is_set())

    async def test_battle_event_listener_advances_match(self):
        match = Match(player_a=10, player_b=20, round_no=1)
        active = ActiveV2Tournament(
            guild_id=self.guild.id,
            reg=self.reg,
            panel=self.panel,
            mode=Mode.SINGLE.value,
            matches=[match],
            blades={10: "Dranzer S", 20: "Dragoon S"},
            channel=self.channel,
            guild=self.guild
        )
        self.cog.active_tournaments[self.guild.id] = active
        active.match_events[match.id] = asyncio.Event()

        # Simulate non-matching battle (different players)
        await self.cog.on_beycord_battle_blades({
            "guild_id": self.guild.id,
            "participants": [50, 60],
            "winner_id": 50,
            "draw": False
        })
        self.assertIsNone(match.winner)
        self.assertFalse(active.match_events[match.id].is_set())

        # Simulate draw between 10 and 20
        await self.cog.on_beycord_battle_blades({
            "guild_id": self.guild.id,
            "participants": [10, 20],
            "winner_id": None,
            "draw": True
        })
        self.assertIsNone(match.winner)
        self.assertFalse(active.match_events[match.id].is_set())

        # Simulate decisive win by Player 10
        await self.cog.on_beycord_battle_blades({
            "guild_id": self.guild.id,
            "participants": [20, 10],  # reversed order
            "winner_id": 10,
            "draw": False
        })
        self.assertEqual(match.winner, 10)
        self.assertEqual(match.loser, 20)
        self.assertEqual(match.state, MatchState.COMPLETED.value)
        self.assertTrue(active.match_events[match.id].is_set())

    async def test_admin_force_win(self):
        match = Match(player_a=100, player_b=200, round_no=1)
        active = ActiveV2Tournament(
            guild_id=self.guild.id,
            reg=self.reg,
            panel=self.panel,
            mode=Mode.SINGLE.value,
            matches=[match],
            blades={100: "Dranzer S", 200: "Dragoon S"},
            channel=self.channel,
            guild=self.guild
        )
        self.cog.active_tournaments[self.guild.id] = active
        active.match_events[match.id] = asyncio.Event()

        # Force win player 200
        ok = self.cog.admin_force_win(self.guild.id, 200)
        self.assertTrue(ok)
        await asyncio.sleep(0.01)  # allow task to run

        self.assertEqual(match.winner, 200)
        self.assertEqual(match.loser, 100)
        self.assertEqual(match.state, MatchState.COMPLETED.value)
        self.assertTrue(active.match_events[match.id].is_set())

    async def test_admin_cancel_releases_active(self):
        match = Match(player_a=1, player_b=2, round_no=1)
        active = ActiveV2Tournament(
            guild_id=self.guild.id,
            reg=self.reg,
            panel=self.panel,
            mode=Mode.SINGLE.value,
            matches=[match],
            blades={1: "Dranzer S", 2: "Dragoon S"},
            channel=self.channel,
            guild=self.guild
        )
        self.cog.active_tournaments[self.guild.id] = active
        evt = asyncio.Event()
        active.match_events[match.id] = evt

        cancelled = await self.cog.admin_cancel(self.guild.id)
        self.assertTrue(cancelled)
        self.assertTrue(active.cancelled)
        self.assertTrue(evt.is_set())

    async def test_full_manual_tournament_run(self):
        """End-to-end test of run_v2 advancing rounds via manual battle events."""
        with patch.object(self.cog, "_post_v2_bracket", new=AsyncMock()), \
             patch.object(self.cog, "_post_v2_champion", new=AsyncMock()), \
             patch.object(self.cog, "_award_v2", new=AsyncMock(return_value={1: 6000})), \
             patch.object(self.cog.registration_store, "write", return_value=None), \
             patch("cogs.battle.boss.boss_copy.equipped_blade", new=AsyncMock(return_value=({"name": "Dranzer S"}, False))), \
             patch("cogs.tournament.tournament.get_user", new=AsyncMock(return_value={"inventory": ["Dranzer S"], "coins": 0})):

            # Start the tournament runner in the background
            run_task = asyncio.create_task(self.cog.run_v2(self.reg, self.panel))
            await asyncio.sleep(0.05)  # Let it generate bracket and reach Round 1 await

            active = self.cog.active_tournaments.get(self.guild.id)
            self.assertIsNotNone(active)

            # Round 1 has 2 matches for 4 entrants
            r1_matches = [m for m in active.matches if m.round_no == 1]
            self.assertEqual(len(r1_matches), 2)
            m1, m2 = r1_matches[0], r1_matches[1]

            # Simulate Player m1.player_a winning match 1
            await self.cog.on_beycord_battle_blades({
                "guild_id": self.guild.id,
                "participants": [m1.player_a, m1.player_b],
                "winner_id": m1.player_a,
                "draw": False
            })
            self.assertEqual(m1.winner, m1.player_a)
            self.assertFalse(run_task.done())  # Still waiting for m2!

            # Simulate Player m2.player_b winning match 2
            await self.cog.on_beycord_battle_blades({
                "guild_id": self.guild.id,
                "participants": [m2.player_a, m2.player_b],
                "winner_id": m2.player_b,
                "draw": False
            })
            self.assertEqual(m2.winner, m2.player_b)
            await asyncio.sleep(0.05)  # Let runner advance to Round 2 (Finals)

            # Finals should now be scheduled with m1.player_a vs m2.player_b
            final_matches = [m for m in active.matches if m.round_no == 2]
            self.assertEqual(len(final_matches), 1)
            final_m = final_matches[0]
            self.assertEqual(set(final_m.players), {m1.player_a, m2.player_b})

            # Simulate m1.player_a winning the grand finals
            await self.cog.on_beycord_battle_blades({
                "guild_id": self.guild.id,
                "participants": [final_m.player_a, final_m.player_b],
                "winner_id": m1.player_a,
                "draw": False
            })

            # Tournament finishes!
            await run_task
            self.assertEqual(self.reg.status, "completed")
            self.assertNotIn(self.guild.id, self.cog.active_tournaments)

    async def test_battle_event_listener_string_ids(self):
        match = Match(player_a=1001, player_b=2002, round_no=1)
        active = ActiveV2Tournament(
            guild_id=self.guild.id,
            reg=self.reg,
            panel=self.panel,
            mode=Mode.SINGLE.value,
            matches=[match],
            blades={1001: "Dranzer S", 2002: "Dragoon S"},
            channel=self.channel,
            guild=self.guild
        )
        self.cog.active_tournaments[self.guild.id] = active
        active.match_events[match.id] = asyncio.Event()

        # Pass string user IDs in the payload
        await self.cog.on_beycord_battle_blades({
            "guild_id": str(self.guild.id),
            "participants": ["2002", "1001"],
            "winner_id": "1001",
            "draw": False
        })
        self.assertEqual(match.winner, 1001)
        self.assertEqual(match.loser, 2002)
        self.assertEqual(match.state, MatchState.COMPLETED.value)
        self.assertTrue(active.match_events[match.id].is_set())

    async def test_launch_battle_crash_advances_coin_flip(self):
        match = Match(player_a=10, player_b=20, round_no=1)
        active = ActiveV2Tournament(
            guild_id=self.guild.id,
            reg=self.reg,
            panel=self.panel,
            mode=Mode.SINGLE.value,
            matches=[match],
            blades={10: "Dranzer S", 20: "Dragoon S"},
            channel=self.channel,
            guild=self.guild
        )
        self.cog.active_tournaments[self.guild.id] = active
        active.match_events[match.id] = asyncio.Event()

        # Mock battle session to crash
        with patch("cogs.battle.session.BattleSession.create", side_effect=RuntimeError("Battle engine crash")), \
             patch("cogs.tournament.tournament.get_user", new=AsyncMock(return_value={"inventory": []})):
            await self.cog._launch_interactive_match_battle(active, match, self.channel)

        self.assertIn(match.winner, (10, 20))
        self.assertEqual(match.state, MatchState.COMPLETED.value)
        self.assertTrue(active.match_events[match.id].is_set())

    async def test_admin_cancel_marks_reg_cancelled(self):
        with patch.object(self.cog, "_post_v2_bracket", new=AsyncMock()), \
             patch.object(self.cog.registration_store, "write", return_value=None), \
             patch("cogs.battle.boss.boss_copy.equipped_blade", new=AsyncMock(return_value=({"name": "Dranzer S"}, False))), \
             patch("cogs.tournament.tournament.get_user", new=AsyncMock(return_value={"inventory": ["Dranzer S"], "coins": 0})):

            run_task = asyncio.create_task(self.cog.run_v2(self.reg, self.panel))
            await asyncio.sleep(0.05)

            active = self.cog.active_tournaments.get(self.guild.id)
            self.assertIsNotNone(active)

            cancelled = await self.cog.admin_cancel(self.guild.id)
            self.assertTrue(cancelled)

            await run_task
            self.assertEqual(self.reg.status, "cancelled")
            self.assertNotIn(self.guild.id, self.cog.active_tournaments)


if __name__ == "__main__":
    unittest.main()
