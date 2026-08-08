"""
cogs/battle.py
--------------
Discord Cog for the Beyblade battle system.

This file is intentionally thin. All game logic lives in the battle/ package:

  battle/constants.py       — balance knobs, move names, stamina costs
  battle/type_system.py     — TypeModifiers (attack/defense/stamina/balance)
  battle/damage_rules.py    — calc_damage(), resolve_special()
  battle/stamina_manager.py — StaminaManager (costs, regen, Special Gauge)
  battle/ability_engine.py  — AbilityEngine (all ability primitives)
                               New in v2: abilities[] array support,
                               on_attack_or_special trigger, on_hit per-hit
                               procs, shatter stacks, rebirth_ key aliases,
                               execute crits, setup(), deflect_chance,
                               _get_abilities() multi-ability iteration.
  battle/session.py         — BattleSession + InChannelControlPanel
  battle/ui.py              — ChallengeView

The cog is responsible only for:
  1. Command validation (equipped beys, no self-battle, one active battle)
  2. Sending the challenge prompt and waiting for acceptance
  3. Dual-special-move selection for Beyblades that have dual_special=True
     (e.g. Spriggan Requiem — player picks Requiem Whip or Requiem Zone
      before the battle starts; chosen move is written into blade["special_move"])
  4. Starting and cleaning up the BattleSession
  5. Exposing ;forfeit and ;battlestats
"""

import discord
from discord.ext import commands

from .session import BattleSession
from .ui import ChallengeView

from utils.database import get_user, get_beyblade, xp_to_next_level
from utils.embeds import RARITY_EMOJIS, level_badge
from cogs.economy.shop import PARTS_CATALOG


# ── Parts helper ──────────────────────────────────────────────────────────────

def _apply_parts(blade: dict, profile: dict) -> dict:
    """Return a shallow-copied blade dict with the player's EQUIPPED parts
    bonuses added to its stats.  Uses profile["equipped_parts"] (the active
    loadout), not the full ownership list in profile["parts"].
    The original blade dict is never mutated."""
    equipped_parts: list[str] = profile.get("equipped_parts", [])
    if not equipped_parts:
        return blade

    catalog: dict[str, dict] = {p["name"].lower(): p for p in PARTS_CATALOG}

    new_stats = dict(blade.get("stats", {}))
    for part_name in equipped_parts:
        part = catalog.get(part_name.lower())
        if part:
            stat  = part["stat"]
            bonus = part["bonus"]
            new_stats[stat] = new_stats.get(stat, 0) + bonus

    upgraded_blade = dict(blade)
    upgraded_blade["stats"] = new_stats
    return upgraded_blade


# ── Dual-special selection UI ─────────────────────────────────────────────────

class SpecialMoveSelectView(discord.ui.View):
    """
    Shown to a player when their equipped Beyblade has dual_special=True
    (currently only Spriggan Requiem).  The player clicks one of two buttons
    to lock in their special move before the battle begins.
    """

    def __init__(self, player: discord.Member, blade: dict) -> None:
        super().__init__(timeout=60)
        self.player   = player
        self.blade    = blade
        self.chosen   = None            # will be set to the chosen move name

        special_moves: dict = blade.get("special_moves", {})
        for move_name, move_data in special_moves.items():
            button = discord.ui.Button(
                label=move_name,
                style=discord.ButtonStyle.primary,
                custom_id=move_name,
            )
            button.callback = self._make_callback(move_name, move_data)
            self.add_item(button)

    def _make_callback(self, move_name: str, move_data: dict):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.player.id:
                await interaction.response.send_message(
                    "❌ Only the battler can select their special move!", ephemeral=True
                )
                return
            self.chosen = move_name
            # Write the chosen special move into the blade dict so
            # BattleSession / AbilityEngine can read it as blade["special_move"]
            self.blade["special_move"] = move_data
            self.blade["chosen_special"] = move_name
            await interaction.response.edit_message(
                content=(
                    f"✅ **{self.player.display_name}** chose "
                    f"**{move_name}** as their special move!"
                ),
                view=None,
            )
            self.stop()
        return callback

    async def on_timeout(self) -> None:
        """If the player doesn't choose in time, default to the first move."""
        if self.chosen is None:
            special_moves: dict = self.blade.get("special_moves", {})
            if special_moves:
                first_name, first_data = next(iter(special_moves.items()))
                self.chosen                   = first_name
                self.blade["special_move"]    = first_data
                self.blade["chosen_special"]  = first_name
        self.stop()


