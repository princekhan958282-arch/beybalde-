"""
onboarding.py  —  🌟 New player onboarding

Why this exists
---------------
The registry had 3,356 rows and 3,131 of them (93%) were completely empty —
no coins, no blades, no XP. Only 46 accounts had ever fought a battle. People
were touching the bot once, hitting a wall of 160 commands, and leaving.

This gives them a single door: `;start`. Pick a starter blade, get some coins,
and land on a short "what now" menu that points at exactly three things
instead of the whole command list.

Commands:
    ;start      →  the onboarding flow (aliases: ;begin, ;newplayer)
    ;whatnext   →  re-open the next-steps menu any time
"""

import random
from typing import Optional

import discord
from discord.ext import commands

from cogs.casino import casino_wallet
from utils.database import (
    add_beyblade_to_inventory,
    get_user,
    load_beyblades,
    set_active_beyblade,
    update_user,
    user_exists,
)
from utils.embeds import RARITY_EMOJIS, rarity_colour

STARTER_COINS   = 1_000
STARTER_RARITY  = "Common"
STARTER_CHOICES = 3


def _starter_pool() -> list[dict]:
    """Common blades only — a starter shouldn't out-stat a Legendary drop."""
    try:
        beys = load_beyblades()
    except Exception:
        return []
    pool = [b for b in beys.values()
            if b.get("rarity") == STARTER_RARITY and b.get("name")]
    return pool or [b for b in beys.values() if b.get("name")]


def _has_started(profile: dict) -> bool:
    return bool(profile.get("inventory")) or profile.get("xp", 0) > 0


def _stat_line(bey: dict) -> str:
    stats = bey.get("stats") or {}
    if not isinstance(stats, dict) or not stats:
        return "—"
    order = ["attack", "defense", "stamina", "hp"]
    keys  = [k for k in order if k in stats] or list(stats)[:4]
    return "  ".join(f"{k[:3].upper()} `{stats[k]}`" for k in keys)


class StarterButton(discord.ui.Button):
    def __init__(self, bey: dict, row: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=bey["name"][:80],
            emoji=RARITY_EMOJIS.get(bey.get("rarity", ""), None) or None,
            row=row,
        )
        self.bey = bey

    async def callback(self, interaction: discord.Interaction):
        view: "StarterPickView" = self.view
        if interaction.user.id != view.player.id:
            return await interaction.response.send_message(
                "This isn't your starter pick — run `;start` yourself!", ephemeral=True)
        if view.claimed:
            return await interaction.response.defer()
        view.claimed = True

        uid  = view.player.id
        name = self.bey["name"]

        add_beyblade_to_inventory(uid, name)
        set_active_beyblade(uid, name)

        profile = get_user(uid)
        profile["coins"] = profile.get("coins", 0) + STARTER_COINS
        update_user(uid, profile)

        for c in view.children:
            c.disabled = True

        casino_bal = await casino_wallet.get_balance(uid)

        e = discord.Embed(
            title=f"🌀  {name} is yours!",
            description=(
                f"**{name}** is equipped and you're ready to battle."
            ),
            color=rarity_colour(self.bey.get("rarity", "Common")),
        )
        img = self.bey.get("image_url")
        if img:
            e.set_thumbnail(url=img)
        # Two wallets exist, so say so from the very first screen — otherwise
        # people find the casino later and wonder why ;bal "lost" their coins.
        e.add_field(name="🪙 Beycoins",
                    value=f"**{profile['coins']:,}**", inline=True)
        e.add_field(name="🎰 Casino Coins",
                    value=f"**{casino_bal:,}**", inline=True)
        e.add_field(name="Stats", value=_stat_line(self.bey), inline=False)
        e.set_footer(text="Two separate wallets — `;bal` shows both")

        await interaction.response.edit_message(embed=e, view=view)
        view.stop()

        nxt = NextStepsView(view.player)
        nxt.message = await interaction.followup.send(
            embed=await nxt.build_embed_async(), view=nxt, wait=True)


class StarterPickView(discord.ui.View):
    def __init__(self, player: discord.Member, choices: list[dict]):
        super().__init__(timeout=180)
        self.player  = player
        self.choices = choices
        self.claimed = False
        self.message: Optional[discord.Message] = None
        for i, bey in enumerate(choices):
            self.add_item(StarterButton(bey, row=i))

    def build_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="🌟  Welcome to Beycord!",
            description=(
                "Pick your **starter Beyblade**. This one's free — you'll catch "
                "plenty more from wild spawns.\n\n"
                "Tap a button below to claim it."
            ),
            color=0x9b59b6,
        )
        for bey in self.choices:
            emoji = RARITY_EMOJIS.get(bey.get("rarity", ""), "")
            e.add_field(
                name=f"{emoji} {bey['name']}",
                value=f"*{bey.get('type', '—')}*\n{_stat_line(bey)}",
                inline=True,
            )
        e.set_footer(text=f"{self.player.display_name}  •  you also get "
                          f"🪙 {STARTER_COINS:,} to start")
        return e

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message and not self.claimed:
            try:
                await self.message.edit(
                    content="⏰ Timed out — run `;start` again whenever you're ready.",
                    view=self)
            except Exception:
                pass


