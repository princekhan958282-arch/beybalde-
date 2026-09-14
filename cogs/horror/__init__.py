"""Beycord Horror Story subsystem."""

from .horror import setup as _setup_horror
from .unknown_battle import setup as _setup_unknown


async def setup(bot) -> None:
    await _setup_horror(bot)
    await _setup_unknown(bot)


__all__ = ["setup"]
