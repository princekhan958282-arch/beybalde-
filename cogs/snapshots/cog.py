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
from . import receipt as RC

log = logging.getLogger("beyblade_bot.snapshot")

TICK_HOURS = 1


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class SnapshotCog(commands.Cog, name="Snapshots"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.folder = SN.folder()
        self.last_error = ""
        self.taken = 0
        self.last_github: dict = {}

    async def cog_load(self) -> None:
        # Registered ONCE — a receipt DM can sit for days, and its buttons
        # must still work after a restart. See cogs/updates/reports.py for
        # the same pattern.
        try:
            self.bot.add_dynamic_items(RC.ReceiptButton)
        except Exception:                                # noqa: BLE001
            log.exception("[snapshot] could not register receipt buttons")
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
        """Write one snapshot, prune, stamp the clock. Returns the path.

        GitHub is a second, best-effort destination — pushed on EVERY run,
        scheduled or manual, so a manual backup is equally protected. It can
        never block or roll back the local half above: that write already
        happened by the time this runs, and a GitHub outage must not be able
        to take the local backup path down with it.
        """
        path = await asyncio.to_thread(SN.write, self.folder)
        clock.mark()
        removed = await asyncio.to_thread(SN.prune, self.folder)
        self.taken += 1
        self.last_error = ""
        local_kb = os.path.getsize(path) // 1024 or 1
        log.info("[snapshot] %s backup -> %s (%s KB, pruned %s)", why,
                 os.path.basename(path), local_kb, removed)

        day = SN.stamp()
        gh = await self._push_github(path, day)

        if why == "scheduled":
            await self._send_receipt(day, local_kb, gh)
        return path

    async def _push_github(self, path: str, day: str) -> dict:
        from utils import github_backup as GB
        token, repo = GB.configured()
        if not token or not repo:
            self.last_github = {"ok": False, "error": "not configured"}
            return self.last_github
        try:
            gz_bytes = await asyncio.to_thread(_read_bytes, path)
            result = await asyncio.to_thread(GB.push_snapshot, gz_bytes, day)
        except Exception as exc:                         # noqa: BLE001
            log.exception("[snapshot] GitHub push crashed")
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self.last_github = result
        if not result.get("ok"):
            log.warning("[snapshot] GitHub push failed: %s", result.get("error"))
            self._record_github_failure(result)
        else:
            log.info("[snapshot] pushed to %s", result.get("repo"))
        return result

    async def _send_receipt(self, day: str, local_kb: int, gh: dict) -> None:
        try:
            local_path = os.path.join(self.folder, SN.DAILY_DIR, f"{day}.json.gz")
            snap = SN.read(local_path)
            describe = SN.describe(snap)
        except Exception:                                # noqa: BLE001
            log.exception("[snapshot] could not describe today's snapshot")
            describe = {}
        try:
            from cogs.admin import actions as A
            owner = self.bot.get_user(A.MASTER_ID) or await self.bot.fetch_user(A.MASTER_ID)
            embed = RC.build_embed(day, describe, local_kb, gh)
            view = RC.view_for(day)
            await owner.send(embed=embed, view=view)
        except Exception:                                # noqa: BLE001
            log.exception("[snapshot] could not DM the backup receipt")

    def _record_github_failure(self, result: dict) -> None:
        try:
            from cogs.admin import actions as A
            from cogs.updates import service as SV
            self.bot.loop.create_task(SV.notify_user(
                self.bot, A.MASTER_ID, event="IMPORTANT_NOTICE",
                title="⚠️ GitHub backup push failed",
                body=(f"The local snapshot in `backups/` is fine — only the "
                      f"GitHub copy failed:\n`{result.get('error', '?')}`\n\n"
                      f"`/admin → System → Backups` for details.")))
        except Exception:                                # noqa: BLE001
            log.exception("[snapshot] could not warn the owner about GitHub")

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
