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
STARTER_CHOICES = 4

# The four anime starters, by name. An explicit list rather than a rarity
# filter: these are authored AS starters — deliberately at the bottom of the
# Rare band, one per type, each carrying its anime ability — and a rarity
# filter would silently start handing out whatever else was added at that
# rarity later. Every new player picks one of exactly these four.
STARTER_NAMES = (
    "Victory Valkyrie",     # Attack
    "King Kerbeus",         # Defense
    "Rising Ragnaruk",      # Stamina
    "Storm Spriggan",       # Balance
)

# Commands that must work BEFORE a player has started, or the gate below locks
# people out of the door it is guarding. Matched against the command's
# qualified name and every alias.
GATE_EXEMPT = {
    "start", "begin", "newplayer", "getstarted",   # the door itself
    "whatnext", "next", "guide",                   # where ;start sends you
    "help", "commands", "h",                       # how to find the door
    "version", "build", "ver",                     # admin diagnostics
    "ping", "invite", "support",
}


class NotStarted(commands.CheckFailure):
    """Raised by the global gate when a player hasn't run ;start yet."""


class BotBanned(commands.CheckFailure):
    """Raised by the global gate for a player on the admin ban list."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


class UnderMaintenance(commands.CheckFailure):
    """Raised by the global gate while maintenance mode is on."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


def _starter_pool() -> list[dict]:
    """The four authored starters, in a stable order.

    Falls back to any blade only if the roster cannot be read at all — a
    missing starter must not leave `;start` with nothing to offer, because
    with the gate in place that would lock the whole bot.
    """
    try:
        beys = load_beyblades()
    except Exception:
        return []
    by_name = {b.get("name"): b for b in beys.values() if b.get("name")}
    pool = [by_name[n] for n in STARTER_NAMES if n in by_name]
    return pool or [b for b in beys.values() if b.get("name")]


# Written the moment a starter is actually claimed. Before this existed,
# "have you started?" was INFERRED, and one of the two things it inferred from
# was wrong — see below.
K_STARTED = "starter_claimed"


