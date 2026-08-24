"""
cogs/snapshots — automatic daily copies of every player's data.

The cog import is deferred into `setup()` so `tools/sim_snapshots.py` can drive
`utils/snapshot.py` and this package's clock headless, the same arrangement
`cogs/updates` and `cogs/community` use.
"""

from __future__ import annotations


async def setup(bot):
    from .cog import setup as _setup
    await _setup(bot)
