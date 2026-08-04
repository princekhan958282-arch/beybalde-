#!/usr/bin/env python3
"""
tools/optimize_assets.py — Blade art optimiser for Beycord
===========================================================

Shrinks ``assets/beys/`` art to what the renderers actually paint, and stores
it as alpha-preserving WebP.

Why
---
Source art arrives at 900–1300 px and 1.0–1.5 MB per blade. Nothing renders it
that big:

    battle card   utils/image_generator._ART_BOX        = 360 px
    info card     HTML disc 250 px CSS x2 device scale  ~ 440 px
    info card     Pillow fallback  R*1.84               = 235 px

So 512 px is the real ceiling (headroom above 440). Everything above that is
disk, RAM and render time spent on pixels nobody sees. At 78 blades the
difference is ~94 MB of art vs ~7 MB — on a Pterodactyl panel that matters.

Format
------
WebP q92 + ``alpha_quality=100``. Measured against the 900 px source at final
render size: mean RGB error ~3/255 (1.2%, invisible) and **zero alpha error**,
so the feathered cutout edges survive intact.

Palette-quantised PNG is a similar size but was rejected: it mangles the alpha
channel (max error 61) and visibly bands the cutout edges.

Usage
-----
    python tools/optimize_assets.py               # optimise assets/beys in place
    python tools/optimize_assets.py --dry-run     # report only, write nothing
    python tools/optimize_assets.py --dir path    # a different folder
    python tools/optimize_assets.py --keep-png    # write .png instead of .webp

Safe to re-run: already-optimised files are detected and skipped, so this can
be dropped into a deploy step or run after adding new art.
"""
from __future__ import annotations

import argparse
import os
import sys

from PIL import Image

TARGET_PX     = 512     # ceiling; see module docstring
WEBP_QUALITY  = 92
WEBP_METHOD   = 6       # slowest/best encoder effort — this is a build step
SRC_EXT       = (".png", ".webp", ".jpg", ".jpeg")


def optimise_one(path: str, out_dir: str, keep_png: bool = False,
                 dry_run: bool = False) -> tuple[int, int, str]:
    """Return (old_bytes, new_bytes, out_name) for one art file."""
    old = os.path.getsize(path)
    im = Image.open(path).convert("RGBA")

    if max(im.size) > TARGET_PX:
        im.thumbnail((TARGET_PX, TARGET_PX), Image.LANCZOS)

    stem = os.path.splitext(os.path.basename(path))[0]
    ext  = ".png" if keep_png else ".webp"
    out  = os.path.join(out_dir, stem + ext)

    if dry_run:
        import io
        b = io.BytesIO()
        if keep_png:
            im.save(b, "PNG", optimize=True)
        else:
            im.save(b, "WEBP", quality=WEBP_QUALITY, alpha_quality=100,
                    method=WEBP_METHOD)
        return old, len(b.getvalue()), os.path.basename(out)

    if keep_png:
        im.save(out, "PNG", optimize=True)
    else:
        im.save(out, "WEBP", quality=WEBP_QUALITY, alpha_quality=100,
                method=WEBP_METHOD)
        # drop the superseded source only once the new file is safely written
        if path != out and os.path.exists(out):
            os.remove(path)

    return old, os.path.getsize(out), os.path.basename(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Optimise Beycord blade art.")
    ap.add_argument("--dir", default=os.path.join("assets", "beys"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-png", action="store_true")
    args = ap.parse_args()

    d = args.dir
    if not os.path.isdir(d):
        print(f"no such folder: {d}", file=sys.stderr)
        return 1

    files = sorted(f for f in os.listdir(d)
                   if f.lower().endswith(SRC_EXT) and not f.startswith("_"))
    if not files:
        print(f"no art found in {d}")
        return 0

    tot_old = tot_new = 0
    skipped = 0
    print(f"{'file':28} {'before':>9} {'after':>9} {'saved':>7}")
    for f in files:
        p = os.path.join(d, f)
        im = Image.open(p)
        # Already small enough AND already WebP → nothing to gain, leave it.
        if (max(im.size) <= TARGET_PX and f.lower().endswith(".webp")
                and not args.keep_png):
            skipped += 1
            continue
        im.close()
        old, new, name = optimise_one(p, d, args.keep_png, args.dry_run)
        tot_old += old
        tot_new += new
        print(f"{name:28} {old//1024:8d}K {new//1024:8d}K "
              f"{100 - 100*new/old:6.1f}%")

    if tot_old:
        print(f"\n{'TOTAL':28} {tot_old//1024:8d}K {tot_new//1024:8d}K "
              f"{100 - 100*tot_new/tot_old:6.1f}%")
    if skipped:
        print(f"({skipped} already optimised, skipped)")
    if args.dry_run:
        print("\n-- dry run: nothing written --")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
