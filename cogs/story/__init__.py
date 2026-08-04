"""
Story Mode — a solo chapter/stage campaign.

    ;story              → pick a stage
    ;story <stage>      → fight one directly, e.g. `;story 1-2`
    ;storymap           → chapters and your progress
    /story play|map|info|stats

Built on the boss PvE resolver (cogs/battle/boss/boss_ai.py), not on the live
PvP engine, for the reason boss_battle.py already documents: injecting a
synthetic second player into BattleSession would mean maintaining a parallel
path through every manager it is wired to.

Unlike the boss cog this is NOT gated behind a system lock — it deliberately
lives outside BossCog so it does not inherit BossCog.cog_check.

The cog import is deferred into setup() so importing this package does not drag
in discord — story_data, story_avatar and story_engine stay loadable headless,
which is what tools/sim_story.py relies on.
"""


async def setup(bot):
    from .story_cog import setup as _setup
    await _setup(bot)


__all__ = ["setup"]
