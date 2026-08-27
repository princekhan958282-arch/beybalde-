"""
cogs/community/cog.py — the Discord surface for the community layer.

    /server     the whole admin surface, owner only        (one picker line)
    /poll       ask the server something — create, close, cancel, results
    /giveaway   run one — create, end now, reroll, cancel
    /level      your community standing

Plus the two listeners that earn XP, and the single loop that closes anything
whose timer has run out.

Every command opens with `guard.gate`, every manager method re-checks with
`require_main`, and both listeners return early unless `listener_ok`. Three
layers on purpose: a command left ungated by accident still cannot reach state,
because the manager refuses it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import chat as CH
from . import config as C
from . import giveaways as GV
from . import guard
from . import manage as MG
from . import memory as MEM
from . import panel as PN
from . import polls as PL
from . import store as S
from . import xp as XP

log = logging.getLogger("beyblade_bot.community")

TICK_SECONDS = 10.0
# How often to re-check for rows stranded in CLOSING. A row is only there for
# milliseconds in the normal case, so this is generous.
RECOVER_EVERY = 60.0
COLOUR = 0x5865F2

class CommunityCog(commands.Cog, name="Community"):
    """Main-server-only community systems."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.xp = XP.XPManager(bot)
        self.levels = XP.LevelManager(bot)
        self.polls = PL.PollManager(bot)
        self.giveaways = GV.GiveawayManager(bot)
        self.chat = CH.ChatEngine()
        self._recovered = False
        self._last_sweep = 0.0

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
            self._sweep_stranded()
            if not guard.is_configured():
                return
            # Per ITEM, not per tick. `claim_due` has already flipped every id
            # it returned to CLOSING before Python sees it, so one row that
            # raises used to strand its whole batch — dead rows, because
            # `claim_due` only ever selects OPEN — and skip the giveaway pass
            # entirely. Now a bad row costs one row.
            for poll_id in self.polls.due():
                await self._guarded(self._finish_poll(poll_id),
                                    f"poll {poll_id}")
            for give_id in self.giveaways.due():
                await self._guarded(self._finish_giveaway(give_id),
                                    f"giveaway {give_id}")
        except Exception as exc:                         # noqa: BLE001
            log.exception("[community] loop tick failed")
            self._record(exc)

    async def _guarded(self, coro, what: str) -> None:
        """Run one item; a failure is logged and the tick carries on."""
        try:
            await coro
        except Exception as exc:                         # noqa: BLE001
            log.exception("[community] %s failed to finish", what)
            self._record(exc)

    def _record(self, exc: Exception) -> None:
        try:
            from utils import errorlog
            errorlog.record("community.loop", exc)
        except Exception:                                # noqa: BLE001
            pass

    def _sweep_stranded(self) -> None:
        """Return anything parked in CLOSING to the queue — repeatedly.

        This used to run once per process behind a `_recovered` flag, which
        meant a row stranded at RUNTIME (see `_guarded` above) waited for the
        next restart. A row only reaches CLOSING for the few milliseconds it
        takes to finish it, so anything still there a minute later is stuck by
        definition and safe to re-queue.
        """
        now = time.time()
        first = not self._recovered
        if not first and (now - self._last_sweep) < RECOVER_EVERY:
            return
        self._last_sweep = now
        self._recovered = True
        back = self.polls.recover() + self.giveaways.recover()
        if back:
            log.info("[community] returned %s stranded row(s) to the queue",
                     back)

    @community_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # These are public because the manage panel presses the same buttons the
    # timer does. "End now" and the expiry path must not be two code paths
    # that can disagree about what ending means.
    async def finish_poll(self, poll_id: str) -> Optional[dict]:
        gid = guard.main_guild_id()
        poll = self.polls.close(gid, poll_id)
        if not poll:
            return None
        res = self.polls.results(gid, poll_id)
        await self._edit_post(poll.get("channel_id"), poll.get("message_id"),
                              embed=PL.build_embed(poll, res), view=None)
        return poll

    _finish_poll = finish_poll

    async def repost_poll(self, poll_id: str, with_view: bool = True) -> None:
        """Re-render a poll's message in place — used after a cancel."""
        gid = guard.main_guild_id()
        poll = self.polls.get(gid, poll_id)
        if not poll:
            return
        await self._edit_post(
            poll.get("channel_id"), poll.get("message_id"),
            embed=PL.build_embed(poll, self.polls.results(gid, poll_id)),
            view=PL.view_for(poll) if with_view else None)

    async def finish_giveaway(self, giveaway_id: str) -> dict:
        gid = guard.main_guild_id()
        out = self.giveaways.end(gid, giveaway_id)
        row = out.get("row") or self.giveaways.get(gid, giveaway_id)
        if not row:
            return {"ok": False, "why": "That giveaway is gone."}
        # Only a fresh draw is announced. `end()` deliberately returns NO
        # winners when it refuses, so pressing End on a finished giveaway
        # cannot re-ping and re-DM everyone who has ever won it.
        winners = out.get("winners") or [] if out.get("ok") else []
        await self._edit_post(
            row.get("channel_id"), row.get("message_id"),
            embed=GV.build_embed(row, S.entry_count(giveaway_id),
                                 row.get("winner_ids") or []),
            view=None)
        if winners:
            await self.announce_winners(giveaway_id, winners)
        return out

    _finish_giveaway = finish_giveaway

    async def repost_giveaway(self, giveaway_id: str,
                              with_view: bool = True) -> None:
        gid = guard.main_guild_id()
        row = self.giveaways.get(gid, giveaway_id)
        if not row:
            return
        await self._edit_post(
            row.get("channel_id"), row.get("message_id"),
            embed=GV.build_embed(row, S.entry_count(giveaway_id),
                                 row.get("winner_ids") or []),
            view=GV.view_for(giveaway_id) if with_view else None)

    async def announce_winners(self, giveaway_id: str, winners: list,
                               rerolled: bool = False) -> None:
        """Ping in channel, then DM through the notification queue."""
        gid = guard.main_guild_id()
        row = self.giveaways.get(gid, giveaway_id)
        if not row or not winners:
            return
        mentions = ", ".join(f"<@{w}>" for w in winners)
        channel = self.bot.get_channel(int(row.get("channel_id") or 0))
        if channel is not None:
            try:
                await channel.send(
                    f"🎉 {mentions} — you won **{row.get('prize')}**!"
                    + (" (reroll)" if rerolled else ""))
            except Exception:                            # noqa: BLE001
                log.exception("[community] could not announce the winners")
        for winner in winners:
            await self._notify(winner, "🎉 You won!",
                               f"You won **{row.get('prize')}** in "
                               f"the giveaway. Congratulations!")

    async def _edit_post(self, channel_id, message_id, **kwargs) -> None:
        """Edit a post we already know the ids of — one REST call, not two.

        `fetch_message` then `edit` costs a GET and a PATCH on a bucket that
        allows ~5 edits per 5s per message; a busy giveaway serialises every
        entry behind it and the embed falls minutes behind. A partial message
        needs no GET.
        """
        if not channel_id or not message_id:
            return
        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                return
            await channel.get_partial_message(int(message_id)).edit(**kwargs)
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
        # Deferred FIRST. Everything below touches the store, and the store
        # takes the process-wide user lock; a busy moment used to blow the 3s
        # interaction window, so the vote was recorded, the player saw "This
        # interaction failed", pressed again and was told they had already
        # voted.
        if not await self._defer(interaction):
            return
        if not await guard.gate(interaction):
            return
        try:
            gid = guard.main_guild_id()
            out = self.polls.vote(gid, poll_id, interaction.user.id, index)
            if not out.get("ok"):
                return await interaction.followup.send(
                    out.get("why", "That didn't work."), ephemeral=True)
            award = await self.xp.award_poll_vote(gid, interaction.user.id)
            extra = f" · +{award['awarded']} XP" if award.get("awarded") else ""
            await interaction.followup.send(
                f"✅ Voted for **{out['label']}**.{extra}", ephemeral=True)
            await self._after_award(interaction, award)
            poll = self.polls.get(gid, poll_id)
            if poll and not poll.get("anonymous", 1):
                await self._edit_post(
                    poll.get("channel_id"), poll.get("message_id"),
                    embed=PL.build_embed(poll,
                                         self.polls.results(gid, poll_id)),
                    view=PL.view_for(poll))
        except Exception as exc:                         # noqa: BLE001
            log.exception("[community] vote failed")
            self._record(exc)
            await self._sorry(interaction)

    async def handle_entry(self, interaction: discord.Interaction,
                           giveaway_id: str) -> None:
        if not await self._defer(interaction):
            return
        if not await guard.gate(interaction):
            return
        try:
            gid = guard.main_guild_id()
            out = self.giveaways.enter(gid, giveaway_id, interaction.user.id)
            if not out.get("ok"):
                return await interaction.followup.send(
                    out.get("why", "That didn't work."), ephemeral=True)
            award = await self.xp.award_giveaway_entry(gid, interaction.user.id)
            extra = f" · +{award['awarded']} XP" if award.get("awarded") else ""
            await interaction.followup.send(
                f"🎉 You're in — entry #{out['count']}.{extra}", ephemeral=True)
            await self._after_award(interaction, award)
            row = self.giveaways.get(gid, giveaway_id)
            if row:
                await self._edit_post(
                    row.get("channel_id"), row.get("message_id"),
                    embed=GV.build_embed(row, out["count"]),
                    view=GV.view_for(giveaway_id))
        except Exception as exc:                         # noqa: BLE001
            log.exception("[community] entry failed")
            self._record(exc)
            await self._sorry(interaction)

    async def _defer(self, interaction: discord.Interaction) -> bool:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return True
        except Exception:                                # noqa: BLE001
            log.exception("[community] could not defer")
            return False

    async def _sorry(self, interaction: discord.Interaction) -> None:
        """Say something. An unanswered interaction is the worst outcome."""
        try:
            await interaction.followup.send(
                "⚠️ That didn't go through — it's in the error log.",
                ephemeral=True)
        except Exception:                                # noqa: BLE001
            pass

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
        user_id = getattr(member, "id", None)

        # Roles first, and deliberately NOT behind the once-per-level claim
        # below: `apply_roles` skips what the member already has, so it is
        # free to re-run, and re-running is the only thing that ever retries a
        # grant that failed the first time (missing permission, role above the
        # bot, an API blip).
        if member is not None and getattr(member, "guild", None) is not None:
            try:
                await self.levels.apply_roles(gid, member, level)
            except Exception:                            # noqa: BLE001
                log.exception("[community] level roles failed")

        # Say it ONCE per level, ever.
        #
        # `levelled` is derived per award from the XP before and after, and
        # nothing used to remember that a level had already been announced —
        # so anything that re-crossed the boundary announced again. That is
        # not hypothetical: a player was congratulated on community level 7
        # seven times. Since XP in this system only ever increases, a repeat
        # means the stored total went BACKWARDS, which is what two bot
        # processes sharing one store do to each other's writes. This guard
        # does not fix that (nothing in one process can), but it does make the
        # announcement idempotent, so the visible symptom cannot recur.
        if user_id is not None:
            from utils.database import claim_high_water
            try:
                # to_thread for the same reason every other store call in this
                # file hops off the loop: on MySQL this is a network
                # round-trip under `_users_lock`.
                fresh = await asyncio.to_thread(
                    claim_high_water, int(user_id), XP.K_LEVEL_SAID, level)
            except Exception:                            # noqa: BLE001
                log.exception("[community] level claim failed")
                fresh = True          # never swallow a real level-up
            if not fresh:
                return

        self.bot.dispatch("beycord_community_level",
                          {"user_id": user_id,
                           "level": level, "xp": award.get("xp")})
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
    @commands.Cog.listener("on_beycord_community_level")
    async def on_community_level(self, payload: dict) -> None:
        """The event `_after_award` dispatches, given somewhere to land.

        Dispatching an event nothing listens for is how `on_level_up` sat dead
        in this codebase for months. Achievements are the natural consumer:
        a community level is progress like any other.
        """
        try:
            cog = self.bot.get_cog("Achievements")
            evaluate = getattr(cog, "evaluate", None) if cog else None
            if callable(evaluate):
                await discord.utils.maybe_coroutine(
                    evaluate, payload.get("user_id"))
        except Exception:                                # noqa: BLE001
            log.debug("[community] no achievement hook for a community level")

    @commands.Cog.listener("on_message")
    async def community_message_xp(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if not guard.listener_ok(message.guild):
            return
        try:
            # A command is not conversation. Same test the spawn counter uses.
            # Resolved ONCE and shared with the chat engine below — this call
            # parses the message against every registered command, and doing it
            # twice per message would double that cost for no new information.
            ctx = await self.bot.get_context(message)
            if ctx.valid:
                return
            award = await self.xp.award_message(message.guild.id, message.author.id,
                                          message.content)
            if award.get("levelled"):
                await self._after_award(message, award, message.author)
        except Exception:                                # noqa: BLE001
            log.exception("[community] message XP failed")
            ctx = None

        # Banter is deliberately AFTER the XP award and in its own try: a
        # failure to think of something to say must never cost somebody their
        # XP for the message.
        try:
            await self._maybe_chat(message, is_command=bool(ctx and ctx.valid))
        except Exception:                                # noqa: BLE001
            log.exception("[community] chat failed")

    async def _maybe_chat(self, message: discord.Message,
                          *, is_command: bool) -> None:
        """Speak, if the engine says to. Every gate lives in `chat.decide`."""
        # Observed before the decision, and regardless of whether we speak:
        # "how busy is this channel" has to count the messages the bot stays
        # quiet for, or the room always reads as quiet.
        self.chat.room.observe(message.channel.id, message.author.id)

        me = self.bot.user
        mentions_bot = bool(me and me in getattr(message, "mentions", ()))
        ref = getattr(message, "reference", None)
        resolved = getattr(ref, "resolved", None) if ref else None
        replies_to_bot = bool(
            me and resolved is not None
            and getattr(getattr(resolved, "author", None), "id", None) == me.id)

        if not (mentions_bot or replies_to_bot) and not self.chat.intensity_on():
            # The cheap exit. With banter OFF — the shipped default — an
            # unaddressed message costs one settings read and nothing else:
            # no profile fetch, no engine call, on every message in the server.
            return

        profile = {}
        try:
            from utils.database import get_user
            profile = await get_user(message.author.id) or {}
        except Exception:                                # noqa: BLE001
            log.debug("[community] no profile for chat", exc_info=True)

        msg = CH.Incoming(
            guild_id=message.guild.id, channel_id=message.channel.id,
            user_id=message.author.id, content=message.content or "",
            display_name=getattr(message.author, "display_name", "") or "",
            is_bot=False, is_command=is_command,
            mentions_bot=mentions_bot, replies_to_bot=replies_to_bot,
            profile=profile)

        decision = self.chat.decide(msg)
        if not decision.speak:
            log.debug("[community] staying quiet: %s", decision.reason)
            return

        line = await self.chat.compose(msg, decision.moment)
        if not line:
            return

        # allowed_mentions is belt AND braces: chat.post_filter already strips
        # every ping shape, and this makes a miss unexploitable rather than
        # merely unlikely.
        await message.channel.send(
            line, reference=message if decision.moment != "banter" else None,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none())

        # Rapport is only earned on a real exchange, and only when the bot was
        # actually spoken to — unprompted banter is the bot's initiative, not
        # the player's, so it must not inflate their relationship score.
        if msg.addressed:
            try:
                from utils.database import mutate_user
                # touch=False for the reason mutate_user's own docstring gives:
                # this is driven by a MESSAGE, and stamping last_seen here
                # would make every chatter count as an active player in the
                # "who played today" numbers.
                await mutate_user(int(message.author.id), MEM.note_exchange,
                                  touch=False)
            except Exception:                            # noqa: BLE001
                log.debug("[community] could not record the exchange",
                          exc_info=True)

    @commands.Cog.listener("on_raw_reaction_add")
    async def community_reaction_xp(self, payload) -> None:
        if not payload.guild_id or not guard.listener_ok(payload.guild_id):
            return
        member = getattr(payload, "member", None)
        if member is None or getattr(member, "bot", False):
            return
        try:
            # Straight off the payload. This used to fetch the message over
            # REST for every reaction anywhere in the server, purely to learn
            # the author — and when that fetch failed it fell back to None,
            # which made self-reactions pay.
            author_id = getattr(payload, "message_author_id", None)
            if author_id is None:
                cached = discord.utils.get(
                    getattr(self.bot, "cached_messages", []) or [],
                    id=payload.message_id)
                author_id = cached.author.id if cached else None
            award = await self.xp.award_reaction(payload.guild_id, payload.user_id,
                                           payload.message_id, author_id)
            # The result was thrown away here, so a level-up earned by
            # reacting announced nothing and granted no role — and since the
            # level had already been written, no later award could re-fire it.
            if award.get("levelled"):
                channel = self.bot.get_channel(payload.channel_id)
                await self._after_award(
                    type("_Src", (), {"channel": channel, "user": member})(),
                    award, member)
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
        try:
            view.parent = await interaction.original_response()
        except Exception:                                # noqa: BLE001
            pass

    # ── /poll ────────────────────────────────────────────────────────────────
    async def may_poll(self, interaction: discord.Interaction) -> bool:
        """Staff-only by default, switchable from `/server`.

        Your spec put polls under admin permissions; v1.28 shipped them open to
        everyone by mistake. The switch means changing your mind costs a button
        press rather than a deploy.
        """
        from cogs.admin import actions as A
        if not C.get(C.K_POLL_STAFF_ONLY, True):
            return True
        if A.is_admin(interaction.user):
            return True
        message = "📊 Only staff can start polls here."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False

    @app_commands.command(
        name="poll",
        description="Ask the server something — create, close, cancel")
    async def poll(self, interaction: discord.Interaction) -> None:
        if not await guard.gate(interaction):
            return
        view = MG.PollPanel(self, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view,
                                                ephemeral=True)

    # ── /giveaway ────────────────────────────────────────────────────────────
    @app_commands.command(
        name="giveaway",
        description="Run a giveaway — create, end now, reroll, cancel")
    async def giveaway(self, interaction: discord.Interaction) -> None:
        if not await guard.gate(interaction):
            return
        from cogs.admin import actions as A
        if not A.is_admin(interaction.user):
            return await interaction.response.send_message(
                "Only staff can run giveaways.", ephemeral=True)
        view = MG.GiveawayPanel(self, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view,
                                                ephemeral=True)

    # ── /level ───────────────────────────────────────────────────────────────
    @app_commands.command(name="level",
                          description="Your community level and XP")
    @app_commands.describe(user="Whose card to show. Defaults to yours.")
    async def level(self, interaction: discord.Interaction,
                    user: Optional[discord.Member] = None) -> None:
        if not await guard.gate(interaction):
            return
        target = user or interaction.user
        card = await self.xp.card(guard.main_guild_id(), target.id)
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

    # ── ;chat ────────────────────────────────────────────────────────────────
    @commands.command(name="chat")
    async def chat_optout(self, ctx, mode: Optional[str] = None) -> None:
        """Let the bot talk to you, or don't.

        A prefix command rather than a slash one on purpose: it adds no entry
        to the global command list (`sim_panels` counts those), and somebody
        who wants the bot to stop talking to them should be able to say so in
        the channel where it just did.
        """
        want = str(mode or "").strip().lower()
        if want not in ("on", "off"):
            state = "off" if MEM.opted_out(
                await self._profile_of(ctx.author.id)) else "on"
            return await ctx.reply(
                f"Chat is **{state}** for you. Use `;chat off` to stop me "
                f"replying to you, or `;chat on` to allow it again.",
                mention_author=False)

        try:
            from utils.database import mutate_user

            def _apply(profile: dict) -> dict:
                profile[MEM.K_OPTOUT] = (want == "off")
                return profile

            await mutate_user(int(ctx.author.id), _apply, touch=False)
        except Exception:                                # noqa: BLE001
            log.exception("[community] could not save the chat preference")
            return await ctx.reply("Couldn't save that — try again in a moment.",
                                   mention_author=False)

        await ctx.reply(
            "Understood — I won't reply to you any more. `;chat on` undoes it."
            if want == "off" else "Good to have you back. I'll reply again.",
            mention_author=False)

    async def _profile_of(self, user_id) -> dict:
        try:
            from utils.database import get_user
            return await get_user(user_id) or {}
        except Exception:                                # noqa: BLE001
            return {}


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CommunityCog(bot))
