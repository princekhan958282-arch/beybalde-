"""Beycord V2 info-card renderer.

A premium, phone-first redesign that keeps the current card implementation
available as a live rollback path.  This module intentionally renders with
Pillow only: the exact JPEG bytes produced here are the bytes Discord receives.
Remote artwork is fetched in a worker thread with a short timeout; any failure
falls back to the legacy renderer instead of breaking ;info.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import urllib.request
from functools import lru_cache
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from utils import info_card_legacy as legacy
from utils.bey_levels import STAT_BAR_MAX as STAT_MAX
from utils.hp_system import blade_hp_stat

log = logging.getLogger(__name__)

CARD_ENABLED = True
IMAGE_FORMAT = "jpg"
IMAGE_QUALITY = 90
W = 900
PAD = 34

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FONT_CANDIDATES = (
    os.path.join(_PROJECT_ROOT, "assets", "font.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
_FONT_CACHE: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}
_CARD_CACHE: dict[str, bytes] = {}
_CARD_CACHE_MAX = 48

_TYPE_ACCENT = {
    "Attack": (255, 100, 86),
    "Defense": (65, 235, 160),
    "Stamina": (80, 190, 255),
    "Balance": (235, 190, 90),
}


def _font(size: int, bold: bool = True):
    key = (int(size), bool(bold))
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    paths = list(_FONT_CANDIDATES)
    if not bold:
        paths = [
            os.path.join(_PROJECT_ROOT, "assets", "font.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    for path in paths:
        try:
            f = ImageFont.truetype(path, size)
            _FONT_CACHE[key] = f
            return f
        except Exception:
            continue
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def _hex(value: str) -> tuple[int, int, int]:
    s = str(value or "#ffffff").lstrip("#")
    if len(s) != 6:
        return (255, 255, 255)
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (255, 255, 255)


def _mix(a, b, t: float):
    return tuple(int(a[i] * (1.0 - t) + b[i] * t) for i in range(3))


def _rr(draw, box, radius: int, *, fill=None, outline=None, width: int = 1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _tw(draw, text: str, font) -> int:
    try:
        return int(draw.textlength(str(text), font=font))
    except Exception:
        return int(draw.textbbox((0, 0), str(text), font=font)[2])


def _wrap(draw, text: str, font, max_width: int, max_lines: int | None = None) -> list[str]:
    words = str(text or "").replace("\n", " ").split()
    if not words:
        return [""]
    lines: list[str] = []
    cur = ""
    for word in words:
        probe = f"{cur} {word}".strip()
        if not cur or _tw(draw, probe, font) <= max_width:
            cur = probe
            continue
        lines.append(cur)
        cur = word
        if max_lines and len(lines) >= max_lines:
            break
    if cur and (not max_lines or len(lines) < max_lines):
        lines.append(cur)
    if max_lines and len(lines) == max_lines and words:
        # Ellipsise only when the last line cannot represent the full source.
        joined = " ".join(lines)
        if len(joined) < len(" ".join(words)):
            last = lines[-1]
            while last and _tw(draw, last + "…", font) > max_width:
                last = last[:-1]
            lines[-1] = last.rstrip() + "…"
    return lines or [""]


def _fit_font(draw, text: str, max_width: int, start: int, minimum: int = 20):
    for size in range(start, minimum - 1, -1):
        f = _font(size)
        if _tw(draw, text, f) <= max_width:
            return f
    return _font(minimum)


def _normalize_type(value: object) -> str:
    s = str(value or "Balance").strip().title()
    return s if s in _TYPE_ACCENT else "Balance"


def _normalize_spin(value: object) -> str:
    s = str(value or "Right").strip().title()
    return s if s in ("Right", "Left", "Dual") else "Right"


def _panel(draw, box, accent, *, fill=(7, 18, 31, 232), radius=24, width=2):
    _rr(draw, box, radius, fill=fill, outline=accent + (180,), width=width)


def _draw_glow(img: Image.Image, box, colour, blur: int = 35, alpha: int = 90):
    scale = 3
    x0, y0, x1, y1 = box
    w = max(1, (x1 - x0) // scale)
    h = max(1, (y1 - y0) // scale)
    layer = Image.new("RGBA", (w + blur * 2, h + blur * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((blur, blur, blur + w, blur + h), fill=colour + (alpha,))
    layer = layer.filter(ImageFilter.GaussianBlur(max(1, blur // scale)))
    layer = layer.resize(((w + blur * 2) * scale, (h + blur * 2) * scale), Image.BILINEAR)
    img.alpha_composite(layer, (x0 - blur * scale, y0 - blur * scale))


def _load_source(path_or_url: str) -> Optional[Image.Image]:
    if not path_or_url:
        return None
    try:
        if os.path.isfile(path_or_url):
            return Image.open(path_or_url).convert("RGBA")
        req = urllib.request.Request(
            path_or_url,
            headers={"User-Agent": "Beycord/2.0 info-card"},
        )
        with urllib.request.urlopen(req, timeout=5.0) as response:
            data = response.read(8 * 1024 * 1024)
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception as exc:
        log.debug("V2 art load failed: %s", exc)
        return None


@lru_cache(maxsize=96)
def _art_cached(name: str, image_url: str, box: int, scale: float, position: str) -> Optional[Image.Image]:
    path = None
    try:
        path = legacy._local_art_path(name)
    except Exception:
        path = None
    src = _load_source(path or image_url)
    if src is None:
        return None

    # Trim transparent padding when possible so the Bey reads larger without
    # changing the authored image itself.
    try:
        alpha = src.getchannel("A")
        bbox = alpha.getbbox()
        if bbox:
            src = src.crop(bbox)
    except Exception:
        pass

    scale = max(1.0, min(float(scale or 1.0), 2.2))
    target = int(box * 0.91 * scale)
    src.thumbnail((target, target), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (box, box), (0, 0, 0, 0))

    px = py = 50.0
    m = re.match(r"^(\d{1,3}(?:\.\d+)?)%\s+(\d{1,3}(?:\.\d+)?)%$", str(position or ""))
    if m:
        px = max(0.0, min(100.0, float(m.group(1))))
        py = max(0.0, min(100.0, float(m.group(2))))
    free_x = box - src.width
    free_y = box - src.height
    x = int(free_x * px / 100.0)
    y = int(free_y * py / 100.0)
    canvas.alpha_composite(src, (x, y))
    return canvas


def _art(blade: dict, box: int) -> Optional[Image.Image]:
    name = str(blade.get("name", ""))
    url = str(blade.get("image_url", "") or "")
    try:
        scale = float(blade.get("art_scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        scale = 1.0
    pos = str(blade.get("art_position", "") or "")
    hit = _art_cached(name, url, box, scale, pos)
    return hit.copy() if hit is not None else None


def _stat_values(blade: dict) -> list[tuple[str, int]]:
    st = blade.get("stats") or {}
    try:
        hp = int(round(float(blade_hp_stat(blade))))
    except Exception:
        hp = int(st.get("hp", 0) or 0)
    defn = st.get("defense", st.get("defence", 0))
    return [
        ("HP", hp),
        ("ATK", int(round(float(st.get("attack", 0) or 0)))),
        ("DEF", int(round(float(defn or 0)))),
        ("STA", int(round(float(st.get("stamina", 0) or 0)))),
        ("SPC", int(round(float(st.get("special", st.get("burst", 0)) or 0)))),
    ]


def _ability_blocks(blade: dict, draw, width: int) -> tuple[list[tuple[dict, list[str], str, int]], int]:
    abilities = legacy._collect_abilities(blade)
    desc_font = _font(18, False)
    name_font = _font(25)
    out = []
    total = 0
    max_lines = 6 if len(abilities) <= 2 else 5
    for ab in abilities:
        desc = _wrap(draw, str(ab.get("description", "")), desc_font, width - 40, max_lines)
        chip = legacy._chip_for(ab)
        h = 22 + name_font.size + 12 + 28 + 12 + len(desc) * 24 + 20
        out.append((ab, desc, chip, h))
        total += h + 14
    if not out:
        total = 110
    return out, max(110, total - 14)


def _cache_key(blade: dict, parts: dict) -> str:
    raw = json.dumps([blade, parts], sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _named(data: bytes, name: str = "bey_info_v2.jpg") -> io.BytesIO:
    buf = io.BytesIO(data)
    buf.name = name
    buf.seek(0)
    return buf


def card_filename(buf: io.BytesIO, blade_name: str) -> str:
    return legacy.card_filename(buf, blade_name)


def render_info_card_pillow(blade: dict, parts: Optional[dict] = None) -> Optional[io.BytesIO]:
    try:
        return _render(blade or {}, parts or {})
    except Exception as exc:
        log.warning("V2 info card failed for %r: %s", (blade or {}).get("name"), exc)
        return None


def _render(blade: dict, parts: dict) -> io.BytesIO:
    rarity = str(blade.get("rarity", "Common"))
    theme = legacy.theme_for(blade)
    rarity_accent = _hex(theme.get("accent", "#5aa9ff"))
    glow = _hex(theme.get("glow", "#1d4ed8"))
    tint = _hex(theme.get("tint", "#0d1420"))
    btype = _normalize_type(blade.get("type"))
    type_accent = _TYPE_ACCENT[btype]
    spin = _normalize_spin(blade.get("spin_direction"))

    # Measure ability content before allocating the dynamic-height canvas.
    meas_img = Image.new("RGB", (W, 100), "black")
    meas = ImageDraw.Draw(meas_img)
    stats_w = 332
    gap = 18
    ab_w = W - PAD * 2 - stats_w - gap
    ability_blocks, abilities_h = _ability_blocks(blade, meas, ab_w - 26)
    stats_h = 386
    mid_h = max(stats_h, abilities_h + 62)

    sm = blade.get("special_move") or {}
    special_desc = str(sm.get("description", "") or "")
    sp_desc_lines = _wrap(meas, special_desc, _font(18, False), W - PAD * 2 - 54, 4)
    special_h = 176 + len(sp_desc_lines) * 22 if sm.get("name") else 0

    HERO_TOP = 105
    HERO_H = 430
    NAME_H = 110
    PARTS_H = 66
    SECTION_GAP = 18
    y_name = HERO_TOP + HERO_H
    y_parts = y_name + NAME_H
    y_mid = y_parts + PARTS_H + SECTION_GAP
    y_special = y_mid + mid_h + SECTION_GAP
    H = y_special + special_h + PAD + (32 if sm.get("name") else 0)

    # Background gradient.
    strip = Image.new("RGB", (1, H))
    px = strip.load()
    dark = (4, 10, 18)
    for y in range(H):
        t = y / max(1, H - 1)
        if t < 0.5:
            c = _mix(_mix(tint, dark, 0.18), dark, t * 0.52)
        else:
            c = _mix(_mix(tint, dark, 0.46), dark, (t - 0.5) * 0.84)
        px[0, y] = c
    img = strip.resize((W, H), Image.Resampling.BILINEAR).convert("RGBA")

    # Premium glows kept diffuse so text remains crisp.
    _draw_glow(img, (W // 2 - 255, 45, W // 2 + 255, 520), glow, 55, 105)
    _draw_glow(img, (W // 2 - 170, 160, W // 2 + 170, 475), type_accent, 48, 78)

    d = ImageDraw.Draw(img)
    text = (244, 248, 252, 255)
    muted = (153, 176, 197, 255)
    panel_fill = (5, 17, 30, 224)

    # Outer frame and corner accents.
    _rr(d, (7, 7, W - 8, H - 8), 30, fill=None, outline=rarity_accent + (220,), width=3)
    _rr(d, (15, 15, W - 16, H - 16), 24, fill=None, outline=(115, 165, 205, 90), width=1)
    for x in (28, W - 145):
        d.line((x, 28, x + 115, 28), fill=rarity_accent + (180,), width=4)

    # Header badges.
    badge_f = _font(23)
    left_w = max(150, _tw(d, rarity.upper(), badge_f) + 62)
    _rr(d, (PAD, 34, PAD + left_w, 88), 16, fill=(5, 15, 28, 235), outline=rarity_accent + (220,), width=2)
    d.ellipse((PAD + 16, 51, PAD + 32, 67), fill=rarity_accent + (255,))
    d.text((PAD + 44, 48), rarity.upper(), font=badge_f, fill=rarity_accent + (255,))

    spin_txt = f"{spin.upper()} SPIN"
    sw = _tw(d, spin_txt, badge_f) + 76
    x0 = W - PAD - sw
    _rr(d, (x0, 34, W - PAD, 88), 16, fill=(5, 15, 28, 235), outline=type_accent + (210,), width=2)
    d.text((x0 + 18, 47), "R" if spin == "Right" else "L" if spin == "Left" else "↔", font=_font(25), fill=type_accent + (255,))
    d.text((x0 + 52, 48), spin_txt, font=badge_f, fill=text)

    # Hero medallion.
    cx = W // 2
    cy = HERO_TOP + HERO_H // 2 - 4
    outer_r = 190
    d.ellipse((cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r), fill=(6, 18, 31, 238), outline=rarity_accent + (210,), width=4)
    d.ellipse((cx - outer_r + 13, cy - outer_r + 13, cx + outer_r - 13, cy + outer_r - 13), outline=type_accent + (200,), width=4)
    for off, start, end, col in (
        (212, 205, 325, rarity_accent),
        (224, 20, 132, type_accent),
    ):
        d.arc((cx - off, cy - off, cx + off, cy + off), start=start, end=end, fill=col + (210,), width=5)

    art_box = 348
    art = _art(blade, art_box)
    if art is not None:
        # Light halo under transparent art.
        halo = Image.new("RGBA", (art_box, art_box), (0, 0, 0, 0))
        hd = ImageDraw.Draw(halo)
        hd.ellipse((28, 28, art_box - 28, art_box - 28), fill=type_accent + (45,))
        halo = halo.filter(ImageFilter.GaussianBlur(28))
        img.alpha_composite(halo, (cx - art_box // 2, cy - art_box // 2))
        img.alpha_composite(art, (cx - art_box // 2, cy - art_box // 2))
        d = ImageDraw.Draw(img)
    else:
        d.text((cx, cy), "B", font=_font(112), fill=rarity_accent + (120,), anchor="mm")

    # Small side microcopy for visual depth, kept intentionally generic.
    micro = _font(13, False)
    d.text((PAD + 12, HERO_TOP + 85), "BEYCORD", font=_font(14), fill=muted)
    d.text((PAD + 12, HERO_TOP + 112), "BLADE DATABASE", font=micro, fill=(108, 143, 173, 255))
    d.text((W - PAD - 154, HERO_TOP + 92), "STABILITY", font=_font(14), fill=muted)
    d.text((W - PAD - 154, HERO_TOP + 118), "CREATES OPENINGS", font=micro, fill=(108, 143, 173, 255))

    # Name plate.
    name = str(blade.get("name", "Unknown"))
    _panel(d, (PAD, y_name + 2, W - PAD, y_name + 92), rarity_accent, fill=(4, 15, 27, 245), radius=26, width=2)
    nf = _fit_font(d, name, W - PAD * 2 - 100, 48, 28)
    d.text((W // 2, y_name + 17), name, font=nf, fill=text, anchor="ma")

    pill = legacy._TYPE_LABEL.get(btype, f"{btype.upper()} TYPE")
    pf = _font(18)
    pw = _tw(d, pill, pf) + 56
    pill_x = W // 2 - pw // 2
    _rr(d, (pill_x, y_name + 65, pill_x + pw, y_name + 100), 17, fill=(4, 25, 28, 240), outline=type_accent + (230,), width=2)
    d.text((W // 2, y_name + 72), pill, font=pf, fill=type_accent + (255,), anchor="ma")

    bid = str(blade.get("id", "") or "")
    if bid:
        bw = _tw(d, bid, _font(17)) + 32
        bx1 = W - PAD - 8
        _rr(d, (bx1 - bw, y_name + 65, bx1, y_name + 100), 14, fill=(7, 21, 35, 235), outline=rarity_accent + (190,), width=2)
        d.text((bx1 - bw // 2, y_name + 73), bid, font=_font(17), fill=text, anchor="ma")

    try:
        level = int(blade.get("level") or 0)
    except (TypeError, ValueError):
        level = 0
    if level > 0:
        lv = f"LV {level}"
        lw = _tw(d, lv, _font(17)) + 30
        lx = PAD + 8
        _rr(d, (lx, y_name + 65, lx + lw, y_name + 100), 14, fill=(7, 21, 35, 235), outline=rarity_accent + (190,), width=2)
        d.text((lx + lw // 2, y_name + 73), lv, font=_font(17), fill=text, anchor="ma")

    # Parts strip.
    _panel(d, (PAD, y_parts + 7, W - PAD, y_parts + 58), rarity_accent, fill=(5, 17, 30, 225), radius=16, width=1)
    part_f = _font(16)
    value_f = _font(17)
    part_values = [
        ("BLADE", str(blade.get("blade_part") or blade.get("name") or "—")),
        ("RATCHET", str(parts.get("ratchet") or blade.get("ratchet") or "—")),
        ("BIT", str(parts.get("bit") or blade.get("bit") or "—")),
    ]
    seg = (W - PAD * 2) // 3
    for i, (label, value) in enumerate(part_values):
        x = PAD + i * seg
        if i:
            d.line((x, y_parts + 17, x, y_parts + 48), fill=(89, 133, 165, 120), width=1)
        d.text((x + 16, y_parts + 22), f"{label}:", font=part_f, fill=rarity_accent + (255,))
        max_w = seg - 24 - _tw(d, f"{label}:", part_f) - 10
        vf = _fit_font(d, value, max_w, 17, 12)
        d.text((x + 22 + _tw(d, f"{label}:", part_f), y_parts + 21), value, font=vf, fill=text)

    # Stats panel.
    sx0, sy0 = PAD, y_mid
    sx1 = sx0 + stats_w
    _panel(d, (sx0, sy0, sx1, sy0 + mid_h), rarity_accent, fill=panel_fill, radius=22, width=2)
    d.text((sx0 + 22, sy0 + 18), "STATS", font=_font(27), fill=text)
    d.line((sx0 + 22, sy0 + 57, sx1 - 22, sy0 + 57), fill=rarity_accent + (115,), width=2)

    values = _stat_values(blade)
    total = sum(v for _, v in values)
    row_y = sy0 + 76
    label_f = _font(18)
    num_f = _font(21)
    bar_x0 = sx0 + 82
    bar_x1 = sx1 - 54
    for idx, (label, value) in enumerate(values):
        y = row_y + idx * 54
        d.text((sx0 + 22, y + 7), label, font=label_f, fill=muted)
        _rr(d, (bar_x0, y + 9, bar_x1, y + 27), 9, fill=(25, 45, 61, 255), outline=(78, 106, 129, 90), width=1)
        pct = max(0.0, min(1.0, float(value) / max(1.0, float(STAT_MAX))))
        fill_w = int((bar_x1 - bar_x0) * pct)
        if value > 0:
            fill_col = type_accent if label == "DEF" else rarity_accent
            _rr(d, (bar_x0, y + 9, bar_x0 + max(10, fill_w), y + 27), 9, fill=fill_col + (240,))
        d.text((sx1 - 22, y + 3), str(value), font=num_f, fill=text, anchor="ra")

    ty = row_y + len(values) * 54 + 2
    d.line((sx0 + 22, ty, sx1 - 22, ty), fill=(100, 137, 165, 135), width=1)
    d.text((sx0 + 22, ty + 18), "TOTAL", font=_font(19), fill=muted)
    d.text((sx1 - 22, ty + 11), str(total), font=_font(28), fill=text, anchor="ra")

    # Abilities panel.
    ax0 = sx1 + gap
    ax1 = W - PAD
    _panel(d, (ax0, sy0, ax1, sy0 + mid_h), rarity_accent, fill=panel_fill, radius=22, width=2)
    d.text((ax0 + 22, sy0 + 18), f"ABILITIES  {len(ability_blocks)}", font=_font(27), fill=text)
    d.line((ax0 + 22, sy0 + 57, ax1 - 22, sy0 + 57), fill=rarity_accent + (115,), width=2)

    ay = sy0 + 72
    if not ability_blocks:
        d.text((ax0 + 22, ay + 20), "No active ability data.", font=_font(18, False), fill=muted)
    else:
        for ab, lines, chip, block_h in ability_blocks:
            box = (ax0 + 12, ay, ax1 - 12, ay + block_h)
            _rr(d, box, 17, fill=(6, 23, 38, 242), outline=(89, 139, 176, 150), width=1)
            name = str(ab.get("name", "Ability"))
            name_f = _fit_font(d, name, ax1 - ax0 - 74, 24, 17)
            d.text((ax0 + 27, ay + 16), name, font=name_f, fill=text)
            chip_f = _font(13)
            cw = min(ax1 - ax0 - 50, _tw(d, chip, chip_f) + 30)
            _rr(d, (ax0 + 27, ay + 51, ax0 + 27 + cw, ay + 78), 13, fill=(3, 31, 41, 245), outline=type_accent + (215,), width=1)
            d.text((ax0 + 27 + cw // 2, ay + 57), chip[:42], font=chip_f, fill=type_accent + (255,), anchor="ma")
            yy = ay + 91
            for line in lines:
                d.text((ax0 + 27, yy), line, font=_font(18, False), fill=(218, 229, 238, 255))
                yy += 24
            ay += block_h + 14

    # Special move panel.
    if sm.get("name"):
        sy = y_special
        _panel(d, (PAD, sy, W - PAD, sy + special_h), rarity_accent, fill=(4, 16, 29, 240), radius=22, width=2)
        d.text((PAD + 22, sy + 16), "SPECIAL MOVE", font=_font(20), fill=rarity_accent + (255,))
        sm_name = str(sm.get("name", "Special Move"))
        smf = _fit_font(d, sm_name, W - PAD * 2 - 260, 38, 24)
        d.text((PAD + 22, sy + 49), sm_name, font=smf, fill=text)

        # Damage badge on the right.
        dmg = sm.get("total_damage") or sm.get("damage_per_hit") or 0
        try:
            dmg_i = int(round(float(dmg)))
        except (TypeError, ValueError):
            dmg_i = 0
        if dmg_i:
            label = f"{dmg_i} DMG"
            dw = _tw(d, label, _font(29)) + 54
            dx1 = W - PAD - 20
            _rr(d, (dx1 - dw, sy + 45, dx1, sy + 94), 17, fill=(5, 29, 29, 245), outline=type_accent + (225,), width=2)
            d.text((dx1 - dw // 2, sy + 55), label, font=_font(29), fill=type_accent + (255,), anchor="ma")

        yy = sy + 108
        for line in sp_desc_lines:
            d.text((PAD + 22, yy), line, font=_font(18, False), fill=(205, 220, 232, 255))
            yy += 22

    # Footer.
    footer = "BEYCORD  //  BLADE DATABASE"
    d.text((W // 2, H - 28), footer, font=_font(12, False), fill=(92, 126, 153, 220), anchor="mm")

    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=IMAGE_QUALITY, optimize=True, progressive=True)
    out.name = "bey_info_v2.jpg"
    out.seek(0)
    return out


async def render_info_card(blade: dict, parts: Optional[dict] = None) -> Optional[io.BytesIO]:
    """Render V2 off the event loop; fall back to the exact legacy renderer."""
    if not CARD_ENABLED:
        return None
    blade = blade or {}
    parts = parts or {}
    key = _cache_key(blade, parts)
    cached = _CARD_CACHE.get(key)
    if cached is not None:
        return _named(cached)

    try:
        buf = await asyncio.to_thread(render_info_card_pillow, blade, parts)
    except Exception as exc:
        log.warning("V2 worker failed for %r: %s", blade.get("name"), exc)
        buf = None

    if buf is None:
        return await legacy.render_info_card(blade, parts=parts)

    data = buf.getvalue()
    if len(_CARD_CACHE) >= _CARD_CACHE_MAX:
        _CARD_CACHE.pop(next(iter(_CARD_CACHE)))
    _CARD_CACHE[key] = data
    return _named(data)


def clear_cache() -> None:
    _CARD_CACHE.clear()
    _art_cached.cache_clear()
    try:
        legacy.clear_cache()
    except Exception:
        pass


def refresh_art_index() -> None:
    try:
        legacy.refresh_art_index()
    except Exception:
        pass
    clear_cache()


async def shutdown() -> None:
    # V2 owns no browser, but the legacy renderer and boss-card renderer share
    # the legacy browser context and still need the normal shutdown path.
    try:
        await legacy.shutdown()
    except Exception:
        pass


# Compatibility surface.  Existing boss cards/tests import helpers and mutable
# browser state from utils.info_card.  The package aliases that import to this
# module on startup; anything V2 does not own is delegated to the frozen legacy
# module so the redesign cannot accidentally break a non-;info surface.
theme_for = legacy.theme_for
_TYPE_LABEL = legacy._TYPE_LABEL
_SPIN_ICON = legacy._SPIN_ICON
_collect_abilities = legacy._collect_abilities
_chip_for = legacy._chip_for
_stat_rows = legacy._stat_rows
_stat_total = legacy._stat_total
_parts_slots = legacy._parts_slots
build_html = legacy.build_html
_get_context = legacy._get_context


def __getattr__(name: str):
    return getattr(legacy, name)
