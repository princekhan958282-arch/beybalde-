"""
cogs/ui/inventory_ui.py
-----------------------
Unified Inventory System — mobile-first UI.

  ;inventory / ;inv [@user]

Design rules (phone-screen optimised):
  • 6 items per page, 2 short lines each — fits one screen, no scrolling
  • Categories: 🌀 Beyblades | 🧑 Avatars | ⚙️ Parts | 🎒 All
  • Nav: ⏮ ⬅️ Page x/y ➡️ ⏭ — buttons disabled at the edges
  • Item select via dropdown → compact detail view → ⚙️ Equip / 🔄 Change Parts / 🔙 Back
  • Every interaction edits the SAME message (interaction.response.edit_message)
  • Inventory snapshot cached on the view; refreshed only after an equip action
  • Max 5 component rows, ≤5 buttons per row, ≤25 options per select

Built for the 2,000-slot ceiling, not the six beys most players have
--------------------------------------------------------------------
At 6 items a page with prev/next only, a full 2,000-bey collection is **334
pages** and the last one is 333 taps away. Three things fix that, and only
together:

  • **A rarity filter.** The reason anyone opens a 2,000-item list is to find
    one blade, and rarity is the axis they think in. Filtering to Ultimate
    turns 334 pages into one.
  • **A page-jump select.** 25 evenly-spread destinations, always including
    the first page, the last page and wherever you already are — so any page
    in a 334-page collection is at most two taps away.
  • **A bigger page on the bey tab.** 12 rather than 6: the bey line is the
    shortest of the four kinds, and this halves the page count outright.
    The other tabs keep 6, because avatars and parts are counted in dozens.

The filter and the jump each cost a component row, which is why the row budget
above went from 3 to 5. That is the ceiling — there is no sixth row.
"""

from __future__ import annotations

import json
import os

import discord
from discord.ext import commands

from utils.database import (
    get_user, update_user, beyblade_ref,
    get_avatar_inventory, get_equipped_avatar, set_equipped_avatar,
)
from utils.embeds import RARITY_EMOJIS, rarity_colour
from utils.mobile_ui import trunc as _trunc
from utils.hp_system import blade_hp_stat, max_hp_for_blade
from utils import info_card
from cogs.economy.shop import PARTS_CATALOG
from cogs.economy.shop import part_penalties as _part_penalties

ITEMS_PER_PAGE = 6
VIEW_TIMEOUT   = 180

# Per-tab page size. Beys get the big page — their line is two short ones and
# a page of 12 is ~900 characters against Discord's 4,096-character embed
# description limit, so there is headroom even at the longest blade names.
PAGE_SIZE = {"bey": 12, "all": 12, "copy": 8}

# Discord's hard limit on a select. Both the item list and the page jump are
# built against it, so neither can silently drop its tail.
MAX_SELECT_OPTIONS = 25

# Filterable tabs are the ones whose items carry a rarity. Parts do not.
RARITY_TABS = ("bey", "copy", "avatar", "all")

