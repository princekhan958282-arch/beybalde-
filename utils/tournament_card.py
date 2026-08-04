"""
utils/tournament_card.py — Beycord tournament bracket card (Pillow)
===================================================================
Renders a phone-first PNG showing, in one glance:

  * the money   — prize pot, entry fee, player count, payout
  * the pairings— who fights who this round
  * the blades  — the random bey each player was drafted

Reuses the fonts / art loader / sanitiser from ``utils.image_generator`` so
the tournament card matches the battle card visually and needs ZERO extra
assets: if ``assets/beys/`` is missing, art slots fall back to a coloured
initial disc instead of failing.

Public API
----------
    render_tournament_card(round_label, matches, pot, fee, players, *,
                           champion=None) -> io.BytesIO | None

    matches: list of dicts —
        {"left": {"name": str, "blade": str, "rarity": str},
         "right": {...} | None,          # None == BYE
         "winner": "left" | "right" | None}

Never raises: any failure returns None and the cog falls back to the embed.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFilter

from utils.image_generator import (
    _blade_art,
    _fit_text,
    _font,
    _sanitize,
    _text_w,
)

CARD_ENABLED = True

# ── Canvas ────────────────────────────────────────────────────────────────────
W           = 1000
HEADER_H    = 132
MONEY_H     = 168
PRIZE_H     = 128
ROW_H       = 164
FOOTER_H    = 64
SIDE        = 32
ART_BOX     = 104

# ── Palette (kept in sync with image_generator) ───────────────────────────────
BG_TOP    = (18, 18, 28)
BG_BOT    = (28, 24, 44)
PANEL     = (34, 32, 50)
PANEL_HI  = (46, 42, 68)
TEXT      = (240, 240, 245)
SUBTEXT   = (158, 158, 176)
GOLD      = (250, 204, 21)
GOLD_DIM  = (161, 128, 12)
L_ACCENT  = (255, 70, 85)
R_ACCENT  = (59, 130, 246)
WIN_COL   = (52, 211, 153)
BYE_COL   = (110, 110, 130)

RARITY_COL = {
    "Common":    (156, 163, 175),
    "Rare":      (59, 130, 246),
    "Epic":      (168, 85, 247),
    "Legendary": (245, 158, 11),
    "Mythic":    (236, 72, 153),
    "Ultimate":  (239, 68, 68),
    "Exclusive": (250, 204, 21),
}


def _rar_col(rarity: str) -> tuple:
    return RARITY_COL.get(str(rarity), RARITY_COL["Common"])


# ── Background ────────────────────────────────────────────────────────────────

_bg_cache: dict[int, Image.Image] = {}


def _background(h: int) -> Image.Image:
    """Vertical gradient + two soft corner glows, sized to the card height.

    Built by drawing a 1px-wide gradient strip and stretching it, instead of
    the ~850k-pixel Python loop the battle card used — a bracket card can be
    re-rendered several times per round, so this stays off the hot path.
    Cached per height; callers get a fast .copy().
    """
    cached = _bg_cache.get(h)
    if cached is not None:
        return cached.copy()

    strip = Image.new("RGBA", (1, h))
    sp = strip.load()
    for y in range(h):
        t = y / max(1, h - 1)
        sp[0, y] = (
            int(BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * t),
            int(BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * t),
            int(BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * t),
            255,
        )
    bg = strip.resize((W, h), Image.BILINEAR)

    glow = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-240, -200, 420, 300), fill=GOLD + (34,))
    gd.ellipse((W - 420, h - 300, W + 240, h + 200), fill=R_ACCENT + (34,))
    glow = glow.filter(ImageFilter.GaussianBlur(80))
    bg.alpha_composite(glow)

    if len(_bg_cache) > 8:          # a handful of bracket sizes, no leak
        _bg_cache.clear()
    _bg_cache[h] = bg
    return bg.copy()


def _panel(img, x0, y0, x1, y1, fill, radius=20, outline=None, width=3):
    """Rounded panel drawn onto an RGBA overlay so fills can be translucent."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (x0, y0, x1, y1), radius=radius, fill=fill, outline=outline, width=width
    )
    img.alpha_composite(layer)


