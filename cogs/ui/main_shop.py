"""
ui/main_shop.py
---------------
Unified `;shop` command — shows all purchasable content in one place
via interactive section tabs.

Sections
--------
  ⚙️  Parts       — performance parts (rings, disks, drivers)
  👑  Premium     — casino premium passes (Pro / Elite / Legend)
  🎁  Booster     — Beyblade booster packs (battle beys)
  🖼️  Avatar Packs — cosmetic avatar packs (Common / Rare / Epic / Legendary)

Usage
-----
  ;shop                — open the main shop (defaults to Parts tab)
  ;shop parts          — jump straight to Parts
  ;shop premium        — jump straight to Premium
  ;shop booster        — jump straight to Booster
  ;shop avatar         — jump straight to Avatar Packs

Buying is still done with the existing commands in each subsystem:
  ;buy <part>          — buy a part
  ;premium buy <tier>  — buy a premium pass
  ;buy booster [n]     — buy booster pack(s)
  ;buypack <tier>      — buy an avatar pack
"""

from __future__ import annotations

import discord
from discord.ext import commands
from discord import ui

# ── Import constants from each subsystem ──────────────────────────────────────
# Parts (economy/shop.py)
from cogs.economy.shop import PARTS_CATALOG, PART_TYPE_EMOJI, PART_TYPE_LABEL

# Casino premium (casino/casino_premium.py)
from cogs.casino.casino_premium import PACKS as PREMIUM_PACKS, PACK_DURATION_DAYS

# Booster (economy/shop.py)
from cogs.economy.shop import BOOSTER_PACK_PRICE, BOOSTER_PACK_ROLLS

# Avatar packs (avatar/avatar_shop.py)
from cogs.avatar.avatar_shop import PACK_PRICE, PACK_DISPLAY, PACK_EMOJI, PACK_GUARANTEE

# ── Color palette ─────────────────────────────────────────────────────────────
COLOR_PARTS   = discord.Color.teal()
COLOR_PREMIUM = discord.Color.gold()
COLOR_BOOSTER = 0xFFD700
COLOR_AVATAR  = discord.Color.purple()
COLOR_HOME    = 0x5865F2  # blurple

# ── Section keys ──────────────────────────────────────────────────────────────
SECTION_HOME    = "home"
SECTION_PARTS   = "parts"
SECTION_PREMIUM = "premium"
SECTION_BOOSTER = "booster"
SECTION_AVATAR  = "avatar"

VALID_ARGS = {
    "parts":   SECTION_PARTS,
    "part":    SECTION_PARTS,
    "premium": SECTION_PREMIUM,
    "pass":    SECTION_PREMIUM,
    "booster": SECTION_BOOSTER,
    "pack":    SECTION_BOOSTER,
    "avatar":  SECTION_AVATAR,
    "avatars": SECTION_AVATAR,
    "ap":      SECTION_AVATAR,
}

ITEMS_PER_PAGE = 5


# ══════════════════════════════════════════════════════════════════════════════
#  Embed builders
# ══════════════════════════════════════════════════════════════════════════════

def _home_embed() -> discord.Embed:
    e = discord.Embed(
        title="🛒 Beycord Shop",
        description=(
            "Welcome! Pick a section below to browse.\n"
            "──────────────────────────────────────────"
        ),
        color=COLOR_HOME,
    )
    e.add_field(
        name="⚙️  Parts",
        value=(
            "Performance upgrades for your Beyblade.\n"
            f"**{len(PARTS_CATALOG)}** parts available | Paid with **Beycoins**\n"
            "`;buy <part name>` to purchase"
        ),
        inline=False,
    )
    e.add_field(
        name="👑  Premium Pass",
        value=(
            "Casino passes that boost bet limits & daily bonuses.\n"
            "**3** tiers: Pro · Elite · Legend | Paid with **Beycoins**\n"
            "`;premium buy <tier>` to purchase"
        ),
        inline=False,
    )
    e.add_field(
        name="🎁  Booster Packs",
        value=(
            f"Packs of **{BOOSTER_PACK_ROLLS}** exclusive Beyblades per pack.\n"
            f"**{BOOSTER_PACK_PRICE:,} coins** per pack | Paid with **Beycoins**\n"
            "`;buy booster [amount]` to purchase"
        ),
        inline=False,
    )
    e.add_field(
        name="🖼️  Avatar Packs",
        value=(
            "Cosmetic avatar packs with guaranteed rarity pulls.\n"
            "**4** tiers: Common · Rare · Epic · Legendary | Paid with **Beycoins**\n"
            "`;buypack <tier>` to purchase"
        ),
        inline=False,
    )
    e.set_footer(text="Use the buttons below to browse each section")
    return e


