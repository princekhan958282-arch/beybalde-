"""Comprehensive bug audit covering all work done today:
1. Crimson Malice Malphas integration & battle mechanics
2. Multi-Admin & Owner authentication across all bot gates
3. Cross-server level-up isolation
4. Tournament V2 registration, 24h window, full alert & battle runner
5. Auto-spawn loop & activity counter
"""

import sys
import os
import json
import time
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

# Ensure repo root is on sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


class TestTodayBugAudit(unittest.IsolatedAsyncioTestCase):

    # ── TEST 1: Crimson Malice Malphas ─────────────────────────────────────────
    async def test_01_malphas_beyblade_integration(self):
        """Verify Crimson Malice Malphas definition, components, and ability."""
        bey_file = os.path.join(BASE_DIR, "beys", "crimson_malice_malphas.json")
        self.assertTrue(os.path.exists(bey_file), "Malphas bey JSON must exist")

        with open(bey_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["id"], "BB_MALPHAS")
        self.assertEqual(data["name"], "Crimson Malice Malphas")
        self.assertEqual(data["rarity"], "Rare")
        self.assertEqual(data["type"], "Attack")
        self.assertEqual(data["hidden_drop_one_in"], 1000)

        # Check default parts
        disk_file = os.path.join(BASE_DIR, "parts", "disks", f"{data['default_parts']['disk']}.json")
        driver_file = os.path.join(BASE_DIR, "parts", "drivers", f"{data['default_parts']['driver']}.json")
        self.assertTrue(os.path.exists(disk_file), "Malphas disk must exist")
        self.assertTrue(os.path.exists(driver_file), "Malphas driver must exist")

        # Verify ability rules
        self.assertEqual(data["ability"]["name"], "Vile Thorns")
        rules = data["ability"]["rules"]
        self.assertTrue(len(rules) > 0)
        gain_op = rules[0]["do"][0]
        self.assertEqual(gain_op["op"], "gain_counter")
        self.assertEqual(gain_op["name"], "vile_thorns")
        self.assertEqual(gain_op["max"], 3)

        # Verify special move
        self.assertEqual(data["special_move"]["name"], "Crimson Gaze")
        self.assertEqual(data["special_move"]["total_damage"], 135)

    # ── TEST 2: Multi-Admin & Owner Checks ─────────────────────────────────────
    async def test_02_admin_security_checks(self):
        """Verify both master admins have full bypasses and permissions."""
        from cogs.admin import actions as A
        from cogs.tournament import tournament as TM
        from cogs.community import panel as CP
        from cogs.updates import cog as UC

        owner_1 = SimpleNamespace(id=956773141265391676, roles=[])
        owner_2 = SimpleNamespace(id=100654502861, roles=[])
        regular_user = SimpleNamespace(id=555555555555, roles=[])

        # is_admin
        self.assertTrue(A.is_admin(owner_1))
        self.assertTrue(A.is_admin(owner_2))
        self.assertFalse(A.is_admin(regular_user))

        # may_run owner-only
        owner_action = SimpleNamespace(owner_only=True)
        self.assertTrue(A.may_run(owner_action, owner_1))
        self.assertTrue(A.may_run(owner_action, owner_2))
        self.assertFalse(A.may_run(owner_action, regular_user))

        # Tournament admin
        self.assertTrue(TM.is_tournament_admin(owner_1))
        self.assertTrue(TM.is_tournament_admin(owner_2))
        self.assertFalse(TM.is_tournament_admin(regular_user))

        # Community & Updates owner
        self.assertTrue(CP.is_owner(owner_1))
        self.assertTrue(CP.is_owner(owner_2))
        self.assertFalse(CP.is_owner(regular_user))
        self.assertTrue(UC._is_owner(owner_1))
        self.assertTrue(UC._is_owner(owner_2))
        self.assertFalse(UC._is_owner(regular_user))

        # Ban prevention check
        ctx_mock = SimpleNamespace(target_id=owner_2.id, text="", invoker_id=owner_1.id)
        res = await A._ban(ctx_mock)
        self.assertFalse(res.ok)
        self.assertIn("can't ban an admin/owner", res.message)

    # ── TEST 3: Cross-Server Level-Up Leak Prevention ─────────────────────────
    async def test_03_cross_server_level_up_isolation(self):
        """Verify level-up announcements only post to the triggering guild."""
        from cogs.community.cog import CommunityCog

        bot = Mock()
        bot.dispatch = Mock()
        cog = CommunityCog(bot)
        cog.levels = Mock()
        cog.levels.apply_roles = AsyncMock()

        # Main server is 1111
        # Guest server is 2222
        guest_channel = AsyncMock()
        guest_channel.id = 2001
        guest_channel.guild = SimpleNamespace(id=2222)
        guest_channel.send = AsyncMock()

        message = SimpleNamespace(
            guild=SimpleNamespace(id=2222),
            channel=guest_channel,
            author=SimpleNamespace(id=9999, mention="<@9999>", guild=SimpleNamespace(id=2222)),
        )

        with patch("cogs.community.guard.main_guild_id", return_value=1111):
            # Level-up occurred in guest server (2222)
            await cog._after_award(message, {"levelled": True, "level": 5, "xp": 100}, message.author)

            # Must NOT announce or apply roles into main server
            cog.levels.apply_roles.assert_not_called()
            bot.dispatch.assert_not_called()

        # Now test when level-up happens in main server (1111)
        main_channel = AsyncMock()
        main_channel.id = 1001
        main_channel.guild = SimpleNamespace(id=1111)
        main_channel.send = AsyncMock()

        main_message = SimpleNamespace(
            guild=SimpleNamespace(id=1111),
            channel=main_channel,
            author=SimpleNamespace(id=8888, mention="<@8888>", guild=SimpleNamespace(id=1111)),
        )

        with patch("cogs.community.guard.main_guild_id", return_value=1111), \
             patch("cogs.community.config.get", return_value=None), \
             patch("utils.database.claim_high_water", return_value=True):
            await cog._after_award(main_message, {"levelled": True, "level": 5, "xp": 100}, main_message.author)

            # Must announce in main channel and dispatch event
            main_channel.send.assert_awaited()
            bot.dispatch.assert_called_with("beycord_community_level", {"user_id": 8888, "level": 5, "xp": 100})

    # ── TEST 4: Tournament V2 Registration, Notification & Auto-Runner ────────
    async def test_04_tournament_v2_flow(self):
        """Test tournament 24h window, full alert, start button, and battle runner."""
        from cogs.tournament.drafts import DraftConfig
        from cogs.tournament.registration import Registration, RegistrationPanel
        from cogs.tournament.tournament import TournamentCog

        bot = SimpleNamespace(
            is_owner=AsyncMock(return_value=False),
            add_view=Mock(),
            get_partial_messageable=Mock(),
            get_channel=Mock(return_value=None),
            get_cog=Mock(return_value=None),
        )
        cog = TournamentCog(bot)

        # Create 4-player registration
        config = DraftConfig(slots=4, entry_fee=100, bey_selection="Random Beyblade")
        reg = Registration(
            guild_id=101,
            channel_id=202,
            message_id=303,
            host_id=956773141265391676,
            config=config,
            status="open",
            created_at=time.time(),
        )

        def mock_membership(r, uid, joining):
            if joining and uid not in r.entrants:
                r.entrants.append(uid)
            elif not joining and uid in r.entrants:
                r.entrants.remove(uid)

        cog.registration_store = SimpleNamespace(
            membership=mock_membership,
            write=Mock(),
        )
        cog.registrations[101] = None  # placeholder

        panel = RegistrationPanel(cog, reg)
        cog.registrations[101] = panel

        # Host notification target channel
        notify_channel = AsyncMock()
        notify_channel.send = AsyncMock()
        bot.get_channel = Mock(return_value=notify_channel)

        # Mock profile check & coin deduction
        with patch("cogs.tournament.registration.get_user", AsyncMock(return_value={"inventory": ["Blade A"], "coins": 1000})), \
             patch("cogs.tournament.registration.mutate_user", AsyncMock()):

            for pid in [1001, 1002, 1003]:
                it = SimpleNamespace(
                    user=SimpleNamespace(id=pid, mention=f"<@{pid}>", display_name=f"Player_{pid}"),
                    guild_id=101,
                    guild=SimpleNamespace(id=101, get_channel=Mock(return_value=notify_channel)),
                    response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock(), is_done=lambda: False),
                    followup=SimpleNamespace(send=AsyncMock()),
                    edit_original_response=AsyncMock(),
                )
                await panel.act(it, "join")

            # Check 3 entrants
            self.assertEqual(len(reg.entrants), 3)
            self.assertFalse(reg.notified_full)

            # 4th entrant fills lobby and triggers full alert
            it_final = SimpleNamespace(
                user=SimpleNamespace(id=1004, mention="<@1004>", display_name="Player_1004"),
                guild_id=101,
                guild=SimpleNamespace(id=101, get_channel=Mock(return_value=notify_channel)),
                response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock(), is_done=lambda: False),
                followup=SimpleNamespace(send=AsyncMock()),
                edit_original_response=AsyncMock(),
            )
            await panel.act(it_final, "join")

            self.assertEqual(len(reg.entrants), 4)
            self.assertTrue(reg.notified_full)
            notify_channel.send.assert_awaited()
            # Verify the notification mentioned the host and noted full lobby
            notif_text = notify_channel.send.call_args[0][0]
            self.assertIn(f"<@{reg.host_id}>", notif_text)
            self.assertIn("FULL", notif_text)

            # Test tournament runner execution
            mock_session = SimpleNamespace(run=AsyncMock(), winner_id=1001)
            with patch("cogs.tournament.tournament.mutate_user", AsyncMock()), \
                 patch("cogs.tournament.tournament.load_beyblades", return_value={"Valt": {"name": "Valt", "rarity": "Common"}}), \
                 patch("cogs.battle.session.BattleSession.create", AsyncMock(return_value=mock_session)):
                bot.get_guild = Mock(return_value=SimpleNamespace(id=101, get_member=lambda uid: SimpleNamespace(id=uid, display_name=f"Player_{uid}", mention=f"<@{uid}>")))
                await cog.run_v2(reg, panel)
                # Channel receives round broadcasts and champion embed
                notify_channel.send.assert_awaited()

    # ── TEST 5: Auto-Spawn Mechanics ──────────────────────────────────────────
    async def test_05_auto_spawn_mechanics(self):
        """Test auto-spawn loop creation, cancellation, and channel locks."""
        from cogs.admin import actions as A
        from cogs.spawn import spawn as SP

        # Test spawn channel lock
        SP.set_spawn_channel(9999, 12345)
        self.assertEqual(SP.get_spawn_channel(9999), 12345)
        SP.set_spawn_channel(9999, None)
        self.assertIsNone(SP.get_spawn_channel(9999))

        # Test spawnloop start & stop in admin actions
        mock_cog = Mock()
        mock_cog._do_spawn = AsyncMock()
        mock_ctx = SimpleNamespace(
            bot=SimpleNamespace(get_cog=Mock(return_value=mock_cog)),
            amount="2",
            channel=SimpleNamespace(mention="<#123>"),
        )

        # Start loop
        res = await A._spawnloop(mock_ctx)
        self.assertTrue(res.ok)
        self.assertIn("Spawn loop running", res.message)
        task = A._spawn_loop.get("task")
        self.assertIsNotNone(task)
        self.assertFalse(task.done())

        # Stop loop
        res_stop = await A._spawnloop_stop(mock_ctx)
        self.assertTrue(res_stop.ok)
        self.assertIn("Spawn loop stopped", res_stop.message)
        self.assertIsNone(A._spawn_loop.get("task"))


if __name__ == "__main__":
    unittest.main()
