"""
casino_premium.py  —  Premium casino pass system
Packs purchased with Beycoins. Each pack lasts 7 days and increases:
  - Max bet limit across all casino games
  - Daily casino coin bonus
  - LOWERS the casino -> Beycoin exchange tax (base 10%, down to 0%)

PACKS:
  pro    → 200,000 Beycoins | limit 70k  | daily 4,000  | tax 6%
  elite  → 500,000 Beycoins | limit 100k | daily 12,000 | tax 3%
  legend → 1,000,000 Beycoins | limit 150k | daily 20,000 | tax 0%

Usage:
  ;premium buy pro
  ;premium buy elite
  ;premium buy legend
  ;premium info
  ;premium status
"""

import discord
import time
from discord.ext import commands
from . import casino_wallet
from utils import database

# ── Pack definitions ──────────────────────────────────────────────────────────
PACKS = {
    "pro": {
        "display":     "⭐ Pro",
        "price":       200_000,          # Beycoins
        "duration":    7 * 86400,        # 7 days in seconds
        "max_bet":     70_000,
        "daily_bonus": 4_000,
        "exchange_tax": 0.06,
        "color":       0x3498db,
        "emoji":       "⭐",
    },
    "elite": {
        "display":     "💎 Elite",
        "price":       500_000,
        "duration":    7 * 86400,
        "max_bet":     100_000,
        "daily_bonus": 12_000,
        "exchange_tax": 0.03,
        "color":       0x9b59b6,
        "emoji":       "💎",
    },
    "legend": {
        "display":     "👑 Legend",
        "price":       1_000_000,
        "duration":    7 * 86400,
        "max_bet":     150_000,
        "daily_bonus": 20_000,
        "exchange_tax": 0.00,
        "color":       0xf1c40f,
        "emoji":       "👑",
    },
}

BASE_MAX_BET   = 50_000
BASE_DAILY     = 500
BASE_EXCHANGE_TAX = 0.10   # 10% tax when selling casino coins -> Beycoins
PACK_DURATION_DAYS = 7


# ── Public helpers (imported by game files) ───────────────────────────────────

async def get_premium(user_id: int) -> dict | None:
    """Return active pack dict (with 'key' and 'expires') or None."""
    data = await casino_wallet._load_async()
    uid  = str(user_id)
    prem = data.get(uid, {}).get("premium")
    if not prem:
        return None
    if time.time() > prem["expires"]:
        # expired — strip it
        async with casino_wallet._lock:
            d = casino_wallet._load()
            d.get(uid, {}).pop("premium", None)
            casino_wallet._save(d)
        return None
    return prem


async def get_max_bet(user_id: int) -> int:
    """Return this user's effective max bet."""
    prem = await get_premium(user_id)
    if prem:
        return PACKS[prem["key"]]["max_bet"]
    return BASE_MAX_BET


async def get_daily_bonus(user_id: int) -> int:
    """Return this user's effective daily bonus amount."""
    prem = await get_premium(user_id)
    if prem:
        return PACKS[prem["key"]]["daily_bonus"]
    return BASE_DAILY


async def grant_premium(user_id: int, pack_key: str) -> int:
    """Activate a pack for a user, bypassing payment. Returns the expiry ts.

    Used by redeem codes and admin gifts. If a pass is already active the new
    one replaces it rather than stacking, matching the buy flow.
    """
    if pack_key not in PACKS:
        raise ValueError(f"unknown pack: {pack_key}")
    expires = int(time.time()) + PACKS[pack_key]["duration"]
    async with casino_wallet._lock:
        data = casino_wallet._load()
        uid_str = str(user_id)
        data.setdefault(uid_str, {})
        data[uid_str]["premium"] = {"key": pack_key, "expires": expires}
        casino_wallet._save(data)
    return expires


async def get_exchange_tax(user_id: int) -> float:
    """Return this user's effective casino -> Beycoin exchange tax rate (0.0-1.0).

    Base is 10%. Premium packs lower it, Legend removes it entirely (0%).
    """
    prem = await get_premium(user_id)
    if prem:
        return PACKS[prem["key"]].get("exchange_tax", BASE_EXCHANGE_TAX)
    return BASE_EXCHANGE_TAX


# ── Cog ───────────────────────────────────────────────────────────────────────

class CasinoPremiumCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── ;premium ─────────────────────────────────────────────────────────────
    @commands.group(name="premium", invoke_without_command=True)
    async def premium(self, ctx: commands.Context):
        """Casino premium pass commands."""
        await self._send_info(ctx)

    @premium.command(name="info")
    async def premium_info(self, ctx: commands.Context):
        """Show all available premium packs."""
        await self._send_info(ctx)

    async def _send_info(self, ctx: commands.Context):
        e = discord.Embed(
            title="👑 Casino Premium Passes",
            description=(
                "Upgrade your casino experience for **7 days**.\n"
                f"Base limit for all games: 🪙 **{BASE_MAX_BET:,}** | Daily: 🪙 **{BASE_DAILY:,}**\n"
                f"Base exchange tax: **{int(BASE_EXCHANGE_TAX*100)}%**\n\n"
                "Purchase with: `;premium buy <pack>`"
            ),
            color=0xf1c40f
        )
        for key, p in PACKS.items():
            e.add_field(
                name=f"{p['emoji']} {p['display']} — 🪙 {p['price']:,} Beycoins",
                value=(
                    f"Max bet limit: **{p['max_bet']:,}** casino coins\n"
                    f"Daily bonus: **{p['daily_bonus']:,}** casino coins\n"
                    f"Exchange tax: **{int(p.get('exchange_tax', BASE_EXCHANGE_TAX)*100)}%** "
                    f"*(base {int(BASE_EXCHANGE_TAX*100)}%)*\n"
                    f"Duration: **{PACK_DURATION_DAYS} days**\n"
                    f"Buy: `;premium buy {key}`"
                ),
                inline=False
            )
        e.set_footer(text="Limits apply to: Slots, Blackjack, Coinflip, Dice, Mines, Tower, Higher/Lower, Roulette, Crash")
        await ctx.send(embed=e)

    @premium.command(name="buy")
    async def premium_buy(self, ctx: commands.Context, pack: str = None):
        """Purchase a premium casino pass.
        Usage: ;premium buy pro | elite | legend
        """
        if not pack or pack.lower() not in PACKS:
            keys = " | ".join(PACKS.keys())
            return await ctx.send(f"❌ Unknown pack. Choose: `{keys}`\nSee `;premium info` for details.")

        key  = pack.lower()
        p    = PACKS[key]
        uid  = ctx.author.id

        # Check Beycoin balance
        profile = database.get_user(uid)
        bal     = profile.get("coins", 0)
        if bal < p["price"]:
            return await ctx.send(
                f"❌ Not enough Beycoins.\n"
                f"Need: 🪙 **{p['price']:,}** | You have: 🪙 **{bal:,}**"
            )

        # Check existing premium
        existing = await get_premium(uid)
        if existing:
            existing_p = PACKS[existing["key"]]
            # Warn if downgrading
            if existing_p["max_bet"] > p["max_bet"]:
                return await ctx.send(
                    f"⚠️ You already have **{existing_p['display']}** which is a higher tier.\n"
                    f"You cannot downgrade while it's active."
                )

        # Deduct Beycoins
        profile["coins"] -= p["price"]
        database.update_user(uid, profile)

        # Store premium in wallet data
        expires = int(time.time()) + p["duration"]
        async with casino_wallet._lock:
            data = casino_wallet._load()
            uid_str = str(uid)
            data.setdefault(uid_str, {})
            data[uid_str]["premium"] = {
                "key":     key,
                "expires": expires,
            }
            casino_wallet._save(data)

        expires_ts = f"<t:{expires}:F>"
        e = discord.Embed(
            title=f"{p['emoji']} {p['display']} Pack Activated!",
            color=p["color"]
        )
        e.add_field(name="Max Bet",     value=f"🪙 {p['max_bet']:,} casino coins", inline=True)
        e.add_field(name="Daily Bonus", value=f"🪙 {p['daily_bonus']:,} casino coins", inline=True)
        e.add_field(name="Exchange Tax",
                    value=f"**{int(p.get('exchange_tax', BASE_EXCHANGE_TAX)*100)}%** "
                          f"(was {int(BASE_EXCHANGE_TAX*100)}%)", inline=True)
        e.add_field(name="Expires",     value=expires_ts, inline=False)
        e.add_field(name="Beycoins Spent", value=f"🪙 {p['price']:,}", inline=True)
        e.add_field(name="Beycoin Balance", value=f"🪙 {profile['coins']:,}", inline=True)
        e.set_footer(text="Your new limits are active immediately across all casino games!")
        await ctx.send(embed=e)

    @premium.command(name="status")
    async def premium_status(self, ctx: commands.Context):
        """Check your current premium pass status."""
        uid  = ctx.author.id
        prem = await get_premium(uid)

        if not prem:
            max_bet = BASE_MAX_BET
            daily   = BASE_DAILY
            e = discord.Embed(
                title="🎰 Premium Status",
                description=(
                    f"You have no active premium pass.\n\n"
                    f"Max bet: 🪙 **{max_bet:,}**\n"
                    f"Daily bonus: 🪙 **{daily:,}**\n"
                    f"Exchange tax: **{int(BASE_EXCHANGE_TAX*100)}%**\n\n"
                    f"See `;premium info` to upgrade!"
                ),
                color=0x95a5a6
            )
        else:
            p       = PACKS[prem["key"]]
            expires = prem["expires"]
            secs_left = int(expires - time.time())
            days_left = secs_left // 86400
            hours_left = (secs_left % 86400) // 3600
            e = discord.Embed(
                title=f"{p['emoji']} {p['display']} Pass — Active",
                color=p["color"]
            )
            e.add_field(name="Max Bet",     value=f"🪙 {p['max_bet']:,}", inline=True)
            e.add_field(name="Daily Bonus", value=f"🪙 {p['daily_bonus']:,}", inline=True)
            e.add_field(name="Exchange Tax",
                        value=f"{int(p.get('exchange_tax', BASE_EXCHANGE_TAX)*100)}%", inline=True)
            e.add_field(name="Expires",     value=f"<t:{expires}:F>", inline=False)
            e.add_field(name="Time Left",   value=f"**{days_left}d {hours_left}h**", inline=True)

        await ctx.send(embed=e)


async def setup(bot):
    await bot.add_cog(CasinoPremiumCog(bot))
