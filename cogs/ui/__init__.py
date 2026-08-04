"""cogs/ui/__init__.py - Entry point for UI subsystem."""

from .help_cog import setup as _help_setup
from .main_shop import setup as _shop_setup
from .level_up import setup as _levelup_setup


async def setup(bot):
    await _help_setup(bot)
    await _shop_setup(bot)
    await _levelup_setup(bot)


__all__ = ["setup"]
