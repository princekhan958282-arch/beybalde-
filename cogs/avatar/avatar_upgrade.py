"""
avatar_upgrade.py — buying avatar card levels.

    ;avatarupgrade [avatar] [levels]   / ;aup
    ;avatarreset   <avatar>            — refund 70% of what was actually spent
    /avatar upgrade | reset | view

Coins are involved, so this is the one flow that cannot be sloppy. Four rules,
each of which exists because the alternative is a bug you cannot recover from
without logs:

1. **Confirm before charging.** A card shows current level → new level, exact
   cost, and the balance afterwards. No purchase happens on the first command.

2. **The balance is re-read inside the confirm handler.** The card can sit for
   60 seconds and the player can spend in the casino meanwhile, so the number
   printed on it is a display, never an authority.

3. **Deduct and grant in ONE write.** `database.mutate_user` holds the lock
   across read-modify-write, and `PurchaseError` aborts it with nothing
   persisted. There is no interleaving where coins leave without the level
   arriving.

4. **A blocked upgrade names its reason.** "Skill capped at Lv4 — raise the
   avatar to Lv3 first", not a silent no-op on a greyed button.

Skill levels are stored and displayed but not yet purchasable. The 29 authored
cards carry their `skills` as name+description text with no mechanical binding —
the real effects are flattened into `bonuses` — so there is nothing for a skill
level to scale yet. Wiring a purchase to a number that changes nothing would be
worse than not shipping it. That binding is the skills phase.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import get_user, mutate_user
from .avatar_engine import avatar_engine
from .avatar_utils import format_type, RARITY_COLORS, RARITY_EMOJI
from . import avatar_levels as AL
from . import avatar_progress as AP

log = logging.getLogger("beyblade_bot")


def _resolve(query: Optional[str], user_id: int) -> Optional[dict]:
    """Find an avatar by id or name; with no query, use whatever is equipped."""
    if not query:
        equipped = avatar_engine.get_equipped_avatar_id(user_id)
        return avatar_engine.get_avatar(equipped) if equipped else None
    q = str(query).strip().lower()
    exact = avatar_engine.get_avatar(q)
    if exact:
        return exact
    for a in avatar_engine.get_all_avatars():
        if a.get("name", "").lower() == q or a.get("id", "").lower() == q:
            return a
    partial = [a for a in avatar_engine.get_all_avatars()
               if q in a.get("name", "").lower()]
    return partial[0] if len(partial) == 1 else None


class ConfirmUpgrade(discord.ui.View):
    """Confirm/cancel for one purchase, locked to the buyer."""

    def __init__(self, buyer_id: int, avatar: dict, levels: int) -> None:
        super().__init__(timeout=60)
        self.buyer_id = buyer_id
        self.avatar = avatar
        self.levels = levels
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.buyer_id:
            await interaction.response.send_message(
                "That's not your upgrade.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="🪙")
    async def confirm(self, interaction: discord.Interaction,
                      _button: discord.ui.Button) -> None:
        avatar_id = self.avatar["id"]

        # Rule 2 + 3: the balance is re-read under the lock, and the deduction
        # and the level land in the same write or neither does.
        try:
            result = mutate_user(
                self.buyer_id,
                lambda prof: AP.apply_card_purchase(prof, avatar_id, self.levels))
        except AP.PurchaseError as exc:
            for c in self.children:
                c.disabled = True
            return await interaction.response.edit_message(
                embed=discord.Embed(title="❌ Not bought",
                                    description=str(exc), colour=0xED4245),
                view=self)
        except Exception as exc:                         # noqa: BLE001
            log.exception("avatar upgrade failed for %s: %s", self.buyer_id, exc)
            return await interaction.response.edit_message(
                embed=discord.Embed(
                    title="❌ Something went wrong",
                    description="Nothing was charged. Try again.",
                    colour=0xED4245),
                view=None)

        after = int(get_user(self.buyer_id).get("coins", 0) or 0)
        gain = AL.card_stat_bonus(self.avatar.get("type"), result["to"])
        gains = ", ".join(f"+{v} {k[:3].upper()}" for k, v in gain.items() if v)

        e = discord.Embed(
            title=f"✅ {self.avatar['name']} is now Lv{result['to']}",
            colour=RARITY_COLORS.get(self.avatar.get("rarity"), 0x2ECC71))
        e.add_field(name="Spent", value=f"🪙 {result['cost']:,}", inline=True)
        e.add_field(name="Balance", value=f"🪙 {after:,}", inline=True)
        e.add_field(name="Now worth", value=gains or "—", inline=False)
        if result["skill_cap_after"] > result["skill_cap_now"]:
            e.add_field(name="Unlocked",
                        value=f"Skills can now reach Lv{result['skill_cap_after']}",
                        inline=False)
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(embed=e, view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction,
                     _button: discord.ui.Button) -> None:
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(title="Cancelled — nothing was charged.",
                                colour=0x99AAB5),
            view=self)
        self.stop()

    async def on_timeout(self) -> None:
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:                            # noqa: BLE001
                pass


class AvatarUpgrade(commands.Cog, name="Avatar Upgrade"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── ;avatarupgrade ────────────────────────────────────────────────────────
    @commands.command(name="avatarupgrade", aliases=["aup", "avatarlevel"])
    async def avatar_upgrade(self, ctx: commands.Context,
                             avatar: Optional[str] = None,
                             levels: int = 1) -> None:
        """Buy levels for an avatar card. Defaults to the one you have equipped."""
        card = _resolve(avatar, ctx.author.id)
        if card is None:
            return await ctx.send(
                "❌ No avatar found. Equip one with `;equipavatar <id>`, or "
                "name it: `;aup Argus`.")

        prof = get_user(ctx.author.id)
        from utils.database import player_owns_avatar
        if not player_owns_avatar(ctx.author.id, card["id"]):
            return await ctx.send(f"❌ You don't own **{card['name']}**.")

        q = AP.quote_card(prof, card["id"], levels)
        if q["maxed"]:
            return await ctx.send(
                f"**{card['name']}** is already at the maximum, "
                f"Lv{AL.MAX_CARD_LEVEL}.")
        if q["coins"] < q["cost"]:
            return await ctx.send(
                f"❌ Lv{q['from']} → Lv{q['to']} costs 🪙 **{q['cost']:,}**. "
                f"You have 🪙 {q['coins']:,} — {q['cost'] - q['coins']:,} short.")

        now_gain = AL.card_stat_bonus(card.get("type"), q["from"])
        new_gain = AL.card_stat_bonus(card.get("type"), q["to"])
        delta = ", ".join(f"+{new_gain[k] - now_gain[k]} {k[:3].upper()}"
                          for k in new_gain if new_gain[k] - now_gain[k])

        e = discord.Embed(
            title=f"{RARITY_EMOJI.get(card.get('rarity'), '⚪')} "
                  f"Upgrade {card['name']}?",
            description=f"{format_type(card.get('type'))} · "
                        f"**Lv{q['from']} → Lv{q['to']}**",
            colour=RARITY_COLORS.get(card.get("rarity"), 0xAAAAAA))
        e.add_field(name="Cost", value=f"🪙 {q['cost']:,}", inline=True)
        e.add_field(name="Balance after",
                    value=f"🪙 {q['coins'] - q['cost']:,}", inline=True)
        e.add_field(name="Gains", value=delta or "—", inline=False)
        if q["skill_cap_after"] > q["skill_cap_now"]:
            e.add_field(name="Also unlocks",
                        value=f"Skill cap Lv{q['skill_cap_now']} → "
                              f"Lv{q['skill_cap_after']}",
                        inline=False)
        e.set_footer(text="Confirm within 60s. Your balance is re-checked then.")

        view = ConfirmUpgrade(ctx.author.id, card, levels)
        view.message = await ctx.send(embed=e, view=view)

    # ── ;avatarreset ──────────────────────────────────────────────────────────
    @commands.command(name="avatarreset", aliases=["areset"])
    async def avatar_reset(self, ctx: commands.Context, *,
                           avatar: Optional[str] = None) -> None:
        """Drop an avatar to Lv1 and refund 70% of what you actually spent."""
        card = _resolve(avatar, ctx.author.id)
        if card is None:
            return await ctx.send("❌ No avatar found. Name it: `;areset Argus`.")

        prof = get_user(ctx.author.id)
        spent = AP.total_spent(prof, card["id"])
        if spent <= 0:
            return await ctx.send(
                f"You haven't spent anything on **{card['name']}** — "
                f"nothing to refund.")

        result = mutate_user(ctx.author.id,
                             lambda p: AP.apply_reset(p, card["id"]))
        await ctx.send(embed=discord.Embed(
            title=f"↩️ {card['name']} reset to Lv1",
            description=f"Spent 🪙 {result['spent']:,} · "
                        f"refunded 🪙 **{result['refund']:,}** (70%)",
            colour=0x99AAB5))

    # ── ;avatarcost ───────────────────────────────────────────────────────────
    @commands.command(name="avatarcost", aliases=["acost"])
    async def avatar_cost(self, ctx: commands.Context) -> None:
        """The whole upgrade curve, so nobody has to buy one to see the next."""
        e = discord.Embed(title="🪙 Avatar upgrade costs", colour=0xF1C40F)
        rows = []
        cum = 0
        for lvl in range(1, AL.MAX_CARD_LEVEL):
            step = AL.card_level_cost(lvl, lvl + 1)
            cum += step
            rows.append(f"Lv{lvl} → Lv{lvl + 1}　🪙 {step:>7,}　(total {cum:,})")
        e.add_field(name="Card levels", value="```\n" + "\n".join(rows) + "\n```",
                    inline=False)

        growth = "\n".join(
            f"{format_type(t)}  Lv5: " +
            ", ".join(f"+{v} {k[:3].upper()}"
                      for k, v in AL.card_stat_bonus(t, 5).items())
            for t in AL.TYPES)
        e.add_field(name="What a maxed card is worth", value=growth, inline=False)
        e.set_footer(text=f"A card at Lv1 gives no level bonus — only its own "
                          f"printed stats. Full card incl. skills: "
                          f"{AL.full_card_cost():,}")
        await ctx.send(embed=e)


class AvatarUpgradeCommands(commands.Cog, name="Avatar (slash)"):
    """Slash entry points, delegating to the prefix commands rather than
    duplicating their logic — a second copy is how the two paths drift."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    avatar = app_commands.Group(name="avatar",
                                description="Avatar cards — levels and upgrades")

    async def _run(self, interaction: discord.Interaction, command_name: str,
                   *args, **kwargs) -> None:
        cmd = self.bot.get_command(command_name)
        if cmd is None:
            return await interaction.response.send_message(
                f"`{command_name}` isn't loaded right now.", ephemeral=True)
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.invoke(cmd, *args, **kwargs)

    async def _owned_autocomplete(self, interaction: discord.Interaction,
                                  current: str):
        try:
            from utils.database import get_avatar_inventory
            owned = set(get_avatar_inventory(interaction.user.id))
        except Exception:                                # noqa: BLE001
            owned = set()
        cur = (current or "").lower()
        out = []
        for a in avatar_engine.get_all_avatars():
            if a["id"] not in owned:
                continue
            if cur and cur not in a["name"].lower() and cur not in a["id"].lower():
                continue
            out.append(app_commands.Choice(name=f"{a['name']} ({a['rarity']})",
                                           value=a["id"]))
            if len(out) >= 25:
                break
        return out

    @avatar.command(name="upgrade", description="Buy levels for an avatar card")
    @app_commands.describe(avatar="Which card (defaults to the one equipped)",
                           levels="How many levels to buy at once")
    @app_commands.autocomplete(avatar=_owned_autocomplete)
    async def a_upgrade(self, interaction: discord.Interaction,
                        avatar: Optional[str] = None, levels: int = 1) -> None:
        await self._run(interaction, "avatarupgrade", avatar=avatar, levels=levels)

    @avatar.command(name="reset",
                    description="Reset a card to Lv1 and refund 70% of the spend")
    @app_commands.describe(avatar="Which card")
    @app_commands.autocomplete(avatar=_owned_autocomplete)
    async def a_reset(self, interaction: discord.Interaction,
                      avatar: str) -> None:
        await self._run(interaction, "avatarreset", avatar=avatar)

    @avatar.command(name="costs", description="The full avatar upgrade curve")
    async def a_costs(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, "avatarcost")


# No setup() here on purpose. cogs/avatar/__init__.py adds both cogs, and this
# module is not in app.py's COGS list — giving it its own entry point as well
# would let a stray load_extension register /avatar twice and fail the boot
# with CommandAlreadyRegistered.
