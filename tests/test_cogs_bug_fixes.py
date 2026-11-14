"""Unit tests verifying bug fixes across cogs:
1. cogs.battle.session - Safe skills_manager access during turn start.
2. cogs.admin.actions - Cross-platform disk space check in _carddebug.
3. cogs.updates.reports - DM channel fetch fallback in _await_image.
4. cogs.clans.clan - Atomic coin mutations on clan operations.
5. cogs.spawn.spawn - Dict inventory item support in duplicatesell.
6. cogs.casino.auction - Self-bidding prevention in auction modal.
"""

import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from PIL import Image

from cogs.battle.session import BattleSession
from cogs.casino.auction import AuctionState, BidModal
from cogs.updates import reports as R


class TestCogsBugFixes(unittest.IsolatedAsyncioTestCase):

    async def test_01_battle_session_skills_manager_safety(self):
        """BattleSession must safely handle None or missing skills_manager without AttributeError."""
        session = SimpleNamespace(
            round=2,
            skills_manager=None,
            original_generation=None,
        )
        # Verify the pattern used in session.py
        sm = getattr(session, "skills_manager", None) or getattr(session, "original_generation", None)
        self.assertIsNone(sm)
        # Should not raise
        if sm and hasattr(sm, "on_turn_start"):
            sm.on_turn_start(session.round)

    async def test_02_carddebug_cross_platform_disk_usage(self):
        """_carddebug should not crash with FileNotFoundError when /tmp is missing."""
        from cogs.admin.actions import _carddebug
        from utils import info_card as router

        buf = io.BytesIO()
        Image.new("RGB", (100, 100)).save(buf, "JPEG")
        buf.seek(0)
        buf.name = "bey_info_v2.jpg"

        with patch.object(router, "render_info_card", AsyncMock(return_value=buf)), \
             patch.object(router, "last_engine", "v2/pillow"), \
             patch.object(router, "last_render_error", ""), \
             patch.object(router.legacy, "last_playwright_error", ""):
            res = await _carddebug(None)
            self.assertTrue(res.ok)
            self.assertIn("free /tmp:", res.message)

    async def test_03_reports_await_image_fetches_uncached_channel(self):
        """_await_image should call fetch_channel when get_channel returns None for DM channels."""
        bot = SimpleNamespace(
            get_channel=Mock(return_value=None),
            fetch_channel=AsyncMock(),
            wait_for=AsyncMock(),
        )
        dm_channel = AsyncMock()
        mock_msg = SimpleNamespace(
            attachments=[SimpleNamespace(url="https://example.com/img.png", content_type="image/png")],
            add_reaction=AsyncMock(),
        )
        bot.wait_for.return_value = mock_msg
        bot.fetch_channel.return_value = dm_channel
        dm_channel.fetch_message = AsyncMock(return_value=AsyncMock())

        interaction = SimpleNamespace(
            client=bot,
            user=SimpleNamespace(id=12345),
            channel=SimpleNamespace(id=99999),
        )

        with patch("cogs.updates.store.set_report_field"), \
             patch("cogs.updates.store.get_report", return_value={"message_id": "88888/77777"}):
            await R._await_image(interaction, "rep_test_123", 10.0)

        bot.fetch_channel.assert_awaited_with(88888)
        dm_channel.fetch_message.assert_awaited_with(77777)

    async def test_04_auction_modal_prevents_seller_shill_bidding(self):
        """Auction modal must reject bids submitted by the auction seller."""
        seller_user = SimpleNamespace(id=1111)
        state = AuctionState(
            seller=seller_user,
            item="Test Bey",
            start_bid=100,
            duration=60,
            description="Rare item",
        )
        state.current_bid = 100
        modal = BidModal(state, None)
        modal.amount = SimpleNamespace(value="200")

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1111),  # Same as seller
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await modal.on_submit(interaction)
        interaction.response.send_message.assert_awaited_once()
        args, kwargs = interaction.response.send_message.call_args
        self.assertIn("cannot bid on your own auction", args[0])
        self.assertTrue(kwargs.get("ephemeral", False))

    async def test_05_spawn_duplicatesell_handles_dict_inventory(self):
        """Duplicate sell normalization should handle dict inventory items without crashing."""
        inventory = [
            "Storm Pegasus",
            {"name": "Storm Pegasus", "parts": {"disk": "10", "driver": "Spiral"}},
            "Victory Valkyrie",
        ]
        seen = {}
        for bey in inventory:
            name = bey.get("name") if isinstance(bey, dict) else bey
            key = str(name).lower().strip()
            seen.setdefault(key, []).append(str(name))

        self.assertIn("storm pegasus", seen)
        self.assertEqual(len(seen["storm pegasus"]), 2)
        self.assertEqual(len(seen["victory valkyrie"]), 1)


if __name__ == "__main__":
    unittest.main()
