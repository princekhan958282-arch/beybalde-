from .clan import ClanCog
from .clan_war import ClanWarCog


async def setup(bot):
    if not bot.get_cog("Clans"):
        await bot.add_cog(ClanCog(bot))
    if not bot.get_cog("Clan Wars"):
        await bot.add_cog(ClanWarCog(bot))


__all__ = ["ClanCog", "ClanWarCog", "setup"]
