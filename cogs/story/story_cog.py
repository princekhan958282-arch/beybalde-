"""
story_cog.py — Story Mode's Discord surface.

Prefix commands own all the logic; the slash group delegates to them with
Context.from_interaction + ctx.invoke. That is the house pattern (see
PlayerCommands in cogs/economy/profile.py) and it exists so validation,
cooldowns and rendering cannot drift between the two entry points.

Deliberately NOT a subclass of, or registered inside, BossCog: that cog has a
cog_check enforcing BOSS_SYSTEM_LOCKED, and Story Mode must stay open.
"""

from __future__ import annotations

import logging
import random
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import bey_levels as _BL
from utils.database import get_user, update_user, grant_xp
from utils.mobile_ui import bar as progress_bar

from cogs.battle.boss import boss_ai as ai

from . import story_data
from .story_engine import MOVE_LABELS, StoryFight

log = logging.getLogger("beyblade_bot.story")

TURN_SECONDS = 60
VIEW_TIMEOUT = TURN_SECONDS * 6

# Story wins feed nothing but Story Mode. `beycord_battle_end` and
# `beycord_battle_blades` drive quests, mastery, achievements and clan wars,
# and firing them here would quietly let a player farm all four against an NPC.
# Flip to True if story wins should count toward those too.
DISPATCH_BATTLE_EVENTS = False

MOVE_STYLES = {
    ai.MOVE_ATTACK:  discord.ButtonStyle.danger,
    ai.MOVE_DEFENSE: discord.ButtonStyle.primary,
    ai.MOVE_STAMINA: discord.ButtonStyle.success,
    ai.MOVE_CHARGE:  discord.ButtonStyle.secondary,
    ai.MOVE_SPECIAL: discord.ButtonStyle.success,
}

RESULT_TEXT = {
    "win":  "🏆 **Stage cleared!**",
    "loss": "💀 **Defeated.** Try again — nothing is lost.",
    "draw": "🤝 **Double knockout.** No reward this time.",
}


# ── Progress helpers ─────────────────────────────────────────────────────────
# Story progress is stored ad-hoc on the profile, the same way boss clears are
# (`bosses_cleared`). The profile is a free-form JSON blob, so no schema change
# is needed and nothing has to be added to HOT_FIELDS.

def cleared_of(profile: dict) -> list[str]:
    return list(profile.get("story_cleared") or [])


def stats_of(profile: dict) -> dict:
    s = profile.get("story_stats") or {}
    return {"wins": int(s.get("wins", 0)), "losses": int(s.get("losses", 0))}


def _bey_level_of(profile: dict) -> int:
    """The equipped bey's level, for comparing against a stage's level.

    Reads the same bey_progress row bey_levels uses, so the number shown here
    is the one that actually reached the fighter through effective_blade. An
    equipped boss copy has no progress row and does not level — it reads as 1.
    """
    try:
        name = profile.get("active_beyblade")
        if not name or profile.get("active_copy"):
            return 1
        entry = (profile.get("bey_progress") or {}).get(str(name))
        if not entry:
            return 1
        return int(_BL.level_from_xp(int(entry.get("xp", 0))))
    except Exception:                                    # noqa: BLE001
        return 1


def _bar(cur: float, mx: float, width: int = 10) -> str:
    filled = max(0, min(width, int(round((cur / mx) * width)))) if mx else 0
    return "█" * filled + "░" * (width - filled)


def _stage_line(st: dict, cleared: set[str]) -> str:
    if st["id"] in cleared:
        mark = "✅"
    elif story_data.is_unlocked(st["id"], cleared):
        mark = "▶️"
    else:
        mark = "🔒"
    boss = " 👑" if st.get("boss") else ""
    return (f"{mark} **{st['id']}** {st['emoji']} {st['name']}"
            f"  `Lv {st['level']}`{boss}\n"
            f"　*{st['blurb']}*")


# ── The fight view ───────────────────────────────────────────────────────────