def _parts_pages() -> list[discord.Embed]:
    chunks = [PARTS_CATALOG[i:i + ITEMS_PER_PAGE]
              for i in range(0, len(PARTS_CATALOG), ITEMS_PER_PAGE)]
    pages: list[discord.Embed] = []
    total = len(chunks)

    for idx, chunk in enumerate(chunks):
        e = discord.Embed(
            title="⚙️ Parts Shop",
            description=(
                "Upgrade your Beyblade with performance parts!\n"
                "**Currency:** Beycoins 💰\n"
                "**Buy:** `;buy <part name>`\n"
                "──────────────────────────────────────"
            ),
            color=COLOR_PARTS,
        )
        for part in chunk:
            stat  = part["stat"].capitalize()
            ptype = PART_TYPE_LABEL.get(part["type"], part["type"].title())
            emoji = PART_TYPE_EMOJI.get(part["type"], "🔩")
            e.add_field(
                name=(
                    f"{emoji} **{part['name']}** [{ptype}] "
                    f"— {part['price']:,} coins"
                ),
                value=f"{part['desc']} *(+{part['bonus']} {stat})*",
                inline=False,
            )
        e.set_footer(text=f"Page {idx + 1}/{total} • `;sell <part>` sells for 50% back")
        pages.append(e)
    return pages


def _premium_embed() -> discord.Embed:
    e = discord.Embed(
        title="👑 Casino Premium Passes",
        description=(
            f"Passes last **{PACK_DURATION_DAYS} days** and boost casino limits.\n"
            "**Currency:** Beycoins 💰\n"
            "**Buy:** `;premium buy <tier>`\n"
            "──────────────────────────────────────"
        ),
        color=COLOR_PREMIUM,
    )
    for key, pack in PREMIUM_PACKS.items():
        e.add_field(
            name=(
                f"{pack['emoji']} **{pack['display']}** "
                f"— {pack['price']:,} Beycoins"
            ),
            value=(
                f"Max Bet: **{pack['max_bet']:,}** casino coins\n"
                f"Daily Bonus: **+{pack['daily_bonus']:,}** casino coins/day\n"
                f"Duration: {PACK_DURATION_DAYS} days"
            ),
            inline=False,
        )
    e.set_footer(text="`;premium info` to see your active pass · `;premium status` for expiry")
    return e


def _booster_embed() -> discord.Embed:
    e = discord.Embed(
        title="🎁 Booster Packs",
        description=(
            f"Each pack reveals **{BOOSTER_PACK_ROLLS} Beyblades** from the "
            f"exclusive booster-only pool.\n"
            f"You receive **1 random Bey** per pack.\n"
            f"Cannot appear in normal spawns or battle rewards.\n"
            "**Currency:** Beycoins 💰\n"
            "──────────────────────────────────────"
        ),
        color=COLOR_BOOSTER,
    )
    e.add_field(
        name=f"🎴 Booster Pack — {BOOSTER_PACK_PRICE:,} Beycoins each",
        value=(
            f"• Reveals **{BOOSTER_PACK_ROLLS}** exclusive Beys\n"
            "• You keep **1** (first in reveal)\n"
            "• Rarity weighted — Legendaries are rare!\n"
            "• Duplicates? Choose auto-sell for instant coins"
        ),
        inline=False,
    )
    e.add_field(
        name="📦 Buy Commands",
        value=(
            "`;buy booster` — 1 pack\n"
            "`;buy booster 5` — 5 packs\n"
            "`;buy booster 50` — max (50 packs)"
        ),
        inline=False,
    )
    e.set_footer(text="Quicksell value is refunded on duplicates at rarity-based rates")
    return e


