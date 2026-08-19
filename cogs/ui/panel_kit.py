"""
panel_kit.py — the select-and-Run panel, once, for every command that needs one.

Why this exists
---------------
Discord lists the subcommands of a group **flat** in the picker. `/player` with
seven subcommands is seven lines in front of every player, `/casino` another
seven, `/avatar` five, `/story` four — twenty-eight lines to reach nine
features, most of which the reader is not looking for.

`console.py` worked this out for `/admin` in v1.12 and v1.13 rebuilt it as a
`discord.ui.View`: one command, a select to choose, a Run button, a Modal for
whatever still needs typing. That left `/admin` at one line and the
player-facing commands at twenty-seven.

Rather than write that view four more times, it lives here once. `/admin`
uses it too, which is what keeps it honest — a "shared" component only one
caller uses drifts into being that caller's private code.

What the kit owns, and what it does not
---------------------------------------
It owns the **view**: the selects, the rows, the Run/Close buttons, the modal,
the confirmation step, and every Discord limit that can 400 a message.

It does not own **dispatch**. Each panel supplies a `PanelSpec` that decides
which actions exist, who may see them, and what running one does. The admin
panel dispatches through its own registry with an `owner_only` check; the
player panels invoke the prefix command that already implements the feature.
Those are genuinely different and pretending otherwise is how a shared base
class grows six flags.

The limits, in one place
------------------------
Every one of these has cost this project a live outage or a near miss:

* a select option value must be **1–100 characters** — an empty one is a 400
  (`components.…value: Must be between 1 and 100 in length`) and it took
  `;inv` down for 75% of blade holders in v96;
* a label is 1–100, a description ≤100, a placeholder ≤150;
* a select holds ≤25 options, a view ≤5 rows, a row ≤5 components;
* an embed field name must be **non-empty** — a blank one is another 400;
* a modal holds ≤5 inputs and its title is ≤45 characters.

`option()` and `PanelView` enforce all of them, so a fifth panel cannot get
them wrong by forgetting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import discord

from . import invoke as INVOKE

log = logging.getLogger("beyblade_bot.panel")

DEFAULT_TIMEOUT = 300
DEFAULT_COLOUR = 0x5865F2

# Discord's documented caps. Named rather than inlined so a future reader sees
# WHY a string is being cut at an odd number.
MAX_OPTIONS = 25
MAX_ROWS = 5
LABEL_MAX = 100
VALUE_MAX = 100
DESC_MAX = 100
PLACEHOLDER_MAX = 150
MODAL_TITLE_MAX = 45
MODAL_INPUTS_MAX = 5


def option(label: str, value: str, description: str = "", emoji=None,
           default: bool = False) -> discord.SelectOption:
    """One select option, clamped to Discord's limits.

    Raises on an empty value rather than shipping one: a blank value is a 400
    that takes down the whole message, and a panel that silently renders an
    unusable option is worse than one that fails in the test suite.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("select option value cannot be empty — Discord 400s")
    label = (label or value).strip() or value
    return discord.SelectOption(
        label=label[:LABEL_MAX],
        value=value[:VALUE_MAX],
        description=(description or "").strip()[:DESC_MAX] or None,
        emoji=emoji or None,
        default=default)


@dataclass(frozen=True)
class PanelAction:
    """One row of a panel's action select.

    Exactly one of `handler` and `invoke` should be set. `invoke` names a
    PREFIX command — the feature is already implemented there, and a second
    copy behind the slash surface is how the two quietly drift apart. That is
    not a hypothetical: it is written on four separate cogs in this repo as
    the reason their slash entry points delegate.
    """
    key: str
    label: str
    description: str = ""
    emoji: Optional[str] = None
    category: str = ""
    needs: tuple[str, ...] = ()
    confirm: str = ""
    long_text: bool = False
    # Names for the modal's text boxes. `/trade` asks for two blade names, and
    # a modal labelled "Text" twice is a coin flip for the player — the panel
    # cannot recover from the two being swapped, because both are valid blade
    # names and the swap only shows up as "you don't own that".
    text_label: str = ""
    text2_label: str = ""
    draft: Optional[Callable[[], str]] = None
    handler: Optional[Callable] = None
    invoke: str = ""
    kwargs: dict = field(default_factory=dict)
    # Maps a PARAMETER of the prefix command to one of this panel's inputs:
    #   {"member": "user", "levels": "amount", "avatar": "text"}
    # Named rather than positional on purpose. `;avatarupgrade` takes
    # (avatar, levels) while the panel collects (amount, text), so appending
    # inputs in a fixed order would hand it the level count as the card name
    # and the card name as the level count — silently, with no error.
    binds: dict = field(default_factory=dict)