async def _resolve_dual_special(
    ctx: commands.Context,
    player: discord.Member,
    blade: dict,
) -> None:
    """
    If the blade has dual_special=True, send the player a message with
    special-move selection buttons and wait for them to choose.
    Mutates `blade` in place (sets blade["special_move"] and
    blade["chosen_special"]).

    Called once per player whose blade is flagged as dual_special before
    BattleSession.run() begins.
    """
    if not blade.get("dual_special"):
        return

    special_moves: dict = blade.get("special_moves", {})
    if not special_moves:
        return

    move_descriptions = "\n".join(
        f"• **{name}** — {data.get('description', '')}"
        for name, data in special_moves.items()
    )

    embed = discord.Embed(
        title=f"👑 {blade['name']} — Choose Your Special Move",
        description=(
            f"{player.mention}, your Beyblade has **2 Special Moves**.\n"
            f"Select the one you want to use **before the battle starts**!\n\n"
            f"{move_descriptions}"
        ),
        color=discord.Color.gold(),
    )
    embed.set_thumbnail(url=blade.get("image_url", ""))
    embed.set_footer(text="You have 60 seconds to choose. Defaulting to the first move if time runs out.")

    view = SpecialMoveSelectView(player, blade)
    await ctx.send(embed=embed, view=view)
    await view.wait()


# ── BattleCog ─────────────────────────────────────────────────────────────────

