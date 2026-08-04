"""
utils/profile_card.py — Beycord blader profile card (Pillow)
=============================================================
Renders a phone-first PNG profile:

  * identity — name, rank tier, tier-coloured accent, level badge
  * progress — level, XP bar to next level, rank score bar to next tier
  * record   — wins / losses / win-rate / current + best streak / coins
  * loadout  — active bey art, rarity, type, ATK / DEF / STA / HP bars
  * collection — owned beys vs the whole database

Reuses fonts / art loading / sanitising from ``utils.image_generator`` so
every Beycord card looks like the same product, and needs ZERO extra assets:
missing ``assets/beys/`` just falls back to an initial disc, and the player
avatar is drawn from their initial (no network call on the render path).

Public API
----------
    render_profile_card(player, profile, blade=None, *, total_beys=None,
                        rank_position=None) -> io.BytesIO | None

    player  — dict(name, [id])
    profile — the user document (wins, losses, xp, coins, streaks, inventory)
    blade   — the active bey document, or None if nothing is equipped

Never raises: any failure returns None so the caller falls back to the embed.
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
W          = 1000
H          = 732
SIDE       = 32
HEADER_H   = 168
COL_GAP    = 20
LEFT_W     = 556                      # left column width
ART_BOX    = 176

# ── Palette ───────────────────────────────────────────────────────────────────
BG_TOP    = (18, 18, 28)
BG_BOT    = (28, 24, 44)
PANEL     = (34, 32, 50)
PANEL_HI  = (44, 41, 64)
TEXT      = (240, 240, 245)
SUBTEXT   = (158, 158, 176)
DIM       = (110, 110, 132)
GOLD      = (250, 204, 21)
XP_COL    = (96, 165, 250)
WIN_COL   = (52, 211, 153)
LOSS_COL  = (239, 68, 68)
COIN_COL  = (250, 204, 21)
BAR_BG    = (46, 44, 66)

STAT_COL = {
    "ATK": (255, 90, 95),
    "DEF": (59, 130, 246),
    "STA": (250, 204, 21),
    "HP":  (52, 211, 153),
}

RARITY_COL = {
    "Common":    (156, 163, 175),
    "Rare":      (59, 130, 246),
    "Epic":      (168, 85, 247),
    "Legendary": (245, 158, 11),
    "Mythic":    (236, 72, 153),
    "Ultimate":  (239, 68, 68),
    "Exclusive": (250, 204, 21),
}

# (min_score, tier name, colour) — mirrors utils/ranks.py RANK_TIERS
RANK_TIERS = [
    (0,    "Rookie",      (149, 165, 166)),
    (50,   "Bronze I",    (205, 127, 50)),
    (150,  "Bronze II",   (205, 127, 50)),
    (300,  "Silver I",    (189, 195, 199)),
    (500,  "Silver II",   (189, 195, 199)),
    (750,  "Gold I",      (241, 196, 15)),
    (1000, "Gold II",     (241, 196, 15)),
    (1350, "Platinum I",  (26, 188, 156)),
    (1750, "Platinum II", (26, 188, 156)),
    (2200, "Diamond I",   (52, 152, 219)),
    (2700, "Diamond II",  (52, 152, 219)),
    (3300, "Legend",      (231, 76, 60)),
    (4000, "Blader God",  (243, 156, 18)),
]

MAX_LEVEL = 100
STAT_MAX  = 150


# ── Theme ─────────────────────────────────────────────────────────────────────
# Locked to the demo-4 look: every card gets the same amber frame/glow/avatar
# accent regardless of the player's rank, so the card reads as one consistent
# product instead of changing colour per tier.
#
# The rank tier is NOT lost — the tier chip and the rank-score bar still use
# the tier's own colour, so rank is still readable at a glance.
#
# Set THEME_LOCKED = False to go back to per-tier accents.
THEME_LOCKED  = True
LOCKED_ACCENT = (243, 156, 18)      # Blader God amber


def _accent_for(tier_col: tuple) -> tuple:
    return LOCKED_ACCENT if THEME_LOCKED else tier_col


def _num(value, default: int = 0) -> int:
    """Coerce a profile field to int. Corrupted documents (a string where a
    count should be, None where a list should be) must not blank the whole
    card — they degrade to 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _rar_col(rarity) -> tuple:
    return RARITY_COL.get(str(rarity), RARITY_COL["Common"])


