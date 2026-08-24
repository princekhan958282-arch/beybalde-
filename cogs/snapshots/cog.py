"""
cogs/snapshots/cog.py — the hourly tick that produces a daily backup.

Hourly, not daily, on purpose: see `clock.py`. The loop asks the clock whether
a day has passed according to a timestamp in the database, so restarts cannot
starve the schedule.

The export reads every profile in the store — 0.63 s for 3,410 of them — so it
runs on a worker thread. Nothing here may block the event loop, and nothing
here may raise: a backup that takes the bot down is worse than no backup.
"""

from __future__ import annotations

import asyncio
import logging
import os

from discord.ext import commands, tasks

from utils import snapshot as SN

from . import clock

log = logging.getLogger("beyblade_bot.snapshot")

TICK_HOURS = 1


class SnapshotCog(commands.Cog, name="Snapshots"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.folder = SN.folder()
        self.last_error = ""
        self.taken = 0

    async def cog_load(self) -> None:
        self.snapshot_loop.start()

    async def cog_unload(self) -> None:
        self.snapshot_loop.cancel()

    @tasks.loop(hours=TICK_HOURS)
    async def snapshot_loop(self) -> None:
        try:
            if not clock.due():
                return
            await self.take("scheduled")
        except Exception as exc:                         # noqa: BLE001
            log.exception("[snapshot] scheduled run failed")
            self._record(exc)

    @snapshot_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def take(self, why: str = "manual") -> str:
        """Write one snapshot, prune, stamp the clock. Returns the path."""
        path = await asyncio.to_thread(SN.write, self.folder)
        clock.mark()
        removed = await asyncio.to_thread(SN.prune, self.folder)
        self.taken += 1
        self.last_error = ""
        log.info("[snapshot] %s backup -> %s (%s KB, pruned %s)", why,
                 os.path.basename(path),
                 os.path.getsize(path) // 1024 or 1, removed)
        return path

    def _record(self, exc: Exception) -> None:
        self.last_error = f"{type(exc).__name__}: {exc}"
        try:
            from utils import errorlog
            errorlog.record("snapshot.loop", exc)
        except Exception:                                # noqa: BLE001
            pass
        # Tell the owner. A backup system that fails silently is the same as
        # not having one, and the failure is only discovered when it is needed.
        try:
            from cogs.admin import actions as A
            from cogs.updates import service as SV
            self.bot.loop.create_task(SV.notify_user(
                self.bot, A.MASTER_ID, event="IMPORTANT_NOTICE",
                title="⚠️ Backup failed",
                body=(f"The daily player-data snapshot did not run:\n"
                      f"`{self.last_error}`\n\n"
                      f"`/admin → System → Backups` to try by hand.")))
        except Exception:                                # noqa: BLE001
            log.exception("[snapshot] could not warn the owner")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SnapshotCog(bot))
