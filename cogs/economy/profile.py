"""
cogs/profile.py
---------------
User profile management and Beyblade info lookup.

Commands
--------
;profile [@user]      — Rich profile embed with background image.
;info <name>          — Full Beyblade stats card.
;equip <name>         — Switch your active Beyblade.
;inventory [@user]    — List all owned Beyblades.
;list                 — All Beyblades grouped by rarity.
"""

import asyncio
import difflib
import logging

log = logging.getLogger(__name__)

import discord
from discord.ext import commands
from typing import Optional

from utils.database import (
    get_user,
    get_beyblade,
    load_beyblades,
    set_active_beyblade,
    xp_to_next_level,
    MAX_LEVEL,
    get_stat_multiplier,
)
from utils.embeds import (
    beyblade_info_embed,
    rarity_colour,
    RARITY_EMOJIS,
    stat_bar,
    xp_bar,
    level_badge,
)
from utils.ranks import tier_for_score, rank_score_for
from utils.mobile_ui import bar as ui_bar, trunc as ui_trunc
from utils.hp_system import blade_hp_stat, max_hp_for_blade, hp_display_pct
from utils import info_card

try:
    from utils.profile_card import render_profile_card
except Exception:                                   # pragma: no cover
    log.exception("profile_card unavailable — ;profile will use the embed fallback")
    render_profile_card = None                      # type: ignore
from cogs.avatar import avatar_engine, AvatarBonuses, NULL_BONUSES

# ── Constants ──────────────────────────────────────────────────────────────────

BG_IMAGE = (
    "https://cdn.discordapp.com/attachments/1510856884943454208/"
    "1512649726032609403/1780714171571.png"
)

RARITY_COLOURS_HEX = {
    "Common":          0x95a5a6,
    "Uncommon":        0x2ecc71,
    "Rare":            0x3498db,
    "Epic":            0x9b59b6,
    "Legendary":       0xe67e22,
    "Mythic":          0xe74c3c,   # deep red
    "Ultimate":        0xf1c40f,   # gold
    "Exclusive":       0x1abc9c,   # teal
    "State Exclusive": 0xff69b4,   # pink
}

RARITY_BANNERS = {
    "Common":          "░░░  C O M M O N  ░░░",
    "Uncommon":        "▒▒▒  U N C O M M O N  ▒▒▒",
    "Rare":            "▓▓▓  R A R E  ▓▓▓",
    "Epic":            "███  E P I C  ███",
    "Legendary":       "⚡ ⚡  L E G E N D A R Y  ⚡ ⚡",
    "Mythic":          "🔥 🔥  M Y T H I C  🔥 🔥",
    "Ultimate":        "👑 👑  U L T I M A T E  👑 👑",
    "Exclusive":       "💎 💎  E X C L U S I V E  💎 💎",
    "State Exclusive": "🌟 🌟  S T A T E  E X C L U S I V E  🌟 🌟",
}

TYPE_EMOJIS = {
    "Attack":  "⚔️",
    "Defense": "🛡️",
    "Stamina": "🌀",
    "Balance": "⚖️",
}

TRIGGER_LABELS = {
    "passive":          "♾️ Passive — Always Active",
    "on_special":       "⚡ On Special Move",
    "on_attack_win":    "⚔️ On Attack Win",
    "on_defense_win":   "🛡️ On Defense Win",
    "on_stamina_win":   "🌀 On Stamina Win",
    "on_take_damage":   "🩸 On Taking Damage",
    "on_mirror_clash":  "💥 On Mirror Clash",
    "on_win":           "🏆 On Win",
}

ITEMS_PER_INV_PAGE = 15


# ── Helpers ────────────────────────────────────────────────────────────────────

def _rarity_color(rarity: str) -> discord.Color:
    return discord.Color(RARITY_COLOURS_HEX.get(rarity, 0x95a5a6))


def _win_rate(wins: int, losses: int) -> str:
    total = wins + losses
    if total == 0:
        return "—"
    return f"{round((wins / total) * 100)}%"


def _stat_line(label: str, emoji: str, value: int, max_val: int = 200) -> str:
    """Single coloured stat bar line."""
    filled = round((max(0, min(value, max_val)) / max_val) * 12)
    bar = "█" * filled + "░" * (12 - filled)
    return f"{emoji} **{label}** `{bar}` **{value}**"


def _hp_stat_line(blade: dict) -> str:
    """HP bar, normalised over the reachable cross-type HP range."""
    hp     = blade_hp_stat(blade)
    filled = round(hp_display_pct(blade) / 100 * 12)
    bar    = "\u2588" * filled + "\u2591" * (12 - filled)
    return f"\u2764\ufe0f **HP** `{bar}` **{hp}**  `{max_hp_for_blade(blade)} pool`"


def _beypedia_hp_bar(blade: dict, bar_len: int = 14) -> str:
    """Beypedia-style HP bar over the reachable cross-type HP range."""
    hp     = blade_hp_stat(blade)
    filled = round(hp_display_pct(blade) / 100 * bar_len)
    # Built outside the f-string: escapes inside an f-string EXPRESSION are a
    # SyntaxError before Python 3.12, and runtime.txt pins 3.11.9 — as written
    # this stopped the whole profile cog (;profile, ;beypedia, ;list) from
    # loading on the deployed runtime.
    bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
    return f"`{bar}` **{hp}**"


def _with_viewer_level(user_id, blade: dict) -> dict:
    """A copy of `blade` at the level THIS viewer has raised it to.

    `;info <name>` used to render the raw species entry, so a bey the player had
    levelled to 60 showed its printed level-1 stats and no level at all. This
    attaches both, using the same bey_levels helpers loadout.effective_blade
    uses, so the two surfaces agree.

    Parts and avatar are deliberately NOT folded in: those belong to whatever
    blade is currently equipped, and this one may not be it.

    Never raises — a lookup must not fail because a profile is malformed.
    """
    try:
        from utils import bey_levels as _BL
        name = blade.get("name")
        if not name:
            return blade
        profile = get_user(user_id)
        entry = (profile.get("bey_progress") or {}).get(str(name))
        if not entry:
            return blade                      # never raised → no level to show
        level = _BL.level_from_xp(int(entry.get("xp", 0)))
        if level <= 1:
            return blade
        out = dict(blade)
        out["stats"] = _BL.stats_at(blade, level, entry.get("ivs"))
        out["level"] = level
        return out
    except Exception:                                # noqa: BLE001
        return blade