def _tier_for(score: int):
    """(name, colour, floor, next_floor|None) for a rank score."""
    idx = 0
    for i, (floor, _n, _c) in enumerate(RANK_TIERS):
        if score >= floor:
            idx = i
    floor, name, col = RANK_TIERS[idx]
    nxt = RANK_TIERS[idx + 1][0] if idx + 1 < len(RANK_TIERS) else None
    return name, col, floor, nxt


def _level_from_xp(xp: int) -> tuple[int, int, int]:
    """(level, xp_into_level, xp_span_of_level) — mirrors database.level_from_xp
    (level = floor(sqrt(xp/50))) without importing the DB layer."""
    xp = max(0, int(xp))
    lvl = min(MAX_LEVEL, int((xp / 50) ** 0.5))
    if lvl >= MAX_LEVEL:
        return MAX_LEVEL, 0, 0
    cur_floor = 50 * lvl * lvl
    nxt_floor = 50 * (lvl + 1) * (lvl + 1)
    return lvl, xp - cur_floor, nxt_floor - cur_floor


# ── Drawing primitives ────────────────────────────────────────────────────────

_bg_cache: "Image.Image | None" = None


def _background(accent: tuple) -> Image.Image:
    """Gradient + a tier-coloured glow. The gradient is built once (1px strip
    stretched, not an 760k-pixel Python loop) and cached; only the cheap glow
    is redrawn per accent colour."""
    global _bg_cache
    if _bg_cache is None:
        strip = Image.new("RGBA", (1, H))
        sp = strip.load()
        for y in range(H):
            t = y / max(1, H - 1)
            sp[0, y] = (
                int(BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * t),
                int(BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * t),
                int(BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * t),
                255,
            )
        _bg_cache = strip.resize((W, H), Image.BILINEAR)

    bg = _bg_cache.copy()
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-280, -240, 520, 320), fill=accent + (46,))
    gd.ellipse((W - 460, H - 340, W + 240, H + 200), fill=accent + (26,))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    bg.alpha_composite(glow)
    return bg


def _panel(img, x0, y0, x1, y1, fill, radius=18, outline=None, width=2):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (x0, y0, x1, y1), radius=radius, fill=fill, outline=outline, width=width)
    img.alpha_composite(layer)


