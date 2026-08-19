"""
invoke.py — running an existing `;` command on behalf of an interaction.

Why this file exists
--------------------
`commands.Context.from_interaction` **cannot** be used from a button, a select
or a modal. discord.py says so in one line (`ext/commands/context.py:256-258`):

    command = interaction.command
    if command is None:
        raise ValueError('interaction does not have command data')

and `Interaction.command` returns None for anything that is not an application
command or an autocomplete (`discord/interactions.py:362-363`). A button press
is `InteractionType.component`; a modal submit is `InteractionType.modal_submit`.
Neither ever carries command data.

Every panel action that ran a prefix command therefore raised, every time,
from v1.14 until this file existed. `guard` turned the Run-button ones into an
ephemeral "⚠️ ValueError: interaction does not have command data" and the modal
ones — `InputModal.on_submit` was not guarded — into nothing at all.

The trap behind the trap
------------------------
Making `from_interaction` accept a component interaction would not have been a
fix either. It only builds its synthetic message `if interaction.message is
None` (`context.py:262`); for a component interaction `interaction.message` is
the panel message, which the **bot** authored, so `ctx.author` resolves to the
bot. `;profile` would render the bot's card and `;trade` would read the bot's
inventory — quietly, with no error anywhere. So this module builds the
synthetic message itself and puts the *clicker* in it.

Why the interaction is kept on the Context
------------------------------------------
`ctx.interaction = None` would make `Context.send` fall through to a plain
channel send, which is one of the two things we want — but it also turns
`ctx.typing()` into a real 5-second HTTP typing loop, makes `ctx.reply()` carry
a `reference` to a message id Discord has never heard of, and drops
`ctx.permissions` back to a cache lookup. So the interaction stays, and `send`
is overridden instead. One lever, one effect.

Where output goes
-----------------
`PUBLIC`  → a normal channel message, exactly like typing the `;` command.
`PRIVATE` → an ephemeral followup, only the person who clicked.

`PUBLIC` is a channel send rather than a non-ephemeral followup on purpose.
A followup returns a `WebhookMessage` whose edits ride the interaction token
and **expire after 15 minutes**; `;trade` edits its offer to drop the buttons
(`cogs/extras/trade.py:161`) and a story battle edits for far longer than that.
A channel send returns a real `Message`, editable for as long as the bot lives,
and registers its View under the real message id so that a *different* user —
the trade target, who is the only one allowed to press Accept — actually
reaches the callback.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands
from discord.ext.commands.view import StringView

log = logging.getLogger("beyblade_bot.invoke")

PUBLIC = "public"
PRIVATE = "private"

# The prefix the player would have typed. NOT "/" — `from_interaction` uses
# that, and `cogs/spawn/spawn.py` interpolates `ctx.prefix` into text it shows
# players, where "/" would print instructions nobody can follow.
PREFIX = ";"

# Mirrors discord.py's own synthetic payload (`context.py:263-276`). Every key
# here is read by `Message.__init__`; `mentions` in particular, because
# `cogs/extras/achievements.py` reads `ctx.message.mentions` and a missing key
# means the attribute is never created at all rather than defaulting to [].
_SYNTHETIC = {
    "reactions": [], "embeds": [], "attachments": [],
    "mentions": [], "mention_roles": [], "mention_everyone": False,
    "tts": False, "pinned": False, "edited_timestamp": None,
    "type": 0, "flags": 64, "content": "",
}

# `Webhook.send` does not accept these, and neither does `Context.send` on its
# interaction path. Dropped rather than raised on: a TypeError inside a command
# that has already written to a profile is worse than a message without a
# sticker.
_WEBHOOK_DROPS = ("reference", "mention_author", "stickers", "nonce",
                  "delete_after")


def resolve_channel(interaction: discord.Interaction, fallback=None):
    """The real channel this ran in.

    `interaction.channel_id` is derived from `interaction.channel`
    (`discord/interactions.py:325-327`), so the `PartialMessageable` branch
    `from_interaction` falls back on cannot actually fire. The honest chain is
    the interaction, then the message the component sits on, then wherever the
    panel was opened.
    """
    return (interaction.channel
            or getattr(interaction.message, "channel", None)
            or fallback)


def synthetic_message(interaction: discord.Interaction, channel,
                      author) -> discord.Message:
    """A Message that exists only to carry an author, a channel and a state.

    The author is the CLICKER, deliberately — see the module docstring.
    """
    data = dict(_SYNTHETIC, id=interaction.id)
    message = discord.Message(state=interaction._state, channel=channel,
                              data=data)
    # Assigned rather than supplied in the payload: with no `author` key and no
    # assignment, `Message.__init__` never creates the attribute at all, and
    # the first of ~300 `ctx.author` reads dies with AttributeError.
    message.author = author
    return message


class Outlet:
    """Where a command invoked from an interaction sends its output.

    Owns the acknowledgement as well as the sending, because the two are one
    decision: an ephemeral `thinking` defer pins the first followup ephemeral
    whatever that followup asks for, and a `thinking` defer that nothing fills
    rots into "the application did not respond".
    """

    def __init__(self, interaction: discord.Interaction, *, channel,
                 visibility: str, author) -> None:
        self.interaction = interaction
        self.channel = channel
        self.visibility = visibility
        self.author = author
        self.sent = 0
        self.thinking = False

    @property
    def public(self) -> bool:
        return self.visibility == PUBLIC

    async def ack(self) -> None:
        """Acknowledge before the command body runs, not after.

        Three interaction types, three right answers:

        * component / modal, PUBLIC — `defer()` sends `deferred_message_update`
          (type 6): acknowledges without a spinner and without pinning
          anything. The output arrives as a channel message.
        * component / modal, PRIVATE — `defer(ephemeral=True, thinking=True)`.
          Note that `thinking=True` is required for `ephemeral` to mean
          anything here: discord.py only attaches the flag inside
          `if thinking and ephemeral`, so a bare `defer(ephemeral=True)` on a
          component silently sends type 6 and drops the flag.
        * application command — type 6 does not exist for slash commands;
          `defer` always sends `deferred_channel_message`. So there is always a
          placeholder, and `send` has to fill it.
        """
        if self.interaction.response.is_done():
            return
        try:
            if self.interaction.type is discord.InteractionType.application_command:
                await self.interaction.response.defer(
                    ephemeral=not self.public, thinking=True)
                self.thinking = True
            elif self.public:
                await self.interaction.response.defer()
            else:
                await self.interaction.response.defer(ephemeral=True,
                                                      thinking=True)
                self.thinking = True
        except discord.InteractionResponded:
            pass

    async def send(self, content=None, **kwargs):
        expired = self.interaction.is_expired()

        if self.public:
            # The first send of an application command has to fill the public
            # placeholder `defer` left, or it turns into "the application did
            # not respond". Everything after that goes to the channel, where it
            # is permanent and editable.
            if self.thinking and not self.sent and not expired:
                self.sent += 1
                return await self._followup(content, kwargs, ephemeral=False)
            self.sent += 1
            return await self._channel(content, kwargs)

        if expired:
            # Fifteen minutes gone and no private transport left. Say it in the
            # channel rather than do nothing: a story battle silently stopping
            # mid-fight is worse than one public line, and nothing routed
            # PRIVATE here is secret.
            log.warning("[invoke] interaction expired; falling back to the "
                        "channel for %s", getattr(self.author, "id", "?"))
            self.sent += 1
            mention = getattr(self.author, "mention", "")
            if content:
                content = f"{mention} {content}".strip()
            elif mention:
                content = mention
            return await self._channel(content, kwargs)

        self.sent += 1
        return await self._followup(content, kwargs, ephemeral=True)

    async def _channel(self, content, kwargs):
        kwargs.pop("ephemeral", None)
        return await self.channel.send(content, **kwargs)

    async def _followup(self, content, kwargs, *, ephemeral: bool):
        payload = {k: v for k, v in kwargs.items() if k not in _WEBHOOK_DROPS}
        # The caller's own `ephemeral=` is deliberately ignored. `Context.send`
        # defaults it to False on every call (`context.py:1136`), so a command
        # that sends three messages would leak two of them.
        payload["ephemeral"] = ephemeral
        return await self.interaction.followup.send(content, wait=True,
                                                    **payload)

    async def close(self) -> None:
        """Fill an unfilled placeholder.

        A `thinking` defer is a promise that a message is coming. A command
        that returns without sending one — an early `return` on a validation
        branch, say — would otherwise leave a spinner forever.
        """
        if self.sent or not self.thinking:
            return
        try:
            await self.interaction.followup.send("Nothing to show.",
                                                 ephemeral=not self.public)
        except Exception as exc:                         # noqa: BLE001
            log.debug("[invoke] could not close the placeholder: %s", exc)


class InvokedContext(commands.Context):
    """A Context for a prefix command reached through an interaction.

    `send` is the only override, and it is the whole point: `Context.send`
    decides where output goes from `self.interaction` alone and re-defaults
    `ephemeral` to False on every single call. The Outlet decides instead, once.
    """

    outlet: Optional[Outlet] = None

    async def send(self, content=None, **kwargs):
        if self.outlet is None:                          # pragma: no cover
            return await super().send(content, **kwargs)
        return await self.outlet.send(content, **kwargs)


async def build_context(interaction: discord.Interaction, command, *, bot,
                        author=None, channel=None,
                        outlet: Optional[Outlet] = None) -> InvokedContext:
    """A Context for `command`, authored by the player who clicked.

    Works for all three interaction types, which is the point — one code path
    for panel buttons, modal submits and plain slash commands, so there is one
    place to be right.
    """
    author = author or interaction.user
    channel = channel or resolve_channel(interaction)
    if channel is None:
        raise RuntimeError("interaction has no resolvable channel")

    ctx = InvokedContext(
        message=synthetic_message(interaction, channel, author),
        bot=bot,
        view=StringView(""),
        args=[], kwargs={},
        prefix=PREFIX,
        command=command,
        invoked_with=getattr(command, "name", None),
        interaction=interaction,
    )
    # `Context.guild` is a cached_property over `message.guild`, which comes
    # from the channel. Set explicitly so an uncached guild cannot silently
    # produce `ctx.guild is None`: `;trade` writes `ctx.guild.id` into the
    # trade log and `;leaderboard` resolves every display name through it,
    # where a None degrades the whole board to "User 956773141265391676"
    # without raising anything.
    ctx.guild = interaction.guild or getattr(channel, "guild", None)
    ctx.outlet = outlet
    interaction._baton = ctx
    return ctx


async def run_prefix_command(interaction: discord.Interaction, command, *, bot,
                             visibility: str = PRIVATE, author=None,
                             channel=None, check: bool = True, **kwargs):
    """Acknowledge, build a Context, run the command, tidy up.

    `check` runs the command's own checks. `ctx.invoke` skips them by design,
    and the tree-level `;start`/ban/maintenance gate only fires for application
    commands — `CommandTree.interaction_check` is never called for a component
    — so a panel opened before a ban stays live and usable without this. It
    also restores `@commands.guild_only()` on `;trade`.
    """
    author = author or interaction.user
    channel = channel or resolve_channel(interaction)
    if channel is None:
        return await reply(interaction, "I can't tell which channel this is.")

    outlet = Outlet(interaction, channel=channel, visibility=visibility,
                    author=author)
    await outlet.ack()

    ctx = await build_context(interaction, command, bot=bot, author=author,
                              channel=channel, outlet=outlet)
    try:
        if check:
            try:
                if not await command.can_run(ctx):
                    await outlet.send("You can't use that here.")
                    return
            except commands.CommandError as exc:
                await outlet.send(f"❌ {exc}")
                return
        await ctx.invoke(command, **kwargs)
    finally:
        await outlet.close()


async def reply(interaction: discord.Interaction, message: str) -> None:
    """A short ephemeral note that is not command output."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception as exc:                             # noqa: BLE001
        log.debug("[invoke] could not reply: %s", exc)
