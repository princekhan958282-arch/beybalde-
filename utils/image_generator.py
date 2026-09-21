"""
utils/image_generator.py — BEYCBOT battle card renderer (Pillow)
================================================================
Generates a phone-first battle status card as a JPEG buffer.

Design goals
------------
* Plain dark canvas; no background image is loaded. ``assets/font.ttf``
  remains an optional font override.
* Phone-first: big type, fat bars, high contrast.  Discord scales the image
  to chat width; at 1000×1040 everything stays readable on a 6" screen.
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
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont

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
W, H = 1000, 1040
_ART_BOX = 420        # blade-art max size (no frame)
PANEL_TOP = 110       # player panels at top; art sits in the bottom zone

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
_cache_lock = threading.RLock()
# A cold render needs three independent decode/resize jobs: the background and
# both Bey artworks. Pillow performs those operations in native code, so doing
# them concurrently cuts first-card latency without changing any pixels.
_asset_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="battle-card-assets")


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
    with _cache_lock:
        if key in _art_cache:
            return _art_cache[key]
    art = None
    try:
        with _cache_lock:
            if _art_index is None:
                index: dict[str, str] = {}
                if os.path.isdir(_BEY_DIR):
                    for f in os.listdir(_BEY_DIR):
                        # WebP/JPG accepted alongside PNG: tools/optimize_assets.py
                        # stores art as alpha-preserving WebP (~8% of the PNG size).
                        if (f.lower().endswith((".png", ".webp", ".jpg", ".jpeg"))
                                and not f.startswith("_")):
                            index[_norm_name(f)] = os.path.join(_BEY_DIR, f)
                _art_index = index
            path = _art_index.get(_norm_name(name))
        if path:
            with Image.open(path) as source:
                im = source.convert("RGBA")
                bbox = im.getbbox()             # trim transparent padding first
                if bbox:
                    im = im.crop(bbox)
                # Scale by WIDTH so every bey shows at the same on-card diameter,
                # regardless of the source file's aspect (tall screenshots no
                # longer render tiny). Cap height so very tall arts don't overflow.
                scale = box / im.width
                new_h = max(1, int(im.height * scale))
                max_h = box
                if new_h > max_h:
                    scale = max_h / im.height
                    new_h = max_h
                new_w = max(1, int(im.width * scale))
                art = im.resize((new_w, new_h), Image.LANCZOS)
                # Preserve the old draw-time trim exactly, but do it once per
                # source artwork instead of copying pixels again every round.
                fitted_bbox = art.getbbox()
                if fitted_bbox:
                    art = art.crop(fitted_bbox)
    except Exception:
        art = None
    with _cache_lock:
        # Another battle may have completed the same asset while this thread
        # was decoding it. Reuse that canonical cached image when it exists.
        return _art_cache.setdefault(key, art)


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


@lru_cache(maxsize=1024)
def _cached_text_width(txt: str, size: int) -> int:
    box = _font(size).getbbox(txt)
    return box[2] - box[0]


def _text_w(draw: ImageDraw.ImageDraw, txt: str, font) -> int:
    size = getattr(font, "size", None)
    if size is not None:
        return _cached_text_width(txt, size)
    box = draw.textbbox((0, 0), txt, font=font)
    return box[2] - box[0]


@lru_cache(maxsize=1024)
def _cached_text_mask(txt: str, size: int):
    """Cache FreeType rasterization; the mask is independent of text color."""
    font = _font(size)
    if not isinstance(font, ImageFont.FreeTypeFont):
        return None
    return font.getmask2(txt, "L")


def _draw_text(draw: ImageDraw.ImageDraw, xy, txt: str, font, fill) -> None:
    """Draw text with Pillow-identical cached glyph masks.

    Pillow normally rasterizes every label on every round. Battle-card text
    uses integer coordinates, no stroke, and the same process-wide fonts, so
    its native mask can be safely reused while color and placement stay fully
    dynamic.
    """
    size = getattr(font, "size", None)
    cached = _cached_text_mask(txt, size) if size is not None else None
    if cached is None:
        draw.text(xy, txt, font=font, fill=fill)
        return
    mask, offset = cached
    ink, fill_ink = draw._getink(fill)
    ink = ink if ink is not None else fill_ink
    x, y = int(xy[0]) + offset[0], int(xy[1]) + offset[1]
    draw.draw.draw_bitmap((x, y), mask, ink)


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
    """Return a copy of the cached plain dark canvas."""
    global _bg_cache
    with _cache_lock:
        cached = _bg_cache
    if cached is None:
        built = _build_background()
        with _cache_lock:
            if _bg_cache is None:
                _bg_cache = built
            cached = _bg_cache
    return cached.copy()


def _build_background() -> Image.Image:
    """Plain dark canvas: no theme image, gradient, or glow processing."""
    return Image.new("RGBA", (W, H), BG_TOP + (255,))


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
    _draw_text(draw, (x + (w - tw) // 2, y + (h - font.size) // 2 - 2),
               label, font, TEXT)


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
    f = _font(28)
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
            _draw_text(draw, (cx + pad_x, ry + pad_y - 1), s, f, TEXT)
            cx += w + gap


def _draw_blade_art(img: Image.Image, draw, side: str, name: str, accent,
                    art: "Image.Image | None" = None):
    """Plain blade artwork (no frame/circle) anchored in the bottom corner.
    Drawn spinning-top placeholder if the PNG is missing."""
    if art is None:
        art = _blade_art(name, _ART_BOX)
    margin = 28
    if art:
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
    """One player's half. side: 'left' | 'right'."""
    accent = P1_ACCENT if side == "left" else P2_ACCENT
    margin = 28
    panel_w = 440
    x = margin if side == "left" else W - margin - panel_w
    right = side == "right"

    # accent tab
    tab_x = x - 14 if not right else x + panel_w + 6
    draw.rounded_rectangle((tab_x, PANEL_TOP - 8, tab_x + 8, PANEL_TOP + 352), radius=4, fill=accent)

    # name + blade
    name  = _sanitize(data.get("name", "?"))[:20]
    blade = _sanitize(data.get("blade", "?"))[:26]
    nf = _fit_text(draw, name, panel_w, 46)
    y = PANEL_TOP
    nx = x if not right else x + panel_w - _text_w(draw, name, nf)
    _draw_text(draw, (nx, y), name, nf, TEXT)
    y += nf.size + 8
    bf = _fit_text(draw, blade, panel_w, 36, floor=26)
    bx = x if not right else x + panel_w - _text_w(draw, blade, bf)
    _draw_text(draw, (bx, y), blade, bf, accent)
    y += bf.size + 26

    # HP bar
    hp, mx = int(data.get("hp", 0)), max(1, int(data.get("max_hp", 1)))
    pct = hp / mx
    _rounded_bar(draw, x, y, panel_w, 54, pct, _hp_color(pct),
                 f"{max(0, hp)} / {mx}", _font(34))
    y += 54 + 20

    # stamina pips + value
    sta, sta_max = float(data.get("stamina", 0)), int(data.get("max_stamina", 10) or 10)
    pip_max = min(sta_max, 10)
    pip_val = sta / sta_max * pip_max
    lab = f"{sta:g}/{sta_max}"
    lf = _font(30)
    if right:
        lw = _text_w(draw, lab, lf)
        _draw_text(draw, (x + panel_w - lw, y - 2), lab, lf, SUBTEXT)
        _pips(draw, x + panel_w - lw - 12 - pip_max * 28, y, pip_val, pip_max, STA_COL)
    else:
        _pips(draw, x, y, pip_val, pip_max, STA_COL)
        _draw_text(draw, (x + pip_max * 28 + 12, y - 2), lab, lf, SUBTEXT)
    y += 40

    # special gauge (thin)
    g, gm = float(data.get("gauge", 0)), max(1, float(data.get("gauge_max", 150)))
    _rounded_bar(draw, x, y, panel_w, 24, g / gm, GAUGE_COL, "", _font(14))
    gl = _font(28)
    gtxt = f"SPECIAL {int(g)}/{int(gm)}"
    gx = x if not right else x + panel_w - _text_w(draw, gtxt, gl)
    _draw_text(draw, (gx, y + 26), gtxt, gl, SUBTEXT)
    y += 66

    # stability bar (thin, steel)
    sv, svm = float(data.get("stability", 0)), max(1, float(data.get("stability_max", 100)))
    spct = sv / svm
    scol = (148, 163, 184) if spct > 0.25 else HP_LOW
    _rounded_bar(draw, x, y, panel_w, 24, spct, scol, "", _font(14))
    stxt = f"STABILITY {int(sv)}/{int(svm)}"
    sx = x if not right else x + panel_w - _text_w(draw, stxt, gl)
    _draw_text(draw, (sx, y + 26), stxt, gl, SUBTEXT)
    y += 66

    # status chips
    statuses = data.get("statuses") or []
    if statuses:
        if right:
            _chips(draw, x + panel_w, y, statuses, align_right=True, max_w=panel_w)
        else:
            _chips(draw, x, y, statuses, max_w=panel_w)


