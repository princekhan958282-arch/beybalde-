"""
utils/profile_card.py — Beycord blader profile card (Pillow)
=============================================================
Renders the profile onto ``assets/ui/profile_frame.png``, an authored HUD frame,
instead of drawing its own panels. Everything the card shows now sits in a slot
the artwork already provides:

  header      player avatar disc · name · subtitle · tier chip · level chip
  progress    two bars — XP to next level, rank score to next tier
  stat grid   six cells — wins, losses, win rate, streak, best streak, coins
  collection  one bar — beys owned against the whole database
  loadout     bey art in the big disc · rarity/type/level pills · ATK/DEF/STA/HP

Why a frame instead of drawn panels
-----------------------------------
The old card built its own gradient, glow and rounded rectangles. It was fine
in isolation and cheap to render, but it looked like a placeholder next to the
rest of the game's art. Compositing onto authored artwork means the card gets
texture, bevels and lighting that would be unreasonable to reproduce in Pillow
primitives, and the layout is fixed by the art — so it cannot drift.

The frame is loaded once and cached. If the asset is missing the card returns
None and `;profile` falls back to its embed, exactly as it does for any other
render failure.

The player avatar
-----------------
This used to draw the first letter of the player's name in a disc, and the
docstring said so — "no network call on the render path". That is why nobody's
profile picture ever appeared. The render already runs inside
`asyncio.to_thread` (see `ProfileCog._profile_card_file`), so a blocking fetch
here costs the caller nothing, and avatars are cached by URL — a Discord avatar
URL contains the image hash, so it changes exactly when the avatar does. The
initial disc is still the fallback for a missing, unreachable or malformed
avatar.

Public API
----------
    render_profile_card(player, profile, blade=None, *, total_beys=None,
                        rank_position=None, avatar_url=None) -> io.BytesIO | None

    player  — dict(name, [id])
    profile — the user document (wins, losses, xp, coins, streaks, inventory)
    blade   — the active bey document, or None if nothing is equipped

Never raises: any failure returns None so the caller falls back to the embed.
"""
from __future__ import annotations

import io
import logging
import os
import urllib.request

from PIL import Image, ImageDraw

from utils.image_generator import (
    _blade_art,
    _fit_text,
    _font,
    _sanitize,
    _text_w,
)

log = logging.getLogger("beyblade_bot.profile_card")

CARD_ENABLED = True

# JPEG quality for the rendered card, and the extension the caller must use.
IMAGE_QUALITY = 88
IMAGE_FORMAT = "jpg"

# ── Frame ─────────────────────────────────────────────────────────────────────

_FRAME_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "ui", "profile_frame.png")

W, H = 1193, 967                      # the frame's native size; do not rescale

# ── Slot geometry ─────────────────────────────────────────────────────────────
# Measured off the artwork, not guessed — the structural lines were found by
# scanning the image's brightness profile, then checked by rendering the boxes
# over the frame and looking at it. Every box is (x0, y0, x1, y1).
#
# Nothing here draws a panel: the frame already has one for every slot. The
# renderer only puts text, bar fills and art INSIDE them.

AVATAR_C   = (147, 123)               # player picture disc: centre + radius
AVATAR_R   = 67
NAME_X     = 262
NAME_Y     = 62
SUB_Y      = 136
CHIP_TIER  = (948, 60, 1144, 104)
CHIP_LEVEL = (948, 124, 1144, 168)

BAR_XP     = (72, 306, 618, 342)
BAR_RANK   = (72, 362, 618, 398)

GRID_COLS  = ((44, 228), (250, 434), (457, 641))
GRID_ROWS  = ((457, 566), (592, 704))

BAR_COLL   = (72, 814, 618, 862)

ART_C      = (910, 421)               # bey art disc
ART_R      = 104
PILLS      = ((708, 596, 836, 630), (850, 596, 966, 630), (990, 596, 1120, 630))
LOAD_BARS  = ((708, 660, 1134, 700), (708, 717, 1134, 757),
              (708, 774, 1134, 814), (708, 831, 1134, 871))

# ── Palette ───────────────────────────────────────────────────────────────────
# Tuned for near-black artwork: the old palette was mixed for a blue-grey
# gradient and reads muddy on this frame.

