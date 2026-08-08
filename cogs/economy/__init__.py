"""cogs/economy/__init__.py - Entry point for economy subsystem."""
from discord.ext import commands

# Import setup functions from each cog module
from .shop import setup as shop_setup
from .profile import setup as profile_setup

# `leaderboard.py` used to be loaded here. It owned `;leaderboard` and `;rank`,
# both of which cogs/ranked/ now provides with the ranked-only rules, five
# categories and the verification gate. Loading both would fail the boot with
# CommandAlreadyRegistered, and keeping two implementations of the same two
# commands is how they drift apart.


async def setup(bot: commands.Bot) -> None:
    """Load all economy cogs."""
    await shop_setup(bot)
    await profile_setup(bot)

__all__ = ["setup"]
