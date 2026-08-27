#!/usr/bin/env python3
"""
tools/sim_info_card.py — the `;info` card renders fast, and is named honestly.

Why this file exists
--------------------
`PNG optimize=True` on a large Pillow canvas is catastrophically slow — it took
1,679 ms of a 1,690 ms profile-card render, and 356 ms of a 496 ms info-card
render. It was fixed in `utils/profile_card.py` in v88 and the identical line
sat untouched in `utils/info_card_pillow.py` for another four versions, because
nothing anywhere asserted that a card renders quickly. A slow render is not an
error; it just quietly costs a third of a second of the bot's only thread.

So the encode format is asserted here rather than trusted.

The second half guards a subtler thing. TWO renderers feed `;info` — Playwright
emits PNG, the Pillow fallback emits JPEG — and Discord decides how to display
an attachment from its FILENAME. Every caller used to hardcode `.png`, which
was correct only while Playwright was the only renderer. A mismatch there is
not an exception, it is a broken-image icon in a channel.

Run:  python3 tools/sim_info_card.py
"""
import io
import json
import os
import sys
import time

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


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = json.load(open(os.path.join(ROOT, "data", "beyblades.json"),
                    encoding="utf-8"))

from PIL import Image                                            # noqa: E402
from utils import info_card_pillow as P                          # noqa: E402
from utils import info_card as IC                                # noqa: E402

# A spread of card shapes: different rarities (different tints, so different
# cached backgrounds) and different ability counts (so different heights).
SAMPLE = [n for n in ("Bloody Longinus", "Ultimate Valkyrie", "Blood Dragon",
                      "Shining Shuriken", "Master Diabolos", "Storm Spriggan")
          if n in DB]

print("\n── 1. it renders at all ─────────────────────────────────────────")
check("there are sample blades to render", len(SAMPLE) >= 4, SAMPLE)
bufs = {}
for n in SAMPLE:
    try:
        bufs[n] = P.render_info_card_pillow(DB[n])
    except Exception as exc:                                     # noqa: BLE001
        bufs[n] = None
        check(f"{n} raised", False, repr(exc))
for n in SAMPLE:
    check(f"{n} produced a card", bufs.get(n) is not None
          and len(bufs[n].getvalue()) > 5000,
          len(bufs[n].getvalue()) if bufs.get(n) else None)

print("\n── 2. the bytes really are JPEG ─────────────────────────────────")
for n in SAMPLE:
    data = bufs[n].getvalue()
    check(f"{n}: JPEG magic bytes", data[:2] == b"\xff\xd8", data[:4])
    img = Image.open(io.BytesIO(data))
    check(f"{n}: decodes as a {img.size[0]}px-wide JPEG",
          img.format == "JPEG" and img.size[0] == P.W, (img.format, img.size))
check("the module declares the format it actually writes",
      P.IMAGE_FORMAT == "jpg", P.IMAGE_FORMAT)
check("...at a sane quality", 70 <= P.IMAGE_QUALITY <= 95, P.IMAGE_QUALITY)

# The regression itself, stated as source. `optimize=True` is fine on JPEG
# (it costs almost nothing there); it is `PNG` that must never come back.
src = open(os.path.join(ROOT, "utils", "info_card_pillow.py"),
           encoding="utf-8").read()
check("the renderer does not save PNG at all",
      '"PNG"' not in src and "'PNG'" not in src
      and 'format="PNG"' not in src)
pc = open(os.path.join(ROOT, "utils", "profile_card.py"), encoding="utf-8").read()
check("neither does the profile card — the same bug, same fix",
      'format="PNG"' not in pc)

print("\n── 3. it is fast, and stays fast ────────────────────────────────")
# Warm: fonts and the background cache are both lazily built, and the first
# render of a process legitimately pays for both.
for n in SAMPLE:
    P.render_info_card_pillow(DB[n])

times = {}
for n in SAMPLE:
    t = time.perf_counter()
    P.render_info_card_pillow(DB[n])
    times[n] = (time.perf_counter() - t) * 1000
worst = max(times.values())
print("     " + "  ".join(f"{n.split()[0]} {ms:.0f}ms" for n, ms in times.items()))
# The pre-fix render was 496 ms. 250 is a generous ceiling that still fails
# loudly if the PNG encode — or anything like it — comes back.
check(f"the slowest card renders in under 250 ms ({worst:.0f} ms)",
      worst < 250, f"{worst:.0f} ms")

