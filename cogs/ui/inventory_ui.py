"""
cogs/ui/inventory_ui.py
-----------------------
Unified Inventory System — mobile-first UI.

  ;inventory / ;inv [@user]

Design rules (phone-screen optimised):
  • 6 items per page, 2 short lines each — fits one screen, no scrolling
  • Categories: 🌀 Beyblades | 🧑 Avatars | ⚙️ Parts | 🎒 All
  • Nav: ⬅️ Prev | Page x/y (disabled) | ➡️ Next — buttons disabled at edges
  • Item select via dropdown → compact detail view → ⚙️ Equip / 🔄 Change Parts / 🔙 Back
  • Every interaction edits the SAME message (interaction.response.edit_message)
  • Inventory snapshot cached on the view; refreshed only after an equip action
  • Max 3 component rows, ≤5 buttons per row
"""

from __future__ import annotations

import json
import os

import discord
from discord.ext import commands

from utils.database import (
    get_user, update_user, get_beyblade,
    get_avatar_inventory, get_equipped_avatar, set_equipped_avatar,
)
from utils.embeds import RARITY_EMOJIS, rarity_colour
from utils.hp_system import blade_hp_stat, max_hp_for_blade
from utils import info_card
from cogs.economy.shop import PARTS_CATALOG

ITEMS_PER_PAGE = 6
VIEW_TIMEOUT   = 180

TYPE_EMOJI = {"attack": "⚔️", "defense": "🛡️", "stamina": "🌀", "balance": "⚖️"}
CATEGORIES = [("bey", "🌀 Beys"), ("copy", "🧬 Boss Copies"),
              ("avatar", "🧑 Avatars"), ("part", "⚙️ Parts"), ("all", "🎒 All")]

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AVATAR_PATH = os.path.join(_PROJECT_ROOT, "cogs", "avatar", "avatar_data.json")
_avatar_cache: dict[str, dict] | None = None


def _avatar_lookup() -> dict[str, dict]:
    """id → avatar dict, cached at module level (static data)."""
    global _avatar_cache
    if _avatar_cache is None:
        try:
            with open(_AVATAR_PATH, encoding="utf-8") as f:
                raw = json.load(f)
            _avatar_cache = {a["id"]: a for a in raw.get("avatars", [])}
        except Exception:
            _avatar_cache = {}
    return _avatar_cache


def _part_lookup(name: str) -> dict | None:
    low = name.lower()
    return next((p for p in PARTS_CATALOG if p["name"].lower() == low), None)


def _stat_line(st: dict, hp: int | None = None) -> str:
    core = (f"`A{st.get('attack', 0)} D{st.get('defense', 0)} "
            f"S{st.get('stamina', 0)} P{st.get('special', 0)}`")
    return f"{core} `\u2764\ufe0f{hp}`" if hp is not None else core