TEXT     = (238, 240, 246)
SUBTEXT  = (150, 154, 170)
DIM      = (104, 108, 124)
GOLD     = (250, 204, 21)
XP_COL   = (96, 165, 250)
WIN_COL  = (52, 211, 153)
LOSS_COL = (239, 68, 68)
COIN_COL = (250, 204, 21)

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

from utils.trainer_levels import MAX_LEVEL, level_from_xp as _lvl_from_xp

from utils.bey_levels import STAT_BAR_MAX as STAT_MAX   # one ceiling, one file

# The frame is one consistent product, so the accent stays fixed rather than
# recolouring per tier. Rank is still readable: the tier chip and the rank bar
# both use the tier's own colour.
THEME_LOCKED  = True
LOCKED_ACCENT = (243, 156, 18)


def _accent_for(tier_col: tuple) -> tuple:
    return LOCKED_ACCENT if THEME_LOCKED else tier_col


# ── Small helpers ─────────────────────────────────────────────────────────────

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
    """(level, xp_into_level, xp_span_of_level).

    This used to re-implement the curve — its own cap, its own arithmetic —
    to avoid importing `utils.database`, which probes MySQL on import just to
    draw a picture. That reason was sound and the duplicate was not: two
    answers to "what level is this player", one of them printed on the card.
    `utils/trainer_levels.py` is the shared curve, with no imports and no side
    effects, so the card can use the real thing.
    """
    xp = max(0, int(xp))
    lvl = _lvl_from_xp(xp)
    if lvl >= MAX_LEVEL:
        return MAX_LEVEL, 0, 0
    cur_floor = 50 * lvl * lvl
    nxt_floor = 50 * (lvl + 1) * (lvl + 1)
    return lvl, xp - cur_floor, nxt_floor - cur_floor


