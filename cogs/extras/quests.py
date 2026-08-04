"""
cogs/extras/quests.py
---------------------
Daily & Weekly Quest system.

Quests track progress via bot events dispatched from other systems:
  beycord_battle_end   (winner_id, participant_ids, guild_id)
  beycord_stamina_move (user_id)
  beycord_spawn_catch  (user_id)

Progress is stored on the user profile under profile["quests"]:
  {
    "daily":  {"date": "YYYY-MM-DD", "progress": {event: n}, "claimed": [ids]},
    "weekly": {"week": "YYYY-Www",   "progress": {event: n}, "claimed": [ids]},
  }

Commands
--------
  ;quests   — show today's / this week's quests with progress bars
  ;questclaim — claim rewards for all completed, unclaimed quests
"""

from __future__ import annotations

import datetime

import discord
from discord.ext import commands

from utils.database import get_user, update_user


# ── Quest definitions ─────────────────────────────────────────────────────────
# (quest_id, label, event_key, target, coin_reward)
DAILY_QUESTS = [
    ("d_win3",   "Win 3 battles",              "battle_win",   3,  300),
    ("d_play5",  "Play 5 battles",             "battle_play",  5,  200),
    ("d_sta5",   "Use the Stamina move 5×",    "stamina_move", 5,  150),
    ("d_catch1", "Catch 1 wild Beyblade",      "spawn_catch",  1,  200),
]
WEEKLY_QUESTS = [
    ("w_win15",  "Win 15 battles",             "battle_win",  15, 1500),
    ("w_play30", "Play 30 battles",            "battle_play", 30, 1000),
    ("w_catch8", "Catch 8 wild Beyblades",     "spawn_catch",  8, 1200),
]


def _today() -> str:
    return datetime.date.today().isoformat()


def _this_week() -> str:
    iso = datetime.date.today().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _ensure_periods(profile: dict) -> dict:
    """Reset daily/weekly blocks when the period rolls over."""
    q = profile.get("quests") or {}
    if not isinstance(q, dict):
        q = {}
    d = q.get("daily") or {}
    if d.get("date") != _today():
        d = {"date": _today(), "progress": {}, "claimed": []}
    w = q.get("weekly") or {}
    if w.get("week") != _this_week():
        w = {"week": _this_week(), "progress": {}, "claimed": []}
    q["daily"], q["weekly"] = d, w
    profile["quests"] = q
    return profile


def _bump(user_id: int, event_key: str, n: int = 1) -> None:
    """Increment quest progress for one user for one event type."""
    try:
        profile = get_user(user_id)
        profile = _ensure_periods(profile)
        for period in ("daily", "weekly"):
            prog = profile["quests"][period]["progress"]
            prog[event_key] = prog.get(event_key, 0) + n
        update_user(user_id, profile)
    except Exception:
        pass  # quests must never break the parent flow


def _bar(cur: int, target: int, length: int = 8) -> str:
    filled = min(length, round((min(cur, target) / target) * length)) if target else 0
    return "🟩" * filled + "⬛" * (length - filled)


class QuestsCog(commands.Cog, name="Quests"):
    """Daily & weekly quests with coin rewards."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Event listeners ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_beycord_battle_end(self, winner_id, participant_ids, guild_id) -> None:
        for pid in participant_ids or []:
            _bump(pid, "battle_play")
        if winner_id:
            _bump(winner_id, "battle_win")

    @commands.Cog.listener()
    async def on_beycord_stamina_move(self, user_id: int) -> None:
        _bump(user_id, "stamina_move")

    @commands.Cog.listener()
    async def on_beycord_spawn_catch(self, user_id: int) -> None:
        _bump(user_id, "spawn_catch")

    # ── Commands ──────────────────────────────────────────────────────────────

    # Prefix-only; the slash entry is `/player quests`.
    @commands.command(name="quests", aliases=["quest", "missions"])
    async def quests(self, ctx: commands.Context) -> None:
        profile = _ensure_periods(get_user(ctx.author.id))
        update_user(ctx.author.id, profile)
        q = profile["quests"]

        def block(defs, period_key: str) -> str:
            data  = q[period_key]
            lines = []
            for qid, label, ev, target, reward in defs:
                cur     = min(data["progress"].get(ev, 0), target)
                claimed = qid in data["claimed"]
                status  = "✅" if claimed else ("🎁" if cur >= target else "▫️")
                lines.append(
                    f"{status} **{label}**\n"
                    f"`{_bar(cur, target)}` {cur}/{target} — 💰 {reward}"
                )
            return "\n".join(lines)

        embed = discord.Embed(
            title=f"📋 Quests — {ctx.author.display_name}",
            color=discord.Color.gold(),
        )
        embed.add_field(name=f"☀️ Daily ({q['daily']['date']})",
                        value=block(DAILY_QUESTS, "daily"), inline=False)
        embed.add_field(name=f"📅 Weekly ({q['weekly']['week']})",
                        value=block(WEEKLY_QUESTS, "weekly"), inline=False)
        embed.set_footer(text="🎁 = ready to claim → use ;questclaim")
        await ctx.send(embed=embed)

    # NOTE: must NOT be named "claim" — cogs/spawn/spawn.py already owns ;claim
    # (wild-spawn claiming). Duplicate names raise CommandRegistrationError and
    # kill this entire cog at load time.
    # Prefix-only; the slash entry is `/player claim`.
    @commands.command(name="questclaim", aliases=["qclaim", "claimquests"])
    async def questclaim(self, ctx: commands.Context) -> None:
        profile = _ensure_periods(get_user(ctx.author.id))
        q = profile["quests"]
        total, claimed_names = 0, []
        for defs, period_key in ((DAILY_QUESTS, "daily"), (WEEKLY_QUESTS, "weekly")):
            data = q[period_key]
            for qid, label, ev, target, reward in defs:
                if qid in data["claimed"]:
                    continue
                if data["progress"].get(ev, 0) >= target:
                    data["claimed"].append(qid)
                    total += reward
                    claimed_names.append(f"• {label} — 💰 {reward}")
        if not total:
            return await ctx.send("📋 No completed quests to claim yet. Check `;quests`!")
        profile["coins"] = profile.get("coins", 0) + total
        update_user(ctx.author.id, profile)
        await ctx.send(embed=discord.Embed(
            title="🎁 Quests Claimed!",
            description="\n".join(claimed_names) + f"\n\n**Total: +{total}** 💰",
            color=discord.Color.green(),
        ))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(QuestsCog(bot))
