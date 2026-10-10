"""Story and Boss Forfeit regressions: python -m unittest tools.test_pve_forfeit."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.battle.session import _InChannelControlPanel, BattleSession
from cogs.battle.boss import boss_battle as bb
from cogs.story.story_match import LeagueMatch
from tools import test_boss_flow as boss_tests

member = boss_tests.member


def interaction(uid):
    return SimpleNamespace(user=member(uid), channel=SimpleNamespace(send=AsyncMock()),
                           response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                           followup=SimpleNamespace(send=AsyncMock()))


class ForfeitTests(unittest.IsolatedAsyncioTestCase):
    def story_panel(self):
        match = LeagueMatch(None, SimpleNamespace(send=AsyncMock()), member(100), {}, 1, "normal")
        session = SimpleNamespace(story_match=match, original_generation=None, finished=False,
                                  hp={"100": 200, "1": 100}, _resolve_lock=asyncio.Lock(),
                                  _end_battle=AsyncMock(), submit_move=AsyncMock(),
                                  is_player=lambda user: user.id == 100)
        view = _InChannelControlPanel(session)
        session._current_view = view
        return match, session, view

    async def test_story_forfeit_marks_whole_match_and_ends_session(self):
        match, session, view = self.story_panel()
        await view.btn_forfeit.callback(interaction(100))
        self.assertTrue(match.forfeited)
        self.assertEqual(session.hp["100"], 0)
        session._end_battle.assert_awaited_once()

    async def test_story_rejects_spectator_finished_and_stale_clicks(self):
        for state in ("spectator", "finished", "stale"):
            with self.subTest(state=state):
                match, session, view = self.story_panel()
                session.finished = state == "finished"
                if state == "stale":
                    session._current_view = None
                await view.btn_forfeit.callback(interaction(999 if state == "spectator" else 100))
                self.assertFalse(match.forfeited)
                session._end_battle.assert_not_awaited()

    async def test_stale_story_moves_do_not_submit(self):
        _, session, view = self.story_panel()
        session.finished = True
        await view.btn_attack.callback(interaction(100))
        session.submit_move.assert_not_awaited()

    async def test_pvp_panel_has_no_story_forfeit(self):
        view = _InChannelControlPanel(SimpleNamespace(original_generation=None))
        self.assertNotIn("Forfeit", [button.label for button in view.children])

    async def test_story_forfeit_while_ahead_is_loss_and_starts_no_more_rounds(self):
        match, session, view = self.story_panel()
        match.points["player"] = 2
        async def run():
            await view.btn_forfeit.callback(interaction(100))
        session.run = run
        with (patch.object(BattleSession, "create", AsyncMock(return_value=session)) as create,
              patch("cogs.avatar.avatar_skills.end_match_for", new_callable=AsyncMock) as release):
            won = await match.run({"name": "Opponent"})
        self.assertFalse(won)
        self.assertEqual(match.rounds, 1)
        create.assert_awaited_once()
        release.assert_awaited_once_with(100)
        self.assertIn("forfeited the match", match.history[-1])

    def boss_view(self):
        fight = boss_tests.BossFlowTests().make_fight()
        cog = SimpleNamespace(_active={1, 2}, finish=AsyncMock())
        view = bb.BossView(cog, fight)
        view.push = AsyncMock()
        return fight, cog, view

    async def test_boss_host_can_forfeit_out_of_turn_and_releases_party_once(self):
        fight, cog, view = self.boss_view()
        fight.turn_index = 1
        self.assertEqual(fight.active.id, 2)
        await view._forfeit(interaction(1))
        self.assertTrue(fight.finished)
        self.assertEqual(fight.result, "forfeit")
        self.assertFalse(cog._active)
        self.assertTrue(view.is_finished())
        self.assertTrue(all(button.disabled for button in view.children))
        self.assertIn("FORFEITED", fight.card_state()["verdict"])
        self.assertIn("Forfeited", next(field.value for field in view.embed().fields if field.name == "Result"))
        await view._forfeit(interaction(1))
        cog.finish.assert_awaited_once()

    async def test_boss_non_host_and_busy_clicks_do_not_end_fight(self):
        for uid, busy in ((999, False), (2, False), (1, True)):
            fight, cog, view = self.boss_view()
            view.busy = busy
            await view._forfeit(interaction(uid))
            self.assertFalse(fight.finished)
            self.assertEqual(cog._active, {1, 2})
            cog.finish.assert_not_awaited()

    async def test_boss_render_failure_still_releases_party(self):
        fight, cog, view = self.boss_view()
        view.push.side_effect = RuntimeError("render failed")
        with self.assertRaises(RuntimeError):
            await view._forfeit(interaction(1))
        self.assertFalse(cog._active)
        self.assertTrue(view.is_finished())
        self.assertFalse(view.busy)

    async def test_boss_forfeit_skips_reward_database_and_dialogue(self):
        fight, cog, _ = self.boss_view()
        fight.finished, fight.result = True, "forfeit"
        channel = SimpleNamespace(send=AsyncMock())
        with (patch.object(bb, "update_user", new_callable=AsyncMock) as write,
              patch.object(bb.gemini, "say_with_deadline", new_callable=AsyncMock) as say):
            await bb.BossCog.finish(cog, fight, channel)
        write.assert_not_awaited()
        say.assert_not_awaited()
        self.assertFalse(cog._active)
        self.assertIn("forfeited", channel.send.call_args.kwargs["embed"].title)


if __name__ == "__main__":
    unittest.main()
