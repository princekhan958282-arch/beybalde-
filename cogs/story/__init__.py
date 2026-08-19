"""
Story Mode — the School League.

    ;story              → the chapter picker  (alias: ;league)
    ;story <n>          → fight battle n on Normal
    ;story <n> nightmare
    ;storymap           → the eight battles, your progress, the difficulty
    /story              → the same picker, from a slash command

Runs on the REAL PvP engine (`cogs/battle/session.py`), not on the boss
resolver it used to use. That is the whole point of the rebuild: stability,
ring-outs, the full ability DSL, named Special moves, status effects and the
real damage pipeline were all unreachable from Story before, so a blade's
ability did nothing and its Special was a flat multiplier.

The opponent is an NPC driven by `story_ai.LeagueOpponent`, which borrows the
boss engine's BRAIN — `OpponentModel` and the IQ ladder — while the fight
itself is a `BattleSession`. `BattleSession` gained three optional parameters
for this and behaves exactly as before for every other caller.

The cog import is deferred into setup() so importing this package does not drag
in discord — `story_data` stays loadable headless, which is what
`tools/sim_story.py` relies on.
"""


async def setup(bot):
    from .story_cog import setup as _setup
    await _setup(bot)


__all__ = ["setup"]
