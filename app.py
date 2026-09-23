"""
main.py
-------
Beyblade Discord Bot — entry point.

Setup
-----
1.  Copy  .env.example  →  .env  and fill in your BOT_TOKEN.
2.  Install dependencies:  pip install -r requirements.txt
3.  Run:  python main.py

Cog structure
-------------
    cogs/spawn.py        — Wild Beyblade spawns & claiming
    cogs/profile.py      — User profiles, ;info, ;equip, ;inventory
    cogs/battle.py       — Button-based turn-by-turn battles
    cogs/shop.py         — Shop: buy/sell Beyblades and parts
    cogs/leaderboard.py  — Global leaderboard and rank cards
    cogs/admin/          — the /admin panel (v1.13)
    cogs/avatar/         — Avatar pack shop, inventory & battle bonuses
    cogs/casino/         — Full casino system (coins, all games)
"""

import os
import asyncio
import logging
import subprocess
import sys

# Dependency check BEFORE anything imports discord. Python caches modules, so
# upgrading discord.py after `import discord` would have no effect until the
# next boot — bootstrap.ensure() installs what requirements.txt asks for and
# re-execs once so the new version is actually the one that loads.
# Set BEYCORD_AUTO_INSTALL=0 to turn this off.
#
# Deliberately NO logging.basicConfig() here: basicConfig is a no-op once any
# handler exists, so configuring the root this early would silently discard the
# real logging setup forty lines below and change every log line's format.
# bootstrap attaches a handler to its own logger instead.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import bootstrap as _bootstrap        # noqa: E402
_bootstrap.ensure()

# Pull the latest code from GitHub, if a GITHUB_TOKEN is configured. Runs after
# bootstrap (so dependencies exist) and before any cog is imported.
#
# It deliberately does NOT restart: the files land on disk and take effect on
# the NEXT restart. An updater that relaunches the process mid-boot can put a
# host into a restart loop when the new code is broken, and that is not
# recoverable remotely. It logs loudly when an update is waiting.
#
# Never raises — a failed update must not stop the bot booting. Disable with
# BEYCORD_AUTO_UPDATE=0.
try:
    from utils import updater as _updater        # noqa: E402
    # Clear stale bytecode BEFORE anything is imported. This install is
    # updated by writing new .py files over the old ones — by the updater
    # below, or by extracting a zip in the hosting panel — and neither removes
    # __pycache__. A zip restores source files with the ARCHIVE's timestamps
    # rather than now, so a .py can land older than the .pyc compiled from the
    # file it replaced, and CPython will keep using the bytecode. Purging here,
    # before the first cog import, is what makes "I uploaded the new files"
    # actually mean the new code runs.
    _updater.purge_pycache_logged("startup")
    _updater.check_and_apply()
except Exception as _exc:                        # noqa: BLE001
    print(f"[update] skipped: {type(_exc).__name__}: {_exc}")

import discord
from discord.ext import commands
from dotenv import load_dotenv

# ── Environment ────────────────────────────────────────────────────────────────
load_dotenv()
# SECURITY: no hardcoded fallback here on purpose. app.py ships inside every
# zip and screenshot, and a leaked bot token hands over full control of the bot.
# utils/secrets.py reads from, in order: real env vars → .env → config_local.py.
# All three work without any hosting-panel support; the last two are just files.
# Read lazily inside main(): calling require() here would log its "here are the
# three ways to set this" guidance BEFORE logging.basicConfig() runs six lines
# below, so the one message that tells you how to fix a missing token would
# never reach the console.
from utils.secrets import require as _require_secret, source_of as _secret_source
TOKEN: str | None = None

# FORCED PREFIX: This completely ignores hidden settings and strictly forces ';'
COMMAND_PREFIX = ";"

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s  [%(levelname)s]  %(name)s: %(message)s",
    datefmt= "%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("beyblade_bot")

