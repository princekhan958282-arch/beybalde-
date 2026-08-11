"""
avatar_utils.py
---------------
Helper functions for the avatar system.
Handles validation, formatting, and display logic.
No battle logic lives here.
"""

from __future__ import annotations
import discord


# ── Rarity config ────────────────────────────────────────────────────────────

RARITY_COLORS: dict[str, int] = {
    "Common":    0xAAAAAA,
    "Rare":      0x3498DB,
    "Epic":      0x9B59B6,
    "Legendary": 0xF1C40F,
    "Mythic":    0xFF4500,
    "Ultimate":  0x00FFFF,
    "Exclusive": 0xFF1493,
    # MLBB — the crossover banner. Sits above Exclusive as its own tier rather
    # than inside one, so the existing packs cannot roll it by accident: every
    # pack filters candidates by an explicit rarity pool.
    "MLBB":      0x00E5A0,
}

RARITY_EMOJI: dict[str, str] = {
    "Common":    "⚪",
    "Rare":      "🔵",
    "Epic":      "🟣",
    "Legendary": "🟡",
    "Mythic":    "🔴",
    "Ultimate":  "🩵",
    "Exclusive": "💎",
    "MLBB":      "🌟",
}

RARITY_ORDER: list[str] = [
    "Common", "Rare", "Epic", "Legendary", "Mythic", "Ultimate", "Exclusive",
    "MLBB",
]


# ── Type config ──────────────────────────────────────────────────────────────
# The same four types as Beyblades, deliberately — one vocabulary for the whole
# game rather than a second one that has to be explained.

TYPE_EMOJI: dict[str, str] = {
    "attack":  "⚔️",
    "defense": "🛡️",
    "stamina": "🌀",
    "balance": "⚖️",
}

TYPE_LABEL: dict[str, str] = {
    "attack":  "Attack",
    "defense": "Defence",
    "stamina": "Stamina",
    "balance": "Balance",
}

VALID_TYPES: list[str] = list(TYPE_LABEL)


# ── Image URLs ───────────────────────────────────────────────────────────────

# A Discord MESSAGE link (discord.com/channels/<guild>/<channel>/<message>) is
# a link to a conversation, not to a picture. Discord's embed renderer needs a
# direct image URL — typically a cdn.discordapp.com/attachments/... one, from
# right-clicking the image and choosing "Copy Link".
#
# Passing a message link to set_thumbnail is accepted by the API and then
# silently fails to load, leaving a broken image icon on the card. This check
# is what turns that into "no image" instead, for any card, not just the one
# that prompted it.
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def is_renderable_image(url: str) -> bool:
    """True when this URL is something Discord can actually draw."""
    u = str(url or "").strip()
    if not u:
        return False
    if not u.startswith(("http://", "https://")):
        # A bare filename is an attachment:// reference, handled by the caller.
        return True
    if "discord.com/channels/" in u:
        return False          # a message link, not an image
    path = u.split("?", 1)[0].lower()
    if path.endswith(_IMAGE_SUFFIXES):
        return True
    # Discord CDN links carry the real filename before the query string, so the
    # suffix check above already covers them; anything else without a known
    # image extension is not worth handing to the renderer.
    return False


def format_type(avatar_type: str) -> str:
    """'attack' -> '⚔️ Attack'. Unknown types render rather than raise."""
    key = str(avatar_type or "").lower()
    return f"{TYPE_EMOJI.get(key, '❔')} {TYPE_LABEL.get(key, key.title() or 'Unknown')}"


# ── Validation ───────────────────────────────────────────────────────────────

