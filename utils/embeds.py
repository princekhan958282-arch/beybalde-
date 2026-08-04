"""
utils/embeds.py
---------------
Reusable Discord Embed factories and UI helpers.
All visual theming lives here — one file to change for a full rebrand.
"""

import discord

# ── Rarity palette ────────────────────────────────────────────────────────────
RARITY_COLOURS = {
    "Common":    discord.Color.from_rgb(180, 180, 180),
    "Rare":      discord.Color.from_rgb(30,  144, 255),
    "Epic":      discord.Color.from_rgb(148,   0, 211),
    "Legendary": discord.Color.from_rgb(255, 165,   0),
}
RARITY_EMOJIS = {
    "Common":    "⚪",
    "Rare":      "🔵",
    "Epic":      "🟣",
    "Legendary": "🟡",
}
TYPE_EMOJIS = {
    "Attack":  "⚔️",
    "Defense": "🛡️",
    "Stamina": "🌀",
    "Balance": "⚖️",
}

# Level badge thresholds
def level_badge(level: int) -> str:
    if level >= 100: return "👑"
    if level >= 75:  return "🌟"
    if level >= 50:  return "💎"
    if level >= 25:  return "🔥"
    if level >= 10:  return "⚡"
    return "🌱"


def rarity_colour(rarity: str) -> discord.Color:
    return RARITY_COLOURS.get(rarity, discord.Color.default())


def stat_bar(value: int, max_value: int = 120, length: int = 10) -> str:
    """Renders  ████████░░  value/max."""
    clamped = max(0, min(value, max_value))
    filled  = round((clamped / max_value) * length)
    bar     = "█" * filled + "░" * (length - filled)
    return f"`{bar}` {value}"


def hp_bar(current: int, maximum: int, length: int = 10) -> str:
    """Colour-coded HP bar — green → yellow → red as HP drops."""
    filled = round((max(0, current) / maximum) * length)
    bar    = "█" * filled + "░" * (length - filled)
    ratio  = current / maximum
    icon   = "🟩" if ratio > 0.5 else ("🟨" if ratio > 0.25 else "🟥")
    return f"{icon} `{bar}` {current}/{maximum}"


def xp_bar(xp_progress: int, xp_needed: int, length: int = 10) -> str:
    """XP progress bar for the level system."""
    if xp_needed == 0:
        return "`██████████` MAX"
    filled = round((min(xp_progress, xp_needed) / xp_needed) * length)
    bar    = "█" * filled + "░" * (length - filled)
    return f"`{bar}` {xp_progress}/{xp_needed} XP"


def beyblade_info_embed(blade: dict) -> discord.Embed:
    """Rich info card for !info and !list detail views."""
    rarity  = blade.get("rarity", "Common")
    btype   = blade.get("type",   "Balance")
    stats   = blade.get("stats",  {})
    emoji   = RARITY_EMOJIS.get(rarity, "")
    t_emoji = TYPE_EMOJIS.get(btype, "⚖️")

    embed = discord.Embed(
        title       = f"{emoji} {blade['name']}",
        description = blade.get("description", "No description available."),
        color       = rarity_colour(rarity),
    )
    embed.set_thumbnail(url=blade.get("image_url", ""))

    # ── Header row ────────────────────────────────────────────────────────────
    embed.add_field(name="Rarity",        value=f"{emoji} {rarity}",     inline=True)
    embed.add_field(name="Type",          value=f"{t_emoji} {btype}",    inline=True)
    embed.add_field(name="Burst Height",  value=blade.get("burst_height", "—"), inline=True)

    # ── Stat bars ─────────────────────────────────────────────────────────────
    embed.add_field(name="⚔️ Attack",   value=stat_bar(stats.get("attack",  0)), inline=True)
    embed.add_field(name="🛡️ Defense", value=stat_bar(stats.get("defense", 0)), inline=True)
    embed.add_field(name="🌀 Stamina", value=stat_bar(stats.get("stamina", 0)), inline=True)
    embed.add_field(name="✨ Special", value=stat_bar(stats.get("special", 0), max_value=130), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    # ── Ability block ─────────────────────────────────────────────────────────
    ab = blade.get("ability")
    if ab:
        trigger_labels = {
            "on_attack_win":  "⚔️ On Attack Win",
            "on_defense_win": "🛡️ On Defense Win",
            "on_stamina_win": "🌀 On Stamina Win",
            "on_mirror_clash":"💥 On Mirror Clash",
            "on_take_damage": "🩸 On Taking Damage",
            "on_special":     "🌟 On Special Move",
            "passive":        "♾️ Passive (Always Active)",
        }
        trigger_str = trigger_labels.get(ab.get("trigger", ""), ab.get("trigger", ""))
        embed.add_field(
            name  = f"✨ Ability — {ab['name']}",
            value = f"*{trigger_str}*\n{ab['description']}",
            inline=False,
        )

    # ── Special move block ────────────────────────────────────────────────────
    sm = blade.get("special_move")
    if sm:
        hits  = sm.get("hits", 1)
        dph   = sm.get("damage_per_hit", 0)
        total = sm.get("total_damage", hits * dph)
        embed.add_field(
            name  = f"🌟 Special Move — {sm.get('name', '???')}",
            value = (
                f"{sm.get('description', '')}\n"
                f"**{hits} hit(s) × {dph} dmg = {total} total**"
            ),
            inline=False,
        )

    embed.set_footer(text=f"ID: {blade.get('id','?')} | Use !battle to fight with this blade!")
    return embed
