"""
mastery.py  —  🔰 Blade Mastery

Every blade you fight with earns its own mastery XP. Stick with one blade and
it gets measurably better in your hands — which gives collectors a reason to
main something instead of always swapping to whatever dropped last.

Storage lives on the profile so it travels with the user:

    profile["mastery"] = {
        "Blade Name": {"xp": int, "battles": int, "wins": int}
    }

Levels are 0-10. Each level adds MASTERY_BONUS_PER_LEVEL to that blade's stat
multiplier, stacking with the existing trainer-level bonus. The cap is
deliberately small (+5% at max) so mastery is a nudge, not a wall a new player
can't get past.

Commands:
    ;mastery              → your top blades by mastery
    ;mastery <blade>      → detail for one blade
"""

import math
from typing import Optional

import discord
from discord.ext import commands

from utils.database import get_beyblade, get_user, update_user
from utils.embeds import RARITY_EMOJIS, rarity_colour
from utils.mobile_ui import MobileListView, bar as ui_bar

MAX_MASTERY_LEVEL       = 10
MASTERY_BONUS_PER_LEVEL = 0.005      # +0.5% per level → +5% at Lv10
XP_PER_BATTLE           = 10
XP_PER_WIN              = 25
XP_BASE                 = 60         # XP needed for level 1

RANK_NAMES = [
    "Unranked", "Novice", "Apprentice", "Adept", "Skilled", "Expert",
    "Veteran", "Elite", "Master", "Grandmaster", "Legend",
]


def xp_for_level(level: int) -> int:
    """Total XP needed to reach `level`. Quadratic, same shape as trainer XP."""
    if level <= 0:
        return 0
    return XP_BASE * level * level


