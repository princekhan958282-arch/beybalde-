"""
utils/image_generator.py — BEYCBOT battle card renderer (Pillow)
================================================================
Generates a phone-first battle status card as a PNG buffer.

Design goals
------------
* ZERO required assets — background, bars and chips are drawn in code, so the
  bot deploys clean on the panel.  If ``assets/background.png`` or
  ``assets/font.ttf`` exist they are used automatically as overrides.
* Phone-first: big type, fat bars, high contrast.  Discord scales the image
  to chat width; at 1000×620 everything stays readable on a 6" screen.
* Never break a battle: the session calls this inside try/except + a thread;
  any failure falls back to the classic text embed.

Public API
----------
    render_battle_card(round_no, left, right) -> io.BytesIO
        left/right: dict(name, blade, hp, max_hp, stamina, max_stamina,
                         gauge, gauge_max, statuses: list[str])

    CARD_ENABLED — flip False to disable image cards without touching session.
"""
from __future__ import annotations

import io
import os
import unicodedata
from PIL import Image, ImageDraw, ImageFont, ImageFilter

CARD_ENABLED = True


def _sanitize(txt: str) -> str:
    """Normalize fancy Unicode (𝑀𝐼𝐾𝐸𝑌 → MIKEY) and drop glyphs the font
    can't draw (emoji etc.) so names never render as tofu boxes."""
    txt = unicodedata.normalize("NFKC", str(txt))
    out = []
    for ch in txt:
        cat = unicodedata.category(ch)
        if cat.startswith(("L", "N", "P", "Zs")) and ord(ch) < 0x2500:
            out.append(ch)
    s = "".join(out).strip()
    return s or "Player"

# ── Canvas ────────────────────────────────────────────────────────────────────
W, H = 1200, 900
_ART_BOX = 390        # blade-art max size (no frame)
PANEL_TOP = 120       # player panels at top; art sits in the bottom zone

# ── Palette ───────────────────────────────────────────────────────────────────
BG_TOP    = (18, 18, 28)
BG_BOT    = (28, 24, 44)
P1_ACCENT = (255, 70, 85)      # red — challenger
P2_ACCENT = (59, 130, 246)     # blue — opponent
TEXT      = (240, 240, 245)
SUBTEXT   = (160, 160, 175)
BAR_BG    = (40, 40, 55)
HP_HIGH   = (52, 211, 153)
HP_MID    = (251, 191, 36)
HP_LOW    = (239, 68, 68)
STA_COL   = (250, 204, 21)
GAUGE_COL = (96, 165, 250)

