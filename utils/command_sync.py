"""
command_sync.py — keep Discord's command list matching the bot's.

The problem this solves
-----------------------
`tree.sync()` replaces the whole GLOBAL set, so a command deleted from the code
disappears globally on the next boot. Guild-scoped commands do not work that
way. `;sync` runs `copy_global_to(guild=...)` followed by `sync(guild=...)`,
which writes a full guild-scoped copy of every command — and nothing ever
removes entries from that copy.

So once a command has been guild-synced, two things follow and neither ever
undoes itself:

* deleting the command from the bot leaves the guild copy behind forever, and
  Discord serves the stale guild version — "commands the bot doesn't have any
  more";
* keeping the command means it now exists twice, globally and in the guild, and
  the picker draws both — "the same command twice".

Both were live. The retired `/tournament` group stayed registered in every
guild ever synced, and every surviving command was listed twice on top of that.

What reconcile() does
---------------------
* Syncs globally, which prunes stale global commands by itself.
* Deletes EVERY guild-scoped command, because after a successful global sync
  each one is either stale (the bot dropped it) or a duplicate of a global that
  already works. Both belong in the bin.
* Warns about names registered more than once inside the bot.

It never raises. A rate limit or a missing permission logs and moves on — the
bot must still start.

Why "delete every guild copy" and not "delete the stale ones"
-------------------------------------------------------------
That WAS the old behaviour, and it is why every command in this bot showed up
twice. It kept "valid mirrors" — a guild copy of a command the bot still has —
on the theory that someone who guild-synced for instant registration should
keep it. But a valid mirror IS the duplicate: Discord's picker is the union of
the global list and the guild list, so a command present in both is drawn
twice, and the boot cleanup deliberately preserved exactly that.

Guild mirrors buy one thing: registration is instant instead of taking up to an
hour. That is worth something on the day you add a command and nothing at all
on every other day, and it is not worth a permanently doubled command list. Set
BEYCORD_GUILD_MIRROR=1 to keep the old behaviour and accept the duplicates.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Iterable, Optional

import discord

log = logging.getLogger("beyblade_bot.sync")

_OPT_OUT = "BEYCORD_AUTO_PRUNE"
# Opt IN to guild-scoped mirrors. Off by default: a mirror duplicates every
# command in the picker, which is the bug this module exists to prevent.
_MIRROR = "BEYCORD_GUILD_MIRROR"
# Each guild costs a fetch and possibly a sync. Bots in many servers should not
# spend their whole startup budget here, so cap it and let later boots continue.
MAX_GUILDS_PER_BOOT = 25
GUILD_DELAY = 0.6          # gentle spacing; the API is not in a hurry


def _own_command_names(bot) -> set[str]:
    """Top-level app command names the bot currently defines.

    Only top level matters: subcommands live inside their group, so a stale
    subcommand disappears when its group is replaced.
    """
    names = set()
    try:
        for cmd in bot.tree.get_commands():
            names.add(cmd.name)
    except Exception as e:                           # noqa: BLE001
        log.warning("[sync] couldn't read local commands: %s", e)
    return names


def find_duplicates(bot) -> list[str]:
    """Names defined more than once locally.

    discord.py rejects a straight duplicate registration, but a prefix command
    and an app command can share a name, and two cogs can each contribute a
    group with the same name through different paths. Those show up as one
    command shadowing another with no error anywhere.
    """
    seen: dict[str, int] = {}
    try:
        for cmd in bot.tree.get_commands():
            seen[cmd.name] = seen.get(cmd.name, 0) + 1
    except Exception:                                # noqa: BLE001
        return []
    return sorted(n for n, c in seen.items() if c > 1)


async def prune_guild(bot, guild, keep: Optional[set[str]] = None) -> list[str]:
    """Delete this guild's own command copies. Returns the names removed.

    `keep` is the set of names to SPARE. The default — None — spares nothing,
    which is what reconcile wants: after a successful global sync every guild
    copy is either stale or a duplicate.

    Passing a set restores the old selective behaviour and is what `;sync clean`
    uses, for a guild running deliberate mirrors where only the stale entries
    should go.
    """
    removed: list[str] = []
    try:
        existing = await bot.tree.fetch_commands(guild=guild)
    except discord.Forbidden:
        return removed                               # no applications.commands scope
    except Exception as e:                           # noqa: BLE001
        log.debug("[sync] fetch failed for %s: %s", getattr(guild, "id", "?"), e)
        return removed

    stale = ([c for c in existing if c.name not in keep] if keep is not None
             else list(existing))
    if not stale:
        return removed

    for cmd in stale:
        try:
            await cmd.delete()
            removed.append(cmd.name)
        except Exception as e:                       # noqa: BLE001
            log.warning("[sync] couldn't delete /%s in %s: %s",
                        cmd.name, getattr(guild, "id", "?"), e)
    return removed


def mirroring() -> bool:
    """True when guild-scoped mirrors are opted in and duplicates are accepted."""
    return os.getenv(_MIRROR, "").strip().lower() in ("1", "true", "yes", "on")


async def reconcile(bot, guilds: Optional[Iterable] = None) -> dict:
    """Sync globally, then delete the guild copies that duplicate the result.

    Safe to call on every boot. Returns a small report for logging.
    """
    report = {"synced": 0, "pruned": {}, "duplicates": [], "skipped": 0,
              "mirroring": mirroring()}

    dupes = find_duplicates(bot)
    if dupes:
        report["duplicates"] = dupes
        log.warning("[sync] defined more than once locally: %s",
                    ", ".join("/" + d for d in dupes))

    try:
        synced = await bot.tree.sync()
        report["synced"] = len(synced)
        log.info("[sync] %d global command(s) registered.", len(synced))
    except Exception as e:                           # noqa: BLE001
        log.error("[sync] global sync failed: %s", e)
        # Without a successful global sync we don't know the real command set,
        # and pruning against a guess could delete working commands.
        return report

    if os.getenv(_OPT_OUT, "1").strip().lower() in ("0", "false", "no", "off"):
        return report

    keep = _own_command_names(bot)
    if not keep:
        log.warning("[sync] no local commands found — skipping prune.")
        return report

    if report["mirroring"]:
        # Opted in: spare the mirrors, take only the stale entries. Say so, so
        # that "why is everything listed twice" has an answer in the log rather
        # than needing this module to be read.
        log.info("[sync] %s=1 — keeping guild mirrors. Commands registered "
                 "both globally and in a guild appear TWICE in the picker; "
                 "unset it to have them removed.", _MIRROR)
    else:
        keep = None

    targets = list(guilds if guilds is not None else getattr(bot, "guilds", []))
    if len(targets) > MAX_GUILDS_PER_BOOT:
        report["skipped"] = len(targets) - MAX_GUILDS_PER_BOOT
        targets = targets[:MAX_GUILDS_PER_BOOT]

    for guild in targets:
        removed = await prune_guild(bot, guild, keep)
        if removed:
            report["pruned"][getattr(guild, "id", "?")] = removed
            log.info("[sync] removed %d guild command copy/copies in %s: %s",
                     len(removed), getattr(guild, "name", guild),
                     ", ".join("/" + r for r in removed))
        await asyncio.sleep(GUILD_DELAY)

    total = sum(len(v) for v in report["pruned"].values())
    if total:
        log.info("[sync] pruned %d guild command copy/copies across %d "
                 "guild(s) — each was a duplicate of, or stale against, the "
                 "global list.", total, len(report["pruned"]))
    if report["skipped"]:
        log.info("[sync] %d guild(s) left for the next boot (per-boot cap).",
                 report["skipped"])
    return report


async def purge_guild(bot, guild) -> int:
    """Remove EVERY guild-scoped command, leaving only the globals.

    The blunt option behind `;sync purge`, for when a guild's copy has drifted
    far enough that reconciling one name at a time isn't worth it. Globals keep
    working throughout — this only deletes the guild-scoped duplicates.
    """
    try:
        bot.tree.clear_commands(guild=guild)
        cleared = await bot.tree.sync(guild=guild)
        return len(cleared)
    except Exception as e:                           # noqa: BLE001
        log.error("[sync] purge failed for %s: %s", getattr(guild, "id", "?"), e)
        raise
