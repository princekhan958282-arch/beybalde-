"""
cogs/shop.py
------------
Parts shop — buy performance parts with coins earned from battles.
Beyblades are NOT sold in the shop; obtain them through battles and spawns.

Commands
--------
  ;shop           — browse parts for sale
  ;buy <part>     — purchase a part by name
  ;sell <part>    — sell a part for 50% back
  ;coins [@user]  — check coin balance
  ;daily          — claim daily coin reward (24h cooldown)
"""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands
from discord import ui

from utils.database import get_user, update_user, load_beyblades
from utils.embeds import RARITY_EMOJIS

logger = logging.getLogger("beyblade_bot.shop")

# ── Config ────────────────────────────────────────────────────────────────────
DAILY_REWARD_MIN = 2000
DAILY_REWARD_MAX = 10000
DAILY_COOLDOWN_H = 4
SELL_RATIO       = 0.5

# ── Quick-sell payouts by rarity ──────────────────────────────────────────────
# Instant sell a Bey for a flat coin amount based on rarity.
# Lower than marketplace value — convenience costs.
BEY_QUICKSELL_VALUE: dict[str, int] = {
    "Common":    250,
    "Rare":      600,
    "Epic":      1_500,
    "Legendary": 4_000,
    "Mythic":    10_000,
    "Ultimate":  20_000,
}

# Rarities that cannot be quick-sold at all
BEY_QUICKSELL_BLOCKED: set[str] = {"Exclusive"}


