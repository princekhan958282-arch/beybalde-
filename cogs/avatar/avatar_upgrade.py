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
from discord.ext import commands

from utils.database import get_user, mutate_user
from .avatar_engine import avatar_engine
from .avatar_utils import format_type, RARITY_COLORS, RARITY_EMOJI
from . import avatar_levels as AL
from . import avatar_progress as AP
from . import avatar_skills as ASK
from utils.mobile_ui import bar as _bar

log = logging.getLogger("beyblade_bot")


async def _resolve(query: Optional[str], user_id: int) -> Optional[dict]:
    """Find an avatar by id or name; with no query, use whatever is equipped."""
    if not query:
        equipped = await avatar_engine.get_equipped_avatar_id(user_id)
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
            result = await mutate_user(
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

        after = int((await get_user(self.buyer_id)).get("coins", 0) or 0)
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


def _can_refill(profile: dict) -> bool:
    """Is the Refill button worth showing at all?

    Hidden when full or mid-match — the purchase would be refused either way,
    and a button that always says no is worse than no button. Deliberately does
    NOT check the balance: somebody who cannot afford it should still see the
    price, which is how they learn there is something to save for.
    """
    return (ASK.energy(profile) < ASK.MAX_ENERGY
            and not ASK.in_ranked_match(profile))


def energy_line(profile: dict) -> str:
    """'⚡ `████░░░░` 50/100 · next +25 in 3 min · full in 13 min'.

    The times are Discord `<t:…:R>` stamps, so every viewer sees them in their
    own timezone and they keep counting down without the message being edited.
    """
    pool = ASK.energy(profile)
    line = (f"⚡ `{_bar(pool, ASK.MAX_ENERGY)}` **{pool}/{ASK.MAX_ENERGY}**")
    if ASK.in_ranked_match(profile):
        return line + "  ·  🔒 frozen — ranked match in progress"
    if pool >= ASK.MAX_ENERGY:
        return line + "  ·  full"
    nxt, full = ASK.next_tick_at(profile), ASK.full_at(profile)
    parts = []
    if nxt:
        parts.append(f"next +{ASK.ENERGY_REGEN_AMOUNT} <t:{int(nxt)}:R>")
    if full and full != nxt:
        parts.append(f"full <t:{int(full)}:R>")
    return line + ("  ·  " + "  ·  ".join(parts) if parts else "")


def build_skill_embed(profile: dict, card: dict) -> discord.Embed:
    """The `;askill` panel: energy, the three prices, and which one is live."""
    skills = card.get("skills") or []
    pool = ASK.energy(profile)
    chosen = max(1, min(ASK.chosen_slot(profile, card["id"]), len(skills)))

    e = discord.Embed(
        title=f"{RARITY_EMOJI.get(card.get('rarity'), '⚪')} "
              f"{card['name']} — battle skill",
        description=energy_line(profile),
        colour=RARITY_COLORS.get(card.get("rarity"), 0xAAAAAA))

    for i, sk in enumerate(skills, 1):
        cost = ASK.skill_cost(i)
        mark = "✅" if i == chosen else "▫️"
        # Affordability is a RANKED statement only — casual grants any skill
        # whatever the pool says — so it is worded that way rather than as a
        # flat "not enough energy" that would be wrong half the time.
        afford = "" if pool >= cost else "  ·  ⚠️ ranked can't afford this yet"
        e.add_field(
            name=f"{mark} {i}. {sk.get('name', 'Skill')}  —  {cost}⚡"
                 f"  ({ASK.uses_affordable(i)}× per full pool){afford}",
            value=sk.get("description", ""),
            inline=False)

    e.set_footer(
        text=f"One skill per battle. Casual is free — it never spends energy. "
             f"Ranked spends, and does not top you up when a match starts. "
             f"Recovery is +{ASK.ENERGY_REGEN_AMOUNT} every "
             f"{ASK.ENERGY_REGEN_SECONDS // 60} min between matches.  "
             f";askill <1-3>")
    return e


class EnergyRefill(discord.ui.View):
    """A single 'Refill — 🪙 40,000' button attached to the ;askill panel."""

    def __init__(self, buyer_id: int, card: Optional[dict] = None) -> None:
        super().__init__(timeout=60)
        self.buyer_id = buyer_id
        self.card = card
        self.message: Optional[discord.Message] = None
        self.children[0].label = f"Refill — 🪙 {ASK.ENERGY_REFILL_PRICE:,}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.buyer_id:
            await interaction.response.send_message(
                "That's not your energy.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Refill", style=discord.ButtonStyle.success)
    async def refill(self, interaction: discord.Interaction,
                     _button: discord.ui.Button) -> None:
        # The balance and the pool are both re-read inside buy_refill, under
        # the profile lock. The numbers printed on the panel are a display and
        # were never an authority — the card can sit for 60 seconds.
        try:
            result = ASK.buy_refill_for(self.buyer_id)
        except ASK.RefillError as exc:
            return await interaction.response.send_message(
                f"❌ {exc}", ephemeral=True)
        except Exception as exc:                         # noqa: BLE001
            log.exception("energy refill failed for %s: %s", self.buyer_id, exc)
            return await interaction.response.send_message(
                "❌ Something went wrong. Nothing was charged.", ephemeral=True)

        for c in self.children:
            c.disabled = True
        prof = await get_user(self.buyer_id)
        e = discord.Embed(
            title="⚡ Energy refilled",
            description=(f"{result['from']} → **{ASK.MAX_ENERGY}**\n"
                         f"Spent 🪙 {result['spent']:,} · "
                         f"balance 🪙 {result['coins_after']:,}"),
            colour=0x2ECC71)
        if self.card:
            e.add_field(name="Ready", value=build_skill_embed(
                prof, self.card).description, inline=False)
        await interaction.response.edit_message(embed=e, view=self)
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
        card = await _resolve(avatar, ctx.author.id)
        if card is None:
            return await ctx.send(
                "❌ No avatar found. Equip one with `;equipavatar <id>`, or "
                "name it: `;aup Argus`.")

        prof = await get_user(ctx.author.id)
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
        card = await _resolve(avatar, ctx.author.id)
        if card is None:
            return await ctx.send("❌ No avatar found. Name it: `;areset Argus`.")

        prof = await get_user(ctx.author.id)
        spent = AP.total_spent(prof, card["id"])
        if spent <= 0:
            return await ctx.send(
                f"You haven't spent anything on **{card['name']}** — "
                f"nothing to refund.")

        result = await mutate_user(ctx.author.id,
                             lambda p: AP.apply_reset(p, card["id"]))
        await ctx.send(embed=discord.Embed(
            title=f"↩️ {card['name']} reset to Lv1",
            description=f"Spent 🪙 {result['spent']:,} · "
                        f"refunded 🪙 **{result['refund']:,}** (70%)",
            colour=0x99AAB5))

    # ── ;avatarskill ──────────────────────────────────────────────────────────
    @commands.command(name="avatarskill", aliases=["askill", "askills"])
    async def avatar_skill(self, ctx: commands.Context,
                           slot: Optional[int] = None, *,
                           avatar: Optional[str] = None) -> None:
        """Pick which of the three skills your avatar fights with.

        With no slot it shows the card, the three prices and what your energy
        currently affords — which is the question that actually matters in a
        ranked match, where the 100 has to last the whole thing.
        """
        card = await _resolve(avatar, ctx.author.id)
        if card is None:
            return await ctx.send(
                "❌ No avatar found. Equip one with `;equipavatar <id>`, or "
                "name it: `;askill 2 Argus`.")

        skills = card.get("skills") or []
        if not skills:
            return await ctx.send(
                f"**{card['name']}** has no signature skills — its bonuses "
                f"always apply in full, and cost no energy.")

        # Settle recovery before anything is printed, so the number on the card
        # is the number the next battle will charge against.
        await ASK.accrue_for(ctx.author.id)
        prof = await get_user(ctx.author.id)

        if slot is not None:
            if not 1 <= int(slot) <= len(skills):
                return await ctx.send(
                    f"❌ **{card['name']}** has skills 1–{len(skills)}.")
            if ASK.in_ranked_match(prof):
                return await ctx.send(
                    "❌ You're mid-way through a ranked match — the pick is "
                    "locked until it finishes.")
            await ASK.set_choice(ctx.author.id, card["id"], int(slot))
            prof = await get_user(ctx.author.id)

        e = build_skill_embed(prof, card)
        view = EnergyRefill(ctx.author.id, card) if _can_refill(prof) else None
        msg = await ctx.send(embed=e, view=view)
        if view is not None:
            view.message = msg

    # ── ;energyrefill ─────────────────────────────────────────────────────────
    @commands.command(name="energyrefill", aliases=["erefill", "arefill"])
    async def energy_refill(self, ctx: commands.Context) -> None:
        """Top your avatar energy back to full for coins, instead of waiting."""
        await ASK.accrue_for(ctx.author.id)
        prof = await get_user(ctx.author.id)

        if not _can_refill(prof):
            return await ctx.send(embed=discord.Embed(
                description=energy_line(prof)
                + ("\n\nNothing to buy — you're already full."
                   if ASK.energy(prof) >= ASK.MAX_ENERGY else
                   "\n\nEnergy is frozen until your ranked match is decided."),
                colour=0x99AAB5))

        pool = ASK.energy(prof)
        coins = int(prof.get("coins", 0) or 0)
        e = discord.Embed(
            title="⚡ Refill avatar energy?",
            description=(f"{energy_line(prof)}\n\n"
                         f"Refill to **{ASK.MAX_ENERGY}** for "
                         f"🪙 **{ASK.ENERGY_REFILL_PRICE:,}**\n"
                         f"Balance after: 🪙 "
                         f"{coins - ASK.ENERGY_REFILL_PRICE:,}"),
            colour=0xF1C40F if coins >= ASK.ENERGY_REFILL_PRICE else 0xED4245)
        if coins < ASK.ENERGY_REFILL_PRICE:
            e.description = (f"{energy_line(prof)}\n\nA refill costs 🪙 "
                             f"**{ASK.ENERGY_REFILL_PRICE:,}**. You have 🪙 "
                             f"{coins:,} — "
                             f"{ASK.ENERGY_REFILL_PRICE - coins:,} short.")
            return await ctx.send(embed=e)
        e.set_footer(text=f"Recovery is free: +{ASK.ENERGY_REGEN_AMOUNT} every "
                          f"{ASK.ENERGY_REGEN_SECONDS // 60} minutes. "
                          f"You only need this if you want it now. "
                          f"Currently {pool}/{ASK.MAX_ENERGY}.")

        view = EnergyRefill(ctx.author.id)
        view.message = await ctx.send(embed=e, view=view)

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


# The `/avatar` group lived here — five subcommands, and Discord lists them
# FLAT in the picker, so five lines in front of every player. v1.14 replaced it
# with one `/avatar` command opening a panel; see `cogs/ui/panels.py:AvatarSpec`,
# which invokes the prefix commands above. The owned-card autocomplete is gone
# with it: the panel asks for a card in its modal, and every prefix command
# already resolves a blank card to the equipped one.


# No setup() here on purpose. cogs/avatar/__init__.py adds both cogs, and this
# module is not in app.py's COGS list — giving it its own entry point as well
# would let a stray load_extension register /avatar twice and fail the boot
# with CommandAlreadyRegistered.
