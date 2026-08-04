"""
ui_v2.py — Components V2 rendering, with a fallback that still works on 2.3.

Why a fallback exists
---------------------
Components V2 (`ui.LayoutView`, `Container`, `TextDisplay`, `Separator`) landed
in discord.py **2.6**. This bot was pinned to 2.3.1, so every V2 symbol is
simply missing there and importing them at module level would take the whole
cog down on load.

`HAS_V2` is resolved once at import. On 2.6+ the tournament card renders as a
Container with a real accent stripe; on anything older the same content renders
as a classic embed and the buttons behave identically. Nothing about the flow
changes — only how the card is drawn — so the bot is deployable before the
dependency bump and gets the better rendering after it.

One V2 rule worth remembering: a message using Components V2 **cannot** also
carry `content` or `embeds`. The container replaces both.
"""

from __future__ import annotations

import discord

# The accent stripe is the one colour Discord lets a bot choose. Beycord's is
# ember rather than blurple so the card doesn't read as every other bot's.
EMBER = 0xFF9F45
GREEN = 0x248046
BLURPLE = 0x5865F2

HAS_V2 = all(hasattr(discord.ui, name) for name in
             ("LayoutView", "Container", "TextDisplay", "Separator", "ActionRow"))


def slot_bar(joined: int, total: int, width: int = 14) -> str:
    """Progress bar as block characters.

    Discord renders no custom graphics inside a message, so a "progress bar" is
    literally text. Blocks survive every client, including mobile.
    """
    total = max(1, total)
    filled = max(0, min(width, round(joined / total * width)))
    return "█" * filled + "░" * (width - filled)


def _card_lines(t, joined: int) -> tuple[str, str, str]:
    """The three text blocks the card is made of, shared by both renderers so
    the two paths can never drift apart."""
    head = (f"# ⚔️ {t.name}\n"
            f"-# Real-time PvP · Limited slots · Instant join")

    fmt = {"single": "Single elimination", "double": "Double elimination",
           "round_robin": "Round robin"}.get(t.mode, t.mode)
    when = f"<t:{int(t.start_time)}:f>" if t.start_time else "To be announced"
    rel = f"<t:{int(t.start_time)}:R>" if t.start_time else ""
    reward = t.reward_value or "Bragging rights"

    body = (f"**📅 Time**\n{when}\n-# {rel}\n\n"
            f"**🌍 Region**\nGlobal\n-# Auto timezone sync\n\n"
            f"**🎁 Reward**\n{reward}\n\n"
            f"**🏆 Format**\n{fmt}")

    left = t.max_players - joined
    tail = (f"**👥 Slots**\n"
            f"{slot_bar(joined, t.max_players)}\n"
            f"**{joined}**/{t.max_players} joined · "
            + ("bracket full" if left <= 0
               else f"**{left} slot{'s' if left != 1 else ''} left**"))
    return head, body, tail


# ── Components V2 path ────────────────────────────────────────────────────────

def _container(*children):
    """Build a Container, tolerating either spelling of the accent kwarg.

    discord.py generally offers both `colour` and `color`, but this constructor
    is new enough that pinning one spelling would be a guess — and guessing
    wrong is an unhandled TypeError at card-render time, on every card.
    """
    for kwarg in ("accent_colour", "accent_color"):
        try:
            return discord.ui.Container(*children, **{kwarg: EMBER})
        except TypeError:
            continue
    return discord.ui.Container(*children)


def attach_card_v2(view, t, joined: int):
    """Add the tournament card to an existing LayoutView.

    Takes the view rather than a class because the caller (TournamentCard) IS
    the LayoutView — it needs its own buttons alongside the container.
    """
    if not HAS_V2:
        return None
    head, body, tail = _card_lines(t, joined)
    container = _container(
        discord.ui.TextDisplay(head),
        discord.ui.Separator(),
        discord.ui.TextDisplay(body),
        discord.ui.Separator(),
        discord.ui.TextDisplay(tail),
    )
    view.add_item(container)
    return container


# ── Fallback path ─────────────────────────────────────────────────────────────

def build_card_embed(t, joined: int) -> discord.Embed:
    """Classic embed carrying exactly the same content as the V2 container."""
    left = t.max_players - joined
    fmt = {"single": "Single elimination", "double": "Double elimination",
           "round_robin": "Round robin"}.get(t.mode, t.mode)
    when = f"<t:{int(t.start_time)}:f>" if t.start_time else "To be announced"
    rel = f"<t:{int(t.start_time)}:R>" if t.start_time else "—"

    e = discord.Embed(
        title=f"⚔️ {t.name}",
        description="Real-time PvP · Limited slots · Instant join",
        colour=EMBER,
    )
    e.add_field(name="📅 Time", value=f"{when}\n{rel}", inline=True)
    e.add_field(name="🌍 Region", value="Global\nAuto timezone sync", inline=True)
    e.add_field(name="🎁 Reward", value=t.reward_value or "Bragging rights",
                inline=True)
    e.add_field(name="🏆 Format", value=fmt, inline=True)
    e.add_field(
        name="👥 Slots",
        value=(f"`{slot_bar(joined, t.max_players)}`\n"
               f"**{joined}**/{t.max_players} joined · "
               + ("bracket full" if left <= 0
                  else f"**{left} slot{'s' if left != 1 else ''} left**")),
        inline=False)
    e.set_footer(text=f"Beycord · id {t.id}")
    return e


def card_kwargs(t, joined: int, view: discord.ui.View) -> dict:
    """Send/edit kwargs for the tournament card on whichever path is available.

    Callers just splat this — they never have to branch on HAS_V2 themselves,
    which is what keeps the two renderings in sync.
    """
    if HAS_V2 and isinstance(view, getattr(discord.ui, "LayoutView", ())):
        return {"view": view}          # V2 forbids embed= alongside the view
    return {"embed": build_card_embed(t, joined), "view": view}
