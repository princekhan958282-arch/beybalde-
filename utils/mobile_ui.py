"""
utils/mobile_ui.py
------------------
Phone-first list rendering.

A Discord phone screen shows roughly 7 lines of an embed before the user has to
scroll, and scrolling inside a long embed on mobile is genuinely annoying — you
lose your place and the buttons are off-screen at the bottom. Every list in the
bot therefore pages at PAGE_SIZE items instead of dumping 10-36 rows at once.

What you get:

    MobileListView(owner=..., title=..., items=[...], render=fn)

  * a page dropdown  (jump straight to page 4 without tapping Next three times)
  * ◀ / ▶ buttons    (thumb-reachable at the bottom)
  * an optional item dropdown, so tapping a row opens its detail view —
    this is the bit that replaces "type ;mastery <exact blade name>"

Only the owner can drive the view; anyone else gets an ephemeral nudge to run
the command themselves.
"""

from typing import Callable, Optional, Sequence

import discord

PAGE_SIZE = 4          # rows per page — tuned so a page + nav fits one screen
LABEL_MAX = 96         # Discord's select-option label cap is 100
DESC_MAX  = 96


def trunc(text: str, limit: int = LABEL_MAX) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def bar(done: int, total: int, width: int = 8) -> str:
    """Compact progress bar. 8 cells reads fine at phone width."""
    if total <= 0:
        return "█" * width
    filled = max(0, min(width, round(done / total * width)))
    return "█" * filled + "░" * (width - filled)


def chunk(items: Sequence, size: int = PAGE_SIZE) -> list[list]:
    return [list(items[i:i + size]) for i in range(0, len(items), size)] or [[]]


class _PageSelect(discord.ui.Select):
    def __init__(self, view: "MobileListView"):
        total = len(view.pages)
        # Discord caps a select at 25 options; past that fall back to buttons.
        opts = []
        for i in range(min(total, 25)):
            first = i * view.page_size + 1
            last  = min((i + 1) * view.page_size, len(view.items))
            opts.append(discord.SelectOption(
                label=f"Page {i + 1} of {total}",
                description=trunc(f"items {first}–{last}", DESC_MAX),
                value=str(i),
                default=(i == view.page),
            ))
        super().__init__(placeholder=f"📄 Jump to page…", options=opts, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: "MobileListView" = self.view
        if not view.owns(interaction):
            return await view.deny(interaction)
        view.page = int(self.values[0])
        view.rebuild()
        await interaction.response.edit_message(embed=view.embed(), view=view)


class _ItemSelect(discord.ui.Select):
    def __init__(self, view: "MobileListView"):
        rows = view.current()
        opts = []
        for i, item in enumerate(rows):
            label, desc, emoji = view.option_of(item)
            opts.append(discord.SelectOption(
                label=trunc(label, LABEL_MAX) or "—",
                description=trunc(desc, DESC_MAX) or None,
                emoji=emoji or None,
                value=str(view.page * view.page_size + i),
            ))
        if not opts:
            opts = [discord.SelectOption(label="—", value="-1")]
        super().__init__(placeholder=view.detail_placeholder,
                         options=opts, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: "MobileListView" = self.view
        if not view.owns(interaction):
            return await view.deny(interaction)
        idx = int(self.values[0])
        if idx < 0 or idx >= len(view.items):
            return await interaction.response.defer()
        await view.detail(interaction, view.items[idx])


class MobileListView(discord.ui.View):
    """A paginated list sized for a phone.

    Subclass or pass callables:
        render(item, absolute_index) -> str      one compact row (1-2 lines)
        option_of(item) -> (label, desc, emoji)  for the item dropdown
        detail(interaction, item)                what tapping a row does
    """

    def __init__(
        self,
        *,
        owner: discord.abc.User,
        title: str,
        items: Sequence,
        render: Callable[[object, int], str],
        page_size: int = PAGE_SIZE,
        colour: int = 0x9b59b6,
        description: str = "",
        footer: str = "",
        empty: str = "Nothing here yet.",
        option_of: Optional[Callable[[object], tuple]] = None,
        detail: Optional[Callable] = None,
        detail_placeholder: str = "🔍 Open one for details…",
        timeout: float = 180,
    ):
        super().__init__(timeout=timeout)
        self.owner       = owner
        self.title       = title
        self.items       = list(items)
        self.render      = render
        self.page_size   = page_size
        self.colour      = colour
        self.description = description
        self.footer      = footer
        self.empty       = empty
        self._option_of  = option_of
        self._detail     = detail
        self.detail_placeholder = detail_placeholder

        self.pages   = chunk(self.items, page_size)
        self.page    = 0
        self.message: Optional[discord.Message] = None
        self.rebuild()

    # ── Hooks ────────────────────────────────────────────────────────────────
    def option_of(self, item) -> tuple:
        if self._option_of:
            return self._option_of(item)
        return (str(item), "", None)

    async def detail(self, interaction: discord.Interaction, item):
        if self._detail:
            return await self._detail(interaction, item)
        await interaction.response.defer()

    # ── Access control ───────────────────────────────────────────────────────
    def owns(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner.id

    async def deny(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "That's not your list — run the command yourself!", ephemeral=True)

    # ── Layout ───────────────────────────────────────────────────────────────
    def current(self) -> list:
        return self.pages[self.page] if self.pages else []

    def rebuild(self):
        self.clear_items()
        multi = len(self.pages) > 1

        if multi and len(self.pages) <= 25:
            self.add_item(_PageSelect(self))
        if self._option_of and self.items:
            self.add_item(_ItemSelect(self))
        if multi:
            self.add_item(_PrevButton(self.page == 0))
            self.add_item(_PageLabel(self.page + 1, len(self.pages)))
            self.add_item(_NextButton(self.page >= len(self.pages) - 1))

    def embed(self) -> discord.Embed:
        rows = self.current()
        if not self.items:
            body = self.empty
        else:
            start = self.page * self.page_size
            body  = "\n".join(self.render(it, start + i) for i, it in enumerate(rows))

        desc = (self.description + "\n\n" + body) if self.description else body
        e = discord.Embed(title=self.title, description=desc, color=self.colour)

        bits = []
        if len(self.pages) > 1:
            bits.append(f"Page {self.page + 1}/{len(self.pages)} · "
                        f"{len(self.items)} total")
        if self.footer:
            bits.append(self.footer)
        if bits:
            e.set_footer(text="  •  ".join(bits))
        return e

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class _PrevButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(emoji="◀", style=discord.ButtonStyle.secondary,
                         row=2, disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        view: MobileListView = self.view
        if not view.owns(interaction):
            return await view.deny(interaction)
        view.page = max(0, view.page - 1)
        view.rebuild()
        await interaction.response.edit_message(embed=view.embed(), view=view)


class _NextButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(emoji="▶", style=discord.ButtonStyle.secondary,
                         row=2, disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        view: MobileListView = self.view
        if not view.owns(interaction):
            return await view.deny(interaction)
        view.page = min(len(view.pages) - 1, view.page + 1)
        view.rebuild()
        await interaction.response.edit_message(embed=view.embed(), view=view)


class _PageLabel(discord.ui.Button):
    """Non-interactive page counter between the arrows."""

    def __init__(self, page: int, total: int):
        super().__init__(label=f"{page}/{total}",
                         style=discord.ButtonStyle.secondary,
                         row=2, disabled=True)


async def send_list(ctx, **kwargs) -> MobileListView:
    """Build a MobileListView, send it, and wire up view.message."""
    view = MobileListView(owner=ctx.author, **kwargs)
    view.message = await ctx.send(embed=view.embed(), view=view)
    return view