def _centre(draw, txt, font, cx, y, fill):
    draw.text((cx - _text_w(draw, txt, font) // 2, y), txt, font=font, fill=fill)


def _right(draw, txt, font, rx, y, fill):
    draw.text((rx - _text_w(draw, txt, font), y), txt, font=font, fill=fill)


def _bar(img, draw, x, y, w, h, pct, colour, label=None, label_font=None):
    pct = max(0.0, min(1.0, pct))
    _panel(img, x, y, x + w, y + h, BAR_BG + (255,), radius=h // 2, width=0)
    if pct > 0:
        fill_w = max(h, int(w * pct))
        _panel(img, x, y, x + fill_w, y + h, colour + (255,), radius=h // 2, width=0)
    if label:
        f = label_font or _font(max(14, h - 8))
        _centre(draw, label, f, x + w // 2, y + (h - f.size) // 2 - 1, TEXT)


def _chip(img, draw, x, y, text, colour, font_size=20, pad=11):
    f = _font(font_size)
    w = _text_w(draw, text, f) + pad * 2
    h = font_size + 12
    _panel(img, x, y, x + w, y + h, colour + (56,), radius=h // 2,
           outline=colour + (190,), width=2)
    draw.text((x + pad, y + 5), text, font=f, fill=colour)
    return w


def _initial_disc(img, draw, cx, cy, r, text, colour):
    _panel(img, cx - r, cy - r, cx + r, cy + r, colour + (52,),
           radius=r, outline=colour + (205,), width=3)
    ch = (_sanitize(text) or "?")[0].upper()
    f = _font(int(r * 1.15))
    draw.text((cx - _text_w(draw, ch, f) // 2, cy - int(r * 0.78)), ch, font=f, fill=colour)


# ── Sections ──────────────────────────────────────────────────────────────────

def _header(img, draw, name, tier_name, tier_col, accent, level, rank_position):
    _panel(img, SIDE, 28, W - SIDE, HEADER_H - 12, PANEL + (220,),
           radius=22, outline=accent + (150,), width=3)

    cy = (28 + HEADER_H - 12) // 2
    _initial_disc(img, draw, SIDE + 76, cy, 50, name, accent)

    x = SIDE + 148
    nf = _fit_text(draw, name, W - SIDE - 300 - x, 46, floor=24)
    draw.text((x, cy - 46), name, font=nf, fill=TEXT)
    subtitle = "BLADER PROFILE"
    if rank_position:
        subtitle += f"   ·   #{rank_position} ON THE SERVER"
    sf = _fit_text(draw, subtitle, W - SIDE - 300 - x, 21, floor=15)
    draw.text((x, cy + 8), subtitle, font=sf, fill=DIM)

    # tier + level chips, right aligned
    lf = _font(22)
    lvl_txt = f"LV {level}" + ("  MAX" if level >= MAX_LEVEL else "")
    lw = _text_w(draw, lvl_txt, lf) + 22
    tw = _text_w(draw, tier_name.upper(), lf) + 22
    top_y = cy - 42
    _chip(img, draw, W - SIDE - 24 - tw, top_y, tier_name.upper(), tier_col, 22)
    _chip(img, draw, W - SIDE - 24 - lw, top_y + 46, lvl_txt, GOLD, 22)


def _progress_block(img, draw, x, y, w, xp, rank_score):
    """Level XP bar + rank-score bar to the next tier."""
    _panel(img, x, y, x + w, y + 156, PANEL + (216,), radius=18,
           outline=(66, 62, 92, 190), width=2)

    lvl, into, span = _level_from_xp(xp)
    pct = 1.0 if span == 0 else into / span
    draw.text((x + 20, y + 16), "LEVEL PROGRESS", font=_font(20), fill=SUBTEXT)
    _right(draw, f"{xp:,} XP total", _font(20), x + w - 20, y + 16, DIM)
    lbl = "MAX LEVEL" if span == 0 else f"{into:,} / {span:,}"
    _bar(img, draw, x + 20, y + 46, w - 40, 30, pct, XP_COL, lbl)

    name, col, floor, nxt = _tier_for(rank_score)
    draw.text((x + 20, y + 92), "RANK SCORE", font=_font(20), fill=SUBTEXT)
    if nxt is None:
        rpct, rlbl = 1.0, f"{rank_score:,} — TOP TIER"
    else:
        rpct = (rank_score - floor) / max(1, nxt - floor)
        rlbl = f"{rank_score:,} / {nxt:,} to next tier"
    _right(draw, name, _font(20), x + w - 20, y + 92, col)
    _bar(img, draw, x + 20, y + 120, w - 40, 26, rpct, col, rlbl)


def _stat_grid(img, draw, x, y, w, profile):
    wins   = _num(profile.get("wins"))
    losses = _num(profile.get("losses"))
    played = wins + losses
    rate   = f"{(wins / played * 100):.0f}%" if played else "—"

    cells = [
        ("WINS",    f"{wins:,}",                              WIN_COL),
        ("LOSSES",  f"{losses:,}",                            LOSS_COL),
        ("WIN RATE", rate,                                    TEXT),
        ("STREAK",  f"{_num(profile.get('win_streak')):,}",  GOLD),
        ("BEST",    f"{_num(profile.get('best_streak')):,}", GOLD),
        ("COINS",   f"{_num(profile.get('coins')):,}",  COIN_COL),
    ]
    cols, rows = 3, 2
    cw = (w - (cols - 1) * 12) // cols
    ch = 116
    for i, (label, value, col) in enumerate(cells):
        cx0 = x + (cw + 12) * (i % cols)
        cy0 = y + (ch + 12) * (i // cols)
        _panel(img, cx0, cy0, cx0 + cw, cy0 + ch, PANEL_HI + (208,),
               radius=16, outline=(66, 62, 92, 170), width=2)
        _centre(draw, label, _font(19), cx0 + cw // 2, cy0 + 18, SUBTEXT)
        vf = _fit_text(draw, value, cw - 20, 44, floor=20)
        _centre(draw, value, vf, cx0 + cw // 2, cy0 + 48, col)
    return y + (ch + 12) * rows - 12


def _collection_bar(img, draw, x, y, w, owned, total):
    _panel(img, x, y, x + w, y + 92, PANEL + (216,), radius=18,
           outline=(66, 62, 92, 190), width=2)
    draw.text((x + 20, y + 18), "COLLECTION", font=_font(20), fill=SUBTEXT)
    # Legacy inventories still hold renamed/removed bey keys, so the raw owned
    # count can exceed the live database. Clamp instead of drawing 105%.
    owned = max(0, min(int(owned), int(total))) if total else max(0, int(owned))
    pct = (owned / total) if total else 0.0
    _right(draw, f"{owned} / {total} beys", _font(20), x + w - 20, y + 18, TEXT)
    _bar(img, draw, x + 20, y + 50, w - 40, 26, pct, (168, 85, 247),
         f"{pct * 100:.0f}%")


def _loadout(img, draw, x, y, w, h, blade):
    _panel(img, x, y, x + w, y + h, PANEL + (220,), radius=22,
           outline=(66, 62, 92, 190), width=2)
    draw.text((x + 20, y + 16), "ACTIVE BEY", font=_font(20), fill=SUBTEXT)

    if not blade:
        _initial_disc(img, draw, x + w // 2, y + 148, ART_BOX // 2, "?", DIM)
        _centre(draw, "NOTHING EQUIPPED", _font(26), x + w // 2, y + 262, DIM)
        _centre(draw, "use  ;equip <name>", _font(21), x + w // 2, y + 296, SUBTEXT)
        return

    name   = str(blade.get("name", "Unknown"))
    rarity = blade.get("rarity", "Common")
    btype  = blade.get("type", "Balance")
    col    = _rar_col(rarity)
    cx     = x + w // 2

    # art
    r = ART_BOX // 2
    acy = y + 60 + r
    _panel(img, cx - r - 5, acy - r - 5, cx + r + 5, acy + r + 5,
           col + (46,), radius=r + 5, outline=col + (200,), width=3)
    art = _blade_art(name, ART_BOX - 12)
    if art is not None:
        box = ART_BOX - 12
        mask = Image.new("L", (box, box), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, box - 1, box - 1), fill=255)
        fitted = Image.new("RGBA", (box, box), (0, 0, 0, 0))
        fitted.paste(art, ((box - art.width) // 2, (box - art.height) // 2), art)
        img.paste(fitted, (cx - box // 2, acy - box // 2), mask)
    else:
        f = _font(76)
        ch = (_sanitize(name) or "?")[0].upper()
        draw.text((cx - _text_w(draw, ch, f) // 2, acy - 50), ch, font=f, fill=col)

    ny = acy + r + 16
    nf = _fit_text(draw, name, w - 40, 34, floor=19)
    _centre(draw, name, nf, cx, ny, TEXT)

    # rarity + type chips, centred as a pair
    f = _font(19)
    rw = _text_w(draw, str(rarity).upper(), f) + 22
    tw = _text_w(draw, str(btype).upper(), f) + 22
    start = cx - (rw + 10 + tw) // 2
    _chip(img, draw, start, ny + 44, str(rarity).upper(), col, 19)
    _chip(img, draw, start + rw + 10, ny + 44, str(btype).upper(), (147, 197, 253), 19)

    # stat bars
    stats = blade.get("stats", {}) or {}
    rows = [
        ("ATK", _num(stats.get("attack"))),
        ("DEF", _num(stats.get("defense"))),
        ("STA", _num(stats.get("stamina"))),
        ("HP",  _num(stats.get("hp"))),
    ]
    top = max(STAT_MAX, max((v for _l, v in rows), default=0))
    by = ny + 92
    for label, val in rows:
        draw.text((x + 22, by + 2), label, font=_font(19), fill=STAT_COL[label])
        _bar(img, draw, x + 74, by, w - 74 - 78, 22, val / top, STAT_COL[label])
        _right(draw, str(val), _font(20), x + w - 20, by + 1, TEXT)
        by += 34


# ── Public API ────────────────────────────────────────────────────────────────

def render_profile_card(
    player: dict,
    profile: dict,
    blade: dict | None = None,
    *,
    total_beys: int | None = None,
    rank_position: int | None = None,
) -> "io.BytesIO | None":
    """Render the profile card. Returns a PNG buffer, or None on any failure."""
    if not CARD_ENABLED:
        return None
    try:
        name       = _sanitize(player.get("name", "Blader"))
        rank_score = max(0, _num(profile.get("rank_score")))
        xp         = max(0, _num(profile.get("xp")))
        tier_name, tier_col, _floor, _nxt = _tier_for(rank_score)
        level, _i, _s = _level_from_xp(xp)

        accent = _accent_for(tier_col)
        img  = _background(accent)
        draw = ImageDraw.Draw(img)

        _header(img, draw, name, tier_name, tier_col, accent, level, rank_position)

        lx = SIDE
        rx = SIDE + LEFT_W + COL_GAP
        rw = W - SIDE - rx
        top = HEADER_H + 12

        _progress_block(img, draw, lx, top, LEFT_W, xp, rank_score)
        gy = _stat_grid(img, draw, lx, top + 172, LEFT_W, profile)
        owned = len(set(profile.get("inventory") or []))
        _collection_bar(img, draw, lx, gy + 12, LEFT_W, owned, total_beys or owned)

        _loadout(img, draw, rx, top, rw, H - top - 32, blade)

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf
    except Exception:
        return None
