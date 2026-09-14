"""Release-safety hooks for the Horror Story subsystem.

These hooks keep the feature isolated from the normal battle engine while
closing the few lifecycle gaps that only show up in production: expiring CDN
URLs, bot restarts during a pending encounter, duplicate spawns while a battle
is already running, and the HP/Stability portion of the Unknown curse.
"""

from __future__ import annotations

import asyncio
import logging
import time

import discord

from cogs.battle.session import BattleSession
from utils import horror_state
from . import horror as horror_mod
from . import unknown_battle as unknown_mod

log = logging.getLogger("beyblade_bot.horror.runtime")

# Immutable commit-backed asset URLs. The old Discord links were signed URLs
# with an expiry timestamp, which would make the Horror art disappear after
# release. Pinning to the asset commit makes these URLs permanent.
_ASSET_BASE = (
    "https://raw.githubusercontent.com/princekhan958282-arch/"
    "beybalde-/48e88ed193a349da77734d80e1c32965bad34e47/assets/horror/"
)
_ACTIVE_STATUSES = {"spawned", "declined_once", "battle_requested", "battle_running"}


def _install_stable_art() -> None:
    """Replace short-lived Discord signed URLs with repository-backed art."""
    horror_mod.HORROR_IMAGE_URL = _ASSET_BASE + "unknown_challenger.jpg"
    unknown_mod.UNKNOWN_IMAGE_URL = _ASSET_BASE + "unknown_bey.jpg"
    unknown_mod.UNKNOWN_INFO_IMAGE_URL = _ASSET_BASE + "unknown_info.jpg"
    unknown_mod.UNKNOWN_BEY["image_url"] = unknown_mod.UNKNOWN_IMAGE_URL


def _install_full_curse_stats() -> None:
    """Make the existing 0.8 curse truly affect every combat stat.

    ATK/DEF/STM and Special output already pass through get_stat_multiplier(),
    which includes horror_state.curse_multiplier(). HP and Stability are built
    on separate paths in BattleSession, so scale only those two here to avoid
    double-applying the curse to the stats that are already correct.
    """
    if getattr(BattleSession, "_horror_full_curse_patch", False):
        return

    original_init = BattleSession.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        for player in getattr(self, "players", []):
            pid = getattr(player, "id", None)
            if pid is None:
                continue
            try:
                if self._is_npc(pid):
                    continue
            except Exception:  # noqa: BLE001
                continue

            mult = horror_state.curse_multiplier(pid)
            if mult >= 0.999:
                continue

            key = str(pid)

            # HP is not part of stat_mult, so reduce both current and max pool.
            hp = getattr(self, "hp", None)
            max_hp = getattr(self, "max_hp_per_player", None)
            if isinstance(hp, dict) and key in hp:
                hp[key] = max(1, int(round(float(hp[key]) * mult)))
                if isinstance(max_hp, dict):
                    max_hp[key] = hp[key]

            # Stability has its own manager and also bypasses stat_mult.
            stability = getattr(self, "stability_manager", None)
            if stability is not None:
                current = getattr(stability, "stability", None)
                maximum = getattr(stability, "max", None)
                if isinstance(current, dict) and key in current:
                    current[key] = max(1, int(round(float(current[key]) * mult)))
                if isinstance(maximum, dict) and key in maximum:
                    maximum[key] = max(1, int(round(float(maximum[key]) * mult)))

                # TypeModifiers keeps a copy of the starting stability value.
                try:
                    mod = self.type_mods.get(key)
                    if mod is not None and isinstance(maximum, dict) and key in maximum:
                        mod.stability_start = maximum[key]
                except Exception:  # noqa: BLE001
                    pass

    BattleSession.__init__ = patched_init
    BattleSession._horror_full_curse_patch = True


def _install_spawn_guard() -> None:
    """Prevent a second public spawn while the first encounter is active."""
    cls = horror_mod.HorrorCog
    if getattr(cls, "_horror_spawn_guard_patch", False):
        return

    original = cls.spawn_encounter

    async def guarded(self, guild, channel, target):
        status = horror_state.encounter(target.id).get("status")
        if status in _ACTIVE_STATUSES:
            return False, "❌ That player already has an unresolved Horror encounter."
        return await original(self, guild, channel, target)

    cls.spawn_encounter = guarded
    cls._horror_spawn_guard_patch = True


def _install_battle_failure_guard() -> None:
    """Never leave a target permanently stuck in battle_running after an error."""
    cls = unknown_mod.UnknownBattleCog
    if getattr(cls, "_horror_failure_guard_patch", False):
        return

    original = cls._run

    async def guarded(self, channel, player, *args, **kwargs):
        try:
            return await original(self, channel, player, *args, **kwargs)
        except Exception:  # noqa: BLE001
            log.exception("[horror] UNKNOWN battle crashed for user=%s", player.id)
            row = horror_state.encounter(player.id)
            if row.get("status") != "completed":
                horror_state.save_encounter(
                    player.id,
                    status="battle_failed",
                    battle_started=False,
                    bey_claimed=False,
                )
                try:
                    await channel.send(
                        f"{player.mention}\n**UNKNOWN:** the battle was interrupted."
                    )
                except discord.HTTPException:
                    pass
            return None

    cls._run = guarded
    cls._horror_failure_guard_patch = True


def install_runtime_hooks() -> None:
    _install_stable_art()
    _install_full_curse_stats()
    _install_spawn_guard()
    _install_battle_failure_guard()


def _encounter_snapshot() -> dict:
    """Read encounter state without exposing a mutable reference."""
    try:
        with horror_state._LOCK:  # intentional: same module-level lock as save_encounter
            return dict(horror_state._read().get("encounters", {}))
    except Exception:  # noqa: BLE001
        log.exception("[horror] could not read recovery snapshot")
        return {}


async def _recover_pending(bot) -> None:
    """Restore target buttons after a restart and fail orphaned live battles safely."""
    await bot.wait_until_ready()
    cog = bot.get_cog("Horror Story")
    if cog is None:
        return

    now = time.time()
    for raw_uid, row in _encounter_snapshot().items():
        try:
            uid = int(raw_uid)
        except (TypeError, ValueError):
            continue

        status = row.get("status")

        # A BattleSession cannot survive a process restart. Mark it retryable;
        # most importantly, never run the inventory-claim path for this case.
        if status in {"battle_requested", "battle_running"}:
            horror_state.save_encounter(
                uid,
                status="battle_failed",
                battle_started=False,
                bey_claimed=False,
            )
            continue

        if status not in {"spawned", "declined_once"}:
            continue

        channel = bot.get_channel(int(row.get("channel_id") or 0))
        if channel is None:
            continue

        try:
            message = await channel.fetch_message(int(row.get("message_id") or 0))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            continue

        elapsed = max(0.0, now - float(row.get("updated_at") or now))
        remaining = horror_mod.PROMPT_TIMEOUT - elapsed
        if remaining <= 0:
            await cog.apply_unknown_curse(uid, message)
            continue

        view_cls = (
            horror_mod.ForcedBattleView
            if status == "declined_once"
            else horror_mod.HorrorChallengeView
        )
        view = view_cls(cog, uid, timeout=max(1.0, remaining))
        view.message = message
        try:
            await message.edit(view=view)
        except discord.HTTPException:
            pass


def schedule_recovery(bot) -> None:
    old = getattr(bot, "_horror_recovery_task", None)
    if old is not None and not old.done():
        old.cancel()
    bot._horror_recovery_task = asyncio.create_task(_recover_pending(bot))
