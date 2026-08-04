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
    cogs/admin.py        — Hidden master control commands
    cogs/avatar/         — Avatar pack shop, inventory & battle bonuses
    cogs/casino/         — Full casino system (coins, all games)
"""

import os
import asyncio
import logging
import subprocess
import sys
from pathlib import Path

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

# ── Cogs to load ───────────────────────────────────────────────────────────────
# Load subsystem packages (each has __init__.py with setup() entry point)
COGS = [
    "cogs.core",       # Core utilities
    "cogs.abilities",  # Ability engine & special moves
    "cogs.battle",     # Battle system
    "cogs.economy",    # Shop, Profile, Leaderboard
    "cogs.spawn",      # Wild spawns & claiming
    "cogs.ui",         # Help & logging
    "cogs.admin",      # Admin commands
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
    # Replaced by the cogs.tournament package below — the old cog registered
    # its own /tournament group, so loading both would collide.
    # "cogs.extras.tournament",
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
    "cogs.clans.clan",
    "cogs.clans.clan_war",
    "cogs.extras.mastery",
    "cogs.extras.achievements",
    "cogs.admin.audit",
    "cogs.economy.chat_xp",   # chat EXP for trainer + equipped bey
    "cogs.admin.console",    # single /admin command for every admin action
    "cogs.tournament",       # scheduled tournament system
]

# ── Playwright Chromium auto-install ───────────────────────────────────────────

def _ensure_chromium() -> None:
    """
    Install the Playwright Chromium browser binary if it isn't present yet.
    This is required on fresh deployments where `playwright install chromium`
    has never been run (pip install alone doesn't download the browser).
    Runs synchronously at startup — takes ~5 s on first boot, instant after.
    """
    try:
        from playwright.sync_api import sync_playwright
        # Quick probe: if we can launch and close chromium, it's already installed
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            browser.close()
        logger.info("🎭 Playwright Chromium already installed — skipping download.")
    except Exception:
        logger.info("🎭 Playwright Chromium not found — installing now (one-time)…")
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info("🎭 Playwright Chromium installed successfully.")
        else:
            logger.warning(
                f"🎭 Playwright Chromium install failed:\n{result.stderr}\n"
                "Profile card PNGs will fall back to embed format."
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
            await ctx.send(f"❌ Member not found. Make sure you @mention them.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You don't have permission to use that command.")
        elif isinstance(error, commands.CommandNotFound):
            pass   # Silently ignore unknown commands
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
