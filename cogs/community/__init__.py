"""
cogs/community — the premium community layer, main server only.

Personality, polls, giveaways, memes, banter, reaction roles, XP/levels and
roasting. Every one of them is gated on ONE configured guild; see `guard.py`,
which is the only module that knows which guild that is.

The cog import is deferred into `setup()` on purpose, exactly as
`cogs/updates/__init__.py` does it: `tools/sim_community.py` imports `guard`,
`store`, `config` and the managers headless, and dragging discord in at package
import time would make that impossible.
"""

from __future__ import annotations


async def setup(bot):
    from .cog import setup as _setup
    await _setup(bot)
