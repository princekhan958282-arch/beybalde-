"""
Notifications — updates out to players, reports back in.

    /update          compose, preview, send, watch, cancel, review  (owner)
    /bugs            report a bug, from any server
    /suggest         suggest an improvement
    ;notifications   the player's two switches

    Admin / game system → service.notify() → the queue → the worker → a DM
                                                              → the ledger

Every delivery is one row in `update_deliveries`, keyed
`(update_id, user_id)`, so a player cannot receive the same update twice —
enforced by the database rather than by anything in this package.

The cog import is deferred into `setup()` so importing this package does not
drag in discord; `store`, `prefs` and `targeting` stay loadable headless, which
is what `tools/sim_updates.py` relies on.
"""


async def setup(bot):
    from .cog import setup as _setup
    await _setup(bot)


__all__ = ["setup"]
