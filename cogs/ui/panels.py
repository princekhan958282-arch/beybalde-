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
is six lines: /admin /avatar /casino /player /story /tournament.

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

import discord
from discord import app_commands
from discord.ext import commands

from . import panel_kit as K

log = logging.getLogger("beyblade_bot.panels")

A = K.PanelAction


# ══════════════════════════════════════════════════════════════════════════════
#  👤  /player
# ══════════════════════════════════════════════════════════════════════════════
#
# Also absorbs /leaderboard, /rank and /verify: three top-level commands whose
# subject is the player, sitting on their own lines for no reason other than
# having been written in a different cog.

def _board_actions() -> list[K.PanelAction]:
    """One action per leaderboard, read from the same table the board sorts on.

    A single `leaderboard` action would have to ask which board through a
    modal — typing the name of a thing that could have been a dropdown entry.
    Restating the five names here instead of importing them is how they drift.
    """
    try:
        from utils import ranked as RK
    except Exception:                                    # noqa: BLE001
        return []
    out = []
    for key, spec in RK.CATEGORIES.items():
        out.append(A(key=f"lb_{key}",
                     label=f"Leaderboard — {spec['label']}",
                     description=f"Top players by {spec['label'].lower()}",
                     emoji=spec.get("emoji") or "🏅",
                     invoke="leaderboard", kwargs={"category": key}))
    return out


class PlayerSpec(K.PrefixSpec):
    title = "👤  Player"
    colour = 0x5865F2
    placeholder = "What would you like to see?"

    BASE = (
        A("profile", "Profile", "your full profile card", "🪪",
          invoke="profile", needs=("user",), binds={"member": "user"}),
        A("inventory", "Inventory", "every blade you own", "🎒",
          invoke="inventory", needs=("user",), binds={"member": "user"}),
        A("balance", "Balance", "your Beycoin wallet", "🪙", invoke="bal"),
        A("quests", "Quests", "your daily and weekly quests", "📋",
          invoke="quests"),
        A("claim", "Claim quest rewards", "collect what you've finished", "🎁",
          invoke="questclaim"),
        A("achievements", "Achievements", "what you've unlocked", "🏆",
          invoke="achievements"),
        A("mastery", "Blade mastery", "how well you know each blade", "🔰",
          invoke="mastery", needs=("text",), binds={"blade": "text"}),
        A("rank", "Ranked card", "tier, score and board positions", "🎖️",
          invoke="rank", needs=("user",), binds={"member": "user"}),
        A("verify", "Verify for ranked", "clear yourself for ranked play", "✅",
          invoke="verify"),
    )

    def actions(self, category: str, user):
        return list(self.BASE) + _board_actions()


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
    view = CasinoLobbyView(panel.bot, interaction.user, bet=0)
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
    title = "📖  Story Mode"
    colour = 0xE67E22
    placeholder = "Play, or look something up?"

    ACTIONS = (
        # Two play actions, not one with a required stage: `;story` on its own
        # opens the game's own stage picker, which is the better route in and
        # would be lost if the panel always demanded a stage up front.
        A("play", "Play", "pick a stage and fight it", "▶️", invoke="story"),
        A("jump", "Jump to a stage", "e.g. 1-2, or a name", "⏩",
          invoke="story", needs=("text",), binds={"stage": "text"}),
        A("map", "Chapter map", "every chapter and your progress", "🗺️",
          invoke="storymap"),
        A("info", "Stage info", "opponent, rewards and lock state", "🔎",
          invoke="storyinfo", needs=("text",), binds={"stage": "text"}),
        A("stats", "Your record", "your Story Mode record", "📊",
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
}


class PanelCommands(commands.Cog, name="Panels"):
    """One `/` command per feature. The prefix commands are untouched."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

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

    @app_commands.command(name="story",
                          description="Story Mode — chapters and stages")
    async def story(self, interaction: discord.Interaction) -> None:
        await self._open(interaction, "story")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PanelCommands(bot))
