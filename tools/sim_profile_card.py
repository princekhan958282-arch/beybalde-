#!/usr/bin/env python3
"""
tools/sim_profile_card.py — the profile card renders, for everybody.

A card renderer fails quietly by design: `;profile` falls back to an embed on
any exception, so a card that has been broken for weeks looks exactly like a
card that is switched off. That is how the player avatar went missing without
anyone noticing — it was never fetched at all, and the initial-letter disc was
a perfectly convincing placeholder.

So the assertions here are: it renders at all, for every shape of profile the
live database actually contains, and the pieces that can silently no-op
(avatar fetch, frame asset, slot geometry) are checked directly.

Run:  python3 tools/sim_profile_card.py [--write DIR]
"""
import io
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {detail}")


from PIL import Image, ImageDraw                              # noqa: E402
import utils.profile_card as PC                               # noqa: E402
from utils.profile_card import render_profile_card            # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "data", "beyblades.json"), encoding="utf-8") as fh:
    _doc = json.load(fh)
BLADES = _doc["beyblades"] if isinstance(_doc, dict) and "beyblades" in _doc else _doc
if isinstance(BLADES, dict):
    BLADES = list(BLADES.values())

WRITE = None
if "--write" in sys.argv:
    WRITE = sys.argv[sys.argv.index("--write") + 1]
    os.makedirs(WRITE, exist_ok=True)


def render(label, *a, **kw):
    """Render and return the decoded image, or None."""
    buf = render_profile_card(*a, **kw)
    if buf is None:
        return None
    data = buf.getvalue()
    if WRITE:
        with open(os.path.join(WRITE, f"{label}.png"), "wb") as fh:
            fh.write(data)
    return Image.open(io.BytesIO(data))


print("\n── 1. the frame asset ships and loads ───────────────────────────")
check("the frame asset exists", os.path.exists(PC._FRAME_PATH), PC._FRAME_PATH)
size_kb = os.path.getsize(PC._FRAME_PATH) / 1024
check(f"...and is a sane size to ship in a zip ({size_kb:.0f} KB)",
      size_kb < 1200, f"{size_kb:.0f} KB")

# THE check this file exists for. The card shipped once as a .jpg, which is not
# on the updater's suffix allowlist — so `_members` skipped it, the update
# reported success, and the frame simply never arrived on the host. The card
# then fell back to the embed with no error anywhere. An asset the updater
# cannot deliver is an asset that does not exist in production.
from utils.updater import ALLOWED_SUFFIXES, _is_protected     # noqa: E402

assets_dir = os.path.join(ROOT, "assets")
undeliverable, protected = [], []
for dirpath, _dirs, files in os.walk(assets_dir):
    for fn in files:
        rel = os.path.relpath(os.path.join(dirpath, fn), ROOT).replace(os.sep, "/")
        if not rel.endswith(ALLOWED_SUFFIXES):
            undeliverable.append(rel)
        if _is_protected(rel):
            protected.append(rel)
check("every file under assets/ has an updater-deliverable suffix",
      not undeliverable, undeliverable[:5])
check("...and none of them is on a PROTECTED path", not protected, protected[:5])
check("the frame itself would be delivered by an update",
      os.path.relpath(PC._FRAME_PATH, ROOT).replace(os.sep, "/")
      .endswith(ALLOWED_SUFFIXES)
      and not _is_protected(os.path.relpath(PC._FRAME_PATH, ROOT)
                            .replace(os.sep, "/")))
frame = PC._frame()
check("the frame loads", frame is not None)
check("...at the size the geometry was measured against",
      frame.size == (PC.W, PC.H), frame.size)
# _frame() must hand back a COPY, or one render's text ends up on the next.
f1, f2 = PC._frame(), PC._frame()
ImageDraw.Draw(f1).rectangle([0, 0, 50, 50], fill=(255, 0, 0, 255))
check("_frame() returns a copy, so renders cannot bleed into each other",
      f2.getpixel((10, 10)) != f1.getpixel((10, 10)))

