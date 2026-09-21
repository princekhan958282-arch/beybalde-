#!/usr/bin/env python3
"""
tools/optimize_assets.py — Blade art optimiser for Beycord
===========================================================

Normalises local Bey artwork from PNG, WebP, JPG and JPEG into renderer-ready
files. WebP is the preferred output because it keeps transparency while cutting
source size and decode work substantially compared with oversized PNGs.

Why
---
Source art often arrives at 900–3000+ px even though the largest current Bey
render surface is about 500 px:

    battle card   utils/image_generator._ART_BOX        = 420 px
    info card     HTML disc 250 px CSS x2 device scale  ~ 500 px
    profile card  Pillow disc                           = 208 px
    tournament    Pillow art                            < 104 px

So 512 px is the real ceiling. Anything larger is disk, decode time and
temporary RAM spent on pixels the bot never displays.

Input formats
-------------
* .webp  — preferred; transparency supported
* .png   — transparency supported
* .jpg   — opaque
* .jpeg  — opaque

Output defaults to WebP q92. --keep-png keeps PNG output instead.

Safety improvements
-------------------
* Applies EXIF orientation before resizing (important for phone JPG/JPEG art).
* Keeps RGB images RGB instead of adding a useless alpha channel.
* Preserves RGBA only when the source actually has transparency.
* Rejects animated images instead of silently taking frame 1.
* Writes atomically and verifies the encoded file before replacing the source.
* Detects duplicate stems (for example Valkyrie.png + Valkyrie.jpg)
  before either one can overwrite the other's output.
* Corrupt/unsupported files are reported per-file instead of crashing midway.

Usage
-----
    python tools/optimize_assets.py
    python tools/optimize_assets.py --dry-run
    python tools/optimize_assets.py --dir path
    python tools/optimize_assets.py --keep-png

Safe to re-run: already-small, correctly oriented WebP files are skipped.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
from collections import defaultdict

from PIL import Image, ImageOps

TARGET_PX = 512
WEBP_QUALITY = 92
WEBP_METHOD = 6
SRC_EXT = (".png", ".webp", ".jpg", ".jpeg")
_EXIF_ORIENTATION = 274


def _has_alpha(image: Image.Image) -> bool:
    """Return whether preserving an alpha channel is meaningful."""
    if image.mode in ("RGBA", "LA"):
        return True
    if image.mode == "P" and "transparency" in image.info:
        return True
    return False


def _prepare_image(path: str) -> Image.Image:
    """Load one supported image, orient it, resize it, and normalise its mode."""
    with Image.open(path) as source:
        if bool(getattr(source, "is_animated", False)) and int(
            getattr(source, "n_frames", 1)
        ) > 1:
            raise ValueError("animated images are not supported for Bey art")

        oriented = ImageOps.exif_transpose(source)
        oriented.load()

        mode = "RGBA" if _has_alpha(oriented) else "RGB"
        image = oriented.convert(mode)

    if max(image.size) > TARGET_PX:
        image.thumbnail((TARGET_PX, TARGET_PX), Image.LANCZOS)

    return image


def _save_image(image: Image.Image, target, *, keep_png: bool) -> None:
    """Encode an already-prepared image to a path or BytesIO."""
    if keep_png:
        image.save(target, "PNG", optimize=True, compress_level=9)
        return

    kwargs = {
        "format": "WEBP",
        "quality": WEBP_QUALITY,
        "method": WEBP_METHOD,
    }
    if image.mode == "RGBA":
        kwargs["alpha_quality"] = 100
    image.save(target, **kwargs)


def _encoded_size(image: Image.Image, *, keep_png: bool) -> int:
    buf = io.BytesIO()
    _save_image(image, buf, keep_png=keep_png)
    return len(buf.getvalue())


def _output_path(path: str, out_dir: str, keep_png: bool) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    ext = ".png" if keep_png else ".webp"
    return os.path.join(out_dir, stem + ext)


def _write_atomic(image: Image.Image, out: str, *, keep_png: bool) -> None:
    """Write+verify in the destination directory, then atomically replace."""
    out_dir = os.path.dirname(os.path.abspath(out)) or "."
    fd, temp_path = tempfile.mkstemp(
        prefix=".bey-art-", suffix=".tmp", dir=out_dir
    )
    os.close(fd)
    try:
        _save_image(image, temp_path, keep_png=keep_png)
        with Image.open(temp_path) as verify:
            verify.verify()
        os.replace(temp_path, out)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def optimise_one(
    path: str,
    out_dir: str,
    keep_png: bool = False,
    dry_run: bool = False,
) -> tuple[int, int, str]:
    """Optimise one file and return (old_bytes, new_bytes, output_name)."""
    old = os.path.getsize(path)
    image = _prepare_image(path)
    out = _output_path(path, out_dir, keep_png)

    if os.path.abspath(path) != os.path.abspath(out) and os.path.exists(out):
        raise FileExistsError(
            f"output already exists: {os.path.basename(out)}"
        )

    if dry_run:
        return old, _encoded_size(image, keep_png=keep_png), os.path.basename(out)

    _write_atomic(image, out, keep_png=keep_png)

    if os.path.abspath(path) != os.path.abspath(out):
        os.remove(path)

    return old, os.path.getsize(out), os.path.basename(out)


def _already_optimised_webp(path: str) -> bool:
    """True only when a WebP needs no resize or EXIF correction."""
    if not path.lower().endswith(".webp"):
        return False
    with Image.open(path) as image:
        if bool(getattr(image, "is_animated", False)) and int(
            getattr(image, "n_frames", 1)
        ) > 1:
            return False
        orientation = image.getexif().get(_EXIF_ORIENTATION, 1)
        return max(image.size) <= TARGET_PX and orientation in (None, 1)


def _stem_collisions(files: list[str]) -> dict[str, list[str]]:
    """Output collisions after extension conversion, case-insensitive."""
    groups: dict[str, list[str]] = defaultdict(list)
    for name in files:
        stem = os.path.splitext(name)[0].casefold()
        groups[stem].append(name)
    return {stem: names for stem, names in groups.items() if len(names) > 1}


def main() -> int:
    ap = argparse.ArgumentParser(description="Optimise Beycord blade art.")
    ap.add_argument("--dir", default=os.path.join("assets", "beys"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-png", action="store_true")
    args = ap.parse_args()

    directory = args.dir
    if not os.path.isdir(directory):
        print(f"no such folder: {directory}", file=sys.stderr)
        return 1

    files = sorted(
        name
        for name in os.listdir(directory)
        if name.lower().endswith(SRC_EXT) and not name.startswith("_")
    )
    if not files:
        print(f"no art found in {directory}")
        return 0

    collisions = _stem_collisions(files)
    if collisions:
        print(
            "refusing to optimise: multiple source files would map to the same "
            "output name:",
            file=sys.stderr,
        )
        for names in collisions.values():
            print("  " + " / ".join(names), file=sys.stderr)
        return 2

    tot_old = 0
    tot_new = 0
    skipped = 0
    failed = 0

    print(f"{'file':32} {'before':>9} {'after':>9} {'saved':>7}")
    for name in files:
        path = os.path.join(directory, name)
        try:
            if not args.keep_png and _already_optimised_webp(path):
                skipped += 1
                continue

            old, new, out_name = optimise_one(
                path,
                directory,
                keep_png=args.keep_png,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            failed += 1
            print(f"ERROR {name}: {exc}", file=sys.stderr)
            continue

        tot_old += old
        tot_new += new
        saved = 100 - (100 * new / old) if old else 0.0
        print(
            f"{out_name:32} {old // 1024:8d}K {new // 1024:8d}K "
            f"{saved:6.1f}%"
        )

    if tot_old:
        saved = 100 - (100 * tot_new / tot_old)
        print(
            f"\n{'TOTAL':32} {tot_old // 1024:8d}K {tot_new // 1024:8d}K "
            f"{saved:6.1f}%"
        )
    if skipped:
        print(f"({skipped} already-optimised WebP files skipped)")
    if failed:
        print(f"({failed} file(s) failed; successful files were left valid)")
    if args.dry_run:
        print("\n-- dry run: nothing written --")

    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
