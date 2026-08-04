"""
cogs/extras/raid.py
-------------------
Co-op Boss Raid system.

An admin spawns a boss with a huge HP pool. Everyone in the server attacks
it with their equipped blade (60s per-player cooldown). Damage scales with
the blade's ATK stat (parts included). When the boss dies, a coin pool is
split proportionally to damage dealt, plus a flat participation bonus.

Commands
--------
  ;raid spawn [hp] [name...]   (admin) spawn a boss (default 50,000 HP)
  ;raid attack                  hit the boss with your equipped blade
  ;raid status                  boss HP + top-5 damage leaderboard
  ;raid end                     (admin) cancel the active raid

State persists to data/raid.json (atomic write) so restarts don't wipe a raid.
"""

from __future__ import annotations

import json
import math
import os
import random
import threading
import time

import discord
from discord.ext import commands

from utils.database import get_user, update_user, get_beyblade

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RAID_PATH = os.path.join(_PROJECT_ROOT, "data", "raid.json")
_raid_lock = threading.Lock()

ATTACK_COOLDOWN   = 60          # seconds between hits per player
DEFAULT_BOSS_HP   = 50_000
REWARD_POOL_RATIO = 0.10        # coin pool = max_hp × this
PARTICIPATION_MIN = 100         # flat coins for anyone who hit at least once
CRIT_CHANCE       = 0.12
CRIT_MULT         = 1.6


# ── Persistence helpers ───────────────────────────────────────────────────────

def _load() -> dict:
    with _raid_lock:
        if not os.path.exists(_RAID_PATH):
            return {}
        try:
            with open(_RAID_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def _save(data: dict) -> None:
    with _raid_lock:
        os.makedirs(os.path.dirname(_RAID_PATH), exist_ok=True)
        tmp = _RAID_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _RAID_PATH)


def _hp_bar(cur: int, mx: int, length: int = 14) -> str:
    pct    = max(0.0, cur / mx) if mx else 0.0
    filled = round(pct * length)
    return "🟥" * filled + "⬛" * (length - filled)


def _player_attack_stat(user_id: int) -> tuple[int, str] | None:
    """Return (attack_stat_with_parts, blade_name) for the equipped blade."""
    profile = get_user(user_id)
    active  = profile.get("active_beyblade")
    if not active:
        return None
    # A boss copy's name is not in beyblades.json, so get_beyblade() returned
    # None and the player was silently dropped from the raid as if they had
    # nothing equipped. equipped_blade() resolves copies and database blades.
    try:
        from cogs.battle.boss import boss_copy as _bcopy
        blade, _copy = _bcopy.equipped_blade(user_id)
    except Exception:
        blade = get_beyblade(active)
    if not blade:
        return None
    atk = int(blade.get("stats", {}).get("attack", 50))
    # Parts flat deltas
    try:
        from cogs.economy.shop import get_part_stat_deltas
        pd  = get_part_stat_deltas(profile.get("equipped_parts", []))
        atk += int(pd.get("attack", 0))
    except Exception:
        pass
    return max(0, atk), blade.get("name", active)