# ── Parts catalog ─────────────────────────────────────────────────────────────
# Each part has a "type": "ring" | "disk" | "driver"
# One slot per type — 3 equipped parts max (matching real Beyblade anatomy).
#
# Schema:
#   stat         — the primary stat boosted
#   bonus        — how much it adds to that stat
#   penalty_stat — optional stat that is REDUCED (omit for no penalty)
#   penalty      — how much is subtracted from penalty_stat (omit for no penalty)
#
# Design rule: if bonus >= 20, there is always a penalty. Parts with bonus < 20
# are pure upgrades; parts with bonus 20-30 carry a moderate penalty; bonus 30+
# carries a heavy penalty. This gives players meaningful build choices.
PARTS_CATALOG: list[dict] = [
    # ── DRIVERS (17) ──────────────────────────────────────────────────────────
    # Pure / low-bonus drivers
    {"name": "Destroy Driver",      "type": "driver", "price":  800, "stat": "attack",  "bonus":  10, "desc": "Aggressive tip — pushes your bey into the opponent."},
    {"name": "Xtend+ Driver",       "type": "driver", "price":  800, "stat": "stamina", "bonus":  10, "desc": "Wide flat tip — maximises spin time."},
    {"name": "High Xtend Tip",      "type": "driver", "price":  700, "stat": "attack",  "bonus":   8, "desc": "Balanced tip with slight attack lean."},
    {"name": "Atomic Driver",       "type": "driver", "price":  850, "stat": "defense", "bonus":  12, "desc": "Ball tip — locks position for great defense."},
    {"name": "Revolve Driver",      "type": "driver", "price":  750, "stat": "stamina", "bonus":   8, "desc": "Low-friction tip for extended stamina."},
    {"name": "Xtreme Driver",       "type": "driver", "price":  900, "stat": "attack",  "bonus":  12, "desc": "Rubber tip — violent attack movement."},
    # Mid-bonus with tradeoffs
    {"name": "Bearing Driver",      "type": "driver", "price": 1000, "stat": "stamina", "bonus":  18, "desc": "Bearing-core tip — near-zero friction spin.", "penalty_stat": "attack",  "penalty": 10},
    {"name": "Drift Driver",        "type": "driver", "price": 1050, "stat": "stamina", "bonus":  20, "desc": "Drifts outward — extreme stamina, poor aggression.", "penalty_stat": "attack",  "penalty": 12},
    {"name": "Volcanic Driver",     "type": "driver", "price": 1100, "stat": "attack",  "bonus":  20, "desc": "Eruption rubber tip — surges at the opponent.", "penalty_stat": "defense", "penalty": 12},
    {"name": "Jolt Driver",         "type": "driver", "price": 1000, "stat": "attack",  "bonus":  18, "desc": "Spring-loaded tip — unpredictable burst speed.", "penalty_stat": "stamina", "penalty": 10},
    {"name": "Giga Driver",         "type": "driver", "price": 1150, "stat": "defense", "bonus":  20, "desc": "Wide flat base — anchors perfectly, but slows attack.", "penalty_stat": "attack",  "penalty": 14},
    {"name": "Wedge Driver",        "type": "driver", "price": 1200, "stat": "defense", "bonus":  22, "desc": "Reinforced wedge tip — fortress-like defense.", "penalty_stat": "stamina", "penalty": 14},
    # High-bonus heavy tradeoffs
    {"name": "Valkyrie Rush Driver","type": "driver", "price": 1800, "stat": "attack",  "bonus":  30, "desc": "Pure aggression — you rush or you lose.", "penalty_stat": "stamina", "penalty": 25},
    {"name": "Zero-G Driver",       "type": "driver", "price": 1700, "stat": "stamina", "bonus":  28, "desc": "Ultra-low contact tip — floats in battle.", "penalty_stat": "defense", "penalty": 22},
    {"name": "Hyper Driver",        "type": "driver", "price": 2000, "stat": "attack",  "bonus":  35, "desc": "Maximum aggression, minimum survival.", "penalty_stat": "defense", "penalty": 30},
    {"name": "Iron Shield Driver",  "type": "driver", "price": 1900, "stat": "defense", "bonus":  30, "desc": "Heavy iron tip — immovable but sluggish.", "penalty_stat": "attack",  "penalty": 25},
    {"name": "Omega Driver",        "type": "driver", "price": 2200, "stat": "stamina", "bonus":  35, "desc": "Bearing + rubber hybrid — incredible stamina sacrifice.", "penalty_stat": "attack",  "penalty": 30},

    # ── DISKS (17) ────────────────────────────────────────────────────────────
    # Pure / low-bonus disks
    {"name": "Over Disk",           "type": "disk",   "price":  650, "stat": "stamina", "bonus":   8, "desc": "Light disk — preserves spin energy."},
    {"name": "Nexus Disk",          "type": "disk",   "price":  750, "stat": "attack",  "bonus":  10, "desc": "Pointed edges — chips away at opponents."},
    {"name": "Dynamite Disk",       "type": "disk",   "price":  900, "stat": "defense", "bonus":  12, "desc": "Heavy disk — absorbs hits efficiently."},
    {"name": "Spread Disk",         "type": "disk",   "price":  700, "stat": "stamina", "bonus":   9, "desc": "Wide disk — centrifugal stamina boost."},
    {"name": "Quad Disk",           "type": "disk",   "price":  800, "stat": "attack",  "bonus":  11, "desc": "Four-prong disk — rotational attack power."},
    {"name": "Glaive Disk",         "type": "disk",   "price":  850, "stat": "defense", "bonus":  10, "desc": "Angled disk — deflects incoming force."},
    # Mid-bonus with tradeoffs
    {"name": "Yell Disk",           "type": "disk",   "price": 1000, "stat": "attack",  "bonus":  18, "desc": "Asymmetric disk — generates burst momentum.", "penalty_stat": "stamina", "penalty": 10},
    {"name": "Trans Disk",          "type": "disk",   "price": 1050, "stat": "stamina", "bonus":  20, "desc": "Transforming disk — shifts weight during spin.", "penalty_stat": "defense", "penalty": 12},
    {"name": "Heavy Disk",          "type": "disk",   "price": 1100, "stat": "defense", "bonus":  20, "desc": "Dense metal disk — outstanding defense weight.", "penalty_stat": "attack",  "penalty": 14},
    {"name": "Vortex Disk",         "type": "disk",   "price": 1150, "stat": "attack",  "bonus":  20, "desc": "Spiral channels — pulls opponent into attacks.", "penalty_stat": "defense", "penalty": 12},
    {"name": "Low Rider Disk",      "type": "disk",   "price": 1000, "stat": "stamina", "bonus":  18, "desc": "Low-profile center — friction-free stamina.", "penalty_stat": "attack",  "penalty": 10},
    {"name": "Forge Disk",          "type": "disk",   "price": 1200, "stat": "defense", "bonus":  22, "desc": "Forged-steel weight — absorbs burst force.", "penalty_stat": "stamina", "penalty": 14},
    # High-bonus heavy tradeoffs
    {"name": "Destroyer Disk",      "type": "disk",   "price": 1800, "stat": "attack",  "bonus":  30, "desc": "Blade-edged disk — relentless offense.", "penalty_stat": "defense", "penalty": 25},
    {"name": "Titan Disk",          "type": "disk",   "price": 1900, "stat": "defense", "bonus":  30, "desc": "Legendary weight — near-impenetrable block.", "penalty_stat": "stamina", "penalty": 22},
    {"name": "Phantom Disk",        "type": "disk",   "price": 2000, "stat": "stamina", "bonus":  30, "desc": "Hollow-core disk — ghostly spin endurance.", "penalty_stat": "attack",  "penalty": 25},
    {"name": "Apex Disk",           "type": "disk",   "price": 2300, "stat": "attack",  "bonus":  38, "desc": "Razor apex disk — catastrophic burst damage.", "penalty_stat": "stamina", "penalty": 32},
    {"name": "Eternity Disk",       "type": "disk",   "price": 2500, "stat": "stamina", "bonus":  40, "desc": "Infinite rotation design — defies spin decay.", "penalty_stat": "defense", "penalty": 30},

    # ── RINGS (16) ────────────────────────────────────────────────────────────
    # Pure / low-bonus rings
    {"name": "Flugel Wing",         "type": "ring",   "price":  600, "stat": "defense", "bonus":   8, "desc": "Wing blade — deflects attacks gracefully."},
    {"name": "Keel Ring",           "type": "ring",   "price":  700, "stat": "defense", "bonus":  10, "desc": "Low-profile ring — resists upper attacks."},
    {"name": "Slash Ring",          "type": "ring",   "price":  650, "stat": "attack",  "bonus":   9, "desc": "Angled blades — scrapes opponents on contact."},
    {"name": "Spin Ring",           "type": "ring",   "price":  700, "stat": "stamina", "bonus":   9, "desc": "Smooth ring — reduces air resistance."},
    {"name": "Impact Ring",         "type": "ring",   "price":  750, "stat": "attack",  "bonus":  10, "desc": "Spoked ring — delivers concentrated hits."},
    # Mid-bonus with tradeoffs
    {"name": "Prominence Ring",     "type": "ring",   "price": 1100, "stat": "attack",  "bonus":  18, "desc": "Prominent blades — massive contact power.", "penalty_stat": "defense", "penalty": 10},
    {"name": "Belial Nexus Ring",   "type": "ring",   "price": 1500, "stat": "defense", "bonus":  20, "desc": "Layered armor ring — legendary defense.", "penalty_stat": "attack",  "penalty": 12},
    {"name": "Tempest Ring",        "type": "ring",   "price": 1200, "stat": "attack",  "bonus":  20, "desc": "Storm blade ring — punishes defense types.", "penalty_stat": "stamina", "penalty": 14},
    {"name": "Fortress Ring",       "type": "ring",   "price": 1300, "stat": "defense", "bonus":  22, "desc": "Castle-wall ring — extreme impact absorption.", "penalty_stat": "attack",  "penalty": 14},
    {"name": "Spiral Ring",         "type": "ring",   "price": 1100, "stat": "stamina", "bonus":  18, "desc": "Helical ring — converts spin to endurance.", "penalty_stat": "attack",  "penalty": 10},
    {"name": "Pulse Ring",          "type": "ring",   "price": 1250, "stat": "stamina", "bonus":  20, "desc": "Resonance ring — stabilises spin frequency.", "penalty_stat": "defense", "penalty": 12},
    # High-bonus heavy tradeoffs
    {"name": "Dragon God Ring",     "type": "ring",   "price": 2000, "stat": "attack",  "bonus":  32, "desc": "Fang-edged ring — divine destruction.", "penalty_stat": "defense", "penalty": 26},
    {"name": "Valkyrie Claw Ring",  "type": "ring",   "price": 2100, "stat": "attack",  "bonus":  30, "desc": "Claw tips — rips through defenses.", "penalty_stat": "stamina", "penalty": 22},
    {"name": "Aegis Shield Ring",   "type": "ring",   "price": 2200, "stat": "defense", "bonus":  35, "desc": "Mythic shield ring — blocks almost anything.", "penalty_stat": "attack",  "penalty": 28},
    {"name": "Phantom Veil Ring",   "type": "ring",   "price": 2400, "stat": "stamina", "bonus":  35, "desc": "Ghostly ring — infinite endurance at a cost.", "penalty_stat": "defense", "penalty": 25},
    {"name": "Omega Blaze Ring",    "type": "ring",   "price": 2600, "stat": "attack",  "bonus":  40, "desc": "Blazing omega tips — unstoppable raw power.", "penalty_stat": "stamina", "penalty": 32},
]

PART_TYPE_EMOJI = {"ring": "💍", "disk": "🪨", "driver": "⚙️"}
PART_TYPE_LABEL = {"ring": "Ring", "disk": "Disk", "driver": "Driver"}
MAX_EQUIPPED_PER_TYPE = 1   # 1 ring + 1 disk + 1 driver = 3 slots total


