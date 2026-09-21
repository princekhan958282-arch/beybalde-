"""Focused scheduling tests for the round result and next battle card."""

import ast
import asyncio
from pathlib import Path
import types
import unittest


def _round_tail():
    """Load the production scheduling block without importing the whole bot."""
    source = Path(__file__).resolve().parents[1] / "cogs/battle/session.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    session = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == "BattleSession")
    method = next(n for n in session.body if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "__resolve_round_body")
    index = next(i for i, n in enumerate(method.body)
                 if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "card_task"
                         for t in n.targets))
    body = method.body[index:index + 2]
    args = ast.arguments(posonlyargs=[], args=[ast.arg(arg="self"),
                                                 ast.arg(arg="result_embed")],
                         vararg=None, kwonlyargs=[], kw_defaults=[],
                         kwarg=None, defaults=[])
    fn = ast.AsyncFunctionDef(name="run", args=args, body=body,
                              decorator_list=[])
    code = compile(ast.fix_missing_locations(ast.Module(body=[fn],
                                                      type_ignores=[])),
                   str(source), "exec")
    scope = {
        "asyncio": asyncio,
        "discord": types.SimpleNamespace(
            Embed=lambda **kw: types.SimpleNamespace(
                set_image=lambda **image_kw: None, **kw),
            Color=types.SimpleNamespace(dark_embed=lambda: 0)),
        "_InChannelControlPanel": lambda session: object(),
    }
    exec(code, scope)
    return scope["run"]


class _Session:
    def __init__(self):
        self.round = 2
        self.card_started = asyncio.Event()
        self.card_release = asyncio.Event()
        self.result_started = asyncio.Event()
        self.result_release = asyncio.Event()
        self.card_cancelled = False
        self.sent = []
        self.fail_result = False
        self.card = object()
        self.channel = self

    async def _battle_card_file(self):
        self.card_started.set()
        try:
            await self.card_release.wait()
        except asyncio.CancelledError:
            self.card_cancelled = True
            raise
        return self.card

    async def _prime_npc_move(self):
        pass

    def _status_embed(self):
        return "fallback"

    async def send(self, *, embed, file=None, view=None):
        if embed == "result":
            self.result_started.set()
            await self.result_release.wait()
            if self.fail_result:
                raise RuntimeError("send failed")
        self.sent.append((embed, file, view))
        return object()


class BattleCardOverlapTests(unittest.IsolatedAsyncioTestCase):
    async def test_render_overlaps_result_and_panel_waits_for_both(self):
        session = _Session()
        task = asyncio.create_task(_round_tail()(session, "result"))
        await asyncio.wait_for(asyncio.gather(session.card_started.wait(),
                                              session.result_started.wait()), 1)
        self.assertEqual(session.sent, [])
        session.result_release.set()
        await asyncio.sleep(0)
        self.assertEqual(len(session.sent), 1)
        session.card_release.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual(len(session.sent), 2)
        self.assertIs(session.sent[1][1], session.card)

    async def test_failed_result_send_cancels_render(self):
        session = _Session()
        session.fail_result = True
        task = asyncio.create_task(_round_tail()(session, "result"))
        await asyncio.wait_for(session.card_started.wait(), 1)
        session.result_release.set()
        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await asyncio.wait_for(task, 1)
        self.assertTrue(session.card_cancelled)
        self.assertEqual(session.sent, [])

    async def test_missing_card_uses_existing_text_fallback(self):
        session = _Session()
        session.card = None
        session.result_release.set()
        session.card_release.set()
        await asyncio.wait_for(_round_tail()(session, "result"), 1)
        self.assertEqual(session.sent[1][0], "fallback")
        self.assertIsNone(session.sent[1][1])


if __name__ == "__main__":
    unittest.main()

