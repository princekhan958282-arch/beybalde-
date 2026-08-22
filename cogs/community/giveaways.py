"""
cogs/community/giveaways.py — `GiveawayManager` and the entry button.

Same two properties as polls, plus one of its own:

**Every winner ever drawn is written to the row.** `winner_ids` accumulates, so
a reroll excludes the people who already won — and still excludes them after a
restart, because the exclusion list is in the database rather than in the memory
of the process that drew them.

Draws use `random.SystemRandom`, not the shared `random` module. A giveaway is
the one place in this bot where "could someone predict this?" is a fair
question, and the module-level generator is seeded and shared with the battle
engine.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Optional

import discord

from . import store as S
from .guard import require_main

log = logging.getLogger("beyblade_bot.community.giveaways")

OPEN, CLOSING, ENDED, CANCELLED = "OPEN", "CLOSING", "ENDED", "CANCELLED"

MAX_PRIZE = 240
MAX_WINNERS = 20
COLOUR = 0xF1C40F

_rng = random.SystemRandom()


class GiveawayError(RuntimeError):
    """Something the host did wrong — the message is for them to read."""


class GiveawayManager:
    def __init__(self, bot=None) -> None:
        self.bot = bot

    def create(self, guild_id: Any, host_id: Any, prize: str, *,
               duration: Optional[float] = None, winners: int = 1,
               requirement: Optional[dict] = None,
               now: Optional[float] = None) -> dict:
        require_main(guild_id)
        prize = (prize or "").strip()[:MAX_PRIZE]
        if not prize:
            raise GiveawayError("What's the prize?")
        winners = max(1, min(MAX_WINNERS, int(winners or 1)))
        ts = time.time() if now is None else float(now)
        row = {
            "giveaway_id": S.new_id("give"),
            "guild_id": str(guild_id),
            "channel_id": None,
            "message_id": None,
            "host_id": str(host_id),
            "prize": prize,
            "winners": winners,
            "requirement": requirement or {},
            "ends_at": (ts + float(duration)) if duration else None,
            "state": OPEN,
            "created_at": ts,
            "ended_at": None,
            "winner_ids": [],
        }
        S.put_giveaway(row)
        return row

    def get(self, guild_id: Any, giveaway_id: str) -> Optional[dict]:
        require_main(guild_id)
        row = S.get_giveaway(giveaway_id)
        if row and str(row.get("guild_id")) != str(require_main(guild_id)):
            return None
        return row

    def recent(self, guild_id: Any, limit: int = 10) -> list[dict]:
        require_main(guild_id)
        return S.list_giveaways(guild_id, limit)

    def enter(self, guild_id: Any, giveaway_id: str, user_id: Any,
              now: Optional[float] = None) -> dict:
        """One entry per player, enforced by the composite primary key."""
        require_main(guild_id)
        row = S.get_giveaway(giveaway_id)
        if not row:
            return {"ok": False, "why": "That giveaway is gone."}
        if row.get("state") != OPEN:
            return {"ok": False, "why": "That giveaway has ended."}
        fresh = S.enter(giveaway_id, user_id, now)
        count = S.entry_count(giveaway_id)
        if not fresh:
            return {"ok": False, "already": True, "count": count,
                    "why": "You're already in — good luck."}
        return {"ok": True, "count": count}

    def entries(self, guild_id: Any, giveaway_id: str) -> list[str]:
        require_main(guild_id)
        return S.entries(giveaway_id)

    def draw(self, guild_id: Any, giveaway_id: str, *, count: int = 0,
             exclude_previous: bool = True) -> list[str]:
        """Pick winners without re-picking anyone who has already won."""
        require_main(guild_id)
        row = S.get_giveaway(giveaway_id)
        if not row:
            return []
        pool = list(S.entries(giveaway_id))
        if exclude_previous:
            already = {str(u) for u in (row.get("winner_ids") or [])}
            pool = [u for u in pool if u not in already]
        want = int(count or row.get("winners") or 1)
        if not pool:
            return []
        return _rng.sample(pool, min(want, len(pool)))

    def end(self, guild_id: Any, giveaway_id: str,
            now: Optional[float] = None) -> dict:
        """Draw, record, close. Idempotent for an already-ended giveaway."""
        require_main(guild_id)
        row = S.get_giveaway(giveaway_id)
        if not row:
            return {"ok": False, "why": "That giveaway is gone."}
        if row.get("state") in (ENDED, CANCELLED):
            return {"ok": False, "why": "That giveaway already ended.",
                    "winners": row.get("winner_ids") or [], "row": row}
        winners = self.draw(guild_id, giveaway_id)
        history = list(row.get("winner_ids") or []) + winners
        S.set_giveaway_state(
            giveaway_id, ENDED, winner_ids=history,
            ended_at=time.time() if now is None else float(now))
        return {"ok": True, "winners": winners,
                "entries": S.entry_count(giveaway_id),
                "row": S.get_giveaway(giveaway_id)}

    def reroll(self, guild_id: Any, giveaway_id: str,
               count: int = 1) -> dict:
        """Draw replacements. Anyone already drawn stays excluded, forever."""
        require_main(guild_id)
        row = S.get_giveaway(giveaway_id)
        if not row:
            return {"ok": False, "why": "That giveaway is gone."}
        if row.get("state") not in (ENDED,):
            return {"ok": False, "why": "End it first, then reroll."}
        winners = self.draw(guild_id, giveaway_id, count=count)
        if not winners:
            return {"ok": False,
                    "why": "Nobody left to draw — everyone eligible has won."}
        history = list(row.get("winner_ids") or []) + winners
        S.set_giveaway_state(giveaway_id, ENDED, winner_ids=history)
        return {"ok": True, "winners": winners}

    def cancel(self, guild_id: Any, giveaway_id: str) -> bool:
        require_main(guild_id)
        row = S.get_giveaway(giveaway_id)
        if not row or row.get("state") in (ENDED, CANCELLED):
            return False
        S.set_giveaway_state(giveaway_id, CANCELLED, ended_at=time.time())
        return True

    def attach(self, guild_id: Any, giveaway_id: str, channel_id,
               message_id) -> None:
        require_main(guild_id)
        S.set_giveaway_message(giveaway_id, channel_id, message_id)

    def due(self, now: Optional[float] = None, limit: int = 20) -> list[str]:
        return S.claim_due_giveaways(now, limit)

    def recover(self) -> int:
        return S.recover_giveaways()


# ── Rendering ────────────────────────────────────────────────────────────────
def build_embed(row: dict, entries: int = 0,
                winners: Optional[list] = None) -> discord.Embed:
    ended = row.get("state") in (ENDED, CANCELLED)
    e = discord.Embed(
        title=f"🎉  {row.get('prize', '?')}"[:250],
        colour=0x99AAB5 if ended else COLOUR)
    lines = []
    if row.get("state") == CANCELLED:
        lines.append("**Cancelled.**")
    elif winners:
        lines.append("**Winner(s):** " + ", ".join(f"<@{w}>" for w in winners))
    elif ended:
        lines.append("**Nobody entered.**")
    else:
        ends = row.get("ends_at")
        lines.append(f"Ends <t:{int(ends)}:R>" if ends
                     else "Ends when the host says so.")
        lines.append(f"Press **Enter** below. "
                     f"{int(row.get('winners') or 1)} winner(s).")
    lines.append(f"\n{entries} entr{'y' if entries == 1 else 'ies'}")
    e.description = "\n".join(lines)
    e.set_footer(text=f"hosted by {row.get('host_id', '?')} · "
                      f"{row.get('giveaway_id', '')}")
    return e


# ── The restart-proof entry button ───────────────────────────────────────────
_TEMPLATE = re.compile(r"beygive:(?P<gid>[A-Za-z0-9_]+):(?P<act>[a-z]+)")


def parse_custom_id(custom_id: str) -> Optional[dict]:
    m = _TEMPLATE.fullmatch(custom_id or "")
    return {"gid": m["gid"], "act": m["act"]} if m else None


class GiveawayEnterButton(discord.ui.DynamicItem[discord.ui.Button],
                          template=r"beygive:(?P<gid>[A-Za-z0-9_]+):(?P<act>[a-z]+)"):

    def __init__(self, giveaway_id: str, act: str = "enter") -> None:
        self.giveaway_id = giveaway_id
        self.act = act
        super().__init__(discord.ui.Button(
            label="Enter", emoji="🎉",
            style=discord.ButtonStyle.success,
            custom_id=f"beygive:{giveaway_id}:{act}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["gid"], match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Community")
        if cog is None:
            return await interaction.response.send_message(
                "Giveaways are offline right now.", ephemeral=True)
        await cog.handle_entry(interaction, self.giveaway_id)


def view_for(giveaway_id: str) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(GiveawayEnterButton(giveaway_id))
    return v