class PanelSpec:
    """What a panel is. Subclass and override; the view does the rest."""

    title: str = "Panel"
    colour: int = DEFAULT_COLOUR
    timeout: int = DEFAULT_TIMEOUT
    # The MENU is always private — it is a chooser, not a result, and a menu
    # in the channel is litter. `public` is about the OUTPUT, which is a
    # separate question: a leaderboard nobody else can see is pointless, and a
    # trade offer only the sender can see is unacceptable, because only the
    # TARGET may press Accept.
    ephemeral: bool = True
    public: bool = False
    footer: str = ""
    placeholder: str = "Pick one…"

    def may_open(self, user) -> bool:
        return True

    def categories(self, user) -> list[tuple[str, str, str]]:
        """`[(key, label, emoji)]`, or `[]` for a single-level panel.

        Only `/admin` has enough actions to need a second row; the player
        panels are four to ten actions and a category row on those would be a
        click that answers nothing.
        """
        return []

    def actions(self, category: str, user) -> list[PanelAction]:
        raise NotImplementedError

    def intro(self, panel: "PanelView") -> str:
        """Optional line under the title."""
        return ""

    async def execute(self, action: PanelAction, panel: "PanelView",
                      interaction: discord.Interaction) -> None:
        raise NotImplementedError


def guard(fn):
    """Wrap a component callback so a failure cannot freeze the panel.

    The v1.07 boss-battle hang was a callback with a `finally` and no `except`:
    the state advanced, the message never did, and the fight sat there with
    live buttons pointing at a state that no longer existed.
    """
    async def wrapper(self, interaction: discord.Interaction):
        try:
            return await fn(self, interaction)
        except Exception as exc:                         # noqa: BLE001
            log.exception("[panel] %s failed", fn.__name__)
            try:
                from utils import errorlog
                errorlog.record(f"panel:{fn.__name__}", exc)
            except Exception:                            # noqa: BLE001
                pass
            note = f"⚠️ `{type(exc).__name__}: {exc}`"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(note, ephemeral=True)
                else:
                    await interaction.response.send_message(note, ephemeral=True)
            except Exception:                            # noqa: BLE001
                pass
    wrapper.__name__ = fn.__name__
    return wrapper


# ══════════════════════════════════════════════════════════════════════════════
#  Components
# ══════════════════════════════════════════════════════════════════════════════

class CategorySelect(discord.ui.Select):
    def __init__(self, panel: "PanelView"):
        cats = panel.spec.categories(panel.invoker)
        options = [
            option(label, key, f"{len(panel.spec.actions(key, panel.invoker))} action(s)",
                   emoji, default=(key == panel.category))
            for key, label, emoji in cats[:MAX_OPTIONS]
        ] or [option("Nothing available", "none", "You can't run any action.")]
        super().__init__(placeholder="Pick a category…"[:PLACEHOLDER_MAX],
                         min_values=1, max_values=1, row=0, options=options)
        self.panel = panel

    @guard
    async def callback(self, interaction: discord.Interaction):
        self.panel.category = self.values[0]
        # A new category means the old action, and everything typed for it, no
        # longer applies. Carrying an amount over from the last action is how
        # you give somebody 5,000 of the wrong thing.
        self.panel.reset_selection()
        await self.panel.refresh(interaction)