# Absolute, derived from this file. These were relative, so launching the bot
# from anywhere but the project root made os.listdir(_BEY_DIR) raise, the art
# index came back empty, and EVERY blade silently lost its artwork on battle
# cards, profile cards and tournament cards. utils/info_card.py was fixed for
# this once; this second copy of the same constant was missed, which is why
# ";info" kept its art while battle cards went blank.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ASSET_BG   = os.path.join(_PROJECT_ROOT, "assets", "ui", "battle_background.png")
_ASSET_FONT = os.path.join(_PROJECT_ROOT, "assets", "font.ttf")
_BEY_DIR    = os.path.join(_PROJECT_ROOT, "assets", "beys")
_SYS_FONTS = [
    _ASSET_FONT,
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

_font_cache: dict[int, ImageFont.FreeTypeFont] = {}
_art_cache: dict[str, "Image.Image | None"] = {}
_art_index: dict[str, str] | None = None


def _norm_name(s: str) -> str:
    """Lenient art-name key: case/underscore/dash/extension insensitive.

    Strips any known art extension, so "dranzer.webp", "Dranzer.png" and the
    blade's JSON name "Dranzer" all collapse to the same key.
    """
    s = s.lower().strip()
    for _ext in (".png", ".webp", ".jpeg", ".jpg"):
        if s.endswith(_ext):
            s = s[: -len(_ext)]
            break
    return s.replace("_", " ").replace("-", " ").strip()


def _blade_art(name: str, box: int) -> "Image.Image | None":
    """Blade artwork fitted into a box×box square (aspect preserved).
    Lenient filename matching (case/underscore/dash insensitive). Cached."""
    global _art_index
    key = f"{_norm_name(name)}@{box}"
    if key in _art_cache:
        return _art_cache[key]
    art = None
    try:
        if _art_index is None:
            _art_index = {}
            if os.path.isdir(_BEY_DIR):
                for f in os.listdir(_BEY_DIR):
                    # WebP/JPG accepted alongside PNG: tools/optimize_assets.py
                    # stores art as alpha-preserving WebP (~8% of the PNG size).
                    if (f.lower().endswith((".png", ".webp", ".jpg", ".jpeg"))
                            and not f.startswith("_")):
                        _art_index[_norm_name(f)] = os.path.join(_BEY_DIR, f)
        path = _art_index.get(_norm_name(name))
        if path:
            im = Image.open(path).convert("RGBA")
            bbox = im.getbbox()                 # trim transparent padding first
            if bbox:
                im = im.crop(bbox)
            # Scale by WIDTH so every bey shows at the same on-card diameter,
            # regardless of the source file's aspect (tall screenshots no
            # longer render tiny). Cap height so very tall arts don't overflow.
            scale = box / im.width
            new_h = max(1, int(im.height * scale))
            max_h = int(box * 1.15)
            if new_h > max_h:
                scale = max_h / im.height
                new_h = max_h
            new_w = max(1, int(im.width * scale))
            art = im.resize((new_w, new_h), Image.LANCZOS)
    except Exception:
        art = None
    _art_cache[key] = art
    return art


def _font(size: int) -> ImageFont.ImageFont:
    if size in _font_cache:
        return _font_cache[size]
    for path in _SYS_FONTS:
        try:
            f = ImageFont.truetype(path, size)
            _font_cache[size] = f
            return f
        except (OSError, IOError):
            continue
    f = ImageFont.load_default()
    _font_cache[size] = f
    return f


def _text_w(draw: ImageDraw.ImageDraw, txt: str, font) -> int:
    box = draw.textbbox((0, 0), txt, font=font)
    return box[2] - box[0]


def _fit_text(draw, txt: str, max_w: int, start: int, floor: int = 22) -> ImageFont.ImageFont:
    """Shrink font size until txt fits max_w."""
    size = start
    while size > floor:
        f = _font(size)
        if _text_w(draw, txt, f) <= max_w:
            return f
        size -= 2
    return _font(floor)


_bg_cache: "Image.Image | None" = None


def _background() -> Image.Image:
    """assets/background.png if present, else a drawn gradient + diagonal glow.

    The composed background is built ONCE and cached; each render gets a
    fast .copy(). The pure-Python gradient loop (~900k pixel writes) and the
    disk open+resize previously ran every single round — this was the main
    card-render bottleneck.
    """
    global _bg_cache
    if _bg_cache is not None:
        return _bg_cache.copy()
    _bg_cache = _build_background()
    return _bg_cache.copy()


def _build_background() -> Image.Image:
    if os.path.exists(_ASSET_BG):
        try:
            source = Image.open(_ASSET_BG).convert("RGBA")
            # Cover-crop rather than stretch so the arena keeps its proportions.
            scale = max(W / source.width, H / source.height)
            resized = source.resize(
                (max(W, int(source.width * scale)), max(H, int(source.height * scale))),
                Image.LANCZOS,
            )
            left = (resized.width - W) // 2
            top = (resized.height - H) // 2
            return resized.crop((left, top, left + W, top + H))
        except Exception:
            pass
    bg = Image.new("RGBA", (W, H))
    px = bg.load()
    for y in range(H):
        t = y / H
        r = int(BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * t)
        for x in range(W):
            px[x, y] = (r, g, b, 255)
    # soft team glows in the corners
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-260, -180, 420, 420), fill=P1_ACCENT + (46,))
    gd.ellipse((W - 420, H - 420, W + 260, H + 180), fill=P2_ACCENT + (46,))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    bg.alpha_composite(glow)
    return bg


def _hp_color(pct: float) -> tuple:
    if pct > 0.55:
        return HP_HIGH
    if pct > 0.25:
        return HP_MID
    return HP_LOW