print("\n── 2. every slot sits inside the frame ──────────────────────────")
boxes = {
    "CHIP_TIER": PC.CHIP_TIER, "CHIP_LEVEL": PC.CHIP_LEVEL,
    "BAR_XP": PC.BAR_XP, "BAR_RANK": PC.BAR_RANK, "BAR_COLL": PC.BAR_COLL,
}
for i, b in enumerate(PC.PILLS):
    boxes[f"PILL{i}"] = b
for i, b in enumerate(PC.LOAD_BARS):
    boxes[f"LOADBAR{i}"] = b
for r, (y0, y1) in enumerate(PC.GRID_ROWS):
    for c, (x0, x1) in enumerate(PC.GRID_COLS):
        boxes[f"GRID{r}{c}"] = (x0, y0, x1, y1)

bad = [(n, b) for n, b in boxes.items()
       if not (0 <= b[0] < b[2] <= PC.W and 0 <= b[1] < b[3] <= PC.H)]
check(f"all {len(boxes)} slots are on-canvas and non-empty", not bad, bad[:3])

for name, c, r in (("avatar", PC.AVATAR_C, PC.AVATAR_R),
                   ("bey art", PC.ART_C, PC.ART_R)):
    check(f"the {name} disc fits on the canvas",
          c[0] - r >= 0 and c[1] - r >= 0 and c[0] + r <= PC.W and c[1] + r <= PC.H,
          (c, r))

# Left column must not spill into the right column: the frame has a hard
# divider at x=671 and text crossing it would look like a rendering fault.
left = [b for n, b in boxes.items() if n.startswith(("BAR_XP", "BAR_RANK",
                                                     "BAR_COLL", "GRID"))]
check("left-column slots stay left of the frame's divider",
      all(b[2] <= 671 for b in left), [b for b in left if b[2] > 671])
right = [b for n, b in boxes.items() if n.startswith(("PILL", "LOADBAR"))]
check("right-column slots stay right of it",
      all(b[0] >= 671 for b in right), [b for b in right if b[0] < 671])

print("\n── 3. it renders for every shape of profile ─────────────────────")
blade = dict(BLADES[0])
blade["level"] = 100
cases = {
    "full": (
        {"name": "PrinceKhan", "id": 1},
        {"wins": 248, "losses": 91, "xp": 142_000, "coins": 1_284_000,
         "rank_score": 2450, "win_streak": 12, "best_streak": 31,
         "inventory": [b["name"] for b in BLADES[:37]]},
        blade, {"total_beys": len(BLADES), "rank_position": 3}),
    # A brand-new player: every counter zero, nothing equipped, nothing owned.
    # This is the majority of the live database — median balance is 0.
    "fresh": ({"name": "New Blader", "id": 2}, {}, None, {}),
    # The corrupted-document case _num() exists for.
    "junk": (
        {"name": "", "id": 3},
        {"wins": "lots", "losses": None, "xp": "abc", "coins": [],
         "rank_score": {}, "inventory": "not a list"},
        {"name": "Broken", "stats": "not a dict"}, {}),
    # Ceilings: max level, top tier, a stat at the cap, a huge coin pile.
    "maxed": (
        {"name": "A Very Long Display Name Indeed", "id": 4},
        {"wins": 99_999, "losses": 4, "xp": 5_000_000, "coins": 20_059_659,
         "rank_score": 9999, "win_streak": 412, "best_streak": 412,
         "inventory": [b["name"] for b in BLADES]},
        dict(blade, stats={"hp": 500, "attack": 500, "defense": 500,
                           "stamina": 500}, level=100),
        {"total_beys": len(BLADES), "rank_position": 1}),
}
for label, (player, prof, bl, kw) in cases.items():
    img = render(label, player, prof, bl, **kw)
    check(f"{label}: renders", img is not None)
    if img is not None:
        check(f"{label}: at the frame's size", img.size == (PC.W, PC.H), img.size)