class StoryFightView(discord.ui.View):
    """One button per move, redrawn each turn so unaffordable moves grey out."""

    def __init__(self, cog: "StoryCog", fight: StoryFight,
                 player: discord.Member) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.fight = fight
        self.player = player
        self.message: Optional[discord.Message] = None
        self.busy = False
        self._build()

    def _build(self) -> None:
        self.clear_items()
        f = self.fight
        for i, move in enumerate(ai.ALL_MOVES):
            emoji, label = MOVE_LABELS[move]
            btn = discord.ui.Button(
                label=label, emoji=emoji, style=MOVE_STYLES[move],
                row=0 if i < 3 else 1,
                disabled=f.finished or not f.foe.can(move),
            )
            btn.callback = self._make_cb(move)
            self.add_item(btn)

    def _make_cb(self, move: str):
        async def cb(interaction: discord.Interaction) -> None:
            f = self.fight
            if interaction.user.id != self.player.id:
                return await interaction.response.send_message(
                    "This isn't your fight — run `;story` to start your own.",
                    ephemeral=True)
            if f.finished or self.busy:
                return await interaction.response.defer()
            if not f.foe.can(move):
                return await interaction.response.send_message(
                    "Not enough stamina for that.", ephemeral=True)

            self.busy = True
            try:
                f.step(move)
                self._build()
                await interaction.response.edit_message(embed=self.embed(),
                                                        view=self)
                if f.finished:
                    self.stop()
                    await self.cog.finish(f, self.player, interaction.channel)
            except Exception:                        # noqa: BLE001
                # Never strand the player in _active on a crash — they would be
                # "already in a fight" forever.
                log.exception("story turn failed")
                f.finished = True
                self.cog.release(self.player.id)
                self.stop()
                try:
                    await interaction.followup.send(
                        "⚠️ Something went wrong resolving that turn — "
                        "the fight was ended. Nothing was lost.", ephemeral=True)
                except Exception:                    # noqa: BLE001
                    pass
            finally:
                self.busy = False
        return cb

    def embed(self) -> discord.Embed:
        f = self.fight
        st = f.stage
        e = discord.Embed(
            title=f"{st['emoji']}  {st['name']}",
            description=f"*{st['chapter_name']} · Stage {st['id']}*",
            color=st["colour"],
        )
        e.add_field(
            name=f"{st['emoji']} {st['name']}  `Lv {f.npc_level}`",
            value=(f"`{_bar(f.npc.hp, f.npc.max_hp)}` {f.npc.hp:.0f}\n"
                   f"🌀 {int(f.npc.gauge)}/{ai.SPECIAL_GAUGE_MAX} · "
                   f"💨 {f.npc.sp:.1f}"),
            inline=True,
        )
        e.add_field(
            name=f"🌀 {f.foe.name}  `Lv {f.bey_level}`",
            value=(f"`{_bar(f.foe.hp, f.foe.max_hp)}` {f.foe.hp:.0f}\n"
                   f"🌀 {int(f.foe.gauge)}/{ai.SPECIAL_GAUGE_MAX} · "
                   f"💨 {f.foe.sp:.1f}"),
            inline=True,
        )
        if f.log:
            e.add_field(name="Last exchanges", value="\n".join(f.log),
                        inline=False)
        # The whole point of the avatar wiring is that you can SEE it work.
        if f.avatar_logs:
            e.add_field(name="🎭 Avatar", value="\n".join(f.avatar_logs),
                        inline=False)
        if f.finished:
            e.add_field(name="Result", value=RESULT_TEXT[f.result], inline=False)

        foot = (f"Turn {f.turn} · difficulty: {f.difficulty} · "
                f"Trainer Lv {f.trainer_level}")
        if f.level_gap > 0:
            foot += f" · ⚠️ {f.level_gap} levels under"
        if not f.avatar_active:
            foot += " · no avatar equipped (;avatarshop)"
        e.set_footer(text=foot)
        return e

    async def on_timeout(self) -> None:
        # Release the player, or an idle fight would leave them permanently
        # "already in a fight" — the same trap BossView.on_timeout guards.
        self.fight.finished = True
        self.cog.release(self.player.id)
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                e = self.embed()
                e.set_footer(text="⏰ Timed out — the fight was abandoned.")
                await self.message.edit(embed=e, view=self)
            except Exception:                        # noqa: BLE001
                pass


# ── Stage picker ─────────────────────────────────────────────────────────────