def _avatar_embed() -> discord.Embed:
    e = discord.Embed(
        title="🖼️ Avatar Packs",
        description=(
            "Cosmetic avatar packs with guaranteed rarity slots.\n"
            "**Currency:** Beycoins 💰\n"
            "**Buy:** `;buypack <tier>`\n"
            "**Browse avatars:** `;avatarshop`\n"
            "──────────────────────────────────────"
        ),
        color=COLOR_AVATAR,
    )
    for key, display in PACK_DISPLAY.items():
        price     = PACK_PRICE[key]
        emoji     = PACK_EMOJI.get(key, "📦")
        guarantee = PACK_GUARANTEE.get(key)
        slot1_txt = (
            f"Slot 1: **Guaranteed {guarantee}**"
            if guarantee
            else "Slot 1: **Random from pool**"
        )

        pools = {
            "common":    "Common, Rare, Epic",
            "rare":      "Common, Rare, Epic",
            "epic":      "Common, Rare, Epic, Legendary, Mythic",
            "legendary": "Common, Rare, Epic, Legendary, Mythic, Ultimate",
        }

        e.add_field(
            name=f"{emoji} **{display}** — {price:,} Beycoins",
            value=(
                f"{slot1_txt}\n"
                f"Slot 2: Random from pool\n"
                f"Pool: *{pools.get(key, '?')}*"
            ),
            inline=False,
        )
    e.set_footer(
        text="Duplicates refunded at 10–40% of pack price • Exclusive avatars: event/quest only"
    )
    return e


# ══════════════════════════════════════════════════════════════════════════════
#  View
# ══════════════════════════════════════════════════════════════════════════════

