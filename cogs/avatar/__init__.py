"""
cogs/avatar/__init__.py
-----------------------
Avatar system package.

Public API (used by combat engine):
    from cogs.avatar import avatar_engine, AvatarBonuses, NULL_BONUSES

Cog entry point (loaded by bot):
    bot.load_extension("cogs.avatar")
"""

from .avatar_engine import avatar_engine, AvatarBonuses, NULL_BONUSES
from .avatar_shop import AvatarShop

__all__ = ["avatar_engine", "AvatarBonuses", "NULL_BONUSES"]


async def setup(bot) -> None:
    avatar_engine.load()
    await bot.add_cog(AvatarShop(bot))