# One blade per (rarity, type) pair. The risk in the loadout panel is a
# palette lookup for a rarity or type nobody anticipated, so covering each
# distinct combination is what matters — rendering all 80 costs a minute and
# proves nothing the sample does not.
sample, seen = [], set()
for b in BLADES:
    key = (str(b.get("rarity")), str(b.get("type")))
    if key not in seen:
        seen.add(key)
        sample.append(b)
broken = [b["name"] for b in sample
          if render_profile_card({"name": "T", "id": 9}, {"wins": 1}, b) is None]
check(f"every rarity/type combination renders ({len(sample)} of "
      f"{len(BLADES)} blades)", not broken, broken[:4])
check("...and the sample really covers the roster's rarities",
      {r for r, _t in seen} == {str(b.get("rarity")) for b in BLADES})

print("\n── 4. the player avatar is actually fetched ─────────────────────")
# The bug this replaces: the card drew an initial letter and never made a
# request, so no profile picture could ever appear.
src = Image.new("RGB", (512, 512), (230, 60, 120))
d = ImageDraw.Draw(src)
d.rectangle([0, 0, 255, 255], fill=(60, 140, 240))
buf = io.BytesIO()
src.save(buf, format="PNG")
RAW = buf.getvalue()


class _Resp:
    def read(self, n=None):
        return RAW

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_real = urllib.request.urlopen
calls = []
urllib.request.urlopen = lambda req, timeout=None: (calls.append(1), _Resp())[1]
try:
    PC._AVATAR_CACHE.clear()
    img = PC._avatar_image("https://cdn.discordapp.com/avatars/1/aaa.png", 134)
    check("an avatar URL is fetched and decoded", img is not None)
    check("...to the requested size", img and img.size == (134, 134),
          img and img.size)
    check("...as RGBA", img and img.mode == "RGBA")
    check("...masked to a circle (corner transparent)",
          img and img.getpixel((2, 2))[3] == 0,
          img and img.getpixel((2, 2)))
    check("...with the middle opaque", img and img.getpixel((67, 67))[3] == 255)
    before = len(calls)
    PC._avatar_image("https://cdn.discordapp.com/avatars/1/aaa.png", 134)
    check("a repeat URL is served from cache, not re-fetched",
          len(calls) == before, len(calls) - before)

    # The card must SHOW it — proven by rendering with and without and
    # comparing the disc, which is the check that the wiring exists at all.
    prof = {"wins": 1, "losses": 1}
    with_av = render("avatar_on", {"name": "Zed", "id": 5}, prof, None,
                     avatar_url="https://cdn.discordapp.com/avatars/1/aaa.png")
    without = render("avatar_off", {"name": "Zed", "id": 5}, prof, None)
    px = PC.AVATAR_C
    check("the avatar disc differs when a URL is supplied",
          with_av.getpixel(px) != without.getpixel(px),
          (with_av.getpixel(px), without.getpixel(px)))
finally:
    urllib.request.urlopen = _real

# Every failure path degrades to the initial disc rather than killing the card.
urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
try:
    check("an unreachable CDN returns None, not an exception",
          PC._avatar_image("https://x.test/a.png", 64) is None)
    img = render("avatar_fail", {"name": "Zed", "id": 6}, {"wins": 1}, None,
                 avatar_url="https://x.test/a.png")
    check("...and the card still renders without a picture", img is not None)
finally:
    urllib.request.urlopen = _real
check("an empty avatar URL is a no-op", PC._avatar_image("", 64) is None)
check("a None avatar URL is a no-op", PC._avatar_image(None, 64) is None)

print("\n── 5. the cog passes an avatar URL at all ───────────────────────")
# The renderer supporting avatars is worthless if the call site never sends
# one, which is exactly the state this release found it in.
with open(os.path.join(ROOT, "cogs", "economy", "profile.py"),
          encoding="utf-8") as fh:
    cog_src = fh.read()
check("profile.py reads display_avatar", "display_avatar" in cog_src)
check("...and passes avatar_url into the renderer",
      "avatar_url=avatar_url" in cog_src)

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