class ActionSelect(discord.ui.Select):
    def __init__(self, panel: "PanelView"):
        acts = panel.spec.actions(panel.category, panel.invoker)
        options = [
            option(a.label,
                   a.key,
                   ("⚠️ " if a.confirm else "") + a.description,
                   a.emoji,
                   default=(a.key == panel.action_key))
            # A 26th action degrades this page rather than 400-ing the panel.
            for a in acts[:MAX_OPTIONS]
        ]
        super().__init__(placeholder=panel.spec.placeholder[:PLACEHOLDER_MAX],
                         min_values=1, max_values=1,
                         row=(1 if panel.has_categories else 0),
                         options=options or [option("Nothing here", "none")],
                         disabled=not options)
        self.panel = panel

    @guard
    async def callback(self, interaction: discord.Interaction):
        self.panel.action_key = self.values[0]
        self.panel.pending_confirm = False
        await self.panel.refresh(interaction)


class TargetSelect(discord.ui.UserSelect):
    """A real player picker — the thing six generic text parameters could not do."""

    def __init__(self, panel: "PanelView"):
        super().__init__(placeholder="Pick a player…", min_values=1,
                         max_values=1, row=panel.picker_row)
        self.panel = panel

    @guard
    async def callback(self, interaction: discord.Interaction):
        user = self.values[0]
        self.panel.target = user
        self.panel.target_id = user.id
        # Changing who it applies to invalidates a confirmation given for
        # somebody else.
        self.panel.pending_confirm = False
        await self.panel.refresh(interaction)


class ChannelPicker(discord.ui.ChannelSelect):
    """Shares the picker row with TargetSelect — no action needs both."""

    def __init__(self, panel: "PanelView"):
        super().__init__(placeholder="Pick a channel…", min_values=1,
                         max_values=1, row=panel.picker_row,
                         channel_types=[discord.ChannelType.text,
                                        discord.ChannelType.news])
        self.panel = panel

    @guard
    async def callback(self, interaction: discord.Interaction):
        self.panel.channel = self.values[0]
        self.panel.pending_confirm = False
        await self.panel.refresh(interaction)


class GuildPicker(discord.ui.Select):
    """Which server a report is about. Shares the picker row.

    Discord has no guild-select component — there is `UserSelect`,
    `RoleSelect`, `ChannelSelect` and nothing for servers — so this is a plain
    Select built from the servers the bot is actually in.

    "All servers" is always first and always the default, which is what makes
    this a filter rather than a required input: the action runs with no
    selection and answers the bot-wide question, exactly as it did before.

    A select holds 25 options. Past that the list shows the busiest 24 by
    recorded activity, because a truncated list ordered by nothing useful is
    how a filter becomes a lottery.
    """

    ALL = "all"

    def __init__(self, panel: "PanelView"):
        guilds = list(getattr(panel.bot, "guilds", None) or [])
        try:
            from utils import activity
            order = {gid: i for i, gid in enumerate(activity.guilds_seen())}
        except Exception:                                # noqa: BLE001
            order = {}
        guilds.sort(key=lambda g: (order.get(g.id, 10 ** 6),
                                   -(g.member_count or 0)))
        guilds = guilds[:MAX_OPTIONS - 1]

        opts = [option("All servers", self.ALL, "everywhere the bot is", "🌍",
                       default=(panel.guild_choice in (None, self.ALL)))]
        for g in guilds:
            opts.append(option(
                (g.name or str(g.id))[:LABEL_MAX], str(g.id),
                f"{g.member_count or 0:,} members", "🏠",
                default=(str(panel.guild_choice) == str(g.id))))
        super().__init__(placeholder="Which server?", min_values=1,
                         max_values=1, options=opts, row=panel.picker_row)
        self.panel = panel

    @guard
    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        self.panel.guild_choice = None if value == self.ALL else int(value)
        self.panel.pending_confirm = False
        await self.panel.refresh(interaction)


