"""Beycord Horror Story subsystem."""

from .horror import setup as _setup_horror
from .unknown_battle import setup as _setup_unknown
from .runtime_hooks import install_runtime_hooks, schedule_recovery


async def setup(bot) -> None:
    # Install the release guards before either cog starts accepting interactions.
    install_runtime_hooks()
    await _setup_horror(bot)
    await _setup_unknown(bot)
    schedule_recovery(bot)


__all__ = ["setup"]
