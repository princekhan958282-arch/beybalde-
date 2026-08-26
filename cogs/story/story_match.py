"""
story_match.py — a School League battle: first to 3 Victory Points.

A League battle is not one exchange. It is a series of rounds, each a full
`BattleSession`, and how each round SCORES is the ranked ladder's own rule:

    burst      the opponent's HP reached 0            2 points
    survival   the opponent ran out of stamina        1 point
    ring-out   the opponent's stability reached 0     1 point

That table is `utils/ranked.py` and it is imported rather than copied — it is
"the existing PvP matchup/result system deciding how each point is awarded",
which is what was asked for. The TARGET is Story's own constant, because the
two agreeing at 3 today is not a reason for a change to the ranked ladder to
silently retune the League.

This is deliberately the same shape as `BattleCog._ranked_rounds`: a fresh
session per round — full HP, full stamina, a clean stability bar — with avatar
skill energy committed once for the whole battle rather than once per round.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord

from cogs.battle.session import BattleSession
from utils import ranked as RK

from . import story_data as SD
from .story_ai import LeagueOpponent

log = logging.getLogger("beyblade_bot.story.match")


class NPCFighter:
    """The opponent as far as `BattleSession` is concerned.

    The session reads exactly two things off a player object — `.id` and
    `.display_name` — and every profile lookup for this side is routed around
    the store by `BattleSession._is_npc`. So this is the whole of it.

    The id is a small fixed integer per battle rather than a random one: it
    never reaches the database, and a stable value keeps logs readable.
    """

    __slots__ = ("id", "display_name", "bot", "mention")

    def __init__(self, battle_no: int, blade_name: str) -> None:
        # Deliberately not a plausible snowflake. If this ever DID reach the
        # store, "3" is obviously wrong in a way that "849213..." would not be.
        self.id = int(battle_no)
        self.display_name = str(blade_name)
        self.bot = False
        self.mention = f"**{blade_name}**"


class LeagueMatch:
    """One League battle, run to `SD.VICTORY_TARGET` points."""

    def __init__(self, bot, channel, player, blade: dict,
                 battle_no: int, difficulty: str) -> None:
        self.bot = bot
        self.channel = channel
        self.player = player
        self.blade = blade
        self.battle_no = int(battle_no)
        self.difficulty = difficulty
        self.entry = SD.battle(battle_no) or {}
        self.points = {"player": 0, "npc": 0}
        self.history: list[str] = []
        self.rounds = 0
        self.won: Optional[bool] = None

    # ── result ───────────────────────────────────────────────────────────────
    @property
    def player_points(self) -> int:
        return self.points["player"]

    @property
    def npc_points(self) -> int:
        return self.points["npc"]

    def decided(self) -> bool:
        return max(self.points.values()) >= SD.VICTORY_TARGET

    def scoreline(self) -> str:
        return (f"**{self.player.display_name}** {self.player_points} — "
                f"{self.npc_points} **{self.entry.get('blade', 'Opponent')}**")

    # ── the loop ─────────────────────────────────────────────────────────────
    async def run(self, opponent_blade: dict, hp_gain: int = 0) -> bool:
        """Run rounds until someone reaches the target. Returns True on a win."""
        from cogs.avatar import avatar_skills as AS

        rung = SD.AI_RUNG.get(self.difficulty, "elite")
        npc_member = NPCFighter(self.battle_no, opponent_blade.get("name", "?"))
        controller = LeagueOpponent(
            npc_member, opponent_blade, difficulty=rung,
            level=SD.OPPONENT_LEVEL, hp_gain=hp_gain,
            avatar_id=self.entry.get("avatar"))

        try:
            await self._rounds(npc_member, controller, opponent_blade)
        finally:
            # Energy is spent for the BATTLE, not for each round — the same
            # lifecycle a ranked match uses. Without this the pool would be
            # charged up to five times for one League battle.
            try:
                await AS.end_match_for(int(self.player.id))
            except Exception:                            # noqa: BLE001
                pass

        self.won = self.player_points > self.npc_points
        return bool(self.won)

    async def _rounds(self, npc_member, controller, opponent_blade) -> None:
        pkey = str(self.player.id)
        nkey = str(npc_member.id)

        while not self.decided() and self.rounds < SD.MAX_ROUNDS:
            self.rounds += 1
            await self.channel.send(embed=discord.Embed(
                title=f"🏫 Battle {self.battle_no} — Round {self.rounds}",
                description=(f"{self.scoreline()}\n"
                             f"First to **{SD.VICTORY_TARGET}** points wins."),
                colour=0xF1C40F))

            session = await BattleSession.create(
                bot=self.bot, channel=self.channel,
                p1=self.player, p2=npc_member,
                blade1=self.blade, blade2=opponent_blade,
                ranked=False,
                # The three PvE hooks. `payout=False` is what stops the NPC
                # being paid and written to the store, and stops the player
                # collecting PvP battle coins on top of the League reward.
                npc_controller=controller,
                payout=False,
                spend_energy=True,
                # The running score, so a skill can read "behind on points".
                # None in PvP, where there is no score to be behind on.
                victory_points={pkey: self.points["player"],
                                nkey: self.points["npc"]},
            )
            await session.run()

            winner_id = getattr(session, "winner_id", None)
            if not winner_id:
                self.history.append(f"R{self.rounds}: 🤝 draw — no points")
                continue

            loser_key = nkey if str(winner_id) == pkey else pkey
            kind = session.finish_for(loser_key)
            gained = RK.finish_points(kind)
            side = "player" if str(winner_id) == pkey else "npc"
            self.points[side] += gained

            # `finish_label` returns one formatted string ("💥 Burst Finish"),
            # not a pair — the emoji/name split lives in `RK.FINISH_LABEL`.
            label = RK.finish_label(kind)
            who = (self.player.display_name if side == "player"
                   else npc_member.display_name)
            self.history.append(
                f"R{self.rounds}: {label} — **{who}** +{gained}")

    # ── the card ─────────────────────────────────────────────────────────────
    def result_embed(self, coins: int, first: bool,
                     card: Optional[dict] = None) -> discord.Embed:
        won = bool(self.won)
        e = discord.Embed(
            title=("🏆 VICTORY" if won else "💀 DEFEAT"),
            description=(self.scoreline() + "\n\n" + "\n".join(self.history)),
            colour=(0x2ECC71 if won else 0xED4245))
        emoji, label = SD.DIFFICULTY_LABEL.get(self.difficulty, ("", "?"))
        e.add_field(name="Battle",
                    value=f"{emoji} {label} · #{self.battle_no} "
                          f"{self.entry.get('blade', '?')}", inline=True)
        if won:
            if first and coins:
                e.add_field(name="Reward", value=f"🪙 **{coins:,}**", inline=True)
            else:
                e.add_field(name="Reward",
                            value="*already cleared — replays pay nothing*",
                            inline=True)
        if card:
            # The League is finished. One blader joins the player's collection,
            # once ever — the same card the opponents fight as.
            e.add_field(
                name="🏫 School League complete",
                value=(f"**{card.get('name', card.get('id', '?'))}** joins your "
                       f"collection!\n`;equipavatar {card.get('id', '')}` to "
                       f"take them into a battle."),
                inline=False)
            if card.get("image"):
                e.set_thumbnail(url=card["image"])
        return e
