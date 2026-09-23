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

import json
import logging
import os
import re
import discord
from discord import app_commands
from discord.ext import commands

from ..ui import panel_kit as K
from . import actions as A

log = logging.getLogger("beyblade_bot.admin")

PANEL_TIMEOUT = 300
COLOUR = 0x5865F2


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
    if result.view is not None:
        kwargs["view"] = result.view
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
#  The spec
# ══════════════════════════════════════════════════════════════════════════════
#
# The view itself lives in cogs/ui/panel_kit.py and is shared with /player,
# /casino, /avatar and /story. What is admin-specific — who may see an action,
# what running one does, and the update-announcement draft — is here.


class AdminSpec(K.PanelSpec):
    title = "🛠️  Admin"
    colour = COLOUR
    timeout = PANEL_TIMEOUT
    placeholder = "Pick an action…"

    def may_open(self, user) -> bool:
        return A.is_admin(user)

    def categories(self, user):
        # Filtered to what this admin may actually run. A role-holder sees the
        # tournament page and nothing else — see `owner_only` in actions.py for
        # why that default is what it is.
        return A.categories_for(user)

    def actions(self, category: str, user):
        return [_as_panel_action(a) for a in A.actions_in(category, user)]

    def intro(self, panel) -> str:
        n = sum(len(A.actions_in(k, panel.invoker)) for k in A.CATEGORY_ORDER)
        return f"-# {n} action(s) available to you"

    async def execute(self, action, panel, interaction) -> None:
        # Some handlers hit the network or the database on a thread; three
        # seconds is not a lot for a MySQL probe or a `fetch_guilds` sweep.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        ctx = A.ActionCtx(
            bot=panel.bot, guild=panel.guild, invoker=panel.invoker,
            invoker_id=getattr(panel.invoker, "id", 0),
            target=panel.target, target_id=panel.target_id,
            amount=panel.amount, text=panel.text, channel=panel.channel,
            guild_choice=panel.guild_choice)
        result = await A.run(action.key, ctx)
        log.info("[admin] %s ran %s → %s", getattr(panel.invoker, "id", "?"),
                 action.key, "ok" if result.ok else "refused")
        await _send(interaction, result)


def _as_panel_action(a: A.Action) -> K.PanelAction:
    """Registry action → panel action. One direction, no shared base class.

    The registry stays discord-light so `tools/sim_admin.py` can drive all 62
    actions with no gateway; the kit is a view. Keeping the translation to one
    small function is what lets both stay true.
    """
    return K.PanelAction(
        key=a.key, label=a.label, description=a.description,
        category=a.category, needs=a.needs, confirm=a.confirm,
        long_text=(a.category == "announce"),
        # `announce_update` opens on a draft naming the version this install is
        # actually running, rather than asking an admin to remember it.
        draft=(A.update_note if a.key == "announce_update" else None))


class AdminPanel(K.PanelView):
    """`/admin`. Everything below is the kit's; this only names the spec."""

    def __init__(self, bot, invoker, guild, channel):
        super().__init__(AdminSpec(), bot, invoker, guild, channel)


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

    @app_commands.command(
        name="checkbeyassets",
        description="[Admin] Find Beys missing from the local assets/beys folder")
    async def check_bey_assets(self, interaction: discord.Interaction) -> None:
        """Compare the authored Bey roster with the live local Bey art folder."""
        if not A.is_admin(interaction.user):
            return await interaction.response.send_message(
                "Not authorized.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        roster_path = os.path.join(root, "data", "beyblades.json")
        assets_dir = os.path.join(root, "assets", "beys")

        try:
            with open(roster_path, encoding="utf-8") as fh:
                roster = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            return await interaction.followup.send(
                f"⚠️ Could not read Bey database: `{type(exc).__name__}`",
                ephemeral=True)

        if not os.path.isdir(assets_dir):
            return await interaction.followup.send(
                "⚠️ `assets/beys/` does not exist on this bot install.",
                ephemeral=True)

        def key(value: str) -> str:
            # Match names independent of spaces, punctuation, case and file
            # extension: "Aegis Valorian" == "Aegis Valorian.png".
            return re.sub(r"[^a-z0-9]+", "", str(value).lower())

        image_exts = {".png", ".jpg", ".jpeg", ".webp"}
        local = set()
        for filename in os.listdir(assets_dir):
            stem, ext = os.path.splitext(filename)
            if ext.lower() in image_exts:
                local.add(key(stem))

        missing = []
        for roster_key, data in roster.items():
            name = (data or {}).get("name") or roster_key
            if key(name) not in local and key(roster_key) not in local:
                missing.append(str(name))
        missing.sort(key=str.casefold)

        total = len(roster)
        present = total - len(missing)
        if not missing:
            return await interaction.followup.send(
                f"✅ **Bey Asset Check**\nAll **{total}** database Beys have a "
                "local asset in `assets/beys/`.",
                ephemeral=True)

        header = (f"🖼️ **Bey Asset Check**\n"
                  f"Database: **{total}** · Found: **{present}** · "
                  f"Missing: **{len(missing)}**\n\n")
        chunks, current = [], header
        for name in missing:
            line = f"• {name}\n"
            if len(current) + len(line) > 1900:
                chunks.append(current)
                current = ""
            current += line
        if current:
            chunks.append(current)

        await interaction.followup.send(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    # ── the three recovery commands ──────────────────────────────────────────
    #
    # Prefix-only entry points onto the SAME functions the panel calls. They
    # exist because a slash-only recovery tool cannot recover slash commands.

    @commands.command(name="sync", hidden=True)
    async def sync(self, ctx: commands.Context, scope: str = "guild") -> None:
        """`;sync` — register globally and clear this server's stale copies.

        `;sync clean`  — remove only what the bot no longer has
        `;sync global` — sync, then clear the copies in every server
        `;sync purge`  — clear this server's copies without re-syncing
        `;sync mirror` — instant here, at the cost of listing everything twice
        """
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