class BattleCog(commands.Cog, name="Battle"):
    """Button-based turn-by-turn Beyblade battles."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot             = bot
        self.active_battles: dict[int, BattleSession] = {}  # user_id → session

    # ── ;battle ──────────────────────────────────────────────────────────────

    @commands.command(
        name="battle",
        help=(
            "Challenge another player to a Beyblade battle!\n\n"
            "Both players must have an equipped Beyblade (use ;equip first).\n"
            "Moves are chosen via buttons visible to everyone in the channel.\n\n"
            "**Moves:**\n"
            "  ⚔️ Attack   — deal damage (costs 1.5 Stamina)\n"
            "  🛡️ Defense  — reduces incoming Attack damage, costs 1.5 Stamina\n"
            "  ⚡ Stamina  — recover +3 Stamina and heal HP equal to 70% of your "
            "Stamina stat (free)\n"
            "  🔋 Charge   — fill Special Gauge by +50 (costs 1 Stamina)\n"
            "  🌟 Special  — powerful move, usable when Special Gauge reaches 150, "
            "costs 3 Stamina\n\n"
            "**Special Gauge:** fills automatically as you attack, defend, use "
            "Stamina, or take damage. Use 🔋 Charge for a fast +50 boost. "
            "When it hits 150 the Special button activates.\n\n"
            "**Dual Special Moves:** Some Beyblades (e.g. Spriggan Requiem) have "
            "TWO special moves. You will be asked to pick one BEFORE the battle "
            "starts. Choose wisely — you cannot change it mid-battle!\n\n"
            "**Counter system:** Attack beats Stamina (1.2× damage!), Stamina beats "
            "Defense.\n"
            "Defense only mitigates Attack damage — it does NOT fully counter it.\n"
            "**Stamina KO:** If your Stamina hits 0, your Beyblade stops spinning — "
            "instant loss!"
        ),
        brief="Challenge a player to a battle! ⚔️",
    )
    async def battle(self, ctx: commands.Context, opponent: discord.Member,
                     mode: str = "casual") -> None:
        # ── Basic validation ──────────────────────────────────────────────────
        if opponent.bot:
            return await ctx.send("❌ You can't battle a bot!")
        if opponent.id == ctx.author.id:
            return await ctx.send("❌ You can't battle yourself!")

        # ── Ranked gate ───────────────────────────────────────────────────────
        # Checked here, before the challenge is even posted, so a player who
        # cannot play ranked finds out immediately instead of after their
        # opponent has accepted.
        ranked = str(mode).lower().startswith("rank")
        if ranked:
            from utils import ranked as RK
            for member in (ctx.author, opponent):
                why = RK.eligibility_error(get_user(member.id))
                if why:
                    who = ("You are" if member.id == ctx.author.id
                           else f"{member.display_name} is")
                    return await ctx.send(f"❌ {who} not verified for ranked.\n{why}")

        if ctx.author.id in self.active_battles or opponent.id in self.active_battles:
            return await ctx.send(
                "⚠️ One of you is already in an active battle! "
                "Finish it before starting a new one."
            )

        # ── Ensure both players have equipped Beyblades ───────────────────────
        c_profile = get_user(ctx.author.id)
        o_profile = get_user(opponent.id)

        if not c_profile.get("active_beyblade"):
            return await ctx.send(
                f"❌ {ctx.author.mention} you don't have a Beyblade equipped!\n"
                f"Use `;equip <name>` to equip one from your inventory."
            )
        if not o_profile.get("active_beyblade"):
            return await ctx.send(
                f"❌ {opponent.mention} doesn't have a Beyblade equipped!"
            )

        # Boss copies are rolled instances, so their name is not in
        # beyblades.json — resolving it there would fail the lookup below and
        # tell the player their own equipped blade "wasn't found". The resolver
        # returns the copy's rolled blade dict when one is equipped.
        from cogs.battle.boss import boss_copy as _bcopy
        blade1_raw, copy1 = _bcopy.equipped_blade(ctx.author.id)
        blade2_raw, copy2 = _bcopy.equipped_blade(opponent.id)

        if not blade1_raw:
            return await ctx.send(
                f"❌ Beyblade **{c_profile['active_beyblade']}** not found in the database!"
            )
        if not blade2_raw:
            return await ctx.send(
                f"❌ Beyblade **{o_profile['active_beyblade']}** not found in the database!"
            )

        # Parts bolt onto database blades. A copy is a fixed roll — letting
        # parts stack on top would make a farmed copy strictly better than the
        # boss it came from.
        blade1 = blade1_raw if copy1 else _apply_parts(blade1_raw, c_profile)
        blade2 = blade2_raw if copy2 else _apply_parts(blade2_raw, o_profile)

        # ── Dual-special selection (e.g. Spriggan Requiem) ───────────────────
        # Each player with a dual_special blade must pick their special move
        # before the challenge embed goes out.  We handle them sequentially so
        # the channel doesn't get flooded with two selection embeds at once.
        if blade1.get("dual_special"):
            await _resolve_dual_special(ctx, ctx.author, blade1)

        if blade2.get("dual_special"):
            await _resolve_dual_special(ctx, opponent, blade2)

        # ── Send challenge ────────────────────────────────────────────────────
        r1 = RARITY_EMOJIS.get(blade1.get("rarity", "Common"), "")
        r2 = RARITY_EMOJIS.get(blade2.get("rarity", "Common"), "")

        # Show chosen special move in challenge embed if applicable
        sm1_note = (
            f" *(Special: {blade1['chosen_special']})*"
            if blade1.get("chosen_special") else ""
        )
        sm2_note = (
            f" *(Special: {blade2['chosen_special']})*"
            if blade2.get("chosen_special") else ""
        )

        challenge_embed = discord.Embed(
            title="⚔️ Beyblade Battle Challenge!",
            description=(
                f"{ctx.author.mention} challenges {opponent.mention} to a battle!\n\n"
                f"{r1} **{blade1['name']}**{sm1_note} vs "
                f"{r2} **{blade2['name']}**{sm2_note}\n\n"
                f"{opponent.mention}, do you accept?"
            ),
            color=discord.Color.red(),
        )

        view = ChallengeView(ctx.author, opponent)
        msg  = await ctx.send(embed=challenge_embed, view=view)
        await view.wait()

        if not view.accepted:
            await msg.edit(view=None)
            return

        await msg.edit(view=None)

        # ── Start session ─────────────────────────────────────────────────────
        session = BattleSession(
            bot     = self.bot,
            channel = ctx.channel,
            p1      = ctx.author,
            p2      = opponent,
            blade1  = blade1,
            blade2  = blade2,
            ranked  = ranked,
        )
        self.active_battles[ctx.author.id] = session
        self.active_battles[opponent.id]   = session

        try:
            await session.run()
        except Exception as e:
            import traceback
            print(f"Battle session error: {e}")
            traceback.print_exc()
            await ctx.send("⚠️ An error occurred during the battle. The battle has been ended.")
        finally:
            self.active_battles.pop(ctx.author.id, None)
            self.active_battles.pop(opponent.id,   None)

    # ── ;forfeit ─────────────────────────────────────────────────────────────

    @commands.command(
        name="forfeit",
        help="Forfeit your current battle. You will take a loss.",
        brief="Give up your current battle.",
    )
    async def forfeit(self, ctx: commands.Context) -> None:
        session = self.active_battles.get(ctx.author.id)

        if not session:
            return await ctx.send("❌ You are not in an active battle.")
        if not session.is_player(ctx.author):
            return await ctx.send("❌ You are not a participant in the current battle.")

        # Check if battle is already over
        if session.hp.get(str(ctx.author.id), 0) <= 0:
            return await ctx.send("❌ The battle is already over!")

        key = str(ctx.author.id)
        session.hp[key] = 0
        await ctx.send(f"🏳️ **{ctx.author.display_name}** forfeits the battle!")
        try:
            await session._end_battle()
        except Exception as e:
            import traceback
            print(f"Forfeit end_battle error: {e}")
            traceback.print_exc()

    # ── ;battlestats ──────────────────────────────────────────────────────────

    @commands.command(
        name="battlestats",
        aliases=["bs"],
        help="Show your win/loss record and trainer level.",
        brief="Show your battle record.",
    )
    async def battlestats(
        self, ctx: commands.Context, member: discord.Member = None
    ) -> None:
        target  = member or ctx.author
        profile = get_user(target.id)
        # Safely unpack xp_to_next_level return value
        try:
            lvl_xp_result = xp_to_next_level(profile.get("xp", 0))
            if len(lvl_xp_result) == 3:
                lvl, xp_needed, xp_prog = lvl_xp_result
            else:
                lvl = lvl_xp_result[0] if len(lvl_xp_result) > 0 else 1
                xp_needed = lvl_xp_result[1] if len(lvl_xp_result) > 1 else 100
                xp_prog = lvl_xp_result[2] if len(lvl_xp_result) > 2 else 0
        except Exception as e:
            print(f"xp_to_next_level error: {e}")
            lvl, xp_needed, xp_prog = 1, 100, 0

        badge = level_badge(lvl)

        blade_name = profile.get("active_beyblade") or "None equipped"

        embed = discord.Embed(
            title=f"{badge} {target.display_name}'s Battle Record",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="🏆 Wins",    value=str(profile.get("wins",   0)), inline=True)
        embed.add_field(name="💀 Losses",  value=str(profile.get("losses", 0)), inline=True)
        embed.add_field(name="⚡ Level",   value=f"{lvl}", inline=True)
        embed.add_field(name="📊 XP",      value=f"{xp_prog}/{xp_needed}", inline=True)
        embed.add_field(name="🌀 Equipped", value=blade_name, inline=True)
        embed.set_thumbnail(url=target.display_avatar.url)
        await ctx.send(embed=embed)


# ── Cog loader ────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BattleCog(bot))