def _viewer_parts(user_id) -> dict:
    """The viewer's equipped ratchet/bit for the card's parts strip.

    Our parts system uses driver/disk/ring slots; the card speaks Beyblade-X
    (Blade / Ratchet / Bit). Map disk -> Ratchet, driver -> Bit.
    """
    try:
        from cogs.economy.shop import PARTS_CATALOG
        equipped = get_user(user_id).get("equipped_parts", []) or []
        by_type  = {}
        for name in equipped:
            cat = next((p for p in PARTS_CATALOG
                        if p["name"].lower() == str(name).lower()), None)
            if cat:
                by_type[cat.get("type")] = name
        return {"ratchet": by_type.get("disk") or by_type.get("ring"),
                "bit":     by_type.get("driver")}
    except Exception:
        return {}


def _abilities_text(blade: dict) -> str:
    """Build a compact ability summary string from any ability format."""
    abilities = []

    # New multi-ability format
    if isinstance(blade.get("abilities"), list):
        abilities = blade["abilities"]
    else:
        # Legacy single/double ability format
        for key in ("ability", "ability_2"):
            ab = blade.get(key)
            if isinstance(ab, dict):
                abilities.append(ab)

    if not abilities:
        return "*No abilities registered.*"

    lines = []
    for ab in abilities:
        trigger = TRIGGER_LABELS.get(ab.get("trigger", "passive"), ab.get("trigger", ""))
        lines.append(f"**{ab.get('name', 'Unknown')}** — *{trigger}*\n{ab.get('description', '')}")

    return "\n\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  Profile embed builder
# ══════════════════════════════════════════════════════════════════════════════

def build_profile_embed(
    target: discord.Member,
    profile_doc: dict,
    active_blade: dict,
    avatar_bonuses: "AvatarBonuses | None" = None,
) -> discord.Embed:
    """
    Builds the main ;profile embed.
    Uses the background image as the embed image, the bey as thumbnail,
    and a rarity-coloured accent strip.
    """
    rarity      = active_blade.get("rarity", "Common")
    btype       = active_blade.get("type", "Balance")
    stats       = active_blade.get("stats", {})
    r_emoji     = RARITY_EMOJIS.get(rarity, "⚪")
    t_emoji     = TYPE_EMOJIS.get(btype, "⚖️")
    r_banner    = RARITY_BANNERS.get(rarity, rarity.upper())

    wins        = profile_doc.get("wins", 0)
    losses      = profile_doc.get("losses", 0)
    total_xp    = profile_doc.get("xp", 0)
    inventory   = profile_doc.get("inventory", [])
    rank_score  = rank_score_for(profile_doc)
    rank_tier   = tier_for_score(rank_score)

    level, xp_need, xp_prog = xp_to_next_level(total_xp)
    badge       = level_badge(level)
    mult        = get_stat_multiplier(target.id)
    bonus_pct   = int((mult - 1.0) * 100)

    # ── Embed shell ────────────────────────────────────────────────────────
    embed = discord.Embed(
        color       = _rarity_color(rarity),
        description = (
            f"## 🌀 {target.display_name}'s Blader Profile\n"
            f"─────────────────────────────"
        ),
    )
    embed.set_thumbnail(url=active_blade.get("image_url") or target.display_avatar.url)
    embed.set_image(url=BG_IMAGE)

    # ── Trainer block ──────────────────────────────────────────────────────
    if level >= MAX_LEVEL:
        xp_display = f"`{'█' * 10}` **MAX LEVEL**"
    else:
        xp_display = xp_bar(xp_prog, xp_need)

    bonus_line = (
        f"⚔️ `+{bonus_pct}%` to all stats in battle"
        if bonus_pct > 0
        else "*(Reach Level 10 for your first stat bonus!)*"
    )

    embed.add_field(
        name  = f"{badge} Trainer  ·  Level {level}{' *(MAX)*' if level >= MAX_LEVEL else f' / {MAX_LEVEL}'}",
        value = (
            f"{xp_display}\n"
            f"🏆 Total XP: **{total_xp:,}**\n"
            f"{bonus_line}"
        ),
        inline=False,
    )

    # ── Rank block ─────────────────────────────────────────────────────────
    _, rank_name, rank_emoji, _ = rank_tier
    embed.add_field(
        name  = "🏅 Rank",
        value = (
            f"{rank_emoji} **{rank_name}**\n"
            f"Score: **{rank_score:,}** pts"
        ),
        inline=True,
    )

    # ── W/L block ─────────────────────────────────────────────────────────
    embed.add_field(
        name  = "⚔️ Battle Record",
        value = (
            f"🏆 Wins: **{wins}**\n"
            f"💀 Losses: **{losses}**\n"
            f"📊 Win Rate: **{_win_rate(wins, losses)}**"
        ),
        inline=True,
    )

    # ── Collection ────────────────────────────────────────────────────────
    embed.add_field(
        name  = "🎒 Collection",
        value = f"**{len(inventory)}** Beyblade{'s' if len(inventory) != 1 else ''} owned",
        inline=True,
    )

    embed.add_field(name="─────────────────────────────", value="", inline=False)

    # ── Active blade ───────────────────────────────────────────────────────
    # The bey's own level, set by loadout.effective_blade. Distinct from the
    # trainer level shown above, hence the explicit "Bey Level" label.
    try:
        _bey_level = int(active_blade.get("level") or 0)
    except (TypeError, ValueError):
        _bey_level = 0

    embed.add_field(
        name  = f"{r_emoji} Active Beyblade",
        value = (
            f"### {active_blade['name']}\n"
            f"{r_banner}\n"
            f"{t_emoji} **Type:** {btype}"
            + (f"　·　🌀 **Bey Level:** {_bey_level}" if _bey_level > 0 else "")
        ),
        inline=False,
    )

    # ── Stats ──────────────────────────────────────────────────────────────
    # Apply avatar bonuses to final display values if an avatar is equipped
    _av = avatar_bonuses or NULL_BONUSES
    _has_av = _av.has_any_bonus

    def _final_atk(base: float) -> int:
        return int(_av.apply_attack_bonus(base)) if _has_av else int(base)

    def _final_def(base: float) -> int:
        return int(_av.apply_defence_bonus(base)) if _has_av else int(base)

    def _final_sta(base: float) -> int:
        return int(_av.apply_stamina_bonus(base)) if _has_av else int(base)

    def _final_spc(base: float) -> int:
        return int(_av.apply_special_move_bonus(base)) if _has_av else int(base)

    # ── Apply equipped parts deltas ────────────────────────────────────────
    equipped_parts = profile_doc.get("equipped_parts", [])
    _part_deltas: dict[str, int] = {}
    _has_parts = False
    if equipped_parts:
        try:
            from cogs.economy.shop import get_part_stat_deltas
            _part_deltas = get_part_stat_deltas(equipped_parts)
            _has_parts   = bool(_part_deltas)
        except Exception:
            pass

    def _with_parts(base: float, stat_key: str) -> float:
        return base + _part_deltas.get(stat_key, 0)

    _atk = _final_atk(_with_parts(stats.get("attack",  0), "attack"))
    _def = _final_def(_with_parts(stats.get("defense", 0), "defense"))
    _sta = _final_sta(_with_parts(stats.get("stamina", 0), "stamina"))
    _spc = _final_spc(_with_parts(stats.get("special", 0), "special"))

    # Stat block title reflects active boosts
    if _has_av and _has_parts:
        _stats_title = "📊 Stats  ✨ Avatar + ⚙️ Parts Boosted"
    elif _has_av:
        _stats_title = "📊 Stats  ✨ Avatar Boosted"
    elif _has_parts:
        _stats_title = "📊 Stats  ⚙️ Parts Applied"
    else:
        _stats_title = "📊 Base Stats"

    embed.add_field(
        name  = _stats_title,
        value = (
            f"{_stat_line('ATK', '⚔️', _atk)}\n"
            f"{_stat_line('DEF', '🛡️', _def)}\n"
            f"{_stat_line('STA', '🌀', _sta)}\n"
            f"{_stat_line('SPC', '✨', _spc)}\n"
            f"{_hp_stat_line(active_blade)}"
        ),
        inline=False,
    )

    # ── Ability (compact) ─────────────────────────────────────────────────
    abilities_raw = active_blade.get("abilities", [])
    if not abilities_raw:
        for key in ("ability", "ability_2"):
            ab = active_blade.get(key)
            if isinstance(ab, dict):
                abilities_raw.append(ab)

    if abilities_raw:
        ab = abilities_raw[0]   # show first ability only on profile card
        trigger = TRIGGER_LABELS.get(ab.get("trigger", "passive"), "")
        embed.add_field(
            name  = f"✨ Ability — {ab.get('name', '?')}",
            value = f"*{trigger}*\n{ab.get('description', '')}",
            inline=False,
        )
        if len(abilities_raw) > 1:
            extra = ", ".join(a.get("name", "?") for a in abilities_raw[1:])
            embed.add_field(
                name  = "＋ More Abilities",
                value = f"*{extra}*  ·  Use `;info {active_blade['name']}` for full details.",
                inline=False,
            )

    # ── Special move ──────────────────────────────────────────────────────
    sm = active_blade.get("special_move")
    if sm:
        hits  = sm.get("hits", 1)
        dph   = sm.get("damage_per_hit", 0)
        total = sm.get("total_damage", hits * dph)
        embed.add_field(
            name  = f"🌋 Special Move — {sm.get('name', '???')}",
            value = (
                f"{sm.get('description', '')}\n"
                f"**{hits} hit{'s' if hits != 1 else ''} × {dph} dmg = {total} total**"
            ),
            inline=False,
        )

    embed.set_footer(
        text  = f"Use ;equip <name> to change blade  ·  ;inventory to see collection",
        icon_url = target.display_avatar.url,
    )
    return embed


