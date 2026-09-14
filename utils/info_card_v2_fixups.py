"""Small presentation fixups for the V2 info-card renderer.

Kept separate from the main renderer so the original implementation stays easy
to compare/revert while we iterate on the new design.
"""
from __future__ import annotations

import copy
import re
from PIL import Image, ImageDraw

_APPLIED = False


def _flat_edge_background_to_alpha(img: Image.Image | None) -> Image.Image | None:
    """Remove only flat neutral backgrounds connected to the image edges.

    Some Bey art arrives as an opaque WebP on black with a neutral grey strip
    along the bottom. A normal alpha crop cannot remove that, so V2 used to
    show a visible square inside the circular hero medallion. Flood-filling
    from many edge seeds removes those *connected* flat fields while preserving
    dark/grey details enclosed inside the Bey itself.
    """
    if img is None:
        return None
    out = img.convert("RGBA")
    w, h = out.size
    if w < 2 or h < 2:
        return out

    # Corners alone are not enough for source images that contain a second
    # flat band (for example a grey footer) touching only the left/right edge.
    points: list[tuple[int, int]] = []
    for i in range(0, 21):
        x = min(w - 1, round((w - 1) * i / 20))
        y = min(h - 1, round((h - 1) * i / 20))
        points.extend(((x, 0), (x, h - 1), (0, y), (w - 1, y)))

    seen: set[tuple[int, int]] = set()
    for xy in points:
        if xy in seen:
            continue
        seen.add(xy)
        r, g, b, a = out.getpixel(xy)
        spread = max(r, g, b) - min(r, g, b)
        mean = (r + g + b) / 3.0
        # Only touch edge-connected colours that look like a background:
        # near-black, or a fairly neutral grey. Coloured backgrounds stay.
        looks_flat_bg = max(r, g, b) <= 55 or (spread <= 18 and 55 <= mean <= 235)
        if a and looks_flat_bg:
            try:
                ImageDraw.floodfill(out, xy, (0, 0, 0, 0), thresh=30)
            except Exception:
                pass
    return out


def _clean_special_name(name: object) -> str:
    text = str(name or "")
    # Pillow's bundled/default fonts often lack emoji glyphs, which rendered as
    # an empty square before a move name. The information remains in the text;
    # only unsupported leading symbol glyphs are removed from the card title.
    return re.sub(r"^[^A-Za-z0-9]+", "", text).strip() or text


def apply(v2) -> None:
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    original_load = v2._load_source
    original_pillow = v2.render_info_card_pillow

    def clean_load(path_or_url: str):
        return _flat_edge_background_to_alpha(original_load(path_or_url))

    def clean_pillow(blade: dict, parts=None):
        doc = copy.deepcopy(blade or {})
        sm = doc.get("special_move")
        if isinstance(sm, dict) and sm.get("name"):
            sm["name"] = _clean_special_name(sm.get("name"))
        return original_pillow(doc, parts)

    v2._load_source = clean_load
    v2.render_info_card_pillow = clean_pillow
    try:
        v2._art_cached.cache_clear()
        v2._CARD_CACHE.clear()
    except Exception:
        pass
