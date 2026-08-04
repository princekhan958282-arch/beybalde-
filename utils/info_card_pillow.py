"""
utils/info_card_pillow.py — Pillow fallback info card (no Chromium needed)
==========================================================================

Same card, same layout language as the Playwright renderer — rarity badge +
spin badge, art disc, name, type pill, parts strip, HP/ATK/DEF/STA bars with
TOTAL, auto-height abilities panel (1/2/3), special move panel — but drawn
entirely with Pillow.

Why this exists
---------------
Pterodactyl-style panels frequently ship containers where Chromium can't run
(missing libnss3/libatk system libs, tight RAM, no apt access). The battle
card renderer (also Pillow) has always worked on the panel, so this fallback
guarantees ``;info`` deploys a real card everywhere, with the HTML renderer
kicking back in automatically the moment Chromium works.

Public API
----------
    render_info_card_pillow(blade, parts=None) -> io.BytesIO | None
"""
from __future__ import annotations

import io
import logging
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from utils.hp_system import blade_hp_stat, hp_display_pct

log = logging.getLogger(__name__)

W = 720
PAD = 30                     # inner padding of the card frame
STAT_MAX = 200

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SYS_FONTS = [
    os.path.join(_PROJECT_ROOT, "assets", "font.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_font_cache: dict[int, ImageFont.FreeTypeFont] = {}


def _font(size: int):
    if size in _font_cache:
        return _font_cache[size]
    for p in _SYS_FONTS:
        try:
            f = ImageFont.truetype(p, size)
            _font_cache[size] = f
            return f
        except Exception:
            continue
    f = ImageFont.load_default()
    _font_cache[size] = f
    return f


def _hex(c: str) -> tuple:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _tw(d, t, f) -> int:
    return int(d.textlength(str(t), font=f))


def _wrap(d, text: str, font, max_w: int) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for w_ in words:
        t = f"{cur} {w_}".strip()
        if _tw(d, t, font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    return lines or [""]


def _rr(d, box, r, **kw):
    d.rounded_rectangle(box, radius=r, **kw)


def render_info_card_pillow(blade: dict, parts: dict | None = None) -> io.BytesIO | None:
    """Draw the full info card with Pillow. Returns PNG buffer or None."""
    try:
        return _render(blade or {}, parts or {})
    except Exception as exc:            # noqa: BLE001 — never break a command
        log.warning("pillow info card failed for %r: %s",
                    (blade or {}).get("name"), exc)
        return None


def _render(blade: dict, parts: dict) -> io.BytesIO:
    # Import shared vocabulary from the HTML renderer so the two cards can
    # never drift apart on chips, themes, ability collection or stat rows.
    from utils.info_card import (_RARITY_THEME, _DEFAULT_THEME, _TYPE_LABEL,
                                 _SPIN_ICON, _collect_abilities, _chip_for,
                                 _stat_rows, _stat_total, _parts_slots)
    from utils.image_generator import _blade_art

    rarity = blade.get("rarity", "Common")
    th     = _RARITY_THEME.get(rarity, _DEFAULT_THEME)
    accent = _hex(th["accent"]); glow = _hex(th["glow"]); tint = _hex(th["tint"])
    text_c = (244, 241, 234); muted = (185, 179, 166); desc_c = (221, 216, 205)

    abils = _collect_abilities(blade)
    n_ab  = len(abils)
    # same auto-adjust steps as the HTML card
    ab_name_f = _font({0: 21, 1: 22, 2: 21, 3: 19}[n_ab])
    ab_desc_f = _font({0: 15, 1: 16, 2: 15, 3: 14}[n_ab])
    ab_gap    = {0: 0, 1: 0, 2: 16, 3: 12}[n_ab]

    meas = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    # ── Pre-measure the abilities column so both boxes stretch equally ──────
    ab_col_w = W - 2 * PAD - int((W - 2 * PAD) * 0.40) - 14 - 30   # inner width
    ab_blocks = []
    ab_h = 0
    for i, ab in enumerate(abils):
        lines = _wrap(meas, ab.get("description", ""), ab_desc_f, ab_col_w)
        blk_h = (ab_name_f.size + 6) + 24 + len(lines) * (ab_desc_f.size + 6)
        ab_blocks.append((ab, lines, blk_h))
        ab_h += blk_h + (ab_gap * 2 + 1 if i else 0)
    if not abils:
        ab_h = 24
    ab_box_h = 52 + ab_h + 18                      # header + content + pad

    stats_rows = _stat_rows(blade)
    stats_box_h = 52 + len(stats_rows) * 58 + 46 + 14   # header + bars + TOTAL
    mid_h = max(ab_box_h, stats_box_h)

    # ── Special move measure ────────────────────────────────────────────────
    sm = blade.get("special_move") or {}
    sm_h = 0
    sm_lines: list[str] = []
    if sm.get("name"):
        sm_lines = _wrap(meas, sm.get("description", ""), _font(16), W - 2 * PAD - 60)
        sm_h = 16 + 26 + 10 + 30 + len(sm_lines) * 22 + (26 if sm.get("damage_per_hit") else 6) + 16

    booster_h = 40 if blade.get("booster_exclusive") else 0

    # ── Total canvas height ─────────────────────────────────────────────────
    ART_H = 330
    y_badges  = PAD + 14
    y_art     = y_badges + 62
    y_name    = y_art + ART_H + 8
    y_pill    = y_name + 62
    y_parts_h = y_pill + 46 + booster_h + 26        # section header baseline
    y_parts   = y_parts_h + 34
    y_mid     = y_parts + 92 + 22
    y_sm      = y_mid + mid_h + 16
    H = y_sm + sm_h + PAD + 8

    # ── Canvas + card chrome ────────────────────────────────────────────────
    img = Image.new("RGBA", (W, H), (7, 7, 10, 255))
    d = ImageDraw.Draw(img)

    # background: tint gradient + top glow
    for y in range(H):
        t = y / H
        col = tuple(int(tint[i] * (1 - t) + 6 * t) for i in range(3))
        d.line([(0, y), (W, y)], fill=col)
    glow_l = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_l)
    gd.ellipse((-W * 0.2, -H * 0.25, W * 1.2, H * 0.35), fill=glow + (70,))
    img = Image.alpha_composite(img, glow_l.filter(ImageFilter.GaussianBlur(60)))
    d = ImageDraw.Draw(img)
    _rr(d, (6, 6, W - 7, H - 7), 24, outline=accent, width=3)

    # ── Top badges ──────────────────────────────────────────────────────────
    bf = _font(20)
    def badge(x, txt, dot=False, icon="", right=False):
        w_ = _tw(d, txt, bf) + (34 if dot else 0) + (30 if icon else 0) + 34
        x0 = x - w_ if right else x
        _rr(d, (x0, y_badges, x0 + w_, y_badges + 44), 12,
            fill=(0, 0, 0, 150), outline=accent, width=2)
        cx = x0 + 17
        if dot:
            d.ellipse((cx, y_badges + 15, cx + 14, y_badges + 29), fill=accent)
            cx += 26
        if icon:
            d.text((cx, y_badges + 9), icon, font=_font(23), fill=text_c)
            cx += 30
        d.text((cx, y_badges + 11), txt, font=bf,
               fill=accent if dot else text_c)
    badge(PAD, rarity.upper(), dot=True)
    spin = blade.get("spin_direction", "Right")
    badge(W - PAD, f"{spin.upper()} SPIN", icon=_SPIN_ICON.get(spin, "↻"), right=True)

    # ── Art disc + rings ────────────────────────────────────────────────────
    cx, cy = W // 2, y_art + ART_H // 2
    ring = Image.new("RGBA", (W, ART_H + 80), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    for r_, st, en in ((178, 300, 60), (208, 120, 235)):
        rd.arc((W // 2 - r_, (ART_H + 80) // 2 - r_, W // 2 + r_, (ART_H + 80) // 2 + r_),
               start=st, end=en, fill=accent + (190,), width=3)
    img.alpha_composite(ring, (0, y_art - 40))
    d = ImageDraw.Draw(img)

    R = 128
    d.ellipse((cx - R - 5, cy - R - 5, cx + R + 5, cy + R + 5),
              outline=(108, 108, 126), width=6)
    disc = Image.new("RGBA", (2 * R, 2 * R), (26, 26, 36, 255))
    dd = ImageDraw.Draw(disc)
    dd.ellipse((-R, -R, int(R * 1.4), int(R * 1.4)), fill=glow + (255,))
    disc = disc.filter(ImageFilter.GaussianBlur(30))
    art = _blade_art(blade.get("name", ""), int(R * 1.84))
    if art:
        ax = R - art.width // 2
        ay = R - art.height // 2
        disc.alpha_composite(art, (ax, ay))
    mask = Image.new("L", (2 * R, 2 * R), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 2 * R, 2 * R), fill=255)
    img.paste(disc, (cx - R, cy - R), mask)
    d = ImageDraw.Draw(img)

    # ── Name + type pill ────────────────────────────────────────────────────
    name = str(blade.get("name", "Unknown"))
    nf = _font(48 if len(name) <= 18 else 38 if len(name) <= 26 else 31)
    nw = _tw(d, name, nf)
    d.text(((W - nw) // 2 + 2, y_name + 2), name, font=nf, fill=(0, 0, 0, 200))
    d.text(((W - nw) // 2, y_name), name, font=nf, fill=text_c)

    pill_t = _TYPE_LABEL.get(blade.get("type"), f"{str(blade.get('type', '')).upper()} TYPE")
    pf = _font(19)
    pw = _tw(d, pill_t, pf) + 44
    _rr(d, ((W - pw) // 2, y_pill, (W + pw) // 2, y_pill + 38), 10, fill=accent)
    d.text(((W - pw) // 2 + 22, y_pill + 8), pill_t, font=pf, fill=(23, 19, 10))

    if booster_h:
        bt = "📦 BOOSTER EXCLUSIVE"
        btf = _font(14)
        bw = _tw(d, bt, btf) + 32
        yb = y_pill + 48
        _rr(d, ((W - bw) // 2, yb, (W + bw) // 2, yb + 30), 8, outline=accent, width=1)
        d.text(((W - bw) // 2 + 16, yb + 6), bt, font=btf, fill=accent)

    def sec_head(x, y, txt, mark=True):
        hf = _font(19)
        if mark:
            d.rectangle((x, y + 2, x + 5, y + 21), fill=accent)
            x += 14
        d.text((x, y), txt, font=hf, fill=accent)

    # ── Parts strip ─────────────────────────────────────────────────────────
    sec_head(PAD, y_parts_h, "EQUIPPED PARTS")
    slot_w = (W - 2 * PAD - 2 * 13) // 3
    sf, vf = _font(14), _font(18)
    for i, p in enumerate(_parts_slots(blade, parts)):
        x0 = PAD + i * (slot_w + 13)
        _rr(d, (x0, y_parts, x0 + slot_w, y_parts + 82), 12,
            fill=(0, 0, 0, 110), outline=(255, 255, 255, 36), width=2)
        st = p["slot"]
        d.text((x0 + (slot_w - _tw(d, st, sf)) // 2, y_parts + 14), st, font=sf, fill=muted)
        val = str(p["value"])
        while _tw(d, val, vf) > slot_w - 16 and len(val) > 3:
            val = val[:-2].rstrip() + "…"
        d.text((x0 + (slot_w - _tw(d, val, vf)) // 2, y_parts + 44), val, font=vf, fill=text_c)

    # ── Middle boxes ────────────────────────────────────────────────────────
    stats_w = int((W - 2 * PAD) * 0.40)
    ab_x = PAD + stats_w + 14
    _rr(d, (PAD, y_mid, PAD + stats_w, y_mid + mid_h), 16,
        fill=(0, 0, 0, 110), outline=(255, 255, 255, 34), width=2)
    _rr(d, (ab_x, y_mid, W - PAD, y_mid + mid_h), 16,
        fill=(0, 0, 0, 110), outline=accent, width=2)

    # stats
    sec_head(PAD + 15, y_mid + 15, "STATS")
    lf, valf = _font(20), _font(25)
    bar_w = stats_w - 30
    # distribute rows across the stretch space, TOTAL pinned to the bottom
    total_y = y_mid + mid_h - 46
    avail = (total_y - 14) - (y_mid + 52)
    step = avail // max(1, len(stats_rows))
    y = y_mid + 52
    for r in stats_rows:
        col = (255, 155, 155) if r.get("hp") else text_c
        fill_col = ((248, 113, 113) if r.get("hp") else accent)
        d.text((PAD + 15, y), r["label"], font=lf, fill=col)
        vtxt = str(r["value"])
        d.text((PAD + stats_w - 15 - _tw(d, vtxt, valf), y - 3), vtxt, font=valf, fill=col)
        by = y + 33
        _rr(d, (PAD + 15, by, PAD + 15 + bar_w, by + 7), 4, fill=(255, 255, 255, 26))
        fw = int(bar_w * r["pct"] / 100)
        if fw > 4:
            _rr(d, (PAD + 15, by, PAD + 15 + fw, by + 7), 4, fill=fill_col)
        y += step
    d.line((PAD + 15, total_y - 6, PAD + stats_w - 15, total_y - 6),
           fill=(255, 255, 255, 42), width=2)
    d.text((PAD + 15, total_y + 6), "TOTAL", font=_font(16), fill=accent)
    tot = str(_stat_total(blade))
    d.text((PAD + stats_w - 15 - _tw(d, tot, _font(27)), total_y - 2),
           tot, font=_font(27), fill=accent)

    # abilities
    sec_head(ab_x + 15, y_mid + 15, f"◆ ABILITIES ({n_ab})", mark=False)
    y = y_mid + 52
    chip_f = _font(13)
    for i, (ab, lines, _h) in enumerate(ab_blocks):
        if i:
            d.line((ab_x + 15, y - ab_gap - 1, W - PAD - 15, y - ab_gap - 1),
                   fill=(255, 255, 255, 36), width=1)
        d.text((ab_x + 15, y), str(ab.get("name", "Ability")), font=ab_name_f, fill=text_c)
        y += ab_name_f.size + 8
        chip = _chip_for(ab)
        cw = _tw(d, chip, chip_f) + 18
        _rr(d, (ab_x + 15, y, ab_x + 15 + cw, y + 22), 6, fill=accent)
        d.text((ab_x + 24, y + 4), chip, font=chip_f, fill=(23, 19, 10))
        y += 30
        for ln in lines:
            d.text((ab_x + 15, y), ln, font=ab_desc_f, fill=desc_c)
            y += ab_desc_f.size + 6
        y += ab_gap * 2
    if not abils:
        d.text((ab_x + 15, y_mid + 52), "No abilities recorded.", font=_font(15), fill=muted)

    # ── Special move ────────────────────────────────────────────────────────
    if sm_h:
        _rr(d, (PAD, y_sm, W - PAD, y_sm + sm_h), 16,
            fill=(0, 0, 0, 115), outline=accent, width=3)
        sec_head(PAD + 16, y_sm + 16, "⚡ SPECIAL MOVE")
        yy = y_sm + 16 + 30
        d.text((PAD + 16, yy), str(sm.get("name", "???")), font=_font(27), fill=text_c)
        yy += 38
        for ln in sm_lines:
            d.text((PAD + 16, yy), ln, font=_font(16), fill=desc_c)
            yy += 22
        if sm.get("damage_per_hit"):
            hits = sm.get("hits", 1)
            dph = sm.get("damage_per_hit", 0)
            total = sm.get("total_damage", hits * dph)
            d.text((PAD + 16, yy + 4),
                   f"{hits} hit{'s' if hits != 1 else ''} × {dph} dmg  ·  {total} total",
                   font=_font(14), fill=accent)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf
