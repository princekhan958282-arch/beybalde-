"""
cogs.tournament — one command, one panel, one bracket.

    tournament.py   the command, the public panel, the bracket runner
    brackets.py     pure bracket maths (seed, generate, advance, byes)
    models.py       the dataclasses brackets speaks in

`brackets` and `models` are discord-free, which is what lets a whole
tournament be played out in a test loop to prove it terminates with exactly
one champion. They survived the v1.12 rewrite unchanged for that reason — the
bracket maths was never the part that was wrong.

`setup` is deferred into the submodule so importing this package does not drag
in `discord`, and the headless suites can import `brackets` on its own.
"""


async def setup(bot):
    from .tournament import setup as _setup
    await _setup(bot)
