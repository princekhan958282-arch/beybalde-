"""
cogs/community/panel.py — `/server`, the one admin surface for this layer.

Nine systems, one picker line. Everything configurable about the community
layer is reachable here: which server it runs in, the personality, whether XP
is on, where level-ups are announced, and the level -> role rewards.

Owner-gated with the same `MASTER_ID` check the rest of the bot uses, imported
from `cogs.admin.actions` rather than copied — that constant already exists in
six files and a seventh is how they drift.
"""

from __future__ import annotations

import logging

from typing import Optional

import discord

from . import config as C
from . import guard

log = logging.getLogger("beyblade_bot.community.panel")

COLOUR = 0x5865F2

PERSONALITIES = [
    ("FUNNY",    "Funny",    "jokes first, everything is a bit"),
    ("FRIENDLY", "Friendly", "warm, encouraging, no edge"),
    ("SAVAGE",   "Savage",   "sharp and teasing, still fond"),
    ("HYPE",     "Hype",     "loud, all caps, permanent finals energy"),
    ("SERIOUS",  "Serious",  "plain and businesslike"),
]


def is_owner(user) -> bool:
    from cogs.admin import actions as A
    return getattr(user, "id", None) == A.MASTER_ID


def build_embed(bot, invoker_guild=None) -> discord.Embed:
    """What `/server` shows: where the layer lives, and how it is tuned."""
    gid = guard.main_guild_id()
    guild = bot.get_guild(gid) if (bot and gid) else None
    if gid is None:
        where = ("⚠️ **No main server set.** Every community feature is off, "
                 "everywhere. Press **Set this server** to switch them on here.")
    elif guild is not None:
        where = f"✅ **{guild.name}** · `{gid}`"
    else:
        where = (f"⚠️ Set to `{gid}`, which I can't see. Community features "
                 f"are off until that's fixed.")

    e = discord.Embed(title="🏠  Community systems", colour=COLOUR,
                      description=where)
    if gid is not None:
        roles = C.level_roles()
        chan = C.get(C.K_ANNOUNCE)
        e.add_field(name="Personality",
                    value=str(C.get(C.K_PERSONALITY)).title(), inline=True)
        e.add_field(name="XP",
                    value="on" if C.get(C.K_XP_ENABLED, True) else "off",
                    inline=True)
        e.add_field(name="Polls",
                    value=("staff only"
                           if C.get(C.K_POLL_STAFF_ONLY, True) else "everyone"),
                    inline=True)
        e.add_field(name="Level-ups",
                    value=(f"<#{chan}>" if chan else "in the channel they "
                           "levelled up in"), inline=True)
        e.add_field(
            name=f"Level roles ({len(roles)})",
            value=("\n".join(f"level **{lvl}** → <@&{rid}>"
                             for lvl, rid in sorted(roles.items())[:8])
                   or "none yet — pick a role below"),
            inline=False)
        if invoker_guild is not None and not guard.is_main(invoker_guild):
            e.set_footer(text="You're configuring the main server from "
                              "somewhere else.")
    return e


class LevelRoleModal(discord.ui.Modal, title="Level role"):
    """Which level earns the role that was just picked."""

    def __init__(self, panel: "ServerView", role: discord.Role) -> None:
        super().__init__(timeout=300)
        self.panel = panel
        self.role = role
        self.level = discord.ui.TextInput(
            label=f"Level that earns @{role.name}"[:45],
            placeholder="10", max_length=4, required=True)
        self.add_item(self.level)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            level = int(str(self.level.value).strip())
        except (TypeError, ValueError):
            return await interaction.response.send_message(
                "That isn't a number.", ephemeral=True)
        ok, message = self.panel.levels.set_level_role(
            guard.main_guild_id(), level, self.role)
        await interaction.response.send_message(message, ephemeral=True)
        if ok:
            # NOT `panel.refresh(interaction)`. For a modal submit,
            # `original_response()` is the ephemeral line just sent — not the
            # /server panel, which belongs to a different interaction — so
            # refreshing through it replaced the confirmation with a second
            # copy of the panel and left the real one stale above.
            await self.panel.refresh_parent()


class PersonalitySelect(discord.ui.Select):
    def __init__(self, panel: "ServerView") -> None:
        current = str(C.get(C.K_PERSONALITY))
        super().__init__(
            placeholder="Personality…", row=0,
            options=[discord.SelectOption(label=label, value=key,
                                          description=blurb,
                                          default=(key == current))
                     for key, label, blurb in PERSONALITIES])
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.panel.guard(interaction):
            return
        C.put(C.K_PERSONALITY, self.values[0])
        await self.panel.refresh(interaction)


class LevelRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: "ServerView") -> None:
        super().__init__(placeholder="Add a level role…", row=1,
                         min_values=1, max_values=1)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.panel.guard(interaction):
            return
        role = self.values[0]
        # A RoleSelect can only offer roles from the guild the interaction
        # happened in, and `/server` is deliberately usable from anywhere. So
        # configuring from another server used to save a role id the main guild
        # has never heard of, validated against the wrong guild's hierarchy —
        # and the reward was then silently skipped at grant time, with the
        # panel still rendering it as if it worked.
        main = guard.main_guild_id()
        if main is None or role.guild is None or role.guild.id != main:
            return await interaction.response.send_message(
                "Level roles have to be picked **in the main server** — a role "
                "from anywhere else can't be granted there.", ephemeral=True)
        ok, why = self.panel.levels.check_role(role.guild, role)
        if not ok:
            # Refused here rather than at grant time, so the mapping never
            # contains a role that cannot actually be given out.
            return await interaction.response.send_message(why, ephemeral=True)
        await interaction.response.send_modal(LevelRoleModal(self.panel, role))


class ServerView(discord.ui.View):
    """The `/server` panel. Owner only, checked on every interaction."""

    def __init__(self, bot, invoker_id: int, levels) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.invoker_id = int(invoker_id)
        self.levels = levels
        # Set once the panel has been sent, so a modal opened from it can edit
        # the panel rather than its own reply.
        self.parent: Optional[discord.InteractionMessage] = None
        self.rebuild()

    async def refresh_parent(self) -> None:
        """Re-render the panel message itself. Never raises."""
        C.invalidate()
        self.rebuild()
        if self.parent is None:
            return
        try:
            await self.parent.edit(embed=build_embed(self.bot), view=self)
        except Exception:                                # noqa: BLE001
            log.exception("[community] could not refresh /server")

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.parent is None:
            return
        try:
            await self.parent.edit(view=self)
        except Exception:                                # noqa: BLE001
            pass

    def rebuild(self) -> None:
        self.clear_items()
        if guard.is_configured():
            self.add_item(PersonalitySelect(self))
            self.add_item(LevelRoleSelect(self))
        for item in (self.set_here, self.toggle_xp, self.toggle_polls,
                     self.announce_here, self.clear_roles, self.unset):
            self.add_item(item)

    async def guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id or not is_owner(
                interaction.user):
            await interaction.response.send_message(
                "That panel isn't yours.", ephemeral=True)
            return False
        return True

    async def refresh(self, interaction: discord.Interaction) -> None:
        C.invalidate()
        self.rebuild()
        embed = build_embed(self.bot, interaction.guild)
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except Exception:                                # noqa: BLE001
            log.exception("[community] could not refresh /server")

    @discord.ui.button(label="Set this server", emoji="🏠", row=2,
                       style=discord.ButtonStyle.success)
    async def set_here(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        guard.set_main_guild(interaction.guild_id)
        await self.refresh(interaction)

    @discord.ui.button(label="XP on/off", emoji="✨", row=2,
                       style=discord.ButtonStyle.secondary)
    async def toggle_xp(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        C.put(C.K_XP_ENABLED, not C.get(C.K_XP_ENABLED, True))
        await self.refresh(interaction)

    @discord.ui.button(label="Who can poll", emoji="📊", row=2,
                       style=discord.ButtonStyle.secondary)
    async def toggle_polls(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        C.put(C.K_POLL_STAFF_ONLY, not C.get(C.K_POLL_STAFF_ONLY, True))
        await self.refresh(interaction)

    @discord.ui.button(label="Announce level-ups here", emoji="📣", row=3,
                       style=discord.ButtonStyle.secondary)
    async def announce_here(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        C.put(C.K_ANNOUNCE, str(interaction.channel_id))
        await self.refresh(interaction)

    @discord.ui.button(label="Clear level roles", emoji="🧹", row=3,
                       style=discord.ButtonStyle.secondary)
    async def clear_roles(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        self.levels.clear_level_roles(guard.main_guild_id())
        await self.refresh(interaction)

    @discord.ui.button(label="Banter…", emoji="💬", row=4,
                       style=discord.ButtonStyle.primary)
    async def banter(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        # A sub-page rather than more controls here: `/server` already uses all
        # five action rows Discord allows (two selects and six buttons), so a
        # sixth row is not a design choice, it is a hard limit.
        view = BanterView(self)
        await interaction.response.send_message(
            embed=banter_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="Turn the whole layer off", emoji="🔒", row=4,
                       style=discord.ButtonStyle.danger)
    async def unset(self, interaction: discord.Interaction, _b) -> None:
        if not await self.guard(interaction):
            return
        guard.set_main_guild(None)
        await self.refresh(interaction)


# ── The banter sub-page ──────────────────────────────────────────────────────

INTENSITIES = [
    ("OFF",     "Off",     "never speaks unless spoken to"),
    ("LIGHT",   "Light",   "rare — at most once every 15 minutes"),
    ("NORMAL",  "Normal",  "chimes in now and then"),
    ("CHAOTIC", "Chaotic", "talkative; expect it in every busy channel"),
]


def banter_embed() -> discord.Embed:
    """What `/server → Banter` shows, including whether generation is live."""
    from . import chat as CH

    intensity = str(C.get(C.K_BANTER) or "OFF").upper()
    deny = C.get(C.K_BANTER_DENY) or []
    name = str(C.get(C.K_PERSONALITY))

    e = discord.Embed(
        title="💬  Banter",
        colour=COLOUR,
        description=("Being **@mentioned or replied to** always gets an answer "
                     "— that needs nothing switched on here.\n"
                     "This setting is only about speaking **unprompted**."))
    e.add_field(name="Personality", value=name.title(), inline=True)
    e.add_field(name="Unprompted", value=intensity.title(), inline=True)

    # Honest about the API, exactly as the boss UI is: "dialogue offline" beats
    # claiming a working generator while every line comes from the pools.
    try:
        st = CH.ChatEngine().client.status()
        if st["state"] == "live":
            gen = f"✅ generative · `{st['model']}`"
        elif st["state"] == "no key":
            gen = "➖ written lines (no `GEMINI_API_KEY` set)"
        elif st["state"] == "budget spent":
            gen = "🟠 written lines — hourly budget spent"
        else:
            gen = f"🟠 written lines — {st['state']}"
    except Exception:                                    # noqa: BLE001
        gen = "written lines"
    e.add_field(name="Wording", value=gen, inline=False)

    e.add_field(
        name=f"Muted channels ({len(deny)})",
        value=("\n".join(f"<#{c}>" for c in list(deny)[:8])
               or "none — it may speak in any channel it can see"),
        inline=False)
    e.set_footer(text="Anyone can opt out for themselves with ;chat off")
    return e


class IntensitySelect(discord.ui.Select):
    def __init__(self, page: "BanterView") -> None:
        current = str(C.get(C.K_BANTER) or "OFF").upper()
        super().__init__(
            placeholder="How often should it speak unprompted?…", row=0,
            options=[discord.SelectOption(label=lbl, value=key, description=d,
                                          default=(key == current))
                     for key, lbl, d in INTENSITIES])
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.page.parent.guard(interaction):
            return
        C.put(C.K_BANTER, self.values[0])
        await self.page.refresh(interaction)


class BanterDenySelect(discord.ui.ChannelSelect):
    def __init__(self, page: "BanterView") -> None:
        super().__init__(placeholder="Mute banter in a channel…", row=1,
                         min_values=1, max_values=1,
                         channel_types=[discord.ChannelType.text])
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.page.parent.guard(interaction):
            return
        deny = list(C.get(C.K_BANTER_DENY) or [])
        cid = str(self.values[0].id)
        # A toggle rather than an add-only list: without this, un-muting a
        # channel would need a second control, and the list would only ever
        # grow. Stored as strings so the JSON round-trip is stable.
        deny = [d for d in deny if str(d) != cid] if cid in {str(d) for d in deny} \
            else deny + [cid]
        C.put(C.K_BANTER_DENY, deny)
        await self.page.refresh(interaction)


class BanterView(discord.ui.View):
    """`/server → Banter`. Ephemeral, owned by whoever opened `/server`."""

    def __init__(self, parent: "ServerView") -> None:
        super().__init__(timeout=300)
        self.parent = parent
        self.add_item(IntensitySelect(self))
        self.add_item(BanterDenySelect(self))

    async def refresh(self, interaction: discord.Interaction) -> None:
        C.invalidate()
        self.clear_items()
        self.add_item(IntensitySelect(self))
        self.add_item(BanterDenySelect(self))
        for item in (self.say_something, self.clear_muted):
            self.add_item(item)
        try:
            await interaction.response.edit_message(embed=banter_embed(),
                                                    view=self)
        except Exception:                                # noqa: BLE001
            log.exception("[community] could not refresh the banter page")

    @discord.ui.button(label="Say something", emoji="🗣️", row=2,
                       style=discord.ButtonStyle.primary)
    async def say_something(self, interaction: discord.Interaction, _b) -> None:
        """Hear the current personality before committing to it.

        Goes through the real engine — same prompt, same fallback, same
        post-filter — so what the owner hears is what the server will get.
        """
        if not await self.parent.guard(interaction):
            return
        from . import chat as CH
        await interaction.response.defer(ephemeral=True, thinking=True)
        # The live cog's engine when there is one, so the sample reflects the
        # real breaker and budget state rather than a fresh client that always
        # looks healthy.
        cog = self.parent.bot.get_cog("Community") if self.parent.bot else None
        engine = getattr(cog, "chat", None) or CH.ChatEngine()
        msg = CH.Incoming(
            guild_id=guard.main_guild_id(), channel_id=interaction.channel_id,
            user_id=interaction.user.id, content="say something",
            display_name=getattr(interaction.user, "display_name", "") or "",
            mentions_bot=True, profile={})
        try:
            line = await engine.compose(msg, "mentioned")
        except Exception:                                # noqa: BLE001
            log.exception("[community] sample line failed")
            line = "…"
        await interaction.followup.send(line or "…", ephemeral=True)

    @discord.ui.button(label="Unmute all", emoji="🧹", row=2,
                       style=discord.ButtonStyle.secondary)
    async def clear_muted(self, interaction: discord.Interaction, _b) -> None:
        if not await self.parent.guard(interaction):
            return
        C.put(C.K_BANTER_DENY, [])
        await self.refresh(interaction)
