"""Startup without network login, auto-updates, bootstrap installs or real DBs."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from utils import database as DB
from utils.userstore import UserStore

class CharacterStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_extensions_and_character_commands_register(self):
        with patch.dict(os.environ, {'BEYCORD_AUTO_INSTALL':'0', 'BEYCORD_AUTO_UPDATE':'0'}):
            import app
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=UserStore(str(root/'users.db'), str(root/'users.json'))
            with patch.object(DB,'USER_STORE',store), patch.object(app,'_ensure_chromium',return_value=None):
                async with app.BeybladeBot() as bot:
                    await bot.setup_hook()
                    self.assertEqual(set(bot.extensions), set(app.COGS))
                    for command in ('info','ainfo','shop','buy','myparts','equippart','unequippart','equip','inventory'):
                        self.assertIsNotNone(bot.get_command(command), command)
                    self.assertIsNotNone(bot.tree.get_command('custombey'))
                    for extension in list(bot.extensions):
                        await bot.unload_extension(extension)

if __name__=='__main__': unittest.main()