class NextStepsView(discord.ui.View):
    """Three doors, not a wall of 160 commands."""

    def __init__(self, player: discord.Member):
        super().__init__(timeout=300)
        self.player = player
        self.message: Optional[discord.Message] = None

    def build_embed(self) -> discord.Embed:
        """Static fallback — prefer build_embed_async so balances show."""
        return discord.Embed(
            title="🎯  What now?",
            description=(
                "⚔️ **Battle** — `;battle @someone`\n"
                "🌀 **Catch blades** — wild spawns appear in chat\n"
                "🎰 **Casino** — `;casino`\n\n"
                "*Tap a button for the details.*"
            ),
            color=0x2ecc71,
        )

    async def build_embed_async(self) -> discord.Embed:
        e = self.build_embed()
        try:
            beycoins = get_user(self.player.id).get("coins", 0)
            casino   = await casino_wallet.get_balance(self.player.id)
            e.add_field(name="🪙 Beycoins",
                        value=f"**{beycoins:,}**", inline=True)
            e.add_field(name="🎰 Casino Coins",
                        value=f"**{casino:,}**", inline=True)
            e.set_footer(text="`;bal` for the full wallet card")
        except Exception:
            pass
        return e

    @discord.ui.button(label="How battles work", emoji="⚔️",
                       style=discord.ButtonStyle.primary)
    async def battle_help(self, interaction: discord.Interaction, _: discord.ui.Button):
        e = discord.Embed(
            title="⚔️  How Battles Work",
            description=(
                "Run `;battle @someone` to challenge a player.\n\n"
                "Each turn, pick a move:\n"
                "⚔️ **Attack** · 🛡️ **Defense** · 🔋 **Stamina**\n"
                "🔌 **Charge** · ✨ **Special** (needs a full gauge)\n\n"
                "Type matchups matter — check `;bey <name>` first.\n\n"
                "Win → XP, coins, rank. Lose → still XP."
            ),
            color=0xe74c3c,
        )
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="Free coins", emoji="🪙",
                       style=discord.ButtonStyle.success)
    async def coins_help(self, interaction: discord.Interaction, _: discord.ui.Button):
        e = discord.Embed(
            title="🪙  Getting Coins",
            description=(
                "`;daily` — daily Beycoins\n"
                "`;casinodaily` — daily casino coins\n"
                "`;battle` — winners get paid\n"
                "`;quests` — objectives that pay\n"
                "`;sellbey <name>` — sell duplicates\n\n"
                "*Two separate wallets — swap with `;casinoexchange`.*"
            ),
            color=0xf1c40f,
        )
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="Catching blades", emoji="🌀",
                       style=discord.ButtonStyle.secondary)
    async def spawn_help(self, interaction: discord.Interaction, _: discord.ui.Button):
        e = discord.Embed(
            title="🌀  Catching Wild Blades",
            description=(
                "Wild Beyblades spawn in chat on their own.\n\n"
                "Hit **🎯 Claim** and pick the right name from four options. "
                "Guess wrong and you wait 8 seconds for a fresh set — and "
                "everyone else is guessing too.\n\n"
                "Know it already? `;claim <name>` is faster."
            ),
            color=0x9b59b6,
        )
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="My stuff", emoji="📋",
                       style=discord.ButtonStyle.secondary, row=1)
    async def profile_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        e = discord.Embed(
            title="📋  Your Stuff",
            description=(
                "`;bal` — both wallets in one card\n"
                "`;profile` — your player card\n"
                "`;inventory` — your collection\n"
                "`;mastery` — how well you know each blade\n"
                "`;achievements` — your badges\n"
                "`;clan` — join or start a clan"
            ),
            color=0x3498db,
        )
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class OnboardingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="start", aliases=["begin", "newplayer", "getstarted"])
    async def start(self, ctx: commands.Context):
        """🌟 Get your starter Beyblade and learn the basics."""
        profile = get_user(ctx.author.id)

        if _has_started(profile):
            casino_bal = await casino_wallet.get_balance(ctx.author.id)
            e = discord.Embed(
                title="✅  You're already started!",
                description=(
                    f"**{len(profile.get('inventory', []))}** blade(s) collected.\n"
                    f"Need a refresher? `;whatnext`"
                ),
                color=0x2ecc71,
            )
            e.add_field(name="🪙 Beycoins",
                        value=f"**{profile.get('coins', 0):,}**", inline=True)
            e.add_field(name="🎰 Casino Coins",
                        value=f"**{casino_bal:,}**", inline=True)
            e.set_footer(text="`;bal` for the full wallet card")
            return await ctx.send(embed=e)

        pool = _starter_pool()
        if len(pool) < STARTER_CHOICES:
            return await ctx.send(
                "⚠️ The blade database isn't loaded properly — tell an admin.")

        choices = random.sample(pool, STARTER_CHOICES)
        view = StarterPickView(ctx.author, choices)
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @commands.command(name="whatnext", aliases=["next", "guide"])
    async def whatnext(self, ctx: commands.Context):
        """🎯 The short version of what to do in this bot."""
        view = NextStepsView(ctx.author)
        view.message = await ctx.send(embed=await view.build_embed_async(), view=view)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error):
        """Nudge brand-new users toward ;start instead of a bare error."""
        if not isinstance(error, commands.CommandNotFound):
            return
        try:
            if user_exists(ctx.author.id):
                profile = get_user(ctx.author.id)
                if _has_started(profile):
                    return
            await ctx.send(
                f"👋 New here? Run **`;start`** to get your first Beyblade.",
                delete_after=20)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(OnboardingCog(bot))
