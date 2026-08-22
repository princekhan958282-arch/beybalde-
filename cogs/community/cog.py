"""
cogs/community/cog.py — the Discord surface for the community layer.

    /server     the whole admin surface, owner only        (one picker line)
    /poll       ask the server something
    /giveaway   run one, end it, reroll it, cancel it
    /level      your community standing

Plus the two listeners that earn XP, and the single loop that closes anything
whose timer has run out.

Every command opens with `guard.gate`, every manager method re-checks with
`require_main`, and both listeners return early unless `listener_ok`. Three
layers on purpose: a command left ungated by accident still cannot reach state,
because the manager refuses it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import config as C
from . import giveaways as GV
from . import guard
from . import panel as PN
from . import polls as PL
from . import store as S
from . import xp as XP

log = logging.getLogger("beyblade_bot.community")

TICK_SECONDS = 10.0
COLOUR = 0x5865F2

DURATIONS = [
    app_commands.Choice(name="10 minutes", value=600),
    app_commands.Choice(name="1 hour", value=3600),
    app_commands.Choice(name="6 hours", value=21600),
    app_commands.Choice(name="1 day", value=86400),
    app_commands.Choice(name="3 days", value=259200),
    app_commands.Choice(name="1 week", value=604800),
]


class CommunityCog(commands.Cog, name="Community"):
    """Main-server-only community systems."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.xp = XP.XPManager(bot)
        self.levels = XP.LevelManager(bot)
        self.polls = PL.PollManager(bot)
        self.giveaways = GV.GiveawayManager(bot)
        self._recovered = False

    async def cog_load(self) -> None:
        # Registered once, for every poll and giveaway posted before this
        # process existed. Without it a button pressed after a deploy does
        # nothing at all.
        for item in (PL.PollVoteButton, GV.GiveawayEnterButton):
            try:
                self.bot.add_dynamic_items(item)
            except Exception:                            # noqa: BLE001
                log.exception("[community] could not register %s", item)
        self.community_loop.start()

    async def cog_unload(self) -> None:
        self.community_loop.cancel()

    # ── The scheduler ────────────────────────────────────────────────────────
    @tasks.loop(seconds=TICK_SECONDS)
    async def community_loop(self) -> None:
        try:
            if not self._recovered:
                # Anything a dead process left mid-close goes back on the queue
                # before this one claims its first row.
                back = self.polls.recover() + self.giveaways.recover()
                if back:
                    log.info("[community] recovered %s stranded row(s)", back)
                self._recovered = True
            if not guard.is_configured():
                return
            for poll_id in self.polls.due():
                await self._finish_poll(poll_id)
            for give_id in self.giveaways.due():
                await self._finish_giveaway(give_id)
        except Exception as exc:                         # noqa: BLE001
            log.exception("[community] loop tick failed")
            try:
                from utils import errorlog
                errorlog.record("community.loop", exc)
            except Exception:                            # noqa: BLE001
                pass

    @community_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _finish_poll(self, poll_id: str) -> None:
        gid = guard.main_guild_id()
        poll = self.polls.close(gid, poll_id)
        if not poll:
            return
        res = self.polls.results(gid, poll_id)
        await self._edit_post(poll.get("channel_id"), poll.get("message_id"),
                              embed=PL.build_embed(poll, res), view=None)

    async def _finish_giveaway(self, giveaway_id: str) -> None:
        gid = guard.main_guild_id()
        out = self.giveaways.end(gid, giveaway_id)
        row = out.get("row") or self.giveaways.get(gid, giveaway_id)
        if not row:
            return
        winners = out.get("winners") or []
        await self._edit_post(
            row.get("channel_id"), row.get("message_id"),
            embed=GV.build_embed(row, S.entry_count(giveaway_id), winners),
            view=None)
        if not winners:
            return
        channel = self.bot.get_channel(int(row.get("channel_id") or 0))
        if channel is not None:
            try:
                await channel.send(
                    "🎉 " + ", ".join(f"<@{w}>" for w in winners)
                    + f" — you won **{row.get('prize')}**!")
            except Exception:                            # noqa: BLE001
                log.exception("[community] could not announce the winners")
        for winner in winners:
            await self._notify(winner, "🎉 You won!",
                               f"You won **{row.get('prize')}** in "
                               f"the giveaway. Congratulations!")

    async def _edit_post(self, channel_id, message_id, **kwargs) -> None:
        if not channel_id or not message_id:
            return
        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                return
            message = await channel.fetch_message(int(message_id))
            await message.edit(**kwargs)
        except Exception:                                # noqa: BLE001
            log.exception("[community] could not edit %s", message_id)

    async def _notify(self, user_id, title: str, body: str) -> None:
        """DM through the notification queue, so it inherits the retries."""
        try:
            from cogs.updates import service as SV
            await SV.notify_user(self.bot, user_id, event="IMPORTANT_NOTICE",
                                 title=title, body=body)
        except Exception:                                # noqa: BLE001
            log.exception("[community] could not queue a DM")

    # ── Button handlers, called by the DynamicItems ──────────────────────────
    async def handle_vote(self, interaction: discord.Interaction,
                          poll_id: str, index: int) -> None:
        if not await guard.gate(interaction):
            return
        gid = guard.main_guild_id()
        out = self.polls.vote(gid, poll_id, interaction.user.id, index)
        if not out.get("ok"):
            return await interaction.response.send_message(
                out.get("why", "That didn't work."), ephemeral=True)
        award = self.xp.award_poll_vote(gid, interaction.user.id)
        extra = f" · +{award['awarded']} XP" if award.get("awarded") else ""
        await interaction.response.send_message(
            f"✅ Voted for **{out['label']}**.{extra}", ephemeral=True)
        await self._after_award(interaction, award)
        poll = self.polls.get(gid, poll_id)
        if poll and not poll.get("anonymous", 1):
            await self._edit_post(poll.get("channel_id"),
                                  poll.get("message_id"),
                                  embed=PL.build_embed(
                                      poll, self.polls.results(gid, poll_id)),
                                  view=PL.view_for(poll))

    async def handle_entry(self, interaction: discord.Interaction,
                           giveaway_id: str) -> None:
        if not await guard.gate(interaction):
            return
        gid = guard.main_guild_id()
        out = self.giveaways.enter(gid, giveaway_id, interaction.user.id)
        if not out.get("ok"):
            return await interaction.response.send_message(
                out.get("why", "That didn't work."), ephemeral=True)
        award = self.xp.award_giveaway_entry(gid, interaction.user.id)
        extra = f" · +{award['awarded']} XP" if award.get("awarded") else ""
        await interaction.response.send_message(
            f"🎉 You're in — entry #{out['count']}.{extra}", ephemeral=True)
        await self._after_award(interaction, award)
        row = self.giveaways.get(gid, giveaway_id)
        if row:
            await self._edit_post(
                row.get("channel_id"), row.get("message_id"),
                embed=GV.build_embed(row, out["count"]),
                view=GV.view_for(giveaway_id))

    # ── Level-ups ────────────────────────────────────────────────────────────
    async def _after_award(self, source: Any, award: dict,
                           member=None) -> None:
        """Announce a level-up and hand out any role it earned."""
        if not award or not award.get("levelled"):
            return
        gid = guard.main_guild_id()
        member = member or getattr(source, "user", None) or getattr(
            source, "author", None)
        level = int(award.get("level") or 0)
        self.bot.dispatch("beycord_community_level",
                          {"user_id": getattr(member, "id", None),
                           "level": level, "xp": award.get("xp")})
        if member is not None and getattr(member, "guild", None) is not None:
            try:
                await self.levels.apply_roles(gid, member, level)
            except Exception:                            # noqa: BLE001
                log.exception("[community] level roles failed")
        chan_id = C.get(C.K_ANNOUNCE)
        channel = (self.bot.get_channel(int(chan_id)) if chan_id
                   else getattr(source, "channel", None))
        if channel is not None:
            try:
                await channel.send(
                    f"✨ {getattr(member, 'mention', 'Someone')} reached "
                    f"**community level {level}**.")
            except Exception:                            # noqa: BLE001
                log.exception("[community] level announcement failed")

    # ── Listeners ────────────────────────────────────────────────────────────
    @commands.Cog.listener("on_message")
    async def community_message_xp(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if not guard.listener_ok(message.guild):
            return
        try:
            # A command is not conversation. Same test the spawn counter uses.
            ctx = await self.bot.get_context(message)
            if ctx.valid:
                return
            award = self.xp.award_message(message.guild.id, message.author.id,
                                          message.content)
            if award.get("levelled"):
                await self._after_award(message, award, message.author)
        except Exception:                                # noqa: BLE001
            log.exception("[community] message XP failed")

    @commands.Cog.listener("on_raw_reaction_add")
    async def community_reaction_xp(self, payload) -> None:
        if not payload.guild_id or not guard.listener_ok(payload.guild_id):
            return
        member = getattr(payload, "member", None)
        if member is None or getattr(member, "bot", False):
            return
        try:
            author_id = None
            channel = self.bot.get_channel(payload.channel_id)
            if channel is not None:
                try:
                    msg = await channel.fetch_message(payload.message_id)
                    author_id = msg.author.id
                except Exception:                        # noqa: BLE001
                    author_id = None
            self.xp.award_reaction(payload.guild_id, payload.user_id,
                                   payload.message_id, author_id)
        except Exception:                                # noqa: BLE001
            log.exception("[community] reaction XP failed")

    # ── /server ──────────────────────────────────────────────────────────────
    @app_commands.command(
        name="server",
        description="[Owner] Set and tune the main community server")
    async def server(self, interaction: discord.Interaction) -> None:
        # NOT gated on the main server: this is the command that sets it.
        if not PN.is_owner(interaction.user):
            return await interaction.response.send_message(
                "That one isn't yours.", ephemeral=True)
        view = PN.ServerView(self.bot, interaction.user.id, self.levels)
        await interaction.response.send_message(
            embed=PN.build_embed(self.bot, interaction.guild), view=view,
            ephemeral=True)

    # ── /poll ────────────────────────────────────────────────────────────────
    @app_commands.command(name="poll", description="Ask the server something")
    @app_commands.describe(duration="When it closes. Leave blank to close it "
                                    "by hand.")
    @app_commands.choices(duration=DURATIONS)
    async def poll(self, interaction: discord.Interaction,
                   duration: Optional[app_commands.Choice[int]] = None) -> None:
        if not await guard.gate(interaction):
            return
        await interaction.response.send_modal(
            PollModal(self, duration.value if duration else None))

    # ── /giveaway ────────────────────────────────────────────────────────────
    @app_commands.command(name="giveaway",
                          description="Run a giveaway — create, end, reroll")
    @app_commands.describe(duration="How long it runs.")
    @app_commands.choices(duration=DURATIONS)
    async def giveaway(self, interaction: discord.Interaction,
                       duration: Optional[app_commands.Choice[int]] = None
                       ) -> None:
        if not await guard.gate(interaction):
            return
        from cogs.admin import actions as A
        if not A.is_admin(interaction.user):
            return await interaction.response.send_message(
                "Only staff can run giveaways.", ephemeral=True)
        await interaction.response.send_modal(
            GiveawayModal(self, duration.value if duration else 3600))

    # ── /level ───────────────────────────────────────────────────────────────
    @app_commands.command(name="level",
                          description="Your community level and XP")
    @app_commands.describe(user="Whose card to show. Defaults to yours.")
    async def level(self, interaction: discord.Interaction,
                    user: Optional[discord.Member] = None) -> None:
        if not await guard.gate(interaction):
            return
        target = user or interaction.user
        card = self.xp.card(guard.main_guild_id(), target.id)
        filled = 0 if not card["span"] else round(
            12 * card["into"] / card["span"])
        e = discord.Embed(
            title=f"✨  {target.display_name}",
            colour=COLOUR,
            description=(f"**Community level {card['level']}**\n"
                         f"`{'▰' * filled}{'▱' * (12 - filled)}`  "
                         f"{card['into']:,} / {card['span']:,}\n"
                         f"{card['xp']:,} XP total"))
        e.set_footer(text=f"{card['today']:,} / {card['cap']:,} XP earned "
                          f"today · community XP is separate from your "
                          f"trainer level")
        await interaction.response.send_message(embed=e)


class PollModal(discord.ui.Modal, title="New poll"):
    def __init__(self, cog: CommunityCog, duration: Optional[int]) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.duration = duration
        self.question = discord.ui.TextInput(
            label="Question", max_length=PL.MAX_QUESTION, required=True,
            placeholder="Which blade should we buff?")
        self.options = discord.ui.TextInput(
            label="Choices — one per line", style=discord.TextStyle.paragraph,
            required=True, max_length=800,
            placeholder="Valkyrie\nSpriggan\nFafnir")
        self.add_item(self.question)
        self.add_item(self.options)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await guard.gate(interaction):
            return
        lines = [ln.strip() for ln in str(self.options.value).splitlines()
                 if ln.strip()]
        try:
            poll = self.cog.polls.create(
                interaction.guild_id, interaction.user.id,
                str(self.question.value), lines, duration=self.duration)
        except PL.PollError as exc:
            return await interaction.response.send_message(str(exc),
                                                           ephemeral=True)
        await interaction.response.send_message(
            embed=PL.build_embed(poll), view=PL.view_for(poll))
        message = await interaction.original_response()
        self.cog.polls.attach(interaction.guild_id, poll["poll_id"],
                              message.channel.id, message.id)


class GiveawayModal(discord.ui.Modal, title="New giveaway"):
    def __init__(self, cog: CommunityCog, duration: int) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.duration = duration
        self.prize = discord.ui.TextInput(
            label="Prize", max_length=GV.MAX_PRIZE, required=True,
            placeholder="Ultimate Valkyrie")
        self.winners = discord.ui.TextInput(
            label="How many winners", max_length=2, required=False,
            default="1")
        self.add_item(self.prize)
        self.add_item(self.winners)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await guard.gate(interaction):
            return
        try:
            count = int(str(self.winners.value or "1").strip() or 1)
        except (TypeError, ValueError):
            count = 1
        try:
            row = self.cog.giveaways.create(
                interaction.guild_id, interaction.user.id,
                str(self.prize.value), duration=self.duration, winners=count)
        except GV.GiveawayError as exc:
            return await interaction.response.send_message(str(exc),
                                                           ephemeral=True)
        await interaction.response.send_message(
            embed=GV.build_embed(row, 0),
            view=GV.view_for(row["giveaway_id"]))
        message = await interaction.original_response()
        self.cog.giveaways.attach(interaction.guild_id, row["giveaway_id"],
                                  message.channel.id, message.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CommunityCog(bot))