class InventoryView(discord.ui.View):
    """One persistent view; every press rebuilds components + edits in place."""

    def __init__(self, owner: discord.Member, target: discord.Member):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.owner    = owner              # who may press buttons
        self.target   = target             # whose inventory is shown
        self.can_edit = owner.id == target.id
        self.category = "bey"
        self.page     = 0
        self.detail: dict | None = None    # currently opened item
        self.parts_mode = False            # "Change Parts" sub-view
        self.message: discord.Message | None = None
        self._cache: dict[str, list[dict]] = {}
        self._load_cache()
        self._rebuild()

    # ── Data layer (cached snapshot) ─────────────────────────────────────────

    def _load_cache(self) -> None:
        prof   = get_user(self.target.id)
        # A copy and a database bey can share a display name, so while a copy
        # is equipped no bey should render the ✅ — active_copy is the tiebreak.
        active = ("" if prof.get("active_copy")
                  else str(prof.get("active_beyblade") or "").lower())

        beys = []
        for nm in prof.get("inventory", []):
            name  = nm.get("name") if isinstance(nm, dict) else nm
            blade = get_beyblade(str(name)) or {}
            beys.append({
                "kind": "bey", "name": str(name),
                "rarity": blade.get("rarity", "?"),
                "type": str(blade.get("type", "")).lower(),
                "stats": blade.get("stats", {}),
                "hp": blade_hp_stat(blade),
                "max_hp": max_hp_for_blade(blade),
                "blade": blade,                 # full doc — feeds the info card
                "image": blade.get("image_url"),
                "equipped": str(name).lower() == active,
            })

        # Boss copies are rolled INSTANCES, not entries in beyblades.json, so
        # they can't ride along in prof["inventory"] without breaking equip,
        # sell and trade — all of which resolve names against the database.
        # They get their own kind and their own category tab instead.
        copies = []
        try:
            from cogs.battle.boss import boss_copy as _bc, boss_info as _bi
            active_copy = str(prof.get("active_copy") or "").lower()
            for c in _bc.all_copies(self.target.id):
                src = _bi.REGISTRY.get(c.get("source"))
                if src is None:
                    continue
                d = _bc.describe(c, src)
                copies.append({
                    "kind": "copy", "name": c["name"], "id": c["id"],
                    "grade": c["grade"], "rarity": d["rarity"],
                    "type": str(src.get("type", "")).lower(),
                    "stats": c["stats"], "hp": c.get("card_hp", 0),
                    "total": d["total"], "band": d.get("band"),
                    "emoji": d["emoji"], "colour": d["colour"],
                    "specials": d["specials"], "ultimates": d["ultimates"],
                    "abilities": d["abilities"],
                    "awakening": c.get("awakening"),
                    "awaken_chance": c.get("awaken_chance", 0.0),
                    "loadout": c.get("loadout", ""),
                    "copy": c, "src": src,
                    "equipped": str(c.get("id","")).lower() == active_copy,
                })
        except Exception:
            copies = []

        eq_av   = get_equipped_avatar(self.target.id)
        avatars = []
        for aid in get_avatar_inventory(self.target.id):
            av = _avatar_lookup().get(aid, {})
            avatars.append({
                "kind": "avatar", "name": av.get("name", aid), "id": aid,
                "rarity": av.get("rarity", "?"), "image": av.get("image"),
                "bonuses": av.get("bonuses", {}), "equipped": aid == eq_av,
            })

        equipped_parts = [p.lower() for p in prof.get("equipped_parts", [])]
        parts = []
        for nm in prof.get("parts", []):
            cat = _part_lookup(nm) or {}
            parts.append({
                "kind": "part", "name": nm,
                "ptype": cat.get("type", "?"), "stat": cat.get("stat"),
                "bonus": cat.get("bonus", 0),
                "penalty_stat": cat.get("penalty_stat"),
                "penalty": cat.get("penalty", 0),
                "equipped": nm.lower() in equipped_parts,
            })

        self._cache = {"bey": beys, "copy": copies, "avatar": avatars,
                       "part": parts,
                       "all": beys + copies + avatars + parts}

    def _items(self) -> list[dict]:
        return self._cache.get(self.category, [])

    def _pages(self) -> int:
        return max(1, -(-len(self._items()) // ITEMS_PER_PAGE))

    def _page_items(self) -> list[dict]:
        i = self.page * ITEMS_PER_PAGE
        return self._items()[i:i + ITEMS_PER_PAGE]

    # ── Rendering ─────────────────────────────────────────────────────────────

    @staticmethod
    def _icon(it: dict) -> str:
        if it["kind"] == "bey":
            return TYPE_EMOJI.get(it["type"], "🌀")
        if it["kind"] == "copy":
            return it.get("emoji", "🧬")
        if it["kind"] == "avatar":
            return "🧑"
        return {"driver": "🔩", "disk": "💿", "ring": "💍"}.get(it.get("ptype"), "⚙️")

    def _line(self, idx: int, it: dict) -> str:
        eq = " ✅" if it.get("equipped") else ""
        head = f"**{idx}.** {self._icon(it)} **{it['name']}**{eq}"
        if it["kind"] == "bey":
            em  = RARITY_EMOJIS.get(it["rarity"], "")
            sub = f"{em} {self._stat_sub(it)}"
        elif it["kind"] == "copy":
            lo, hi = it.get("band") or (0, 1)
            sub = (f"{it['grade']} · **{it['total']}** total "
                   f"({lo}-{hi}) · `#{it['id']}`"
                   + ("  ✨" if it.get("awakening") else ""))
        elif it["kind"] == "avatar":
            sub = f"{RARITY_EMOJIS.get(it['rarity'], '')} {it['rarity']}"
        else:
            sub = f"`+{it['bonus']} {str(it.get('stat'))[:3].upper()}`"
            if it.get("penalty"):
                sub += f" `-{it['penalty']} {str(it['penalty_stat'])[:3].upper()}`"
        return f"{head}\n{sub}"

    def _stat_sub(self, it: dict) -> str:
        return _stat_line(it.get("stats", {}), it.get("hp"))

    def build_embed(self) -> discord.Embed:
        if self.detail:
            return self._detail_embed()
        items = self._page_items()
        cat_label = dict(CATEGORIES)[self.category]
        e = discord.Embed(
            title=f"{cat_label} — {self.target.display_name} "
                  f"(Page {self.page + 1}/{self._pages()})",
            description="\n".join(self._line(self.page * ITEMS_PER_PAGE + i + 1, it)
                                  for i, it in enumerate(items)) or "*Nothing here yet!*",
            color=discord.Color.blurple(),
        )
        return e

    def _detail_embed(self) -> discord.Embed:
        it = self.detail
        e  = discord.Embed(title=f"{self._icon(it)} {it['name']}", color=discord.Color.blurple())
        if it["kind"] == "bey":
            e.color = rarity_colour(it["rarity"])
            e.description = (
                f"{RARITY_EMOJIS.get(it['rarity'], '')} {it['rarity']} • "
                f"{it['type'].title()}\n{self._stat_sub(it)}\n"
                f"❤️ **{it.get('hp', '?')} HP** — `{it.get('max_hp', '?')}` battle pool"
                + ("\n✅ **Equipped**" if it["equipped"] else "")
            )
            if it.get("image"):
                e.set_thumbnail(url=it["image"])
        elif it["kind"] == "copy":
            e.color = it.get("colour", discord.Color.blurple())
            lo, hi = it.get("band") or (0, 1)
            st = it["stats"]
            kit = []
            if it["ultimates"]: kit.append("💀 " + ", ".join(it["ultimates"]))
            if it["specials"]:  kit.append("⚡ " + ", ".join(it["specials"]))
            if it["abilities"]: kit.append("◆ " + ", ".join(it["abilities"]))
            if it.get("awakening"):
                kit.append(f"✨ Awakening ({it['awaken_chance'] * 100:.0f}%)")
            e.description = (
                f"{it['emoji']} **{it['grade']}** boss copy · `#{it['id']}`\n"
                f"❤️ {it['hp']} · ⚔️ {st['attack']} · 🛡️ {st['defense']} · "
                f"🔋 {st['stamina']}\n"
                f"**{it['total']}** total  *(band {lo}-{hi})*\n\n"
                + "\n".join(kit)
            )
            e.set_footer(text=f"{it['loadout']}  •  ;copy {it['id']} for the card")
        elif it["kind"] == "avatar":
            bn = it.get("bonuses", {}) or {}
            perks = " ".join(
                f"`{k.split('_')[0][:3].upper()}+{v}`"
                for k, v in bn.items() if v
            ) or "`no bonuses`"
            e.description = (f"{RARITY_EMOJIS.get(it['rarity'], '')} {it['rarity']}\n{perks}"
                             + ("\n✅ **Equipped**" if it["equipped"] else ""))
            if it.get("image"):
                e.set_thumbnail(url=it["image"])
        else:
            e.description = (
                f"{str(it.get('ptype', '?')).title()}\n"
                f"`+{it['bonus']} {str(it.get('stat')).upper()}`"
                + (f" `-{it['penalty']} {str(it['penalty_stat']).upper()}`" if it.get("penalty") else "")
                + ("\n✅ **Equipped**" if it["equipped"] else "")
            )
        return e

    # ── Component construction (≤3 rows, ≤5 buttons/row) ─────────────────────

    def _rebuild(self) -> None:
        self.clear_items()
        if self.detail:
            self._build_detail_components()
        else:
            self._build_list_components()

    def _build_list_components(self) -> None:
        # Row 0 — categories
        for key, label in CATEGORIES:
            b = discord.ui.Button(
                label=label, row=0,
                style=discord.ButtonStyle.primary if key == self.category
                else discord.ButtonStyle.secondary)
            b.callback = self._make_cat_cb(key)
            self.add_item(b)

        # Row 1 — item select (current page only)
        items = self._page_items()
        if items:
            opts = [
                discord.SelectOption(
                    label=f"{self.page * ITEMS_PER_PAGE + i + 1}. {it['name'][:80]}",
                    value=str(i),
                    emoji="✅" if it.get("equipped") else None)
                for i, it in enumerate(items)
            ]
            sel = discord.ui.Select(placeholder="Select an item…", options=opts, row=1)
            sel.callback = self._select_cb
            self.add_item(sel)

        # Row 2 — nav
        prev = discord.ui.Button(label="⬅️", style=discord.ButtonStyle.secondary,
                                 row=2, disabled=self.page <= 0)
        prev.callback = self._prev_cb
        info = discord.ui.Button(label=f"Page {self.page + 1}/{self._pages()}",
                                 style=discord.ButtonStyle.secondary, row=2, disabled=True)
        nxt = discord.ui.Button(label="➡️", style=discord.ButtonStyle.secondary,
                                row=2, disabled=self.page >= self._pages() - 1)
        nxt.callback = self._next_cb
        self.add_item(prev); self.add_item(info); self.add_item(nxt)

    def _build_detail_components(self) -> None:
        it = self.detail
        if self.parts_mode:
            # Part-swap select for owned parts
            owned = self._cache.get("part", [])
            if owned:
                opts = [
                    discord.SelectOption(
                        label=p["name"][:80],
                        description=f"+{p['bonus']} {str(p.get('stat'))[:6]}"[:100],
                        value=str(i),
                        emoji="✅" if p["equipped"] else None)
                    for i, p in enumerate(owned[:25])
                ]
                sel = discord.ui.Select(placeholder="Tap a part to equip/unequip…",
                                        options=opts, row=0)
                sel.callback = self._part_toggle_cb
                self.add_item(sel)
            back = discord.ui.Button(label="🔙 Back", style=discord.ButtonStyle.secondary, row=1)
            back.callback = self._back_from_parts_cb
            self.add_item(back)
            return

        if self.can_edit:
            eq = discord.ui.Button(
                label="✅ Equipped" if it.get("equipped") else "⚙️ Equip",
                style=discord.ButtonStyle.success, row=0,
                disabled=bool(it.get("equipped")))
            eq.callback = self._equip_cb
            self.add_item(eq)
            if it["kind"] == "bey":
                cp = discord.ui.Button(label="🔄 Change Parts",
                                       style=discord.ButtonStyle.primary, row=0)
                cp.callback = self._parts_mode_cb
                self.add_item(cp)
            elif it["kind"] == "copy":
                from cogs.battle.boss import boss_copy as _bc
                worth = _bc.sell_value(it["copy"], it.get("src"))
                sell = discord.ui.Button(label=f"💰 Sell · {worth:,}",
                                         style=discord.ButtonStyle.danger, row=0)
                sell.callback = self._sell_copy_cb
                self.add_item(sell)
        if it["kind"] == "bey":
            card = discord.ui.Button(label="📇 Info Card",
                                     style=discord.ButtonStyle.secondary, row=0)
            card.callback = self._info_card_cb
            self.add_item(card)
        back = discord.ui.Button(label="🔙 Back", style=discord.ButtonStyle.secondary, row=0)
        back.callback = self._back_cb
        self.add_item(back)

    # ── Interaction plumbing ──────────────────────────────────────────────────

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message(
                "This inventory panel belongs to someone else — use `;inv` yourself!",
                ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    def _make_cat_cb(self, key: str):
        async def cb(interaction: discord.Interaction):
            self.category, self.page, self.detail = key, 0, None
            await self._refresh(interaction)
        return cb

    async def _prev_cb(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    async def _next_cb(self, interaction: discord.Interaction):
        self.page = min(self._pages() - 1, self.page + 1)
        await self._refresh(interaction)

    async def _select_cb(self, interaction: discord.Interaction):
        idx = int(interaction.data["values"][0])
        items = self._page_items()
        if 0 <= idx < len(items):
            self.detail = items[idx]
        await self._refresh(interaction)

    async def _back_cb(self, interaction: discord.Interaction):
        self.detail, self.parts_mode = None, False
        await self._refresh(interaction)

    async def _parts_mode_cb(self, interaction: discord.Interaction):
        self.parts_mode = True
        await self._refresh(interaction)

    async def _back_from_parts_cb(self, interaction: discord.Interaction):
        self.parts_mode = False
        self._load_cache()  # parts may have changed
        # keep detail fresh
        self.detail = self._find_refreshed(self.detail)
        await self._refresh(interaction)

    def _find_refreshed(self, old: dict | None) -> dict | None:
        if not old:
            return None
        for it in self._cache.get(old["kind"], []):
            if it["name"] == old["name"]:
                return it
        return None

    async def _info_card_cb(self, interaction: discord.Interaction):
        """Send the full PNG info card as an ephemeral follow-up.

        Ephemeral keeps the panel message intact and stops one person's card
        spam from burying the channel.
        """
        it = self.detail
        if not it or it["kind"] != "bey":
            return await self._refresh(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            buf = await info_card.render_info_card(
                it.get("blade") or {}, parts=self._card_parts())
        except Exception:
            buf = None
        if buf is None:
            return await interaction.followup.send(
                "⚠️ Couldn't render the card right now — "
                f"try `;info {it['name']}` instead.", ephemeral=True)
        fname = f"{it['name'].lower().replace(' ', '_')}_card.png"
        await interaction.followup.send(file=discord.File(buf, filename=fname),
                                        ephemeral=True)

    def _card_parts(self) -> dict:
        """Owner's equipped disk/driver mapped onto the card's Ratchet/Bit slots."""
        by_type: dict[str, str] = {}
        for p in self._cache.get("part", []):
            if p.get("equipped"):
                by_type[p.get("ptype")] = p["name"]
        return {"ratchet": by_type.get("disk") or by_type.get("ring"),
                "bit":     by_type.get("driver")}

    # ── Equip actions ─────────────────────────────────────────────────────────

    async def _equip_cb(self, interaction: discord.Interaction):
        it = self.detail
        if not (it and self.can_edit):
            return await self._refresh(interaction)
        if it["kind"] == "bey":
            prof = get_user(self.owner.id)
            prof["active_beyblade"] = it["name"]
            # Clearing the copy pointer is what actually swaps back to a
            # database blade — leaving it set would keep the copy equipped
            # while the panel showed the bey's name.
            prof["active_copy"] = None
            update_user(self.owner.id, prof)
        elif it["kind"] == "copy":
            from cogs.battle.boss import boss_copy as _bc
            _bc.equip(self.owner.id, it["id"])
        elif it["kind"] == "avatar":
            set_equipped_avatar(self.owner.id, it.get("id"))
        else:
            self._toggle_part(it["name"])
        self._load_cache()
        self.detail = self._find_refreshed(it)
        await self._refresh(interaction)

    async def _sell_copy_cb(self, interaction: discord.Interaction):
        it = self.detail
        if not (it and self.can_edit and it.get("kind") == "copy"):
            return await self._refresh(interaction)
        from cogs.battle.boss import boss_copy as _bc
        value = _bc.sell_value(it["copy"], it.get("src"))
        gone  = _bc.remove_copy(self.owner.id, it["id"])
        if gone is None:
            return await interaction.response.send_message(
                "That copy is already gone.", ephemeral=True)
        prof = get_user(self.owner.id)
        prof["coins"] = prof.get("coins", 0) + value
        update_user(self.owner.id, prof)
        self._load_cache()
        self.detail = None
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        await interaction.followup.send(
            f"🧬 Sold **{gone['name']}** ({gone['grade']} · `#{gone['id']}`) "
            f"for 🪙 **{value:,}**.", ephemeral=True)

    def _toggle_part(self, part_name: str) -> None:
        """Equip a part (replacing any same-type part) or unequip if already on."""
        prof     = get_user(self.owner.id)
        equipped = prof.get("equipped_parts", [])
        cat      = _part_lookup(part_name) or {}
        ptype    = cat.get("type")
        if part_name.lower() in (p.lower() for p in equipped):
            equipped = [p for p in equipped if p.lower() != part_name.lower()]
        else:
            # replace existing part of the same slot type
            equipped = [p for p in equipped
                        if (_part_lookup(p) or {}).get("type") != ptype]
            equipped.append(part_name)
        prof["equipped_parts"] = equipped
        update_user(self.owner.id, prof)

    async def _part_toggle_cb(self, interaction: discord.Interaction):
        idx   = int(interaction.data["values"][0])
        owned = self._cache.get("part", [])
        if 0 <= idx < len(owned):
            self._toggle_part(owned[idx]["name"])
            self._load_cache()
        await self._refresh(interaction)

    async def on_timeout(self) -> None:
        for c in self.children:
            c.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass


class InventoryUICog(commands.Cog, name="Inventory"):
    """Unified, mobile-optimised inventory browser."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.guild_only()
    @commands.command(name="inventory", aliases=["inv", "items", "bag"],
                      brief="Browse your inventory 🎒")
    async def inventory(self, ctx: commands.Context,
                        member: discord.Member | None = None) -> None:
        target = member or ctx.author
        view   = InventoryView(ctx.author, target)
        msg    = await ctx.send(embed=view.build_embed(), view=view)
        view.message = msg


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InventoryUICog(bot))
