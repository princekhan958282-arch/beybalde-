"""Public latency diagnostics for BEYCBOT."""
from __future__ import annotations

import time

import discord
from discord.ext import commands


class PingCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="ping")
    @commands.cooldown(1, 5.0, commands.BucketType.user)
    async def ping(self, ctx: commands.Context) -> None:
        """Measure gateway heartbeat plus a real Discord message round trip."""
        websocket_ms = max(0.0, self.bot.latency * 1000.0)

        started = time.perf_counter()
        message = await ctx.send("🏓 Measuring latency…")
        api_ms = (time.perf_counter() - started) * 1000.0

        embed = discord.Embed(title="🏓 Pong!", color=discord.Color.green())
        embed.add_field(name="Discord WebSocket", value=f"{websocket_ms:.0f} ms", inline=True)
        embed.add_field(name="Message / API", value=f"{api_ms:.0f} ms", inline=True)
        embed.set_footer(text="Message/API includes the bot → Discord request round trip.")
        await message.edit(content=None, embed=embed)

    @ping.error
    async def ping_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏱️ Try ;ping again in {error.retry_after:.1f}s.", delete_after=3)
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PingCog(bot))