def _centre(draw, txt, font, cx, y, fill):
    draw.text((cx - _text_w(draw, txt, font) // 2, y), txt, font=font, fill=fill)


def _art_disc(img, draw, cx, cy, blade: str, accent: tuple, box: int = ART_BOX):
    """Circular blade portrait; falls back to an initial disc when art is absent."""
    r = box // 2
    _panel(img, cx - r - 4, cy - r - 4, cx + r + 4, cy + r + 4,
           accent + (52,), radius=r + 4, outline=accent + (200,), width=3)
    art = _blade_art(blade, box - 10)
    if art is not None:
        mask = Image.new("L", (box - 10, box - 10), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, box - 11, box - 11), fill=255)
        fitted = Image.new("RGBA", (box - 10, box - 10), (0, 0, 0, 0))
        fitted.paste(art, ((box - 10 - art.width) // 2,
                           (box - 10 - art.height) // 2), art)
        img.paste(fitted, (cx - (box - 10) // 2, cy - (box - 10) // 2), mask)
    else:
        initial = (_sanitize(blade) or "?")[0].upper()
        f = _font(46)
        draw.text((cx - _text_w(draw, initial, f) // 2, cy - 30),
                  initial, font=f, fill=accent)


# ── Money band ────────────────────────────────────────────────────────────────

def _money_band(img, draw, y0, pot: int, fee: int, players: int) -> None:
    x0, x1 = SIDE, W - SIDE
    _panel(img, x0, y0, x1, y0 + MONEY_H - 20, PANEL + (232,),
           radius=22, outline=GOLD_DIM + (170,), width=3)

    cells = [
        ("PRIZE POT",  f"{pot:,}",     GOLD,     "winner takes all"),
        ("ENTRY FEE",  f"{fee:,}",     R_ACCENT, "per player"),
        ("PLAYERS",    f"{players}",   L_ACCENT, "in bracket"),
    ]
    cw = (x1 - x0) // len(cells)
    for i, (label, value, col, sub) in enumerate(cells):
        cx = x0 + cw * i + cw // 2
        if i:
            draw.line((x0 + cw * i, y0 + 26, x0 + cw * i, y0 + MONEY_H - 46),
                      fill=(70, 66, 96), width=2)
        _centre(draw, label, _font(24), cx, y0 + 22, SUBTEXT)
        vf = _fit_text(draw, value, cw - 40, 62, floor=32)
        _centre(draw, value, vf, cx, y0 + 54, col)
        _centre(draw, sub, _font(21), cx, y0 + 116, SUBTEXT)


# ── Prize ribbon ──────────────────────────────────────────────────────────────

def _prize_ribbon(img, draw, y0, prize: str, payout: list) -> None:
    """Full-width band for the custom prize (anything the host wants — a game
    top-up, a pass, a role) plus the coin split per placement."""
    x0, x1 = SIDE, W - SIDE
    _panel(img, x0, y0, x1, y0 + PRIZE_H - 20, GOLD + (30,),
           radius=22, outline=GOLD + (150,), width=3)

    has_payout = bool(payout)
    text_right = (x1 - 320) if has_payout else (x1 - 24)

    draw.text((x0 + 26, y0 + 18), "PRIZE POOL", font=_font(23), fill=GOLD_DIM)
    ptxt = _sanitize(prize) if prize else "Coins only"
    pf = _fit_text(draw, ptxt, text_right - (x0 + 26), 46, floor=22)
    draw.text((x0 + 26, y0 + 48), ptxt, font=pf, fill=GOLD)

    if not has_payout:
        return

    draw.line((x1 - 312, y0 + 20, x1 - 312, y0 + PRIZE_H - 42),
              fill=(120, 100, 40), width=2)
    cw = 288 // max(1, len(payout))
    for i, (place, amount) in enumerate(payout):
        cx = x1 - 300 + cw * i + cw // 2
        _centre(draw, str(place), _font(21), cx, y0 + 24, SUBTEXT)
        af = _fit_text(draw, str(amount), cw - 14, 32, floor=16)
        _centre(draw, str(amount), af, cx, y0 + 54, TEXT)


# ── Match row ─────────────────────────────────────────────────────────────────

def _side(img, draw, side_dict, x_centre, y_centre, accent, is_winner, mirrored):
    name  = _sanitize(side_dict.get("name", "Player"))
    blade = str(side_dict.get("blade") or "—")
    rar   = side_dict.get("rarity", "Common")
    BOX   = 214

    art_cx = x_centre - 150 if not mirrored else x_centre + 150
    _art_disc(img, draw, art_cx, y_centre, blade,
              WIN_COL if is_winner else accent)

    # Text column sits between the disc and the centre VS marker. On the
    # mirrored (right) side it is right-aligned so it hugs the disc instead
    # of leaving a ragged gap in the middle of the card.
    box_left = art_cx + 68 if not mirrored else art_cx - 68 - BOX

    def _put(txt, font, y, fill):
        x = box_left if not mirrored else box_left + BOX - _text_w(draw, txt, font)
        draw.text((x, y), txt, font=font, fill=fill)
        return x

    nf = _fit_text(draw, name, BOX, 34, floor=20)
    _put(name, nf, y_centre - 40, WIN_COL if is_winner else TEXT)

    bf = _fit_text(draw, blade, BOX, 26, floor=17)
    _put(blade, bf, y_centre - 2, _rar_col(rar))

    rf   = _font(19)
    rtxt = str(rar).upper()
    rw   = _text_w(draw, rtxt, rf) + 20
    cx0  = box_left if not mirrored else box_left + BOX - rw
    _panel(img, cx0, y_centre + 30, cx0 + rw, y_centre + 58,
           _rar_col(rar) + (58,), radius=12, outline=_rar_col(rar) + (170,), width=2)
    draw.text((cx0 + 10, y_centre + 34), rtxt, font=rf, fill=_rar_col(rar))

    if is_winner:
        cf = _font(26)
        _centre(draw, "WINNER", cf, art_cx, y_centre - 88, WIN_COL)


def _match_row(img, draw, y0, idx, match) -> None:
    left, right, winner = match.get("left"), match.get("right"), match.get("winner")
    decided = winner is not None
    _panel(img, SIDE, y0, W - SIDE, y0 + ROW_H - 16,
           (PANEL_HI if decided else PANEL) + (216,), radius=20,
           outline=(WIN_COL + (140,)) if decided else (66, 62, 92, 190), width=2)

    cy = y0 + (ROW_H - 16) // 2
    tag = _font(22)
    draw.text((SIDE + 18, y0 + 14), f"M{idx}", font=tag, fill=SUBTEXT)

    _side(img, draw, left, 268, cy, L_ACCENT, winner == "left", mirrored=False)

    if right is None:
        _centre(draw, "BYE", _font(30), W - 268, cy - 16, BYE_COL)
        _centre(draw, "auto-advance", _font(20), W - 268, cy + 22, SUBTEXT)
    else:
        _side(img, draw, right, W - 268, cy, R_ACCENT, winner == "right", mirrored=True)

    vf = _font(34)
    _centre(draw, "VS" if not decided else "»", vf, W // 2, cy - 20,
            GOLD if not decided else WIN_COL)
    _centre(draw, "pending" if not decided else "done", _font(19),
            W // 2, cy + 20, SUBTEXT)


# ── Public API ────────────────────────────────────────────────────────────────

def render_tournament_card(
    round_label: str,
    matches: list[dict],
    pot: int,
    fee: int,
    players: int,
    *,
    champion: str | None = None,
    prize: str | None = None,
    payout: list | None = None,
) -> "io.BytesIO | None":
    """Render the bracket card. Returns a PNG buffer, or None on any failure.

    ``prize``  — free-text custom reward the host is putting up (a game
                 top-up, a weekly pass, a role — anything). Shown in its own
                 gold ribbon.
    ``payout`` — list of (place, amount) pairs, e.g. [("1st", "12,000"), ...].
    """
    if not CARD_ENABLED:
        return None
    try:
        rows      = max(1, len(matches))
        show_ribbon = bool(prize) or bool(payout)
        ribbon_h  = PRIZE_H if show_ribbon else 0
        h = HEADER_H + MONEY_H + ribbon_h + rows * ROW_H + FOOTER_H
        img  = _background(h)
        draw = ImageDraw.Draw(img)

        # Header
        title = "TOURNAMENT CHAMPION" if champion else "TOURNAMENT BRACKET"
        tf = _fit_text(draw, title, W - 2 * SIDE - 240, 52, floor=30)
        draw.text((SIDE, 34), title, font=tf, fill=GOLD)
        rl = _sanitize(round_label).upper()
        rf = _font(26)
        rw = _text_w(draw, rl, rf) + 30
        _panel(img, W - SIDE - rw, 40, W - SIDE, 84,
               GOLD + (46,), radius=16, outline=GOLD + (180,), width=2)
        draw.text((W - SIDE - rw + 15, 48), rl, font=rf, fill=GOLD)
        draw.line((SIDE, HEADER_H - 22, W - SIDE, HEADER_H - 22),
                  fill=(64, 60, 90), width=2)

        _money_band(img, draw, HEADER_H, pot, fee, players)

        y = HEADER_H + MONEY_H
        if show_ribbon:
            _prize_ribbon(img, draw, y, prize or "", payout or [])
            y += ribbon_h

        for i, m in enumerate(matches, 1):
            _match_row(img, draw, y, i, m)
            y += ROW_H

        if champion:
            foot = f"CHAMPION: {_sanitize(champion)}"
            if prize:
                foot += f" — wins {_sanitize(prize)}"
            elif pot:
                foot += f" — takes {pot:,} coins"
        else:
            foot = "Random beys drafted for every player  |  /tournament next"
        ff = _fit_text(draw, foot, W - 2 * SIDE, 22, floor=15)
        _centre(draw, foot, ff, W // 2, h - FOOTER_H + 16,
                GOLD if champion else SUBTEXT)

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf
    except Exception:
        return None