def get_part_stat_deltas(equipped_part_names: list[str]) -> dict[str, int]:
    """
    Given a list of equipped part names, return the net stat deltas
    as {stat_name: delta} — positive or negative.

    Used by profile display AND by the battle engine to apply part effects.
    Example return: {"attack": +30, "stamina": -25, "defense": +20}
    """
    catalog = _parts_by_name()
    deltas: dict[str, int] = {}
    for name in equipped_part_names:
        part = catalog.get(name.lower())
        if not part:
            continue
        deltas[part["stat"]] = deltas.get(part["stat"], 0) + part["bonus"]
        ps = part.get("penalty_stat")
        pv = part.get("penalty", 0)
        if ps and pv:
            deltas[ps] = deltas.get(ps, 0) - pv
    return deltas

ITEMS_PER_PAGE = 6


def _parts_by_name() -> dict[str, dict]:
    return {p["name"].lower(): p for p in PARTS_CATALOG}


# ── Paginator view ────────────────────────────────────────────────────────────

class ShopView(ui.View):
    def __init__(self, pages: list[discord.Embed], author_id: int):
        super().__init__(timeout=60)
        self.pages     = pages
        self.page      = 0
        self.author_id = author_id
        self._refresh()

    def _refresh(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= len(self.pages) - 1

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message("Only the command caller can browse pages.", ephemeral=True)
            return False
        return True

    @ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, i: discord.Interaction, _: ui.Button):
        self.page -= 1
        self._refresh()
        await i.response.edit_message(embed=self.pages[self.page], view=self)

    @ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, i: discord.Interaction, _: ui.Button):
        self.page += 1
        self._refresh()
        await i.response.edit_message(embed=self.pages[self.page], view=self)


# ══════════════════════════════════════════════════════════════════════════════
#  Shop Cog
# ══════════════════════════════════════════════════════════════════════════════