class StageSelect(discord.ui.Select):
    def __init__(self, cleared: set[str]) -> None:
        options = []
        for st in story_data.all_stages():
            unlocked = story_data.is_unlocked(st["id"], cleared)
            done = st["id"] in cleared
            if not unlocked:
                continue
            options.append(discord.SelectOption(
                label=f"{st['id']} · {st['name']}"[:100],
                description=(("Cleared — replay for reduced rewards"
                              if done else st["blurb"]))[:100],
                value=st["id"],
                emoji="✅" if done else st["emoji"],
            ))
        # Discord allows at most 25 options; the campaign is 12, but slicing
        # keeps this correct if chapters are added later.
        super().__init__(placeholder="Pick a stage…", options=options[:25])

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StoryPickView" = self.view          # type: ignore[assignment]
        if interaction.user.id != view.player.id:
            return await interaction.response.send_message(
                "This isn't your menu.", ephemeral=True)
        await view.cog.launch(interaction, view.player, self.values[0])
        view.stop()


class StoryPickView(discord.ui.View):
    def __init__(self, cog: "StoryCog", player: discord.Member,
                 cleared: set[str]) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.player = player
        self.message: Optional[discord.Message] = None
        self.add_item(StageSelect(cleared))

    async def on_timeout(self) -> None:
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:                        # noqa: BLE001
                pass


# ── The cog ──────────────────────────────────────────────────────────────────