# The "no filter" option's value. It CANNOT be the empty string: Discord
# requires a select option's value to be 1–100 characters and rejects the whole
# message with a 400 at send time, so `;inv` did not degrade — it stopped
# opening at all for anyone holding two or more rarities.
#
# `__all__` is this codebase's existing sentinel for exactly this, in
# `profile.RaritySelect` (the `;list` rarity filter) and in
# `achievements.py`. Same spelling here so there is one idiom, not three.
ALL_RARITIES = "__all__"

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
        self.rarity: str | None = None     # active rarity filter, None = all
        self.detail: dict | None = None    # currently opened item
        self.parts_mode = False            # "Change Parts" sub-view
        self.message: discord.Message | None = None
        self._cache: dict[str, list[dict]] = {}
        self._view_cache: dict[tuple, list[dict]] = {}
        self._slots: tuple[int, int] | None = None   # (used, capacity)
        # `_load_cache` now awaits the profile fetch (BUG-02) and `__init__`
        # can't await — call `InventoryView.create(...)` instead.

    @classmethod
    async def create(cls, owner: discord.Member, target: discord.Member) -> "InventoryView":
        view = cls(owner, target)
        await view._load_cache()
        view._rebuild()
        return view

    # ── Data layer (cached snapshot) ─────────────────────────────────────────

    async def _load_cache(self) -> None:
        prof   = await get_user(self.target.id)
        # Read once, here, off the profile this method already loaded — the
        # footer must not put a store read on every re-render.
        from utils.inventory import capacity as _cap, used as _used
        self._slots = (_used(prof), _cap(prof))
        # A copy and a database bey can share a display name, so while a copy
        # is equipped no bey should render the ✅ — active_copy is the tiebreak.
        active = ("" if prof.get("active_copy")
                  else str(prof.get("active_beyblade") or "").lower())

        beys = []
        # `beyblade_ref` hands back the SHARED cached record instead of a
        # deepcopy. `get_beyblade` copies ~0.05 ms a record, which is nothing
        # once and ~100 ms for a two-thousand-item inventory — synchronously,
        # on the event loop, every time this panel opens. Nothing below writes
        # to `blade`; it is read for display and handed to the info card,
        # which also only reads.
        for nm in prof.get("inventory", []):
            name  = nm.get("name") if isinstance(nm, dict) else nm
            blade = beyblade_ref(str(name)) or {}
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
            for c in await _bc.all_copies(self.target.id):
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

        eq_av   = await get_equipped_avatar(self.target.id)
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
                # Every stat this part reduces, not just the first. A part can
                # carry more than one penalty and the singular pair could only
                # ever show one of them.
                "penalties": _part_penalties(cat) if cat else {},
                "equipped": nm.lower() in equipped_parts,
            })

        self._cache = {"bey": beys, "copy": copies, "avatar": avatars,
                       "part": parts,
                       "all": beys + copies + avatars + parts}
        # The filtered view is derived from `_cache`, so it dies with it.
        self._view_cache = {}

    # ── Paging and filtering ─────────────────────────────────────────────────

    def _all_items(self) -> list[dict]:
        """The whole tab, before the rarity filter — what the counts are of."""
        return self._cache.get(self.category, [])

    def _items(self) -> list[dict]:
        """The tab as filtered. Memoised: this is called five times a render,
        and at 2,000 items an un-memoised filter is 10,000 comparisons a tap."""
        key = (self.category, self.rarity)
        hit = self._view_cache.get(key)
        if hit is None:
            items = self._all_items()
            if self.rarity:
                items = [it for it in items if it.get("rarity") == self.rarity]
            self._view_cache[key] = hit = items
        return hit

    def _per_page(self) -> int:
        return PAGE_SIZE.get(self.category, ITEMS_PER_PAGE)

    def _pages(self) -> int:
        return max(1, -(-len(self._items()) // self._per_page()))

    def _clamp_page(self) -> None:
        """Keep `page` inside the current view.

        Filtering 334 pages down to 2 while sitting on page 300 would otherwise
        render an empty list with both nav buttons disabled — a dead panel.
        """
        self.page = max(0, min(self.page, self._pages() - 1))

    def _page_items(self) -> list[dict]:
        per = self._per_page()
        i   = self.page * per
        return self._items()[i:i + per]

    def _rarities(self) -> list[tuple[str, int]]:
        """Rarities present in this tab, in roster order, with counts."""
        counts: dict[str, int] = {}
        for it in self._all_items():
            r = it.get("rarity")
            if r and r != "?":
                counts[r] = counts.get(r, 0) + 1
        order = list(RARITY_EMOJIS)
        return sorted(counts.items(),
                      key=lambda kv: (order.index(kv[0])
                                      if kv[0] in order else len(order)))

    def _jump_targets(self) -> list[int]:
        """Up to 25 page indices to offer as jump destinations.

        Under 25 pages every page is offered. Above it they are spread evenly
        across the range — and the first page, the last page and the page you
        are on are forced in, so the select can never fail to show where you
        already are or refuse to take you to the end.
        """
        pages = self._pages()
        if pages <= MAX_SELECT_OPTIONS:
            return list(range(pages))
        last = pages - 1
        step = last / (MAX_SELECT_OPTIONS - 1)
        spread = {round(i * step) for i in range(MAX_SELECT_OPTIONS)}
        spread |= {0, last, self.page}
        out = sorted(spread)
        # Forcing the current page in can push the count to 26 or 27; drop
        # from the middle, never the ends, and never the page you are on.
        while len(out) > MAX_SELECT_OPTIONS:
            drop = next((p for p in out[1:-1] if p != self.page), None)
            if drop is None:
                break
            out.remove(drop)
        return out

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
            for _stat, _amt in (it.get("penalties") or {}).items():
                sub += f" `-{_amt} {str(_stat)[:3].upper()}`"
        return f"{head}\n{sub}"

    def _stat_sub(self, it: dict) -> str:
        return _stat_line(it.get("stats", {}), it.get("hp"))

    def build_embed(self) -> discord.Embed:
        if self.detail:
            return self._detail_embed()
        self._clamp_page()
        items = self._page_items()
        per   = self._per_page()
        cat_label = dict(CATEGORIES)[self.category]
        shown, total = len(self._items()), len(self._all_items())

        if not items and self.rarity:
            # Reachable only if the tab emptied under the view (an equip that
            # sold the last one). Say which filter is hiding everything.
            body = f"*No {self.rarity} items here — tap **All rarities**.*"
        else:
            body = "\n".join(self._line(self.page * per + i + 1, it)
                             for i, it in enumerate(items)) or "*Nothing here yet!*"

        e = discord.Embed(
            title=f"{cat_label} — {self.target.display_name} "
                  f"(Page {self.page + 1}/{self._pages()})",
            description=body,
            color=discord.Color.blurple(),
        )
        # The footer is the only place a filtered count can live without
        # pushing the title past Discord's 256 characters on a long nickname.
        foot = (f"{shown:,} of {total:,} shown · {self.rarity}"
                if self.rarity else f"{total:,} item(s)")
        if self.category in ("bey", "all") and self._slots:
            foot += f" · 🎒 {self._slots[0]:,}/{self._slots[1]:,} slots"
        e.set_footer(text=foot)
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
                + "".join(f" `-{a} {str(s).upper()}`"
                          for s, a in (it.get("penalties") or {}).items())
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
        self._clamp_page()
        per   = self._per_page()
        items = self._page_items()
        if items:
            opts = [
                discord.SelectOption(
                    # `trunc` strips before it cuts, so a name that is only
                    # whitespace cannot produce a zero-length label — the same
                    # 400 as the empty value, one field over.
                    label=f"{self.page * per + i + 1}. "
                          f"{_trunc(str(it['name']), 80) or '?'}",
                    value=str(i),
                    emoji="✅" if it.get("equipped") else None)
                for i, it in enumerate(items[:MAX_SELECT_OPTIONS])
            ]
            sel = discord.ui.Select(placeholder="Select an item…", options=opts, row=1)
            sel.callback = self._select_cb
            self.add_item(sel)

        # Row 2 — nav. ⏮/⏭ matter more than they look: at 167 pages the last
        # page is otherwise 166 taps from the first.
        pages = self._pages()
        first = discord.ui.Button(label="⏮", style=discord.ButtonStyle.secondary,
                                  row=2, disabled=self.page <= 0)
        first.callback = self._first_cb
        prev = discord.ui.Button(label="⬅️", style=discord.ButtonStyle.secondary,
                                 row=2, disabled=self.page <= 0)
        prev.callback = self._prev_cb
        info = discord.ui.Button(label=f"Page {self.page + 1}/{pages}",
                                 style=discord.ButtonStyle.secondary, row=2, disabled=True)
        nxt = discord.ui.Button(label="➡️", style=discord.ButtonStyle.secondary,
                                row=2, disabled=self.page >= pages - 1)
        nxt.callback = self._next_cb
        last = discord.ui.Button(label="⏭", style=discord.ButtonStyle.secondary,
                                 row=2, disabled=self.page >= pages - 1)
        last.callback = self._last_cb
        for b in (first, prev, info, nxt, last):
            self.add_item(b)

        # Row 3 — rarity filter, only where the items have a rarity and only
        # when there is more than one to choose between.
        rarities = self._rarities() if self.category in RARITY_TABS else []
        if len(rarities) > 1:
            total = len(self._all_items())
            opts = [discord.SelectOption(
                label=f"All rarities ({total:,})", value=ALL_RARITIES,
                emoji="🎒", default=self.rarity is None)]
            for r, n in rarities[:MAX_SELECT_OPTIONS - 1]:
                opts.append(discord.SelectOption(
                    # `or None` because an empty-string emoji is a 400 too, the
                    # same way an empty value is. profile.RaritySelect guards
                    # it the same way.
                    label=f"{r} ({n:,})", value=r,
                    emoji=RARITY_EMOJIS.get(r) or None,
                    default=self.rarity == r))
            sel = discord.ui.Select(placeholder="Filter by rarity…",
                                    options=opts, row=3)
            sel.callback = self._rarity_cb
            self.add_item(sel)

        # Row 4 — page jump. Pointless at one page, essential at 167.
        if pages > 1:
            opts = [
                discord.SelectOption(
                    label=f"Page {p + 1} of {pages}",
                    description=f"items {p * per + 1:,}–"
                                f"{min((p + 1) * per, len(self._items())):,}",
                    value=str(p), default=p == self.page)
                for p in self._jump_targets()
            ]
            sel = discord.ui.Select(placeholder=f"Jump to a page (1–{pages})…",
                                    options=opts, row=4)
            sel.callback = self._jump_cb
            self.add_item(sel)

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
        self._clamp_page()
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    def _make_cat_cb(self, key: str):
        async def cb(interaction: discord.Interaction):
            self.category, self.page, self.detail = key, 0, None
            # The filter is per-tab by nature — "Ultimate" carried from beys
            # onto parts would show an empty list nobody asked for.
            self.rarity = None
            await self._refresh(interaction)
        return cb

    async def _prev_cb(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    async def _next_cb(self, interaction: discord.Interaction):
        self.page = min(self._pages() - 1, self.page + 1)
        await self._refresh(interaction)

    async def _first_cb(self, interaction: discord.Interaction):
        self.page = 0
        await self._refresh(interaction)

    async def _last_cb(self, interaction: discord.Interaction):
        self.page = self._pages() - 1
        await self._refresh(interaction)

    async def _jump_cb(self, interaction: discord.Interaction):
        try:
            self.page = int(interaction.data["values"][0])
        except (KeyError, IndexError, TypeError, ValueError):
            pass
        await self._refresh(interaction)

    async def _rarity_cb(self, interaction: discord.Interaction):
        val = (interaction.data.get("values") or [ALL_RARITIES])[0]
        # `""` stays in the check deliberately: a panel opened before the
        # restart still has the old empty-valued option on screen, and a press
        # on it should clear the filter rather than filter by nothing.
        if val in ("", ALL_RARITIES):
            val = None
        # Changing the filter always returns to page 1 — staying on page 40 of
        # a two-page result is the dead panel `_clamp_page` exists to prevent,
        # and landing mid-list is disorienting even when it is legal.
        self.rarity, self.page = (val or None), 0
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
        await self._load_cache()  # parts may have changed
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
        fname = info_card.card_filename(buf, it["name"])
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
            prof = await get_user(self.owner.id)
            prof["active_beyblade"] = it["name"]
            # Clearing the copy pointer is what actually swaps back to a
            # database blade — leaving it set would keep the copy equipped
            # while the panel showed the bey's name.
            prof["active_copy"] = None
            await update_user(self.owner.id, prof)
        elif it["kind"] == "copy":
            from cogs.battle.boss import boss_copy as _bc
            await _bc.equip(self.owner.id, it["id"])
        elif it["kind"] == "avatar":
            await set_equipped_avatar(self.owner.id, it.get("id"))
        else:
            await self._toggle_part(it["name"])
        await self._load_cache()
        self.detail = self._find_refreshed(it)
        await self._refresh(interaction)

    async def _sell_copy_cb(self, interaction: discord.Interaction):
        it = self.detail
        if not (it and self.can_edit and it.get("kind") == "copy"):
            return await self._refresh(interaction)
        from cogs.battle.boss import boss_copy as _bc
        value = _bc.sell_value(it["copy"], it.get("src"))
        gone  = await _bc.remove_copy(self.owner.id, it["id"])
        if gone is None:
            return await interaction.response.send_message(
                "That copy is already gone.", ephemeral=True)
        prof = await get_user(self.owner.id)
        prof["coins"] = prof.get("coins", 0) + value
        await update_user(self.owner.id, prof)
        await self._load_cache()
        self.detail = None
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        await interaction.followup.send(
            f"🧬 Sold **{gone['name']}** ({gone['grade']} · `#{gone['id']}`) "
            f"for 🪙 **{value:,}**.", ephemeral=True)

    async def _toggle_part(self, part_name: str) -> None:
        """Equip a part (replacing any same-type part) or unequip if already on."""
        prof     = await get_user(self.owner.id)
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
        await update_user(self.owner.id, prof)

    async def _part_toggle_cb(self, interaction: discord.Interaction):
        idx   = int(interaction.data["values"][0])
        owned = self._cache.get("part", [])
        if 0 <= idx < len(owned):
            await self._toggle_part(owned[idx]["name"])
            await self._load_cache()
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
        view   = await InventoryView.create(ctx.author, target)
        msg    = await ctx.send(embed=view.build_embed(), view=view)
        view.message = msg


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InventoryUICog(bot))
