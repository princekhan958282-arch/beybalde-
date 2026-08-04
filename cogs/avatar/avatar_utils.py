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
}

RARITY_EMOJI: dict[str, str] = {
    "Common":    "⚪",
    "Rare":      "🔵",
    "Epic":      "🟣",
    "Legendary": "🟡",
    "Mythic":    "🔴",
    "Ultimate":  "🩵",
    "Exclusive": "💎",
}

RARITY_ORDER: list[str] = [
    "Common", "Rare", "Epic", "Legendary", "Mythic", "Ultimate", "Exclusive"
]


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
        if val:
            formatted = format_percent(val) if kind == "percent" else format_flat(val)
            lines.append(f"• **{label}**: {formatted}")

    if bonuses.get("multi_hit_power_double"):
        lines.append("• **Multi-Hit**: Double damage per hit")
    if bonuses.get("multi_hit_extra_hits"):
        lines.append("• **Multi-Hit**: Extra hits added")

    return "\n".join(lines) if lines else "No stat bonuses."


def build_avatar_embed(avatar: dict, owned: bool = False, equipped: bool = False) -> discord.Embed:
    """
    Build a Discord embed for a single avatar.
    Used in shop previews and inventory views.
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
    embed.add_field(name="Price",  value=format_price(avatar["price"]), inline=True)
    embed.add_field(
        name="Bonuses",
        value=format_bonuses_summary(avatar.get("bonuses", {})),
        inline=False,
    )

    # Signature skills. Only the Exclusive banner avatars carry these, so the
    # field is skipped entirely for everyone else rather than showing an empty
    # heading.
    skills = avatar.get("skills") or []
    if skills:
        lines = []
        for i, sk in enumerate(skills, 1):
            lines.append(f"**{i}. {sk.get('name', 'Skill')}**\n"
                         f"{sk.get('description', '')}")
        embed.add_field(name="⚡ Skills", value="\n\n".join(lines), inline=False)

    if avatar.get("limited"):
        until = avatar.get("available_until")
        embed.add_field(
            name="⏳ Limited",
            value=("Limited-time avatar" if not until
                   else f"Available until `{until}`"),
            inline=False,
        )

    image_path = avatar.get("image")
    if image_path:
        if image_path.startswith("http"):
            embed.set_thumbnail(url=image_path)
        else:
            embed.set_thumbnail(url=f"attachment://{image_path.split('/')[-1]}")

    return embed


def rarity_sort_key(avatar: dict) -> int:
    return RARITY_ORDER.index(avatar.get("rarity", "Common"))