class RaidCog(commands.Cog, name="Boss Raid"):
    """Server-wide co-op boss battles."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Command group ─────────────────────────────────────────────────────────

    @commands.guild_only()
    @commands.group(name="raid", invoke_without_command=True,
                    brief="Co-op boss raid! 🐉")
    async def raid(self, ctx: commands.Context) -> None:
        await self.status(ctx)

    @raid.command(name="spawn", brief="(admin) Spawn a raid boss")
    @commands.has_permissions(administrator=True)
    async def spawn(self, ctx: commands.Context, hp: int = DEFAULT_BOSS_HP,
                    *, name: str = "Dark Dragoon") -> None:
        gid  = str(ctx.guild.id)
        data = _load()
        if data.get(gid, {}).get("hp", 0) > 0:
            return await ctx.send("⚠️ A raid boss is already active! `;raid status`")
        hp = max(1000, min(10_000_000, int(hp)))
        data[gid] = {
            "name": name[:40], "hp": hp, "max_hp": hp,
            "damage": {}, "last_hit": {}, "started_at": int(time.time()),
            "channel_id": ctx.channel.id,
        }
        _save(data)
        await ctx.send(embed=discord.Embed(
            title=f"🐉 RAID BOSS APPEARED — {name}!",
            description=(
                f"`{_hp_bar(hp, hp)}`\n**{hp:,} / {hp:,} HP**\n\n"
                f"⚔️ Everyone attack with `;raid attack`!\n"
                f"💰 Reward pool: **{int(hp * REWARD_POOL_RATIO):,}** coins "
                f"split by damage dealt!"
            ),
            color=discord.Color.dark_red(),
        ))

    @raid.command(name="attack", aliases=["hit", "atk"], brief="Attack the raid boss ⚔️")
    async def attack(self, ctx: commands.Context) -> None:
        gid  = str(ctx.guild.id)
        data = _load()
        boss = data.get(gid)
        if not boss or boss.get("hp", 0) <= 0:
            return await ctx.send("💤 No active raid boss. An admin can spawn one with `;raid spawn`.")

        uid  = str(ctx.author.id)
        now  = time.time()
        last = boss.get("last_hit", {}).get(uid, 0)
        if now - last < ATTACK_COOLDOWN:
            wait = int(ATTACK_COOLDOWN - (now - last))
            return await ctx.send(f"⏳ {ctx.author.mention} your blade is recovering — **{wait}s** left!")

        stat = _player_attack_stat(ctx.author.id)
        if stat is None:
            return await ctx.send("❌ You need an equipped Beyblade! Use `;equip <name>`.")
        atk, blade_name = stat

        # Damage: 2×ATK base + up to 1×ATK random, 12% crit ×1.6
        dmg  = atk * 2 + random.randint(0, max(1, atk))
        crit = random.random() < CRIT_CHANCE
        if crit:
            dmg = math.ceil(dmg * CRIT_MULT)
        dmg = max(10, dmg)

        boss["hp"] = max(0, boss["hp"] - dmg)
        boss.setdefault("damage", {})[uid] = boss["damage"].get(uid, 0) + dmg
        boss.setdefault("last_hit", {})[uid] = now
        data[gid] = boss
        _save(data)

        line = (f"{'💥 **CRIT!** ' if crit else '⚔️ '}**{ctx.author.display_name}**'s "
                f"**{blade_name}** hits **{boss['name']}** for **{dmg:,}** damage!")
        if boss["hp"] > 0:
            await ctx.send(
                f"{line}\n`{_hp_bar(boss['hp'], boss['max_hp'])}` "
                f"**{boss['hp']:,} / {boss['max_hp']:,} HP**"
            )
        else:
            await self._boss_defeated(ctx, gid, boss, killer=ctx.author)

    async def _boss_defeated(self, ctx: commands.Context, gid: str, boss: dict,
                             killer: discord.Member) -> None:
        total = sum(boss.get("damage", {}).values()) or 1
        pool  = int(boss["max_hp"] * REWARD_POOL_RATIO)
        lines = []
        ranked = sorted(boss["damage"].items(), key=lambda kv: kv[1], reverse=True)
        for i, (uid, dmg) in enumerate(ranked):
            share = int(pool * (dmg / total)) + PARTICIPATION_MIN
            try:
                prof = get_user(int(uid))
                prof["coins"] = prof.get("coins", 0) + share
                update_user(int(uid), prof)
            except Exception:
                continue
            if i < 5:
                member = ctx.guild.get_member(int(uid))
                mname  = member.display_name if member else uid
                medal  = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i]
                lines.append(f"{medal} **{mname}** — {dmg:,} dmg → 💰 {share:,}")

        # Clear the raid
        data = _load()
        data.pop(gid, None)
        _save(data)

        await ctx.send(embed=discord.Embed(
            title=f"🏆 {boss['name']} DEFEATED!",
            description=(
                f"Final blow by **{killer.display_name}**! ⚔️\n\n"
                f"**Damage Leaderboard**\n" + "\n".join(lines) +
                f"\n\n💰 Pool **{pool:,}** coins + {PARTICIPATION_MIN} participation "
                f"bonus distributed to **{len(ranked)}** blader(s)!"
            ),
            color=discord.Color.gold(),
        ))

    @raid.command(name="status", aliases=["lb", "leaderboard"], brief="Raid boss status")
    async def status(self, ctx: commands.Context) -> None:
        gid  = str(ctx.guild.id)
        boss = _load().get(gid)
        if not boss or boss.get("hp", 0) <= 0:
            return await ctx.send("💤 No active raid boss. An admin can spawn one with `;raid spawn`.")
        ranked = sorted(boss.get("damage", {}).items(), key=lambda kv: kv[1], reverse=True)[:5]
        lb = []
        for i, (uid, dmg) in enumerate(ranked):
            member = ctx.guild.get_member(int(uid))
            mname  = member.display_name if member else uid
            lb.append(f"{['🥇','🥈','🥉','4️⃣','5️⃣'][i]} **{mname}** — {dmg:,} dmg")
        await ctx.send(embed=discord.Embed(
            title=f"🐉 {boss['name']}",
            description=(
                f"`{_hp_bar(boss['hp'], boss['max_hp'])}`\n"
                f"**{boss['hp']:,} / {boss['max_hp']:,} HP**\n\n"
                + ("**Top Damage**\n" + "\n".join(lb) if lb else "*No attacks yet — be first!*")
                + f"\n\n⚔️ `;raid attack` — 60s cooldown"
            ),
            color=discord.Color.dark_red(),
        ))

    @raid.command(name="end", brief="(admin) Cancel the active raid")
    @commands.has_permissions(administrator=True)
    async def end(self, ctx: commands.Context) -> None:
        gid  = str(ctx.guild.id)
        data = _load()
        if gid not in data:
            return await ctx.send("💤 No active raid to end.")
        name = data[gid].get("name", "Boss")
        data.pop(gid, None)
        _save(data)
        await ctx.send(f"🛑 Raid against **{name}** has been cancelled by an admin. No rewards distributed.")

    @spawn.error
    @end.error
    async def admin_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ Only administrators can use this raid command.")
        else:
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RaidCog(bot))