# And prove the encode is no longer where the time goes.
img = Image.open(io.BytesIO(bufs[SAMPLE[0]].getvalue())).convert("RGB")
o = io.BytesIO()
t = time.perf_counter()
img.save(o, format="JPEG", quality=P.IMAGE_QUALITY, optimize=True,
         progressive=True)
enc = (time.perf_counter() - t) * 1000
check(f"the encode is a minority of the render ({enc:.0f} of "
      f"{times[SAMPLE[0]]:.0f} ms)",
      enc < times[SAMPLE[0]] * 0.6, (enc, times[SAMPLE[0]]))

print("\n── 4. the background cache is a cache, and is bounded ───────────")
P._BG_CACHE.clear()
P.render_info_card_pillow(DB[SAMPLE[0]])
check("one render populates it", len(P._BG_CACHE) >= 1, len(P._BG_CACHE))
before = len(P._BG_CACHE)
P.render_info_card_pillow(DB[SAMPLE[0]])
check("rendering the same blade again adds nothing",
      len(P._BG_CACHE) == before, (before, len(P._BG_CACHE)))

# It hands out copies. A shared Image would accumulate every card ever drawn.
bg1 = P._background(400, (20, 10, 5), (200, 160, 40))
bg2 = P._background(400, (20, 10, 5), (200, 160, 40))
check("it returns a fresh image each time, not the cached one",
      bg1 is not bg2)
check("...that is nonetheless identical", bg1.tobytes() == bg2.tobytes())
from PIL import ImageDraw                                        # noqa: E402
ImageDraw.Draw(bg1).rectangle((0, 0, 50, 50), fill=(255, 0, 255, 255))
bg3 = P._background(400, (20, 10, 5), (200, 160, 40))
check("drawing on one does not poison the cache",
      bg3.tobytes() == bg2.tobytes())

for h in range(100, 100 + (P._BG_CACHE_MAX + 8) * 7, 7):
    P._background(h, (20, 10, 5), (200, 160, 40))
check(f"it never grows past {P._BG_CACHE_MAX}",
      len(P._BG_CACHE) <= P._BG_CACHE_MAX, len(P._BG_CACHE))

print("\n── 5. the filename matches the bytes ────────────────────────────")
check("the renderer stamps its own name on the buffer",
      getattr(bufs[SAMPLE[0]], "name", "").endswith(".jpg"),
      getattr(bufs[SAMPLE[0]], "name", None))
check("_named calls JPEG bytes .jpg",
      IC._named(b"\xff\xd8\xff\xe0rest").name.endswith(".jpg"))
check("_named calls PNG bytes .png",
      IC._named(b"\x89PNG\r\n\x1a\nrest").name.endswith(".png"))
check("card_filename follows the buffer, not a hardcoded extension",
      IC.card_filename(bufs[SAMPLE[0]], "Storm Pegasus")
      == "storm_pegasus_card.jpg",
      IC.card_filename(bufs[SAMPLE[0]], "Storm Pegasus"))
check("...and PNG stays PNG",
      IC.card_filename(IC._named(b"\x89PNG\r\n\x1a\nx"), "Storm Pegasus")
      == "storm_pegasus_card.png")
check("a blade with a slash in its name cannot escape the filename",
      "/" not in IC.card_filename(bufs[SAMPLE[0]], "Left/Right"),
      IC.card_filename(bufs[SAMPLE[0]], "Left/Right"))
check("a buffer with no name at all still yields something sendable",
      IC.card_filename(io.BytesIO(b"x"), "Nameless")
      == "nameless_card.png")

# No caller may hardcode the extension any more — that is the actual bug.
for rel in ("cogs/economy/profile.py", "cogs/ui/inventory_ui.py"):
    text = open(os.path.join(ROOT, *rel.split("/")), encoding="utf-8").read()
    check(f"{rel} builds the card name from the buffer",
          "_card.png" not in text and "card_filename" in text)

print("\n── 6. the missing-browser latch ─────────────────────────────────")
check("the latch exists and starts clear",
      hasattr(IC, "_browser_unavailable") and IC._browser_unavailable == "",
      getattr(IC, "_browser_unavailable", "<missing>"))
ics = open(os.path.join(ROOT, "utils", "info_card.py"), encoding="utf-8").read()
check("it is only set for a MISSING binary, not for a crash",
      "executable doesn't exist" in ics.lower())
