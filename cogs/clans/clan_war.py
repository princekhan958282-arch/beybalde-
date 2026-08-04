"""
clan_war.py  —  ⚔️ Clan Wars

Clan vs clan, scored over a time window instead of one scripted match. Both
clans stake coins from their treasury, then every battle a member wins during
the window scores a point for their side. Highest score at the deadline takes
the whole pot.

Why this shape rather than a bracket of 1v1s:
  * it reuses the real battle system untouched — no parallel combat path to
    keep in sync with the ability engine
  * it needs the whole clan to show up, not just the two best players
  * it fills the window with ordinary battles, which is exactly the activity
    the bot wants more of

Wars live in data/clan_wars.json.

    ;clan war @Clan <stake>   → declare (owner only)
    ;clan war accept          → accept a declaration (owner only)
    ;clan war decline         → refuse
    ;clan war status          → live scoreboard
    ;clan war history         → past wars
"""

import os
import threading
import time
import uuid
from typing import Optional

import discord
from discord.ext import commands

from utils.database import BASE_DIR, _atomic_write_json, _read_json
from utils.mobile_ui import MobileListView

from . import clan_data as cd

WARS_PATH      = os.path.join(BASE_DIR, "data", "clan_wars.json")
_lock          = threading.Lock()

WAR_DURATION   = 24 * 3600     # seconds a war runs for
DECLARE_TTL    = 6 * 3600      # how long an unanswered declaration stands
MIN_STAKE      = 1_000
POINTS_PER_WIN = 1


def _blank() -> dict:
    return {"wars": {}, "history": []}


def _load() -> dict:
    data = _read_json(WARS_PATH, _blank)
    if not isinstance(data, dict):
        return _blank()
    data.setdefault("wars", {})
    data.setdefault("history", [])
    return data


def _save(data: dict) -> None:
    _atomic_write_json(WARS_PATH, data)


# ── War lookup ────────────────────────────────────────────────────────────────
def war_for_clan(clan_id: str) -> Optional[dict]:
    for w in _load()["wars"].values():
        if clan_id in (w["a"], w["b"]):
            return w
    return None


def _expire(data: dict) -> bool:
    """Drop stale declarations. Returns True if anything changed."""
    now, changed = time.time(), False
    for wid, w in list(data["wars"].items()):
        if w["state"] == "pending" and now > w["declared_at"] + DECLARE_TTL:
            data["wars"].pop(wid)
            changed = True
    return changed