# ══════════════════════════════════════════════════════════════════════════════
#  Info embed builder (;info)
# ══════════════════════════════════════════════════════════════════════════════

def build_info_embed(blade: dict) -> discord.Embed:
    """
    Detailed Beyblade info card — richer than the old beyblade_info_embed.
    """
    rarity   = blade.get("rarity", "Common")
    btype    = blade.get("type", "Balance")
    stats    = blade.get("stats", {})
    r_emoji  = RARITY_EMOJIS.get(rarity, "⚪")
    t_emoji  = TYPE_EMOJIS.get(btype, "⚖️")
    r_banner = RARITY_BANNERS.get(rarity, rarity.upper())

    embed = discord.Embed(
        color       = _rarity_color(rarity),
        description = (
            f"## {r_emoji} {blade['name']}\n"
            f"{r_banner}\n\n"
            f"*{blade.get('description', 'No description available.')}*"
        ),
    )
    embed.set_image(url=blade.get("image_url", ""))

    # ── Header info ────────────────────────────────────────────────────────
    embed.add_field(name="Rarity",       value=f"{r_emoji} {rarity}",                      inline=True)
    embed.add_field(name="Type",         value=f"{t_emoji} {btype}",                       inline=True)
    embed.add_field(name="Burst Height", value=blade.get("burst_height", "—"),             inline=True)

    # Booster exclusive badge
    if blade.get("booster_exclusive"):
        embed.add_field(
            name="📦 Acquisition",
            value="**Booster Pack Exclusive**\nCannot be found in wild spawns.\nObtain via `;booster`.",
            inline=False,
        )

    # ── Stats ──────────────────────────────────────────────────────────────
    embed.add_field(
        name  = "📊 Stats",
        value = (
            f"{_stat_line('ATK', '⚔️', stats.get('attack',  0))}\n"
            f"{_stat_line('DEF', '🛡️', stats.get('defense', 0))}\n"
            f"{_stat_line('STA', '🌀', stats.get('stamina', 0))}\n"
            f"{_stat_line('SPC', '✨', stats.get('special', 0))}\n"
            f"{_hp_stat_line(blade)}"
        ),
        inline=False,
    )

    # ── All abilities ──────────────────────────────────────────────────────
    abilities = blade.get("abilities", [])
    if not abilities:
        for key in ("ability", "ability_2"):
            ab = blade.get(key)
            if isinstance(ab, dict):
                abilities.append(ab)

    for ab in abilities:
        trigger = TRIGGER_LABELS.get(ab.get("trigger", "passive"), ab.get("trigger", ""))
        embed.add_field(
            name  = f"✨ {ab.get('name', 'Ability')}",
            value = f"*{trigger}*\n{ab.get('description', '')}",
            inline=False,
        )

    # ── Special move ──────────────────────────────────────────────────────
    sm = blade.get("special_move")
    if sm:
        hits  = sm.get("hits", 1)
        dph   = sm.get("damage_per_hit", 0)
        total = sm.get("total_damage", hits * dph)
        embed.add_field(
            name  = f"🌋 Special Move — {sm.get('name', '???')}",
            value = (
                f"{sm.get('description', '')}\n"
                f"**{hits} hit{'s' if hits != 1 else ''} × {dph} dmg = {total} total**"
            ),
            inline=False,
        )

    # ── Ultimate move ──────────────────────────────────────────────────────
    um = blade.get("ultimate_move")
    if um:
        embed.add_field(
            name  = f"☄️ Ultimate Move — {um.get('name', '???')}",
            value = um.get("description", ""),
            inline=False,
        )

    embed.set_footer(text=f"ID: {blade.get('id', '?')}  ·  Use ;equip {blade['name']} to wield this blade!")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