class ShopCog(commands.Cog, name="Shop"):
    """Buy and sell performance parts, and claim daily coins."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── ;coins ────────────────────────────────────────────────────────────────
    # Retired: the unified card in cogs/economy/wallet_card.py owns ;bal,
    # ;balance and ;coins now, because it shows BOTH currencies. Keeping a
    # Beycoin-only version here would just shadow it.

    # ── ;daily ────────────────────────────────────────────────────────────────

    @commands.command(
        name="daily",
        help="Claim your daily coin reward. Resets every 24 hours.",
        brief="Claim daily coins 🎁",
    )
    async def daily(self, ctx: commands.Context) -> None:
        profile = get_user(ctx.author.id)
        now     = datetime.now(timezone.utc)
        last_s  = profile.get("last_daily")

        if last_s:
            last = datetime.fromisoformat(last_s)
            # Handle legacy naive timestamps stored without tzinfo
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            diff = now - last
            if diff < timedelta(hours=DAILY_COOLDOWN_H):
                remaining = timedelta(hours=DAILY_COOLDOWN_H) - diff
                h, rem    = divmod(int(remaining.total_seconds()), 3600)
                m         = rem // 60
                return await ctx.send(
                    f"⏰ {ctx.author.mention}, you already claimed today!\n"
                    f"Come back in **{h}h {m}m**."
                )

        reward = random.randint(DAILY_REWARD_MIN, DAILY_REWARD_MAX)
        profile["coins"]      = profile.get("coins", 0) + reward
        profile["last_daily"] = now.isoformat()
        update_user(ctx.author.id, profile)

        embed = discord.Embed(
            title="🎁 Daily Reward!",
            description=(
                f"{ctx.author.mention} collected **{reward:,} coins**!\n"
                f"💰 Balance: **{profile['coins']:,} coins**"
            ),
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    # ── ;shop [parts] ─────────────────────────────────────────────────────────

    @commands.command(
        name="partsbrowse",
        aliases=["partshop"],
        hidden=True,
        help=(
            "Browse the parts shop.\n\n"
            "Tip: use `;shop parts` to view this from the main shop.\n\n"
            "Buy with `;buy <part name>`\n"
            "Note: Beyblades are NOT sold in the shop. Obtain them through battles and spawns!"
        ),
        brief="Browse the parts shop ⚙️",
    )
    async def parts_shop(self, ctx: commands.Context) -> None:
        await self._show_parts_shop(ctx)

    async def _show_parts_shop(self, ctx: commands.Context) -> None:
        chunks = [PARTS_CATALOG[i:i + ITEMS_PER_PAGE] for i in range(0, len(PARTS_CATALOG), ITEMS_PER_PAGE)]
        pages: list[discord.Embed] = []

        for idx, chunk in enumerate(chunks):
            embed = discord.Embed(
                title="⚙️ Parts Shop",
                description=(
                    "Upgrade your Beyblade with performance parts!\n"
                    "`;buy <part name>` to purchase\n"
                    "──────────────────────────────────────"
                ),
                color=discord.Color.teal(),
            )
            for part in chunk:
                stat      = part["stat"].capitalize()
                ptype     = PART_TYPE_LABEL.get(part["type"], part["type"].title())
                emoji     = PART_TYPE_EMOJI.get(part["type"], "🔩")
                ps        = part.get("penalty_stat")
                pv        = part.get("penalty", 0)
                bonus_str = f"**+{part['bonus']} {stat}**"
                pen_str   = f"  `−{pv} {ps.capitalize()}`" if ps and pv else ""
                embed.add_field(
                    name=f"{emoji} {part['name']} [{ptype}] — **{part['price']:,} coins**",
                    value=f"{part['desc']}\n{bonus_str}{pen_str}",
                    inline=False,
                )
            embed.set_footer(text=f"Page {idx+1}/{len(chunks)} | `;daily` to earn more coins")
            pages.append(embed)

        await ctx.send(embed=pages[0], view=ShopView(pages, ctx.author.id))

    # ── ;buy <item> ───────────────────────────────────────────────────────────

    @commands.command(
        name="buy",
        help="Buy a part from the shop.\n\nExamples:\n  ;buy Destroy Driver\n  ;buy Bearing Driver\n\nNote: Beyblades cannot be purchased — obtain them through battles and spawns!",
        brief="Buy a part 🛍️",
    )
    async def buy(self, ctx: commands.Context, *, item_name: str) -> None:
        profile = get_user(ctx.author.id)
        coins   = profile.get("coins", 0)

        # Check parts first
        part = _parts_by_name().get(item_name.lower())
        if part:
            owned_parts = profile.get("parts", [])
            if any(p.lower() == part["name"].lower() for p in owned_parts):
                return await ctx.send(f"❌ You already own **{part['name']}**!")
            if coins < part["price"]:
                return await ctx.send(
                    f"❌ Not enough coins! **{part['name']}** costs **{part['price']:,}** "
                    f"but you have **{coins:,}**."
                )
            profile["coins"] = coins - part["price"]
            profile.setdefault("parts", []).append(part["name"])
            update_user(ctx.author.id, profile)
            return await ctx.send(
                f"✅ Bought **{part['name']}** for **{part['price']:,} coins**!\n"
                f"💰 Remaining: **{profile['coins']:,}**"
            )

        # Check if they're trying to buy a booster pack
        # Supports: ;buy booster  |  ;buy booster pack  |  ;buy booster 3  |  ;buy pack 5
        name_lower = item_name.lower().strip()
        booster_amount = None
        for prefix in ("booster pack", "booster", "pack"):
            if name_lower == prefix:
                booster_amount = 1
                break
            if name_lower.startswith(prefix + " "):
                suffix = name_lower[len(prefix):].strip()
                if suffix.isdigit():
                    booster_amount = int(suffix)
                    break
        if booster_amount is not None:
            booster_cog = self.bot.get_cog("Booster")
            if booster_cog is None:
                await ctx.send("❌ The Booster system is currently unavailable.")
                return
            await ctx.invoke(booster_cog.booster, amount=booster_amount)
            return

        await ctx.send(
            f"❌ **{item_name}** not found. Use `;shop` to see available parts.\n"
            f"💡 To buy a Booster Pack, use `;buy booster pack`."
        )

    # ── ;sell <part> ──────────────────────────────────────────────────────────

    @commands.command(
        name="sell",
        help="Sell a part from your collection for 50% of its purchase price.\n\nExample: ;sell Destroy Driver",
        brief="Sell a part 💸",
    )
    async def sell(self, ctx: commands.Context, *, part_name: str) -> None:
        profile = get_user(ctx.author.id)
        owned_parts: list[str] = profile.get("parts", [])

        # Case-insensitive match against owned parts
        match = next((p for p in owned_parts if p.lower() == part_name.lower()), None)
        if not match:
            return await ctx.send(
                f"❌ You don't own a part called **{part_name}**.\n"
                f"Use `;shop` to see available parts, or check your inventory."
            )

        # Look up catalog price — part may have been removed from catalog
        part = _parts_by_name().get(match.lower())
        if not part:
            return await ctx.send(
                f"❌ **{match}** is no longer in the catalog and cannot be sold.\n"
                f"Contact a server admin if you think this is a mistake."
            )

        refund = int(part["price"] * SELL_RATIO)

        owned_parts.remove(match)
        profile["parts"] = owned_parts

        # Auto-unequip if the sold part was currently equipped
        equipped: list[str] = profile.get("equipped_parts", [])
        was_equipped = match in equipped
        if was_equipped:
            equipped.remove(match)
            profile["equipped_parts"] = equipped

        profile["coins"] = profile.get("coins", 0) + refund
        update_user(ctx.author.id, profile)

        unequip_note = " *(auto-unequipped)*" if was_equipped else ""
        await ctx.send(
            f"💸 Sold **{match}**{unequip_note} for **{refund:,} coins** (50% of {part['price']:,}).\n"
            f"💰 Balance: **{profile['coins']:,} coins**"
        )

    # ── ;quicksell <bey name> ─────────────────────────────────────────────────

    @commands.command(
        name="quicksell",
        aliases=["qs", "fastsell"],
        help=(
            "Instantly sell a Beyblade from your inventory for a flat coin payout\n"
            "based on its rarity.\n\n"
            "Payout table:\n"
            "  Common    →      250 coins\n"
            "  Rare      →      600 coins\n"
            "  Epic      →    1,500 coins\n"
            "  Legendary →    4,000 coins\n"
            "  Mythic    →   10,000 coins\n"
            "  Ultimate  →   20,000 coins\n"
            "  Exclusive →   ❌ Cannot be quick-sold\n\n"
            "You'll be asked to confirm before the sale goes through.\n"
            "You cannot quick-sell your currently equipped Bey.\n\n"
            "Tip: listing on `;marketplace` usually earns more — quick-sell\n"
            "is for when you want coins immediately.\n\n"
            "Example: ;quicksell Dynamite Belial"
        ),
        brief="Instantly sell a Bey for coins 💨💸",
    )
    async def quicksell(self, ctx: commands.Context, *, bey_name: str) -> None:
        profile   = get_user(ctx.author.id)
        inventory = profile.get("inventory", [])

        match = next((b for b in inventory if b.lower() == bey_name.lower()), None)
        if not match:
            return await ctx.send(
                f"❌ **{bey_name}** not found in your inventory.\n"
                f"Use `;inventory` to see your Beys."
            )

        if profile.get("active_beyblade", "").lower() == match.lower():
            return await ctx.send(
                f"❌ **{match}** is your currently equipped Bey!\n"
                f"Unequip it before quick-selling."
            )

        # Look up rarity
        all_beys = load_beyblades()
        bey_data = all_beys.get(match, {})
        rarity   = bey_data.get("rarity", "Common")
        r_emoji  = RARITY_EMOJIS.get(rarity, "⚪")

        if rarity in BEY_QUICKSELL_BLOCKED:
            return await ctx.send(
                f"❌ {r_emoji} **{match}** is **{rarity}** rarity and cannot be quick-sold.\n"
                f"List it on `;marketplace` if you want to trade it."
            )

        payout = BEY_QUICKSELL_VALUE.get(rarity, BEY_QUICKSELL_VALUE["Common"])

        # Confirmation view
        class ConfirmView(ui.View):
            def __init__(self):
                super().__init__(timeout=30)
                self.confirmed = False

            @ui.button(label="✅ Confirm", style=discord.ButtonStyle.danger)
            async def confirm(self, interaction: discord.Interaction, _: ui.Button):
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("Not your sale!", ephemeral=True)
                    return
                await interaction.response.defer()
                self.confirmed = True
                self.stop()

            @ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
            async def cancel(self, interaction: discord.Interaction, _: ui.Button):
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("Not your sale!", ephemeral=True)
                    return
                await interaction.response.defer()
                self.stop()

        view = ConfirmView()
        confirm_msg = await ctx.send(
            f"⚠️ Quick-sell **{r_emoji} {match}** ({rarity}) for **{payout:,} coins**?\n"
            f"*Listing on `;marketplace` may earn more. This cannot be undone.*",
            view=view,
        )
        await view.wait()
        await confirm_msg.edit(view=None)

        if not view.confirmed:
            return await ctx.send(f"↩️ Quick-sell cancelled. **{match}** stays in your inventory.")

        # Re-fetch in case of concurrent updates
        profile   = get_user(ctx.author.id)
        inventory = profile.get("inventory", [])
        if match not in inventory:
            return await ctx.send(f"❌ **{match}** is no longer in your inventory.")

        inventory.remove(match)
        profile["inventory"] = inventory
        profile["coins"]     = profile.get("coins", 0) + payout
        update_user(ctx.author.id, profile)

        embed = discord.Embed(
            title="💨💸 Quick Sell!",
            description=(
                f"{r_emoji} **{match}** sold for **{payout:,} coins**!\n"
                f"Rarity: {rarity}\n"
                f"💰 New balance: **{profile['coins']:,} coins**"
            ),
            color=discord.Color.gold(),
        )
        await ctx.send(embed=embed)

    # ── ;myparts ──────────────────────────────────────────────────────────────

    @commands.command(
        name="myparts", aliases=["partsinv"],
        help="Show all your owned parts and your current equipped loadout.",
        brief="Show your parts inventory ⚙️",
    )
    async def myparts(self, ctx: commands.Context) -> None:
        profile  = get_user(ctx.author.id)
        owned    = profile.get("parts", [])
        equipped = profile.get("equipped_parts", [])
        catalog  = _parts_by_name()

        if not owned:
            return await ctx.send(
                f"❌ {ctx.author.mention}, you don't own any parts yet!\n"
                f"Buy some with `;shop` → `;buy <part name>`."
            )

        by_type: dict[str, list[str]] = {"ring": [], "disk": [], "driver": []}
        for name in owned:
            part = catalog.get(name.lower())
            if part:
                by_type[part["type"]].append(name)

        embed = discord.Embed(
            title=f"⚙️ {ctx.author.display_name}'s Parts",
            description=(
                "**Equipped parts apply in every battle.**\n"
                "`;equippart <name>` / `;unequippart <name>` to manage.\n"
                "─────────────────────────────────"
            ),
            color=discord.Color.teal(),
        )

        for ptype, label in PART_TYPE_LABEL.items():
            emoji = PART_TYPE_EMOJI[ptype]
            names = by_type.get(ptype, [])
            if not names:
                embed.add_field(name=f"{emoji} {label} Slot", value="*(none owned)*", inline=False)
                continue

            lines = []
            for name in names:
                part   = catalog.get(name.lower())
                if part:
                    ps     = part.get("penalty_stat")
                    pv     = part.get("penalty", 0)
                    pen    = f"  `−{pv} {ps.capitalize()}`" if ps and pv else ""
                    bonus  = f"+{part['bonus']} {part['stat'].capitalize()}{pen}"
                else:
                    bonus  = ""
                status = "✅ **EQUIPPED**" if name in equipped else "○ unequipped"
                lines.append(f"• **{name}** {bonus} — {status}")

            embed.add_field(
                name=f"{emoji} {label} Slot  *(1 max)*",
                value="\n".join(lines),
                inline=False,
            )

        embed.set_footer(text=f"Owned: {len(owned)} parts | Equipped: {len(equipped)}/3")
        await ctx.send(embed=embed)

    # ── ;equippart <name> ─────────────────────────────────────────────────────

    @commands.command(
        name="equippart", aliases=["ep", "equip_part"],
        help=(
            "Equip a part onto your active Beyblade.\n"
            "One part per slot (Ring / Disk / Driver).\n"
            "Equipping a new part in the same slot auto-replaces the old one.\n\n"
            "Example: ;equippart Destroy Driver"
        ),
        brief="Equip a part ✅",
    )
    async def equippart(self, ctx: commands.Context, *, part_name: str) -> None:
        profile = get_user(ctx.author.id)
        owned   = profile.get("parts", [])
        catalog = _parts_by_name()

        match = next((p for p in owned if p.lower() == part_name.lower()), None)
        if not match:
            return await ctx.send(
                f"❌ You don't own **{part_name}**.\n"
                f"Check `;myparts` or `;shop` to browse available parts."
            )

        part  = catalog.get(match.lower())
        if not part:
            return await ctx.send(
                f"❌ **{match}** is no longer in the parts catalog.\n"
                f"Contact an admin if you think this is a mistake."
            )
        ptype = part["type"]
        emoji = PART_TYPE_EMOJI[ptype]
        label = PART_TYPE_LABEL[ptype]

        equipped: list[str] = profile.get("equipped_parts", [])

        if match in equipped:
            return await ctx.send(f"✅ **{match}** is already equipped in your {label} slot!")

        # Remove any existing part of the same type from the slot
        replaced = None
        for existing in list(equipped):
            ep = catalog.get(existing.lower())
            if ep and ep["type"] == ptype:
                equipped.remove(existing)
                replaced = existing
                break

        equipped.append(match)
        profile["equipped_parts"] = equipped
        update_user(ctx.author.id, profile)

        ps  = part.get("penalty_stat")
        pv  = part.get("penalty", 0)
        pen_line = f"\n⬇️ −{pv} {ps.capitalize()} (tradeoff)" if ps and pv else ""
        msg = f"{emoji} **{match}** equipped in {label} slot!\n⬆️ +{part['bonus']} {part['stat'].capitalize()} now active in battles.{pen_line}"
        if replaced:
            msg += f"\n↩️ **{replaced}** moved back to inventory."
        await ctx.send(msg)

    # ── ;unequippart <name> ───────────────────────────────────────────────────

    @commands.command(
        name="unequippart", aliases=["uep", "unequip_part"],
        help=(
            "Remove a part from your equipped loadout.\n"
            "The part stays in your inventory and can be re-equipped anytime.\n\n"
            "Example: ;unequippart Destroy Driver"
        ),
        brief="Unequip a part ❌",
    )
    async def unequippart(self, ctx: commands.Context, *, part_name: str) -> None:
        profile  = get_user(ctx.author.id)
        equipped: list[str] = profile.get("equipped_parts", [])

        match = next((p for p in equipped if p.lower() == part_name.lower()), None)
        if not match:
            return await ctx.send(
                f"❌ **{part_name}** is not currently equipped.\n"
                f"Use `;myparts` to see your loadout."
            )

        equipped.remove(match)
        profile["equipped_parts"] = equipped
        update_user(ctx.author.id, profile)

        catalog = _parts_by_name()
        part    = catalog.get(match.lower())
        label   = PART_TYPE_LABEL.get(part["type"], "Part") if part else "Part"

        await ctx.send(
            f"✅ **{match}** unequipped from {label} slot.\n"
            f"Still in your inventory — `;equippart {match}` to re-equip."
        )




# ══════════════════════════════════════════════════════════════════════════════
#  Booster Pack System  (added by Friend 3)
# ══════════════════════════════════════════════════════════════════════════════

BOOSTER_PACK_PRICE = 5000   # coins per pack
BOOSTER_PACK_ROLLS = 7      # beys revealed per pack

def _load_booster_pool() -> list[dict]:
    """Return all Beys that have booster_exclusive == True."""
    try:
        all_beys: dict = load_beyblades()
    except Exception as e:
        logger.warning("Failed to load beyblades for booster pool: %s", e)
        return []
    return [bey for bey in all_beys.values() if bey.get("booster_exclusive") is True]


# Rarity → weight mapping (higher = more likely within the exclusive pool)
_RARITY_WEIGHTS = {
    "Common":    100,
    "Rare":       60,
    "Epic":       30,
    "Legendary":  10,
    "Mythic":      4,
    "Exclusive":   2,
    "Ultimate":    1,
}


def _weighted_roll(pool: list[dict], k: int = 1) -> list[dict]:
    """Roll k beys from the pool using rarity-based weights (with replacement)."""
    if not pool:
        return []
    weights = [_RARITY_WEIGHTS.get(b.get("rarity", "Common"), 10) for b in pool]
    return random.choices(pool, weights=weights, k=k)


class BoosterCog(commands.Cog, name="Booster"):
    """Buy booster packs to obtain exclusive Beys."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── ;booster ──────────────────────────────────────────────────────────────

    @commands.command(
        name="booster",
        aliases=["boosterpack", "pack"],
        help=(
            f"Purchase Booster Pack(s) for {BOOSTER_PACK_PRICE:,} coins each.\n\n"
            f"Each pack reveals {BOOSTER_PACK_ROLLS} Beys drawn exclusively from the\n"
            "booster-only pool — Beys that CANNOT appear in normal wild spawns\n"
            "or battle rewards.\n\n"
            f"You receive ONE random Bey per pack from your {BOOSTER_PACK_ROLLS}-Bey reveal.\n\n"
            "Buy multiple packs at once:\n"
            "  ;booster       — buy 1 pack\n"
            "  ;booster 5     — buy 5 packs\n"
            "  ;booster 50    — buy max (50 packs)\n\n"
            "Note: rarity still applies within the pack — Legendary pulls are\n"
            "rarer than Rare pulls.\n\n"
            "If you get duplicate Beys, you'll be asked if you want to auto-sell\n"
            "duplicates for instant coins at quicksell value!\n"
        ),
    )
    async def booster(self, ctx: commands.Context, amount: int = 1) -> None:
        """Buy one or more booster packs to obtain exclusive Beyblades."""
        MAX_PACKS = 50
        amount = max(1, min(amount, MAX_PACKS))
        total_cost = BOOSTER_PACK_PRICE * amount

        user  = get_user(ctx.author.id)
        coins = user.get("coins", 0)

        if coins < total_cost:
            shortage = total_cost - coins
            affordable = coins // BOOSTER_PACK_PRICE
            hint = (
                f" You can afford **{affordable}** pack(s) — try `;booster {affordable}`."
                if affordable > 0 else ""
            )
            await ctx.send(
                f"❌ You need **{total_cost:,} coins** for {amount} pack(s) "
                f"({BOOSTER_PACK_PRICE:,} × {amount}). "
                f"You only have **{coins:,} coins** — short by **{shortage:,}**.{hint}"
            )
            return

        pool = _load_booster_pool()
        if not pool:
            await ctx.send("❌ No booster-exclusive Beys are available right now.")
            return

        # Roll all packs and collect winnings
        user["coins"] = coins - total_cost
        user.setdefault("inventory", [])

        pack_results: list[tuple[list[dict], dict]] = []
        for _ in range(amount):
            revealed = _weighted_roll(pool, BOOSTER_PACK_ROLLS)
            won      = revealed[0]
            user["inventory"].append(won["name"])
            pack_results.append((revealed, won))

        # Save immediately after opening packs (coins deducted, inventory updated)
        # _handle_duplicate_sale_prompt will re-fetch and do its own save if needed
        update_user(ctx.author.id, user)

        # ── Detect duplicates among pack winnings ──
        won_names = [won["name"] for _, won in pack_results]
        seen: dict[str, int] = {}
        for name in won_names:
            seen[name] = seen.get(name, 0) + 1
        # duplicates = beys won MORE than once in this purchase (extras beyond the first)
        duplicates: dict[str, dict] = {
            name: next(won for _, won in pack_results if won["name"] == name)
            for name, count in seen.items()
            if count > 1
        }
        # extra_counts tracks how many extras (copies beyond 1) to remove if sold
        extra_counts: dict[str, int] = {name: count - 1 for name, count in seen.items() if count > 1}

        # Show initial results and ask about duplicates
        if amount == 1:
            # Single pack — no duplicates possible
            revealed, won = pack_results[0]
            rarity_emoji  = RARITY_EMOJIS.get(won.get("rarity", "Common"), "⚪")
            reveal_list   = "\n".join(
                f"{RARITY_EMOJIS.get(b.get('rarity', 'Common'), '⚪')} {b['name']}"
                for b in revealed
            )
            embed = discord.Embed(
                title="🎁 Booster Pack Opened!",
                description=(
                    f"**{BOOSTER_PACK_ROLLS} Beys revealed:**\n{reveal_list}\n\n"
                    f"✅ You received: {rarity_emoji} **{won['name']}**!"
                ),
                colour=0xFFD700,
            )
            embed.set_footer(text=f"Remaining coins: {user['coins']:,}")
            await ctx.send(embed=embed)
        else:
            # Multi-pack case — may have duplicates
            winnings_lines = "\n".join(
                f"{i+1}. {RARITY_EMOJIS.get(won.get('rarity', 'Common'), '⚪')} **{won['name']}**"
                for i, (_, won) in enumerate(pack_results)
            )
            embed = discord.Embed(
                title=f"🎁 {amount} Booster Packs Opened!",
                description=(
                    f"**Spent:** {total_cost:,} coins ({BOOSTER_PACK_PRICE:,} × {amount})\n\n"
                    f"**Beys received:**\n{winnings_lines}"
                ),
                colour=0xFFD700,
            )
            embed.set_footer(text=f"Remaining coins: {user['coins']:,} | All Beys added to inventory")
            await ctx.send(embed=embed)

            # ── Handle duplicates ──
            if duplicates:
                await self._handle_duplicate_sale_prompt(ctx, duplicates, extra_counts)
        

    async def _handle_duplicate_sale_prompt(
        self,
        ctx: commands.Context,
        duplicates: dict[str, dict],
        extra_counts: dict[str, int],
    ) -> None:
        """
        Prompt user to auto-sell duplicate Beys from booster packs.
        Re-fetches the profile to avoid stomping the pack-open save.
        Removes exactly `extra_counts[name]` copies of each duplicate.
        """
        all_beys = load_beyblades()
        total_refund = 0
        dup_list = []

        for bey_name, bey_data in duplicates.items():
            bey_lookup = all_beys.get(bey_name, bey_data)
            rarity = bey_lookup.get("rarity", "Common")
            extras = extra_counts.get(bey_name, 1)

            # Use quicksell value for this rarity, multiplied by number of extra copies
            refund = BEY_QUICKSELL_VALUE.get(rarity, BEY_QUICKSELL_VALUE["Common"]) * extras
            total_refund += refund
            r_emoji = RARITY_EMOJIS.get(rarity, "⚪")
            dup_list.append(f"{r_emoji} **{bey_name}** ×{extras} ({rarity}) → {refund:,} coins")

        dup_text = "\n".join(dup_list)

        # Confirmation view for auto-sale
        class DuplicateSaleView(ui.View):
            def __init__(self):
                super().__init__(timeout=30)
                self.sell_duplicates = False

            @ui.button(label="✅ Auto-Sell Duplicates", style=discord.ButtonStyle.success)
            async def sell_btn(self, interaction: discord.Interaction, _: ui.Button):
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("Not your sale!", ephemeral=True)
                    return
                await interaction.response.defer()
                self.sell_duplicates = True
                self.stop()

            @ui.button(label="❌ Keep Them", style=discord.ButtonStyle.secondary)
            async def keep_btn(self, interaction: discord.Interaction, _: ui.Button):
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("Not your decision!", ephemeral=True)
                    return
                await interaction.response.defer()
                self.stop()

        view = DuplicateSaleView()
        prompt_msg = await ctx.send(
            f"⚠️ **Duplicate Beys Detected!**\n\n"
            f"You received the same Beyblade(s) in multiple packs:\n{dup_text}\n\n"
            f"**Total refund if sold: {total_refund:,} coins**\n\n"
            f"Would you like to auto-sell these duplicates for instant coins?",
            view=view,
        )

        await view.wait()
        await prompt_msg.edit(view=None)

        if view.sell_duplicates:
            # Re-fetch to avoid stomping the pack-open save
            user_profile = get_user(ctx.author.id)
            inventory: list[str] = user_profile.get("inventory", [])

            # Remove exactly (count - 1) extra copies of each duplicate
            for dup_name, extras in extra_counts.items():
                removed = 0
                while removed < extras and dup_name in inventory:
                    inventory.remove(dup_name)
                    removed += 1

            user_profile["inventory"] = inventory
            user_profile["coins"] = user_profile.get("coins", 0) + total_refund
            update_user(ctx.author.id, user_profile)

            embed = discord.Embed(
                title="💸 Duplicates Auto-Sold!",
                description=(
                    f"**Sold:**\n{dup_text}\n\n"
                    f"💰 Refunded: **{total_refund:,} coins**\n"
                    f"New balance: **{user_profile['coins']:,} coins**"
                ),
                color=discord.Color.gold(),
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(
                f"👍 Kept your duplicates! You now have all these Beys in your inventory.\n"
                f"You can always `;quicksell` or `;sellbey` them later."
            )


# ══════════════════════════════════════════════════════════════════════════════
#  Player Marketplace  — sell Beys to other players
#
#  Listings are stored in the seller's profile under:
#    profile["marketplace_listings"] = [
#        {"bey_name": str, "price": int, "listed_at": ISO str}, ...
#    ]
#
#  Commands
#  --------
#    ;sellbey <bey name> <price>   — list a Bey for sale
#    ;cancellisting <bey name>     — pull your listing
#    ;marketplace                  — browse all active listings
#    ;buybey <seller @> <bey name> — purchase a listed Bey
# ══════════════════════════════════════════════════════════════════════════════

MARKETPLACE_FEE = 0.05   # 5 % transaction fee taken from seller
MARKETPLACE_MIN = 100    # minimum listing price


class MarketplaceCog(commands.Cog, name="Marketplace"):
    """Player-to-player Beyblade marketplace."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── ;sellbey <bey name> <price> ───────────────────────────────────────────

    @commands.command(
        name="sellbey",
        help=(
            "List one of your Beyblades for sale on the player marketplace.\n\n"
            "Example:\n"
            "  ;sellbey Dynamite Belial 8000\n\n"
            "A 5% marketplace fee is taken from the sale price when it sells.\n"
            "You cannot list your currently equipped Bey.\n"
            "Use `;cancellisting <bey name>` to pull the listing."
        ),
        brief="List a Bey for sale 🏷️",
    )
    async def sellbey(self, ctx: commands.Context, *, args: str) -> None:
        # Parse last token as price, rest as bey name
        parts = args.rsplit(" ", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            return await ctx.send(
                "❌ Usage: `;sellbey <bey name> <price>`\n"
                "Example: `;sellbey Dynamite Belial 8000`"
            )
        bey_name_input = parts[0].strip()
        price          = int(parts[1])

        if price < MARKETPLACE_MIN:
            return await ctx.send(
                f"❌ Minimum listing price is **{MARKETPLACE_MIN:,} coins**."
            )

        profile   = get_user(ctx.author.id)
        inventory = profile.get("inventory", [])

        # Case-insensitive match
        match = next((b for b in inventory if b.lower() == bey_name_input.lower()), None)
        if not match:
            return await ctx.send(
                f"❌ **{bey_name_input}** not found in your inventory.\n"
                f"Use `;inventory` to see your Beys."
            )

        # Prevent listing the equipped Bey
        if profile.get("active_beyblade", "").lower() == match.lower():
            return await ctx.send(
                f"❌ **{match}** is your currently equipped Bey!\n"
                f"Unequip it first before listing."
            )

        # Prevent duplicate listings for the same Bey
        listings: list[dict] = profile.setdefault("marketplace_listings", [])
        if any(l["bey_name"].lower() == match.lower() for l in listings):
            return await ctx.send(
                f"❌ **{match}** is already listed on the marketplace.\n"
                f"Use `;cancellisting {match}` to update the price."
            )

        # Remove from inventory and add listing
        inventory.remove(match)
        listings.append({
            "bey_name":  match,
            "price":     price,
            "listed_at": datetime.now(timezone.utc).isoformat(),
        })
        profile["inventory"]            = inventory
        profile["marketplace_listings"] = listings
        update_user(ctx.author.id, profile)

        fee = int(price * MARKETPLACE_FEE)
        await ctx.send(
            f"🏷️ **{match}** listed for **{price:,} coins**!\n"
            f"You'll receive **{price - fee:,} coins** after the 5% fee when it sells.\n"
            f"Use `;cancellisting {match}` to remove the listing."
        )

    # ── ;cancellisting <bey name> ─────────────────────────────────────────────

    @commands.command(
        name="cancellisting",
        aliases=["delistbey", "removelisting"],
        help="Cancel your marketplace listing for a Bey and return it to your inventory.\n\nExample: ;cancellisting Dynamite Belial",
        brief="Cancel a Bey listing ❌",
    )
    async def cancellisting(self, ctx: commands.Context, *, bey_name: str) -> None:
        profile  = get_user(ctx.author.id)
        listings = profile.get("marketplace_listings", [])

        match = next((l for l in listings if l["bey_name"].lower() == bey_name.lower()), None)
        if not match:
            return await ctx.send(
                f"❌ No active listing found for **{bey_name}**."
            )

        listings.remove(match)
        profile.setdefault("inventory", []).append(match["bey_name"])
        profile["marketplace_listings"] = listings
        update_user(ctx.author.id, profile)

        await ctx.send(
            f"↩️ Listing for **{match['bey_name']}** cancelled.\n"
            f"It's been returned to your inventory."
        )

    # ── ;marketplace ──────────────────────────────────────────────────────────

    @commands.command(
        name="marketplace",
        aliases=["market", "bmarket"],
        help=(
            "Browse all Beyblades currently listed for sale by other players.\n\n"
            "Purchase with: `;buybey @seller <bey name>`"
        ),
        brief="Browse the player marketplace 🏪",
    )
    async def marketplace(self, ctx: commands.Context) -> None:
        all_listings: list[tuple[discord.Member, dict]] = []

        # guild.members only contains cached members; fetch_members ensures complete coverage.
        # Requires the server Members Intent to be enabled in the bot's configuration.
        async for member in ctx.guild.fetch_members(limit=None):
            if member.bot:
                continue
            profile  = get_user(member.id)
            listings = profile.get("marketplace_listings")
            if not listings:
                continue
            for listing in listings:
                all_listings.append((member, listing))

        if not all_listings:
            return await ctx.send(
                "🏪 The marketplace is empty right now.\n"
                "List your own Beys with `;sellbey <bey name> <price>`!"
            )

        # Sort cheapest first
        all_listings.sort(key=lambda x: x[1]["price"])

        ITEMS_PER_PAGE = 8
        chunks = [all_listings[i:i + ITEMS_PER_PAGE] for i in range(0, len(all_listings), ITEMS_PER_PAGE)]
        pages: list[discord.Embed] = []
        all_beys_cache = load_beyblades()

        for idx, chunk in enumerate(chunks):
            embed = discord.Embed(
                title="🏪 Player Marketplace",
                description=(
                    "Beys listed for sale by other players.\n"
                    "`;buybey @seller <bey name>` to purchase.\n"
                    "──────────────────────────────────────"
                ),
                color=discord.Color.purple(),
            )
            for seller, listing in chunk:
                bey_data    = all_beys_cache.get(listing["bey_name"], {})
                rarity      = bey_data.get("rarity", "Common")
                r_emoji     = RARITY_EMOJIS.get(rarity, "⚪")
                embed.add_field(
                    name=f"{r_emoji} {listing['bey_name']} — **{listing['price']:,} coins**",
                    value=f"Seller: {seller.display_name} | Rarity: {rarity}",
                    inline=False,
                )
            embed.set_footer(
                text=f"Page {idx+1}/{len(chunks)} | {len(all_listings)} listing(s) total"
            )
            pages.append(embed)

        await ctx.send(embed=pages[0], view=ShopView(pages, ctx.author.id))

    # ── ;buybey @seller <bey name> ────────────────────────────────────────────

    @commands.command(
        name="buybey",
        help=(
            "Purchase a Bey listed on the marketplace.\n\n"
            "Example:\n"
            "  ;buybey @PlayerName Dynamite Belial\n\n"
            "The seller receives the listing price minus a 5% fee.\n"
            "You cannot buy your own listings."
        ),
        brief="Buy a Bey from another player 🛒",
    )
    async def buybey(self, ctx: commands.Context, seller: discord.Member, *, bey_name: str) -> None:
        if seller.id == ctx.author.id:
            return await ctx.send("❌ You can't buy your own listing!")
        if seller.bot:
            return await ctx.send("❌ You can't buy from a bot!")

        seller_profile = get_user(seller.id)
        listings       = seller_profile.get("marketplace_listings", [])

        listing = next((l for l in listings if l["bey_name"].lower() == bey_name.lower()), None)
        if not listing:
            return await ctx.send(
                f"❌ **{seller.display_name}** doesn't have **{bey_name}** listed.\n"
                f"Check `;marketplace` for available listings."
            )

        price          = listing["price"]
        buyer_profile  = get_user(ctx.author.id)
        buyer_coins    = buyer_profile.get("coins", 0)

        if buyer_coins < price:
            return await ctx.send(
                f"❌ **{listing['bey_name']}** costs **{price:,} coins** but you only have **{buyer_coins:,}**.\n"
                f"Short by **{price - buyer_coins:,} coins**."
            )

        fee           = int(price * MARKETPLACE_FEE)
        seller_payout = price - fee

        # Re-fetch seller profile to guard against concurrent purchases of the same listing
        seller_profile = get_user(seller.id)
        listings       = seller_profile.get("marketplace_listings", [])
        listing        = next((l for l in listings if l["bey_name"].lower() == bey_name.lower()), None)
        if not listing:
            return await ctx.send(
                f"❌ **{bey_name}** was just sold to someone else. Check `;marketplace` for other listings."
            )

        # Update seller first — remove listing and pay out
        listings.remove(listing)
        seller_profile["marketplace_listings"] = listings
        seller_profile["coins"] = seller_profile.get("coins", 0) + seller_payout
        update_user(seller.id, seller_profile)

        # Then deduct from buyer and grant Bey
        buyer_profile["coins"] = buyer_coins - price
        buyer_profile.setdefault("inventory", []).append(listing["bey_name"])
        update_user(ctx.author.id, buyer_profile)

        bey_data   = load_beyblades().get(listing["bey_name"], {})
        rarity     = bey_data.get("rarity", "Common")
        r_emoji    = RARITY_EMOJIS.get(rarity, "⚪")

        embed = discord.Embed(
            title="🤝 Marketplace Sale!",
            description=(
                f"{ctx.author.mention} bought {r_emoji} **{listing['bey_name']}** "
                f"from {seller.mention} for **{price:,} coins**!\n\n"
                f"💰 {ctx.author.display_name} paid: **{price:,} coins**\n"
                f"💸 {seller.display_name} received: **{seller_payout:,} coins** *(after 5% fee)*"
            ),
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ShopCog(bot))
    await bot.add_cog(BoosterCog(bot))
    await bot.add_cog(MarketplaceCog(bot))