def _has_started(profile: dict) -> bool:
    """Has this player been through `;start` and come out with a blade?

    The old answer was `inventory or xp > 0`, and the `xp > 0` half was a trap:
    XP can be handed out by a redeem code, an admin grant or a migration
    without a blade ever changing hands. Five live accounts sat on exactly
    60,000 XP, 110,999 coins, level 34 and an EMPTY inventory — so `;start`
    told them "You're already started!" and closed, they never got to pick,
    and nothing that needs a blade would work. That is the reported bug, and
    it was unrecoverable without an admin: there is no second door.

    Owning a blade is now the only thing that counts, plus an explicit flag so
    the answer survives the player later selling or trading every blade away.
    Without the flag, "no blades" would read as "never started" and hand out
    another free starter and another STARTER_COINS — an exploit, and the reason
    the xp check was there in the first place. The flag says what the xp was
    standing in for, and says it correctly.
    """
    return bool(profile.get(K_STARTED)) or bool(profile.get("inventory"))


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

        # Re-read AFTER add_beyblade_to_inventory, never before: that call
        # writes the profile itself, so a snapshot taken earlier would be
        # written back here and erase the blade we just granted. Same bug that
        # ate blades in redeem.grant.
        profile = await get_user(uid)
        profile["coins"] = profile.get("coins", 0) + STARTER_COINS
        # The flag goes down here, not in ;start, because this is the line
        # where the player actually HAS something. Setting it when the picker
        # opens would strand anyone who closed it or let it time out.
        profile[K_STARTED] = True
        await update_user(uid, profile)

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
    """A few doors, not a wall of 160 commands — plus one door TO that wall.

    "All Commands" is deliberately the last, least-emphasized button: the
    default experience is still the short list, the full one is one
    unforced click away for whoever actually wants it.
    """

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
            beycoins = (await get_user(self.player.id)).get("coins", 0)
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

    @discord.ui.button(label="All Commands", emoji="📚",
                       style=discord.ButtonStyle.secondary, row=1)
    async def all_commands_btn(self, interaction: discord.Interaction,
                               _: discord.ui.Button):
        """The wall of 160 commands — opt-in, one click from here.

        The three (now four) buttons above stay the DEFAULT because dumping
        the full list on a brand new player is what emptied the registry in
        the first place (see the module docstring). This button exists for
        the player who wants it anyway, without forcing it on everyone else.
        """
        from cogs.ui.help_cog import send_full_command_list
        await interaction.response.defer(ephemeral=True)
        sent = await send_full_command_list(interaction.user)
        if sent:
            await interaction.followup.send(
                "📬 Sent every command to your DMs.", ephemeral=True)
        else:
            await interaction.followup.send(
                "❌ I couldn't DM you — check that direct messages from "
                "server members are allowed, then try again. `;help` works "
                "here in the meantime.", ephemeral=True)

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
        profile = await get_user(ctx.author.id)

        if _has_started(profile):
            casino_bal = await casino_wallet.get_balance(ctx.author.id)
            held = len(profile.get("inventory") or [])
            e = discord.Embed(
                title="✅  You're already started!",
                description=(
                    f"**{held}** blade(s) collected.\n"
                    f"Need a refresher? `;whatnext`"
                    # Reachable only by selling or trading away every blade.
                    # "Already started" plus an empty bag is a dead end unless
                    # the message says where to get another one.
                    if held else
                    "You've traded or sold every blade you had.\n"
                    "Catch one from a wild spawn, or buy one with `;shop`."
                ),
                color=0x2ecc71 if held else 0xf1c40f,
            )
            e.add_field(name="🪙 Beycoins",
                        value=f"**{profile.get('coins', 0):,}**", inline=True)
            e.add_field(name="🎰 Casino Coins",
                        value=f"**{casino_bal:,}**", inline=True)
            e.set_footer(text="`;bal` for the full wallet card")
            return await ctx.send(embed=e)

        pool = _starter_pool()
        if not pool:
            return await ctx.send(
                "⚠️ The blade database isn't loaded properly — tell an admin.")

        # All four, always, in type order — not a random sample. The point of
        # authoring one starter per type is that a new player gets to choose a
        # PLAYSTYLE; sampling three of four would hide one at random and make
        # the choice feel arbitrary.
        choices = pool[:STARTER_CHOICES]
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
        if isinstance(error, NotStarted):
            return await ctx.send(embed=_gate_embed(), delete_after=60)
        # One line explaining why, rather than a command that appears to do
        # nothing. A deploy that silently swallows every command looks exactly
        # like a bot that has crashed.
        if isinstance(error, UnderMaintenance):
            return await ctx.send(embed=_maintenance_embed(error.reason),
                                  delete_after=60)
        if isinstance(error, BotBanned):
            return await ctx.send(embed=_banned_embed(error.reason),
                                  delete_after=60)
        if not isinstance(error, commands.CommandNotFound):
            return
        try:
            if user_exists(ctx.author.id):
                profile = await get_user(ctx.author.id)
                if _has_started(profile):
                    return
            await ctx.send(
                "👋 New here? Run **`;start`** to get your first Beyblade.",
                delete_after=20)
        except Exception:
            pass


# ── The gate ─────────────────────────────────────────────────────────────────

def _gate_embed() -> discord.Embed:
    e = discord.Embed(
        title="🌟 Pick your Beyblade first",
        description=(
            "You need a blade before you can do anything here.\n\n"
            "Run **`;start`** — it takes one click, it's free, and you get "
            f"🪙 {STARTER_COINS:,} to go with it."),
        colour=0xF1C40F)
    e.add_field(
        name="What you'll choose from",
        value="\n".join(f"• **{n}**" for n in STARTER_NAMES),
        inline=False)
    e.set_footer(text="Already started and still seeing this? Tell an admin.")
    return e


def _maintenance_embed(reason: str) -> discord.Embed:
    return discord.Embed(
        title="🚧 Beycord is down for maintenance",
        description=(reason or "A new build is being deployed.")
                    + "\n\nNothing is lost — try again in a few minutes.",
        colour=0xE67E22)


def _banned_embed(reason: str) -> discord.Embed:
    return discord.Embed(
        title="🔨 You're banned from this bot",
        description=(f"Reason: **{reason}**" if reason else "No reason given.")
                    + "\n\nTalk to an admin if you think this is a mistake.",
        colour=0xED4245)


def blocked_reason(user, bot=None):
    """Is this user refused right now, and why? `None` means let them through.

    Maintenance mode and the ban list are both answered HERE, in the check that
    already runs in front of every command, rather than as a second global
    check. Two independent checks would be two things that have to agree about
    who may run what, and they would disagree the first time one of them was
    edited.

    Returns the exception to raise, so the caller decides how to surface it.
    Never raises on its own — a failure inside this function must not take the
    whole bot down.
    """
    try:
        from cogs.admin import actions as A
    except Exception:                                    # noqa: BLE001
        return None                                      # fail open
    try:
        uid = getattr(user, "id", None)
        # The owner is never locked out — the panel that turns maintenance mode
        # OFF is itself a command, so gating it would be a one-way door.
        if uid == A.MASTER_ID:
            return None
        rec = A.banned(uid)
        if rec:
            return BotBanned(rec.get("reason", ""))
        m = A.maintenance()
        if m["on"]:
            return UnderMaintenance(m["reason"])
    except Exception:                                    # noqa: BLE001
        return None                                      # fail open
    return None