class StoryCog(commands.Cog, name="Story"):
    """Solo chapter/stage campaign."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._active: set[int] = set()

    def release(self, user_id: int) -> None:
        self._active.discard(user_id)

    # ── Gating ───────────────────────────────────────────────────────────────

    def _can_fight(self, user_id: int, stage_id: str) -> tuple[bool, str]:
        """Mirrors BossCog._can_fight: (ok, reason-if-not)."""
        if user_id in self._active:
            return False, "You're already in a Story Mode fight."
        profile = get_user(user_id)
        cleared = set(cleared_of(profile))
        if not story_data.is_unlocked(stage_id, cleared):
            need = story_data.prereq_of(stage_id)
            st = story_data.stage(need) or {}
            return False, (f"🔒 **{stage_id}** is locked.\n"
                           f"Clear **{need} · {st.get('name', '?')}** first — "
                           f"`;story {need}`.")
        return True, ""

    # ── Launching ────────────────────────────────────────────────────────────

    async def launch(self, interaction: discord.Interaction,
                     player: discord.Member, stage_id: str) -> None:
        ok, why = self._can_fight(player.id, stage_id)
        if not ok:
            return await interaction.response.send_message(why, ephemeral=True)
        try:
            fight = StoryFight(player.id, player.display_name, stage_id)
        except Exception:                            # noqa: BLE001
            log.exception("could not build story fight %s", stage_id)
            return await interaction.response.send_message(
                "⚠️ Couldn't start that stage. Do you have a Beyblade equipped? "
                "Try `;inventory`.", ephemeral=True)

        self._active.add(player.id)
        view = StoryFightView(self, fight, player)
        await interaction.response.send_message(embed=view.embed(), view=view)
        view.message = await interaction.original_response()

    async def _start(self, ctx: commands.Context, stage_id: str) -> None:
        ok, why = self._can_fight(ctx.author.id, stage_id)
        if not ok:
            return await ctx.send(why)
        try:
            fight = StoryFight(ctx.author.id, ctx.author.display_name, stage_id)
        except Exception:                            # noqa: BLE001
            log.exception("could not build story fight %s", stage_id)
            return await ctx.send(
                "⚠️ Couldn't start that stage. Do you have a Beyblade equipped? "
                "Try `;inventory`.")

        self._active.add(ctx.author.id)
        view = StoryFightView(self, fight, ctx.author)
        view.message = await ctx.send(embed=view.embed(), view=view)

    # ── Rewards ──────────────────────────────────────────────────────────────

    async def finish(self, fight: StoryFight, player: discord.Member,
                     channel) -> None:
        """Pay out a finished fight. Only a win pays."""
        self.release(player.id)
        st = fight.stage
        profile = get_user(player.id)
        stats = stats_of(profile)

        if fight.result != "win":
            stats["losses"] += 1
            profile["story_stats"] = stats
            update_user(player.id, profile)
            return

        reward = st["reward"]
        cleared = cleared_of(profile)
        first = st["id"] not in cleared
        mult = 2 if first else 1

        coins = int(reward["coins"]) * mult
        xp = int(reward["xp"]) * mult
        bey_xp = random.randint(*reward["bey_xp"])

        profile["coins"] = int(profile.get("coins", 0)) + coins
        if first:
            cleared.append(st["id"])
            profile["story_cleared"] = cleared
        stats["wins"] += 1
        profile["story_stats"] = stats
        levelled = self._grant_bey_xp(profile, fight.blade, bey_xp)
        # Must be persisted BEFORE grant_xp, which re-reads the profile from
        # the store — the same ordering trap boss_battle.finish documents.
        update_user(player.id, profile)
        new_level, _total, levelled_up = grant_xp(player.id, xp)

        nxt = story_data.next_stage(st["id"])
        e = discord.Embed(
            title=f"🏆 {st['id']} cleared — {st['name']} defeated!",
            color=st["colour"],
            description=("**First clear — double rewards!**" if first
                         else "Replay — standard rewards."),
        )
        e.add_field(name="💰 Beycoins", value=f"+{coins:,}", inline=True)
        e.add_field(name="⭐ Trainer EXP", value=f"+{xp:,}", inline=True)
        if levelled:
            e.add_field(name=f"🌀 {levelled['blade']}",
                        value=(f"+{levelled['gained']:,} EXP"
                               + (f" — **Lv {levelled['level']}!**"
                                  if levelled["leveled"] else "")),
                        inline=True)
        if levelled_up:
            e.add_field(name="🎉 Level up!", value=f"You are now **Lv {new_level}**",
                        inline=False)
        if nxt:
            nst = story_data.stage(nxt)
            e.add_field(name="▶️ Next", value=f"**{nxt}** {nst['emoji']} {nst['name']}"
                                              f" — `;story {nxt}`", inline=False)
        else:
            e.add_field(name="👑 Campaign complete",
                        value="You have cleared every stage. Replays still pay.",
                        inline=False)
        if not fight.avatar_active:
            e.set_footer(text="No avatar equipped — ;avatarshop, then ;equipavatar")

        if DISPATCH_BATTLE_EVENTS:
            self.bot.dispatch("beycord_battle_end", player.id, [player.id],
                              getattr(channel, "guild", None)
                              and channel.guild.id)
        try:
            await channel.send(embed=e)
        except Exception:                            # noqa: BLE001
            log.exception("could not post story reward")

    @staticmethod
    def _grant_bey_xp(profile: dict, blade: dict, amount: int) -> Optional[dict]:
        """Blade EXP for the bey that fought. Mirrors session._bey_xp: skipped
        for an equipped boss copy (a copy is a fixed roll and doesn't level),
        and never raises — a bad blade dict must not cost someone a reward."""
        try:
            if not blade or profile.get("active_copy"):
                return None
            name = blade.get("name")
            if not name:
                return None
            return _BL.award(profile, name, amount)
        except Exception:                            # noqa: BLE001
            return None

    # ── Prefix commands ──────────────────────────────────────────────────────

    @commands.command(name="story", aliases=["campaign"])
    async def story(self, ctx: commands.Context, *, stage: str = None) -> None:
        """📖 Play Story Mode. `;story` to pick, `;story 1-2` to jump in."""
        profile = get_user(ctx.author.id)
        cleared = set(cleared_of(profile))

        if not stage:
            view = StoryPickView(self, ctx.author, cleared)
            nxt = story_data.first_uncleared(cleared)
            e = discord.Embed(
                title="📖 Story Mode",
                description=(f"Up next: **{nxt}** — "
                             f"{story_data.stage(nxt)['name']}"
                             if nxt else
                             "You've cleared the whole campaign. Replays still pay."),
                color=0x3498DB,
            )
            e.set_footer(text="`;storymap` for the full chapter list")
            view.message = await ctx.send(embed=e, view=view)
            return

        st = story_data.resolve(stage)
        if st is None:
            hits = story_data.matches(stage)
            if len(hits) > 1:
                names = ", ".join(f"**{h['id']} {h['name']}**" for h in hits[:5])
                return await ctx.send(
                    f"❌ Multiple matches for `{stage}`: {names}. Be more specific.")
            return await ctx.send(
                f"❌ No stage matching `{stage}`. Try `;storymap`.")
        await self._start(ctx, st["id"])

    @commands.command(name="storymap", aliases=["chapters", "storylist"])
    async def storymap(self, ctx: commands.Context) -> None:
        """📖 Every chapter and how far you've got."""
        profile = get_user(ctx.author.id)
        cleared = set(cleared_of(profile))

        e = discord.Embed(title="📖 Story Mode — Chapters", color=0x3498DB)
        for cnum in sorted(story_data.CHAPTERS):
            ch = story_data.CHAPTERS[cnum]
            stages = story_data.chapter_stages(cnum)
            done = sum(1 for s in stages if s["id"] in cleared)
            e.add_field(
                name=f"{ch['emoji']} Chapter {cnum} — {ch['name']}  "
                     f"{progress_bar(done, len(stages))} {done}/{len(stages)}",
                value="\n".join(_stage_line(s, cleared) for s in stages),
                inline=False,
            )
        total = story_data.total_stages()
        e.set_footer(text=f"{len(cleared & {s['id'] for s in story_data.all_stages()})}"
                          f"/{total} stages cleared  •  ;story <stage> to fight")
        await ctx.send(embed=e)

    @commands.command(name="storyinfo", aliases=["stageinfo"])
    async def storyinfo(self, ctx: commands.Context, *, stage: str) -> None:
        """📖 Stats, rewards and lock state for one stage."""
        st = story_data.resolve(stage)
        if st is None:
            return await ctx.send(f"❌ No stage matching `{stage}`. Try `;storymap`.")

        profile = get_user(ctx.author.id)
        cleared = set(cleared_of(profile))
        unlocked = story_data.is_unlocked(st["id"], cleared)
        done = st["id"] in cleared

        e = discord.Embed(
            title=f"{st['emoji']}  {st['id']} — {st['name']}",
            description=f"*{st['blurb']}*",
            color=st["colour"],
        )
        e.add_field(name="Chapter", value=f"{st['chapter']} · {st['chapter_name']}",
                    inline=True)
        e.add_field(name="Difficulty", value=st["difficulty"].title(), inline=True)
        e.add_field(name="Type", value=st["type"].title(), inline=True)

        stats = st["stats"]
        bey_level = _bey_level_of(profile)
        e.add_field(
            name=f"Opponent — Level {st['level']}",
            value=(f"❤️ {stats['hp']:,} HP\n"
                   f"⚔️ {stats['attack']} ATK · 🛡️ {stats['defense']} DEF · "
                   f"🌀 {stats['stamina']} STA"),
            inline=False,
        )
        gap = st["level"] - bey_level
        e.add_field(
            name="Your blade",
            value=(f"Lv {bey_level}"
                   + (f" — ⚠️ **{gap} levels under**, expect a hard fight"
                      if gap > 0 else " — level advantage 👍")),
            inline=False,
        )
        r = st["reward"]
        e.add_field(
            name="Rewards",
            value=(f"💰 {r['coins']:,} coins · ⭐ {r['xp']:,} EXP · "
                   f"🌀 {r['bey_xp'][0]}–{r['bey_xp'][1]} blade EXP\n"
                   f"*First clear pays double.*"),
            inline=False,
        )
        if done:
            e.add_field(name="Status", value="✅ Cleared", inline=False)
        elif unlocked:
            e.add_field(name="Status", value=f"▶️ Open — `;story {st['id']}`",
                        inline=False)
        else:
            need = story_data.prereq_of(st["id"])
            e.add_field(name="Status",
                        value=f"🔒 Locked — clear **{need}** first", inline=False)
        await ctx.send(embed=e)

    @commands.command(name="storystats", aliases=["storyprogress"])
    async def storystats(self, ctx: commands.Context,
                         member: discord.Member = None) -> None:
        """📖 Story Mode record and progress."""
        target = member or ctx.author
        profile = get_user(target.id)
        cleared = set(cleared_of(profile))
        stats = stats_of(profile)
        total = story_data.total_stages()
        done = len([s for s in story_data.all_stages() if s["id"] in cleared])
        played = stats["wins"] + stats["losses"]

        e = discord.Embed(title=f"📖 {target.display_name} — Story Mode",
                          color=0x3498DB)
        e.add_field(name="Progress",
                    value=f"{progress_bar(done, total)} **{done}/{total}** stages",
                    inline=False)
        e.add_field(name="Record",
                    value=(f"🏆 {stats['wins']} wins · 💀 {stats['losses']} losses"
                           + (f" · {stats['wins'] / played:.0%} win rate"
                              if played else "")),
                    inline=False)
        bey_level = _bey_level_of(profile)
        e.add_field(
            name="Levels",
            value=(f"🌀 Blade **Lv {bey_level}**"
                   f"　·　⭐ Trainer **Lv {int(profile.get('level', 0) or 0)}**"),
            inline=False)
        nxt = story_data.first_uncleared(cleared)
        if nxt:
            nst = story_data.stage(nxt)
            gap = nst["level"] - bey_level
            e.add_field(name="▶️ Up next",
                        value=(f"**{nxt}** {nst['emoji']} {nst['name']} "
                               f"`Lv {nst['level']}` — `;story {nxt}`"
                               + (f"\n⚠️ You are **{gap} levels under** — "
                                  f"battles and `;story` replays both level "
                                  f"your blade." if gap > 0 else "")),
                        inline=False)
        else:
            e.add_field(name="👑 Complete", value="Every stage cleared.",
                        inline=False)
        await ctx.send(embed=e)


# ── Slash surface ────────────────────────────────────────────────────────────

async def stage_autocomplete(interaction: discord.Interaction,
                             current: str) -> list[app_commands.Choice[str]]:
    """Stages, annotated with the caller's own progress."""
    try:
        cleared = set(cleared_of(get_user(interaction.user.id)))
    except Exception:                                # noqa: BLE001
        cleared = set()
    cur = (current or "").lower()
    out: list[app_commands.Choice[str]] = []
    for st in story_data.all_stages():
        if cur and cur not in st["id"] and cur not in st["name"].lower():
            continue
        if st["id"] in cleared:
            mark = "✅"
        elif story_data.is_unlocked(st["id"], cleared):
            mark = "▶️"
        else:
            mark = "🔒"
        out.append(app_commands.Choice(
            name=f"{mark} {st['id']} · {st['name']}"[:100], value=st["id"]))
    return out[:25]                                  # Discord shows 25 at most


class StoryCommands(commands.Cog, name="Story (slash)"):
    """Slash entry points. These delegate to the prefix commands rather than
    duplicating their logic — a second copy is how the two paths drift."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    story = app_commands.Group(name="story",
                               description="Story Mode — chapters & stages")

    async def _run(self, interaction: discord.Interaction, command_name: str,
                   *args) -> None:
        cmd = self.bot.get_command(command_name)
        if cmd is None:
            return await interaction.response.send_message(
                f"`{command_name}` isn't loaded right now.", ephemeral=True)
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.invoke(cmd, *args)

    @story.command(name="play", description="Fight a Story Mode stage")
    @app_commands.describe(stage="Which stage, e.g. 1-2 — leave blank to pick one")
    @app_commands.autocomplete(stage=stage_autocomplete)
    async def s_play(self, interaction: discord.Interaction,
                     stage: Optional[str] = None) -> None:
        await self._run(interaction, "story", stage=stage)

    @story.command(name="map", description="Every chapter and your progress")
    async def s_map(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, "storymap")

    @story.command(name="info",
                   description="Opponent stats, rewards and lock state")
    @app_commands.describe(stage="Which stage, e.g. 2-3")
    @app_commands.autocomplete(stage=stage_autocomplete)
    async def s_info(self, interaction: discord.Interaction, stage: str) -> None:
        await self._run(interaction, "storyinfo", stage=stage)

    @story.command(name="stats", description="Your Story Mode record")
    @app_commands.describe(member="Whose record (defaults to you)")
    async def s_stats(self, interaction: discord.Interaction,
                      member: Optional[discord.Member] = None) -> None:
        await self._run(interaction, "storystats", member)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StoryCog(bot))
    await bot.add_cog(StoryCommands(bot))