#  Beypedia embed builder (;beypedia)
# ══════════════════════════════════════════════════════════════════════════════

def _beypedia_stat_bar(value: int, max_val: int = 200, bar_len: int = 14) -> str:
    """Render a visual stat bar like: 98 ██████████████░░"""
    filled = round((max(0, min(value, max_val)) / max_val) * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    return f"`{value:<3}` `{bar}`"


def build_beypedia_embed(blade: dict) -> discord.Embed:
    """
    Beypedia-style card matching the Discord screenshot format:
    Rarity · Type · Stats (with bars) · Ability · Special Move · Image
    """
    rarity   = blade.get("rarity", "Common")
    btype    = blade.get("type", "Balance")
    stats    = blade.get("stats", {})
    r_emoji  = RARITY_EMOJIS.get(rarity, "⚪")
    t_emoji  = TYPE_EMOJIS.get(btype, "⚖️")

    # Level is present only when the blade came through loadout.effective_blade
    # (i.e. it is a player's own bey); a bare species lookup has none.
    try:
        _bey_level = int(blade.get("level") or 0)
    except (TypeError, ValueError):
        _bey_level = 0

    embed = discord.Embed(
        title = (f"📚 Beypedia: {blade['name']}"
                 + (f"  •  Lv {_bey_level}" if _bey_level > 0 else "")),
        color = _rarity_color(rarity),
    )

    # Bey image as the big visual (like the screenshot)
    if blade.get("image_url"):
        embed.set_image(url=blade["image_url"])

    # ── ⭐ Rarity ──────────────────────────────────────────────────────────
    embed.add_field(
        name  = "⭐ Rarity",
        value = f"{r_emoji} {rarity}",
        inline=True,
    )

    # ── 🏷️ Type ────────────────────────────────────────────────────────────
    embed.add_field(
        name  = "🏷️ Type",
        value = f"{t_emoji} {btype}",
        inline=True,
    )

    # ── Burst Height / Generation ───────────────────────────────────────────
    bh = blade.get("burst_height")
    if bh:
        embed.add_field(
            name  = "🗂️ Burst Height",
            value = bh,
            inline=True,
        )

    # ── Booster Exclusive badge ─────────────────────────────────────────────
    if blade.get("booster_exclusive"):
        embed.add_field(
            name  = "📦 Acquisition",
            value = "**Booster Pack Exclusive**\nCannot be found in wild spawns.\nObtain via `;booster`.",
            inline=False,
        )

    # ── ⚔️ Stats with bars ─────────────────────────────────────────────────
    atk = stats.get("attack",  0)
    def_ = stats.get("defense", 0)
    sta = stats.get("stamina", 0)
    spc = stats.get("special", 0)

    embed.add_field(
        name  = "📊 Stats",
        value = (
            f"⚔️ **Attack**\n{_beypedia_stat_bar(atk)}\n"
            f"🛡️ **Defense**\n{_beypedia_stat_bar(def_)}\n"
            f"🌀 **Stamina**\n{_beypedia_stat_bar(sta)}\n"
            f"✨ **Special**\n{_beypedia_stat_bar(spc)}\n"
            f"❤️ **HP**\n{_beypedia_hp_bar(blade)}  `{max_hp_for_blade(blade)} battle pool`"
        ),
        inline=False,
    )

    # ── Abilities ──────────────────────────────────────────────────────────
    abilities = blade.get("abilities", [])
    if not abilities:
        for key in ("ability", "ability_2"):
            ab = blade.get(key)
            if isinstance(ab, dict):
                abilities.append(ab)

    for i, ab in enumerate(abilities, 1):
        trigger = TRIGGER_LABELS.get(ab.get("trigger", "passive"), ab.get("trigger", ""))
        num = f" {i}" if len(abilities) > 1 else ""
        embed.add_field(
            name  = f"✨ Ability{num} — {ab.get('name', 'Unknown')}",
            value = f"*{trigger}*\n{ab.get('description', '')}",
            inline=False,
        )

    # ── Special Move ───────────────────────────────────────────────────────
    sm = blade.get("special_move")
    if sm:
        hits  = sm.get("hits", 1)
        dph   = sm.get("damage_per_hit", 0)
        total = sm.get("total_damage", hits * dph)
        embed.add_field(
            name  = f"🌋 Special Move — {sm.get('name', '???')}",
            value = (
                f"{sm.get('description', '')}\n"
                f"💥 **{hits} hit{'s' if hits != 1 else ''} × {dph} = {total} total damage**"
            ),
            inline=False,
        )

    # ── Ultimate Move ──────────────────────────────────────────────────────
    um = blade.get("ultimate_move")
    if um:
        embed.add_field(
            name  = f"☄️ Ultimate Move — {um.get('name', '???')}",
            value = um.get("description", ""),
            inline=False,
        )

    embed.set_footer(text=f"ID: {blade.get('id', '?')}  ·  ;info {blade['name']} for full details")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
#  Cog
# ══════════════════════════════════════════════════════════════════════════════

def fuzzy_find_beyblade(query: str):
    """Return (blade_dict, exact_name) for the closest matching Beyblade name,
    or None if nothing is close enough.  Handles typos, partial names, and
    case differences by trying three strategies in order:

    1. Exact case-insensitive match (delegates to get_beyblade).
    2. Substring match on any word in the name.
    3. difflib closest-match with a similarity cutoff of 0.6.
    """
    # Strategy 1 — exact (already handled by caller, but repeat for safety)
    blade = get_beyblade(query)
    if blade is not None:
        return blade

    all_blades = load_beyblades()          # dict: name -> data
    q_lower = query.strip().lower()

    # Strategy 2 — substring: "sriggan" hits "Lord Spriggan"
    for name, data in all_blades.items():
        if q_lower in name.lower() or any(
            q_lower in word.lower() for word in name.split()
        ):
            data.setdefault("name", name)
            return data

    # Strategy 3 — fuzzy closest match
    all_names = list(all_blades.keys())
    matches = difflib.get_close_matches(
        q_lower,
        [n.lower() for n in all_names],
        n=1,
        cutoff=0.6,
    )
    if matches:
        # Map back to the original-case key
        matched_lower = matches[0]
        for name in all_names:
            if name.lower() == matched_lower:
                data = all_blades[name]
                data.setdefault("name", name)
                return data

    return None


class ProfileCog(commands.Cog, name="Profile"):
    """Handles user profiles and Beyblade information lookups."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── ;profile ──────────────────────────────────────────────────────────────

    # Prefix-only now — the slash entry is `/player profile`, so the top-level
    # slash list carries one `/player` instead of a scattering of `/profile`,
    # `/quests`, `/questclaim`. `;profile` is unchanged for everyone used to it.
    @commands.command(name="profile")
    async def profile(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
    ) -> None:
        """;profile [@user] — Show a player's full Beyblade profile card."""
        target      = member or ctx.author
        profile_doc = get_user(target.id)
        # Resolve through equipped_blade so a player with a boss copy equipped
        # gets their blade on the profile card instead of a blank slot.
        try:
            from cogs.battle.boss import boss_copy as _bcopy
            active_blade, _copy = _bcopy.equipped_blade(target.id)
            # Fold in parts and avatar so the CARD shows what the player
            # actually fights with. Previously the card got the raw blade and
            # the avatar bonuses were only computed on the fallback embed path
            # — which runs when the card render fails — so equipping anything
            # visibly changed nothing.
            if active_blade:
                from utils.loadout import effective_blade
                active_blade, _bd, _av = effective_blade(
                    target.id, profile_doc, active_blade,
                    include_parts=(_copy is None))
        except Exception:
            active_name  = profile_doc.get("active_beyblade")
            active_blade = get_beyblade(active_name) if active_name else None

        async with ctx.typing():
            # The card handles the no-bey case itself, so an unequipped player
            # still gets their level, record and collection instead of a
            # dead-end error.
            card = await self._profile_card_file(target, profile_doc, active_blade)
            if card is not None:
                return await ctx.send(file=card)

            if active_blade is None:
                return await ctx.send(
                    f"🌀 **{target.display_name}** has no active Beyblade!\n"
                    f"Catch one during a spawn with `;claim`, then use `;equip <name>`."
                )
            avatar_bonuses = avatar_engine.get_battle_bonuses(target.id)
            embed = build_profile_embed(target, profile_doc, active_blade, avatar_bonuses)
            await ctx.send(embed=embed)

    async def _profile_card_file(self, target, profile_doc, active_blade):
        """Render the profile card off the event loop. None on any failure so
        the caller falls back to the classic embed."""
        if render_profile_card is None:
            return None
        try:
            total = len(load_beyblades())
        except Exception:
            total = None
        try:
            buf = await asyncio.to_thread(
                render_profile_card,
                {"name": target.display_name, "id": target.id},
                profile_doc,
                active_blade,
                total_beys=total,
            )
            if buf is None:
                return None
            return discord.File(buf, filename="profile.png")
        except Exception:
            # Still fall back to the embed — but do NOT swallow the reason,
            # or a permanently broken card looks like "the card is disabled".
            log.exception("profile card render failed for %s", getattr(target, "id", "?"))
            return None

    # ── ;info — merged into beypedia (see below) ────────────────────────────

    # ── ;equip ────────────────────────────────────────────────────────────────

    @commands.command(name="equip")
    async def equip(self, ctx: commands.Context, *, name: str) -> None:
        """;equip <name> — Switch your active Beyblade."""
        blade = fuzzy_find_beyblade(name)
        if blade is None:
            await ctx.send(f"❌ **{name}** doesn't exist in the database.")
            return

        success = set_active_beyblade(ctx.author.id, blade["name"])
        if not success:
            await ctx.send(
                f"❌ You don't own **{blade['name']}**! "
                f"Catch it first during a spawn."
            )
            return

        rarity = blade.get("rarity", "Common")
        emoji  = RARITY_EMOJIS.get(rarity, "")
        embed  = discord.Embed(
            description = (
                f"✅ {ctx.author.mention} equipped\n"
                f"## {emoji} {blade['name']}\n"
                f"*{RARITY_BANNERS.get(rarity, rarity)}*"
            ),
            color = _rarity_color(rarity),
        )
        embed.set_thumbnail(url=blade.get("image_url", ""))
        await ctx.send(embed=embed)

    # ── ;inventory ────────────────────────────────────────────────────────────

    @commands.command(name="inventory_legacy", hidden=True)
    async def inventory(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
    ) -> None:
        """;inventory [@user] — Browse your full Beyblade collection."""
        target    = member or ctx.author
        prof      = get_user(target.id)
        inventory = prof.get("inventory", [])
        active    = prof.get("active_beyblade")

        if not inventory:
            await ctx.send(f"🎒 **{target.display_name}** has no Beyblades yet!")
            return

        # Group by rarity for a cleaner display
        grouped: dict[str, list[str]] = {}
        for blade_name in inventory:
            blade  = get_beyblade(blade_name)
            rarity = (blade or {}).get("rarity", "Common")
            grouped.setdefault(rarity, []).append(blade_name)

        lines = []
        for rarity in (
            "State Exclusive", "Exclusive", "Ultimate", "Mythic",
            "Legendary", "Epic", "Rare", "Uncommon", "Common",
        ):
            blades = grouped.get(rarity, [])
            if not blades:
                continue
            emoji = RARITY_EMOJIS.get(rarity, "")
            lines.append(f"**{emoji} {rarity}**")
            for b in blades:
                marker = " ◄ **Active**" if b == active else ""
                lines.append(f"  • {b}{marker}")

        embed = discord.Embed(
            title       = f"🎒 {target.display_name}'s Collection — {len(inventory)} Beyblades",
            description = "\n".join(lines),
            color       = discord.Color.from_rgb(30, 144, 255),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_image(url=BG_IMAGE)

        # ── Equipped parts summary ─────────────────────────────────────────────
        equipped = prof.get("equipped_parts", [])
        if equipped:
            from cogs.economy.shop import PARTS_CATALOG, PART_TYPE_EMOJI, get_part_stat_deltas
            cat = {p["name"].lower(): p for p in PARTS_CATALOG}
            part_lines = []
            for pname in equipped:
                p     = cat.get(pname.lower())
                emoji = PART_TYPE_EMOJI.get(p["type"], "🔩") if p else "🔩"
                if p:
                    ps     = p.get("penalty_stat")
                    pv     = p.get("penalty", 0)
                    pen    = f"  `−{pv} {ps.capitalize()}`" if ps and pv else ""
                    bonus  = f"`+{p['bonus']} {p['stat'].capitalize()}`{pen}"
                else:
                    bonus = ""
                part_lines.append(f"{emoji} **{pname}** {bonus}")
            # Show net stat deltas from all parts
            deltas = get_part_stat_deltas(equipped)
            delta_parts = []
            for stat_key, label in (("attack","ATK"),("defense","DEF"),("stamina","STA"),("special","SPC")):
                d = deltas.get(stat_key, 0)
                if d > 0:
                    delta_parts.append(f"⬆️ +{d} {label}")
                elif d < 0:
                    delta_parts.append(f"⬇️ {d} {label}")
            net_line = "  ·  ".join(delta_parts) if delta_parts else ""
            if net_line:
                part_lines.append(f"\n**Net:** {net_line}")
            embed.add_field(
                name="⚙️ Equipped Parts",
                value="\n".join(part_lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="⚙️ Equipped Parts",
                value="*None equipped — use `;equippart <name>` to boost your stats!*",
                inline=False,
            )

        embed.set_footer(text="Use ;equip <name> to change your active blade  ·  ;myparts to manage parts.")
        await ctx.send(embed=embed)

    # ── ;beypedia ─────────────────────────────────────────────────────────────

    @commands.command(name="beypedia", aliases=["bey", "binfo", "info"])
    async def beypedia(self, ctx: commands.Context, *, name: str = None) -> None:
        """;beypedia — your equipped bey | ;beypedia <name> — any bey."""
        if name is None:
            # An equipped boss copy is a rolled instance, so its name is not in
            # beyblades.json — get_beyblade() returned None and that None went
            # straight into render_info_card(), so ";info" broke for anyone
            # holding a copy. equipped_blade() resolves either kind.
            try:
                from cogs.battle.boss import boss_copy as _bcopy
                blade, _copy = _bcopy.equipped_blade(ctx.author.id)
                if blade:
                    from utils.loadout import effective_blade
                    blade, _bd, _av = effective_blade(
                        ctx.author.id, None, blade,
                        include_parts=(_copy is None))
            except Exception:
                profile_doc = get_user(ctx.author.id)
                active_name = profile_doc.get("active_beyblade")
                blade = get_beyblade(active_name) if active_name else None
            if blade is None:
                return await ctx.send(
                    "❌ You have no Beyblade equipped!\n"
                    "Catch one during a spawn then use `;equip <name>`."
                )
        else:
            # Boss blades get a STRICT check first. The fuzzy matcher below is
            # lenient enough that ";info drakos org" matched "DranSword" and
            # answered with the wrong blade entirely — a confident boss match
            # has to win before fuzzy guessing starts.
            try:
                from cogs.battle.boss import boss_info as _bi
                _prof = _bi.find_strict(name)
                if _prof is not None:
                    return await _bi.send_info(
                        ctx, _prof, as_copy=_bi.wants_copy(name))
            except Exception:
                pass

            blade = fuzzy_find_beyblade(name)
            if blade is not None:
                # Show the VIEWER's copy of this bey, not the species sheet:
                # if they have levelled it, the card should say so and quote the
                # levelled stats. effective_blade can't be reused here — it
                # gates levelling behind include_parts, and this blade may not
                # be the one their parts are equipped to.
                blade = _with_viewer_level(ctx.author.id, blade)
            if blade is None:
                # Boss-only blades aren't in beyblades.json — that absence is
                # what keeps them out of ;list, spawns, the shop, boosters, the
                # marketplace and the quiz decoys. They're still lookupable if
                # you know the name, so fall through to the boss registry
                # rather than telling the player they don't exist.
                try:
                    from cogs.battle.boss import boss_info
                    prof = boss_info.find(name)
                    if prof is not None:
                        return await boss_info.send_info(
                            ctx, prof, as_copy=boss_info.wants_copy(name))
                except Exception:
                    pass
                return await ctx.send(
                    f"❌ **{name}** wasn't found. Use `;list` to browse all Beyblades."
                )

        async with ctx.typing():
            # PNG info card first — falls back to the classic embed if Chromium
            # is missing, the CDN art is dead, or the render times out.
            buf = await info_card.render_info_card(
                blade, parts=_viewer_parts(ctx.author.id)
            )
            if buf is not None:
                fname = f"{blade['name'].lower().replace(' ', '_')}_card.png"
                return await ctx.send(file=discord.File(buf, filename=fname))
            await ctx.send(embed=build_beypedia_embed(blade))

    # ── ;list ─────────────────────────────────────────────────────────────────

    @commands.command(name="list", aliases=["blades", "beylist", "dex"])
    async def list_all(self, ctx: commands.Context, *, rarity: str = None) -> None:
        """;list — Browse every Beyblade. ;list <rarity> jumps straight to a tier."""
        beyblades = load_beyblades()
        if not beyblades:
            return await ctx.send("⚠️ The Beyblade database is empty.")

        start = None
        if rarity:
            q = rarity.strip().lower()
            start = next((r for r in RARITY_ORDER if r.lower() == q), None)
            if start is None:
                start = next((r for r in RARITY_ORDER if q in r.lower()), None)
            if start is None:
                return await ctx.send(
                    "❌ Unknown rarity. Options: "
                    + " · ".join(f"`{r}`" for r in RARITY_ORDER
                                 if any(b.get("rarity") == r
                                        for b in beyblades.values())))

        view = BeyListView(ctx.author, beyblades, start)
        view.message = await ctx.send(embed=view.embed(), view=view)



# ══════════════════════════════════════════════════════════════════════════════
#  ;list — Beyblade browser
#
#  The old version dumped all 78 blades into seven inline fields: roughly 85
#  lines, about a dozen phone screens, and one growth spurt away from blowing
#  the 1024-char field limit. This is the same data as a rarity dropdown plus
#  five blades a page, and it marks what you already own so ;list doubles as a
#  collection tracker.
# ══════════════════════════════════════════════════════════════════════════════

RARITY_ORDER = [
    "State Exclusive", "Exclusive", "Ultimate", "Mythic",
    "Legendary", "Epic", "Rare", "Uncommon", "Common",
]

LIST_PAGE = 5   # one-line rows — five plus nav fits a phone screen


def _owned_names(user_id: int) -> set[str]:
    try:
        return {n.lower() for n in (get_user(user_id).get("inventory") or [])}
    except Exception:
        return set()


class RaritySelect(discord.ui.Select):
    def __init__(self, view: "BeyListView"):
        opts = [discord.SelectOption(
            label="All rarities", value="__all__", emoji="📖",
            description=f"{len(view.beyblades)} blades total",
            default=(view.rarity is None))]
        for r in RARITY_ORDER:
            names = view.by_rarity.get(r)
            if not names:
                continue
            owned = sum(1 for n in names if n.lower() in view.owned)
            opts.append(discord.SelectOption(
                label=r, value=r,
                emoji=RARITY_EMOJIS.get(r) or None,
                description=f"{owned}/{len(names)} collected",
                default=(view.rarity == r)))
        super().__init__(placeholder="✨ Pick a rarity…", options=opts[:25], row=0)

    async def callback(self, interaction: discord.Interaction):
        v: "BeyListView" = self.view
        if not v.owns(interaction):
            return await v.deny(interaction)
        v.rarity = None if self.values[0] == "__all__" else self.values[0]
        v.page = 0
        v.rebuild()
        await interaction.response.edit_message(embed=v.embed(), view=v)


class BladeSelect(discord.ui.Select):
    def __init__(self, view: "BeyListView"):
        rows = view.current()
        opts = []
        for name in rows:
            blade = view.beyblades.get(name, {})
            mark  = "✅" if name.lower() in view.owned else "⬜"
            opts.append(discord.SelectOption(
                label=ui_trunc(name, 96),
                value=ui_trunc(name, 96),
                emoji=RARITY_EMOJIS.get(blade.get("rarity", "")) or None,
                description=ui_trunc(
                    f"{mark} {blade.get('type', '—')} · "
                    f"{blade.get('rarity', '—')}", 96),
            ))
        if not opts:
            opts = [discord.SelectOption(label="—", value="__none__")]
        super().__init__(placeholder="🔍 Open a blade…", options=opts, row=1)

    async def callback(self, interaction: discord.Interaction):
        v: "BeyListView" = self.view
        if not v.owns(interaction):
            return await v.deny(interaction)
        name = self.values[0]
        blade = v.beyblades.get(name) or get_beyblade(name)
        if blade is None:
            return await interaction.response.defer()
        e = build_beypedia_embed(blade)
        if name.lower() in v.owned:
            e.set_footer(text="✅ You own this one")
        else:
            e.set_footer(text="⬜ Not in your collection yet")
        await interaction.response.send_message(embed=e, ephemeral=True)


class _ListPrev(discord.ui.Button):
    def __init__(self, disabled):
        super().__init__(emoji="◀", row=2, disabled=disabled,
                         style=discord.ButtonStyle.secondary)

    async def callback(self, interaction):
        v = self.view
        if not v.owns(interaction):
            return await v.deny(interaction)
        v.page = max(0, v.page - 1); v.rebuild()
        await interaction.response.edit_message(embed=v.embed(), view=v)


class _ListNext(discord.ui.Button):
    def __init__(self, disabled):
        super().__init__(emoji="▶", row=2, disabled=disabled,
                         style=discord.ButtonStyle.secondary)

    async def callback(self, interaction):
        v = self.view
        if not v.owns(interaction):
            return await v.deny(interaction)
        v.page = min(v.pages() - 1, v.page + 1); v.rebuild()
        await interaction.response.edit_message(embed=v.embed(), view=v)


class _ListLabel(discord.ui.Button):
    def __init__(self, page, total):
        super().__init__(label=f"{page}/{total}", row=2, disabled=True,
                         style=discord.ButtonStyle.secondary)


class _OwnedToggle(discord.ui.Button):
    def __init__(self, active: bool):
        super().__init__(
            label="Missing only" if not active else "Showing missing",
            emoji="⬜", row=2,
            style=discord.ButtonStyle.primary if active
            else discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction):
        v = self.view
        if not v.owns(interaction):
            return await v.deny(interaction)
        v.missing_only = not v.missing_only
        v.page = 0
        v.rebuild()
        await interaction.response.edit_message(embed=v.embed(), view=v)


class BeyListView(discord.ui.View):
    def __init__(self, owner, beyblades: dict, rarity: Optional[str] = None):
        super().__init__(timeout=180)
        self.owner     = owner
        self.beyblades = beyblades
        self.owned     = _owned_names(owner.id)
        self.rarity    = rarity
        self.page      = 0
        self.missing_only = False
        self.message   = None

        self.by_rarity: dict[str, list[str]] = {}
        for name, data in beyblades.items():
            self.by_rarity.setdefault(data.get("rarity", "Common"), []).append(name)
        for names in self.by_rarity.values():
            names.sort()

        self.rebuild()

    # ── Access ───────────────────────────────────────────────────────────────
    def owns(self, interaction) -> bool:
        return interaction.user.id == self.owner.id

    async def deny(self, interaction):
        await interaction.response.send_message(
            "That's not your list — run `;list` yourself!", ephemeral=True)

    # ── Data ─────────────────────────────────────────────────────────────────
    def names(self) -> list[str]:
        if self.rarity is None:
            out = []
            for r in RARITY_ORDER:
                out.extend(self.by_rarity.get(r, []))
        else:
            out = list(self.by_rarity.get(self.rarity, []))
        if self.missing_only:
            out = [n for n in out if n.lower() not in self.owned]
        return out

    def pages(self) -> int:
        return max(1, (len(self.names()) + LIST_PAGE - 1) // LIST_PAGE)

    def current(self) -> list[str]:
        start = self.page * LIST_PAGE
        return self.names()[start:start + LIST_PAGE]

    # ── Layout ───────────────────────────────────────────────────────────────
    def rebuild(self):
        self.clear_items()
        self.add_item(RaritySelect(self))
        if self.names():
            self.add_item(BladeSelect(self))
        total = self.pages()
        if total > 1:
            self.add_item(_ListPrev(self.page == 0))
            self.add_item(_ListLabel(self.page + 1, total))
            self.add_item(_ListNext(self.page >= total - 1))
        self.add_item(_OwnedToggle(self.missing_only))

    def embed(self) -> discord.Embed:
        names = self.names()
        total_owned = sum(1 for n in self.beyblades if n.lower() in self.owned)

        # ── Hub: progress per tier only. Showing the grid AND five blade rows
        #    put this at 13 lines; a phone shows about seven. ────────────────
        if self.rarity is None and not self.missing_only:
            e = discord.Embed(
                title="📖  Beyblade Database",
                description=(
                    f"`{ui_bar(total_owned, max(1, len(self.beyblades)), 10)}` "
                    f"**{total_owned}/{len(self.beyblades)}** collected\n"
                    f"*Pick a rarity below to browse it.*"
                ),
                color=discord.Color.gold(),
            )
            tiers = [r for r in RARITY_ORDER if self.by_rarity.get(r)]
            half  = (len(tiers) + 1) // 2
            # Two inline fields render side by side on mobile, halving the height.
            for group in (tiers[:half], tiers[half:]):
                if not group:
                    continue
                e.add_field(
                    name="\u200b",
                    value="\n".join(
                        f"{RARITY_EMOJIS.get(r, '')} "
                        f"`{sum(1 for n in self.by_rarity[r] if n.lower() in self.owned)}"
                        f"/{len(self.by_rarity[r])}` {r}"
                        for r in group),
                    inline=True,
                )
            e.set_footer(text="✅ owned  ⬜ missing  📦 booster exclusive")
            return e

        # ── Tier / missing-only listing ──────────────────────────────────────
        rows = self.current()
        if not rows:
            body = ("🎉 You've collected every blade here!"
                    if self.missing_only else "Nothing here.")
        else:
            lines = []
            for n in rows:
                blade = self.beyblades.get(n, {})
                mark  = "✅" if n.lower() in self.owned else "⬜"
                emoji = RARITY_EMOJIS.get(blade.get("rarity", ""), "")
                pack  = " 📦" if blade.get("booster_exclusive") else ""
                lines.append(f"{mark} {emoji} **{n}**{pack}")
            body = "\n".join(lines)

        if self.rarity:
            tier       = self.by_rarity.get(self.rarity, [])
            total_here = len(tier)
            owned_here = sum(1 for n in tier if n.lower() in self.owned)
            scope      = self.rarity
        else:
            total_here = len(self.beyblades)
            owned_here = total_owned
            scope      = "All rarities"

        header = (f"**{scope}** `{ui_bar(owned_here, max(1, total_here), 8)}` "
                  f"{owned_here}/{total_here}\n")
        if self.missing_only:
            header += "*Showing missing only.*\n"

        e = discord.Embed(
            title="📖  Beyblade Database",
            description=header + "\n" + body,
            color=rarity_colour(self.rarity) if self.rarity else discord.Color.gold(),
        )
        e.set_footer(text=f"Page {self.page + 1}/{self.pages()} · "
                          f"{len(names)} shown  •  ✅ owned  ⬜ missing")
        return e

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  XP Award Utility
#  Call this everywhere XP is granted (battle wins, events, etc.)
#
#  Usage:
#    from cogs.economy.profile import award_xp
#    await award_xp(bot, user_id, xp_amount, channel=ctx.channel)
#
#  If the user levels up, fires bot.dispatch("level_up", user_id, old_level, new_level, channel)
#  The LevelUpCog in ui/level_up.py listens for this and sends the embed.
# ══════════════════════════════════════════════════════════════════════════════

async def award_xp(
    bot: commands.Bot,
    user_id: int,
    amount: int,
    channel: discord.abc.Messageable | None = None,
) -> tuple[int, int, int]:
    """
    Add *amount* XP to user_id, persist the change, and fire a level_up
    event if the user crossed a level threshold.

    Returns (total_xp, old_level, new_level).
    """
    from utils.database import get_user, update_user

    profile = get_user(user_id)
    old_total = profile.get("xp", 0)
    new_total = old_total + amount

    old_level, _, _ = xp_to_next_level(old_total)
    new_level, _, _ = xp_to_next_level(new_total)

    profile["xp"] = new_total
    update_user(user_id, profile)

    if new_level > old_level:
        bot.dispatch("level_up", user_id, old_level, new_level, channel)

    return new_total, old_level, new_level


class PlayerCommands(commands.Cog, name="Player"):
    """Slash-only grouping for the per-player commands.

    These delegate to the existing prefix commands rather than duplicating
    their logic: the originals own the validation, cooldowns and rendering,
    and a second copy is how the two paths quietly drift apart. Prefix
    commands keep working exactly as before.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    player = discord.app_commands.Group(
        name="player", description="Your profile, quests and progress")

    async def _run(self, interaction: discord.Interaction, command_name: str,
                   *args) -> None:
        cmd = self.bot.get_command(command_name)
        if cmd is None:
            return await interaction.response.send_message(
                f"`{command_name}` isn't loaded right now.", ephemeral=True)
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.invoke(cmd, *args)

    @player.command(name="profile", description="Show a blader's profile card")
    @discord.app_commands.describe(member="Whose profile (defaults to you)")
    async def p_profile(self, interaction: discord.Interaction,
                        member: Optional[discord.Member] = None) -> None:
        await self._run(interaction, "profile", member)

    @player.command(name="quests", description="View your daily & weekly quests")
    async def p_quests(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, "quests")

    @player.command(name="claim",
                    description="Claim rewards for completed quests")
    async def p_claim(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, "questclaim")

    @player.command(name="inventory", description="Your Beyblade collection")
    async def p_inventory(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, "inventory")

    @player.command(name="balance", description="Your Beycoin balance")
    async def p_balance(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, "bal")

    @player.command(name="achievements", description="Your achievements")
    async def p_achievements(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, "achievements")

    @player.command(name="mastery", description="Your blade mastery levels")
    async def p_mastery(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, "mastery")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProfileCog(bot))
    await bot.add_cog(PlayerCommands(bot))
