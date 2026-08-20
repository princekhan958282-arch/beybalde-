"""
story_cog.py — the School League's Discord surface.

    ;story                  the chapter picker  (alias: ;league)
    ;story <n> [difficulty] fight one battle
    ;storymap               the eight battles and your progress
    /story                  the same picker, from a slash command

The battle itself is `story_match.LeagueMatch`, which runs real
`BattleSession` rounds. This file owns the picker, the gate and the payout —
and nothing about combat, which is the point.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from utils import bey_levels as BL
from utils.database import get_user, grant_xp, update_user

from . import story_data as SD
from .story_match import LeagueMatch

log = logging.getLogger("beyblade_bot.story")

# Story wins feed Story alone. `beycord_battle_end` and `beycord_battle_blades`
# drive quests, mastery, achievements and clan wars, and a PvE round that fired
# them would let all four be farmed against an opponent that never gets tired.
# `BattleSession(payout=False)` is what keeps them silent — this note is here so
# nobody concludes the events were merely forgotten.
DISPATCH_BATTLE_EVENTS = False

# Trainer EXP for taking a battle. Small and flat: the League's reward is the
# coins, and the EXP Surge multiplies whatever goes through `grant_xp`.
LEAGUE_XP = {SD.NORMAL: 120, SD.NIGHTMARE: 300}
LEAGUE_BEY_XP = {SD.NORMAL: 220, SD.NIGHTMARE: 550}


def _opponent_blade(name: str) -> Optional[tuple[dict, int]]:
    """The League's copy of a blade, at `SD.OPPONENT_LEVEL`.

    Returns `(blade, hp_gain)`. The HP gain is handed to the session separately
    because `max_hp_for_blade` clamps the printed HP stat back into the blade's
    type band — right for a printed stat, and it would otherwise throw away
    every point of the levelling this function just did.
    """
    from utils.database import get_beyblade
    base = get_beyblade(name)
    if not base:
        return None
    blade = dict(base)
    printed = dict(base.get("stats") or {})
    blade["stats"] = BL.stats_at(base, SD.OPPONENT_LEVEL, {})
    gain = int(blade["stats"].get("hp", 0)) - int(printed.get("hp", 0))
    return blade, max(0, gain)


def _state_icon(profile: dict, n: int, difficulty: str) -> str:
    if n in SD.cleared(profile, difficulty):
        return "✅"
    return "▶️" if SD.is_unlocked(profile, n, difficulty) else "🔒"


class ChapterSelect(discord.ui.Select):
    """🏫 Beyblade Burst School · 🔒 Xender Dojo — Coming Soon."""

    def __init__(self, cog: "StoryCog", user: discord.Member) -> None:
        opts = []
        for ch in SD.CHAPTERS:
            opts.append(discord.SelectOption(
                label=ch["name"][:100],
                value=ch["key"],
                description=ch["blurb"][:100] or None,
                emoji=ch["emoji"]))
        super().__init__(placeholder="Where are you fighting?", options=opts)
        self.cog = cog
        self.user = user

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(
                "That isn't your menu — run `;story` yourself.", ephemeral=True)
        key = self.values[0]
        chapter = next((c for c in SD.CHAPTERS if c["key"] == key), None)
        if chapter is None or not chapter.get("available"):
            return await interaction.response.send_message(
                "🔒 The Xender Dojo isn't open yet.", ephemeral=True)
        view = LeagueView(self.cog, self.user)
        await interaction.response.edit_message(
            embed=view.embed(), view=view)


class DifficultyButton(discord.ui.Button):
    def __init__(self, view: "LeagueView", difficulty: str) -> None:
        emoji, label = SD.DIFFICULTY_LABEL[difficulty]
        current = view.difficulty == difficulty
        super().__init__(
            label=label, emoji=emoji, row=1,
            style=(discord.ButtonStyle.success if current
                   else discord.ButtonStyle.secondary),
            disabled=current)
        self.parent = view
        self.difficulty = difficulty

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.parent.user.id:
            return await interaction.response.send_message(
                "That isn't your menu.", ephemeral=True)
        self.parent.difficulty = self.difficulty
        self.parent.build()
        await interaction.response.edit_message(
            embed=self.parent.embed(), view=self.parent)


class BattleSelect(discord.ui.Select):
    def __init__(self, view: "LeagueView", profile: dict) -> None:
        opts = []
        for b in SD.SCHOOL_LEAGUE:
            n = b["n"]
            icon = _state_icon(profile, n, view.difficulty)
            reward = SD.reward_for(n, view.difficulty)
            done = n in SD.cleared(profile, view.difficulty)
            desc = ("Cleared — replay pays nothing" if done
                    else f"🪙 {reward:,}")
            opts.append(discord.SelectOption(
                label=f"{icon} {n}. {b['blade']}"[:100],
                value=str(n),
                description=desc[:100]))
        super().__init__(placeholder="Which battle?", options=opts[:25], row=0)
        self.parent = view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.parent.user.id:
            return await interaction.response.send_message(
                "That isn't your menu.", ephemeral=True)
        n = int(self.values[0])
        await self.parent.cog.launch(interaction, self.parent.user, n,
                                     self.parent.difficulty)


class LeagueView(discord.ui.View):
    def __init__(self, cog: "StoryCog", user: discord.Member,
                 difficulty: str = SD.NORMAL) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user = user
        self.difficulty = difficulty
        self.build()

    def build(self) -> None:
        self.clear_items()
        profile = get_user(self.user.id)
        self.add_item(BattleSelect(self, profile))
        for d in SD.DIFFICULTIES:
            self.add_item(DifficultyButton(self, d))

    def embed(self) -> discord.Embed:
        profile = get_user(self.user.id)
        emoji, label = SD.DIFFICULTY_LABEL[self.difficulty]
        done = len(SD.cleared(profile, self.difficulty))
        e = discord.Embed(
            title="🏫  School League",
            description=(f"{emoji} **{label}** — {done}/{SD.total_battles()} "
                         f"cleared\n"
                         f"Every battle is first to **{SD.VICTORY_TARGET}** "
                         f"Victory Points."),
            colour=(0xED4245 if self.difficulty == SD.NIGHTMARE else 0x3498DB))
        lines = []
        for b in SD.SCHOOL_LEAGUE:
            n = b["n"]
            icon = _state_icon(profile, n, self.difficulty)
            reward = SD.reward_for(n, self.difficulty)
            lines.append(f"{icon} **{n}.** {b['blade']} — 🪙 {reward:,}")
        e.add_field(name="Battles", value="\n".join(lines), inline=False)
        if self.difficulty == SD.NIGHTMARE and not SD.normal_complete(profile):
            e.add_field(
                name="🔒 Locked",
                value=SD.lock_reason(profile, 1, SD.NIGHTMARE), inline=False)
        else:
            rung = SD.AI_RUNG[self.difficulty]
            from cogs.battle.boss import boss_ai as ai
            iq = ai.DIFFICULTY[rung]["iq"]
            e.set_footer(text=f"Every opponent is level {SD.OPPONENT_LEVEL} · "
                              f"Boss IQ {iq} ({ai.IQ_LABELS.get(iq, '?')})")
        return e


class StoryCog(commands.Cog, name="Story Mode"):
    """The School League."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._active: set[int] = set()

    def release(self, user_id: int) -> None:
        self._active.discard(int(user_id))

    def picker(self, user) -> "ChapterPickView":
        """The chapter picker, for `/story` as well as `;story`.

        `cogs/ui/panels.py` calls this rather than importing the view, so the
        slash command degrades to the generic panel if this cog is unloaded
        instead of failing to import.
        """
        return ChapterPickView(self, user)

    # ── gate ─────────────────────────────────────────────────────────────────
    def _can_fight(self, user_id: int, n: int, difficulty: str) -> tuple[bool, str]:
        if int(user_id) in self._active:
            return False, "You're already in a School League battle."
        profile = get_user(user_id)
        if not SD.is_unlocked(profile, n, difficulty):
            return False, SD.lock_reason(profile, n, difficulty)
        if not profile.get("active_beyblade"):
            return False, ("You need a Beyblade equipped — `;equip <name>`.")
        return True, ""

    # ── entry points ─────────────────────────────────────────────────────────
    async def launch(self, interaction: discord.Interaction,
                     player: discord.Member, n: int, difficulty: str) -> None:
        ok, why = self._can_fight(player.id, n, difficulty)
        if not ok:
            return await interaction.response.send_message(why, ephemeral=True)
        await interaction.response.defer()
        await self._fight(interaction.channel, player, n, difficulty)

    async def _fight(self, channel, player, n: int, difficulty: str) -> None:
        entry = SD.battle(n)
        if entry is None:
            return
        built = _opponent_blade(entry["blade"])
        if built is None:
            await channel.send(f"⚠️ **{entry['blade']}** isn't in the roster.")
            return
        opponent_blade, hp_gain = built

        from cogs.battle.boss import boss_copy as bcopy
        from utils.loadout import bey_level_and_stats
        profile = get_user(player.id)
        blade_raw, copy = bcopy.equipped_blade(player.id)
        if not blade_raw:
            await channel.send("❌ You need a Beyblade equipped.")
            return
        try:
            blade, _lvl = bey_level_and_stats(player.id, profile, dict(blade_raw))
        except Exception:                                # noqa: BLE001
            blade = dict(blade_raw)

        self._active.add(int(player.id))
        try:
            emoji, label = SD.DIFFICULTY_LABEL[difficulty]
            await channel.send(embed=discord.Embed(
                title=f"🏫 Battle {n} — {entry['blade']}",
                description=(f"{entry['blurb']}\n\n"
                             f"{emoji} **{label}** · Level "
                             f"{SD.OPPONENT_LEVEL}\n"
                             f"First to **{SD.VICTORY_TARGET}** Victory "
                             f"Points takes the battle."),
                colour=(0xED4245 if difficulty == SD.NIGHTMARE else 0x3498DB)))

            match = LeagueMatch(self.bot, channel, player, blade, n, difficulty)
            won = await match.run(opponent_blade, hp_gain=hp_gain)
            await self._finish(channel, player, match, won, difficulty, copy)
        except Exception:                                # noqa: BLE001
            log.exception("[story] battle %s failed", n)
            try:
                await channel.send("⚠️ That battle ended unexpectedly. "
                                   "Nothing was charged.")
            except Exception:                            # noqa: BLE001
                pass
        finally:
            self.release(player.id)

    # ── payout ───────────────────────────────────────────────────────────────
    async def _finish(self, channel, player, match: LeagueMatch, won: bool,
                      difficulty: str, copy) -> None:
        coins = 0
        first = False
        if won:
            profile = get_user(player.id)
            first = SD.record_clear(profile, difficulty, match.battle_no)
            if first:
                coins = SD.reward_for(match.battle_no, difficulty)
                profile["coins"] = int(profile.get("coins", 0)) + coins
            # Blade EXP is written into the profile already in hand and
            # persisted below; `grant_xp` re-reads the profile, so it has to
            # come after the write or the two clobber each other.
            if not copy:
                try:
                    BL.award(profile, match.blade.get("name"),
                             LEAGUE_BEY_XP.get(difficulty, 0))
                except Exception:                        # noqa: BLE001
                    pass
            update_user(player.id, profile)
            try:
                grant_xp(player.id, LEAGUE_XP.get(difficulty, 0))
            except Exception:                            # noqa: BLE001
                pass

        try:
            await channel.send(embed=match.result_embed(coins, first))
        except Exception:                                # noqa: BLE001
            log.exception("[story] could not post the League result card")

    # ── commands ─────────────────────────────────────────────────────────────
    @commands.command(name="story", aliases=["league", "campaign"],
                      brief="School League 🏫")
    async def story(self, ctx: commands.Context, *, args: str = "") -> None:
        """`;story` for the picker, `;story 3` or `;story 3 nightmare`."""
        parts = (args or "").split()
        difficulty = SD.NORMAL
        if parts and parts[-1].lower() in (SD.NIGHTMARE, "nm", "hard"):
            difficulty = SD.NIGHTMARE
            parts = parts[:-1]

        if not parts:
            view = ChapterPickView(self, ctx.author)
            return await ctx.send(embed=view.embed(), view=view)

        try:
            n = int(parts[0])
        except ValueError:
            return await ctx.send(
                f"Pick a battle number, 1–{SD.total_battles()} — "
                f"`;story 3` or `;story 3 nightmare`.")

        ok, why = self._can_fight(ctx.author.id, n, difficulty)
        if not ok:
            return await ctx.send(why)
        await self._fight(ctx.channel, ctx.author, n, difficulty)

    @commands.command(name="storymap", aliases=["leaguemap", "storylist"],
                      brief="Your School League progress 🗺️")
    async def storymap(self, ctx: commands.Context) -> None:
        view = LeagueView(self, ctx.author)
        await ctx.send(embed=view.embed(), view=view)

    @commands.command(name="storyinfo", aliases=["battleinfo"],
                      brief="One League battle up close 🔎")
    async def storyinfo(self, ctx: commands.Context, n: str = "1",
                        difficulty: str = SD.NORMAL) -> None:
        # `n` is a string, not an int: the panel binds a free-text box to it,
        # and a typo there should read as "no such battle" rather than a
        # converter error with no output at all.
        difficulty = (SD.NIGHTMARE if str(difficulty).lower().startswith("n")
                      else SD.NORMAL)
        entry = SD.battle(n)
        if entry is None:
            return await ctx.send(
                f"There are {SD.total_battles()} battles in the League — "
                f"`;storyinfo 3` or `;storyinfo 3 nightmare`.")
        n = int(entry["n"])
        built = _opponent_blade(entry["blade"])
        profile = get_user(ctx.author.id)
        emoji, label = SD.DIFFICULTY_LABEL[difficulty]
        e = discord.Embed(
            title=f"🏫 Battle {n} — {entry['blade']}",
            description=entry["blurb"],
            colour=(0xED4245 if difficulty == SD.NIGHTMARE else 0x3498DB))
        if built:
            s = built[0]["stats"]
            e.add_field(name=f"Level {SD.OPPONENT_LEVEL}",
                        value=(f"ATK {s.get('attack', 0)} · "
                               f"DEF {s.get('defense', 0)} · "
                               f"STA {s.get('stamina', 0)} · "
                               f"HP {s.get('hp', 0)}"), inline=False)
        from cogs.battle.boss import boss_ai as ai
        iq = ai.DIFFICULTY[SD.AI_RUNG[difficulty]]["iq"]
        e.add_field(name="Mode",
                    value=f"{emoji} {label} · Boss IQ {iq} "
                          f"({ai.IQ_LABELS.get(iq, '?')})", inline=True)
        e.add_field(name="Reward",
                    value=f"🪙 {SD.reward_for(n, difficulty):,} (first clear)",
                    inline=True)
        e.add_field(name="Status",
                    value=(SD.lock_reason(profile, n, difficulty)
                           or ("✅ Cleared" if n in SD.cleared(profile, difficulty)
                               else "▶️ Open")), inline=False)
        await ctx.send(embed=e)

    @commands.command(name="storystats", aliases=["leaguestats"],
                      brief="Your School League record 📊")
    async def storystats(self, ctx: commands.Context,
                         member: Optional[discord.Member] = None) -> None:
        target = member or ctx.author
        profile = get_user(target.id)
        e = discord.Embed(title=f"🏫 {target.display_name} — School League",
                          colour=0x3498DB)
        for d in SD.DIFFICULTIES:
            emoji, label = SD.DIFFICULTY_LABEL[d]
            done = len(SD.cleared(profile, d))
            nxt = SD.next_battle(profile, d)
            entry = SD.battle(nxt) if nxt else None
            up = (f"Up next: **{nxt} · {entry['blade']}**" if entry
                  else "🏆 Complete")
            if d == SD.NIGHTMARE and not SD.normal_complete(profile):
                up = "🔒 Clear the League on Normal first"
            e.add_field(
                name=f"{emoji} {label}",
                value=f"{done}/{SD.total_battles()} cleared\n{up}",
                inline=True)
        await ctx.send(embed=e)


class ChapterPickView(discord.ui.View):
    def __init__(self, cog: StoryCog, user: discord.Member) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user = user
        self.add_item(ChapterSelect(cog, user))

    def embed(self) -> discord.Embed:
        e = discord.Embed(
            title="📖  Story Mode",
            description="Where are you fighting?",
            colour=0xE67E22)
        for ch in SD.CHAPTERS:
            e.add_field(name=f"{ch['emoji']} {ch['name']}",
                        value=ch["blurb"], inline=False)
        return e


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StoryCog(bot))