def level_from_xp(xp: int) -> int:
    if xp <= 0:
        return 0
    return min(MAX_MASTERY_LEVEL, int(math.isqrt(max(0, xp) // XP_BASE)))


def progress(xp: int) -> tuple[int, int, int]:
    """(level, xp_into_level, xp_needed_for_next). Needed is 0 at max level."""
    lvl = level_from_xp(xp)
    if lvl >= MAX_MASTERY_LEVEL:
        return lvl, 0, 0
    floor_xp = xp_for_level(lvl)
    next_xp  = xp_for_level(lvl + 1)
    return lvl, xp - floor_xp, next_xp - floor_xp


def rank_name(level: int) -> str:
    return RANK_NAMES[min(level, len(RANK_NAMES) - 1)]


def get_entry(profile: dict, blade_name: str) -> dict:
    m = profile.get("mastery") or {}
    return m.get(blade_name) or {"xp": 0, "battles": 0, "wins": 0}


def mastery_level(user_id: int, blade_name: Optional[str]) -> int:
    if not blade_name:
        return 0
    try:
        return level_from_xp(get_entry(get_user(user_id), blade_name).get("xp", 0))
    except Exception:
        return 0


def mastery_bonus(user_id: int, blade_name: Optional[str]) -> float:
    """Extra stat multiplier from mastery. 0.0 when the blade is unmastered."""
    return mastery_level(user_id, blade_name) * MASTERY_BONUS_PER_LEVEL


def award(user_id: int, blade_name: str, won: bool) -> tuple[int, int, bool]:
    """Grant mastery XP after a battle. Returns (old_level, new_level, leveled)."""
    profile = get_user(user_id)
    mastery = profile.get("mastery")
    if not isinstance(mastery, dict):
        mastery = {}

    entry = mastery.get(blade_name)
    if not isinstance(entry, dict):
        entry = {"xp": 0, "battles": 0, "wins": 0}

    old = level_from_xp(entry.get("xp", 0))
    entry["xp"]      = entry.get("xp", 0) + XP_PER_BATTLE + (XP_PER_WIN if won else 0)
    entry["battles"] = entry.get("battles", 0) + 1
    if won:
        entry["wins"] = entry.get("wins", 0) + 1

    mastery[blade_name] = entry
    profile["mastery"]  = mastery
    update_user(user_id, profile)

    new = level_from_xp(entry["xp"])
    return old, new, new > old


def _bar(done: int, total: int, width: int = 8) -> str:
    """8 cells, not 12 — 12 wraps on a narrow phone."""
    return ui_bar(done, total, width)


def _detail_embed(user, blade_name: str, entry: dict) -> discord.Embed:
    """Compact single-blade card. Two inline fields per row max so a phone
    renders them side by side instead of squashing three into one line."""
    lvl, into, need = progress(entry.get("xp", 0))
    bey    = get_beyblade(blade_name) or {}
    rarity = bey.get("rarity", "Common")
    wins   = entry.get("wins", 0)
    fights = entry.get("battles", 0)
    wr     = f"{wins / fights * 100:.0f}%" if fights else "—"

    prog = (f"`{_bar(into, need)}` {into}/{need} XP" if need
            else "`████████` **MAXED**")

    e = discord.Embed(
        title=f"🔰 {RARITY_EMOJIS.get(rarity, '')} {trunc_name(blade_name)}",
        description=(
            f"**Mastery {lvl}** · *{rank_name(lvl)}*\n"
            f"{prog}\n"
            f"**+{lvl * MASTERY_BONUS_PER_LEVEL * 100:.1f}%** stats with this blade"
        ),
        color=rarity_colour(rarity),
    )
    if bey.get("image_url"):
        e.set_thumbnail(url=bey["image_url"])
    e.add_field(name="Battles", value=str(fights), inline=True)
    e.add_field(name="Wins",    value=f"{wins} ({wr})", inline=True)
    e.set_footer(text=f"+{XP_PER_BATTLE} XP a battle, +{XP_PER_WIN} more for a win")
    return e


def trunc_name(name: str, limit: int = 28) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


class MasteryCog(commands.Cog, name="Mastery"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Battle hook ──────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_beycord_battle_blades(self, result: dict):
        """Fired by session.py with per-player blade info."""
        try:
            blades   = result.get("blades") or {}
            winner   = result.get("winner_id")
            channel  = result.get("channel")
            level_ups = []

            for uid_str, blade_name in blades.items():
                if not blade_name:
                    continue
                uid = int(uid_str)
                _old, new, up = award(uid, blade_name, won=(winner == uid))
                if up:
                    level_ups.append((uid, blade_name, new))

            if level_ups and channel is not None:
                lines = [
                    f"🔰 <@{uid}> — **{blade}** reached "
                    f"**Mastery {lvl}** ({rank_name(lvl)}) "
                    f"→ +{lvl * MASTERY_BONUS_PER_LEVEL * 100:.1f}% stats with this blade"
                    for uid, blade, lvl in level_ups
                ]
                await channel.send(embed=discord.Embed(
                    title="🔰  Mastery Up!",
                    description="\n".join(lines),
                    color=0x1abc9c,
                ))
        except Exception:
            pass

    # ── ;mastery ─────────────────────────────────────────────────────────────
    @commands.command(name="mastery", aliases=["bladelevel", "bl"])
    async def mastery(self, ctx: commands.Context, *, blade: str = None):
        """🔰 See how well you know your blades."""
        profile = get_user(ctx.author.id)
        data    = profile.get("mastery") or {}

        if not data:
            return await ctx.send(embed=discord.Embed(
                title="🔰  Blade Mastery",
                description=(
                    "No mastery yet.\n\n"
                    "Every battle earns mastery XP for the blade you used — "
                    "stick with one and it gets stronger in your hands."
                ),
                color=0x1abc9c,
            ))

        # ── Direct lookup: ;mastery <blade> ──────────────────────────────────
        if blade:
            q = blade.strip().lower()
            match = next((k for k in data if k.lower() == q), None) \
                 or next((k for k in data if q in k.lower()), None)
            if match is None:
                return await ctx.send(
                    f"❌ No mastery on **{blade}**. Run `;mastery` to browse.")
            return await ctx.send(embed=_detail_embed(ctx.author, match, data[match]))

        # ── Paginated browser ────────────────────────────────────────────────
        ranked = sorted(data.items(), key=lambda kv: -kv[1].get("xp", 0))
        total_lvls = sum(level_from_xp(v.get("xp", 0)) for v in data.values())

        def render(item, idx):
            name, entry = item
            lvl, into, need = progress(entry.get("xp", 0))
            tail = "MAX" if not need else f"{into}/{need}"
            return (f"**{idx + 1}. {trunc_name(name)}** — M**{lvl}** "
                    f"*{rank_name(lvl)}*\n"
                    f"`{_bar(into, need)}` {tail} · "
                    f"{entry.get('battles', 0)}⚔ · "
                    f"+{lvl * MASTERY_BONUS_PER_LEVEL * 100:.1f}%")

        def option_of(item):
            name, entry = item
            lvl = level_from_xp(entry.get("xp", 0))
            return (name, f"Mastery {lvl} · {entry.get('battles', 0)} battles", "🔰")

        async def detail(interaction, item):
            name, entry = item
            await interaction.response.send_message(
                embed=_detail_embed(ctx.author, name, entry), ephemeral=True)

        view = MobileListView(
            owner=ctx.author,
            title=f"🔰  {ctx.author.display_name}'s Mastery",
            items=ranked,
            render=render,
            option_of=option_of,
            detail=detail,
            detail_placeholder="🔍 Open a blade…",
            page_size=3,          # 2-line rows — 3 fits a phone, 4 overflows
            colour=0x1abc9c,
            description=f"**{len(data)}** blades trained · **{total_lvls}** total levels",
            footer="tap a blade for detail",
        )
        view.message = await ctx.send(embed=view.embed(), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(MasteryCog(bot))
