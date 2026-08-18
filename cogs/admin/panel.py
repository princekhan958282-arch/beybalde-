"""
panel.py — one `/admin` command, and the three prefix commands that survive.

The shape
---------
`/admin` opens an ephemeral `discord.ui.View`:

    row 0   category select      💰 Economy · 🎁 Content · 👤 Players · …
    row 1   action select        filtered to that category
    row 2   UserSelect           only for actions that take a player
    row 3   Run · Cancel

and Run either fires immediately or opens a `Modal` for whatever numbers or
text the action still needs.

Why this and not slash parameters
---------------------------------
`console.py` put every action behind one command with six fixed, generic
parameters — `target/user/user2/amount/text/extra` — because Discord lists
subcommands FLAT in the picker and thirteen admin subcommands means thirteen
lines in front of every player. The single command was right; the parameters
were not. They fit no action exactly (three were dead weight until v1.12
deleted them), and a player id had to be typed in as text. A view has none of
those constraints: the user picker is a real `UserSelect`, and each action asks
only for what it actually needs.

Why three prefix commands stay
------------------------------
`;sync`, `;reload` and `;version` are the recovery tools. If slash registration
is broken or a cog failed to load, a slash-only `/admin sync` cannot fix it and
the only route left is re-uploading the zip. They are mirrored by the rule the
tournament rewrite established: **one implementation, two entry points**. Both
`;sync` and the panel's sync action call `actions.do_sync`. Two modules owning
one command name is what produced `CommandAlreadyRegistered`; two entry points
on one function does not.

Destructive actions confirm
---------------------------
`;resetplayer` wiped a profile on one line with no confirmation, and
`;giveallcoins` touched every row the same way. Anything with `confirm` set in
the registry now needs a second press on a button that names what is about to
happen and to whom.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from . import actions as A

log = logging.getLogger("beyblade_bot.admin")

PANEL_TIMEOUT = 300
COLOUR = 0x5865F2


def _guard(fn):
    """Wrap a component callback so a failure cannot freeze the panel.

    The v1.07 boss-battle hang was a callback with a `finally` and no `except`:
    the state advanced, the message never did, and the fight sat there with
    live buttons pointing at a state that no longer existed. Every callback in
    this file is wrapped from the start rather than after the bug report.
    """
    async def wrapper(self, interaction: discord.Interaction):
        try:
            return await fn(self, interaction)
        except Exception as exc:                         # noqa: BLE001
            log.exception("[admin] panel %s failed", fn.__name__)
            try:
                from utils import errorlog
                errorlog.record(f"admin-panel:{fn.__name__}", exc)
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


async def _send(interaction: discord.Interaction, result: A.Result) -> None:
    """Put a Result on screen, whatever shape it came back in."""
    # A handler that pages (the server list) hands back `embeds`; everything
    # else hands back at most one. Discord takes up to ten per message, and a
    # longer list is sent as follow-ups rather than silently truncated.
    embeds = list(result.embeds) if result.embeds else (
        [result.embed] if result.embed is not None else [])
    kwargs: dict = {}
    if embeds:
        kwargs["embeds"] = embeds[:10]
    if result.file is not None:
        kwargs["file"] = result.file
    content = result.message or ("" if embeds else "Done.")
    if content:
        kwargs["content"] = content

    if interaction.response.is_done():
        await interaction.followup.send(ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(ephemeral=True, **kwargs)
    for chunk in (embeds[i:i + 10] for i in range(10, len(embeds), 10)):
        await interaction.followup.send(embeds=chunk, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  Components
# ══════════════════════════════════════════════════════════════════════════════

class CategorySelect(discord.ui.Select):
    def __init__(self, panel: "AdminPanel"):
        # Values are non-empty ASCII, never "". An empty select value is a
        # Discord 400 — `components.…value: Must be between 1 and 100 in
        # length` — and it took `;inv` down for 75% of blade holders in v96.
        super().__init__(
            placeholder="Pick a category…", min_values=1, max_values=1, row=0,
            # Filtered to what this admin may actually run. A role-holder sees
            # the tournament page and nothing else — see `owner_only` in
            # actions.py for why that default is what it is.
            options=[
                discord.SelectOption(
                    label=label, value=key, emoji=emoji,
                    description=f"{len(A.actions_in(key, panel.invoker))} action(s)",
                    default=(key == panel.category))
                for key, label, emoji in A.categories_for(panel.invoker)
            ] or [discord.SelectOption(label="Nothing available", value="none",
                                       description="You can't run any action.")])
        self.panel = panel

    @_guard
    async def callback(self, interaction: discord.Interaction):
        self.panel.category = self.values[0]
        # A new category means the old action, and everything typed for it, no
        # longer applies. Carrying an amount over from the last action is how
        # you give somebody 5,000 of the wrong thing.
        self.panel.reset_selection()
        await self.panel.refresh(interaction)


class ActionSelect(discord.ui.Select):
    def __init__(self, panel: "AdminPanel"):
        acts = A.actions_in(panel.category, panel.invoker)
        options = [
            discord.SelectOption(
                label=a.label[:100], value=a.key[:100],
                description=(("⚠️ " if a.confirm else "") + a.description)[:100],
                default=(a.key == panel.action_key))
            # Discord caps a select at 25 options. No category is near it, but
            # the slice means adding a 26th action degrades the page instead of
            # 400-ing the whole panel.
            for a in acts[:25]
        ]
        super().__init__(placeholder="Pick an action…", min_values=1,
                         max_values=1, row=1, options=options,
                         disabled=not options)
        self.panel = panel

    @_guard
    async def callback(self, interaction: discord.Interaction):
        self.panel.action_key = self.values[0]
        self.panel.pending_confirm = False
        await self.panel.refresh(interaction)


class TargetSelect(discord.ui.UserSelect):
    """The thing `console.py` could not do: a real player picker."""

    def __init__(self, panel: "AdminPanel"):
        super().__init__(placeholder="Pick a player…", min_values=1,
                         max_values=1, row=2)
        self.panel = panel

    @_guard
    async def callback(self, interaction: discord.Interaction):
        user = self.values[0]
        self.panel.target = user
        self.panel.target_id = user.id
        # Changing who it applies to invalidates a confirmation given for
        # somebody else.
        self.panel.pending_confirm = False
        await self.panel.refresh(interaction)


class InputModal(discord.ui.Modal):
    """Asks only for the fields the chosen action declared."""

    def __init__(self, panel: "AdminPanel", action: A.Action):
        super().__init__(title=action.label[:45])
        self.panel = panel
        self.action = action
        self.amount_field: Optional[discord.ui.TextInput] = None
        self.text_field: Optional[discord.ui.TextInput] = None

        if "amount" in action.needs:
            self.amount_field = discord.ui.TextInput(
                label="Amount", placeholder="a whole number",
                default=(str(panel.amount) if panel.amount is not None else None),
                max_length=20, required=True)
            self.add_item(self.amount_field)
        if "text" in action.needs:
            self.text_field = discord.ui.TextInput(
                label="Text", placeholder=action.description[:100],
                default=panel.text or None,
                style=discord.TextStyle.short, max_length=300, required=True)
            self.add_item(self.text_field)

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
        await self.panel.fire(interaction)


class RunButton(discord.ui.Button):
    def __init__(self, panel: "AdminPanel"):
        action = panel.selected()
        confirming = panel.pending_confirm and action is not None
        super().__init__(
            label=("Yes — do it" if confirming else "Run"),
            emoji=("⚠️" if confirming else "▶️"),
            style=(discord.ButtonStyle.danger if confirming
                   else discord.ButtonStyle.success),
            row=3, disabled=(action is None))
        self.panel = panel

    @_guard
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
        # sent as a followup — so the "does this need typing?" question is
        # answered before anything else touches the response.
        if {"amount", "text"} & set(action.needs):
            return await interaction.response.send_modal(InputModal(panel, action))
        await panel.fire(interaction)


class CancelButton(discord.ui.Button):
    def __init__(self, panel: "AdminPanel"):
        super().__init__(label="Close", emoji="✖️",
                         style=discord.ButtonStyle.secondary, row=3)
        self.panel = panel

    @_guard
    async def callback(self, interaction: discord.Interaction):
        self.panel.stop()
        for child in self.panel.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Panel closed.", embed=None, view=self.panel)


# ══════════════════════════════════════════════════════════════════════════════
#  The panel
# ══════════════════════════════════════════════════════════════════════════════

class AdminPanel(discord.ui.View):
    def __init__(self, bot: commands.Bot, invoker, guild, channel):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.bot = bot
        self.invoker = invoker
        self.guild = guild
        self.channel = channel
        # Open on the first page this admin can actually use, not on Economy
        # — an owner-only page as the landing screen would show a role-holder
        # an empty select and no way to tell why.
        allowed = A.categories_for(invoker)
        self.category = allowed[0][0] if allowed else A.CATEGORY_ORDER[0]
        self.action_key: Optional[str] = None
        self.target = None
        self.target_id: Optional[int] = None
        self.amount: Optional[int] = None
        self.text: Optional[str] = None
        self.pending_confirm = False
        self.build()

    # ── state ────────────────────────────────────────────────────────────────
    def selected(self) -> Optional[A.Action]:
        return A.REGISTRY.get(self.action_key or "")

    def reset_selection(self) -> None:
        self.action_key = None
        self.amount = None
        self.text = None
        self.pending_confirm = False

    def ctx(self) -> A.ActionCtx:
        return A.ActionCtx(
            bot=self.bot, guild=self.guild, invoker=self.invoker,
            invoker_id=getattr(self.invoker, "id", 0),
            target=self.target, target_id=self.target_id,
            amount=self.amount, text=self.text, channel=self.channel)

    # ── layout ───────────────────────────────────────────────────────────────
    def build(self) -> None:
        self.clear_items()
        self.add_item(CategorySelect(self))
        self.add_item(ActionSelect(self))
        action = self.selected()
        # Row 2 only exists for actions that take a player. Showing an inert
        # user picker on every action is how an admin learns to ignore it.
        if action is not None and "user" in action.needs:
            self.add_item(TargetSelect(self))
        self.add_item(RunButton(self))
        self.add_item(CancelButton(self))

    def embed(self) -> discord.Embed:
        emoji, label = next(((e, l) for k, l, e in A.CATEGORIES
                             if k == self.category), ("🔧", self.category))
        n_allowed = sum(len(A.actions_in(k, self.invoker))
                        for k in A.CATEGORY_ORDER)
        action = self.selected()
        e = discord.Embed(title="🛠️  Admin", colour=COLOUR)
        e.add_field(name="Category", value=f"{emoji} **{label}**", inline=True)
        e.add_field(name="Action",
                    value=(f"**{action.label}**" if action else "*pick one*"),
                    inline=True)
        if action is not None:
            e.description = action.description
            wants = []
            if "user" in action.needs:
                who = (self.target.mention if self.target is not None
                       else "*not picked*")
                wants.append(f"👤 player — {who}")
            if "amount" in action.needs:
                wants.append("🔢 amount — "
                             + (f"**{self.amount:,}**" if self.amount is not None
                                else "*asked on Run*"))
            if "text" in action.needs:
                wants.append("✏️ text — "
                             + (f"`{self.text}`" if self.text
                                else "*asked on Run*"))
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
                value=(f"**{action.label}** → {who}\n"
                       f"{action.confirm}\nPress **Yes — do it** to go ahead."),
                inline=False)
        e.set_footer(text=f"{n_allowed} action(s) available to you · only you "
                          f"can see this · closes after "
                          f"{PANEL_TIMEOUT // 60} min")
        return e

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    # ── running ──────────────────────────────────────────────────────────────
    async def fire(self, interaction: discord.Interaction) -> None:
        action = self.selected()
        if action is None:
            return
        # Some handlers hit the network or the database on a thread; three
        # seconds is not a lot for a MySQL probe or a `fetch_guilds` sweep.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        result = await A.run(action.key, self.ctx())
        self.pending_confirm = False
        log.info("[admin] %s ran %s → %s", getattr(self.invoker, "id", "?"),
                 action.key, "ok" if result.ok else "refused")
        await _send(interaction, result)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the admin who opened it may drive it.

        The panel is ephemeral, so in practice nobody else can see it — but an
        ephemeral message is a display property, not a permission, and a leaked
        component id is not a reason to hand over the reset button.
        """
        if interaction.user.id != getattr(self.invoker, "id", None):
            await interaction.response.send_message("Not your panel.",
                                                    ephemeral=True)
            return False
        if not A.is_admin(interaction.user):
            await interaction.response.send_message("Not authorized.",
                                                    ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()


# ══════════════════════════════════════════════════════════════════════════════
#  The cog
# ══════════════════════════════════════════════════════════════════════════════

class AdminCog(commands.Cog, name="Admin"):
    """One slash panel, three prefix survivors. Everything hidden from `;help`."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        # Non-admins get a silent failure — the commands appear not to exist.
        return A.is_admin(ctx.author)

    async def cog_command_error(self, ctx: commands.Context,
                                error: Exception) -> None:
        if isinstance(error, (commands.CheckFailure,
                              commands.MissingRequiredArgument,
                              commands.BadArgument)):
            return
        log.error("[admin] %s", error, exc_info=error)

    # ── /admin ───────────────────────────────────────────────────────────────
    @app_commands.command(name="admin",
                          description="[Admin] Every admin action, in one panel")
    async def admin(self, interaction: discord.Interaction) -> None:
        if not A.is_admin(interaction.user):
            return await interaction.response.send_message("Not authorized.",
                                                           ephemeral=True)
        panel = AdminPanel(self.bot, interaction.user, interaction.guild,
                           interaction.channel)
        await interaction.response.send_message(embed=panel.embed(), view=panel,
                                                ephemeral=True)

    # ── the three recovery commands ──────────────────────────────────────────
    #
    # Prefix-only entry points onto the SAME functions the panel calls. They
    # exist because a slash-only recovery tool cannot recover slash commands.

    @commands.command(name="sync", hidden=True)
    async def sync(self, ctx: commands.Context, scope: str = "guild") -> None:
        """`;sync` · `;sync clean` · `;sync global` · `;sync purge`"""
        res = await A.do_sync(self.bot, ctx.guild, scope)
        await ctx.send(res.message or "Done.")

    @commands.command(name="reload", hidden=True)
    async def reload_cogs(self, ctx: commands.Context) -> None:
        res = await A.do_reload(self.bot)
        await ctx.send(content=res.message or None, embed=res.embed)

    @commands.command(name="version", aliases=["build", "ver"], hidden=True)
    async def version(self, ctx: commands.Context) -> None:
        res = await A.do_version(self.bot)
        await ctx.send(content=res.message or None, embed=res.embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
