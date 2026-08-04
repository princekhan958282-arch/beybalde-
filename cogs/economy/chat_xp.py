"""
chat_xp.py — EXP for talking.

Two separate things were both missing:

  * Trainer XP.  `grant_xp()` existed and battles called it, but NOTHING ever
    called it for chatting, so a player's profile XP never moved unless they
    battled. That's the "profile exp not coming for chatting" report — not a
    bug in the XP maths, a listener that was never written.

  * Bey XP.  Per-bey levels need a steady trickle that isn't gated behind
    finding an opponent, so the equipped bey earns alongside its trainer.

EVERY qualifying message is paid — there is no cooldown. That is deliberate
per request, but it does mean chat XP scales with message count, so the cheap
filters below (bots, commands, very short messages) are the only thing standing
between the level curve and a spam loop. Set XP_CHAT_COOLDOWN_S and restore the
timestamp check if that turns out to matter.
"""

from __future__ import annotations

import logging
import random

import discord
from discord.ext import commands

from utils import bey_levels as BL
from utils.database import get_user, update_user, grant_xp

log = logging.getLogger("beyblade_bot.chatxp")

TRAINER_XP = BL.XP_CHAT       # trainer and bey move together
MIN_LENGTH = 3                # "k" and "lol" shouldn't farm XP


class ChatXPCog(commands.Cog, name="Chat XP"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        content = (message.content or "").strip()
        if len(content) < MIN_LENGTH:
            return
        # Commands are their own reward path; paying for them too would make
        # spamming `;bal` the fastest way to level.
        prefix = getattr(self.bot, "command_prefix", ";")
        if isinstance(prefix, str) and content.startswith(prefix):
            return

        uid = message.author.id
        try:
            await self._award(message, uid)
        except Exception as e:                       # noqa: BLE001
            # A chat listener runs on every message in every server — it must
            # never be able to take the bot down.
            log.warning("chat XP failed for %s: %s", uid, e)

    async def _award(self, message: discord.Message, uid: int) -> None:
        # Trainer XP — the half that was missing entirely.
        grant_xp(uid, random.randint(*TRAINER_XP))

        profile = get_user(uid)
        blade = profile.get("active_beyblade")
        if not blade:
            return
        # Boss copies are fixed rolls and don't level.
        if profile.get("active_copy"):
            return

        result = BL.award(profile, blade, random.randint(*BL.XP_CHAT))
        update_user(uid, profile)

        if result["milestone"]:
            await self._announce(message.channel, message.author, blade, result)

    async def _announce(self, channel, member, blade_name: str,
                        result: dict) -> None:
        from utils.database import get_beyblade
        bd = get_beyblade(blade_name) or {"stats": {}, "type": ""}
        gains = BL.gains_at(bd, result["level"], result["ivs"])
        # What the last five levels added, not just the last one — the ping
        # only fires every fifth level, so per-level gains would understate it.
        span = {}
        for s in gains:
            span[s] = (BL.stats_at(bd, result["level"], result["ivs"]).get(s, 0)
                       - BL.stats_at(bd, max(1, result["old_level"]),
                                     result["ivs"]).get(s, 0))

        e = discord.Embed(
            title=f"⭐ {blade_name} reached Level {result['level']}!",
            colour=0xFEE75C,
            description=" · ".join(f"**{k.upper()}** +{v}"
                                   for k, v in span.items() if v > 0)
                        or "Stats are at their cap.")
        e.set_footer(text=f"{member.display_name}  •  next milestone at "
                          f"Lv{(result['level'] // BL.NOTIFY_EVERY + 1) * BL.NOTIFY_EVERY}")
        try:
            await channel.send(embed=e)
        except Exception as e2:                      # noqa: BLE001
            log.debug("couldn't post level-up: %s", e2)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChatXPCog(bot))
