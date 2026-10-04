#!/usr/bin/env python3
"""Offline transaction, UI and renderer regressions. Run from any directory."""
import asyncio
import copy
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.profile_cosmetics import (
    ProfileCosmeticError, apply_profile_purchase, apply_profile_equip,
    equipped_theme, owned_themes,
)


class CosmeticTests(unittest.TestCase):
    def test_purchase_and_free_switching(self):
        p = {"coins": 100_000, "inventory": ["Valkyrie"], "stats": {"attack": 123}}
        other = copy.deepcopy(p)
        r = apply_profile_purchase(p, "cyber_arena")
        self.assertEqual((r["spent"], p["coins"]), (90_000, 10_000))
        self.assertEqual(owned_themes(p), ["default", "cyber_arena"])
        self.assertEqual(equipped_theme(p), "cyber_arena")
        for key in ["default", "cyber_arena"] * 5:
            apply_profile_equip(p, key)
            self.assertEqual(equipped_theme(p), key)
            self.assertEqual(p["coins"], 10_000)
        self.assertEqual(p["inventory"], other["inventory"])
        self.assertEqual(p["stats"], other["stats"])
        before = copy.deepcopy(p)
        with self.assertRaises(ProfileCosmeticError):
            apply_profile_purchase(p, "cyber_arena")
        self.assertEqual(p, before)

    def test_rejections_do_not_change_document(self):
        for coins, key in [(89_999, "cyber_arena"), (100_000, "invalid"),
                           ("broken", "cyber_arena"), ([], "cyber_arena"),
                           (90_000.5, "cyber_arena")]:
            p = {"coins": coins, "inventory": ["preserve"]}
            before = copy.deepcopy(p)
            with self.assertRaises(ProfileCosmeticError):
                apply_profile_purchase(p, key)
            self.assertEqual(p, before)
        for key in ["cyber_arena", "invalid", None, [], {}]:
            p = {"coins": 100_000}
            with self.assertRaises(ProfileCosmeticError):
                apply_profile_equip(p, key)
            self.assertEqual(p, {"coins": 100_000})

    def test_legacy_and_corrupt_fields(self):
        for raw in [None, 1, True, "cyber_arena", {"cyber_arena": True}, [],
                    [None, {}, 12, "invalid"]]:
            p = {"coins": 100_000, "owned_profile_themes": raw,
                 "equipped_profile_theme": {"bad": "value"}}
            self.assertEqual(owned_themes(p), ["default"])
            self.assertEqual(equipped_theme(p), "default")
            apply_profile_purchase(p, "cyber_arena")
            self.assertEqual(p["coins"], 10_000)
            self.assertEqual(equipped_theme(p), "cyber_arena")
        self.assertEqual(equipped_theme({}), "default")
        p = {"coins": 100_000, "owned_profile_themes": [" CYBER_ARENA ", {}, "cyber_arena"]}
        self.assertEqual(owned_themes(p), ["default", "cyber_arena"])
        with self.assertRaises(ProfileCosmeticError):
            apply_profile_purchase(p, "cyber_arena")
        self.assertEqual(p["coins"], 100_000)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_command_sends_discord_jpeg_and_switch_updates_attachment(self):
        from PIL import Image
        from cogs.economy.profile import ProfileCog, ProfileThemePicker
        from utils import profile_card as pc
        from utils import image_generator as ig
        avatar_bytes = io.BytesIO()
        Image.new("RGB", (512, 512), (250, 10, 20)).save(avatar_bytes, "PNG")
        art_path = os.path.join(self.tmp.name, "test-blade.png")
        # A narrow real PNG exercises trim/resize/centering, rather than a
        # mocked square art function that hides geometry problems.
        Image.new("RGBA", (30, 180), (0, 240, 40, 255)).save(art_path)
        await self.db.mutate_user(1, lambda p: apply_profile_purchase(p, "cyber_arena"))
        blade = {"name":"Test Blade", "type":"Attack", "rarity":"Epic", "level":2,
                 "stats":{"hp":100,"attack":120,"defense":80,"stamina":90}}
        owner = SimpleNamespace(id=1, display_name="Test Player", name="tester",
            display_avatar=SimpleNamespace(replace=lambda **kw: SimpleNamespace(url="https://cdn.discordapp.com/avatars/1/test.png")))
        @asynccontextmanager
        async def typing():
            yield
        ctx = SimpleNamespace(author=owner, typing=typing, send=AsyncMock())
        cog = ProfileCog(None)
        pc.clear_cache()
        with patch("urllib.request.urlopen", side_effect=lambda *a, **k: io.BytesIO(avatar_bytes.getvalue())), \
             patch.object(ig, "_art_index", {"test blade":art_path}), \
             patch.dict(ig._art_cache, {}, clear=True), \
             patch.dict(ig._missing_art_cache, {}, clear=True), \
             patch("cogs.battle.boss.boss_copy.equipped_blade", AsyncMock(return_value=(blade, None))), \
             patch("utils.loadout.effective_blade", AsyncMock(return_value=(blade, {}, None))), \
             patch("cogs.casino.casino_premium.get_premium", AsyncMock(return_value=None)), \
             patch("cogs.clans.clan_data.clan_of", return_value=None):
            await ProfileCog.profile.callback(cog, ctx)
            ctx.send.assert_awaited_once()
            sent = ctx.send.call_args.kwargs
            self.assertEqual(sent["file"].filename, "profile.jpg")
            img = Image.open(sent["file"].fp)
            self.assertEqual(img.format, "JPEG")
            self.assertEqual(img.size, pc.CYBER_SIZE)
            self.assertGreater(img.getpixel(pc.CYBER_AVATAR_C)[0], 230)
            self.assertGreater(img.getpixel(pc.CYBER_ART_C)[1], 200)
            self.assertEqual(sent["view"].children[0].label, "Switch Profile")
            sent["file"].close()
            message = SimpleNamespace(edit=AsyncMock())
            for theme, size in [("default", (pc.W,pc.H)), ("cyber_arena", pc.CYBER_SIZE)]:
                picker = await ProfileThemePicker(cog, owner, message).build()
                await picker._selected(self.interaction(values=[theme]))
                attachment = message.edit.call_args.kwargs["attachments"][0]
                self.assertEqual(Image.open(attachment.fp).size, size)
                self.assertEqual(self.store.get_one("1")["coins"], 10_000)
                attachment.close()
                picker.stop()
            # Viewing someone else's card never exposes their switch control.
            ctx.author = SimpleNamespace(id=2)
            await ProfileCog.profile.callback(cog, ctx, owner)
            self.assertIsNone(ctx.send.call_args.kwargs["view"])
            ctx.send.call_args.kwargs["file"].close()
        pc.clear_cache()

    async def asyncSetUp(self):
        from utils import database as db
        from utils.userstore import UserStore
        self.db = db
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UserStore(os.path.join(self.tmp.name, "users.db"),
                               os.path.join(self.tmp.name, "absent.json"))
        self.patcher = patch.object(db, "USER_STORE", self.store)
        self.patcher.start()
        self.store.put_one("1", {"coins": 100_000, "inventory": ["keep"]})

    async def asyncTearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def interaction(self, user=1, values=None):
        return SimpleNamespace(user=SimpleNamespace(id=user),
            data={"values": values} if values is not None else {},
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
            message=SimpleNamespace(edit=AsyncMock()))

    async def test_concurrent_purchase_and_persistence(self):
        results = await asyncio.gather(*[
            self.db.mutate_user(1, lambda p: apply_profile_purchase(p, "cyber_arena"))
            for _ in range(20)], return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, ProfileCosmeticError) for r in results), 19)
        p = self.store.get_one("1")
        self.assertEqual(p["coins"], 10_000)
        self.assertEqual(p["inventory"], ["keep"])
        self.assertEqual(p["owned_profile_themes"].count("cyber_arena"), 1)
        self.assertEqual(equipped_theme(p), "cyber_arena")

    async def test_storage_failure_does_not_persist_charge_or_grant(self):
        before = self.store.get_one("1")
        with patch.object(self.store, "put_one", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                await self.db.mutate_user(1, lambda p: apply_profile_purchase(p, "cyber_arena"))
        self.assertEqual(self.store.get_one("1"), before)

    async def test_bey_and_part_purchases_preserve_profile_ownership(self):
        from cogs.economy.shop import apply_bey_purchase, apply_part_purchase, PARTS_CATALOG
        self.store.put_one("1", {"coins": 1_000_000, "inventory": []})
        await self.db.mutate_user(1, lambda p: apply_profile_purchase(p, "cyber_arena"))
        before = self.store.get_one("1")
        bey = await self.db.mutate_user(1, lambda p: apply_bey_purchase(p, "Epsilon"))
        part = await self.db.mutate_user(1, lambda p: apply_part_purchase(p, PARTS_CATALOG[0]["name"]))
        after = self.store.get_one("1")
        self.assertIn(bey["bey"], after["inventory"])
        self.assertEqual(after["coins"], before["coins"] - bey["spent"] - part["spent"])
        self.assertEqual(after["owned_profile_themes"], before["owned_profile_themes"])
        self.assertEqual(equipped_theme(after), "cyber_arena")

    async def test_shop_sections_and_purchase_callback(self):
        from cogs.ui import main_shop as shop
        for section in [shop.SECTION_HOME, shop.SECTION_BEYS, shop.SECTION_PARTS,
                        shop.SECTION_AVATAR, shop.SECTION_BOOSTER,
                        shop.SECTION_PREMIUM, shop.SECTION_PROFILE]:
            view = shop.MainShopView(1, section)
            self.assertIsNotNone(view.current_embed())
            # Serialization catches Discord's per-row limits too.
            self.assertTrue(view.to_components())
            view.stop()
        view = shop.MainShopView(1, shop.SECTION_PROFILE)
        denied = self.interaction(user=2)
        await view._buy_profile(denied)
        self.assertEqual(self.store.get_one("1")["coins"], 100_000)
        denied.response.defer.assert_not_awaited()
        i = self.interaction()
        await view._buy_profile(i)
        i.response.defer.assert_awaited_once()
        self.assertEqual(self.store.get_one("1")["coins"], 10_000)
        await view._buy_profile(self.interaction())
        self.assertEqual(self.store.get_one("1")["coins"], 10_000)
        view.stop()

    async def test_selector_authorization_and_message_refresh(self):
        from cogs.economy.profile import ProfileThemePicker, ProfileCardView
        owner = SimpleNamespace(id=1)
        message = SimpleNamespace(edit=AsyncMock())
        cog = SimpleNamespace(_profile_card_file=AsyncMock(return_value="card"))
        picker = await ProfileThemePicker(cog, owner, message).build()
        self.assertEqual([o.value for o in picker.children[0].options], ["default"])
        await picker._selected(self.interaction(values=["cyber_arena"]))
        self.assertEqual(self.store.get_one("1")["coins"], 100_000)
        await self.db.mutate_user(1, lambda p: apply_profile_purchase(p, "cyber_arena"))
        await picker.build()
        self.assertEqual([o.value for o in picker.children[0].options], ["default", "cyber_arena"])
        denied = self.interaction(user=2, values=["default"])
        await picker._selected(denied)
        self.assertEqual(equipped_theme(self.store.get_one("1")), "cyber_arena")
        denied.response.defer.assert_not_awaited()
        for values in [[], ["default", "cyber_arena"], [None]]:
            await picker._selected(self.interaction(values=values))
        with patch("cogs.battle.boss.boss_copy.equipped_blade", AsyncMock(return_value=(None, None))):
            for key in ["default", "cyber_arena"]:
                await picker._selected(self.interaction(values=[key]))
                self.assertEqual(equipped_theme(self.store.get_one("1")), key)
                self.assertEqual(self.store.get_one("1")["coins"], 10_000)
            self.assertEqual(message.edit.await_count, 2)
            self.assertEqual(message.edit.call_args.kwargs["attachments"], ["card"])
            cog._profile_card_file.return_value = None
            i = self.interaction(values=["default"])
            await picker._selected(i)
            self.assertIn("could not refresh", i.followup.send.call_args.args[0])
        card_view = ProfileCardView(cog, owner)
        self.assertFalse(await card_view.interaction_check(self.interaction(user=2)))
        card_view.stop()
        picker.stop()

    async def test_cog_passes_real_discord_avatar_and_current_theme(self):
        from cogs.economy.profile import ProfileCog
        avatar = SimpleNamespace(replace=lambda **kw: SimpleNamespace(url="https://cdn.discordapp.com/avatars/1/real.png"))
        target = SimpleNamespace(id=1, display_name="Darko", display_avatar=avatar)
        with patch("cogs.economy.profile.render_profile_card", return_value=io.BytesIO(b"test")) as render, \
             patch("cogs.casino.casino_premium.get_premium", AsyncMock(return_value={"expires": 200_000})), \
             patch("cogs.clans.clan_data.clan_of", return_value={"name": "Night Spin"}), \
             patch("cogs.economy.profile.time.time", return_value=100_000):
            p = {"owned_profile_themes": ["cyber_arena"], "equipped_profile_theme": "cyber_arena", "equipped_title": "Champion"}
            file = await ProfileCog(None)._profile_card_file(target, p, None)
            self.assertIsNotNone(file)
            self.assertEqual(render.call_args.kwargs["avatar_url"], "https://cdn.discordapp.com/avatars/1/real.png")
            self.assertEqual(render.call_args.kwargs["theme"], "cyber_arena")
            metadata = render.call_args.args[0]
            self.assertEqual(metadata["premium_days"], 2)
            self.assertEqual(metadata["guild_name"], "Night Spin")
            self.assertEqual(metadata["title"], "Champion")
            file.close()


class RendererTests(unittest.TestCase):
    def test_both_themes_preserve_dynamic_layers(self):
        from PIL import Image
        from utils import profile_card as pc
        p = {"xp": 12_345, "rank_score": 250, "wins": 27, "losses": 13,
             "win_streak": 3, "best_streak": 8, "coins": 10_000, "inventory": ["Valkyrie"]}
        blade = {"name": "Valkyrie", "type": "Attack", "rarity": "Epic", "level": 42,
                 "stats": {"attack": 170, "defense": 110, "stamina": 125, "hp": 190}}
        before = copy.deepcopy((p, blade))
        with patch.object(pc, "_avatar_image", return_value=Image.new("RGBA", (134,134), "red")), \
             patch.object(pc, "_blade_art", return_value=Image.new("RGBA", (208,208), "green")):
            images = []
            for theme in ["default", "cyber_arena"]:
                buf = pc.render_profile_card({"name": "Darko"}, p, blade, theme=theme,
                                             total_beys=127, avatar_url="discord-avatar")
                self.assertIsNotNone(buf)
                img = Image.open(buf)
                self.assertEqual(img.size, pc.CYBER_SIZE if theme == "cyber_arena" else (pc.W, pc.H))
                self.assertGreater(img.getpixel(pc.CYBER_AVATAR_C if theme == "cyber_arena" else pc.AVATAR_C)[0], 240)
                self.assertGreater(img.getpixel(pc.CYBER_ART_C if theme == "cyber_arena" else pc.ART_C)[1], 110)
                images.append(img.copy())
            self.assertNotEqual(images[0].tobytes(), images[1].tobytes())
            self.assertEqual((p, blade), before)
            for raw in [None, 42, {}, "cyber_arena", [None, {}]]:
                malformed = dict(p, owned_profile_themes=raw, equipped_profile_theme=[])
                self.assertIsNotNone(pc.render_profile_card({"name":"Legacy"}, malformed,
                    blade, theme=equipped_theme(malformed)))
        frame = pc._frame("cyber_arena")
        frame.putpixel((0,0), (255,0,0,255))
        self.assertNotEqual(pc._frame("cyber_arena").getpixel((0,0)), (255,0,0,255))

    def test_reference_frame_blanks_optional_fields_and_masks_example_data(self):
        from PIL import Image
        from utils import profile_card as pc
        p = {"xp": 0, "rank_score": 0, "wins": 0, "losses": 0, "coins": 0}
        blank = pc._frame("cyber_arena")
        pc._cyber_content(blank, {"name":"New Player","id":1}, p, None, 127, None, None)
        # The reference's PREMIUM dash, TITLE and GUILD NIGHT SPIN must all
        # disappear when the player has no values; no invented membership.
        for box in [(521,764,731,815),(778,764,1045,815),(1088,764,1332,815)]:
            self.assertEqual(len(set(blank.crop(box).getdata())), 1)
        full = pc._frame("cyber_arena")
        pc._cyber_content(full, {"name":"X"*400,"id":1,"premium_days":3,
            "title":"Arena Champion", "guild_name":"Night Spin"}, p, None, 127, None, None)
        for box in [(521,764,731,815),(778,764,1045,815),(1088,764,1332,815)]:
            self.assertGreater(len(set(full.crop(box).getdata())), 1)
        pc.clear_cache()
        with patch.object(pc, "CYBER_FRAME_PATH", "/missing/cyber.png"):
            fallback = pc.render_profile_card({"name":"Player"}, p, theme="cyber_arena")
            self.assertIsNotNone(fallback)
            self.assertEqual(Image.open(fallback).size, (pc.W, pc.H))
        pc.clear_cache()


if __name__ == "__main__":
    unittest.main(verbosity=2)
