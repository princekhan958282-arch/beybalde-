"""
views.py — every interaction in the tournament system, button-driven.

Design rule: a player never types. The only text entry anywhere is the
announcement body, which is genuinely free-form and belongs in a modal.

Why the join flow is an ephemeral MESSAGE and not a modal
---------------------------------------------------------
A modal looks like the right tool — it is a centered overlay — but it submits
all at once and cannot carry custom Confirm / Cancel buttons; Discord renders
its own Submit. The brief asks for a dropdown, availability buttons the user
can toggle and see, and an explicit Confirm. That is an ephemeral message with
components: it supports live interaction, it works on every discord.py 2.x, and
it is what Discord actually shows as "Only you can see this".
"""

from __future__ import annotations

import logging
from typing import Optional

import discord

from . import notifications as notify, ui_v2
from .models import TournamentState

log = logging.getLogger("beyblade_bot.tournament")

AVAILABILITY = {
    "evening": ("🌆 Evening", "18:00 – 22:00 your time",
                [{"day": d, "start": "18:00", "end": "22:00"} for d in
                 ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")]),
    "night":   ("🌙 Night", "22:00 – 23:59 your time",
                [{"day": d, "start": "22:00", "end": "23:59"} for d in
                 ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")]),
    "flexible": ("✨ Flexible", "Any time — best odds of a match",
                 [{"day": d, "start": "09:00", "end": "23:59"} for d in
                  ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")]),
}

# Offsets people actually use, kept under the 25-option select cap.
REGIONS = [
    ("🇮🇳 India", "5.5"), ("🇬🇧 United Kingdom", "0"),
    ("🇺🇸 US East", "-5"), ("🇺🇸 US West", "-8"),
    ("🇩🇪 Germany", "1"), ("🇧🇷 Brazil", "-3"),
    ("🇦🇪 UAE", "4"), ("🇸🇬 Singapore", "8"),
    ("🇯🇵 Japan", "9"), ("🇦🇺 Australia East", "10"),
]


def _locale_guess(interaction: discord.Interaction) -> str:
    """Best guess at the player's offset from their Discord locale.

    A guess, shown as pre-selected and fully overridable — never silently
    applied. Getting someone's timezone wrong and scheduling around it is worse
    than asking.
    """
    loc = str(getattr(interaction, "locale", "") or "").lower()
    table = {"hi": "5.5", "en-gb": "0", "de": "1", "pt-br": "-3",
             "ja": "9", "ko": "9", "zh-cn": "8", "zh-tw": "8",
             "es-es": "1", "fr": "1", "it": "1", "ru": "3", "tr": "3"}
    for key, off in table.items():
        if loc.startswith(key):
            return off
    return "0"


# ── Tournament card ───────────────────────────────────────────────────────────

def _card_view_base():
    """LayoutView on 2.6+, plain View below it. Chosen once at import."""
    return getattr(discord.ui, "LayoutView", discord.ui.View)


class TournamentCard(_card_view_base()):  # type: ignore[misc]
    """The announcement card. Buttons only — no commands.

    `timeout=None` plus a stable custom_id makes this ELIGIBLE to survive a
    restart, but eligibility is not enough: discord.py only routes a component
    interaction to a view the bot has registered with `add_view()`. Without
    that call the message keeps its buttons and every click fails, which is
    fatal here — a card sits in a channel for hours or days before its
    tournament starts, and this bot gets redeployed from a phone.

    `TournamentCog.cog_load` registers one of these per live tournament for
    exactly that reason.
    """

    def __init__(self, cog, tournament_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.tid = tournament_id
        self._build()

    # ---- construction ----
    def _build(self):
        self.clear_items()
        t = self.cog.svc.store.get_tournament(self.tid)
        joined = len(t.entrants) if t else 0

        row = discord.ui.ActionRow() if ui_v2.HAS_V2 else None

        join = discord.ui.Button(
            label="Join Tournament", emoji="⚔️",
            style=discord.ButtonStyle.success,
            custom_id=f"tourney:join:{self.tid}",
            disabled=bool(t and (t.is_full()
                                 or t.state != TournamentState.SIGNUP.value)))
        join.callback = self.on_join

        rules = discord.ui.Button(
            label="View Rules", emoji="📜",
            style=discord.ButtonStyle.secondary,
            custom_id=f"tourney:rules:{self.tid}")
        rules.callback = self.on_rules

        leave = discord.ui.Button(
            label="Leave", style=discord.ButtonStyle.danger,
            custom_id=f"tourney:leave:{self.tid}")
        leave.callback = self.on_leave

        if ui_v2.HAS_V2:
            if t:
                ui_v2.attach_card_v2(self, t, joined)
            for b in (join, rules, leave):
                row.add_item(b)
            self.add_item(row)
        else:
            for b in (join, rules, leave):
                self.add_item(b)

    async def refresh(self, interaction: discord.Interaction):
        """Redraw the card in place after the entrant count changes."""
        t = self.cog.svc.store.get_tournament(self.tid)
        if not t:
            return
        fresh = TournamentCard(self.cog, self.tid)
        try:
            await interaction.message.edit(
                **ui_v2.card_kwargs(t, len(t.entrants), fresh))
        except Exception as e:                       # noqa: BLE001
            # The card is cosmetic; a failed redraw must never break the join
            # the player just completed.
            log.debug("card refresh failed: %s", e)

    # ---- callbacks ----
    async def on_join(self, interaction: discord.Interaction):
        t = self.cog.svc.store.get_tournament(self.tid)
        if not t or t.state != TournamentState.SIGNUP.value:
            return await interaction.response.send_message(
                "Signups are closed.", ephemeral=True)
        if interaction.user.id in t.entrants:
            return await interaction.response.send_message(
                "You're already in this tournament.", ephemeral=True)
        await interaction.response.send_message(
            embed=JoinFlow.intro_embed(t),
            view=JoinFlow(self.cog, self.tid, self,
                          _locale_guess(interaction)),
            ephemeral=True)

    async def on_rules(self, interaction: discord.Interaction):
        e = discord.Embed(
            title="📜 Tournament rules", colour=ui_v2.BLURPLE,
            description="Everything below is enforced by the bot — you never "
                        "have to track it yourself.")
        e.add_field(name="Check-in opens", value="10 min before", inline=True)
        e.add_field(name="Check-in window", value="10 min", inline=True)
        e.add_field(name="Miss check-in", value="Forfeit", inline=True)
        e.add_field(name="Both miss", value="Rescheduled once", inline=True)
        e.add_field(name="3 no-shows", value="7-day ban", inline=True)
        e.add_field(name="Scheduling", value="Automatic, from your availability",
                    inline=True)
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def on_leave(self, interaction: discord.Interaction):
        ok, msg, _ = self.cog.svc.leave(self.tid, interaction.user.id)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + msg,
                                                ephemeral=True)
        if ok:
            await self.refresh(interaction)


# ── Ephemeral join flow ───────────────────────────────────────────────────────

class JoinFlow(discord.ui.View):
    """Region + availability + Confirm, all in one ephemeral message."""

    def __init__(self, cog, tournament_id: str, card: Optional[TournamentCard],
                 default_offset: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.tid = tournament_id
        self.card = card
        self.offset = default_offset
        self.avail = "flexible"
        self._build()

    @staticmethod
    def intro_embed(t) -> discord.Embed:
        return discord.Embed(
            title="Confirm tournament entry",
            colour=ui_v2.BLURPLE,
            description=(f"You're about to join **{t.name}**. Your region is "
                         f"detected from your Discord language — change it "
                         f"below if it's wrong."))

    def _build(self):
        self.clear_items()

        region = discord.ui.Select(
            placeholder="🌍 Region",
            options=[discord.SelectOption(label=name, value=off,
                                          description=f"UTC{float(off):+g}",
                                          default=(off == self.offset))
                     for name, off in REGIONS],
            row=0)
        region.callback = self._on_region
        self.add_item(region)

        avail = discord.ui.Select(
            placeholder="⏰ When can you play?",
            options=[discord.SelectOption(label=lab, value=key, description=desc,
                                          default=(key == self.avail))
                     for key, (lab, desc, _) in AVAILABILITY.items()],
            row=1)
        avail.callback = self._on_avail
        self.add_item(avail)

        confirm = discord.ui.Button(label="Confirm join", emoji="✅",
                                    style=discord.ButtonStyle.success, row=2)
        confirm.callback = self._on_confirm
        self.add_item(confirm)

        cancel = discord.ui.Button(label="Cancel",
                                   style=discord.ButtonStyle.secondary, row=2)
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_region(self, interaction: discord.Interaction):
        self.offset = interaction.data["values"][0]
        self._build()
        await interaction.response.edit_message(view=self)

    async def _on_avail(self, interaction: discord.Interaction):
        self.avail = interaction.data["values"][0]
        self._build()
        await interaction.response.edit_message(view=self)

    async def _on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Cancelled — you haven't joined.", embed=None, view=None)

    async def _on_confirm(self, interaction: discord.Interaction):
        label, _desc, slots = AVAILABILITY[self.avail]

        ok, msg, _p = self.cog.svc.register(
            interaction.user.id, "", self.offset, slots)
        if not ok:
            return await interaction.response.edit_message(
                content=f"❌ {msg}", embed=None, view=None)

        ok, msg, t = self.cog.svc.join(self.tid, interaction.user.id)
        if not ok:
            return await interaction.response.edit_message(
                content=f"❌ {msg}", embed=None, view=None)

        e = discord.Embed(
            title="✅ You're in the bracket", colour=ui_v2.GREEN,
            description="Match schedule will be sent via DM.")
        e.add_field(name="Seed", value=f"#{len(t.entrants)}", inline=True)
        e.add_field(name="Availability", value=label, inline=True)
        e.add_field(name="Region",
                    value=f"UTC{float(self.offset):+g}", inline=True)
        if t.start_time:
            e.add_field(name="Starts", value=f"<t:{int(t.start_time)}:F>",
                        inline=False)
        await interaction.response.edit_message(content=None, embed=e, view=None)

        if self.card:
            await self.card.refresh(interaction)


# ── RSVP ──────────────────────────────────────────────────────────────────────
# Attached to the DM itself — the answer to "how do I know how many are
# coming" is a button the recipient taps, not a second message they have to
# send back. Made persistent the same way TournamentCard is: cog_load
# enumerates recent DM announcements and registers one of these per id, rather
# than relying on a single bare instance (a bare custom_id like "tourney:rsvp"
# would never match the id-specific one actually sent — see CheckinView's note
# in cog.py for why that shortcut doesn't work).

class RSVPView(discord.ui.View):
    """Two buttons on the recipient's own DM: I'll be there / Can't make it."""

    def __init__(self, cog, ann_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.ann_id = ann_id

        yes = discord.ui.Button(label="I'll be there", emoji="✅",
                                style=discord.ButtonStyle.success,
                                custom_id=f"tourney:rsvp:yes:{ann_id}")
        yes.callback = self._on_yes
        no = discord.ui.Button(label="Can't make it", emoji="❌",
                               style=discord.ButtonStyle.secondary,
                               custom_id=f"tourney:rsvp:no:{ann_id}")
        no.callback = self._on_no
        self.add_item(yes)
        self.add_item(no)

    async def _record(self, interaction: discord.Interaction, response: str,
                      note: str):
        self.cog.svc.store.set_rsvp(self.ann_id, interaction.user.id, response)
        for child in self.children:
            child.disabled = True
        # Editing the DM itself, not an ephemeral reply — the recipient's own
        # message is the natural place for "you said yes" to live, and it
        # doubles as their reminder of what they answered.
        await interaction.response.edit_message(content=note, view=self)

    async def _on_yes(self, interaction: discord.Interaction):
        await self._record(interaction, "yes",
                           "✅ **You're marked as coming.** See you there!")

    async def _on_no(self, interaction: discord.Interaction):
        await self._record(interaction, "no",
                           "❌ **You're marked as not coming.** No worries.")


class RSVPCheckView(discord.ui.View):
    """The organiser's side: one button, safe to tap any time, that shows the
    current tally. Not a live-updating message — Discord has no push for
    that — but re-tappable for as long as the message exists."""

    def __init__(self, cog, ann_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.ann_id = ann_id
        btn = discord.ui.Button(label="Check responses", emoji="📊",
                                style=discord.ButtonStyle.secondary,
                                custom_id=f"tourney:rsvpcheck:{ann_id}")
        btn.callback = self._on_check
        self.add_item(btn)

    async def _on_check(self, interaction: discord.Interaction):
        counts = self.cog.svc.store.rsvp_counts(self.ann_id)
        e = discord.Embed(title="📊 Who's coming", colour=ui_v2.BLURPLE)
        e.add_field(name="✅ Coming", value=str(counts["yes"]), inline=True)
        e.add_field(name="❌ Not coming", value=str(counts["no"]), inline=True)
        e.add_field(name="⏳ No answer yet", value=str(counts["pending"]),
                    inline=True)
        e.set_footer(text=f"{counts['total']} people were messaged · "
                          f"pending includes anyone whose DMs are closed")
        await interaction.response.send_message(embed=e, ephemeral=True)


# ── Announcement ──────────────────────────────────────────────────────────────

class AnnouncementBody(discord.ui.Modal, title="Write your announcement"):
    """The one place typing is genuinely the right input."""

    body = discord.ui.TextInput(
        label="Message", style=discord.TextStyle.paragraph,
        placeholder="Write your announcement…", max_length=1800, required=True)

    def __init__(self, composer: "AnnouncementComposer"):
        super().__init__()
        self.composer = composer
        if composer.body:
            self.body.default = composer.body

    async def on_submit(self, interaction: discord.Interaction):
        self.composer.body = str(self.body)
        await interaction.response.edit_message(
            embed=self.composer.preview(), view=self.composer)


class AnnouncementComposer(discord.ui.View):
    """Target, audience and schedule as controls; only the body is typed."""

    def __init__(self, cog, guild_id: int, channel_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.body = ""
        self.target = "dm"
        self.audience = "entrants"
        self.scheduled = False
        self.confirming = False
        self.registry_error = ""
        self._build()

    # ---- rendering ----
    def preview(self) -> discord.Embed:
        if self.confirming:
            e = discord.Embed(
                title="Send this now?", colour=ui_v2.EMBER,
                description=f">>> {self.body}")
            e.add_field(
                name="To",
                value=("📩 Direct message" if self.target == "dm"
                       else f"📢 <#{self.channel_id}>"), inline=True)
            e.add_field(name="Recipients", value=str(len(self.recipients())),
                        inline=True)
            e.set_footer(text="This can't be undone once sent.")
            return e

        e = discord.Embed(title="📢 Create announcement", colour=ui_v2.BLURPLE)
        e.description = (f">>> {self.body}" if self.body
                         else "*No message yet — press **Write message**.*")
        e.add_field(
            name="Send to",
            value=("📩 Direct message" if self.target == "dm"
                   else f"📢 <#{self.channel_id}>"), inline=True)
        e.add_field(
            name="Audience",
            value=("Tournament entrants" if self.audience == "entrants"
                   else "All Beycord players"), inline=True)
        n = len(self.recipients())
        e.add_field(name="Recipients", value=str(n), inline=True)
        if self.registry_error:
            # A zero with no explanation is the thing that made this bug hard
            # to see from the outside. An AttributeError on the store almost
            # always means utils/ didn't update with the rest of the build, so
            # say that rather than leaving it looking like a database fault.
            hint = "Check the bot logs."
            if "attribute" in self.registry_error.lower():
                hint = ("This usually means `utils/` didn't update with the "
                        "rest of the build — re-upload the **whole** zip, "
                        "delete every `__pycache__` folder, and restart. "
                        "Run `;version` to confirm.")
            e.add_field(
                name="⚠️ Couldn't read the player list",
                value=f"`{self.registry_error[:150]}`\n{hint}",
                inline=False)
        elif self.target == "dm" and n == 0 and self.audience == "all":
            e.add_field(
                name="⚠️ No registered players found",
                value="The player database came back empty.", inline=False)
        elif self.target == "dm":
            e.set_footer(text="Only players are messaged — nobody else is pinged.")
        return e

    def recipients(self) -> list[int]:
        """Who this would actually reach, resolved live so the preview count is
        the real one rather than an estimate.

        "All players" means every registered Beycord player — read from the
        user database, not from tournament history. Deriving it from entrant
        lists (as this used to) silently excluded everyone who plays the bot
        but has never entered a tournament, which is most of the player base
        and exactly the audience a general announcement is for.
        """
        store = self.cog.svc.store
        if self.audience == "entrants":
            ids: list[int] = []
            for t in store.active_tournaments():
                if self.guild_id and t.guild_id != self.guild_id:
                    continue
                ids.extend(t.entrants)
            return list(dict.fromkeys(ids))

        try:
            from utils.database import all_user_ids
            ids = list(dict.fromkeys(all_user_ids()))
            if not ids:
                log.warning("player registry returned no ids for an "
                            "all-players announcement")
            return ids
        except Exception as e:                       # noqa: BLE001
            # Deliberately NOT falling back to tournament entrants here. That
            # fallback is what hid the original bug: the registry lookup threw
            # (all_user_ids existed only on the SQLite store, not MySQL), the
            # except swallowed it, and the admin saw "Recipients 0 · nobody
            # matches that audience" — which reads like there are no players
            # rather than like something is broken. An empty list with a
            # logged error is honest; a wrong-but-plausible number is not.
            log.exception("couldn't read the player registry: %s", e)
            self.registry_error = str(e) or type(e).__name__
            return []

    def _build(self):
        self.clear_items()

        if self.confirming:
            n = len(self.recipients())
            confirm = discord.ui.Button(
                label=f"Confirm — send to {n}" if self.target == "dm"
                      else "Confirm — post it",
                emoji="🚀", style=discord.ButtonStyle.primary, row=0,
                disabled=(self.target == "dm" and n == 0))
            confirm.callback = self._on_confirm_send
            back = discord.ui.Button(label="Back", emoji="⬅️",
                                     style=discord.ButtonStyle.secondary, row=0)
            back.callback = self._on_back
            self.add_item(confirm)
            self.add_item(back)
            return

        target = discord.ui.Select(
            placeholder="Send to…", row=0,
            options=[
                discord.SelectOption(
                    label="📩 Send as DM", value="dm",
                    description="Only people playing Beycord right now",
                    default=self.target == "dm"),
                discord.SelectOption(
                    label="📢 Send to channel", value="channel",
                    description="Everyone who can see this channel",
                    default=self.target == "channel")])
        target.callback = self._on_target
        self.add_item(target)

        aud = discord.ui.Select(
            placeholder="🎯 Audience…", row=1,
            options=[
                discord.SelectOption(
                    label="Tournament entrants only", value="entrants",
                    description="People in an open or running bracket",
                    default=self.audience == "entrants"),
                discord.SelectOption(
                    label="All Beycord players", value="all",
                    description="Everyone in the player database",
                    default=self.audience == "all")])
        aud.callback = self._on_audience
        self.add_item(aud)

        write = discord.ui.Button(label="Write message", emoji="✏️",
                                  style=discord.ButtonStyle.secondary, row=2)
        write.callback = self._on_write
        self.add_item(write)

        sched = discord.ui.Button(
            label=("⏰ Scheduled: on" if self.scheduled else "⏰ Send now"),
            style=(discord.ButtonStyle.success if self.scheduled
                   else discord.ButtonStyle.secondary), row=2)
        sched.callback = self._on_schedule
        self.add_item(sched)

        send = discord.ui.Button(label="Send announcement", emoji="🚀",
                                 style=discord.ButtonStyle.primary, row=3,
                                 disabled=not self.body)
        send.callback = self._on_send
        self.add_item(send)

        cancel = discord.ui.Button(label="Cancel",
                                   style=discord.ButtonStyle.secondary, row=3)
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    # ---- callbacks ----
    async def _on_target(self, interaction: discord.Interaction):
        self.target = interaction.data["values"][0]
        self._build()
        await interaction.response.edit_message(embed=self.preview(), view=self)

    async def _on_audience(self, interaction: discord.Interaction):
        self.audience = interaction.data["values"][0]
        self._build()
        await interaction.response.edit_message(embed=self.preview(), view=self)

    async def _on_write(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AnnouncementBody(self))

    async def _on_schedule(self, interaction: discord.Interaction):
        self.scheduled = not self.scheduled
        self._build()
        await interaction.response.edit_message(embed=self.preview(), view=self)

    async def _on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Cancelled — nothing was sent.", embed=None, view=None)

    async def _on_send(self, interaction: discord.Interaction):
        """Show what's about to happen and who it reaches — nothing is sent
        yet. Confirm is a separate click so a stray tap can't blast a DM."""
        if not self.body.strip():
            return await interaction.response.send_message(
                "Write a message first.", ephemeral=True)
        if self.target == "dm" and not self.recipients():
            return await interaction.response.send_message(
                "Nobody matches that audience yet.", ephemeral=True)
        self.confirming = True
        self._build()
        await interaction.response.edit_message(embed=self.preview(), view=self)

    async def _on_back(self, interaction: discord.Interaction):
        self.confirming = False
        self._build()
        await interaction.response.edit_message(embed=self.preview(), view=self)

    async def _on_confirm_send(self, interaction: discord.Interaction):
        await interaction.response.defer()
        text = self.body.strip()

        if self.target == "channel":
            ok = await notify.announce(
                self.cog.bot, self.channel_id,
                embed=discord.Embed(description=text, colour=ui_v2.EMBER,
                                    title="📢 Announcement"))
            done = discord.Embed(
                title="✅ Announcement posted" if ok else "❌ Couldn't post",
                colour=ui_v2.GREEN if ok else 0xED4245,
                description=(f"Posted in <#{self.channel_id}>." if ok
                             else "I couldn't send to that channel — check my "
                                  "permissions."))
            return await interaction.edit_original_response(
                embed=done, view=None)

        targets = self.recipients()
        if not targets:
            return await interaction.edit_original_response(
                embed=discord.Embed(
                    title="Nobody to message", colour=0xED4245,
                    description="No players match that audience yet."),
                view=None)

        # Recorded BEFORE sending — the id needs to exist so every DM's RSVP
        # buttons can point at it, and if the DM fan-out only gets partway
        # before something goes wrong the id (and the recipient list "who was
        # supposed to get this") isn't lost.
        aid = self.cog.svc.store.create_announcement(
            self.guild_id, self.channel_id, interaction.user.id, text,
            self.target, self.audience, targets)

        results = await notify.dm_all(
            self.cog.bot, targets,
            embed=discord.Embed(description=text, colour=ui_v2.EMBER,
                                title="📢 Beycord"),
            view=RSVPView(self.cog, aid))
        sent = sum(1 for v in results.values() if v)
        failed = len(results) - sent

        done = discord.Embed(
            title="✅ Announcement sent", colour=ui_v2.GREEN,
            description="Delivered to active Beycord players via DM.")
        done.add_field(name="Delivered", value=str(sent), inline=True)
        done.add_field(name="DMs closed", value=str(failed), inline=True)
        done.add_field(
            name="Audience",
            value=("Tournament entrants" if self.audience == "entrants"
                   else "All Beycord players"), inline=True)
        if failed:
            done.set_footer(text="People with DMs closed can't be reached — "
                                 "post to the channel to catch them.")
        # This button is how the "how many said yes" question gets answered
        # later — tap it any time, the count is read live from the DB.
        await interaction.edit_original_response(
            embed=done, view=RSVPCheckView(self.cog, aid))