class ClanWarCog(commands.Cog, name="Clan Wars"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Battle hook: every win scores for the winner's clan ──────────────────
    @commands.Cog.listener()
    async def on_beycord_battle_blades(self, result: dict):
        winner = result.get("winner_id")
        if winner is None:
            return
        try:
            clan = cd.clan_of(int(winner))
            if clan is None:
                return
            with _lock:
                data = _load()
                war  = None
                for w in data["wars"].values():
                    if w["state"] == "active" and clan["id"] in (w["a"], w["b"]):
                        war = w
                        break
                if war is None:
                    return
                if time.time() > war["ends_at"]:
                    return
                side = "score_a" if war["a"] == clan["id"] else "score_b"
                war[side] = war.get(side, 0) + POINTS_PER_WIN
                war.setdefault("scorers", {})
                war["scorers"][str(winner)] = war["scorers"].get(str(winner), 0) + 1
                _save(data)
        except Exception:
            pass

    # ── Background: settle wars whose clock ran out ──────────────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        if getattr(self, "_settler", None) is None:
            self._settler = self.bot.loop.create_task(self._settle_loop())

    async def _settle_loop(self):
        import asyncio
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self._settle_due()
            except Exception:
                pass
            await asyncio.sleep(120)

    async def _settle_due(self):
        with _lock:
            data    = _load()
            changed = _expire(data)
            due     = [w for w in data["wars"].values()
                       if w["state"] == "active" and time.time() >= w["ends_at"]]
            for war in due:
                data["wars"].pop(war["id"], None)
                changed = True
            if changed:
                _save(data)

        for war in due:
            await self._payout(war)

    async def _payout(self, war: dict):
        a, b   = cd.get_clan(war["a"]), cd.get_clan(war["b"])
        sa, sb = war.get("score_a", 0), war.get("score_b", 0)
        pot    = war["stake"] * 2

        if sa == sb:
            # Draw — each side gets its stake back
            for cid in (war["a"], war["b"]):
                if cd.get_clan(cid):
                    cd.update_treasury(cid, war["stake"])
            outcome, winner_id = "draw", None
        else:
            winner_id = war["a"] if sa > sb else war["b"]
            if cd.get_clan(winner_id):
                cd.update_treasury(winner_id, pot)
            else:
                # Winning clan disbanded mid-war — refund the loser instead
                loser = war["b"] if winner_id == war["a"] else war["a"]
                if cd.get_clan(loser):
                    cd.update_treasury(loser, war["stake"])
            outcome = "win"

        with _lock:
            data = _load()
            data["history"].insert(0, {
                "a": war["a"], "b": war["b"],
                "a_name": war.get("a_name"), "b_name": war.get("b_name"),
                "score_a": sa, "score_b": sb,
                "stake": war["stake"], "winner": winner_id,
                "ended_at": time.time(),
            })
            data["history"] = data["history"][:50]
            _save(data)

        channel = self.bot.get_channel(war.get("channel_id") or 0)
        if channel is None:
            return

        a_name = (a or {}).get("name") or war.get("a_name") or "Clan A"
        b_name = (b or {}).get("name") or war.get("b_name") or "Clan B"

        if outcome == "draw":
            e = discord.Embed(
                title="🤝  Clan War — Draw!",
                description=(f"**{a_name}** {sa} — {sb} **{b_name}**\n\n"
                             f"Dead even. Both stakes refunded."),
                color=0x95a5a6,
            )
        else:
            wname = a_name if winner_id == war["a"] else b_name
            e = discord.Embed(
                title="🏆  Clan War Over!",
                description=(f"**{a_name}** {sa} — {sb} **{b_name}**\n\n"
                             f"**{wname}** takes the pot of 🪙 **{pot:,}**!"),
                color=0xf1c40f,
            )

        scorers = war.get("scorers") or {}
        if scorers:
            top = sorted(scorers.items(), key=lambda kv: -kv[1])[:5]
            e.add_field(name="Top scorers",
                        value="\n".join(f"<@{u}> — {n} win(s)" for u, n in top),
                        inline=False)
        try:
            await channel.send(embed=e)
        except Exception:
            pass

    # ── Commands ─────────────────────────────────────────────────────────────
    @commands.group(name="clanwar", aliases=["cwar", "war"],
                    invoke_without_command=True)
    async def clanwar(self, ctx: commands.Context):
        """⚔️ Clan war status."""
        await self.war_status(ctx)

    @clanwar.command(name="declare", aliases=["challenge", "start"])
    async def war_declare(self, ctx: commands.Context, target: str = None,
                          stake: int = 0):
        if not target or stake <= 0:
            return await ctx.send(
                "❌ Usage: `;clanwar declare <clan name|tag> <stake>`\n"
                f"Minimum stake: 🪙 {MIN_STAKE:,} from your treasury.")

        mine = cd.clan_of(ctx.author.id)
        if mine is None:
            return await ctx.send("❌ You're not in a clan.")
        if mine["owner_id"] != ctx.author.id:
            return await ctx.send("❌ Only the clan owner can declare war.")

        theirs = cd.find_by_name(target)
        if theirs is None:
            return await ctx.send(f"❌ No clan called **{target}**.")
        if theirs["id"] == mine["id"]:
            return await ctx.send("❌ You can't war your own clan.")
        if stake < MIN_STAKE:
            return await ctx.send(f"❌ Minimum stake is 🪙 {MIN_STAKE:,}.")
        if mine.get("treasury", 0) < stake:
            return await ctx.send(
                f"❌ Your treasury only has 🪙 {mine.get('treasury', 0):,}. "
                f"`;clan deposit <amount>` to top it up.")
        if theirs.get("treasury", 0) < stake:
            return await ctx.send(
                f"❌ **{theirs['name']}** only has 🪙 {theirs.get('treasury', 0):,} "
                f"— they can't match that stake.")

        if war_for_clan(mine["id"]):
            return await ctx.send("❌ Your clan already has a war pending or active.")
        if war_for_clan(theirs["id"]):
            return await ctx.send(f"❌ **{theirs['name']}** is already in a war.")

        # _lock is a threading.Lock — nothing may be awaited while it is held,
        # or the coroutine parks with the lock taken and the next war command
        # blocks the event loop on it. Decide here, reply after.
        error = None
        war   = None
        with _lock:
            data = _load()
            _expire(data)
            war = next((w for w in data["wars"].values()
                        if w["state"] == "pending" and w["b"] == mine["id"]), None)
            if war is None:
                _save(data)
                error = "❌ No pending war declaration against your clan."
            else:
                a = cd.get_clan(war["a"])
                if a is None:
                    data["wars"].pop(war["id"], None)
                    _save(data)
                    error = "❌ The challenging clan no longer exists."
                else:
                    stake = war["stake"]
                    if a.get("treasury", 0) < stake or mine.get("treasury", 0) < stake:
                        data["wars"].pop(war["id"], None)
                        _save(data)
                        error = ("❌ One of the treasuries dropped below the "
                                 "stake — war cancelled.")
                    else:
                        # Escrow both stakes before flipping the war live
                        ok_a, _ = cd.update_treasury(war["a"], -stake)
                        if not ok_a:
                            data["wars"].pop(war["id"], None)
                            _save(data)
                            error = "❌ Couldn't take the challenger's stake."
                        else:
                            ok_b, _ = cd.update_treasury(mine["id"], -stake)
                            if not ok_b:
                                cd.update_treasury(war["a"], stake)   # roll back
                                data["wars"].pop(war["id"], None)
                                _save(data)
                                error = "❌ Couldn't take your stake — war cancelled."
                            else:
                                war["state"]      = "active"
                                war["ends_at"]    = time.time() + WAR_DURATION
                                war["channel_id"] = ctx.channel.id
                                _save(data)

        if error:
            return await ctx.send(error)

        e = discord.Embed(
            title="⚔️  WAR IS ON!",
            description=(
                f"**{war['a_name']}**  vs  **{war['b_name']}**\n\n"
                f"Pot: 🪙 **{war['stake'] * 2:,}**\n"
                f"Ends: <t:{int(war['ends_at'])}:R>\n\n"
                f"**Every battle your members win scores a point.** "
                f"Get everyone fighting."
            ),
            color=0xe74c3c,
        )
        await ctx.send(embed=e)

    @clanwar.command(name="decline", aliases=["reject"])
    async def war_decline(self, ctx: commands.Context):
        mine = cd.clan_of(ctx.author.id)
        if mine is None:
            return await ctx.send("❌ You're not in a clan.")
        if mine["owner_id"] != ctx.author.id:
            return await ctx.send("❌ Only the clan owner can decline.")
        with _lock:
            data = _load()
            war = next((w for w in data["wars"].values()
                        if w["state"] == "pending" and w["b"] == mine["id"]), None)
            if war is not None:
                data["wars"].pop(war["id"], None)
                _save(data)
        if war is None:
            return await ctx.send("❌ Nothing to decline.")
        await ctx.send(f"🕊️ Declined the war from **{war['a_name']}**.")

    @clanwar.command(name="status", aliases=["score"])
    async def war_status(self, ctx: commands.Context):
        mine = cd.clan_of(ctx.author.id)
        if mine is None:
            return await ctx.send("❌ You're not in a clan. `;clan list` to find one.")

        war = war_for_clan(mine["id"])
        if war is None:
            return await ctx.send(
                "🕊️ No active war.\n"
                f"`;clanwar declare <clan> <stake>` to start one "
                f"(min 🪙 {MIN_STAKE:,} from the treasury).")

        if war["state"] == "pending":
            other = war["b"] if war["a"] == mine["id"] else war["a"]
            side  = "You declared war on" if war["a"] == mine["id"] else "War was declared on you by"
            other_clan = cd.get_clan(other)
            return await ctx.send(
                f"⏳ **Pending.** {side} "
                f"**{(other_clan or {}).get('name', 'a clan')}** "
                f"for 🪙 {war['stake']:,}.\n"
                f"Waiting on the defender to `;clanwar accept`.")

        sa, sb = war.get("score_a", 0), war.get("score_b", 0)
        lead = ("🟢 You're ahead" if
                (sa > sb) == (war["a"] == mine["id"]) and sa != sb
                else ("🤝 Level" if sa == sb else "🔴 You're behind"))

        e = discord.Embed(
            title="⚔️  Clan War",
            description=(f"**{war['a_name']}**  `{sa}` — `{sb}`  **{war['b_name']}**\n\n"
                         f"{lead}\nEnds <t:{int(war['ends_at'])}:R>"),
            color=0xe74c3c,
        )
        e.add_field(name="Pot", value=f"🪙 {war['stake'] * 2:,}", inline=True)
        scorers = war.get("scorers") or {}
        if scorers:
            top = sorted(scorers.items(), key=lambda kv: -kv[1])[:3]
            e.add_field(name="Top scorers",
                        value="\n".join(f"<@{u}> — {n}" for u, n in top),
                        inline=False)
        e.set_footer(text="Every battle win by a member scores a point")
        await ctx.send(embed=e)

    @clanwar.command(name="history", aliases=["past"])
    async def war_history(self, ctx: commands.Context):
        hist = _load()["history"]
        if not hist:
            return await ctx.send("No wars have been fought yet.")

        def render(h, idx):
            wname = ("🤝 Draw" if h["winner"] is None else
                     "🏆 " + (h["a_name"] if h["winner"] == h["a"] else h["b_name"]))
            return (f"**{idx + 1}.** {h['a_name']} `{h['score_a']}—{h['score_b']}` "
                    f"{h['b_name']}\n　{wname} · 🪙 {h['stake'] * 2:,}")

        view = MobileListView(
            owner=ctx.author,
            title="📜  Clan War History",
            items=hist,
            render=render,
            colour=0x9b59b6,
            footer="most recent first",
        )
        view.message = await ctx.send(embed=view.embed(), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(ClanWarCog(bot))
