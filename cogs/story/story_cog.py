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

import asyncio
import logging
import random
from typing import Optional

import discord
from discord.ext import commands

from utils import bey_levels as BL
from utils.database import (
    add_avatar_to_inventory, claim_once, get_user, grant_xp, release_claim,
    update_user,
)

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


async def player_blade(user_id: int) -> Optional[tuple[dict, Optional[dict]]]:
    """The blade this player fights the League with, plus its copy instance.

    Returns `(blade, copy)`, or None when they have nothing equipped.

    THE BUG THIS REPLACES, because it cost four releases of players their
    levels. This was:

        blade, _lvl = bey_level_and_stats(player.id, profile, dict(blade_raw))

    and `utils.loadout.bey_level_and_stats(profile, blade)` takes TWO arguments
    and returns `(level, stats)` — wrong arity, and unpacked backwards. It
    raised `TypeError` every single time, a bare `except Exception` two lines
    below swallowed it, and the fallback handed the battle the blade's PRINTED
    stats. A level-50 Storm Spriggan fought at 74/78/78 instead of 192/196/196,
    against an opponent at level 100.

    It read as three separate bugs because it IS internally inconsistent:
    `BattleSession` levels HP and the Special stat on its own paths
    (`_level_hp_gain`, `_effective_special`), so those were right while attack,
    defence and stamina were not — full HP, a quarter of the stats, an empty
    stamina bar (the bar is derived from the stamina stat) and a ring-out every
    time the matchup was lost.

    `battle._apply_parts` is the shared entry point PvP (`battle.py:307`) and
    the tournament (`tournament.py:665`) already use, and two suites pin its
    behaviour. It resolves the spin mode first — which Story also never did, so
    Master Diabolos, Janus Bahamut and Cho-Z Achilles fought in the wrong form
    — then applies bey levels, and deliberately leaves HP and Special printed
    because the session levels those itself. Exactly the half Story was
    missing.

    No `try` around it. Swallowing is what turned a hard error into a silent
    quarter-strength nerf that nobody could see.
    """
    from cogs.battle.battle import _apply_parts
    from cogs.battle.boss import boss_copy as bcopy

    blade_raw, copy = await bcopy.equipped_blade(user_id)
    if not blade_raw:
        return None
    # A boss copy arrives already resolved and levelled — levelling it a second
    # time would double its growth. Same arm as `battle.py:307`.
    blade = dict(blade_raw) if copy else _apply_parts(dict(blade_raw),
                                                      await get_user(user_id))
    return blade, copy


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
        # ACK FIRST. Building the League view reads the player's profile, and
        # on a remote store that read can outlast Discord's three-second
        # interaction deadline — which is exactly what "BEYCBOT didn't respond
        # in time" is. A component `defer()` is a type-6 deferred message
        # update: it acknowledges without showing a spinner and leaves the
        # message editable for as long as the work takes.
        await interaction.response.defer()
        view = await LeagueView.create(self.cog, self.user)
        await interaction.edit_original_response(embed=view.embed(), view=view)


class DifficultyButton(discord.ui.Button):
    def __init__(self, view: "LeagueView", difficulty: str) -> None:
        emoji, label = SD.DIFFICULTY_LABEL[difficulty]
        current = view.difficulty == difficulty
        super().__init__(
            label=label, emoji=emoji, row=1,
            style=(discord.ButtonStyle.success if current
                   else discord.ButtonStyle.secondary),
            disabled=current)
        self.panel = view
        self.difficulty = difficulty

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.panel.user.id:
            return await interaction.response.send_message(
                "That isn't your menu.", ephemeral=True)
        await interaction.response.defer()          # ack before the store read
        self.panel.difficulty = self.difficulty
        await self.panel.refresh()
        await interaction.edit_original_response(
            embed=self.panel.embed(), view=self.panel)


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
        self.panel = view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.panel.user.id:
            return await interaction.response.send_message(
                "That isn't your menu.", ephemeral=True)
        n = int(self.values[0])
        await self.panel.cog.launch(interaction, self.panel.user, n,
                                     self.panel.difficulty)


