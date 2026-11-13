"""Unit tests for the Clan War system and ClanCog integrations."""

import asyncio
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.clans import clan_data as cd
from cogs.clans.clan import ClanCog
from cogs.clans.clan_war import ClanWarCog, war_for_clan, MIN_STAKE, WAR_DURATION


class TestClanWarSystem(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.clans_file = os.path.join(self.test_dir, "clans.json")
        self.wars_file = os.path.join(self.test_dir, "clan_wars.json")

        self.patch_clans = patch("cogs.clans.clan_data.CLANS_PATH", self.clans_file)
        self.patch_wars = patch("cogs.clans.clan_war.WARS_PATH", self.wars_file)
        self.patch_clans.start()
        self.patch_wars.start()

        self.bot = Mock()
        self.bot.is_closed = Mock(return_value=False)
        self.bot.loop = asyncio.get_event_loop()
        self.bot.get_channel = Mock(return_value=None)
        self.war_cog = ClanWarCog(self.bot)
        self.clan_cog = ClanCog(self.bot)
        self.bot.get_cog = Mock(side_effect=lambda name: {
            "Clan Wars": self.war_cog,
            "Clans": self.clan_cog,
        }.get(name))

    def tearDown(self):
        self.patch_wars.stop()
        self.patch_clans.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_mock_ctx(self, author_id: int):
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=author_id, mention=f"<@{author_id}>"),
            channel=SimpleNamespace(id=777, send=AsyncMock()),
            send=AsyncMock(),
        )
        return ctx

    def test_find_by_name_and_brackets(self):
        """Test clan lookup by exact name, tag, bracketed tag, and leading hash."""
        clan = cd.create_clan(101, "Dragon Riders", "DRGN")
        self.assertEqual(cd.find_by_name("Dragon Riders")["id"], clan["id"])
        self.assertEqual(cd.find_by_name("dragon riders")["id"], clan["id"])
        self.assertEqual(cd.find_by_name("DRGN")["id"], clan["id"])
        self.assertEqual(cd.find_by_name("drgn")["id"], clan["id"])
        self.assertEqual(cd.find_by_name("[DRGN]")["id"], clan["id"])
        self.assertEqual(cd.find_by_name("#DRGN")["id"], clan["id"])
        self.assertIsNone(cd.find_by_name("NonExistent"))

    async def test_war_declare_success(self):
        """Declaring war properly validates treasuries and creates pending war."""
        clan_a = cd.create_clan(101, "Dragon Riders", "DRGN")
        clan_b = cd.create_clan(201, "Shadow Wolves", "SHDW")
        cd.update_treasury(clan_a["id"], 10_000)
        cd.update_treasury(clan_b["id"], 10_000)

        ctx = self._create_mock_ctx(101)
        await self.war_cog.do_war_declare(ctx, "SHDW", 5000)

        ctx.send.assert_awaited_once()
        sent_embed = ctx.send.call_args[1].get("embed")
        self.assertIsNotNone(sent_embed)
        self.assertIn("Clan War Declared", sent_embed.title)

        # Check war record exists and is pending
        war = war_for_clan(clan_a["id"])
        self.assertIsNotNone(war)
        self.assertEqual(war["state"], "pending")
        self.assertEqual(war["a"], clan_a["id"])
        self.assertEqual(war["b"], clan_b["id"])
        self.assertEqual(war["stake"], 5000)

    async def test_war_declare_flexible_args(self):
        """war_declare handles multi-word names and flexible stake order."""
        clan_a = cd.create_clan(101, "Dragon Riders", "DRGN")
        clan_b = cd.create_clan(201, "Shadow Wolves", "SHDW")
        cd.update_treasury(clan_a["id"], 10_000)
        cd.update_treasury(clan_b["id"], 10_000)

        # Argument style 1: Target with spaces, stake at the end
        ctx1 = self._create_mock_ctx(101)
        await self.war_cog.do_war_declare(ctx1, "Shadow", "Wolves", "5,000")
        ctx1.send.assert_awaited_once()
        self.assertIn("Clan War Declared", ctx1.send.call_args[1]["embed"].title)

        # Reset wars
        from cogs.clans.clan_war import _save, _blank
        _save(_blank())

        # Argument style 2: Stake first, target after
        ctx2 = self._create_mock_ctx(101)
        await self.war_cog.do_war_declare(ctx2, "5000", "Shadow", "Wolves")
        ctx2.send.assert_awaited_once()
        self.assertIn("Clan War Declared", ctx2.send.call_args[1]["embed"].title)

    async def test_war_declare_member_mention(self):
        """war_declare handles target provided as defender owner mention."""
        clan_a = cd.create_clan(101, "Dragon Riders", "DRGN")
        clan_b = cd.create_clan(201, "Shadow Wolves", "SHDW")
        cd.update_treasury(clan_a["id"], 10_000)
        cd.update_treasury(clan_b["id"], 10_000)

        ctx = self._create_mock_ctx(101)
        await self.war_cog.do_war_declare(ctx, "<@201>", "5000")
        ctx.send.assert_awaited_once()
        self.assertIn("Clan War Declared", ctx.send.call_args[1]["embed"].title)

    async def test_war_declare_failures(self):
        """war_declare enforces owner, treasury, minimum stake, and existing wars."""
        clan_a = cd.create_clan(101, "Dragon Riders", "DRGN")
        clan_b = cd.create_clan(201, "Shadow Wolves", "SHDW")
        cd.add_member(clan_a["id"], 102)

        # Non-owner fails
        ctx = self._create_mock_ctx(102)
        await self.war_cog.do_war_declare(ctx, "SHDW", 5000)
        ctx.send.assert_awaited_with("❌ Only the clan owner can declare war.")

        # Stake below minimum
        ctx_owner = self._create_mock_ctx(101)
        await self.war_cog.do_war_declare(ctx_owner, "SHDW", 100)
        self.assertIn("Minimum stake", ctx_owner.send.call_args[0][0])

        # Self war
        await self.war_cog.do_war_declare(ctx_owner, "DRGN", 5000)
        self.assertIn("can't war your own clan", ctx_owner.send.call_args[0][0])

        # Insufficient treasury for caller
        await self.war_cog.do_war_declare(ctx_owner, "SHDW", 5000)
        self.assertIn("Your treasury only has", ctx_owner.send.call_args[0][0])

        # Caller has coins, but defender doesn't
        cd.update_treasury(clan_a["id"], 10_000)
        await self.war_cog.do_war_declare(ctx_owner, "SHDW", 5000)
        self.assertIn("can't match that stake", ctx_owner.send.call_args[0][0])

    async def test_war_accept_and_escrow(self):
        """war_accept validates defender ownership, escrows stakes, and activates war."""
        clan_a = cd.create_clan(101, "Dragon Riders", "DRGN")
        clan_b = cd.create_clan(201, "Shadow Wolves", "SHDW")
        cd.update_treasury(clan_a["id"], 10_000)
        cd.update_treasury(clan_b["id"], 10_000)

        # Declare war
        ctx_a = self._create_mock_ctx(101)
        await self.war_cog.do_war_declare(ctx_a, "SHDW", 5000)

        # Non-owner defender fails
        cd.add_member(clan_b["id"], 202)
        ctx_b_member = self._create_mock_ctx(202)
        await self.war_cog.do_war_accept(ctx_b_member)
        ctx_b_member.send.assert_awaited_with("❌ Only the clan owner can accept a war.")

        # Owner accepts
        ctx_b = self._create_mock_ctx(201)
        await self.war_cog.do_war_accept(ctx_b)
        ctx_b.send.assert_awaited_once()
        embed = ctx_b.send.call_args[1]["embed"]
        self.assertIn("WAR IS ON", embed.title)

        # Verify state is active
        war = war_for_clan(clan_a["id"])
        self.assertEqual(war["state"], "active")
        self.assertAlmostEqual(war["ends_at"], time.time() + WAR_DURATION, delta=5)

        # Verify both stakes were escrowed from treasuries
        self.assertEqual(cd.get_clan(clan_a["id"])["treasury"], 5000)
        self.assertEqual(cd.get_clan(clan_b["id"])["treasury"], 5000)

    async def test_war_decline_and_cancel(self):
        """war_decline allows defender to decline or challenger to cancel."""
        clan_a = cd.create_clan(101, "Dragon Riders", "DRGN")
        clan_b = cd.create_clan(201, "Shadow Wolves", "SHDW")
        cd.update_treasury(clan_a["id"], 10_000)
        cd.update_treasury(clan_b["id"], 10_000)

        # Declare
        ctx_a = self._create_mock_ctx(101)
        await self.war_cog.do_war_declare(ctx_a, "SHDW", 5000)

        # Challenger cancels
        await self.war_cog.do_war_decline(ctx_a)
        self.assertIn("Cancelled your war declaration", ctx_a.send.call_args[0][0])
        self.assertIsNone(war_for_clan(clan_a["id"]))

        # Declare again
        await self.war_cog.do_war_declare(ctx_a, "SHDW", 5000)
        self.assertIsNotNone(war_for_clan(clan_a["id"]))

        # Defender declines
        ctx_b = self._create_mock_ctx(201)
        await self.war_cog.do_war_decline(ctx_b)
        self.assertIn("Declined the war", ctx_b.send.call_args[0][0])
        self.assertIsNone(war_for_clan(clan_b["id"]))

    async def test_battle_scoring_and_friendly_fire_guard(self):
        """Battles score for the winner's clan, but friendly battles between clan members do not."""
        clan_a = cd.create_clan(101, "Dragon Riders", "DRGN")
        clan_b = cd.create_clan(201, "Shadow Wolves", "SHDW")
        cd.add_member(clan_a["id"], 102)
        cd.update_treasury(clan_a["id"], 10_000)
        cd.update_treasury(clan_b["id"], 10_000)

        # Declare and accept
        ctx_a = self._create_mock_ctx(101)
        await self.war_cog.do_war_declare(ctx_a, "SHDW", 5000)
        ctx_b = self._create_mock_ctx(201)
        await self.war_cog.do_war_accept(ctx_b)

        # Battle: Clan A member (101) vs Clan B member (201). Winner 101.
        await self.war_cog.on_beycord_battle_blades({
            "winner_id": 101,
            "participants": [101, 201],
        })

        war = war_for_clan(clan_a["id"])
        self.assertEqual(war["score_a"], 1)
        self.assertEqual(war["score_b"], 0)
        self.assertEqual(war["scorers"]["101"], 1)

        # Friendly battle: Clan A member (101) vs Clan A member (102). Winner 101.
        # Must NOT score!
        await self.war_cog.on_beycord_battle_blades({
            "winner_id": 101,
            "participants": [101, 102],
        })

        war = war_for_clan(clan_a["id"])
        self.assertEqual(war["score_a"], 1)  # unchanged!

    async def test_settle_due_win_and_payout(self):
        """Settling ended war awards pot to winner, updates wins/losses, and saves history."""
        clan_a = cd.create_clan(101, "Dragon Riders", "DRGN")
        clan_b = cd.create_clan(201, "Shadow Wolves", "SHDW")
        cd.update_treasury(clan_a["id"], 10_000)
        cd.update_treasury(clan_b["id"], 10_000)

        # Declare and accept
        ctx_a = self._create_mock_ctx(101)
        await self.war_cog.do_war_declare(ctx_a, "SHDW", 5000)
        ctx_b = self._create_mock_ctx(201)
        await self.war_cog.do_war_accept(ctx_b)

        # 101 scores a point
        await self.war_cog.on_beycord_battle_blades({
            "winner_id": 101,
            "participants": [101, 201],
        })

        # Fast forward war ends_at to past
        from cogs.clans.clan_war import _load, _save
        data = _load()
        for w in data["wars"].values():
            w["ends_at"] = time.time() - 10
        _save(data)

        # Settle
        await self.war_cog._settle_due()

        # War is no longer active
        self.assertIsNone(war_for_clan(clan_a["id"]))

        # Winner received pot (10,000) + remaining treasury (5,000) = 15,000
        self.assertEqual(cd.get_clan(clan_a["id"])["treasury"], 15_000)
        # Loser kept remaining treasury (5,000)
        self.assertEqual(cd.get_clan(clan_b["id"])["treasury"], 5_000)

        # Check clan wins/losses updated
        self.assertEqual(cd.get_clan(clan_a["id"])["wins"], 1)
        self.assertEqual(cd.get_clan(clan_b["id"])["losses"], 1)

        # Check history
        data_after = _load()
        self.assertEqual(len(data_after["history"]), 1)
        self.assertEqual(data_after["history"][0]["winner"], clan_a["id"])

    async def test_clan_cog_war_delegation(self):
        """Testing `;clan war declare` and `;clan war status` via ClanCog group."""
        clan_a = cd.create_clan(101, "Dragon Riders", "DRGN")
        clan_b = cd.create_clan(201, "Shadow Wolves", "SHDW")
        cd.update_treasury(clan_a["id"], 10_000)
        cd.update_treasury(clan_b["id"], 10_000)

        ctx = self._create_mock_ctx(101)
        # Call clan_war_declare on ClanCog
        await self.clan_cog.clan_war_declare.callback(self.clan_cog, ctx, "SHDW", "5000")
        ctx.send.assert_awaited_once()
        self.assertIn("Clan War Declared", ctx.send.call_args[1]["embed"].title)

        # Call clan_war_status
        ctx_status = self._create_mock_ctx(101)
        await self.clan_cog.clan_war_status.callback(self.clan_cog, ctx_status)
        self.assertIn("Pending", ctx_status.send.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