class InputModal(discord.ui.Modal):
    """Asks only for the fields the chosen action declared."""

    def __init__(self, panel: "PanelView", action: PanelAction):
        super().__init__(title=(action.label or "Input")[:MODAL_TITLE_MAX])
        self.panel = panel
        self.action = action
        self.amount_field: Optional[discord.ui.TextInput] = None
        self.text_field: Optional[discord.ui.TextInput] = None
        self.text2_field: Optional[discord.ui.TextInput] = None

        if "amount" in action.needs:
            self.amount_field = discord.ui.TextInput(
                label="Amount", placeholder="a whole number",
                default=(str(panel.amount) if panel.amount is not None else None),
                max_length=20, required=True)
            self.add_item(self.amount_field)
        if "text" in action.needs:
            # An announcement is a paragraph; a reason is a line. `long` also
            # gives the composer a box you can actually write in on a phone.
            draft = panel.text
            if draft is None and action.draft is not None:
                try:
                    draft = action.draft()
                except Exception:                        # noqa: BLE001
                    draft = None
            self.text_field = discord.ui.TextInput(
                label=(action.text_label or
                       ("Message" if action.long_text else "Text")),
                placeholder=(action.description or "")[:PLACEHOLDER_MAX] or None,
                default=draft or None,
                style=(discord.TextStyle.paragraph if action.long_text
                       else discord.TextStyle.short),
                max_length=(1800 if action.long_text else 300), required=True)
            self.add_item(self.text_field)
        if "text2" in action.needs:
            self.text2_field = discord.ui.TextInput(
                label=(action.text2_label or "Second value"),
                default=(panel.text2 or None),
                style=discord.TextStyle.short, max_length=300, required=True)
            self.add_item(self.text2_field)

    # Guarded like every other callback. It was NOT, and that is why `/trade`
    # failed in complete silence for three versions: the Run button's failure
    # at least printed something, while a modal that raises just closes.
    @guard
    async def on_submit(self, interaction: discord.Interaction):
        if self.amount_field is not None:
            raw = str(self.amount_field.value).strip().replace(",", "")
            try:
                self.panel.amount = int(float(raw))
            except ValueError:
                return await interaction.response.send_message(
                    f"`{raw}` isn't a number.", ephemeral=True)
        if self.text_field is not None:
            self.panel.text = str(self.text_field.value).strip()
        if self.text2_field is not None:
            self.panel.text2 = str(self.text2_field.value).strip()
        await self.panel.fire(interaction)


class RunButton(discord.ui.Button):
    def __init__(self, panel: "PanelView"):
        action = panel.selected()
        confirming = panel.pending_confirm and action is not None
        super().__init__(
            label=("Yes — do it" if confirming else "Run"),
            emoji=("⚠️" if confirming else "▶️"),
            style=(discord.ButtonStyle.danger if confirming
                   else discord.ButtonStyle.success),
            row=panel.button_row, disabled=(action is None))
        self.panel = panel

    @guard
    async def callback(self, interaction: discord.Interaction):
        panel = self.panel
        action = panel.selected()
        if action is None:
            return await interaction.response.defer()

        # A destructive action gets a second press, on a button that already
        # names what it is about to do and to whom.
        if action.confirm and not panel.pending_confirm:
            panel.pending_confirm = True
            return await panel.refresh(interaction)

        # A modal must be the FIRST response to an interaction — it cannot be
        # sent as a followup — so "does this need typing?" is answered before
        # anything else touches the response.
        if {"amount", "text", "text2"} & set(action.needs):
            return await interaction.response.send_modal(InputModal(panel, action))
        await panel.fire(interaction)


class CloseButton(discord.ui.Button):
    def __init__(self, panel: "PanelView"):
        super().__init__(label="Close", emoji="✖️",
                         style=discord.ButtonStyle.secondary,
                         row=panel.button_row)
        self.panel = panel

    @guard
    async def callback(self, interaction: discord.Interaction):
        self.panel.stop()
        for child in self.panel.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Panel closed.", embed=None, view=self.panel)


# ══════════════════════════════════════════════════════════════════════════════
#  The view
# ══════════════════════════════════════════════════════════════════════════════

