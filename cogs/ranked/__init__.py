"""
cogs/ranked/__init__.py
-----------------------
The ranked ladder: leaderboards, rank cards, verification and owner resets.

    from cogs.ranked import RankedCog, RankedCommands
    bot.load_extension("cogs.ranked")

Rules live in `utils/ranked.py` (no discord imports, headlessly testable);
this package only renders them.
"""

from .ranked_cog import RankedCog, RankedCommands

__all__ = ["RankedCog", "RankedCommands"]


async def setup(bot) -> None:
    await bot.add_cog(RankedCog(bot))
    await bot.add_cog(RankedCommands(bot))
