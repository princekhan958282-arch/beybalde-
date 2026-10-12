"""Live uploads must not leave battle callers using old leveling code."""
import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from utils import bey_levels as BL
from utils.loadout import bey_level_and_stats


class LevelModuleReloadTests(unittest.TestCase):
    def blade(self):
        return {"id": "CUSTOM", "name": "Reload Test", "type": "Attack",
                "stats": {"hp": 335, "attack": 20, "defense": 20, "stamina": 20}}

    def test_stale_module_recovers_for_new_and_levelled_players(self):
        for level in (1, 100):
            blade = self.blade()
            profile = {"bey_progress": {blade["name"]: {
                "xp": BL.xp_for_level(level), "ivs": {"hp": 10}}}}
            before = copy.deepcopy((blade, profile))
            expected = bey_level_and_stats(profile, blade)
            old_module = BL
            del BL.level_base_stats
            try:
                self.assertEqual(bey_level_and_stats(profile, blade), expected)
                self.assertIs(BL, old_module)
                self.assertEqual((blade, profile), before)
            finally:
                import importlib
                importlib.reload(BL)

    def test_admin_reload_refreshes_levels_before_cogs(self):
        from cogs.admin.actions import do_reload
        def check_level_module(cog):
            self.assertTrue(callable(BL.level_base_stats))
            self.assertEqual(BL.GROWTH["attack"]["hp"], 0.25)
        bot = SimpleNamespace(extensions={"cogs.battle": object()},
                              reload_extension=AsyncMock(side_effect=check_level_module))
        del BL.level_base_stats
        BL.GROWTH["attack"]["hp"] = 99
        try:
            result = asyncio.run(do_reload(bot))
            self.assertTrue(result.ok)
            bot.reload_extension.assert_awaited_once_with("cogs.battle")
        finally:
            import importlib
            importlib.reload(BL)

    def test_failed_utility_reload_stops_cog_reload(self):
        from cogs.admin.actions import do_reload
        bot = SimpleNamespace(extensions={"cogs.battle": object()},
                              reload_extension=AsyncMock())
        with patch("importlib.reload", side_effect=SyntaxError("bad upload")):
            result = asyncio.run(do_reload(bot))
        self.assertFalse(result.ok)
        bot.reload_extension.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
