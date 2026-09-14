"""Beycord Horror Story subsystem."""

from .horror import setup as _setup_horror
from .unknown_battle import setup as _setup_unknown
from .settings_fix import setup as _setup_settings_fix
from .selection_fix import setup as _setup_selection_fix


async def setup(bot) -> None:
    await _setup_horror(bot)
    await _setup_unknown(bot)
    await _setup_settings_fix(bot)
    await _setup_selection_fix(bot)


__all__ = ["setup"]