def validate_avatar_data(avatar: dict) -> tuple[bool, str]:
    """
    Validate a single avatar dict from avatar_data.json.
    Returns (is_valid, error_message).
    """
    required_fields = ["id", "name", "rarity", "price", "description", "bonuses"]
    for field in required_fields:
        if field not in avatar:
            return False, f"Missing required field: '{field}'"

    if avatar["rarity"] not in RARITY_ORDER:
        return False, f"Invalid rarity '{avatar['rarity']}'. Must be one of: {RARITY_ORDER}"

    # `type` is required rather than defaulted. A card with no type would
    # silently take the Balance growth curve, which is a real balance decision
    # made by an omission — exactly the kind of thing that is invisible until
    # somebody spends coins on it. tools/classify_avatars.py derives one.
    if avatar.get("type") not in VALID_TYPES:
        return False, (f"Invalid or missing type {avatar.get('type')!r}. "
                       f"Must be one of: {VALID_TYPES}. "
                       f"Run tools/classify_avatars.py to derive it.")

    if not isinstance(avatar["price"], (int, float)) or avatar["price"] < 0:
        return False, f"Price must be a non-negative number."

    bonuses = avatar.get("bonuses", {})
    expected_bonus_keys = [
        "attack_flat", "attack_percent",
        "crit_percent",
        "dodge_chance", "counter_chance",
        "defence_flat", "defence_percent",
        "stamina_flat", "stamina_percent",
        "stability_flat", "stability_percent",
        "charge_flat", "charge_percent",
        "special_move_flat", "special_move_percent",
        "multi_hit_power_double", "multi_hit_extra_hits",
        "resistance_damage_percent", "resistance_status_chance",
        "hp_flat", "hp_percent",
    ]
    for key in expected_bonus_keys:
        if key not in bonuses:
            return False, f"Missing bonus key: '{key}'"

    return True, ""


def is_valid_avatar_id(avatar_id: str, all_avatars: list[dict]) -> bool:
    return any(a["id"] == avatar_id for a in all_avatars)


# ── Formatting ───────────────────────────────────────────────────────────────

def format_price(price: int | float) -> str:
    return f"{int(price):,} coins"


def format_percent(value: float) -> str:
    return f"+{int(value * 100)}%"


def format_flat(value: int | float) -> str:
    return f"+{int(value)}"


def format_bonuses_summary(bonuses: dict) -> str:
    """
    Return a concise human-readable summary of all non-zero bonuses.
    Used in shop previews and profile displays.
    """
    lines = []

    stat_map = [
        ("attack_flat",              "ATK",            "flat"),
        ("attack_percent",           "ATK",            "percent"),
        ("defence_flat",             "DEF",            "flat"),
        ("defence_percent",          "DEF",            "percent"),
        ("stamina_flat",             "STA",            "flat"),
        ("stamina_percent",          "STA",            "percent"),
        ("stability_flat",           "STB",            "flat"),
        ("stability_percent",        "STB",            "percent"),
        ("charge_flat",              "CHG",            "flat"),
        ("charge_percent",           "CHG",            "percent"),
        ("special_move_flat",        "Special Move",   "flat"),
        ("special_move_percent",     "Special Move",   "percent"),
        ("crit_percent",             "Crit Chance",    "percent"),
        ("dodge_chance",             "Dodge Chance",   "percent"),
        ("counter_chance",           "Counter Chance", "percent"),
        ("resistance_damage_percent","DMG Resistance", "percent"),
        ("resistance_status_chance", "Status Resist",  "percent"),
        ("hp_flat",                  "HP",             "flat"),
        ("hp_percent",               "HP",             "percent"),
    ]

    for key, label, kind in stat_map:
        val = bonuses.get(key, 0)
        # Dodge is capped in the engine (AvatarBonuses.DODGE_CAP), so printing
        # the authored number told 16 cards' owners they dodge up to 28% when
        # the roll has always been 5%. Show what actually rolls.
        if key == "dodge_chance" and val:
            try:
                from .avatar_engine import AvatarBonuses
                val = min(float(val), AvatarBonuses.DODGE_CAP)
            except Exception:                            # noqa: BLE001
                pass
        if val:
            formatted = format_percent(val) if kind == "percent" else format_flat(val)
            lines.append(f"• **{label}**: {formatted}")

    if bonuses.get("multi_hit_power_double"):
        lines.append("• **Multi-Hit**: Double damage per hit")
    if bonuses.get("multi_hit_extra_hits"):
        lines.append("• **Multi-Hit**: Extra hits added")

    return "\n".join(lines) if lines else "No stat bonuses."


