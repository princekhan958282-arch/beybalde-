"""
avatar_shop.py
--------------
Discord cog for avatar purchasing, pack opening, and inventory management.

Commands:
  ;avatarshop               — Browse all avatars available for direct purchase
  ;avatarpacks / ;apacks    — View available avatar packs
  ;buypack <pack>           — Open an avatar pack (common/rare/epic/legendary)
  ;buyavatar <id>           — Purchase a specific avatar by ID
  ;myavatars                — View owned avatars
  ;equipavatar <id>         — Equip an owned avatar
  ;unequipavatar            — Remove equipped avatar
  ;avatarinfo <id>          — Inspect an avatar's stats

Rarities (lowest → highest):
  Common, Rare, Epic, Legendary, Mythic, Ultimate, Exclusive

  Exclusive avatars cannot be pulled from packs — event/quest rewards only.

Pack pools:
  Common   → Common, Rare
  Rare     → Common, Rare, Epic
  Epic     → Common, Rare, Epic, Legendary, Mythic
  Legendary→ Common, Rare, Epic, Legendary, Mythic, Ultimate

Pack guarantees (slot 1 = EXACT rarity guaranteed, slot 2 = random from pool):
  Common    — 1 guaranteed Common (exact)               — 75,000 coins
  Rare      — 1 guaranteed Rare (exact)                 — 150,000 coins
  Epic      — 1 guaranteed Epic (exact)                 — 275,000 coins
  Legendary — 1 guaranteed Legendary (exact)            — 500,000 coins

Duplicate handling:
  If a pulled avatar is already owned, the player receives a coin refund
  equal to a percentage of the pack price based on the avatar's rarity.
"""

from __future__ import annotations

import random
from typing import Any, Optional

import discord
from discord.ext import commands

from .avatar_engine import avatar_engine, AvatarBonuses
from .avatar_utils import (
    build_avatar_embed,
    format_price,
    rarity_sort_key,
    RARITY_EMOJI,
    RARITY_COLORS,
)


# ── Rarity ordering ───────────────────────────────────────────────────────────

RARITY_ORDER: list[str] = [
    "Common",
    "Rare",
    "Epic",
    "Legendary",
    "Mythic",
    "Ultimate",
    "Exclusive",
]

# Index for quick comparisons
_RARITY_RANK: dict[str, int] = {r: i for i, r in enumerate(RARITY_ORDER)}


def rarity_rank(rarity: str) -> int:
    return _RARITY_RANK.get(rarity, -1)


# ── Pack definitions ──────────────────────────────────────────────────────────

# Rarities each pack can pull from (slot 2 random pull)
PACK_POOL: dict[str, list[str]] = {
    "common":    ["Common", "Rare", "Epic"],
    "rare":      ["Common", "Rare", "Epic"],
    "epic":      ["Common", "Rare", "Epic", "Legendary", "Mythic"],
    # Exclusive is only reachable from the legendary pack, at the banner rate
    # set in PACK_RARITY_WEIGHT. Weight alone was not enough — _pull_from_pool
    # filters candidates by this pool first, so it also has to be listed here.
    "legendary": ["Common", "Rare", "Epic", "Legendary", "Mythic", "Ultimate",
                  "Exclusive"],
}

# Exact guaranteed rarity for slot 1
PACK_GUARANTEE: dict[str, Optional[str]] = {
    "common":    None,
    "rare":      "Rare",
    "epic":      "Epic",
    "legendary": "Legendary",
}

PACK_PRICE: dict[str, int] = {
    "common":    75_000,
    "rare":      150_000,
    "epic":      275_000,
    "legendary": 500_000,
}

PACK_DISPLAY: dict[str, str] = {
    "common":    "Common Pack",
    "rare":      "Rare Pack",
    "epic":      "Epic Pack",
    "legendary": "Legendary Pack",
}

# ── Duplicate refund rates (% of pack price returned per rarity) ──────────────

DUPE_REFUND_RATE: dict[str, float] = {
    "Common":    0.10,   # 10%
    "Rare":      0.15,   # 15%
    "Epic":      0.20,   # 20%
    "Legendary": 0.25,   # 25%
    "Mythic":    0.30,   # 30%
    "Ultimate":  0.40,   # 40%
    "Exclusive": 0.50,   # 50% (shouldn't happen via packs but included for safety)
}

# ── Pack emoji ────────────────────────────────────────────────────────────────

PACK_EMOJI: dict[str, str] = {
    "common":    "⚪",
    "rare":      "🔵",
    "epic":      "🟣",
    "legendary": "🟡",
}