def _rounded_bar(draw, x, y, w, h, pct, fill, label: str, font):
    """Fat rounded progress bar with centred label."""
    pct = max(0.0, min(1.0, pct))
    r = h // 2
    draw.rounded_rectangle((x, y, x + w, y + h), radius=r, fill=BAR_BG)
    fw = int(w * pct)
    if fw > h:                                     # avoid degenerate radius
        draw.rounded_rectangle((x, y, x + fw, y + h), radius=r, fill=fill)
    elif fw > 0:
        draw.ellipse((x, y, x + max(fw, h), y + h), fill=fill)
    tw = _text_w(draw, label, font)
    draw.text((x + (w - tw) // 2, y + (h - font.size) // 2 - 2),
              label, font=font, fill=TEXT)


def _pips(draw, x, y, count, maximum, color, size=20, gap=8):
    """Stamina pips (filled/empty circles)."""
    maximum = max(1, int(maximum))
    count = max(0, min(maximum, int(round(count))))
    for i in range(maximum):
        x0 = x + i * (size + gap)
        if i < count:
            draw.ellipse((x0, y, x0 + size, y + size), fill=color)
        else:
            draw.ellipse((x0, y, x0 + size, y + size), outline=color, width=3)


_CHIP_COLORS = {
    "BURN":    (239, 68, 68),
    "SHIELD":  (96, 165, 250),
    "SILENCE": (168, 85, 247),
    "INVULN":  (250, 204, 21),
    "CRIT":    (244, 114, 182),
    "ATK":     (251, 146, 60),
    "DEF":     (52, 211, 153),
    "AMP":     (248, 113, 113),
    "PIERCE":  (129, 140, 248),
    "TRUE":    (232, 121, 249),
    "DEFLECT": (45, 212, 191),
    "REFLECT": (94, 234, 212),
    "SPC":     (96, 165, 250),
    "MODE":    (250, 204, 21),
    "STACK":   (251, 146, 60),
}


def _chips(draw, x, y, statuses: list[str], align_right: bool = False,
           max_w: int = 400):
    """Rounded status chips, auto-colored, wrapping to 2 rows (max 8 shown)."""
    f = _font(24)
    pad_x, pad_y, gap = 14, 7, 10
    row_h = f.size + pad_y * 2 + 10
    items = []
    for s in statuses[:8]:
        color = next((c for k, c in _CHIP_COLORS.items() if s.upper().startswith(k)),
                     (120, 120, 140))
        w = _text_w(draw, s, f) + pad_x * 2
        items.append((s, color, w))

    # pack into rows
    rows, cur, cur_w = [], [], 0
    for it in items:
        if cur and cur_w + gap + it[2] > max_w:
            rows.append(cur); cur, cur_w = [], 0
            if len(rows) == 2:
                break
        cur.append(it); cur_w += (gap if cur_w else 0) + it[2]
    if cur and len(rows) < 2:
        rows.append(cur)

    for ri, row in enumerate(rows):
        total = sum(w for *_, w in row) + gap * (len(row) - 1)
        cx = (x - total) if align_right else x
        ry = y + ri * row_h
        for s, color, w in row:
            h = f.size + pad_y * 2
            draw.rounded_rectangle((cx, ry, cx + w, ry + h), radius=h // 2,
                                   fill=color + (60,), outline=color, width=2)
            draw.text((cx + pad_x, ry + pad_y - 1), s, font=f, fill=TEXT)
            cx += w + gap


def _draw_blade_art(img: Image.Image, draw, side: str, name: str, accent):
    """Plain blade artwork (no frame/circle) anchored in the bottom corner.
    Drawn spinning-top placeholder if the PNG is missing."""
    art = _blade_art(name, _ART_BOX)
    margin = 44
    if art:
        # trim transparent padding from the fitted canvas
        bbox = art.getbbox()
        if bbox:
            art = art.crop(bbox)
        x = margin if side == "left" else W - margin - art.width
        y = H - 26 - art.height
        img.alpha_composite(art, (x, y))
        return
    # placeholder: minimal spinning-top silhouette (no frame)
    cx = margin + 90 if side == "left" else W - margin - 90
    cy = H - 130
    col = tuple(int(c * 0.9) for c in accent)
    draw.ellipse((cx - 56, cy - 40, cx + 56, cy + 4), fill=col + (80,), outline=col, width=3)
    draw.polygon([(cx - 42, cy - 8), (cx + 42, cy - 8), (cx, cy + 66)],
                 fill=col + (80,), outline=col)
    draw.ellipse((cx - 12, cy - 32, cx + 12, cy - 8), fill=(255, 255, 255, 55))


def _player_panel(img, draw, side: str, data: dict):
    """Compact game-HUD player panel. Returns the bottom of the stats block."""
    accent = P1_ACCENT if side == "left" else P2_ACCENT
    margin, panel_w = 54, 500
    x = margin if side == "left" else W - margin - panel_w
    right = side == "right"

    # translucent framed HUD block
    draw.rounded_rectangle((x - 16, PANEL_TOP - 14, x + panel_w + 16, 475),
                           radius=22, fill=(8, 11, 22, 205),
                           outline=accent + (210,), width=3)

    name = _sanitize(data.get("name", "?"))[:24]
    blade = _sanitize(data.get("blade", "?"))[:28]
    nf = _fit_text(draw, name, panel_w, 38, floor=22)
    nx = x if not right else x + panel_w - _text_w(draw, name, nf)
    draw.text((nx, PANEL_TOP), name, font=nf, fill=TEXT)
    bf = _fit_text(draw, blade, panel_w, 27, floor=19)
    bx = x if not right else x + panel_w - _text_w(draw, blade, bf)
    draw.text((bx, PANEL_TOP + 45), blade, font=bf, fill=accent)

    hp, mx = int(data.get("hp", 0)), max(1, int(data.get("max_hp", 1)))
    pct = hp / mx
    y = PANEL_TOP + 88
    _rounded_bar(draw, x, y, panel_w, 42, pct, _hp_color(pct),
                 f"HP   {max(0, hp)} / {mx}   {max(0, int(pct * 100))}%", _font(24))

    def stat_row(label, val, maximum, color, yy):
        maximum = max(1.0, float(maximum))
        val = float(val)
        lf = _font(21)
        draw.text((x, yy), label, font=lf, fill=SUBTEXT)
        bw, bx0 = 270, x + 116
        _rounded_bar(draw, bx0, yy + 2, bw, 18, val / maximum, color, "", _font(12))
        value = f"{val:g}/{maximum:g}"
        draw.text((x + panel_w - _text_w(draw, value, lf), yy), value, font=lf, fill=TEXT)

    sta = float(data.get("stamina", 0)); sta_max = float(data.get("max_stamina", 10) or 10)
    g = float(data.get("gauge", 0)); gm = float(data.get("gauge_max", 150) or 150)
    sv = float(data.get("stability", 0)); svm = float(data.get("stability_max", 100) or 100)
    stat_row("STAMINA", sta, sta_max, STA_COL, y + 58)
    stat_row("SPECIAL", g, gm, (168, 85, 247), y + 96)
    stat_row("STABILITY", sv, svm, GAUGE_COL if sv / svm > .25 else HP_LOW, y + 134)

    draw.line((x, y + 172, x + panel_w, y + 172), fill=accent + (100,), width=2)
    draw.text((x, y + 184), "ACTIVE EFFECTS", font=_font(18), fill=SUBTEXT)
    statuses = data.get("statuses") or []
    if statuses:
        _chips(draw, x + panel_w if right else x, y + 214, statuses,
               align_right=right, max_w=panel_w)
    else:
        draw.text((x if not right else x + panel_w - 170, y + 216),
                  "No active effects", font=_font(18), fill=(125, 130, 145))
    return 475


def render_battle_card(round_no: int, left: dict, right: dict) -> io.BytesIO:
    """Render the BEYCBOT Discord battle HUD as a compact JPEG."""
    img = _background()
    draw = ImageDraw.Draw(img, "RGBA")

    # central split and round header
    draw.polygon([(W // 2 - 30, 0), (W // 2 + 30, 0),
                  (W // 2 + 8, H), (W // 2 - 8, H)],
                 fill=(8, 10, 22, 150))
    title = f"ROUND {int(round_no)}"
    tf = _font(38); tw = _text_w(draw, title, tf)
    draw.rounded_rectangle(((W - tw) // 2 - 28, 24, (W + tw) // 2 + 28, 82),
                           radius=18, fill=(4, 7, 16, 230),
                           outline=(100, 120, 180), width=2)
    draw.text(((W - tw) // 2, 31), title, font=tf, fill=TEXT)

    _player_panel(img, draw, "left", left)
    _player_panel(img, draw, "right", right)

    # Bey art occupies the lower battle arena instead of leaving dead space.
    _draw_blade_art(img, draw, "left", str(left.get("blade", "")), P1_ACCENT)
    _draw_blade_art(img, draw, "right", str(right.get("blade", "")), P2_ACCENT)

    vf = _font(54); vw = _text_w(draw, "VS", vf)
    vcx, vcy = W // 2, 670
    draw.ellipse((vcx - 55, vcy - 55, vcx + 55, vcy + 55),
                 fill=(3, 6, 15, 220), outline=(120, 135, 185), width=3)
    draw.text((vcx - vw // 2, vcy - 34), "VS", font=vf, fill=TEXT)

    # Product name, not the repository/code name.
    brand = "BEYCBOT"
    bf = _font(18); bw = _text_w(draw, brand, bf)
    draw.text(((W - bw) // 2, H - 35), brand, font=bf, fill=(170, 175, 195))

    buf = io.BytesIO()
    # Battle cards contain large detailed bey artwork. Low-compression PNGs were
    # several times larger than necessary, so Discord/mobile clients could sit
    # on the attachment placeholder even though the round and buttons had
    # already arrived. JPEG keeps the card opaque (it is RGB already), encodes
    # quickly, and drastically reduces the bytes uploaded every round.
    img.convert("RGB").save(
        buf, format="JPEG", quality=90, subsampling=0, optimize=False
    )
    buf.seek(0)
    return buf
