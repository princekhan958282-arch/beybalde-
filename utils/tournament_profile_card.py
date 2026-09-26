"""Phone-first tournament career profile card.

Kept separate from tournament_card.py so career UI changes cannot break the
live bracket renderer. The function never raises; None lets Discord callers
fall back to an embed.
"""
from __future__ import annotations

import io
from PIL import Image, ImageDraw
from utils.image_generator import _blade_art, _fit_text, _font, _sanitize, _text_w

W, H = 1000, 1080
BG = (18, 18, 28, 255)
PANEL = (34, 32, 50, 255)
TEXT = (240, 240, 245, 255)
SUB = (158, 158, 176, 255)
GOLD = (250, 204, 21, 255)
ACCENT = (99, 102, 241, 255)
GREEN = (52, 211, 153, 255)


def _center(draw, text, font, cx, y, fill):
    draw.text((cx - _text_w(draw, text, font) // 2, y), text, font=font, fill=fill)


def _box(draw, xy, fill=PANEL, outline=(66, 62, 92, 255), radius=22):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=2)


def render_tournament_profile_card(name: str, stats: dict, *,
                                   best_bey: str = "—", best_bey_wins: int = 0,
                                   avatar: Image.Image | None = None) -> io.BytesIO | None:
    try:
        img = Image.new("RGBA", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.text((42, 34), "TOURNAMENT PROFILE", font=_font(46), fill=GOLD)
        d.text((44, 91), "BEYCORD COMPETITIVE CAREER", font=_font(21), fill=SUB)

        # Identity block
        _box(d, (32, 140, 968, 340))
        if avatar is not None:
            av = avatar.convert("RGBA").resize((150, 150), Image.LANCZOS)
            mask = Image.new("L", (150, 150), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 149, 149), fill=255)
            img.paste(av, (60, 165), av if av.getbbox() else mask)
        else:
            d.ellipse((60, 165, 210, 315), fill=(48, 46, 70), outline=ACCENT, width=4)
            initial = (_sanitize(name) or "?")[0].upper()
            _center(d, initial, _font(68), 135, 199, TEXT)
        safe_name = _sanitize(name) or "Player"
        nf = _fit_text(d, safe_name, 660, 48, floor=26)
        d.text((245, 177), safe_name, font=nf, fill=TEXT)
        rating = int(stats.get("rating", 1000))
        d.text((247, 239), f"Tournament Rating  {rating:,}", font=_font(29), fill=ACCENT)
        d.text((247, 284), f"Best streak  {int(stats.get('best_streak', 0))}", font=_font(23), fill=SUB)

        # Career grid
        labels = [
            ("CHAMPIONSHIPS", stats.get("championships", 0), GOLD),
            ("TOURNAMENTS", stats.get("tournaments", 0), TEXT),
            ("MATCH WINS", stats.get("match_wins", 0), GREEN),
            ("MATCH LOSSES", stats.get("match_losses", 0), TEXT),
            ("FINALS", stats.get("finals", 0), TEXT),
            ("WIN RATE", f"{float(stats.get('win_rate', 0.0)):.1f}%", ACCENT),
        ]
        for i, (label, value, col) in enumerate(labels):
            row, column = divmod(i, 3)
            x0 = 32 + column * 312
            y0 = 370 + row * 145
            _box(d, (x0, y0, x0 + 292, y0 + 125))
            _center(d, label, _font(19), x0 + 146, y0 + 19, SUB)
            _center(d, str(value), _font(42), x0 + 146, y0 + 55, col)

        # Best tournament bey
        _box(d, (32, 680, 968, 880), fill=(38, 35, 49, 255), outline=(120, 100, 40, 255))
        d.text((58, 702), "BEST TOURNAMENT BEY", font=_font(23), fill=GOLD)
        art = _blade_art(best_bey, 150) if best_bey and best_bey != "—" else None
        if art is not None:
            img.alpha_composite(art, (68, 744))
        else:
            d.ellipse((68, 744, 218, 894), fill=(48, 46, 70), outline=GOLD, width=3)
        bf = _fit_text(d, _sanitize(best_bey), 650, 40, floor=22)
        d.text((255, 754), _sanitize(best_bey), font=bf, fill=TEXT)
        d.text((257, 813), f"{best_bey_wins:,} tournament match wins", font=_font(25), fill=SUB)

        recent = list(stats.get("recent") or [])[-6:]
        d.text((42, 925), "RECENT RESULTS", font=_font(22), fill=SUB)
        recent_text = "   ".join("🏆 1st" if p == 1 else f"#{p}" for p in recent) or "No completed tournaments yet"
        rf = _fit_text(d, recent_text, 910, 27, floor=17)
        d.text((42, 966), recent_text, font=rf, fill=TEXT)
        d.text((42, 1030), "Stats update only from completed real tournament battles", font=_font(18), fill=SUB)

        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG", optimize=True)
        buf.seek(0)
        return buf
    except Exception:
        return None