def command_names(command) -> set[str]:
    """Every name a command answers to, including its aliases and parents."""
    names = {getattr(command, "name", ""), getattr(command, "qualified_name", "")}
    names.update(getattr(command, "aliases", []) or [])
    parent = getattr(command, "parent", None)
    if parent is not None:
        names.update(command_names(parent))
    return {n for n in names if n}


def is_exempt(command) -> bool:
    """Can this command run before the player has started?"""
    if command is None:
        return True
    return bool(command_names(command) & GATE_EXEMPT)


def gate_check(bot: commands.Bot):
    """Build the global check that requires `;start` before anything else.

    Deliberately fails OPEN on any internal error. A gate in front of every
    command in the bot is the single worst place for an unhandled exception —
    a database blip would take the whole bot down for everyone, which is
    strictly worse than briefly letting an unstarted player run a command.
    """
    async def predicate(ctx: commands.Context) -> bool:
        # A ban and a maintenance lockout apply to EVERY command, the exempt
        # ones included. `;start` is exempt from the starter gate because it is
        # the door; it is not exempt from a ban, or the ban would only stop
        # people who had already walked through.
        blocked = blocked_reason(ctx.author, bot)
        if blocked is not None:
            raise blocked

        if is_exempt(ctx.command):
            return True
        try:
            # Owner is never gated: admin commands have to work on a fresh
            # install, before anybody — including the owner — has started.
            if await bot.is_owner(ctx.author):
                return True
        except Exception:                                # noqa: BLE001
            pass
        try:
            if not user_exists(ctx.author.id):
                raise NotStarted()
            if not _has_started(await get_user(ctx.author.id)):
                raise NotStarted()
        except NotStarted:
            raise
        except Exception:                                # noqa: BLE001
            return True                                  # fail open
        return True

    return predicate


async def has_started_id(bot: commands.Bot, user) -> bool:
    """Shared truth for both gates. True (open) on any internal error."""
    try:
        if await bot.is_owner(user):
            return True
    except Exception:                                    # noqa: BLE001
        pass
    try:
        if not user_exists(user.id):
            return False
        return _has_started(await get_user(user.id))
    except Exception:                                    # noqa: BLE001
        return True                                      # fail open


def install_tree_gate(bot: commands.Bot) -> None:
    """Gate SLASH commands too.

    `bot.add_check` only covers prefix commands, and seven cogs expose their
    slash entry points by building a Context and calling `ctx.invoke` — which
    explicitly does NOT run checks. Without this, every gated command has an
    ungated `/` twin, which is not a gate at all.
    """
    tree = bot.tree
    if getattr(tree, "_beycord_start_gate", False):
        return
    previous = tree.interaction_check

    async def check(interaction: discord.Interaction) -> bool:
        try:
            blocked = blocked_reason(interaction.user, bot)
            if isinstance(blocked, UnderMaintenance):
                await interaction.response.send_message(
                    embed=_maintenance_embed(blocked.reason), ephemeral=True)
                return False
            if isinstance(blocked, BotBanned):
                await interaction.response.send_message(
                    embed=_banned_embed(blocked.reason), ephemeral=True)
                return False

            name = getattr(interaction.command, "qualified_name", "") or ""
            root = name.split(" ")[0] if name else ""
            if not name or root in GATE_EXEMPT or name in GATE_EXEMPT:
                pass
            elif not await has_started_id(bot, interaction.user):
                await interaction.response.send_message(
                    embed=_gate_embed(), ephemeral=True)
                return False
        except discord.InteractionResponded:
            return False
        except Exception:                                # noqa: BLE001
            pass                                         # fail open
        # Chain, so this never silently replaces a check something else set.
        try:
            return await previous(interaction)
        except Exception:                                # noqa: BLE001
            return True

    tree.interaction_check = check
    tree._beycord_start_gate = True


async def setup(bot: commands.Bot):
    await bot.add_cog(OnboardingCog(bot))
    # A global check, so it covers every cog without each one opting in.
    # `add_check` is idempotent enough for a reload: remove first, then add.
    check = gate_check(bot)
    try:
        bot.remove_check(getattr(bot, "_beycord_start_gate", None))
    except Exception:                                    # noqa: BLE001
        pass
    bot._beycord_start_gate = check
    bot.add_check(check)
    install_tree_gate(bot)