# ── Helper: weighted random pull from a rarity pool ───────────────────────────

# Pull weights per rarity, per pack (higher = more likely)
# Slot 2 random pull uses these. Slot 1 is always exact rarity, no weights needed.
PACK_RARITY_WEIGHT: dict[str, dict[str, int]] = {
    "common": {
        "Common":    850,   # ~85%
        "Rare":      120,   # ~12%
        "Epic":       30,   # ~3%
        "Exclusive":   0,
    },
    "rare": {
        "Common":    500,   # ~58.8%
        "Rare":      250,   # ~29.4%
        "Epic":      100,   # ~11.8%
        "Exclusive": 0,
    },
    "epic": {
        "Common":    500,   # ~61.3%
        "Rare":      250,   # ~30.7%
        "Epic":      100,   # ~12.3%
        "Legendary": 40,    # ~4.9%
        "Mythic":    15,    # ~1.8%
        "Exclusive": 0,
    },
    "legendary": {
        "Common":    500,   # ~58.4%
        "Rare":      250,   # ~29.2%
        "Epic":      100,   # ~11.7%
        "Legendary": 40,    # ~4.7%
        "Mythic":    28,    # ~3.0%  ← tuned
        "Ultimate":  5,     # ~0.6%
        # Exclusive was 0 in every pack, so Omega Prime had never been
        # obtainable. The limited banner avatars need a real (tiny) rate:
        # 1 in 923 total weight ~= 0.108%, i.e. the 0.001 draw chance.
        "Exclusive": 1,     # ~0.1%
    },
}


def _pull_from_pool(
    pack: str,
    pool: list[str],
    avatars_by_rarity: dict[str, list[dict]],
    exact_rarity: str | None = None,
) -> dict | None:
    """
    Pick one avatar from the rarity pool.
    - If exact_rarity is set, pick only from that rarity (slot 1 guarantee).
    - Otherwise, weighted random across the full pool using per-pack weights (slot 2).
    Returns None if no eligible avatars exist.
    """
    if exact_rarity is not None:
        candidates = avatars_by_rarity.get(exact_rarity, [])
        return random.choice(candidates) if candidates else None

    weight_table = PACK_RARITY_WEIGHT.get(pack, {})
    eligible_rarities = [
        r for r in pool
        if r in avatars_by_rarity
        and avatars_by_rarity[r]
        and weight_table.get(r, 0) > 0
    ]

    if not eligible_rarities:
        return None

    weights = [weight_table.get(r, 1) for r in eligible_rarities]
    chosen_rarity = random.choices(eligible_rarities, weights=weights, k=1)[0]
    return random.choice(avatars_by_rarity[chosen_rarity])


# ── Skills info button ────────────────────────────────────────────────────────