check("the Pillow fallback runs off the event loop",
      "asyncio.to_thread(render_info_card_pillow" in ics)

print("\n── 7. art_scale — zooming CDN art that has no local crop ────────")
# CDN-sourced art (no local file in assets/beys, which is the ONLY case that
# actually happens today — see git history) renders via CSS `object-fit:
# cover`, which crops its OVERFLOWING dimension centered on the image's own
# canvas by default. Two independent knobs exist for it: `art_position`
# (object-position — recenters the crop window on the actual subject, for a
# subject that isn't centered in its own canvas) and `art_scale` (transform:
# scale — zooms in further from wherever that window ends up).
NO_OVERRIDE = dict(DB[SAMPLE[0]])
NO_OVERRIDE.pop("art_scale", None)
NO_OVERRIDE.pop("art_position", None)
html_default = IC.build_html(NO_OVERRIDE)
check("a blade with neither override gets no style attribute at all "
      "(byte-identical to before this feature existed)",
      "transform: scale" not in html_default
      and "object-position" not in html_default)

SCALED = dict(NO_OVERRIDE)
SCALED["art_scale"] = 1.5
html_scaled = IC.build_html(SCALED)
check("art_scale renders as a CSS transform on the art image",
      'style="transform: scale(1.5);"' in html_scaled, html_scaled[:2000])

TOO_BIG = dict(NO_OVERRIDE)
TOO_BIG["art_scale"] = 50
check("an absurd art_scale is clamped, not applied verbatim "
      "(a bad value must not blow the art out of the disc entirely)",
      'style="transform: scale(3);"' in IC.build_html(TOO_BIG))

TOO_SMALL = dict(NO_OVERRIDE)
TOO_SMALL["art_scale"] = 0.2
check("a sub-1.0 art_scale is clamped up to 1.0 — this field only zooms IN, "
      "it can't shrink art below its normal size",
      "transform: scale" not in IC.build_html(TOO_SMALL),
      IC.build_html(TOO_SMALL)[:2000])

BAD = dict(NO_OVERRIDE)
BAD["art_scale"] = "not-a-number"
check("a garbage art_scale value doesn't raise — falls back to no zoom",
      "transform: scale" not in IC.build_html(BAD))

POSITIONED = dict(NO_OVERRIDE)
POSITIONED["art_position"] = "36% 50%"
html_pos = IC.build_html(POSITIONED)
check("art_position renders as a CSS object-position on the art image",
      'style="object-position: 36% 50%;"' in html_pos, html_pos[:2000])

BOTH = dict(NO_OVERRIDE)
BOTH["art_position"] = "36% 50%"
BOTH["art_scale"] = 1.2
html_both = IC.build_html(BOTH)
check("art_position and art_scale compose into one style attribute",
      'style="object-position: 36% 50%; transform: scale(1.2);"' in html_both,
      html_both[:2000])

BAD_POS = dict(NO_OVERRIDE)
BAD_POS["art_position"] = "center center"
check("a non-percentage art_position doesn't raise — falls back to no "
      "override rather than emitting invalid CSS",
      "object-position" not in IC.build_html(BAD_POS))

OOB_POS = dict(NO_OVERRIDE)
OOB_POS["art_position"] = "150% 20%"
html_oob = IC.build_html(OOB_POS)
check("an out-of-range art_position percentage is clamped to 100, not "
      "applied verbatim",
      'style="object-position: 100% 20%;"' in html_oob, html_oob[:2000])

NEG_POS = dict(NO_OVERRIDE)
NEG_POS["art_position"] = "-20% 50%"
check("a negative art_position doesn't match the percentage format at all "
      "and is rejected outright, rather than risk unclamped CSS",
      "object-position" not in IC.build_html(NEG_POS))

check("Radiant Valkyrie is actually authored with a reposition "
      "(its source art's subject isn't centered on its own canvas)",
      DB.get("Radiant Valkyrie", {}).get("art_position"),
      DB.get("Radiant Valkyrie", {}))
check("...and NOT also a scale — a scale with no reposition just crops "
      "MORE off the wrong side, which is exactly what the first pass "
      "shipped and got wrong",
      DB.get("Radiant Valkyrie", {}).get("art_scale") in (None, 1, 1.0),
      DB.get("Radiant Valkyrie", {}).get("art_scale"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
