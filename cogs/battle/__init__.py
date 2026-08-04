"""cogs/battle/__init__.py - Entry point for battle subsystem.

The import of .battle is deferred into setup() on purpose.

cogs.abilities.ability_engine imports `cogs.battle.constants`, and importing
any submodule of a package runs that package's __init__ first. When this file
imported .battle at module level, that chain became:

    ability_engine -> cogs.battle (__init__) -> battle -> session -> ability_engine

so anything that reached ability_engine BEFORE cogs.battle was already loaded
died with "cannot import name 'AbilityEngine' from partially initialized
module". The bot only survived because app.py happens to list cogs.abilities
(whose __init__ is a no-op) ahead of cogs.battle — a load-order accident, not a
design. tools/sim_aero.py already had to fake the package to work around it.

discord.py only needs `setup` to be callable at load time, so importing inside
the function costs nothing and breaks the cycle outright.
"""


async def setup(bot):
    from .battle import setup as _battle_setup
    await _battle_setup(bot)


__all__ = ["setup"]
