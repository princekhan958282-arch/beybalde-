"""
cogs/updates/reports.py — `/bugs` and `/suggest`, from any server.

A player anywhere fills in a modal; the report lands in ONE channel the owner
chose; the owner presses a button; the reporter gets a DM back.

Three decisions worth stating
-----------------------------
**The buttons survive a restart.** A report can sit for days, and a view held
in memory dies with the process. `discord.ui.DynamicItem` matches a custom_id
by regex, so the handler is rebuilt from the id on the button itself —
`beyreport:<report_id>:<action>` — with nothing kept in memory between.

**The reply rides the notification queue** rather than calling `user.send()`
here. That buys the retries, the pacing and the delivery ledger for free, and
means an answer to a reporter with DMs closed is recorded as BLOCKED instead of
vanishing.

**The spam gate reads the table, not a dict.** A cooldown held in memory is
reset by every restart, which makes it a suggestion rather than a limit.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

import discord

from . import prefs as P
from . import service as SV
from . import store as S

log = logging.getLogger("beyblade_bot.updates.reports")

BUG = "BUG"
IDEA = "IDEA"

KIND_LABEL = {BUG: ("🐛", "Bug report", 0xED4245),
              IDEA: ("💡", "Suggestion", 0x3498DB)}

OPEN, ACK, FIXED, WONTFIX, DUPE = "OPEN", "ACK", "FIXED", "WONTFIX", "DUPE"

STATUS_LABEL = {
    OPEN:    ("📥", "Open", 0x95A5A6),
    ACK:     ("👀", "Acknowledged", 0x3498DB),
    FIXED:   ("✅", "Fixed", 0x2ECC71),
    WONTFIX: ("🚫", "Won't fix", 0xE67E22),
    DUPE:    ("🔁", "Duplicate", 0x9B59B6),
}

# What the reporter is told, per status. Written as a sentence to a person who
# may have forgotten they filed anything.
REPLY = {
    ACK:     "Thanks — this has been seen and is on the list.",
    FIXED:   "This has been fixed. Thanks for reporting it.",
    WONTFIX: "This one won't be changed for now — but thank you for raising it.",
    DUPE:    "Someone had already reported this. Thanks for taking the time.",
}

# ── The spam gate ────────────────────────────────────────────────────────────
COOLDOWN_SECONDS = 300.0        # between one report and the next
DAILY_CAP = 10                  # per player, rolling 24h
IMAGE_WINDOW = 60.0             # seconds to post a screenshot


def tune() -> dict:
    out = {"cooldown": COOLDOWN_SECONDS, "cap": DAILY_CAP,
           "image_window": IMAGE_WINDOW}
    try:
        from utils.database import load_config
        cfg = (load_config() or {}).get("reports") or {}
        for key, name in (("cooldown", "cooldown_seconds"),
                          ("cap", "daily_cap"),
                          ("image_window", "image_window_seconds")):
            if name in cfg:
                out[key] = type(out[key])(cfg[name])
    except Exception:                                    # noqa: BLE001
        pass
    return out


def may_report(user_id) -> tuple[bool, str]:
    """`(allowed, why_not)` — read from the reports table, never from memory."""
    cfg = tune()
    now = time.time()
    try:
        last = S.last_report_at(user_id)
        if last and (now - last) < cfg["cooldown"]:
            wait = int(cfg["cooldown"] - (now - last))
            return False, (f"⏳ You can send another report in "
                           f"**{wait // 60}m {wait % 60}s**.")
        if S.reports_since(user_id, now - 86400.0) >= cfg["cap"]:
            return False, (f"📪 That's **{cfg['cap']}** reports in a day, "
                           f"which is the limit. Try again tomorrow.")
    except Exception:                                    # noqa: BLE001
        log.exception("[reports] spam gate failed — allowing")
    return True, ""


def build_embed(report: dict) -> discord.Embed:
    kemoji, klabel, _kc = KIND_LABEL.get(report.get("kind", BUG),
                                         KIND_LABEL[BUG])
    semoji, slabel, colour = STATUS_LABEL.get(report.get("status", OPEN),
                                              STATUS_LABEL[OPEN])
    e = discord.Embed(
        title=f"{kemoji} {report.get('summary', '(no summary)')}"[:250],
        description=(report.get("body") or "")[:4000],
        colour=colour,
        timestamp=discord.utils.utcnow())
    e.add_field(name="Status", value=f"{semoji} {slabel}", inline=True)
    e.add_field(name="Kind", value=klabel, inline=True)
    uid = report.get("user_id")
    e.add_field(name="From", value=f"<@{uid}> · `{uid}`", inline=True)
    if report.get("guild_id"):
        e.add_field(name="Server", value=f"`{report['guild_id']}`", inline=True)
    if report.get("image_url"):
        e.set_image(url=report["image_url"])
    if report.get("handled_by"):
        e.add_field(name="Handled by", value=f"<@{report['handled_by']}>",
                    inline=True)
    e.set_footer(text=f"id {report.get('report_id', '?')}")
    return e


# ── The restart-proof status buttons ─────────────────────────────────────────
class ReportButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"beyreport:(?P<rid>[A-Za-z0-9_]+):(?P<act>[a-z]+)"):
    """One status button, rebuilt from its own custom_id after a restart.

    `DynamicItem` is the reason this works at all: discord.py matches the
    custom_id against the template and calls `from_custom_id`, so no view has
    to be alive — or even remembered — for a button pressed three days later
    to do the right thing.
    """

    ACTIONS = {
        "ack":     (ACK, "Acknowledge", discord.ButtonStyle.primary, "👀"),
        "fixed":   (FIXED, "Fixed", discord.ButtonStyle.success, "✅"),
        "wontfix": (WONTFIX, "Won't fix", discord.ButtonStyle.secondary, "🚫"),
        "dupe":    (DUPE, "Duplicate", discord.ButtonStyle.secondary, "🔁"),
    }

    def __init__(self, report_id: str, act: str) -> None:
        status, label, style, emoji = self.ACTIONS[act]
        self.report_id = report_id
        self.act = act
        self.status = status
        super().__init__(discord.ui.Button(
            label=label, style=style, emoji=emoji,
            custom_id=f"beyreport:{report_id}:{act}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["rid"], match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        from cogs.admin import actions as A
        if not A.is_admin(interaction.user):
            return await interaction.response.send_message(
                "Only staff can triage reports.", ephemeral=True)
        await interaction.response.defer()

        report = S.get_report(self.report_id)
        if not report:
            return await interaction.followup.send(
                f"Report `{self.report_id}` is no longer on file.",
                ephemeral=True)

        S.set_report_status(self.report_id, self.status, interaction.user.id)
        report = S.get_report(self.report_id) or report

        try:
            await interaction.message.edit(embed=build_embed(report),
                                           view=view_for(self.report_id))
        except Exception:                                # noqa: BLE001
            log.exception("[reports] could not update the report post")

        # The reply goes through the notification queue, so it inherits the
        # retries and lands in the ledger — including when the reporter's DMs
        # are shut, which a bare user.send() would lose silently.
        note = REPLY.get(self.status)
        if note:
            emoji, label, _c = STATUS_LABEL[self.status]
            try:
                await SV.notify_user(
                    interaction.client, report["user_id"],
                    event="IMPORTANT_NOTICE", priority=P.NORMAL,
                    title=f"{emoji} Your report — {label}",
                    body=f"**{report.get('summary', '')}**\n\n{note}")
            except Exception:                            # noqa: BLE001
                log.exception("[reports] could not queue the reply DM")
        await interaction.followup.send(
            f"Marked **{STATUS_LABEL[self.status][1]}**, and the reporter has "
            f"been told.", ephemeral=True)


def view_for(report_id: str) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for act in ("ack", "fixed", "wontfix", "dupe"):
        v.add_item(ReportButton(report_id, act))
    return v


# ── The modal ────────────────────────────────────────────────────────────────
class ReportModal(discord.ui.Modal):
    def __init__(self, kind: str) -> None:
        emoji, label, _c = KIND_LABEL[kind]
        super().__init__(title=f"{emoji} {label}"[:45], timeout=600)
        self.kind = kind
        self.summary = discord.ui.TextInput(
            label="In one line, what is it?",
            placeholder=("Story Mode battle 4 is unwinnable" if kind == BUG
                         else "Let me sort my inventory by rarity"),
            max_length=140, required=True)
        self.body = discord.ui.TextInput(
            label=("What happened, and what did you expect?" if kind == BUG
                   else "How would it work?"),
            style=discord.TextStyle.paragraph,
            max_length=1800, required=True)
        self.add_item(self.summary)
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await submit_report(
            interaction, kind=self.kind,
            summary=str(self.summary.value).strip(),
            body=str(self.body.value).strip())


async def submit_report(interaction: discord.Interaction, *, kind: str,
                        summary: str, body: str) -> None:
    """Store it, post it, then offer the image window. Never raises."""
    from utils.database import get_report_channel

    ok, why = may_report(interaction.user.id)
    if not ok:
        return await interaction.response.send_message(why, ephemeral=True)

    report = {
        "report_id": S.new_id("rep"),
        "kind": kind,
        "user_id": str(interaction.user.id),
        "guild_id": (str(interaction.guild_id) if interaction.guild_id
                     else None),
        "summary": summary[:250],
        "body": body[:4000],
        "image_url": None,
        "status": OPEN,
        "created_at": time.time(),
        "handled_by": None,
        "handled_at": None,
        "message_id": None,
    }
    S.put_report(report)

    cid = get_report_channel()
    channel = interaction.client.get_channel(cid) if cid else None
    if channel is None:
        # Filed but undeliverable. The report is NOT lost — it is in the table
        # and `/admin → Open reports` will show it — so the player is thanked
        # rather than told about a configuration problem that is not theirs.
        log.warning("[reports] no report channel set; %s stored only",
                    report["report_id"])
    else:
        try:
            msg = await channel.send(embed=build_embed(report),
                                     view=view_for(report["report_id"]))
            S.set_report_field(report["report_id"], "message_id", str(msg.id))
        except Exception:                                # noqa: BLE001
            log.exception("[reports] could not post to the report channel")

    cfg = tune()
    await interaction.response.send_message(
        f"✅ Thanks — filed as `{report['report_id']}`.\n"
        f"📎 Want to add a screenshot? Post **one image here in the next "
        f"{int(cfg['image_window'])} seconds** and I'll attach it.",
        ephemeral=True)
    interaction.client.loop.create_task(
        _await_image(interaction, report["report_id"], cfg["image_window"]))


async def _await_image(interaction: discord.Interaction, report_id: str,
                       window: float) -> None:
    """Wait for one image from this player in this channel. Never raises.

    A modal cannot take a file upload — Discord has no such component — so the
    only way to get a screenshot without making the player host it somewhere
    first is to watch the channel briefly afterwards.
    """
    bot = interaction.client

    def check(m: discord.Message) -> bool:
        return (m.author.id == interaction.user.id
                and m.channel.id == getattr(interaction.channel, "id", None)
                and bool(m.attachments)
                and str(m.attachments[0].content_type or "")
                .startswith("image/"))

    try:
        msg = await bot.wait_for("message", check=check, timeout=window)
    except asyncio.TimeoutError:
        return                       # no screenshot; the report stands as-is
    except Exception:                                    # noqa: BLE001
        log.exception("[reports] image window failed")
        return

    try:
        url = msg.attachments[0].url
        S.set_report_field(report_id, "image_url", url)
        report = S.get_report(report_id)
        if report and report.get("message_id"):
            from utils.database import get_report_channel
            ch = bot.get_channel(get_report_channel() or 0)
            if ch:
                m = await ch.fetch_message(int(report["message_id"]))
                await m.edit(embed=build_embed(report),
                             view=view_for(report_id))
        await msg.add_reaction("📎")
    except Exception:                                    # noqa: BLE001
        log.exception("[reports] could not attach the screenshot")


_TEMPLATE = re.compile(r"beyreport:(?P<rid>[A-Za-z0-9_]+):(?P<act>[a-z]+)")


def parse_custom_id(custom_id: str) -> Optional[dict]:
    """What discord.py will do on a restart, exposed so it can be tested."""
    m = _TEMPLATE.fullmatch(custom_id or "")
    return {"rid": m["rid"], "act": m["act"]} if m else None