class LeagueView(discord.ui.View):
    def __init__(self, cog: "StoryCog", user: discord.Member,
                 difficulty: str = SD.NORMAL) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user = user
        self.difficulty = difficulty
        # One profile read per render, held here. `build()` and `embed()` both
        # need it, and fetching it twice doubled the store latency on the exact
        # path that was blowing the interaction deadline.
        #
        # `get_user` is `async def` now (BUG-02) and `__init__` cannot await —
        # so construction starts with an EMPTY profile (built once here so
        # `self.children` is never left unset) and every real call site must
        # use `await LeagueView.create(...)` instead of `LeagueView(...)`,
        # which immediately calls `refresh()` to populate the real profile.
        self.profile: dict = {}
        self.build()

    @classmethod
    async def create(cls, cog: "StoryCog", user: discord.Member,
                     difficulty: str = SD.NORMAL) -> "LeagueView":
        view = cls(cog, user, difficulty)
        await view.refresh()
        return view

    async def refresh(self) -> None:
        """Re-read the profile and rebuild the components."""
        self.profile = await get_user(self.user.id)
        self.build()

    def build(self) -> None:
        self.clear_items()
        self.add_item(BattleSelect(self, self.profile))
        for d in SD.DIFFICULTIES:
            self.add_item(DifficultyButton(self, d))

    def embed(self) -> discord.Embed:
        profile = self.profile
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
    async def _can_fight(self, user_id: int, n: int, difficulty: str) -> tuple[bool, str]:
        if int(user_id) in self._active:
            return False, "You're already in a School League battle."
        profile = await get_user(user_id)
        if not SD.is_unlocked(profile, n, difficulty):
            return False, SD.lock_reason(profile, n, difficulty)
        from cogs.battle.boss.boss_copy import has_equipped_blade
        if not await has_equipped_blade(user_id):
            return False, ("You need a Beyblade equipped — `;equip <name>`.")
        return True, ""

    # ── entry points ─────────────────────────────────────────────────────────
    async def launch(self, interaction: discord.Interaction,
                     player: discord.Member, n: int, difficulty: str) -> None:
        # Ack first: `_can_fight` reads the profile, and the deadline is three
        # seconds from the click, not from the first await.
        await interaction.response.defer()
        ok, why = await self._can_fight(player.id, n, difficulty)
        if not ok:
            return await interaction.followup.send(why, ephemeral=True)
        # The battle is minutes long. Run it as its own task so this component
        # callback returns immediately and the League panel stays responsive —
        # a callback that blocks for the length of a fight is a panel that
        # looks broken to anyone who touches it meanwhile.
        asyncio.get_running_loop().create_task(
            self._fight(interaction.channel, player, n, difficulty))

    async def _fight(self, channel, player, n: int, difficulty: str) -> None:
        entry = SD.battle(n)
        if entry is None:
            return
        built = _opponent_blade(entry["blade"])
        if built is None:
            await channel.send(f"⚠️ **{entry['blade']}** isn't in the roster.")
            return
        opponent_blade, hp_gain = built

        built_blade = await player_blade(player.id)
        if built_blade is None:
            await channel.send("❌ You need a Beyblade equipped.")
            return
        blade, copy = built_blade

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

            # Which avatar skill are you taking in? Once per LEAGUE BATTLE,
            # not once per round — a battle is up to nine sessions and the
            # energy budget is match-long, the same rule `;battle` follows.
            #
            # The solo entry point, deliberately: the two-player one resolves
            # both sides through `get_user`, which would create a profile for
            # the NPC opponent.
            try:
                from cogs.battle.skill_prompt import resolve_avatar_skills_solo
                await resolve_avatar_skills_solo(channel, player)
            except Exception:                            # noqa: BLE001
                log.exception("[story] skill prompt failed for %s", player.id)

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
    def _award_blader(self, user_id: int) -> Optional[dict]:
        """One random blader card for finishing the League. Once per player, ever.

        Returns the card granted, or None if this player already has theirs.

        The claim flag is its own profile key, NOT the League progress — which
        is what lets a player who cleared the League before this reward existed
        collect it by winning once more, while nobody is handed one
        retroactively.

        The flag goes down BEFORE the card is handed over. Paying twice is the
        failure that matters, and `claim_once` is atomic under the store lock,
        so two wins landing together cannot both win the race. The one hole
        that ordering leaves — marked, then the grant raises — is closed by
        releasing the claim.
        """
        if not claim_once(user_id, SD.K_AVATAR_CLAIM):
            return None
        card_id = random.choice(SD.LEAGUE_AVATARS)
        try:
            add_avatar_to_inventory(user_id, card_id)
        except Exception:                                # noqa: BLE001
            release_claim(user_id, SD.K_AVATAR_CLAIM)
            raise
        try:
            from cogs.avatar.avatar_engine import avatar_engine
            return avatar_engine.get_avatar(card_id) or {"id": card_id}
        except Exception:                                # noqa: BLE001
            return {"id": card_id}

    async def _finish(self, channel, player, match: LeagueMatch, won: bool,
                      difficulty: str, copy) -> None:
        coins = 0
        first = False
        card: Optional[dict] = None
        if won:
            profile = await get_user(player.id)
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
            await update_user(player.id, profile)
            try:
                grant_xp(player.id, LEAGUE_XP.get(difficulty, 0))
            except Exception:                            # noqa: BLE001
                pass

            # AFTER `update_user`, never before. `profile` above is a snapshot
            # taken at the top of this function and `claim_once` does its own
            # locked write — claiming first and then writing the snapshot back
            # would erase the flag, which is the bug `onboarding.py` records as
            # "the same bug that ate blades in redeem.grant".
            if difficulty == SD.NORMAL and SD.normal_complete(
                    await get_user(player.id)):
                try:
                    card = self._award_blader(player.id)
                except Exception:                        # noqa: BLE001
                    log.exception("[story] blader reward failed for %s",
                                  player.id)

        try:
            await channel.send(embed=match.result_embed(coins, first, card))
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

        ok, why = await self._can_fight(ctx.author.id, n, difficulty)
        if not ok:
            return await ctx.send(why)
        await self._fight(ctx.channel, ctx.author, n, difficulty)

    @commands.command(name="storymap", aliases=["leaguemap", "storylist"],
                      brief="Your School League progress 🗺️")
    async def storymap(self, ctx: commands.Context) -> None:
        view = await LeagueView.create(self, ctx.author)
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
        profile = await get_user(ctx.author.id)
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
        profile = await get_user(target.id)
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