# Keep the last 50 exceptions in memory where an admin can read them from
# `/admin → 🔧 System → Recent errors`. There is no `logs/` directory on the
# panel and nothing is written to disk, so until v1.13 every one of the 21
# `log.exception` sites went straight to a console nobody was watching — which
# is how a boss battle stayed frozen for weeks.
try:
    from utils import errorlog
    errorlog.install()
except Exception as _exc:                                # noqa: BLE001
    logger.warning("error ring buffer not installed: %s", _exc)

# ── Cogs to load ───────────────────────────────────────────────────────────────
# Load subsystem packages (each has __init__.py with setup() entry point)
COGS = [
    "cogs.core",       # Core utilities
    "cogs.ping",       # ;ping WebSocket + Discord API/message latency
    "cogs.abilities",  # Ability engine & special moves
    "cogs.battle",     # Battle system
    "cogs.economy",    # Shop, Profile
    "cogs.ranked",     # Ranked ladder, leaderboards, verification
    "cogs.spawn",      # Wild spawns & claiming
    "cogs.ui",         # Help & logging
    "cogs.custom_bey", # /custombey player-created Bey builder
    "cogs.admin",      # /admin panel + ;sync ;reload ;version
    "cogs.avatar",     # Avatar system (optional)
    # ── Casino ──────────────────────────────────────────────
    "cogs.casino.mines",
    "cogs.casino.blackjack",
    "cogs.casino.slots",
    "cogs.casino.roulette",
    "cogs.casino.dice",
    "cogs.casino.crash",
    "cogs.casino.tower_climb",
    "cogs.casino.poker",
    "cogs.casino.coinflip",
    "cogs.casino.higher_lower",
    "cogs.casino.auction",
    # ── Casino: new games ────────────────────────────────────
    "cogs.casino.wheel",
    "cogs.casino.plinko",
    "cogs.casino.keno",
    "cogs.casino.video_poker",
    "cogs.casino.bey_race",
    "cogs.casino.mines_multi",
    # ── Extras: Tournament / Raid / Quests / Trading ─────────
    "cogs.extras.quests",
    "cogs.extras.raid",
    "cogs.extras.trade",
    "cogs.ui.inventory_ui",
    "cogs.casino.casino_hub",
    "cogs.casino.casino_premium",
    "cogs.casino.casino_menu",   # must load last: reads the other casino cogs

    # ── Retention & operations ───────────────────────────────
    "cogs.core.onboarding",
    "cogs.economy.wallet_card",
    "cogs.codes.redeem",
    "cogs.battle.boss.boss_battle",
    "cogs.story",            # the School League (PvE on the real PvP engine)
    "cogs.updates",          # update DMs out, /bugs and /suggest back in
    "cogs.vote",             # /vote Top.gg rewards + re-vote reminders
    "cogs.community",        # polls, giveaways, XP — MAIN SERVER ONLY
    "cogs.snapshots",        # daily player-data backups + restore
    "cogs.clans.clan",
    "cogs.clans.clan_war",
    "cogs.extras.mastery",
    "cogs.extras.achievements",
    "cogs.economy.chat_xp",   # chat EXP for trainer + equipped bey
    "cogs.tournament",       # one-command tournament (v1.12)
    "cogs.ui.panels",        # /player /casino /avatar /story panels (v1.14)
]

# ── Playwright Chromium auto-install ───────────────────────────────────────────

_PLAYWRIGHT_BROWSER_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".playwright-browsers"
)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _PLAYWRIGHT_BROWSER_DIR)


