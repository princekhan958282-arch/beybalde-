"""
boss_card_pillow.py — Pillow fallback for the boss cards.

Why this exists
---------------
boss_card.py renders through Chromium. utils/info_card.py has always had a
Pillow fallback for exactly the case where Chromium isn't available — a host
without Playwright, a failed browser launch, a render timeout — but boss_card
had none, so on those hosts EVERY boss fight silently dropped to a plain embed
and the card nobody could see was simply never drawn.

This mirrors utils/info_card_pillow.py: same font resolution, same rounded-rect
helpers, same "return None and let the caller fall back" contract. Two entry
points, matching the two Chromium templates:

    render_battle_pillow(state)   the in-fight card, redrawn every turn
    render_lobby_pillow(state)    the pre-fight lobby / roster card

Deliberately no network access. The Chromium templates embed boss art as a
data: URI via info_card._art_src; here art is drawn from the local assets
directory when present and falls back to an initial disc, because a fallback
renderer that blocks on a CDN is worse than one that draws a circle.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Optional

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:                                    # noqa: BLE001
    Image = ImageDraw = ImageFont = None             # type: ignore

log = logging.getLogger("beyblade_bot.boss_card")

W = 640                       # matches boss_card.CARD_WIDTH
PAD = 20

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_SYS_FONTS = [
    os.path.join(_PROJECT_ROOT, "assets", "font.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_font_cache: dict[int, "ImageFont.FreeTypeFont"] = {}

BG_DEEP = (5, 7, 12)
TEXT = (232, 236, 244)
SUBTEXT = (141, 153, 173)
DIM = (93, 106, 122)
TRACK = (27, 33, 48)
HP_GREEN = (74, 222, 128)


# ── small helpers, same shapes as utils/info_card_pillow ─────────────────────

def _font(size: int):
    if size in _font_cache:
        return _font_cache[size]
    for p in _SYS_FONTS:
        try:
            f = ImageFont.truetype(p, size)
            _font_cache[size] = f
            return f
        except Exception:                            # noqa: BLE001
            continue
    f = ImageFont.load_default()
    _font_cache[size] = f
    return f


def _hex(c: str, default=(199, 125, 255)) -> tuple:
    try:
        c = str(c).lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:                                # noqa: BLE001
        return default


def _tw(d, t, f) -> int:
    return int(d.textlength(str(t), font=f))


def _rr(d, box, r, **kw):
    d.rounded_rectangle(box, radius=r, **kw)


def _fit(d, text: str, max_w: int, start: int, floor: int = 13):
    """Largest font size at which `text` fits `max_w`."""
    size = start
    while size > floor and _tw(d, text, _font(size)) > max_w:
        size -= 1
    return _font(size)


def _bar(d, x, y, w, h, cur, mx, colour):
    _rr(d, (x, y, x + w, y + h), h // 2, fill=TRACK, outline=(38, 45, 61), width=1)
    frac = max(0.0, min(1.0, (cur / mx) if mx else 0.0))
    if frac > 0:
        fw = max(h, int(w * frac))
        _rr(d, (x, y, x + fw, y + h), h // 2, fill=colour)


def _art_disc(img, d, cx, cy, r, name: str, accent):
    """Local boss art when we have it, an initial disc when we don't."""
    path = None
    try:
        from utils.info_card import _local_art_path
        path = _local_art_path(name or "")
    except Exception:                                # noqa: BLE001
        path = None

    d.ellipse((cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2),
              outline=accent, width=2)
    if path:
        try:
            art = Image.open(path).convert("RGBA")
            box = r * 2
            art.thumbnail((box, box), Image.LANCZOS)
            mask = Image.new("L", (box, box), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, box - 1, box - 1), fill=255)
            fitted = Image.new("RGBA", (box, box), (0, 0, 0, 0))
            fitted.paste(art, ((box - art.width) // 2, (box - art.height) // 2), art)
            img.paste(fitted, (cx - r, cy - r), mask)
            return
        except Exception as exc:                     # noqa: BLE001
            log.debug("[boss_card_pillow] art failed: %s", exc)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(17, 21, 31))
    ch = (str(name).strip() or "?")[0].upper()
    f = _font(int(r * 1.1))
    d.text((cx - _tw(d, ch, f) // 2, cy - int(r * 0.72)), ch, font=f, fill=accent)


def _canvas(height: int, tint, accent):
    img = Image.new("RGBA", (W, height), BG_DEEP + (255,))
    d = ImageDraw.Draw(img)
    # Flat tint wash instead of a gradient — cheap, and this is the fallback.
    _rr(d, (0, 0, W - 1, height - 1), 18, fill=tint + (255,),
        outline=accent + (90,), width=1)
    return img, d


def _out(img) -> io.BytesIO:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    buf.name = "boss.png"
    return buf


def _available() -> bool:
    return Image is not None


# ── Battle card ──────────────────────────────────────────────────────────────

def render_battle_pillow(state: dict) -> Optional[io.BytesIO]:
    """The in-fight card. Same `state` dict BossFight.card_state() produces."""
    if not _available():
        return None
    try:
        return _battle(state)
    except Exception as exc:                         # noqa: BLE001
        log.debug("[boss_card_pillow] battle render failed: %s", exc)
        return None


def _battle(state: dict) -> io.BytesIO:
    accent = _hex(state.get("accent", "#c77dff"))
    tint = _hex(state.get("tint", "#140a1e"), (20, 10, 30))

    party = state.get("party") or []
    log_lines = [l for l in (state.get("log") or [])][-3:]
    height = (200 + max(1, len(party)) * 46
              + (34 + len(log_lines) * 18 if log_lines else 0)
              + (40 if state.get("verdict") else 0) + 44)
    img, d = _canvas(height, tint, accent)

    # header: art + name + boss HP
    _art_disc(img, d, PAD + 55, PAD + 55, 52, state.get("boss_name", ""), accent)
    tx = PAD + 125
    nf = _fit(d, str(state.get("boss_name", "")), W - tx - PAD, 26, 15)
    d.text((tx, PAD + 8), str(state.get("boss_name", "")), font=nf, fill=(255, 255, 255))

    tag = str(state.get("tier", "boss")).upper()
    tf = _font(12)
    tw_ = _tw(d, tag, tf) + 18
    _rr(d, (tx, PAD + 42, tx + tw_, PAD + 64), 11, outline=accent, width=1)
    d.text((tx + 9, PAD + 46), tag, font=tf, fill=accent)

    bw = W - tx - PAD
    _bar(d, tx, PAD + 74, bw, 12, state.get("boss_hp", 0), state.get("boss_max", 1), accent)
    sub = (f"{state.get('boss_hp', 0):.0f} / {state.get('boss_max', 0):.0f}   ·   "
           f"gauge {state.get('boss_gauge', 0):.0f}/{state.get('gauge_max', 150):.0f}"
           f"   ·   sp {state.get('boss_sp', 0):.1f}")
    d.text((tx, PAD + 92), sub, font=_font(12), fill=SUBTEXT)

    y = PAD + 128
    d.text((PAD, y), f"PARTY ({state.get('alive', 0)}/{state.get('total', 0)})",
           font=_font(12), fill=accent)
    y += 22

    for m in party:
        alive = m.get("alive", True)
        col = TEXT if alive else DIM
        mark = "▶" if m.get("active") else ("✖" if not alive else " ")
        nf2 = _fit(d, f"{mark} {m.get('name', '?')}", 168, 13, 10)
        d.text((PAD, y + 2), f"{mark} {m.get('name', '?')}", font=nf2,
               fill=accent if m.get("active") else col)
        bx = PAD + 178
        _bar(d, bx, y + 4, W - bx - PAD, 10,
             m.get("hp", 0), m.get("max_hp", 1), HP_GREEN if alive else DIM)
        d.text((bx, y + 19),
               f"{m.get('hp', 0):.0f} hp   ·   gauge {m.get('gauge', 0):.0f}"
               f"   ·   sp {m.get('sp', 0):.1f}",
               font=_font(11), fill=SUBTEXT if alive else DIM)
        y += 46

    if log_lines:
        d.text((PAD, y), "LAST EXCHANGES", font=_font(12), fill=accent)
        y += 20
        for line in log_lines:
            lf = _fit(d, str(line), W - 2 * PAD, 12, 9)
            d.text((PAD, y), str(line), font=lf, fill=SUBTEXT)
            y += 18

    if state.get("verdict"):
        vf = _font(20)
        d.text(((W - _tw(d, state["verdict"], vf)) // 2, y + 6),
               str(state["verdict"]), font=vf, fill=accent)
        y += 40

    foot = f"TURN {state.get('turn', 0)}   ·   {state.get('difficulty', '')}"
    d.text((PAD, height - 26), foot, font=_font(11), fill=DIM)
    return _out(img)


# ── Lobby / roster card ──────────────────────────────────────────────────────

def render_lobby_pillow(state: dict) -> Optional[io.BytesIO]:
    """The pre-fight card. Same `state` boss_card.build_lobby_html() takes."""
    if not _available():
        return None
    try:
        return _lobby(state)
    except Exception as exc:                         # noqa: BLE001
        log.debug("[boss_card_pillow] lobby render failed: %s", exc)
        return None


def _lobby(state: dict) -> io.BytesIO:
    accent = _hex(state.get("accent", "#c77dff"))
    tint = _hex(state.get("tint", "#140a1e"), (20, 10, 30))

    party = state.get("party") or []
    stats = state.get("stats") or []

    # Height is derived from the SAME arithmetic the draw pass uses below.
    # Computing it independently is how the party list ended up running off the
    # bottom of the card — keep these two in step.
    y_body = PAD + 226 + 26                      # art + name + tag + blurb
    h_stats = len(stats) * 22
    h_party = (6 + 20 + len(party) * 24) if party else 0
    height = y_body + h_stats + h_party + 40     # + footer band
    img, d = _canvas(height, tint, accent)

    _art_disc(img, d, W // 2, PAD + 68, 62, state.get("boss_name", ""), accent)

    name = str(state.get("boss_name", ""))
    nf = _fit(d, name, W - 2 * PAD, 34, 18)
    d.text(((W - _tw(d, name, nf)) // 2, PAD + 140), name, font=nf, fill=(255, 255, 255))

    tag = str(state.get("tier", "boss")).upper()
    tf = _font(13)
    tw_ = _tw(d, tag, tf) + 22
    _rr(d, ((W - tw_) // 2, PAD + 182, (W + tw_) // 2, PAD + 208), 13,
        outline=accent, width=2)
    d.text(((W - _tw(d, tag, tf)) // 2, PAD + 188), tag, font=tf, fill=accent)

    y = PAD + 226
    if state.get("blurb"):
        bf = _fit(d, state["blurb"], W - 2 * PAD, 13, 10)
        d.text(((W - _tw(d, state["blurb"], bf)) // 2, y), str(state["blurb"]),
               font=bf, fill=SUBTEXT)
    y += 26

    for label, value in stats:
        d.text((PAD, y), str(label), font=_font(12), fill=DIM)
        vf = _font(13)
        d.text((W - PAD - _tw(d, value, vf), y - 1), str(value), font=vf, fill=TEXT)
        y += 22

    if party:
        y += 6
        d.text((PAD, y), f"PARTY ({len(party)}/{state.get('max_party', 4)})",
               font=_font(12), fill=accent)
        y += 20
        for p in party:
            d.text((PAD + 6, y), f"• {p}", font=_font(12), fill=TEXT)
            y += 24

    if state.get("footer"):
        d.text((PAD, height - 26), str(state["footer"]), font=_font(11), fill=DIM)
    return _out(img)
