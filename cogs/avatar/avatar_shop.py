"""
avatar_shop.py
--------------
Discord cog for avatar purchasing, pack opening, and inventory management.

Commands:
  ;avatarpacks / ;apacks    — View available avatar packs
  ;buypack <pack>           — Open an avatar pack (common/rare/epic/legendary/mlbb)
  ;myavatars                — View owned avatars
  ;equipavatar <id>         — Equip an owned avatar
  ;unequipavatar            — Remove equipped avatar
  ;avatarinfo <id>          — Inspect an avatar's stats

Rarities (lowest → highest):
  Common, Rare, Epic, Legendary, Mythic, Ultimate, Exclusive, MLBB

  Exclusive avatars are not pullable from any pack — they are event/reward
  cards. MLBB is a CLOSED crossover banner: only the mlbb pack can produce one,
  and the mlbb pack can produce nothing else. Argus and Dyrroth are Mobile
  Legends heroes and live on the MLBB banner with the rest of the crossover
  cast, which leaves Omega Prime and Cobra Titan as the Exclusive tier.

Pack pools:
  Common   → Common, Rare
  Rare     → Common, Rare, Epic
  Epic     → Common, Rare, Epic, Legendary, Mythic
  Legendary→ Common, Rare, Epic, Legendary, Mythic, Ultimate, Exclusive
  MLBB     → MLBB only

Pack guarantees (slot 1 = EXACT rarity guaranteed, slot 2 = random from pool):
  Common    — 1 guaranteed Common (exact)               — 75,000 coins
  Rare      — 1 guaranteed Rare (exact)                 — 150,000 coins
  Epic      — 1 guaranteed Epic (exact)                 — 275,000 coins
  Legendary — 1 guaranteed Legendary (exact)            — 500,000 coins
  MLBB      — both slots guaranteed MLBB                — 15,000,000 coins

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
    is_renderable_image,
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
    # See the note in avatar_utils.RARITY_ORDER — the two lists must agree.
    "Blader",
    "Mythic",
    "Ultimate",
    "Exclusive",
    "MLBB",
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
    # The MLBB banner is a CLOSED pool — MLBB and nothing else. Listing only
    # that rarity is what stops a 15M pack handing back a Common, and equally
    # what stops the other four packs ever rolling an MLBB avatar, since every
    # pack filters candidates by this list before weights are applied.
    "mlbb":      ["MLBB"],
    # The Season 1 banner — a closed pool of the eight School League bladers,
    # exactly like MLBB. `_build_rarity_map` only groups rarities that are in
    # the pack's own pool, so listing `Blader` here and nowhere else is the
    # whole of the containment: no other pack can reach one.
    "season1":   ["Blader"],
}

# Exact guaranteed rarity for slot 1
PACK_GUARANTEE: dict[str, Optional[str]] = {
    "common":    None,
    "rare":      "Rare",
    "epic":      "Epic",
    "legendary": "Legendary",
    "mlbb":      "MLBB",
    "season1":   "Blader",
}

PACK_PRICE: dict[str, int] = {
    "common":    75_000,
    "rare":      150_000,
    "epic":      275_000,
    "legendary": 500_000,
    "mlbb":      15_000_000,
    "season1":   250_000,
}

PACK_DISPLAY: dict[str, str] = {
    "common":    "Common Pack",
    "rare":      "Rare Pack",
    "epic":      "Epic Pack",
    "legendary": "Legendary Pack",
    "mlbb":      "MLBB Pack",
    "season1":   "Beyblade Burst Season 1 Pack",
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
    # 20% of 15,000,000 = 3,000,000 exactly.
    #
    # This was 60%, which was indefensible once you notice the refund is paid
    # PER PULL and the MLBB pack pulls twice: two duplicates returned
    # 18,000,000 on a 15,000,000 pack. With only six cards on the banner, a
    # player who owned most of them could open packs at a profit — the pack
    # stopped being a purchase and became a coin printer.
    #
    # At 20% the worst case is 6,000,000 back on 15,000,000 spent, so a bad
    # pull still hurts without ever paying.
    "MLBB":      0.20,   # 20%
    # 20% of 250,000 = 50,000. Eight cards on a closed banner means duplicates
    # arrive quickly, and the refund is what stops a late pack feeling wasted.
    "Blader":    0.20,   # 20%
}

# ── Pack emoji ────────────────────────────────────────────────────────────────

PACK_EMOJI: dict[str, str] = {
    "common":    "⚪",
    "rare":      "🔵",
    "epic":      "🟣",
    "legendary": "🟡",
    "mlbb":      "🌟",
    "season1":   "🏫",
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
    # A closed banner: MLBB is the only rarity in the pool, so both slots can
    # only ever produce an MLBB avatar. A 15,000,000 pack that could return a
    # Common would be indefensible.
    "mlbb": {
        "MLBB": 1000,
    },
    # Season 1: eight bladers, one closed tier, equal odds. They are all the
    # same power by design, so there is nothing to weight.
    "season1": {
        "Blader": 1000,
    },
}

# How many avatars a pack hands over. This was a hardcoded `if pack != "common"`
# inside `buy_pack`, which meant `;avatarpacks` said "Each pack gives 2 avatars"
# above a Common pack that gives one — and a second single-pull pack had nowhere
# to say so.
PACK_PULLS: dict[str, int] = {
    "common":    1,
    "rare":      2,
    "epic":      2,
    "legendary": 2,
    "mlbb":      2,
    "season1":   1,
}

# What a pack needs before it can be bought at all, checked BEFORE any coins
# move. `None` — every pack that existed before Season 1 — is always open.
# Each entry is `(predicate(profile) -> bool, reason_if_locked(profile) -> str)`.


def _season1_open(profile: dict) -> bool:
    from cogs.story import story_data as SD
    return SD.season_complete(profile, 1)


def _season1_reason(profile: dict) -> str:
    from cogs.story import story_data as SD
    done, total = SD.season_progress(profile, 1)
    return (f"🔒 Finish **Beyblade Burst Season 1** first — "
            f"**{done}/{total}** chapters cleared. "
            f"(`;story` → 🏫 Beyblade Burst School)")


PACK_REQUIRES: dict[str, tuple] = {
    "season1": (_season1_open, _season1_reason),
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
    """Mobile-friendly detail page for an avatar's signature skills."""

    def __init__(self, avatar: dict, *, active_slot: int = 0,
                 skill_levels: dict | None = None, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.avatar = avatar
        self.active_slot = active_slot
        self.skill_levels = skill_levels or {}

    @discord.ui.button(label="Skills", emoji="⚡",
                       style=discord.ButtonStyle.primary)
    async def show_skills(self, interaction: discord.Interaction,
                          button: discord.ui.Button) -> None:
        from . import avatar_skills as AS
        from .avatar_progress import slugify

        av = self.avatar
        rarity = av.get("rarity", "Common")
        emoji = RARITY_EMOJI.get(rarity, "⚪")
        embed = discord.Embed(
            title=f"{emoji} {av['name']} — Skills",
            description="Choose **one** signature skill per battle.",
            color=RARITY_COLORS.get(rarity, 0xAAAAAA),
        )

        skills = av.get("skills") or []
        for slot, skill in enumerate(skills, 1):
            selected = "✅ Selected" if slot == self.active_slot else "▫️ Available"
            level = max(1, int(self.skill_levels.get(
                slugify(skill.get("name", "")), 1
            )))
            embed.add_field(
                name=(f"{selected}  •  {slot}. {skill.get('name', 'Skill')} "
                      f"• {AS.skill_cost(slot)}⚡ • Lv{level}"),
                value=skill.get("description") or "No description.",
                inline=False,
            )

        image = av.get("image", "")
        if image.startswith(("http://", "https://")) and is_renderable_image(image):
            embed.set_thumbnail(url=image)
        embed.set_footer(text="Change selection with ;askill <1-3>")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class AvatarShop(commands.Cog, name="Avatar"):
    """Buy and manage your battle avatars."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Storage helpers (JSON flat-file) ──────────────────────────────────────

    async def _get_player_coins(self, player_id: int) -> int:
        from utils.database import get_user
        return (await get_user(player_id)).get("coins", 0)

    async def _deduct_coins(self, player_id: int, amount: int) -> None:
        from utils.database import get_user, update_user
        profile = await get_user(player_id)
        profile["coins"] = max(0, profile.get("coins", 0) - amount)
        await update_user(player_id, profile)

    async def _add_coins(self, player_id: int, amount: int) -> None:
        from utils.database import get_user, update_user
        profile = await get_user(player_id)
        profile["coins"] = profile.get("coins", 0) + amount
        await update_user(player_id, profile)

    def _player_owns(self, player_id: int, avatar_id: str) -> bool:
        from utils.database import player_owns_avatar
        return player_owns_avatar(player_id, avatar_id)

    def _add_to_inventory(self, player_id: int, avatar_id: str) -> None:
        from utils.database import add_avatar_to_inventory
        add_avatar_to_inventory(player_id, avatar_id)

    def _get_owned_avatar_ids(self, player_id: int) -> list[str]:
        from utils.database import get_avatar_inventory
        return get_avatar_inventory(player_id)

    async def _get_equipped_id(self, player_id: int) -> str | None:
        from utils.database import get_equipped_avatar
        return await get_equipped_avatar(player_id)

    # ── Pack internals ────────────────────────────────────────────────────────

    def _build_rarity_map(self, pool: list[str]) -> dict[str, list[dict]]:
        """Group all pullable non-Exclusive avatars by rarity, filtered to pool.

        This is the single choke point every pack pull goes through
        (`_pull_from_pool` only ever reads this map), so the limited-time gate
        belongs here rather than at the two call sites.

        `avatar_is_available` had been written and never called — a limited
        avatar stayed pullable forever, which meant `limited` was decoration.
        """
        from cogs.avatar.avatar_engine import avatar_is_available
        result: dict[str, list[dict]] = {r: [] for r in pool}
        for av in avatar_engine.get_all_avatars():
            if av["rarity"] in result and av["rarity"] != "Exclusive" \
                    and avatar_is_available(av):
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
            await self._add_coins(player_id, refund)
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
        equipped_id = await self._get_equipped_id(player_id)
        owned_count = len(self._get_owned_avatar_ids(player_id))
        coins       = await self._get_player_coins(player_id)

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

    @commands.command(name="avatarpacks", aliases=["apacks"])
    async def avatar_packs(self, ctx: commands.Context) -> None:
        """View available avatar packs and their contents."""
        embed = discord.Embed(
            title="🎴 Avatar Packs",
            description=(
                "Slot 1 is the **guaranteed** pull. Slot 2, where a pack has "
                "one, is random from the pool.\n"
                "Duplicate avatars are automatically refunded in coins.\n\n"
                "Use `;buypack <pack>` to open one."
            ),
            color=0x3498DB,
        )
        # Read once for the lock states below rather than per pack.
        from utils.database import get_user as _get_user
        _profile = _get_user(ctx.author.id)

        # Derived from the tables the pack opener actually reads, not a
        # hand-written list. The hardcoded version had drifted twice over: the
        # MLBB banner was missing entirely — a 15,000,000 pack that `;packs`
        # never mentioned — and its refund percentages were frozen at values
        # DUPE_REFUND_RATE no longer held. Anything listed here is now, by
        # construction, what the pack will really do.
        for pack_key in PACK_PRICE:
            emoji = PACK_EMOJI.get(pack_key, "🎴")
            price = PACK_PRICE[pack_key]
            guar = PACK_GUARANTEE.get(pack_key)
            # Only rarities a pull can REACH. `_build_rarity_map` drops
            # Exclusive outright, so listing it advertised a tier — and a 50%
            # refund — that no pack has ever been able to produce.
            pool = [r for r in PACK_POOL.get(pack_key, []) if r != "Exclusive"]
            if not pool:
                pool = list(PACK_POOL.get(pack_key, []))

            if guar and len(set(pool)) == 1 and pool[0] == guar:
                guarantee = f"both pulls **{guar}**"     # a closed banner
            elif guar:
                guarantee = f"1× guaranteed **{guar}** (exact)"
            else:
                guarantee = "no guaranteed rarity"

            rates = [DUPE_REFUND_RATE.get(r, 0.10) for r in pool] or [0.10]
            lo, hi = min(rates), max(rates)
            # The coin value matters far more than the percentage at these
            # prices — "20%" of a 15,000,000 pack is not a number anyone
            # converts in their head while deciding whether to buy.
            if lo == hi:
                refund = f"{lo:.0%}  ({int(price * lo):,} coins)"
            else:
                refund = (f"{lo:.0%}–{hi:.0%}  ({int(price * lo):,}–"
                          f"{int(price * hi):,} coins)")

            pulls = PACK_PULLS.get(pack_key, 2)
            _gate = PACK_REQUIRES.get(pack_key)
            locked = bool(_gate) and not _gate[0](_profile)
            last_line = (_gate[1](_profile) if locked
                         else f"`;buypack {pack_key}`")

            embed.add_field(
                name=(f"{'🔒 ' if locked else ''}{emoji} "
                      f"{PACK_DISPLAY[pack_key]}  —  {price:,} coins"),
                value=(
                    f"**Pulls:** {pulls}\n"
                    f"**Guarantee:** {guarantee}\n"
                    f"**Pool:** {', '.join(pool)}\n"
                    f"**Dupe refund:** {refund} per duplicate\n"
                    f"{last_line}"
                ),
                inline=False,
            )

        embed.add_field(
            name="💎 Exclusive Avatars",
            value=(
                "Exclusive avatars **cannot be pulled from packs**.\n"
                "They are obtained through events, quests, and special rewards only.\n"
                "*(Argus and Dyrroth are MLBB heroes — they moved to the "
                "🌟 MLBB banner and are pullable from `;buypack mlbb`.)*"
            ),
            inline=False,
        )

        coins = await self._get_player_coins(ctx.author.id)
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

        # The gate comes BEFORE the balance check and long before any coins
        # move. A locked pack that took the money and then refused would be
        # the worst possible ordering, and refunding after the fact is a
        # second write that can fail on its own.
        gate = PACK_REQUIRES.get(pack)
        if gate:
            from utils.database import get_user
            profile = await get_user(player_id)
            allowed, reason = gate
            if not allowed(profile):
                await ctx.send(reason(profile))
                return

        coins     = await self._get_player_coins(player_id)

        if coins < price:
            shortfall = price - coins
            await ctx.send(
                f"❌ Not enough coins for a **{PACK_DISPLAY[pack]}**.\n"
                f"Cost: {price:,} coins | You have: {coins:,} coins | "
                f"Need: {shortfall:,} more."
            )
            return

        # Deduct immediately
        await self._deduct_coins(player_id, price)

        pool      = PACK_POOL[pack]
        guarantee = PACK_GUARANTEE[pack]
        rarity_map = self._build_rarity_map(pool)

        # Slot 1 — exact guaranteed rarity (or random if common)
        slot1 = _pull_from_pool(pack, pool, rarity_map, exact_rarity=guarantee)
        # Slot 2 — fully random from pool, for the packs that pull twice
        slot2 = (_pull_from_pool(pack, pool, rarity_map, exact_rarity=None)
                 if PACK_PULLS.get(pack, 2) > 1 else None)

        if not slot1 and not slot2:
            # Shouldn't happen but refund gracefully
            await self._add_coins(player_id, price)
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

        remaining = await self._get_player_coins(player_id)

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
        equipped_id = await self._get_equipped_id(ctx.author.id)

        owned    = avatar["id"] in owned_ids
        equipped = avatar["id"] == equipped_id

        # Level comes from the VIEWER's profile, so ;ainfo shows their copy of
        # the card rather than the card in the abstract.
        lvl, skill_lvls, active_slot = 1, {}, 0
        if owned or equipped:
            try:
                from utils.database import get_user
                from . import avatar_progress as AP
                from . import avatar_skills as AS
                prof = await get_user(ctx.author.id)
                lvl = AP.card_level(prof, avatar["id"])
                skill_lvls = AP.card_entry(prof, avatar["id"]).get("skills") or {}
                # The standing pick, not the in-battle lock: ;ainfo is read
                # between fights, and what a player wants to know is which
                # skill their NEXT battle will use.
                if avatar.get("skills"):
                    active_slot = AS.chosen_slot(prof, avatar["id"])
            except Exception:                            # noqa: BLE001
                pass

        embed = build_avatar_embed(
            avatar,
            owned=owned,
            equipped=equipped,
            level=lvl,
            skill_levels=skill_lvls,
            active_skill_slot=active_slot,
            compact=True,
        )

        # The shared builder uses a thumbnail. Avoid also adding the same art as
        # a full-width image: that doubled the card height on phones.
        if avatar.get("skills"):
            await ctx.send(
                embed=embed,
                view=AvatarSkillsView(
                    avatar,
                    active_slot=active_slot,
                    skill_levels=skill_lvls,
                ),
            )
        else:
            await ctx.send(embed=embed)

    # `;buyavatar` / `;buya` removed on request. Avatars now come from packs,
    # events and quests only — direct purchase was the one path that bypassed
    # the banner entirely, which made every pack price a suggestion. The
    # `price` field stays in the data: it is still shown on cards and read by
    # the shop listing, it simply is no longer a way to buy.

    @commands.command(name="myavatars", aliases=["avatars", "myav", "avinv"])
    async def my_avatars(self, ctx: commands.Context) -> None:
        """View your owned avatars."""
        player_id   = ctx.author.id
        owned_ids   = self._get_owned_avatar_ids(player_id)
        equipped_id = self._get_equipped_id(player_id)

        if not owned_ids:
            await ctx.send(
                "You don't own any avatars yet. "
                "Use `;avatarpacks` to open packs."
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

        success, msg = await avatar_engine.equip(ctx.author.id, avatar["id"])
        emoji = "✅" if success else "❌"
        await ctx.send(f"{emoji} {msg}")

    @commands.command(name="unequipavatar", aliases=["unequipa"])
    async def unequip_avatar(self, ctx: commands.Context) -> None:
        """Remove your currently equipped avatar."""
        success, msg = await avatar_engine.unequip(ctx.author.id)
        emoji = "✅" if success else "❌"
        await ctx.send(f"{emoji} {msg}")


# ── Setup ─────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    avatar_engine.load()
    await bot.add_cog(AvatarShop(bot))