def render_battle_card(round_no: int, left: dict, right: dict) -> io.BytesIO:
    """Render the BEYCBOT Discord battle HUD as a compact mobile-first JPEG.

    The classic 1000x1040 layout and 4:2:0 JPEG output keep attachments small
    enough to leave the mobile placeholder quickly while preserving the same
    HUD content, artwork, and battle state.
    """
    left_blade = str(left.get("blade", ""))
    right_blade = str(right.get("blade", ""))
    left_key = f"{_norm_name(left_blade)}@{_ART_BOX}"
    right_key = f"{_norm_name(right_blade)}@{_ART_BOX}"
    with _cache_lock:
        background_ready = _bg_cache is not None
        left_ready = left_key in _art_cache
        right_ready = right_key in _art_cache
        cached_background = _bg_cache
        left_art = _art_cache.get(left_key)
        right_art = _art_cache.get(right_key)

    if background_ready and left_ready and right_ready:
        # Consecutive rounds take this allocation-only fast path. Avoiding
        # executor scheduling here matters because a warm render is ~20 ms.
        img = cached_background.copy()
    else:
        # On the first render, decode/resize only the missing independent image
        # assets in parallel. Following rounds use the fast path above.
        futures = {}
        if not background_ready:
            futures["background"] = _asset_executor.submit(_background)
        if not left_ready:
            futures["left"] = _asset_executor.submit(_blade_art, left_blade, _ART_BOX)
        if not right_ready:
            futures["right"] = _asset_executor.submit(_blade_art, right_blade, _ART_BOX)
        img = (futures["background"].result() if "background" in futures
               else cached_background.copy())
        if "left" in futures:
            left_art = futures["left"].result()
        if "right" in futures:
            right_art = futures["right"].result()
    draw = ImageDraw.Draw(img, "RGBA")

    # header
    title = f"ROUND {int(round_no)}"
    tf = _font(40)
    tw = _text_w(draw, title, tf)
    draw.rounded_rectangle(((W - tw) // 2 - 26, 30, (W + tw) // 2 + 26, 92),
                           radius=31, fill=(0, 0, 0, 110), outline=(90, 90, 120), width=2)
    draw.text(((W - tw) // 2, 38), title, font=tf, fill=TEXT)

    _player_panel(img, draw, "left", left)
    _player_panel(img, draw, "right", right)

    # Bey art occupies the lower battle arena instead of leaving dead space.
    _draw_blade_art(img, draw, "left", left_blade, P1_ACCENT, left_art)
    _draw_blade_art(img, draw, "right", right_blade, P2_ACCENT, right_art)

    # small "VS" badge centered between the two bottom artworks
    bvf = _font(46)
    bvw = _text_w(draw, "VS", bvf)
    bcx, bcy = W // 2, H - 210
    draw.ellipse((bcx - 48, bcy - 48, bcx + 48, bcy + 48), fill=(0, 0, 0, 150),
                 outline=(120, 120, 150), width=3)
    draw.text((bcx - bvw // 2, bcy - bvf.size // 2 - 4), "VS", font=bvf, fill=TEXT)

    buf = io.BytesIO()
    # Battle cards contain large detailed bey artwork. Low-compression PNGs were
    # several times larger than necessary, so Discord/mobile clients could sit
    # on the attachment placeholder even though the round and buttons had
    # already arrived. JPEG keeps the card opaque (it is RGB already), encodes
    # quickly, and drastically reduces the bytes uploaded every round.
    img.convert("RGB").save(
        buf, format="JPEG", quality=88, subsampling=2, optimize=False
    )
    buf.seek(0)
    return buf