class AvatarSkillsView(discord.ui.View):
    """Adds a "Skills" button to an avatar card.

    Only attached when the avatar actually has a `skills` list — the flat
    bonus avatars have nothing extra to show, so they get no button.
    """

    def __init__(self, avatar: dict, *, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.avatar = avatar

    @discord.ui.button(label="Skills", emoji="\u26a1",
                       style=discord.ButtonStyle.primary)
    async def show_skills(self, interaction: discord.Interaction,
                          button: discord.ui.Button) -> None:
        av     = self.avatar
        rarity = av.get("rarity", "Common")
        # NOTE: no backslash escapes inside the f-string expressions — this
        # project targets Python 3.11 (runtime.txt), where that is a
        # SyntaxError and would stop the whole cog from loading.
        emoji  = RARITY_EMOJI.get(rarity, "\u26aa")
        name   = av["name"]
        embed  = discord.Embed(
            title=f"{emoji} {name} \u2014 Skills",
            color=RARITY_COLORS.get(rarity, 0xAAAAAA),
        )
        for i, sk in enumerate(av.get("skills") or [], 1):
            sk_name = sk.get("name", "Skill")
            embed.add_field(
                name=f"\u26a1 Skill {i} \u2014 {sk_name}",
                value=sk.get("description", "\u2014"),
                inline=False,
            )
        if not embed.fields:
            embed.description = "This avatar has no signature skills."
        img = av.get("image", "")
        if img.startswith("http"):
            embed.set_thumbnail(url=img)
        # Ephemeral so it does not clutter the channel for everyone else.
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class AvatarShop(commands.Cog, name="Avatar"):
    """Buy and manage your battle avatars."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Storage helpers (JSON flat-file) ──────────────────────────────────────

    def _get_player_coins(self, player_id: int) -> int:
        from utils.database import get_user
        return get_user(player_id).get("coins", 0)

    def _deduct_coins(self, player_id: int, amount: int) -> None:
        from utils.database import get_user, update_user
        profile = get_user(player_id)
        profile["coins"] = max(0, profile.get("coins", 0) - amount)
        update_user(player_id, profile)

    def _add_coins(self, player_id: int, amount: int) -> None:
        from utils.database import get_user, update_user
        profile = get_user(player_id)
        profile["coins"] = profile.get("coins", 0) + amount
        update_user(player_id, profile)

    def _player_owns(self, player_id: int, avatar_id: str) -> bool:
        from utils.database import player_owns_avatar
        return player_owns_avatar(player_id, avatar_id)

    def _add_to_inventory(self, player_id: int, avatar_id: str) -> None:
        from utils.database import add_avatar_to_inventory
        add_avatar_to_inventory(player_id, avatar_id)

    def _get_owned_avatar_ids(self, player_id: int) -> list[str]:
        from utils.database import get_avatar_inventory
        return get_avatar_inventory(player_id)

    def _get_equipped_id(self, player_id: int) -> str | None:
        from utils.database import get_equipped_avatar
        return get_equipped_avatar(player_id)

    # ── Pack internals ────────────────────────────────────────────────────────

    def _build_rarity_map(self, pool: list[str]) -> dict[str, list[dict]]:
        """Group all non-Exclusive avatars by rarity, filtered to pool."""
        result: dict[str, list[dict]] = {r: [] for r in pool}
        for av in avatar_engine.get_all_avatars():
            if av["rarity"] in result and av["rarity"] != "Exclusive":
                result[av["rarity"]].append(av)
        return result

    def _resolve_avatar_query(self, query: str) -> dict | None:
        """Resolve an avatar by ID or name (exact then partial)."""
        q = query.strip().lower()
        # Exact ID
        avatar = avatar_engine.get_avatar(q)
        if avatar:
            return avatar
        all_avatars = avatar_engine.get_all_avatars()
        # Exact name
        avatar = next((a for a in all_avatars if a["name"].lower() == q), None)
        if avatar:
            return avatar
        # Partial name — only if unambiguous
        matches = [a for a in all_avatars if q in a["name"].lower()]
        if len(matches) == 1:
            return matches[0]
        return None  # ambiguous or not found

    def _ambiguous_matches(self, query: str) -> list[dict]:
        """Return multiple partial-name matches (for disambiguation)."""
        q = query.strip().lower()
        return [a for a in avatar_engine.get_all_avatars() if q in a["name"].lower()]

    async def _resolve_pull(
        self,
        player_id: int,
        avatar: dict,
        pack_price: int,
    ) -> tuple[str, int]:
        """
        Grant avatar or refund on dupe.
        Returns (result_line, refund_amount).
        """
        emoji = RARITY_EMOJI.get(avatar["rarity"], "⚪")
        already_owned = self._player_owns(player_id, avatar["id"])

        if already_owned:
            rate = DUPE_REFUND_RATE.get(avatar["rarity"], 0.10)
            refund = int(pack_price * rate)
            self._add_coins(player_id, refund)
            return (
                f"{emoji} **{avatar['name']}** *({avatar['rarity']})* — "
                f"**Duplicate!** +{refund:,} coins refunded",
                refund,
            )
        else:
            self._add_to_inventory(player_id, avatar["id"])
            return (
                f"{emoji} **{avatar['name']}** *({avatar['rarity']})*  ✨ **NEW!**",
                0,
            )

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.command(name="avatar", aliases=["av"])
    async def avatar_main(self, ctx: commands.Context) -> None:
        """Avatar system overview — quick reference for all avatar commands."""
        player_id   = ctx.author.id
        equipped_id = self._get_equipped_id(player_id)
        owned_count = len(self._get_owned_avatar_ids(player_id))
        coins       = self._get_player_coins(player_id)

        equipped_text = "None"
        if equipped_id:
            av = avatar_engine.get_avatar(equipped_id)
            if av:
                emoji = RARITY_EMOJI.get(av["rarity"], "⚪")
                equipped_text = f"{emoji} **{av['name']}** ({av['rarity']})"

        embed = discord.Embed(
            title="🎭 Avatar System",
            description=(
                "Equip an avatar to gain passive battle bonuses.\n"
                f"You own **{owned_count}** avatar(s). Currently equipped: {equipped_text}"
            ),
            color=0xE67E22,
        )
        embed.add_field(
            name="🛒 Browse & Buy",
            value=(
                "`;avatarshop` / `;ashop` — Browse all avatars\n"
                "`;buyavatar <id>` / `;buya <id>` — Buy directly\n"
                "`;avatarinfo <id>` / `;ainfo <id>` — Inspect an avatar"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎴 Packs",
            value=(
                "`;avatarpacks` / `;apacks` — View packs & prices\n"
                "`;buypack <type>` / `;bpack <type>` — Open a pack\n"
                "Pack types: `common` • `rare` • `epic` • `legendary`"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚙️ Manage",
            value=(
                "`;myavatars` / `;avinv` — View your collection\n"
                "`;equipavatar <name or id>` / `;equipa <name or id>` — Equip an avatar\n"
                "`;unequipavatar` / `;unequipa` — Unequip current avatar"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Balance: {coins:,} coins")
        await ctx.send(embed=embed)

    @commands.command(name="avatarshop", aliases=["ashop"])
    async def avatar_shop(self, ctx: commands.Context) -> None:
        """Browse all avatars available for direct purchase."""
        all_avatars = sorted(
            avatar_engine.get_all_avatars(), key=rarity_sort_key, reverse=True
        )

        if not all_avatars:
            await ctx.send("❌ No avatars are available right now.")
            return

        owned_ids   = self._get_owned_avatar_ids(ctx.author.id)
        equipped_id = self._get_equipped_id(ctx.author.id)

        embed = discord.Embed(
            title="🎭 Avatar Shop",
            description=(
                "Equip an avatar to gain passive battle bonuses.\n"
                "Use `;buyavatar <id>` to purchase directly.\n"
                "Use `;avatarpacks` to open random packs.\n"
                "Use `;avatarinfo <id>` to inspect an avatar.\n\n"
                f"{'⚪ Common'} • {'🔵 Rare'} • {'🟣 Epic'} • "
                f"{'🟡 Legendary'} • {'🌸 Mythic'} • {'💠 Ultimate'} • {'🌟 Exclusive'}"
            ),
            color=0xE67E22,
        )

        for av in all_avatars:
            emoji  = RARITY_EMOJI.get(av["rarity"], "⚪")
            status = ""
            if av["id"] == equipped_id:
                status = " ✅"
            elif av["id"] in owned_ids:
                status = " 📦"

            embed.add_field(
                name=f"{emoji} {av['name']}{status}  —  {format_price(av['price'])}",
                value=f"`{av['id']}`  •  {av['rarity']}\n_{av['description']}_",
                inline=False,
            )

        coins = self._get_player_coins(ctx.author.id)
        embed.set_footer(text=f"Your balance: {coins:,} coins")
        await ctx.send(embed=embed)

    @commands.command(name="avatarpacks", aliases=["apacks"])
    async def avatar_packs(self, ctx: commands.Context) -> None:
        """View available avatar packs and their contents."""
        embed = discord.Embed(
            title="🎴 Avatar Packs",
            description=(
                "Each pack gives **2 avatars**.\n"
                "Slot 1 is the **guaranteed** pull. Slot 2 is random from the pool.\n"
                "Duplicate avatars are automatically refunded in coins.\n\n"
                "Use `;buypack <pack>` to open one."
            ),
            color=0x3498DB,
        )

        pack_info = [
            (
                "common",
                "1× guaranteed **Common** (exact)",
                "Common, Rare",
                "10–15%",
            ),
            (
                "rare",
                "1× guaranteed **Rare** (exact)",
                "Common, Rare, Epic",
                "15–20%",
            ),
            (
                "epic",
                "1× guaranteed **Epic** (exact)",
                "Common, Rare, Epic, Legendary, Mythic",
                "20–30%",
            ),
            (
                "legendary",
                "1× guaranteed **Legendary** (exact)",
                "Common, Rare, Epic, Legendary, Mythic, Ultimate",
                "25–40%",
            ),
        ]

        for pack_key, guarantee, pool_str, refund_range in pack_info:
            emoji = PACK_EMOJI[pack_key]
            price = PACK_PRICE[pack_key]
            embed.add_field(
                name=f"{emoji} {PACK_DISPLAY[pack_key]}  —  {price:,} coins",
                value=(
                    f"**Guarantee:** {guarantee}\n"
                    f"**Pool:** {pool_str}\n"
                    f"**Dupe refund:** {refund_range} of pack price\n"
                    f"`;buypack {pack_key}`"
                ),
                inline=False,
            )

        embed.add_field(
            name="🌟 Exclusive Avatars",
            value=(
                "Exclusive avatars **cannot be pulled from packs**.\n"
                "They are obtained through events, quests, and special rewards only."
            ),
            inline=False,
        )

        coins = self._get_player_coins(ctx.author.id)
        embed.set_footer(text=f"Your balance: {coins:,} coins")
        await ctx.send(embed=embed)

    @commands.command(name="buypack", aliases=["bpack", "openpack"])
    async def buy_pack(self, ctx: commands.Context, pack: str) -> None:
        """Open an avatar pack. Usage: ;buypack <common|rare|epic|legendary>"""
        pack = pack.lower()

        if pack not in PACK_PRICE:
            valid = ", ".join(PACK_PRICE.keys())
            await ctx.send(
                f"❌ Unknown pack `{pack}`. Valid packs: `{valid}`."
            )
            return

        player_id = ctx.author.id
        price     = PACK_PRICE[pack]
        coins     = self._get_player_coins(player_id)

        if coins < price:
            shortfall = price - coins
            await ctx.send(
                f"❌ Not enough coins for a **{PACK_DISPLAY[pack]}**.\n"
                f"Cost: {price:,} coins | You have: {coins:,} coins | "
                f"Need: {shortfall:,} more."
            )
            return

        # Deduct immediately
        self._deduct_coins(player_id, price)

        pool      = PACK_POOL[pack]
        guarantee = PACK_GUARANTEE[pack]
        rarity_map = self._build_rarity_map(pool)

        # Slot 1 — exact guaranteed rarity (or random if common)
        slot1 = _pull_from_pool(pack, pool, rarity_map, exact_rarity=guarantee)
        # Slot 2 — fully random from pool (common pack only gets 1 pull)
        slot2 = _pull_from_pool(pack, pool, rarity_map, exact_rarity=None) if pack != "common" else None

        if not slot1 and not slot2:
            # Shouldn't happen but refund gracefully
            self._add_coins(player_id, price)
            await ctx.send(
                "❌ No avatars are available in this pack's pool right now. "
                "You have been fully refunded."
            )
            return

        lines: list[str] = []
        total_refund = 0

        slot1_label = f"🎯 Guaranteed {guarantee}" if guarantee else "🎲 Random Pull"
        for i, (avatar, label) in enumerate([
            (slot1, slot1_label),
            (slot2, "🎲 Random Pull"),
        ], start=1):
            if avatar is None:
                lines.append(f"{label}: *(no avatar available)*")
                continue
            result_line, refund = await self._resolve_pull(player_id, avatar, price)
            lines.append(f"**{label}:** {result_line}")
            total_refund += refund

        remaining = self._get_player_coins(player_id)

        embed = discord.Embed(
            title=f"{PACK_EMOJI[pack]} {PACK_DISPLAY[pack]} Opened!",
            description="\n".join(lines),
            color=0x9B59B6,
        )

        footer_parts = [f"Remaining balance: {remaining:,} coins"]
        if total_refund:
            footer_parts.append(f"Total dupe refund: +{total_refund:,} coins")
        embed.set_footer(text=" • ".join(footer_parts))

        await ctx.send(embed=embed)

    @commands.command(name="avatarinfo", aliases=["ainfo"])
    async def avatar_info(self, ctx: commands.Context, *, query: str) -> None:
        """View detailed stats for an avatar (by name or ID)."""
        avatar = self._resolve_avatar_query(query)
        if not avatar:
            matches = self._ambiguous_matches(query)
            if len(matches) > 1:
                names = ", ".join(f"**{a['name']}**" for a in matches[:5])
                await ctx.send(f"❌ Multiple matches for `{query}`: {names}. Be more specific.")
            else:
                await ctx.send(f"❌ No avatar found matching `{query}`.")
            return

        owned_ids   = self._get_owned_avatar_ids(ctx.author.id)
        equipped_id = self._get_equipped_id(ctx.author.id)

        owned    = avatar["id"] in owned_ids
        equipped = avatar["id"] == equipped_id

        embed = build_avatar_embed(avatar, owned=owned, equipped=equipped)

        # Show image if the avatar has one
        image = avatar.get("image", "")
        if image:
            if image.startswith("http://") or image.startswith("https://"):
                embed.set_image(url=image)
            else:
                embed.add_field(name="🖼️ Image", value=f"`{image}`", inline=False)

        # Avatars with signature skills get a button to read them in full.
        if avatar.get("skills"):
            await ctx.send(embed=embed, view=AvatarSkillsView(avatar))
        else:
            await ctx.send(embed=embed)

    @commands.command(name="buyavatar", aliases=["buya"])
    async def buy_avatar(self, ctx: commands.Context, avatar_id: str) -> None:
        """Purchase a specific avatar from the shop."""
        avatar_id = avatar_id.lower()
        avatar    = avatar_engine.get_avatar(avatar_id)

        if not avatar:
            await ctx.send(f"❌ No avatar found with ID `{avatar_id}`.")
            return

        if avatar.get("rarity") == "Exclusive":
            await ctx.send(
                f"❌ **{avatar['name']}** is an **Exclusive** avatar and cannot be purchased directly.\n"
                "Exclusive avatars are obtained through events and special quests only."
            )
            return

        player_id = ctx.author.id

        if self._player_owns(player_id, avatar_id):
            await ctx.send(f"You already own **{avatar['name']}**.")
            return

        price = int(avatar["price"])
        coins = self._get_player_coins(player_id)

        if coins < price:
            shortfall = price - coins
            await ctx.send(
                f"❌ Not enough coins.\n"
                f"**{avatar['name']}** costs {format_price(price)} "
                f"and you have {coins:,} coins. "
                f"You need {shortfall:,} more."
            )
            return

        self._deduct_coins(player_id, price)
        self._add_to_inventory(player_id, avatar_id)

        emoji = RARITY_EMOJI.get(avatar["rarity"], "⚪")
        embed = discord.Embed(
            title="✅ Avatar Purchased!",
            description=(
                f"{emoji} You bought **{avatar['name']}** *({avatar['rarity']})*!\n"
                f"Use `;equipavatar {avatar_id}` to equip it."
            ),
            color=0x2ECC71,
        )
        embed.set_footer(text=f"Remaining balance: {coins - price:,} coins")
        await ctx.send(embed=embed)

    @commands.command(name="myavatars", aliases=["avatars", "myav", "avinv"])
    async def my_avatars(self, ctx: commands.Context) -> None:
        """View your owned avatars."""
        player_id   = ctx.author.id
        owned_ids   = self._get_owned_avatar_ids(player_id)
        equipped_id = self._get_equipped_id(player_id)

        if not owned_ids:
            await ctx.send(
                "You don't own any avatars yet. "
                "Use `;avatarshop` to browse or `;avatarpacks` to open packs."
            )
            return

        owned_avatars = sorted(
            [a for a in avatar_engine.get_all_avatars() if a["id"] in owned_ids],
            key=rarity_sort_key,
            reverse=True,
        )

        embed = discord.Embed(
            title=f"🎭 {ctx.author.display_name}'s Avatars",
            color=0x9B59B6,
        )

        for av in owned_avatars:
            emoji  = RARITY_EMOJI.get(av["rarity"], "⚪")
            status = " ✅ [Equipped]" if av["id"] == equipped_id else ""
            embed.add_field(
                name=f"{emoji} {av['name']}{status}",
                value=f"`{av['id']}`  •  {av['rarity']}",
                inline=False,
            )

        embed.set_footer(text="Use ;equipavatar <name or id> to equip one.")
        await ctx.send(embed=embed)

    @commands.command(name="equipavatar", aliases=["equipa"])
    async def equip_avatar(self, ctx: commands.Context, *, query: str) -> None:
        """Equip an avatar by name or ID."""
        avatar = self._resolve_avatar_query(query)
        if not avatar:
            matches = self._ambiguous_matches(query)
            if len(matches) > 1:
                names = ", ".join(f"**{a['name']}**" for a in matches[:5])
                await ctx.send(f"❌ Multiple matches for `{query}`: {names}. Be more specific.")
            else:
                await ctx.send(f"❌ No avatar found matching `{query}`.")
            return

        success, msg = avatar_engine.equip(ctx.author.id, avatar["id"])
        emoji = "✅" if success else "❌"
        await ctx.send(f"{emoji} {msg}")

    @commands.command(name="unequipavatar", aliases=["unequipa"])
    async def unequip_avatar(self, ctx: commands.Context) -> None:
        """Remove your currently equipped avatar."""
        success, msg = avatar_engine.unequip(ctx.author.id)
        emoji = "✅" if success else "❌"
        await ctx.send(f"{emoji} {msg}")


# ── Setup ─────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    avatar_engine.load()
    await bot.add_cog(AvatarShop(bot))
