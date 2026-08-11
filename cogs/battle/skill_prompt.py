"""
battle/skill_prompt.py — asking, out loud, which avatar skill you are taking in.

The problem
-----------
`;askill` stores a STANDING pick, and `BattleSession.__init__` spends it
silently. A player who never ran `;askill` fights with slot 1 forever and is
never told. "Choose one skill before the battle" shipped as a setting rather
than as a choice, so from the player's side it looked automatic — because it
was.

This module puts the choice in the channel, once, after the challenge is
accepted and before any session exists.

Three rules it obeys
--------------------
1. **It stays out of the way.** 27 of the 36 avatars carry no skills at all,
   and plenty of players have none equipped, so a battle where nobody has
   anything to pick gets NO message. Silence is the common case.

2. **It never blocks the battle.** Timing out, misclicking, or ignoring it
   leaves each player on the pick they already had — the same behaviour as
   before this module existed. `SpecialMoveSelectView.on_timeout` set that
   precedent for the dual-special prompt and it is the right one: a fight must
   not fail to start because somebody was slow.

3. **It runs once per MATCH, not once per round.** A ranked match builds a
   fresh `BattleSession` every round, so hanging this off the session would ask
   up to nine times. The energy budget is a match-long thing; so is the pick.

Where it is NOT used, deliberately
----------------------------------
`cogs/extras/tournament.py` builds a session with nobody watching — its players
come from `guild.get_member()` and may be offline — so a blocking prompt would
hang a bracket. The boss lobby is up to four players and auto-launches on
timeout, and Story has no confirm step at all. All three keep the standing
`;askill` pick, which is exactly what they had before.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord

from .constants import SKILL_PROMPT_SECONDS
from cogs.avatar import avatar_skills as AS
from cogs.avatar.avatar_engine import avatar_engine
from cogs.avatar.avatar_utils import RARITY_EMOJI

log = logging.getLogger("beyblade_bot.skill_prompt")


# ── Who actually needs asking ────────────────────────────────────────────────

def participants(players) -> list[tuple[object, dict]]:
    """(member, avatar) for every player holding a card with skills.

    Pure and side-effect free so it can be tested without a gateway. Everything
    that can go wrong per player — no avatar equipped, an id that no longer
    resolves, an unreadable profile — drops that player from the prompt rather
    than raising, because the alternative is a battle that will not start.
    """
    out: list[tuple[object, dict]] = []
    for member in players:
        try:
            avatar_id = avatar_engine.get_equipped_avatar_id(int(member.id))
            if not avatar_id:
                continue
            avatar = avatar_engine.get_avatar(avatar_id)
            if AS.has_skills(avatar):
                out.append((member, avatar))
        except Exception as exc:                         # noqa: BLE001
            log.debug("skill prompt skipped %s: %s", member, exc)
    return out


# ── The per-player dropdown, shown ephemerally ───────────────────────────────

class _SkillSelect(discord.ui.Select):
    """One player's three skills. Ephemeral, because the two players hold
    different cards and a single shared Select cannot show both."""

    def __init__(self, prompt: "SkillPromptView", member, avatar: dict,
                 energy: int, ranked: bool) -> None:
        skills = avatar.get("skills") or []
        options = []
        for i, sk in enumerate(skills, 1):
            cost = AS.skill_cost(i)
            # In ranked the pool is real, so say plainly what is unaffordable.
            # In casual nothing is charged, so no warning would be honest.
            warn = "  ⚠️ can't afford" if ranked and energy < cost else ""
            options.append(discord.SelectOption(
                label=f"{i}. {sk.get('name', 'Skill')}"[:100],
                description=f"{cost}⚡{warn} · "
                            f"{sk.get('description', '')}"[:100],
                value=str(i),
                default=(i == prompt.picked.get(member.id)),
            ))
        super().__init__(placeholder="Choose the skill you fight with…",
                         options=options, min_values=1, max_values=1)
        # NOT `self.parent`. discord.ui.Item declares `parent` as a read-only
        # property (no setter), so assigning it raises AttributeError inside
        # __init__ — which happens while building the argument to
        # send_message, so the interaction is never acknowledged and Discord
        # shows "The application did not respond" with the traceback buried in
        # the discord.ui.view logger. Any name but `parent` is fine.
        self.prompt = prompt
        self.member = member
        self.avatar = avatar

    async def callback(self, interaction: discord.Interaction) -> None:
        slot = int(self.values[0])
        try:
            AS.set_choice(self.member.id, self.avatar["id"], slot)
        except Exception as exc:                         # noqa: BLE001
            log.exception("could not store skill pick: %s", exc)
            return await interaction.response.send_message(
                "❌ Couldn't save that. Your previous pick still stands.",
                ephemeral=True)

        self.prompt.picked[self.member.id] = slot
        sk = AS.skill_at(self.avatar, slot) or {}
        await interaction.response.edit_message(
            content=f"✅ Locked in **{sk.get('name', 'Skill')}** "
                    f"({AS.skill_cost(slot)}⚡).",
            view=None)
        await self.prompt.refresh()


class _SkillSelectView(discord.ui.View):
    def __init__(self, prompt: "SkillPromptView", member, avatar: dict,
                 energy: int, ranked: bool) -> None:
        super().__init__(timeout=SKILL_PROMPT_SECONDS)
        self.add_item(_SkillSelect(prompt, member, avatar, energy, ranked))


# ── The public prompt ────────────────────────────────────────────────────────

class SkillPromptView(discord.ui.View):
    """One public message, one button per player, each opening their own list."""

    def __init__(self, entries: list[tuple[object, dict]], ranked: bool) -> None:
        super().__init__(timeout=SKILL_PROMPT_SECONDS)
        self.entries = entries
        self.ranked = ranked
        self.picked: dict[int, int] = {}
        self.message: Optional[discord.Message] = None
        self.energy: dict[int, int] = {}

        for member, avatar in entries:
            try:
                AS.accrue_for(int(member.id))
                self.energy[member.id] = AS.state_for(int(member.id))["energy"]
            except Exception:                            # noqa: BLE001
                self.energy[member.id] = AS.MAX_ENERGY
            self.add_item(self._button(member, avatar))

    def _button(self, member, avatar: dict) -> discord.ui.Button:
        button = discord.ui.Button(
            label=f"{getattr(member, 'display_name', str(member))[:20]} — "
                  f"choose skill",
            style=discord.ButtonStyle.primary,
            emoji=RARITY_EMOJI.get(avatar.get("rarity"), "✨"))

        async def callback(interaction: discord.Interaction) -> None:
            # Per-callback ownership check rather than View.interaction_check:
            # the view holds a button PER player, so a single view-wide check
            # could not tell which of them the clicker is allowed to press.
            if interaction.user.id != member.id:
                return await interaction.response.send_message(
                    "That's not your avatar.", ephemeral=True)
            await interaction.response.send_message(
                embed=self._card_embed(member, avatar),
                view=_SkillSelectView(self, member, avatar,
                                      self.energy.get(member.id, AS.MAX_ENERGY),
                                      self.ranked),
                ephemeral=True)

        button.callback = callback
        return button

    def _card_embed(self, member, avatar: dict) -> discord.Embed:
        pool = self.energy.get(member.id, AS.MAX_ENERGY)
        e = discord.Embed(
            title=f"{RARITY_EMOJI.get(avatar.get('rarity'), '✨')} "
                  f"{avatar['name']} — pick your skill",
            colour=0x00E5A0)
        for i, sk in enumerate(avatar.get("skills") or [], 1):
            e.add_field(name=f"{i}. {sk.get('name', 'Skill')} — "
                             f"{AS.skill_cost(i)}⚡",
                        value=sk.get("description", ""), inline=False)
        e.set_footer(
            text=(f"⚡ {pool}/{AS.MAX_ENERGY} · this is a RANKED match, so the "
                  f"cost comes out of your pool and has to last every round."
                  if self.ranked else
                  "Casual battle — your skill is free, whatever your energy says."))
        return e

    def _status(self) -> discord.Embed:
        e = discord.Embed(
            title="✨ Choose your avatar skill",
            description=(
                "One skill is live per battle. Pick yours below — the fight "
                "starts as soon as everyone has."),
            colour=0x00E5A0)
        for member, avatar in self.entries:
            slot = self.picked.get(member.id)
            if slot:
                sk = AS.skill_at(avatar, slot) or {}
                state = (f"✅ **{sk.get('name', 'Skill')}** "
                         f"({AS.skill_cost(slot)}⚡)")
            else:
                # Name the fallback rather than saying "waiting": if they never
                # press the button this is what they will actually fight with,
                # and it is better learned here than after the battle.
                cur = AS.chosen_slot({"avatar_skill": {}}, avatar["id"])
                try:
                    from utils.database import get_user
                    cur = AS.chosen_slot(get_user(int(member.id)), avatar["id"])
                except Exception:                        # noqa: BLE001
                    pass
                cur = max(1, min(cur, len(avatar.get("skills") or [])))
                sk = AS.skill_at(avatar, cur) or {}
                state = f"⏳ waiting — currently **{sk.get('name', 'Skill')}**"
            e.add_field(
                name=f"{getattr(member, 'display_name', str(member))}"
                     f"  ·  {avatar['name']}",
                value=f"{state}\n⚡ {self.energy.get(member.id, AS.MAX_ENERGY)}"
                      f"/{AS.MAX_ENERGY}",
                inline=False)
        e.set_footer(text=f"{SKILL_PROMPT_SECONDS}s — anyone who doesn't pick "
                          f"keeps the skill they already had.")
        return e

    async def refresh(self) -> None:
        """Redraw the public message, and stop once everybody has chosen."""
        done = len(self.picked) >= len(self.entries)
        if done:
            for c in self.children:
                c.disabled = True
        if self.message:
            try:
                await self.message.edit(embed=self._status(), view=self)
            except Exception:                            # noqa: BLE001
                pass
        if done:
            self.stop()

    async def on_timeout(self) -> None:
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:                            # noqa: BLE001
                pass


# ── Entry point ──────────────────────────────────────────────────────────────

async def resolve_avatar_skills(ctx, p1, p2, ranked: bool = False) -> None:
    """Ask both players which skill they are taking into the fight.

    Returns when everyone has chosen or the prompt times out. Sends nothing at
    all when neither player has a card with skills, which is most battles.

    Never raises. The picker is a convenience over a stored setting that
    already has a working default, so any failure here degrades to exactly the
    behaviour that shipped before it.
    """
    try:
        entries = participants((p1, p2))
        if not entries:
            return
        view = SkillPromptView(entries, ranked)
        view.message = await ctx.send(embed=view._status(), view=view)
        await view.wait()
        try:
            await view.message.edit(view=None)
        except Exception:                                # noqa: BLE001
            pass
    except Exception as exc:                             # noqa: BLE001
        log.exception("avatar skill prompt failed: %s", exc)
