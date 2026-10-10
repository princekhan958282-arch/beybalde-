"""Roster reload and zip-install rename regressions; no Discord connection."""
import asyncio
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from utils.character_registry import ROOT, CharacterRegistry
from utils import updater


class RosterRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for folder in updater.CATALOG_FOLDERS:
            shutil.copytree(ROOT / folder, self.root / folder)
        self.registry = CharacterRegistry(self.root)

    def archive(self, *, incomplete=False, invalid=False):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("repo/app.py", "# new entry point\n")
            archive.writestr("repo/data/users.json", "{}")
            for folder in updater.CATALOG_FOLDERS[:1] if incomplete else updater.CATALOG_FOLDERS:
                for path in (ROOT / folder).glob("*.json"):
                    content = path.read_bytes()
                    if invalid and path.name == "blast_jinnius.json":
                        content = b"{}"
                    archive.writestr("repo/" + path.relative_to(ROOT).as_posix(), content)
        return zipfile.ZipFile(buffer)

    def apply(self, archive):
        with patch.object(updater, "_ROOT", str(self.root)), patch.object(
                updater, "_BACKUP_DIR", str(self.root / ".update_backup")):
            return updater._apply(archive, "test-sha")

    def test_refresh_loads_new_bey_and_its_new_parts(self):
        bey = self.root / "beys/blast_jinnius.json"
        entry = json.loads(bey.read_text())
        # Locate bundled files by ID rather than assuming their filenames.
        paths = [bey] + [p for folder in ("parts/disks", "parts/drivers")
                        for p in (self.root / folder).glob("*.json")
                        if json.loads(p.read_text())["id"] in entry["default_parts"].values()]
        saved = {path: path.read_bytes() for path in paths}
        for path in paths:
            path.unlink()
        self.assertEqual(len(self.registry.load("bey")), 164)
        for path, content in saved.items():
            path.write_bytes(content)
        self.assertIsNone(self.registry.find_bey("Blast Jinnius"))
        self.assertEqual(self.registry.refresh()["bey"], 165)
        self.assertEqual(self.registry.find_bey("Blast Jinnius")["id"], "BB164")
        self.assertEqual(self.registry.find_bey("Jambo Jamunter")["name"], "Jail Jormungand")
        from cogs.economy.profile import BeyListView, fuzzy_find_beyblade
        async def check_commands():
            with patch("utils.character_registry.REGISTRY", self.registry):
                view = BeyListView(SimpleNamespace(id=1), self.registry.load("bey"))
                self.assertIn("Blast Jinnius", view.names())
                self.assertIn("Jail Jormungand", view.names())
                self.assertEqual(fuzzy_find_beyblade("Blast Jinnius")["id"], "BB164")
        asyncio.run(check_commands())

    def test_invalid_refresh_keeps_entire_previous_snapshot(self):
        self.registry.refresh()
        old_cache, old_indexes = self.registry._cache, self.registry._indexes
        (self.root / "avatars/broken.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.registry.refresh()
        self.assertIs(self.registry._cache, old_cache)
        self.assertIs(self.registry._indexes, old_indexes)
        self.assertIsNotNone(self.registry.find_bey("Blast Jinnius"))

    def test_reload_refreshes_before_extensions_and_reports_count(self):
        from cogs.admin.actions import do_reload
        self.registry.load("bey")
        bot = SimpleNamespace(extensions={"test.cog": object()}, reload_extension=AsyncMock())
        with patch("utils.character_registry.REGISTRY", self.registry):
            result = asyncio.run(do_reload(bot))
        self.assertTrue(result.ok)
        self.assertIn("165 Beys", result.embed.description)
        bot.reload_extension.assert_awaited_once_with("test.cog")

    def test_reload_failure_preserves_cogs(self):
        from cogs.admin.actions import do_reload
        self.registry.refresh()
        (self.root / "beys/broken.json").write_text("{}")
        bot = SimpleNamespace(extensions={"test.cog": object()}, reload_extension=AsyncMock())
        with patch("utils.character_registry.REGISTRY", self.registry):
            result = asyncio.run(do_reload(bot))
        self.assertFalse(result.ok)
        bot.reload_extension.assert_not_awaited()

    def test_update_removes_renamed_duplicate_with_backup_and_preserves_players(self):
        old = self.root / "beys/jambo_jamunter.json"
        old.write_bytes((self.root / "beys/jail_jormungand.json").read_bytes())
        (self.root / "beys/blast_jinnius.json").unlink()
        (self.root / "data").mkdir()
        player = self.root / "data/users.json"
        player.write_text('{"player": "keep me"}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.registry.load("bey")
        with self.archive() as archive:
            self.apply(archive)
        self.assertFalse(old.exists())
        backups = list((self.root / ".update_backup").glob("*/beys/jambo_jamunter.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text())["id"], "BB155")
        self.assertEqual(player.read_text(), '{"player": "keep me"}')
        self.assertEqual(self.registry.refresh()["bey"], 165)

    def test_bad_archives_change_nothing(self):
        app = self.root / "app.py"
        app.write_text("# working entry point\n")
        old = self.root / "beys/retired.json"
        old.write_text("keep until a validated update")
        for kwargs in ({"incomplete": True}, {"invalid": True}):
            with self.subTest(**kwargs), self.archive(**kwargs) as archive:
                with self.assertRaises(ValueError):
                    self.apply(archive)
                self.assertEqual(app.read_text(), "# working entry point\n")
                self.assertTrue(old.exists())

    def test_existing_sha_repairs_once_before_becoming_up_to_date(self):
        old = self.root / "beys/jambo_jamunter.json"
        old.write_bytes((self.root / "beys/jail_jormungand.json").read_bytes())
        state = {"sha": "test-sha"}  # Written by the old updater.
        head = {"sha": "test-sha", "message": "same commit", "date": "today"}
        def write_state(value):
            state.clear()
            state.update(value)
        with self.archive() as archive, patch.object(updater, "_ROOT", str(self.root)), \
                patch.object(updater, "_BACKUP_DIR", str(self.root / ".update_backup")), \
                patch.object(updater, "_read_state", side_effect=lambda: dict(state)), \
                patch.object(updater, "_write_state", side_effect=write_state), \
                patch.object(updater, "_cfg", side_effect=lambda name, default="": "token" if name == "GITHUB_TOKEN" else default), \
                patch.object(updater, "head_commit", return_value=head), \
                patch.object(updater, "_download_zip", return_value=archive) as download, \
                patch.object(updater, "purge_pycache_logged"), \
                patch.dict("os.environ", {"BEYCORD_AUTO_UPDATE": "1"}):
            updater.check_and_apply()
            self.assertFalse(old.exists())
            self.assertEqual(state["catalog_sync_version"], updater.CATALOG_SYNC_VERSION)
            updater.check_and_apply()
            download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
