"""
cogs/community/polls.py — `PollManager` and the vote buttons.

Two properties the whole design turns on:

**One vote per player is the DATABASE's job.** `community_poll_votes` has
`PRIMARY KEY (poll_id, user_id)` and the insert is `INSERT OR IGNORE`, so a
double-click, a lagging client and a second process all land on the same row.
A set held in memory would forget every vote on restart.

**The buttons outlive the process.** A poll can run for a week. `DynamicItem`
rebuilds the handler from the button's own `custom_id`, so a vote pressed after
a deploy still counts — see `cogs/updates/reports.py`, which introduced the
pattern here.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

import discord

from . import store as S
from .guard import require_main

log = logging.getLogger("beyblade_bot.community.polls")

OPEN, CLOSING, CLOSED, CANCELLED = "OPEN", "CLOSING", "CLOSED", "CANCELLED"

MAX_OPTIONS = 10          # Discord allows 25 components; 10 keeps it readable
MIN_OPTIONS = 2
MAX_QUESTION = 240

NUMBERS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
           "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")

COLOUR = 0x5865F2
BAR_WIDTH = 12


class PollError(RuntimeError):
    """Something the player did wrong — the message is for them to read."""


class PollManager:
    """Every method takes `guild_id` first and calls `require_main`."""

    def __init__(self, bot=None) -> None:
        self.bot = bot

    def create(self, guild_id: Any, author_id: Any, question: str,
               options: list, *, duration: Optional[float] = None,
               multi: bool = False, anonymous: bool = True,
               now: Optional[float] = None) -> dict:
        require_main(guild_id)
        question = (question or "").strip()[:MAX_QUESTION]
        clean = [str(o).strip()[:80] for o in options if str(o).strip()]
        if not question:
            raise PollError("A poll needs a question.")
        if len(clean) < MIN_OPTIONS:
            raise PollError(f"Give it at least {MIN_OPTIONS} choices.")
        if len(clean) > MAX_OPTIONS:
            raise PollError(f"{MAX_OPTIONS} choices is the limit.")
        ts = time.time() if now is None else float(now)
        row = {
            "poll_id": S.new_id("poll"),
            "guild_id": str(guild_id),
            "channel_id": None,
            "message_id": None,
            "author_id": str(author_id),
            "question": question,
            "options": clean,
            "multi": 1 if multi else 0,
            "anonymous": 1 if anonymous else 0,
            "ends_at": (ts + float(duration)) if duration else None,
            "state": OPEN,
            "created_at": ts,
            "closed_at": None,
            "results": None,
        }
        S.put_poll(row)
        return row

    def _own(self, guild_id: Any, poll_id: str) -> Optional[dict]:
        """The row, but only if it belongs to the guild asking for it.

        `require_main` proves the CALLER is the main server; this proves the
        ROW is. Repointing the main server made those different questions, and
        without this the loop would close and announce the old guild's polls.
        """
        main = require_main(guild_id)
        poll = S.get_poll(poll_id)
        if poll is None or str(poll.get("guild_id")) != str(main):
            return None
        return poll

    def get(self, guild_id: Any, poll_id: str) -> Optional[dict]:
        return self._own(guild_id, poll_id)

    def recent(self, guild_id: Any, limit: int = 10) -> list[dict]:
        require_main(guild_id)
        return S.list_polls(guild_id, limit)

    def vote(self, guild_id: Any, poll_id: str, user_id: Any,
             choice: int, now: Optional[float] = None) -> dict:
        """Record one vote. `{"ok", "why", "choice"}` — never raises."""
        poll = self._own(guild_id, poll_id)
        if not poll:
            return {"ok": False, "why": "That poll is gone."}
        if poll.get("state") != OPEN:
            return {"ok": False, "why": "That poll is closed."}
        options = poll.get("options") or []
        try:
            index = int(choice)
        except (TypeError, ValueError):
            return {"ok": False, "why": "That isn't one of the choices."}
        if not 0 <= index < len(options):
            return {"ok": False, "why": "That isn't one of the choices."}
        fresh = S.vote(poll_id, user_id, str(index), now)
        if not fresh:
            already = S.vote_of(poll_id, user_id)
            label = options[int(already)] if (already or "").isdigit() else "?"
            return {"ok": False, "already": True,
                    "why": f"You already voted for **{label}**."}
        return {"ok": True, "choice": index, "label": options[index]}

    def results(self, guild_id: Any, poll_id: str) -> dict:
        poll = self._own(guild_id, poll_id) or {}
        options = poll.get("options") or []
        tally = S.tally(poll_id)
        counts = [int(tally.get(str(i), 0)) for i in range(len(options))]
        return {"options": options, "counts": counts, "total": sum(counts)}

    def close(self, guild_id: Any, poll_id: str,
              now: Optional[float] = None) -> Optional[dict]:
        """Finalise and freeze the tally. Idempotent — safe to call twice."""
        poll = self._own(guild_id, poll_id)
        if not poll or poll.get("state") in (CLOSED, CANCELLED):
            return poll
        res = self.results(guild_id, poll_id)
        stored = {str(i): n for i, n in enumerate(res["counts"])}
        S.set_poll_state(poll_id, CLOSED, results=stored,
                         closed_at=time.time() if now is None else float(now))
        return S.get_poll(poll_id)

    def cancel(self, guild_id: Any, poll_id: str) -> bool:
        poll = self._own(guild_id, poll_id)
        if not poll or poll.get("state") in (CLOSED, CANCELLED):
            return False
        S.set_poll_state(poll_id, CANCELLED, closed_at=time.time())
        return True

    def attach(self, guild_id: Any, poll_id: str, channel_id,
               message_id) -> None:
        require_main(guild_id)
        S.set_poll_message(poll_id, channel_id, message_id)

    def due(self, now: Optional[float] = None, limit: int = 20) -> list[str]:
        """Claim polls whose timer has run out. Loop-facing, no guild arg."""
        return S.claim_due_polls(now, limit)

    def recover(self) -> int:
        """Put anything a dead process left mid-close back on the queue."""
        return S.recover_polls()


# ── Rendering ────────────────────────────────────────────────────────────────
def _bar(count: int, total: int) -> str:
    filled = 0 if not total else round(BAR_WIDTH * count / total)
    return "▰" * filled + "▱" * (BAR_WIDTH - filled)


def build_embed(poll: dict, results: Optional[dict] = None,
                voice=None) -> discord.Embed:
    options = poll.get("options") or []
    closed = poll.get("state") in (CLOSED, CANCELLED)
    e = discord.Embed(
        title=f"📊  {poll.get('question', '?')}"[:250],
        colour=0x99AAB5 if closed else COLOUR)
    if results and (closed or not poll.get("anonymous", 1)):
        total = results.get("total", 0) or 0
        lines = []
        best = max(results.get("counts") or [0])
        for i, label in enumerate(options):
            n = results["counts"][i] if i < len(results["counts"]) else 0
            share = 0 if not total else round(100 * n / total)
            crown = " 👑" if (closed and n == best and n > 0) else ""
            lines.append(f"{NUMBERS[i]} **{label}**{crown}\n"
                         f"`{_bar(n, total)}` {n} · {share}%")
        e.description = "\n".join(lines)
        e.set_footer(text=f"{total} vote(s) · {poll.get('poll_id', '')}")
    else:
        e.description = "\n".join(f"{NUMBERS[i]} {label}"
                                  for i, label in enumerate(options))
        ends = poll.get("ends_at")
        when = (f"\nCloses <t:{int(ends)}:R>" if ends else
                "\nOpen until it's closed by hand.")
        e.description += when
        e.set_footer(text=f"Votes stay hidden until it closes · "
                          f"{poll.get('poll_id', '')}")
    return e


# ── The restart-proof vote buttons ───────────────────────────────────────────
_TEMPLATE = re.compile(r"beypoll:(?P<pid>[A-Za-z0-9_]+):(?P<idx>\d+)")


def parse_custom_id(custom_id: str) -> Optional[dict]:
    """What discord.py will do after a restart, exposed so it can be tested."""
    m = _TEMPLATE.fullmatch(custom_id or "")
    return {"pid": m["pid"], "idx": int(m["idx"])} if m else None


class PollVoteButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"beypoll:(?P<pid>[A-Za-z0-9_]+):(?P<idx>\d+)"):
    """One choice. Everything it needs is in its own custom_id."""

    def __init__(self, poll_id: str, index: int, label: str = "") -> None:
        self.poll_id = poll_id
        self.index = int(index)
        super().__init__(discord.ui.Button(
            label=(label or f"Option {index + 1}")[:80],
            emoji=NUMBERS[self.index] if self.index < len(NUMBERS) else None,
            style=discord.ButtonStyle.secondary,
            custom_id=f"beypoll:{poll_id}:{index}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["pid"], int(match["idx"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Community")
        if cog is None:
            return await interaction.response.send_message(
                "Polls are offline right now.", ephemeral=True)
        await cog.handle_vote(interaction, self.poll_id, self.index)


def view_for(poll: dict) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for i, label in enumerate(poll.get("options") or []):
        v.add_item(PollVoteButton(poll["poll_id"], i, label))
    return v
