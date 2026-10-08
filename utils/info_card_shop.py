"""Render dynamic fields on the supplied BEYCBOT two-ability template.

Coordinates use the original 1536x1152 asset. No background is reconstructed.
Returning None delegates to the normal info card.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont, ImageOps
from utils import info_card_legacy as legacy
from utils import bey_levels
from utils.hp_system import blade_hp_stat

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'assets/ui/beycbot_info_frame.png'
FONT = ROOT / 'assets/ui/beycbot_font.ttf'
WHITE = (238, 245, 250)
CYAN = (112, 218, 241)
GOLD = (236, 198, 94)


@lru_cache(maxsize=64)
def _font(size):
    return ImageFont.truetype(str(FONT), size)


def _abilities(blade):
    """Reject malformed entries before the legacy collector can silently skip them."""
    raw = blade.get('abilities')
    if raw is not None and not isinstance(raw, list):
        return []
    entries = list(raw or [])
    entries += [blade[k] for k in ('ability', 'ability_2') if blade.get(k) is not None]
    if any(not isinstance(a, dict) or any(
        not isinstance(a.get(k), str) or not a[k].strip()
        for k in ('name', 'description')) for a in entries):
        return []
    abilities = legacy._collect_abilities(blade)
    return abilities if len(abilities) == 2 else []


def _lines(draw, text, font, width):
    # Character wrapping also bounds unbroken URLs and unusually long names.
    lines, current = [], ''
    for word in str(text).split():
        probe = (current + ' ' + word).strip()
        if draw.textlength(probe, font=font) <= width:
            current = probe
            continue
        if current:
            lines.append(current)
        current = ''
        for char in word:
            if current and draw.textlength(current + char, font=font) > width:
                lines.append(current)
                current = ''
            current += char
    return lines + ([current] if current else [])


def _text(draw, box, text, size=30, minimum=18, max_lines=1, fill=WHITE):
    x, y, right, bottom = box
    for n in range(size, minimum - 1, -1):
        font = _font(n)
        lines = _lines(draw, text, font, right - x)
        step = n + 5
        if len(lines) <= max_lines and len(lines) * step <= bottom - y:
            break
    allowed = min(max_lines, max(1, (bottom - y) // step))
    if len(lines) > allowed:
        lines = lines[:allowed]
        last = lines[-1]
        while last and draw.textlength(last + '…', font=font) > right - x:
            last = last[:-1]
        lines[-1] = last.rstrip() + '…'
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill, anchor='lt')
        y += step


def _art(blade):
    from utils.info_card_v2 import _load_source
    path = legacy._local_art_path(str(blade.get('name', '')))
    source = str(path or blade.get('image_url') or '')
    if source and not source.startswith(('https://', 'http://')):
        p = Path(source)
        source = str(p if p.is_absolute() else ROOT / p)
    art = _load_source(source)
    if art is not None:
        bounds = art.getchannel('A').getbbox()
        if not bounds:
            return None
        art = art.crop(bounds)
        return ImageOps.contain(art, (526, 356), Image.Resampling.LANCZOS)
    return None


def render(blade: dict):
    if not isinstance(blade, dict) or not _abilities(blade):
        return None
    try:
        return _render(blade)
    except Exception:
        log.warning('BEYCBOT template render failed; using default card', exc_info=True)
        return None


def _render(blade):
    abilities = _abilities(blade)
    with Image.open(TEMPLATE) as source:
        img = source.convert('RGBA')
    if img.size != (1536, 1152):
        raise ValueError('Unexpected BEYCBOT template dimensions')
    d = ImageDraw.Draw(img)
    # Erase only printed preview placeholders inside the existing chips.
    # Keep the authored borders, icons, panels and artwork intact.
    for box in ((119, 187, 244, 221), (328, 187, 450, 221),
                (768, 187, 910, 221), (584, 1097, 950, 1131)):
        d.rectangle(box, fill=(5, 16, 26))
    _text(d, (68, 83, 1467, 155), blade.get('name') or 'Unknown Bey', 48, 24, 1)
    _text(d, (120, 190, 244, 219), blade.get('rarity') or 'Unknown', 23, 14, fill=GOLD)
    _text(d, (330, 190, 450, 219), blade.get('type') or 'Unknown', 23, 16, fill=CYAN)
    _text(d, (529, 190, 680, 219), blade.get('spin_direction') or blade.get('spin') or 'Unknown', 23, 16, fill=CYAN)
    level = max(1, min(bey_levels.MAX_LEVEL, int(blade.get('level') or 1)))
    _text(d, (771, 190, 909, 219), f'Lv. {level}', 23, 16, fill=CYAN)
    art = _art(blade)
    if art is not None:
        img.alpha_composite(art, (87 + (526-art.width)//2, 278 + (356-art.height)//2))
    else:
        _text(d, (150, 430, 550, 470), 'Artwork unavailable', 25, fill=CYAN)
    stats = blade.get('stats') or {}
    values = (stats.get('hp', blade_hp_stat(blade)), stats.get('attack', 0),
              stats.get('defense', stats.get('defence', 0)), stats.get('stamina', 0))
    for value, box in zip(values, ((803, 323, 1037, 367), (1202, 323, 1438, 367),
                                  (803, 475, 1037, 517), (1202, 475, 1438, 517))):
        _text(d, box, value, 32, 18, fill=CYAN)
    xp = blade.get('xp', blade.get('experience'))
    if level >= bey_levels.MAX_LEVEL:
        label, ratio = 'MAX LEVEL', 1.0
    elif xp is not None:
        _, into, need = bey_levels.progress(max(0, int(xp)))
        label, ratio = f'{into:,} / {need:,}', into / need if need else 1.0
    else:
        # A species sheet/rolled copy has no player progress to invent.
        label, ratio = 'Progress unavailable', 0.0
    d.rectangle((796, 570, 1428, 620), fill=(12, 21, 30))
    _text(d, (795, 579, 1420, 620), label, 30, 20, fill=CYAN)
    d.rectangle((698, 638, 1344, 647), fill=(23, 49, 61))
    if ratio > 0:
        d.rectangle((698, 638, 698 + round(646 * min(1, ratio)), 647), fill=CYAN)
    for ab, top in zip(abilities, (700, 832)):
        _text(d, (260, top, 1442, top+37), ab['name'], 29, 20)
        _text(d, (260, top+40, 1442, top+99), ab['description'], 23, 18, 2, CYAN)
    special = blade.get('special') or blade.get('special_move') or {}
    if not isinstance(special, dict):
        special = {'name': str(special)}
    _text(d, (442, 973, 1441, 1011), special.get('name') or 'Special unavailable', 29, 20, fill=GOLD)
    _text(d, (442, 1015, 1441, 1072), special.get('description') or '', 23, 18, 2)
    out = io.BytesIO()
    img.convert('RGB').save(out, 'PNG')
    out.name = 'beycbot_info.png'
    out.seek(0)
    return out