class MainShopView(ui.View):
    """
    Tabbed shop view with Home + 4 section buttons.
    Parts section supports multi-page pagination.
    """

    def __init__(self, author_id: int, section: str = SECTION_HOME) -> None:
        super().__init__(timeout=90)
        self.author_id    = author_id
        self.section      = section
        self.parts_pages  = _parts_pages()
        self.parts_page   = 0
        self._build_buttons()

    # ── Build / rebuild button set ────────────────────────────────────────────

    def _build_buttons(self) -> None:
        self.clear_items()

        # Row 0 — section tabs
        home_btn = ui.Button(
            label="🏠 Home",
            style=(
                discord.ButtonStyle.blurple
                if self.section == SECTION_HOME
                else discord.ButtonStyle.secondary
            ),
            row=0,
        )
        home_btn.callback = self._go_home
        self.add_item(home_btn)

        parts_btn = ui.Button(
            label="⚙️ Parts",
            style=(
                discord.ButtonStyle.blurple
                if self.section == SECTION_PARTS
                else discord.ButtonStyle.secondary
            ),
            row=0,
        )
        parts_btn.callback = self._go_parts
        self.add_item(parts_btn)

        premium_btn = ui.Button(
            label="👑 Premium",
            style=(
                discord.ButtonStyle.blurple
                if self.section == SECTION_PREMIUM
                else discord.ButtonStyle.secondary
            ),
            row=0,
        )
        premium_btn.callback = self._go_premium
        self.add_item(premium_btn)

        booster_btn = ui.Button(
            label="🎁 Booster",
            style=(
                discord.ButtonStyle.blurple
                if self.section == SECTION_BOOSTER
                else discord.ButtonStyle.secondary
            ),
            row=0,
        )
        booster_btn.callback = self._go_booster
        self.add_item(booster_btn)

        avatar_btn = ui.Button(
            label="🖼️ Avatars",
            style=(
                discord.ButtonStyle.blurple
                if self.section == SECTION_AVATAR
                else discord.ButtonStyle.secondary
            ),
            row=0,
        )
        avatar_btn.callback = self._go_avatar
        self.add_item(avatar_btn)

        # Row 1 — pagination (only shown on Parts section)
        if self.section == SECTION_PARTS and len(self.parts_pages) > 1:
            prev_btn = ui.Button(
                label="◀ Prev",
                style=discord.ButtonStyle.secondary,
                disabled=self.parts_page == 0,
                row=1,
            )
            prev_btn.callback = self._parts_prev
            self.add_item(prev_btn)

            page_btn = ui.Button(
                label=f"{self.parts_page + 1}/{len(self.parts_pages)}",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=1,
            )
            self.add_item(page_btn)

            next_btn = ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.secondary,
                disabled=self.parts_page >= len(self.parts_pages) - 1,
                row=1,
            )
            next_btn.callback = self._parts_next
            self.add_item(next_btn)

    # ── Current embed ─────────────────────────────────────────────────────────

    def current_embed(self) -> discord.Embed:
        if self.section == SECTION_HOME:
            return _home_embed()
        if self.section == SECTION_PARTS:
            return self.parts_pages[self.parts_page]
        if self.section == SECTION_PREMIUM:
            return _premium_embed()
        if self.section == SECTION_BOOSTER:
            return _booster_embed()
        if self.section == SECTION_AVATAR:
            return _avatar_embed()
        return _home_embed()

    # ── Auth check ────────────────────────────────────────────────────────────

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message(
                "Only the person who opened this shop can browse it.", ephemeral=True
            )
            return False
        return True

    # ── Section callbacks ─────────────────────────────────────────────────────

    async def _go_home(self, i: discord.Interaction) -> None:
        self.section = SECTION_HOME
        self._build_buttons()
        await i.response.edit_message(embed=self.current_embed(), view=self)

    async def _go_parts(self, i: discord.Interaction) -> None:
        self.section    = SECTION_PARTS
        self.parts_page = 0
        self._build_buttons()
        await i.response.edit_message(embed=self.current_embed(), view=self)

    async def _go_premium(self, i: discord.Interaction) -> None:
        self.section = SECTION_PREMIUM
        self._build_buttons()
        await i.response.edit_message(embed=self.current_embed(), view=self)

    async def _go_booster(self, i: discord.Interaction) -> None:
        self.section = SECTION_BOOSTER
        self._build_buttons()
        await i.response.edit_message(embed=self.current_embed(), view=self)

    async def _go_avatar(self, i: discord.Interaction) -> None:
        self.section = SECTION_AVATAR
        self._build_buttons()
        await i.response.edit_message(embed=self.current_embed(), view=self)

    # ── Parts pagination ──────────────────────────────────────────────────────

    async def _parts_prev(self, i: discord.Interaction) -> None:
        self.parts_page -= 1
        self._build_buttons()
        await i.response.edit_message(embed=self.current_embed(), view=self)

    async def _parts_next(self, i: discord.Interaction) -> None:
        self.parts_page += 1
        self._build_buttons()
        await i.response.edit_message(embed=self.current_embed(), view=self)

    # ── Timeout ───────────────────────────────────────────────────────────────

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


# ══════════════════════════════════════════════════════════════════════════════
#  Cog
# ══════════════════════════════════════════════════════════════════════════════

class MainShopCog(commands.Cog, name="MainShop"):
    """Unified shop — browse all purchasable content in one place."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(
        name="shop",
        aliases=["store"],
        help=(
            "Open the main Beycord shop.\n\n"
            "Sections:\n"
            "  ⚙️  Parts       — performance parts for your Bey\n"
            "  👑  Premium     — casino premium passes\n"
            "  🎁  Booster     — exclusive Beyblade booster packs\n"
            "  🖼️  Avatar Packs — cosmetic avatar packs\n\n"
            "Optional: jump to a section directly:\n"
            "  ;shop parts\n"
            "  ;shop premium\n"
            "  ;shop booster\n"
            "  ;shop avatar"
        ),
        brief="Open the main shop 🛒",
    )
    async def shop(self, ctx: commands.Context, section: str = "") -> None:
        target = VALID_ARGS.get(section.lower().strip(), SECTION_HOME)
        view   = MainShopView(author_id=ctx.author.id, section=target)
        await ctx.send(embed=view.current_embed(), view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MainShopCog(bot))
