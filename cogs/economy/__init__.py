"""cogs/economy/__init__.py - Entry point for economy subsystem."""
from discord.ext import commands

# Import setup functions from each cog module
from .shop import setup as shop_setup
from .profile import setup as profile_setup
from .leaderboard import setup as leaderboard_setup

async def setup(bot: commands.Bot) -> None:
    """Load all economy cogs."""
    await shop_setup(bot)
    await profile_setup(bot)
    await leaderboard_setup(bot)

__all__ = ["setup"]