def build_avatar_embed(avatar: dict, owned: bool = False, equipped: bool = False,
                       level: int = 1, skill_levels: dict | None = None,
                       active_skill_slot: int = 0) -> discord.Embed:
    """
    Build a Discord embed for a single avatar.
    Used in shop previews and inventory views.

    `level` is the viewing player's purchased level for THIS card, and defaults
    to 1 so every existing call site keeps rendering exactly what it rendered
    before. A shop preview showing somebody else's level would be wrong, so the
    default is also the correct value there.

    `active_skill_slot` ticks the skill the viewer has committed to. It
    defaults to 0 — no tick — for the same reason: on a shop preview the viewer
    has no pick on a card they do not own yet.
    """
    rarity = avatar.get("rarity", "Common")
    color  = RARITY_COLORS.get(rarity, 0xAAAAAA)
    emoji  = RARITY_EMOJI.get(rarity, "⚪")

    title = avatar["name"]
    if equipped:
        title += " ✅ [Equipped]"
    elif owned:
        title += " 📦 [Owned]"

    embed = discord.Embed(
        title=f"{emoji} {title}",
        description=avatar.get("description", ""),
        color=color,
    )
    embed.add_field(name="Rarity", value=rarity, inline=True)
    embed.add_field(name="Type",   value=format_type(avatar.get("type")), inline=True)
    embed.add_field(name="Price",  value=format_price(avatar["price"]), inline=True)

    # Level, and what it is currently worth. Shown for owned cards only — on a
    # shop preview the player does not own it yet, so "Lv1" would read as a
    # property of the card rather than of their copy.
    if owned or equipped:
        try:
            from . import avatar_levels as AL
            lvl = AL.clamp_level(level)
            gain = AL.card_stat_bonus(avatar.get("type"), lvl)
            bar = "▰" * lvl + "▱" * (AL.MAX_CARD_LEVEL - lvl)
            if lvl >= AL.MAX_CARD_LEVEL:
                nxt = "**MAX**"
            else:
                nxt = f"Next: {AL.card_level_cost(lvl, lvl + 1):,} coins"
            gains = ", ".join(f"+{v} {k[:3].upper()}"
                              for k, v in gain.items() if v) or "no bonus yet"
            embed.add_field(
                name=f"Level {lvl}/{AL.MAX_CARD_LEVEL}",
                value=f"`{bar}`\n{gains}\n{nxt}",
                inline=False,
            )
        except Exception:                                # noqa: BLE001
            pass

    embed.add_field(
        name="Bonuses",
        value=format_bonuses_summary(avatar.get("bonuses", {})),
        inline=False,
    )

    # Signature skills. Only the nine banner avatars carry these, so the field
    # is skipped entirely for everyone else rather than showing an empty
    # heading.
    #
    # Exactly ONE of them is live in a battle, so each line carries its energy
    # price and the active one is ticked. Listing three skills with no
    # indication that you only get one is how a player would find out the
    # expensive way.
    skills = avatar.get("skills") or []
    if skills:
        try:
            from . import avatar_skills as AS
            costs = [AS.skill_cost(i) for i in range(1, len(skills) + 1)]
        except Exception:                                # noqa: BLE001
            costs = [0] * len(skills)
        lines = []
        for i, sk in enumerate(skills, 1):
            tick = "✅" if i == active_skill_slot else "▫️"
            price = f"  ·  {costs[i - 1]}⚡" if costs[i - 1] else ""
            head = f"{tick} **{i}. {sk.get('name', 'Skill')}**{price}"
            if (owned or equipped) and skill_levels:
                try:
                    from .avatar_progress import slugify
                    sl = int(skill_levels.get(slugify(sk.get("name", "")), 1))
                    if sl > 1:
                        head += f"  ·  Lv{sl}"
                except Exception:                        # noqa: BLE001
                    pass
            lines.append(f"{head}\n{sk.get('description', '')}")
        embed.add_field(
            name="⚡ Skills — one per battle",
            value="\n\n".join(lines)
                  + "\n\n*Pick with `;askill <1-3>`. Casual is free. Ranked "
                    "spends from a pool of 100 that has to cover the whole "
                    "match, and recovers +25 every 5 minutes.*",
            inline=False)

    if avatar.get("limited"):
        until = avatar.get("available_until")
        embed.add_field(
            name="⏳ Limited",
            value=("Limited-time avatar" if not until
                   else f"Available until `{until}`"),
            inline=False,
        )

    image_path = avatar.get("image")
    if image_path and is_renderable_image(image_path):
        if image_path.startswith("http"):
            embed.set_thumbnail(url=image_path)
        else:
            embed.set_thumbnail(url=f"attachment://{image_path.split('/')[-1]}")
    elif image_path:
        # Stored but not drawable — say so on the card rather than showing a
        # broken image, so whoever authored it can see it needs a real link.
        embed.add_field(
            name="🖼️ Art",
            value="Stored link isn't a direct image URL, so it can't be shown. "
                  "Right-click the image → **Copy Link** for a usable one.",
            inline=False)

    return embed


def rarity_sort_key(avatar: dict) -> int:
    return RARITY_ORDER.index(avatar.get("rarity", "Common"))