def _ensure_chromium() -> None:
    """Ensure Playwright's Chromium binary exists in persistent bot storage.

    Do not use "can Chromium launch?" as the installation test. A browser can
    already be installed but fail to launch because the host image is missing
    an OS library; reinstalling the same browser on every boot does not fix that
    and creates the warning loop seen on Pterodactyl panels.

    The browser is stored inside the bot directory instead of Playwright's
    per-user cache so panel/container recreation does not discard it.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        logger.warning("🎭 Playwright package unavailable: %s", exc)
        return

    try:
        with sync_playwright() as p:
            executable = p.chromium.executable_path
        if executable and os.path.isfile(executable) and os.access(executable, os.X_OK):
            logger.info("🎭 Playwright Chromium ready: %s", executable)
            return
    except Exception as exc:
        logger.debug("🎭 Chromium path probe failed: %s", exc)

    try:
        os.makedirs(_PLAYWRIGHT_BROWSER_DIR, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "🎭 Cannot create Playwright browser directory %s: %s. "
            "Cards will use the Pillow fallback.",
            _PLAYWRIGHT_BROWSER_DIR,
            exc,
        )
        return

    logger.info("🎭 Playwright Chromium missing — installing once…")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=300,
            env=dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=_PLAYWRIGHT_BROWSER_DIR),
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "🎭 Playwright Chromium install timed out after 300s. "
            "Cards will use the Pillow fallback."
        )
        return
    except Exception as exc:
        logger.warning(
            "🎭 Playwright Chromium install could not start: %s. "
            "Cards will use the Pillow fallback.",
            exc,
        )
        return

    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip().splitlines()
        tail = " | ".join(output[-6:]) if output else "no installer output"
        logger.warning(
            "🎭 Playwright Chromium install failed (exit %s): %s. "
            "Cards will use the Pillow fallback.",
            result.returncode,
            tail,
        )
        return

    try:
        with sync_playwright() as p:
            executable = p.chromium.executable_path
        if executable and os.path.isfile(executable):
            logger.info("🎭 Playwright Chromium installed successfully: %s", executable)
        else:
            logger.warning(
                "🎭 Playwright installer exited successfully but Chromium was "
                "not found at the expected path. Cards will use Pillow."
            )
    except Exception as exc:
        logger.warning(
            "🎭 Chromium verification failed after install: %s. "
            "Cards will use the Pillow fallback.",
            exc,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Bot setup
# ══════════════════════════════════════════════════════════════════════════════

class BeybladeBot(commands.Bot):
    """Main bot class with async setup hook for Cog loading."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True   # Required for prefix commands
        intents.members          = True   # Required for ;battle @mention resolving

        super().__init__(
            command_prefix = COMMAND_PREFIX,
            intents        = intents,
            help_command   = None,
            description    = "🌀 Let It Rip! — A Beyblade collection & battle bot.",
        )

    async def setup_hook(self) -> None:
        """Called automatically by discord.py before the bot connects."""
        # Ensure Chromium is available for HTML profile card rendering
        await asyncio.get_event_loop().run_in_executor(None, _ensure_chromium)

        # Today's command tally, if the process restarted part-way through a
        # day. A file from any other day is ignored by `load` itself.
        try:
            from utils import activity as _activity
            await asyncio.to_thread(_activity.load)
        except Exception as exc:                         # noqa: BLE001
            logger.debug(f"activity tracker not restored: {exc}")

        for cog_path in COGS:
            try:
                await self.load_extension(cog_path)
                logger.info(f"✅ Loaded cog: {cog_path}")
            except Exception as exc:
                logger.error(f"❌ Failed to load {cog_path}: {exc}", exc_info=True)

        # ── Slash command registration ────────────────────────────────────────
        # Without this the app_commands / hybrid commands exist locally but
        # Discord never shows them. Global sync can take up to an hour to
        # propagate — use ;sync in a guild for an instant per-guild sync.
        # Sync happens in on_ready, not here: pruning stale GUILD commands
        # needs self.guilds, which is empty until the gateway has sent them.
        # Syncing here as well would just burn a duplicate global sync.

    async def _reconcile_commands(self) -> None:
        """Register commands and delete ones Discord kept but the bot dropped.

        Guild-scoped commands are never pruned by `tree.sync()` — only the
        global set is replaced — so a command removed from the code lingers in
        every guild that was ever `;sync`ed, and shadows its replacement. This
        reconciles both. Set BEYCORD_AUTO_PRUNE=0 to sync without pruning.
        """
        try:
            from utils.command_sync import reconcile
            report = await reconcile(self)
            pruned = sum(len(v) for v in report["pruned"].values())
            logger.info(
                f"🔁 {report['synced']} global command(s)"
                + (f", pruned {pruned} stale" if pruned else "")
                + (f", {len(report['duplicates'])} duplicate name(s)"
                   if report["duplicates"] else ""))
        except Exception as exc:
            logger.error(f"🔁 Command reconcile failed: {exc}", exc_info=True)

    async def on_ready(self) -> None:
        logger.info(f"🌀 Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"   Prefix: {COMMAND_PREFIX}")
        logger.info(f"   Serving {len(self.guilds)} guild(s)")

        await self.change_presence(
            activity=discord.Game(name=f"{COMMAND_PREFIX}help | Let it rip! 🌀")
        )

        # Report the running build before anything else. A half-applied deploy
        # (cogs/ updated, utils/ not) otherwise shows up much later as a
        # baffling runtime error rather than one line here at boot.
        if not getattr(self, "_build_checked", False):
            self._build_checked = True
            try:
                from utils.buildinfo import selfcheck
                selfcheck()
            except Exception as exc:
                logger.warning(f"build self-check failed: {exc}")

        # on_ready can fire again after a reconnect; syncing every time would
        # burn rate limit for nothing, so this runs once per process.
        if not getattr(self, "_commands_reconciled", False):
            self._commands_reconciled = True
            await self._reconcile_commands()
            await self._verify_guild_cache()

    async def _verify_guild_cache(self) -> None:
        """Compare the gateway's guild cache against the REST API.

        `self.guilds` is filled from the READY payload plus one GUILD_CREATE per
        guild. If the gateway drops part-way through that stream — which a small
        container does regularly — the bot carries on serving a short list and
        NOTHING says so. The only symptom is `;servers` disagreeing with the
        Developer Portal, weeks later, and looking like a broken command.

        This is a report, not a repair: discord.py has no supported way to inject
        a guild into the cache, and a reconnect refills it properly. One warning
        at boot is enough to turn an invisible problem into an obvious one.
        """
        try:
            rest_guilds = [g async for g in self.fetch_guilds(limit=200)]
            # Keep the REST directory separately from discord.py's gateway
            # cache. UI server pickers can still show every server when READY
            # lost part of its GUILD_CREATE stream; we never mutate bot.guilds.
            self._rest_guild_directory = {g.id: g for g in rest_guilds}
            rest = set(self._rest_guild_directory)
        except Exception as exc:                         # noqa: BLE001
            logger.debug(f"guild cache check skipped: {exc}")
            return

        cached = {g.id for g in self.guilds}
        missing = rest - cached
        stale = cached - rest
        if missing:
            logger.warning(
                f"⚠️  Guild cache is INCOMPLETE: the API lists {len(rest)} servers "
                f"but the gateway delivered {len(cached)}. Missing "
                f"{len(missing)}: {sorted(missing)}. This is a dropped gateway "
                f"connection during startup, not a missing intent — restart to "
                f"refill. Commands scoped to those servers will not work.")
        elif stale:
            logger.info(
                f"Guild cache holds {len(stale)} server(s) the API no longer "
                f"lists (removed while offline): {sorted(stale)}")
        else:
            logger.info(f"   Guild cache verified against the API: {len(rest)} server(s)")

    # ── Activity tracking ─────────────────────────────────────────────────────
    #
    # Two listeners, because a player reaches the same feature two ways: the
    # prefix command, and the `/` panel that invokes it. `ctx.invoke` from a
    # panel does NOT dispatch `command_completion`, so counting both is not
    # double counting — it is the slash surface being counted once, under the
    # name the player actually typed.
    #
    # Completion, not invocation: a command that failed its checks (unstarted,
    # banned, on cooldown) did not get used, and counting it would report a
    # busy day made of refusals.

    async def _record_activity(self, user_id, name: str, guild_id=None) -> None:
        try:
            from utils import activity
            activity.record(user_id, name, guild_id=guild_id)
            if activity.due_for_flush():
                await asyncio.to_thread(activity.flush)
        except Exception as exc:                         # noqa: BLE001
            logger.debug(f"activity not recorded: {exc}")

    async def on_command_completion(self, ctx: commands.Context) -> None:
        await self._record_activity(
            ctx.author.id, getattr(ctx.command, "qualified_name", "") or "",
            getattr(ctx.guild, "id", None))

    async def on_app_command_completion(self, interaction: discord.Interaction,
                                        command) -> None:
        name = getattr(command, "qualified_name", None) or getattr(
            command, "name", "")
        await self._record_activity(interaction.user.id,
                                    f"/{name}" if name else "",
                                    interaction.guild_id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        directory = getattr(self, "_rest_guild_directory", None)
        if directory is not None:
            directory[guild.id] = guild
        logger.info(f"➕ Joined {guild.name} ({guild.id}) — "
                    f"now in {len(self.guilds)} server(s)")

        # New-server onboarding: point admins at the spawn-channel setup command.
        me = guild.me
        if me is not None:
            for channel in guild.text_channels:
                perms = channel.permissions_for(me)
                if perms.view_channel and perms.send_messages and perms.embed_links:
                    embed = discord.Embed(
                        title="🌀 Beycord Setup",
                        description=(
                            "Thanks for adding **Beycord**!\n\n"
                            "Set the channel for wild Beyblade spawns with:\n"
                            "**`;setspawnchannel #channel`**\n\n"
                            "If you don\'t set one, wild Beyblades can still spawn "
                            "in eligible channels based on message activity."
                        ),
                        color=discord.Color.blurple(),
                    )
                    try:
                        await channel.send(embed=embed)
                    except discord.DiscordException as exc:
                        logger.warning(
                            "Could not send join setup message in %s (%s): %s",
                            guild.name, guild.id, exc,
                        )
                    break

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        directory = getattr(self, "_rest_guild_directory", None)
        if directory is not None:
            directory.pop(guild.id, None)
        logger.info(f"➖ Removed from {guild.name} ({guild.id}) — "
                    f"now in {len(self.guilds)} server(s)")

    async def on_command_error(
        self,
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        """Global error handler — keeps tracebacks out of chat."""
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                f"❌ Missing argument: `{error.param.name}`\n"
                f"Usage: `{COMMAND_PREFIX}help {ctx.command}`"
            )
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("❌ Member not found. Make sure you @mention them.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You don't have permission to use that command.")
        elif isinstance(error, commands.CommandNotFound):
            pass   # Silently ignore unknown commands
        elif isinstance(error, commands.CheckFailure):
            # A refused check is never an "unexpected error". Whoever owns the
            # check owns the message: the onboarding cog explains the `;start`
            # gate, a ban and maintenance mode, and the admin cog stays silent
            # on purpose so its hidden commands look like they don't exist.
            # Without this branch the else below fired too, so a banned player
            # got their ban notice AND "⚠️ An unexpected error occurred", and a
            # non-admin who guessed `;sync` was told the check for it failed —
            # which is how a hidden command announces itself.
            pass
        else:
            logger.error(f"Unhandled error in {ctx.command}: {error}", exc_info=error)
            await ctx.send(f"⚠️ An unexpected error occurred: `{error}`")


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    global TOKEN
    TOKEN = _require_secret("BOT_TOKEN")   # logs the three options if missing
    if not TOKEN:
        return

    logger.info(f"BOT_TOKEN loaded from: {_secret_source('BOT_TOKEN')}")

    bot = BeybladeBot()
    try:
        async with bot:
            await bot.start(TOKEN)
    finally:
        # Release the shared info-card Chromium so panel restarts don't leak
        # a zombie renderer process.
        try:
            from utils import info_card
            await info_card.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