class PanelView(discord.ui.View):
    def __init__(self, spec: PanelSpec, bot, invoker, guild, channel):
        super().__init__(timeout=spec.timeout)
        self.spec = spec
        self.bot = bot
        self.invoker = invoker
        self.guild = guild
        self.channel = channel
        # Where the panel was opened. A channel action overwrites `channel`
        # with the picker's choice, which is why `reset_selection` puts it back.
        self.home_channel = channel

        self.has_categories = bool(spec.categories(invoker))
        cats = spec.categories(invoker)
        self.category = cats[0][0] if cats else ""
        self.action_key: Optional[str] = None
        self.target = None
        self.target_id: Optional[int] = None
        self.amount: Optional[int] = None
        self.text: Optional[str] = None
        self.text2: Optional[str] = None
        # None means "all servers" — the filter's default, not a missing value.
        self.guild_choice: Optional[int] = None
        self.pending_confirm = False
        self.message = None
        self.build()

    # ── rows ─────────────────────────────────────────────────────────────────
    @property
    def picker_row(self) -> int:
        return 2 if self.has_categories else 1

    @property
    def button_row(self) -> int:
        return 3 if self.has_categories else 2

    # ── state ────────────────────────────────────────────────────────────────
    def selected(self) -> Optional[PanelAction]:
        """The chosen action, looked up across every page this user can see.

        Deliberately not scoped to the open category. Scoping it means an
        action key that belongs to another page resolves to None, and a None
        here does not raise — Run just quietly does nothing. "The button did
        nothing" is the single worst failure a panel can have, because it is
        indistinguishable from the bot being down.
        """
        if not self.action_key:
            return None
        cats = [c[0] for c in self.spec.categories(self.invoker)] or [self.category]
        for cat in dict.fromkeys([self.category] + cats):
            for a in self.spec.actions(cat, self.invoker):
                if a.key == self.action_key:
                    return a
        return None

    def reset_selection(self) -> None:
        self.action_key = None
        self.amount = None
        self.text = None
        self.text2 = None
        self.guild_choice = None
        self.channel = self.home_channel
        self.pending_confirm = False

    # ── layout ───────────────────────────────────────────────────────────────
    def build(self) -> None:
        self.clear_items()
        if self.has_categories:
            self.add_item(CategorySelect(self))
        self.add_item(ActionSelect(self))
        action = self.selected()
        # The picker row only exists for actions that take a player or a
        # channel. Showing an inert picker on every action is how a user
        # learns to ignore it.
        if action is not None and "server" in action.needs:
            self.add_item(GuildPicker(self))
        elif action is not None and "user" in action.needs:
            self.add_item(TargetSelect(self))
        elif action is not None and "channel" in action.needs:
            self.add_item(ChannelPicker(self))
        self.add_item(RunButton(self))
        self.add_item(CloseButton(self))

    def embed(self) -> discord.Embed:
        action = self.selected()
        e = discord.Embed(title=self.spec.title, colour=self.spec.colour)
        intro = self.spec.intro(self)
        if intro:
            e.description = intro

        if self.has_categories:
            emoji, label = next(
                ((em, la) for k, la, em in self.spec.categories(self.invoker)
                 if k == self.category), ("", self.category))
            # An embed field name must be non-empty — a blank one is a 400.
            e.add_field(name="Category",
                        value=f"{emoji} **{label}**".strip() or "—", inline=True)
        e.add_field(name="Action",
                    value=(f"**{action.label}**" if action else "*pick one*"),
                    inline=True)

        if action is not None:
            if action.description:
                e.description = action.description
            wants = []
            if "user" in action.needs:
                wants.append("👤 player — " + (
                    self.target.mention if self.target is not None
                    else "*not picked*"))
            if "server" in action.needs:
                g = None
                if self.guild_choice is not None:
                    g = self.bot.get_guild(self.guild_choice)
                wants.append("🌍 server — " + (
                    f"**{g.name}**" if g is not None else
                    (f"`{self.guild_choice}`" if self.guild_choice
                     else "*all servers*")))
            if "channel" in action.needs:
                where = (self.channel.mention if self.channel is not None
                         else "*none*")
                here = (" *(here — pick another below)*"
                        if self.channel is self.home_channel else "")
                wants.append(f"📣 channel — {where}{here}")
            if "amount" in action.needs:
                wants.append("🔢 amount — " + (
                    f"**{self.amount:,}**" if self.amount is not None
                    else "*asked on Run*"))
            if "text" in action.needs:
                wants.append(f"✏️ {action.text_label or 'text'} — " + (
                    f"`{self.text}`" if self.text else "*asked on Run*"))
            if "text2" in action.needs:
                wants.append(f"✏️ {action.text2_label or 'second value'} — " + (
                    f"`{self.text2}`" if self.text2 else "*asked on Run*"))
            if wants:
                e.add_field(name="Needs", value="\n".join(wants), inline=False)
            if action.confirm:
                e.add_field(name="⚠️ Destructive", value=action.confirm,
                            inline=False)

        if self.pending_confirm and action is not None:
            who = self.target.mention if self.target is not None else "everyone"
            e.colour = 0xED4245
            e.add_field(
                name="Confirm",
                value=(f"**{action.label}** → {who}\n{action.confirm}\n"
                       f"Press **Yes — do it** to go ahead."),
                inline=False)
        if self.spec.footer:
            e.set_footer(text=self.spec.footer[:2048])
        return e

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def fire(self, interaction: discord.Interaction) -> None:
        action = self.selected()
        if action is None:
            return
        self.pending_confirm = False
        await self.spec.execute(action, self, interaction)

    # ── lifetime ─────────────────────────────────────────────────────────────
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only whoever opened it may drive it.

        An ephemeral message is a display property, not a permission, and a
        leaked component id is not a reason to hand over somebody else's panel.
        """
        if interaction.user.id != getattr(self.invoker, "id", None):
            await interaction.response.send_message("Not your panel.",
                                                    ephemeral=True)
            return False
        if not self.spec.may_open(interaction.user):
            await interaction.response.send_message("Not authorized.",
                                                    ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()


# ══════════════════════════════════════════════════════════════════════════════
#  Running a prefix command from a panel
# ══════════════════════════════════════════════════════════════════════════════

class PrefixSpec(PanelSpec):
    """A panel whose actions each run an existing prefix command.

    Every player-facing slash surface in this bot already delegated this way,
    with the same `_run` helper copy-pasted into four cogs: the prefix command
    owns the validation, the cooldowns and the rendering, and a second copy
    behind the slash entry point is how the two paths drift. The panel changes
    how an action is CHOSEN, not what it does.
    """

    ACTIONS: Sequence[PanelAction] = ()

    def actions(self, category: str, user) -> list[PanelAction]:
        return list(self.ACTIONS)

    async def execute(self, action: PanelAction, panel: "PanelView",
                      interaction: discord.Interaction) -> None:
        if action.handler is not None:
            return await action.handler(panel, interaction)

        cmd = panel.bot.get_command(action.invoke)
        if cmd is None:
            return await _reply(interaction,
                                f"`{action.invoke}` isn't loaded right now.")
        supplied = {"user": panel.target, "amount": panel.amount,
                    "text": panel.text, "text2": panel.text2}
        kwargs = dict(action.kwargs)
        for param, source in action.binds.items():
            value = supplied.get(source)
            # An unfilled optional input is left OUT rather than passed as
            # None, so the command's own default applies — `;rank` with no
            # member means your own card, and passing None explicitly would
            # only work by luck of the signature.
            if value not in (None, ""):
                kwargs[param] = value

        # `Context.from_interaction` used to be here and could never work: it
        # raises for any interaction that is not an application command, which
        # is every button, every select and every modal submit. See
        # `cogs/ui/invoke.py` for the whole story.
        #
        # `run_prefix_command` also re-runs the command's checks. `ctx.invoke`
        # skips them, and the tree gate in onboarding.py only fires for
        # application commands — so without this a panel opened before a ban
        # stays live and usable afterwards.
        await INVOKE.run_prefix_command(
            interaction, cmd, bot=panel.bot,
            visibility=(INVOKE.PUBLIC if self.public else INVOKE.PRIVATE),
            author=panel.invoker, channel=panel.home_channel, **kwargs)


async def _reply(interaction: discord.Interaction, message: str) -> None:
    await INVOKE.reply(interaction, message)
