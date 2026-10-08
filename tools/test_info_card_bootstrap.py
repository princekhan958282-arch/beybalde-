"""Cold-process regressions for a failed early info-card selector import."""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]

BOOT = '''
import importlib.abc
import sys
class BlockSelector(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'utils.info_card_router':
            raise ImportError('simulated early startup dependency failure')
block = BlockSelector()
sys.meta_path.insert(0, block)
from utils import info_card
assert info_card.__name__ == 'utils.info_card', info_card.__name__
import inspect
assert 'info_theme' in inspect.signature(info_card.render_info_card).parameters
'''


class BootstrapTests(unittest.TestCase):
    def run_script(self, source):
        result = subprocess.run([sys.executable, '-c', source], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_failed_startup_selector_recovers_lazily_and_command_accepts_theme(self):
        self.run_script(BOOT + '''
sys.meta_path.remove(block)
import asyncio
import io
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
from contextlib import asynccontextmanager
from utils import info_card_router as router
from cogs.economy.profile import ProfileCog
from utils import database
@asynccontextmanager
async def typing():
    yield
async def main():
    # The command retains the original fallback module reference after startup.
    import cogs.economy.profile as profile
    assert profile.info_card is info_card
    ctx = SimpleNamespace(author=SimpleNamespace(id=1), typing=typing, send=AsyncMock())
    buf = io.BytesIO(b'test PNG attachment'); buf.name='beycbot_info.png'
    with patch.object(database,'get_user',AsyncMock(return_value={
        'owned_info_card_themes':['beycbot_2ability'],
        'equipped_info_card_theme':'beycbot_2ability'})), \\
         patch.object(profile,'_viewer_parts',AsyncMock(return_value={})), \\
         patch.object(router,'render_info_card',AsyncMock(return_value=buf)) as render:
        await ProfileCog(None)._send_bey_card(ctx,{'name':'Cold Boot Bey','type':'Attack'})
        assert render.call_args.kwargs['info_theme'] == 'beycbot_2ability'
        assert 'file' in ctx.send.call_args.kwargs
        ctx.send.call_args.kwargs['file'].close()
asyncio.run(main())
''')

    def test_selector_still_unavailable_accepts_theme_and_falls_back(self):
        self.run_script(BOOT + '''
import asyncio
from unittest.mock import patch, AsyncMock
async def main():
    blade={'name':'Default Bey', 'abilities':[]}
    with patch.object(info_card,'_render_compat_legacy',AsyncMock(return_value='normal')) as normal:
        assert await info_card.render_info_card(blade,parts={},info_theme='beycbot_2ability') == 'normal'
        assert await info_card.render_info_card(blade,info_theme='default') == 'normal'
        assert normal.await_count == 2
asyncio.run(main())
''')

    def test_normal_boot_routes_and_legacy_rollback_accepts_theme(self):
        self.run_script('''
import asyncio
import os
from unittest.mock import patch, AsyncMock
from utils import info_card
from utils import info_card_router as router
assert info_card is router
async def main():
    os.environ['BEYCORD_INFO_CARD_VERSION']='legacy'
    with patch.object(router.legacy,'render_info_card',AsyncMock(return_value='legacy')):
        assert await info_card.render_info_card({'name':'Normal','abilities':[]},info_theme='beycbot_2ability') == 'legacy'
asyncio.run(main())
''')


if __name__ == '__main__':
    unittest.main()
