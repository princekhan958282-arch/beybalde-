"""
cogs/community/guard.py — the main-server lock. One module, one truth.

Nothing else in this package learns the guild id; everything asks here. That
matters because `MASTER_ID` is copy-pasted into six files across this codebase
and a seventh copy of anything is how these checks drift apart.

Three layers, because hiding a command is not enforcement
-----------------------------------------------------------
`gate()`        the COMMAND layer — every slash callback opens with it.
`require_main()` the SERVICE layer — every public manager method calls it
                before touching state. This is the one that makes a mistake in
                the command layer harmless rather than a cross-server leak.
`listener_ok()` the EVENT layer — on_message and on_raw_reaction_add.

**Unset means locked.** With no main server configured every entry point
refuses in every guild, including the one that will later be the main server.
The alternative — treating "unconfigured" as "allow" — would open the whole
layer everywhere the first time the config read failed.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import config as C

log = logging.getLogger("beyblade_bot.community.guard")

REFUSAL = ("🔒 That's a **main-server** feature — it isn't available here.")
UNSET = ("🔒 No main server is configured yet. The owner sets it with "
         "**/server → Set this server**.")


class NotMainServer(RuntimeError):
    """Raised by the service layer. Carries the guild that was refused."""

    def __init__(self, guild_id: Any = None, configured: Any = None) -> None:
        self.guild_id = guild_id
        self.configured = configured
        super().__init__(
            f"guild {guild_id!r} is not the main server "
            f"({'unset' if not configured else configured})")


def _as_id(guild_or_id: Any) -> Optional[int]:
    """Accept a Guild, a Member's guild, an int, a str, or None."""
    if guild_or_id is None:
        return None
    if isinstance(guild_or_id, int):
        return guild_or_id
    if isinstance(guild_or_id, str):
        return int(guild_or_id) if guild_or_id.isdigit() else None
    for attr in ("id", "guild_id"):
        val = getattr(guild_or_id, attr, None)
        if isinstance(val, int):
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)
    guild = getattr(guild_or_id, "guild", None)
    return getattr(guild, "id", None) if guild is not None else None


def main_guild_id() -> Optional[int]:
    return C.main_guild_id()


def set_main_guild(guild_or_id: Any) -> Optional[int]:
    gid = _as_id(guild_or_id)
    if gid is None:
        C.put(C.K_MAIN_GUILD, None)
        return None
    C.put(C.K_MAIN_GUILD, str(gid))
    return gid


def is_configured() -> bool:
    return main_guild_id() is not None


def is_main(guild_or_id: Any) -> bool:
    """True only when a main server is set AND this is it."""
    configured = main_guild_id()
    if configured is None:
        return False
    gid = _as_id(guild_or_id)
    return gid is not None and gid == configured


def require_main(guild_or_id: Any) -> int:
    """The service-layer gate. Returns the guild id, or raises.

    Every public method on every manager calls this first. The suite proves it
    by introspection, so a manager method added later without it fails the day
    it is written rather than the day it leaks.
    """
    configured = main_guild_id()
    gid = _as_id(guild_or_id)
    if configured is None or gid is None or gid != configured:
        raise NotMainServer(gid, configured)
    return gid


def listener_ok(event: Any) -> bool:
    """The event-layer gate: a Message, a RawReactionActionEvent, a guild.

    Never raises — a listener that throws on every message in every other
    server would be worse than the leak it is preventing.
    """
    try:
        return is_main(event)
    except Exception:                                    # noqa: BLE001
        log.exception("[community] listener guard failed")
        return False


async def gate(interaction: Any) -> bool:
    """The command-layer gate. Replies ephemerally and returns False.

    Returns True only for the main server, so the caller's first line is
    `if not await gate(interaction): return`.
    """
    guild_id = (getattr(interaction, "guild_id", None)
                or _as_id(getattr(interaction, "guild", None)))
    if is_main(guild_id):
        return True
    message = UNSET if not is_configured() else REFUSAL
    try:
        response = interaction.response
        if response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await response.send_message(message, ephemeral=True)
    except Exception:                                    # noqa: BLE001
        log.exception("[community] could not deliver the refusal")
    return False
