"""cogs/tournament — the scheduled tournament system.

Layered on purpose:

    models      pure types + the match state machine
    timeslots   availability, UTC conversion, overlap
    brackets    single / double elimination, round robin
    store       SQLite persistence
    service     all the rules; no discord import anywhere below this line
    cog         the Discord surface only

`service` and everything under it can be driven headlessly, which is how the
bracket and scheduling logic get tested without a gateway connection.

The import of .cog is deferred into setup() for the same reason cogs/battle
defers its own: importing the package must not drag in discord at import time.
"""


async def setup(bot):
    from .cog import setup as _setup
    await _setup(bot)


__all__ = ["setup"]
