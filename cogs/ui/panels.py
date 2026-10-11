"""
panels.py — the player-facing `/` commands, one line each.

The problem
-----------
Discord lists the subcommands of a group FLAT in the picker. Before this,
reaching nine features cost twenty-eight lines:

    /avatar 5 · /casino 7 · /player 7 · /story 4  +  /leaderboard /rank /verify

Most of those lines are in front of a reader who is not looking for them, and
`/admin` had already solved exactly this — one command, a select, a Run button.
So the same view (`cogs/ui/panel_kit.py`) now backs all of them, and the picker
is eight lines: /admin /avatar /casino /leaderboard /player /story /tournament
/trade.

Why every action just runs a prefix command
-------------------------------------------
Because the feature is already implemented there. All four cogs these panels
replace said so themselves, in four separate copies of the same comment: the
prefix command owns the validation, the cooldowns and the rendering, and a
second copy behind the slash entry point is how the two paths drift. The panel
changes how an action is CHOSEN. It does not reimplement one.

That is also why `binds` is a mapping rather than a positional list —
`;avatarupgrade` takes (avatar, levels) and the panel collects (text, amount),
so appending in a fixed order would pass the level count as the card name with
no error anywhere.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import ranked as RK

from . import invoke as INVOKE
from . import panel_kit as K

log = logging.getLogger("beyblade_bot.panels")

A = K.PanelAction


# ══════════════════════════════════════════════════════════════════════════════
#  👤  /player
# ══════════════════════════════════════════════════════════════════════════════
#
# The ranked card lives here. /rank itself is reserved for starting a ranked
# match, while /player remains the place to inspect one player's profile/rank.
# The boards moved out to /leaderboard in v1.18 because a board is about
# everyone and /player is about one player.

class PlayerSpec(K.PrefixSpec):
    title = "👤  Player"
    colour = 0x5865F2
    placeholder = "What would you like to see?"

    ACTIONS = (
        A("profile", "Profile", "your full profile card", "🪪",
          invoke="profile", needs=("user",), binds={"member": "user"}),
        A("inventory", "Inventory", "every blade you own", "🎒",
          invoke="inventory", needs=("user",), binds={"member": "user"}),
        A("balance", "Balance", "your Beycoin wallet", "🪙", invoke="bal"),
        A("quests", "Quests", "your daily and weekly quests", "📋",
          invoke="quests"),
        A("notifications", "Notifications", "which DMs you get from Beycord",
          "🔔", invoke="notifications"),
        A("claim", "Claim quest rewards", "collect what you've finished", "🎁",
          invoke="questclaim"),
        A("achievements", "Achievements", "what you've unlocked", "🏆",
          invoke="achievements"),
        A("mastery", "Blade mastery", "how well you know each blade", "🔰",
          invoke="mastery", needs=("text",), binds={"blade": "text"}),
        A("rank", "Ranked card", "tier, score and board positions", "🎖️",
          invoke="rank", needs=("user",), binds={"member": "user"}),
    )

    # `;verify` was here until v1.18. Verification is gone — ranked is open to
    # everyone — so the action would have run a command that no longer exists.


# ══════════════════════════════════════════════════════════════════════════════
#  🏆  /leaderboard
# ══════════════════════════════════════════════════════════════════════════════

# /leaderboard uses autocomplete rather than static @choices. Static choices
# are registered globally by Discord, so main-server-only boards appeared in
# every server even though the command correctly refused to render them there.
# Autocomplete runs per interaction and can filter the list for that guild.
# The command layer still keeps the permission check as the final authority.
class LeaderboardSpec(K.PrefixSpec):
    """One action per board, read from the same table the board sorts on.

    Built rather than typed out: `RK.CATEGORIES` is what `;leaderboard` itself
    validates against and sorts by, so a new board appears here the moment it
    exists there. Restating the names in this file is how the two drift, and a
    single `leaderboard` action would instead ask which board through a modal
    — typing the name of a thing that could have been a dropdown entry.
    """
    title = "🏆  Leaderboards"
    colour = 0xF1C40F
    placeholder = "Which board?"
    # A board only you can see is not a board.
    public = True

    def actions(self, category: str, user) -> list[K.PanelAction]:
        try:
            from utils import ranked as RK
        except Exception:                                # noqa: BLE001
            return []
        # Built per invocation, so unlike the native slash choices — which are
        # registered once, globally — this list CAN tell which server it is in.
        # The community boards are main-server only, and offering a player a
        # dropdown entry that answers "that board is main-server only" (in
        # public, since this panel posts publicly) is worse than not offering
        # it.
        main_only = getattr(RK, "MAIN_ONLY", frozenset())
        allowed = True
        if main_only:
            try:
                from cogs.community import guard as _guard
                allowed = _guard.is_main(getattr(user, "guild", None))
            except Exception:                            # noqa: BLE001
                allowed = False
        return [A(key=f"lb_{key}",
                  label=spec["label"],
                  description=spec.get("describe", "")[:100],
                  emoji=spec.get("emoji") or "🏅",
                  invoke="leaderboard", kwargs={"category": key})
                for key, spec in RK.CATEGORIES.items()
                if allowed or key not in main_only]


# ══════════════════════════════════════════════════════════════════════════════
#  🎰  /casino
# ══════════════════════════════════════════════════════════════════════════════

async def _open_casino_lobby(panel, interaction) -> None:
    """Hand off to the casino's own lobby view.

    `CasinoLobbyView` is already a full panel — category buttons, a game
    select, a bet modal, wallet and premium buttons. Rebuilding any of that
    here would be a second casino menu to keep in step with the first.
    """
    from cogs.casino.casino_menu import CasinoLobbyView

    # `CasinoLobbyView(cog, player, ctx, bet=0)` — three positional arguments,
    # and this passed two. `ctx` has no default, so the call raised TypeError
    # every time and `guard` turned it into "⚠️ TypeError: ...". Worse, the bot
    # was landing in the `cog` slot, so even a defaulted `ctx` would have died
    # on `self.cog.bot` at the first Play.
    #
    # `ctx` is not optional in spirit either: `casino_menu._launch` answers
    # "Run `;blackjack` to start this one" for every ctx-driven game when it is
    # None, so the lobby could launch almost nothing.
    cog = panel.bot.get_cog("CasinoMenuCog")
    ctx = await INVOKE.build_context(interaction, None, bot=panel.bot,
                                     author=panel.invoker,
                                     channel=panel.home_channel)
    view = CasinoLobbyView(cog, interaction.user, ctx, bet=0)
    embed = await view.build_embed()
    if interaction.response.is_done():
        msg = await interaction.followup.send(embed=embed, view=view,
                                              ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, view=view,
                                                ephemeral=True)
        msg = await interaction.original_response()
    view.message = msg


class CasinoSpec(K.PrefixSpec):
    title = "🎰  Casino"
    colour = 0xF1C40F
    placeholder = "Wallet, or a game?"

    ACTIONS = (
        A("menu", "Play a game", "open the game lobby", "🕹️",
          handler=_open_casino_lobby),
        A("balance", "Balance", "your casino coin balance", "🪙",
          invoke="casinobal"),
        A("daily", "Daily bonus", "claim your daily casino coins", "🎁",
          invoke="casinodaily"),
        A("leaderboard", "Leaderboard", "top casino coin holders", "🏅",
          invoke="casinoleaderboard"),
        A("buy", "Buy casino coins", "100 Beycoins → 60 casino coins", "📥",
          invoke="casinoexchange", needs=("amount",),
          kwargs={"direction": "buy"}, binds={"amount": "amount"}),
        A("sell", "Sell casino coins", "back to Beycoins, minus tax", "📤",
          invoke="casinoexchange", needs=("amount",),
          kwargs={"direction": "sell"}, binds={"amount": "amount"}),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  🎴  /avatar
# ══════════════════════════════════════════════════════════════════════════════

class AvatarSpec(K.PrefixSpec):
    title = "🎴  Avatar cards"
    colour = 0x9B59B6
    placeholder = "Levels, skills or costs?"

    ACTIONS = (
        A("upgrade", "Upgrade a card", "buy levels — blank card = equipped", "⬆️",
          invoke="avatarupgrade", needs=("amount", "text"),
          binds={"levels": "amount", "avatar": "text"}),
        A("skill", "Pick a skill", "which skill your avatar fights with", "⚡",
          invoke="avatarskill", needs=("amount", "text"),
          binds={"slot": "amount", "avatar": "text"}),
        A("reset", "Reset a card", "back to Lv1, refunds 70% of the spend", "♻️",
          invoke="avatarreset", needs=("text",), binds={"avatar": "text"},
          confirm="The card drops to Lv1. You get 70% of what you spent back, "
                  "not all of it."),
        A("costs", "Upgrade costs", "the whole curve, before you buy", "📈",
          invoke="avatarcost"),
        A("refill", "Refill energy", "top your avatar's energy back up", "🔋",
          invoke="energyrefill"),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  📖  /story
# ══════════════════════════════════════════════════════════════════════════════

class StorySpec(K.PrefixSpec):
    """The fallback panel for `/story`, and the one `;story` itself does not need.

    `/story` opens the School League's own chapter picker directly (see
    `PanelCommands.story` below) — a select for the chapter, a select for the
    battle and a Normal/Nightmare toggle, which is the screen that was asked
    for and which a generic select-and-Run panel cannot draw. This spec is what
    `/story` falls back to if the Story cog is not loaded, and it still gets
    you to every Story command.
    """

    title = "📖  Story Mode"
    colour = 0xE67E22
    placeholder = "Play, or look something up?"

    ACTIONS = (
        # Two play actions, not one with a required battle number: `;story` on
        # its own opens the chapter picker, which is the better route in and
        # would be lost if the panel always demanded a battle up front.
        A("play", "Play", "pick a battle and fight it", "▶️", invoke="story"),
        A("jump", "Jump to a battle", "e.g. 3, or 3 nightmare", "⏩",
          invoke="story", needs=("text",), binds={"args": "text"}),
        A("map", "School League", "all eight battles and your progress", "🗺️",
          invoke="storymap"),
        A("info", "Battle info", "opponent, reward and lock state", "🔎",
          invoke="storyinfo", needs=("text",), binds={"n": "text"}),
        A("stats", "Your record", "your School League record", "📊",
          invoke="storystats", needs=("user",), binds={"member": "user"}),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  The cog
# ══════════════════════════════════════════════════════════════════════════════

SPECS = {
    "player": PlayerSpec,
    "casino": CasinoSpec,
    "avatar": AvatarSpec,
    "story": StorySpec,
    "leaderboard": LeaderboardSpec,
}


class PanelCommands(commands.Cog, name="Panels"):
    """One `/` command per panel-backed feature. Direct shortcuts such as `/rank` live here too."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _run(self, interaction: discord.Interaction, command: str,
                   **kwargs) -> None:
        """Run a prefix command straight from a slash command, publicly.

        The same helper the panels use. Direct slash shortcuts such as
        `/rank @player` and a chosen `/leaderboard` delegate to the existing
        prefix implementation instead of duplicating validation or battle logic.
        """
        cmd = self.bot.get_command(command)
        if cmd is None:
            return await INVOKE.reply(interaction,
                                      f"`{command}` isn't loaded right now.")
        await INVOKE.run_prefix_command(interaction, cmd, bot=self.bot,
                                        visibility=INVOKE.PUBLIC, **kwargs)

    async def _open(self, interaction: discord.Interaction, key: str) -> None:
        view = K.PanelView(SPECS[key](), self.bot, interaction.user,
                           interaction.guild, interaction.channel)
        await interaction.response.send_message(embed=view.embed(), view=view,
                                                ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except Exception:                                # noqa: BLE001
            pass

    @app_commands.command(name="player",
                          description="Your profile, collection, quests and rank")
    async def player(self, interaction: discord.Interaction) -> None:
        await self._open(interaction, "player")

    @app_commands.command(name="casino",
                          description="Casino games, wallet and exchange")
    async def casino(self, interaction: discord.Interaction) -> None:
        await self._open(interaction, "casino")

    @app_commands.command(name="avatar",
                          description="Avatar cards — levels, skills and upgrades")
    async def avatar(self, interaction: discord.Interaction) -> None:
        await self._open(interaction, "avatar")

    @app_commands.command(
        name="story",
        description="Story Mode — the School League, Normal or Nightmare")
    async def story(self, interaction: discord.Interaction) -> None:
        # The one panel that is not a select-and-Run: the League picker is a
        # chapter select, a battle select and a difficulty toggle, and it reads
        # the player's progress to draw the ✅/▶️/🔒 state. `_open` is the
        # fallback for a tree where the Story cog failed to load.
        cog = self.bot.get_cog("Story Mode")
        if cog is None:
            return await self._open(interaction, "story")
        view = cog.picker(interaction.user)
        await interaction.response.send_message(embed=view.embed(), view=view)

    @app_commands.command(name="leaderboard",
                          description="Every leaderboard — rank, level, money and more")
    @app_commands.describe(board="Which board. Leave it blank to pick from a menu.")
    async def leaderboard(self, interaction: discord.Interaction,
                          board: Optional[str] = None) -> None:
        """Two ways in, one command.

        With a board chosen, autocomplete has already asked the only question,
        so there is nothing to open — the board is posted straight away. With
        nothing chosen, the panel does the asking.
        """
        if board is None:
            return await self._open(interaction, "leaderboard")
        await self._run(interaction, "leaderboard", category=board)

    @leaderboard.autocomplete("board")
    async def leaderboard_board_autocomplete(
            self, interaction: discord.Interaction, current: str
            ) -> list[app_commands.Choice[str]]:
        """Only offer boards that are valid in the interaction's server."""
        main_only = getattr(RK, "MAIN_ONLY", frozenset())
        in_main_server = False
        if main_only:
            try:
                from cogs.community import guard as _guard
                in_main_server = _guard.is_main(interaction.guild)
            except Exception:                            # noqa: BLE001
                in_main_server = False

        needle = (current or "").casefold().strip()
        choices: list[app_commands.Choice[str]] = []
        for key, spec in RK.CATEGORIES.items():
            if key in main_only and not in_main_server:
                continue
            label = f"{spec.get('emoji') or '🏅'} {spec['label']}"
            if needle and needle not in key.casefold() and needle not in label.casefold():
                continue
            choices.append(app_commands.Choice(name=label, value=key))
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(name="rank",
                          description="Challenge a player to a ranked match")
    @app_commands.describe(player="Player to challenge in ranked")
    async def rank(self, interaction: discord.Interaction,
                   player: discord.Member) -> None:
        """Start the existing ranked battle flow through a short slash command."""
        await self._run(interaction, "battle",
                        opponent=player, mode="ranked")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PanelCommands(bot))