def _centre(draw, txt, font, cx, y, fill):
    draw.text((cx - _text_w(draw, txt, font) // 2, y), txt, font=font, fill=fill)


def _right(draw, txt, font, rx, y, fill):
    draw.text((rx - _text_w(draw, txt, font), y), txt, font=font, fill=fill)


def _short(n: int) -> str:
    """12,400 -> '12.4K'. Keeps six-figure coin piles inside a grid cell."""
    n = int(n)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if abs(n) >= 10_000:
        return f"{n / 1_000:.0f}K"
    return f"{n:,}"


# ── Frame + avatar loading ────────────────────────────────────────────────────

_frame_cache: "Image.Image | None" = None
_cyber_frame_cache: "Image.Image | None" = None


CYBER_FRAME_PATH = os.path.join(os.path.dirname(_FRAME_PATH), "cyber_arena_frame.png")
CYBER_SIZE = (1536, 864)
CYBER_AVATAR_C, CYBER_AVATAR_R = (350, 386), 235
CYBER_ART_C = (805, 548)


def _cyber_frame() -> "Image.Image | None":
    """Load the user's authored frame. Live overlays replace its example text."""
    global _cyber_frame_cache
    if _cyber_frame_cache is None:
        try:
            with Image.open(CYBER_FRAME_PATH) as source:
                _cyber_frame_cache = source.convert("RGBA").resize(CYBER_SIZE, Image.LANCZOS)
        except Exception:
            log.warning("Cyber Arena asset unavailable at %s; using default profile", CYBER_FRAME_PATH)
            return None
    return _cyber_frame_cache.copy()


def _cyber_content(img, player, profile, blade, total_beys, rank_position, avatar_url):
    """Reference-layout composition, isolated from the default card geometry.

    The uploaded artwork contains example identity and values. Repaint their
    exact content regions on every render; none is used as player data. Keep
    its metal avatar ring, panel borders and stat icons intact.
    """
    draw = ImageDraw.Draw(img)
    navy = (3, 7, 17, 255)
    cyan = (36, 182, 226)
    for box in [(719, 40, 1450, 218), (737, 233, 1448, 258),
                (908, 317, 1456, 425), (778, 350, 820, 402),
                (908, 482, 1455, 611), (740, 685, 826, 724),
                (950, 685, 1045, 724), (1162, 685, 1260, 724),
                (1380, 685, 1475, 724), (167, 764, 407, 815),
                (521, 764, 731, 815), (778, 764, 1045, 815),
                (1088, 764, 1332, 815)]:
        draw.rectangle(box, fill=navy)
    # Cover the demonstration footer, then give it a product label.
    draw.rectangle((66, 828, 850, 859), fill=navy)

    def text(value, x, y, width, size=24, colour=TEXT):
        value = _sanitize(str(value))
        f = _fit_text(draw, value, width, size, floor=12)
        # Bound even extreme identifiers/names after reaching the font floor.
        while value and _text_w(draw, value, f) > width:
            value = value[:-2] + "…" if len(value) > 2 else ""
        draw.text((x, y), value, font=f, fill=colour)

    name = _sanitize(str(player.get("name") or "Blader"))
    art = _avatar_image(avatar_url, CYBER_AVATAR_R*2) if avatar_url else None
    if art is not None:
        _disc_art(img, CYBER_AVATAR_C, CYBER_AVATAR_R, art)
    else:
        _initial_disc(img, draw, CYBER_AVATAR_C, CYBER_AVATAR_R, name, cyan)
    xp = max(0, _num(profile.get("xp")))
    score = max(0, _num(profile.get("rank_score")))
    tier, tier_col, _, _ = _tier_for(score)
    level, into, span = _level_from_xp(xp)
    text(name, 724, 41, 705, 68)
    username = player.get("username")
    text("@" + str(username) if username else "BLADER PROFILE", 731, 136, 490, 28, cyan)
    text(f"ID  {player.get('id', '—')}", 1153, 118, 289, 17, SUBTEXT)
    text(f"LV. {level}", 1290, 161, 152, 36, GOLD)
    text(f"XP  {into:,} / {span:,}" if level < MAX_LEVEL else "XP  MAX LEVEL",
         733, 190, 522, 24, cyan)
    _slot_bar(img, draw, (735, 231, 1450, 260),
              into / span if span and level < MAX_LEVEL else 1.0, XP_COL)

    text("R", 785, 356, 34, 30, tier_col)
    text(tier, 915, 322, 520, 32, tier_col)
    if rank_position:
        text(f"#{rank_position}", 1340, 329, 104, 20, SUBTEXT)
    wins, losses = _num(profile.get("wins")), _num(profile.get("losses"))
    played = wins + losses
    rate = f"{wins/played*100:.0f}%" if played > 0 else "0%"
    text(f"W  {wins:,}", 916, 373, 127, 26, WIN_COL)
    text(f"L  {losses:,}", 1094, 373, 120, 26, LOSS_COL)
    text(f"WIN RATE  {rate}", 1270, 373, 178, 23)
    text(f"RANK SCORE  {score:,}", 916, 409, 520, 15, SUBTEXT)

    if blade:
        bey_name = _sanitize(str(blade.get("name") or "Unknown"))
        rarity = str(blade.get("rarity") or "Common")
        art = _blade_art(bey_name, 122)
        if art is not None:
            img.alpha_composite(art.convert("RGBA"),
                                (CYBER_ART_C[0] - art.width // 2,
                                 CYBER_ART_C[1] - art.height // 2))
        else:
            _initial_disc(img, draw, CYBER_ART_C, 49, bey_name, _rar_col(rarity))
        text(bey_name, 915, 484, 538, 34)
        text(f"TYPE  {blade.get('type', '—')}", 916, 536, 300, 22, cyan)
        text(f"BEY LV  {_num(blade.get('level'), 1) or 1}", 1252, 536, 194, 21, GOLD)
        text(f"RARITY  {rarity}", 916, 574, 538, 22, _rar_col(rarity))
        stats = blade.get("stats") if isinstance(blade.get("stats"), dict) else {}
    else:
        text("NO BEY EQUIPPED", 915, 490, 535, 32, SUBTEXT)
        text("Use ;equip <name>", 915, 544, 530, 24, cyan)
        stats = {}
    for x, key in [(750,"hp"), (958,"attack"), (1170,"defense"), (1390,"stamina")]:
        text(_num(stats.get(key)), x, 688, 95, 30, STAT_COL[{"hp":"HP","attack":"ATK","defense":"DEF","stamina":"STA"}[key]])
    inventory = profile.get("inventory")
    owned = len({str(n) for n in inventory}) if isinstance(inventory, list) else 0
    text("BEYCOINS", 174, 762, 228, 18, cyan)
    text(f"{_num(profile.get('coins')):,}", 174, 788, 228, 23, COIN_COL)
    days = _num(player.get("premium_days"))
    if days > 0:
        text("PREMIUM", 526, 762, 202, 18, cyan)
        text(f"{days} DAY{'S' if days != 1 else ''} LEFT", 526, 788, 202, 23, GOLD)
    title = player.get("title")
    if isinstance(title, str) and title.strip():
        text("TITLE", 783, 762, 258, 18, cyan)
        text(title, 783, 788, 258, 23)
    guild = player.get("guild_name")
    if isinstance(guild, str) and guild.strip():
        text("GUILD", 1093, 762, 236, 18, cyan)
        text(guild, 1093, 788, 235, 23)
    collection = f"{owned}/{total_beys}" if total_beys else str(owned)
    text(f"STREAK {_num(profile.get('win_streak'))}   •   BEST {_num(profile.get('best_streak'))}   •   COLLECTION {collection}",
         77, 723, 565, 17, SUBTEXT)
    text("BEYcord  /  CYBER ARENA", 70, 833, 660, 16, SUBTEXT)


def _frame(theme: str = "default") -> "Image.Image | None":
    """The HUD artwork, loaded once. None when the asset is missing."""
    global _frame_cache
    if str(theme).lower() == "cyber_arena":
        cyber = _cyber_frame()
        if cyber is not None:
            return cyber
    if _frame_cache is None:
        try:
            _frame_cache = Image.open(_FRAME_PATH).convert("RGBA")
        except Exception as exc:                         # noqa: BLE001
            log.warning("profile frame missing at %s: %s", _FRAME_PATH, exc)
            return None
    return _frame_cache.copy()


# Avatars are cached by URL. A Discord avatar URL embeds the image hash, so the
# URL changes exactly when the picture does — which makes the URL a correct
# cache key with no staleness window. Bounded so a busy server cannot grow it
# without limit.
_AVATAR_CACHE: dict[str, "Image.Image"] = {}
_AVATAR_CACHE_MAX = 64
_AVATAR_TIMEOUT = 6


def clear_cache() -> None:
    """Drop rebuildable profile-card artwork held in memory."""
    global _frame_cache, _cyber_frame_cache
    _frame_cache = None
    _cyber_frame_cache = None
    _AVATAR_CACHE.clear()


def _avatar_image(url: str, size: int) -> "Image.Image | None":
    """Fetch a player's avatar as a circular RGBA disc. None on any failure.

    Blocking on purpose: the whole render runs in `asyncio.to_thread`, so this
    never touches the event loop. Failing quietly is the point — a slow CDN or
    a deleted avatar must cost the player their picture, not their card.
    """
    if not url:
        return None
    key = f"{url}|{size}"
    hit = _AVATAR_CACHE.get(key)
    if hit is not None:
        return hit.copy()

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Beycord/1.0"})
        with urllib.request.urlopen(req, timeout=_AVATAR_TIMEOUT) as resp:
            raw = resp.read(4 * 1024 * 1024)             # a cap, not a promise
        src = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception as exc:                             # noqa: BLE001
        log.debug("avatar fetch failed (%s): %s", url[:60], exc)
        return None

    # Square-crop from the centre, then mask to a circle. Discord avatars are
    # already square, but an animated or oddly-sized one must not stretch.
    w, h = src.size
    side = min(w, h)
    src = src.crop(((w - side) // 2, (h - side) // 2,
                    (w - side) // 2 + side, (h - side) // 2 + side))
    src = src.resize((size, size), Image.LANCZOS)

    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    src.putalpha(mask.resize((size, size), Image.LANCZOS))

    if len(_AVATAR_CACHE) >= _AVATAR_CACHE_MAX:
        _AVATAR_CACHE.clear()
    _AVATAR_CACHE[key] = src
    return src.copy()


# ── Drawing primitives ────────────────────────────────────────────────────────

def _overlay(img, box, fn):
    """Draw onto a transparent layer and composite, so alpha fills blend with
    the artwork instead of punching a hole in it.

    `box` is the region being drawn, and `fn` receives a draw surface whose
    origin is that box's top-left — so coordinates inside are LOCAL.

    It used to allocate and composite a full-canvas layer per call. At nine
    calls a render that was nine 1.15-megapixel alpha composites to paint a few
    small bars, and it dominated the render. Compositing only the affected
    region is the same picture for a fraction of the pixels.
    """
    x0, y0, x1, y1 = box
    w, h = max(1, int(x1 - x0)), max(1, int(y1 - y0))
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    fn(ImageDraw.Draw(layer))
    img.alpha_composite(layer, (int(x0), int(y0)))


def _shadowed(draw, xy, txt, font, fill):
    """Text with a dark halo.

    A bar label crosses the boundary between the coloured fill and the empty
    track, so no single colour reads well along its whole length. The halo is
    what lets one label sit over both — centring it and hoping was the version
    where "RANK 2,450 · 250 TO NEXT TIER" had its middle swallowed by the fill
    edge.

    Pillow's own `stroke_width` does this in ONE pass. Hand-rolling it as six
    offset draws plus the real one meant seven text rasterisations per string,
    and text was a fifth of the whole render. The native stroke also produces a
    cleaner outline than four-way offsets, which leave diagonal gaps.
    """
    draw.text(xy, txt, font=font, fill=fill,
              stroke_width=2, stroke_fill=(0, 0, 0, 210))


def _slot_bar(img, draw, box, pct, colour, label=None, value=None,
              label_col=None):
    """Fill part of a slot the frame already drew.

    Only the FILL is drawn — the track is the artwork's own recess, which is
    why this looks like part of the frame rather than a rectangle sitting on
    top of it.

    `label` is left-aligned and `value` right-aligned inside the slot, rather
    than one centred string: a centred label lands exactly where the fill edge
    moves as the bar grows, so it is illegible at whichever percentage happens
    to put the boundary under the text.
    """
    x0, y0, x1, y1 = box
    pct = max(0.0, min(1.0, pct))
    h = y1 - y0
    inset = 3
    if pct > 0:
        w = max(h - inset * 2, int((x1 - x0 - inset * 2) * pct))
        _overlay(img, (x0 + inset, y0 + inset, x0 + inset + w, y1 - inset),
                 lambda d: d.rounded_rectangle(
                     (0, 0, w - 1, h - inset * 2 - 1),
                     radius=(h - inset * 2) // 2, fill=colour + (225,)))

    f = _font(max(15, h - 16))
    ty = y0 + (h - f.size) // 2 - 2
    if label:
        _shadowed(draw, (x0 + 18, ty), label, f, label_col or TEXT)
    if value:
        _shadowed(draw, (x1 - 18 - _text_w(draw, value, f), ty), value, f, TEXT)


def _pill_text(img, draw, box, text, colour):
    """Centre a short label in one of the frame's small pill slots."""
    x0, y0, x1, y1 = box
    f = _fit_text(draw, text, x1 - x0 - 16, 22, floor=13)
    _centre(draw, text, f, (x0 + x1) // 2, y0 + ((y1 - y0) - f.size) // 2 - 2,
            colour)


def _stat_cell(img, draw, box, label, value, colour):
    """One cell of the 2x3 grid: a small caption over a big number."""
    x0, y0, x1, y1 = box
    cx = (x0 + x1) // 2
    lf = _font(19)
    _centre(draw, label.upper(), lf, cx, y0 + 18, DIM)
    vf = _fit_text(draw, value, x1 - x0 - 22, 52, floor=24)
    _centre(draw, value, vf, cx, y0 + 46, colour)


def _disc_art(img, centre, radius, art):
    """Drop a circular image into one of the frame's two discs."""
    cx, cy = centre
    d = radius * 2
    art = art.resize((d, d), Image.LANCZOS) if art.size != (d, d) else art
    img.alpha_composite(art, (cx - radius, cy - radius))


def _initial_disc(img, draw, centre, radius, text, colour):
    """Fallback for a missing picture: the first letter on a tinted disc."""
    cx, cy = centre
    d2 = radius * 2
    _overlay(img, (cx - radius, cy - radius, cx + radius, cy + radius),
             lambda d: d.ellipse((0, 0, d2 - 1, d2 - 1),
                                 fill=colour + (46,),
                                 outline=colour + (190,), width=3))
    ch = (_sanitize(text) or "?")[0].upper()
    f = _font(int(radius * 1.15))
    draw.text((cx - _text_w(draw, ch, f) // 2, cy - int(radius * 0.78)),
              ch, font=f, fill=colour)


# ── Sections ──────────────────────────────────────────────────────────────────

def _header(img, draw, name, tier_name, tier_col, accent, level,
            rank_position, avatar_url):
    art = _avatar_image(avatar_url, AVATAR_R * 2) if avatar_url else None
    if art is not None:
        _disc_art(img, AVATAR_C, AVATAR_R, art)
    else:
        _initial_disc(img, draw, AVATAR_C, AVATAR_R, name, accent)

    nf = _fit_text(draw, name, CHIP_TIER[0] - NAME_X - 30, 54, floor=26)
    draw.text((NAME_X, NAME_Y), name, font=nf, fill=TEXT)

    subtitle = "BLADER PROFILE"
    if rank_position:
        subtitle += f"   ·   #{rank_position} ON THE SERVER"
    sf = _fit_text(draw, subtitle, CHIP_TIER[0] - NAME_X - 30, 23, floor=15)
    draw.text((NAME_X, SUB_Y), subtitle, font=sf, fill=DIM)

    _pill_text(img, draw, CHIP_TIER, tier_name.upper(), tier_col)
    lvl_txt = f"LEVEL {level}" + ("  MAX" if level >= MAX_LEVEL else "")
    _pill_text(img, draw, CHIP_LEVEL, lvl_txt, GOLD)


def _progress(img, draw, xp, rank_score, tier_col):
    level, into, span = _level_from_xp(xp)
    if level >= MAX_LEVEL:
        _slot_bar(img, draw, BAR_XP, 1.0, XP_COL, "MAX LEVEL", "100 / 100")
    else:
        _slot_bar(img, draw, BAR_XP, (into / span) if span else 0.0, XP_COL,
                  f"LEVEL {level}", f"{into:,} / {span:,} XP")

    _name, _col, floor, nxt = _tier_for(rank_score)
    if nxt is None:
        _slot_bar(img, draw, BAR_RANK, 1.0, tier_col, "TOP TIER",
                  f"{rank_score:,} RANK")
    else:
        span_r = max(1, nxt - floor)
        _slot_bar(img, draw, BAR_RANK, (rank_score - floor) / span_r, tier_col,
                  f"RANK {rank_score:,}", f"{nxt - rank_score:,} TO NEXT")


def _stat_grid(img, draw, profile):
    wins   = _num(profile.get("wins"))
    losses = _num(profile.get("losses"))
    played = wins + losses
    rate   = (wins / played * 100) if played else 0.0
    cells = [
        ("Wins",       f"{wins:,}",       WIN_COL),
        ("Losses",     f"{losses:,}",     LOSS_COL),
        ("Win rate",   f"{rate:.0f}%",    TEXT),
        ("Streak",     _short(_num(profile.get("win_streak"))),  GOLD),
        ("Best",       _short(_num(profile.get("best_streak"))), GOLD),
        ("Beycoins",   _short(_num(profile.get("coins"))),       COIN_COL),
    ]
    for i, (label, value, colour) in enumerate(cells):
        x0, x1 = GRID_COLS[i % 3]
        y0, y1 = GRID_ROWS[i // 3]
        _stat_cell(img, draw, (x0, y0, x1, y1), label, value, colour)


def _collection(img, draw, owned, total):
    total = int(total or 0)
    if total <= 0:
        # The roster count was unavailable, so a denominator would be a lie.
        # "0 / 1 BEYS" was what the old max(1, total) produced for a brand-new
        # player, which reads as a bug rather than an empty collection.
        _slot_bar(img, draw, BAR_COLL, 0.0, LOCKED_ACCENT,
                  "COLLECTION", f"{owned} BEYS")
        return
    _slot_bar(img, draw, BAR_COLL, owned / total, LOCKED_ACCENT,
              "COLLECTION", f"{owned} / {total} BEYS")


def _loadout(img, draw, blade):
    if not blade:
        # The empty state is what every brand-new player sees first, so it gets
        # the same shape as a real loadout — caption on the name line, pills
        # left empty — rather than a cramped label stuffed into the middle pill.
        _initial_disc(img, draw, ART_C, ART_R, "?", DIM)
        nf = _font(30)
        _centre(draw, "NO BEY EQUIPPED", nf, ART_C[0], 552, DIM)
        # The hint goes in the first stat-bar recess, not under the caption —
        # the pill row sits between them and the text would land on its edges.
        b = LOAD_BARS[0]
        hf = _font(22)
        _centre(draw, "USE  ;equip <name>", hf, (b[0] + b[2]) // 2,
                b[1] + ((b[3] - b[1]) - hf.size) // 2 - 2, DIM)
        return

    name   = _sanitize(str(blade.get("name", "Unknown")))
    rarity = str(blade.get("rarity", "Common"))
    rcol   = _rar_col(rarity)

    art = None
    try:
        art = _blade_art(name, ART_R * 2)
    except Exception:                                    # noqa: BLE001
        art = None
    if art is not None:
        _disc_art(img, ART_C, ART_R, art.convert("RGBA"))
    else:
        _initial_disc(img, draw, ART_C, ART_R, name, rcol)

    # The bey's own name sits above the pills, centred on the disc.
    nf = _fit_text(draw, name.upper(), 430, 34, floor=18)
    _centre(draw, name.upper(), nf, ART_C[0], 552, TEXT)

    _pill_text(img, draw, PILLS[0], rarity.upper(), rcol)
    _pill_text(img, draw, PILLS[1], str(blade.get("type", "—")).upper(), TEXT)
    lvl = _num(blade.get("level"), 1) or 1
    _pill_text(img, draw, PILLS[2], f"BEY LV {lvl}", GOLD)

    # `or {}` is not enough: a corrupted document can carry a STRING here,
    # which is truthy and then explodes on .get — taking the whole card down
    # and silently falling the player back to the embed. Same reasoning as
    # _num() for the scalar fields.
    stats = blade.get("stats")
    if not isinstance(stats, dict):
        stats = {}
    rows = (
        ("ATK", _num(stats.get("attack"))),
        ("DEF", _num(stats.get("defense"))),
        ("STA", _num(stats.get("stamina"))),
        ("HP",  _num(stats.get("hp"))),
    )
    for (label, value), box in zip(rows, LOAD_BARS):
        _slot_bar(img, draw, box, value / STAT_MAX, STAT_COL[label],
                  label, f"{value}")


# ── Entry point ───────────────────────────────────────────────────────────────

def render_profile_card(
    player: dict,
    profile: dict,
    blade: dict | None = None,
    *,
    total_beys: int | None = None,
    rank_position: int | None = None,
    avatar_url: str | None = None,
    theme: str = "default",
) -> "io.BytesIO | None":
    """Render the profile card. Returns a PNG buffer, or None on any failure."""
    if not CARD_ENABLED:
        return None
    try:
        img = _frame(theme)
        if img is None:
            return None
        draw = ImageDraw.Draw(img)

        if str(theme).lower() == "cyber_arena" and img.size == CYBER_SIZE:
            _cyber_content(img, player, profile, blade, total_beys, rank_position, avatar_url)
        else:
            name       = _sanitize(player.get("name", "Blader"))
            rank_score = max(0, _num(profile.get("rank_score")))
            xp         = max(0, _num(profile.get("xp")))
            tier_name, tier_col, _floor, _nxt = _tier_for(rank_score)
            level, _i, _s = _level_from_xp(xp)
            accent = _accent_for(tier_col)

            _header(img, draw, name, tier_name, tier_col, accent, level,
                    rank_position, avatar_url)
            _progress(img, draw, xp, rank_score, tier_col)
            _stat_grid(img, draw, profile)
            owned = len(set(profile.get("inventory") or []))
            _collection(img, draw, owned, total_beys or owned)
            _loadout(img, draw, blade)

        buf = io.BytesIO()
        # JPEG, not PNG. `PNG optimize=True` on this 1193x967 canvas took
        # 1,679 ms of a 1,690 ms render — the encode WAS the render — and
        # produced an 882 KB upload. JPEG at q88 takes ~12 ms for 253 KB with
        # no artefact visible on the text, because the frame is a dark
        # photographic texture and the card has no transparency to preserve.
        # Progressive so it paints top-down on a slow phone connection.
        #
        # The extension matters to the caller: Discord names the attachment
        # from it, so `_profile_card_file` sends profile.jpg.
        img.convert("RGB").save(buf, format="JPEG", quality=IMAGE_QUALITY,
                                optimize=True, progressive=True)
        buf.seek(0)
        return buf
    except Exception:
        log.exception("profile card render failed")
        return None
