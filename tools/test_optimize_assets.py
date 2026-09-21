#!/usr/bin/env python3
"""Focused regression checks for tools/optimize_assets.py."""
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "optimize_assets_under_test", ROOT / "tools" / "optimize_assets.py"
)
OPT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(OPT)


def check(label: str, ok: bool, detail=None) -> None:
    if not ok:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS  {label}")


def main() -> int:
    check(
        "supported source extensions stay explicit",
        OPT.SRC_EXT == (".png", ".webp", ".jpg", ".jpeg"),
        OPT.SRC_EXT,
    )

    with tempfile.TemporaryDirectory(prefix="bey-art-opt-") as temp:
        d = Path(temp)

        # Transparent oversized PNG: alpha must survive and dimensions must cap.
        png = d / "alpha.png"
        Image.new("RGBA", (1400, 900), (220, 30, 50, 128)).save(png)
        prepared_png = OPT._prepare_image(str(png))
        check("PNG is capped to 512px", max(prepared_png.size) <= OPT.TARGET_PX, prepared_png.size)
        check("PNG transparency stays RGBA", prepared_png.mode == "RGBA", prepared_png.mode)

        # Opaque JPG should remain RGB instead of allocating a useless alpha plane.
        jpg = d / "opaque.jpg"
        Image.new("RGB", (1200, 800), (20, 80, 190)).save(jpg, "JPEG", quality=92)
        prepared_jpg = OPT._prepare_image(str(jpg))
        check("JPG is capped to 512px", max(prepared_jpg.size) <= OPT.TARGET_PX, prepared_jpg.size)
        check("opaque JPG stays RGB", prepared_jpg.mode == "RGB", prepared_jpg.mode)

        # Phone-photo EXIF orientation must be baked before the renderer sees it.
        rotated = d / "phone.jpeg"
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (800, 400), (80, 160, 40)).save(rotated, "JPEG", exif=exif)
        prepared_rotated = OPT._prepare_image(str(rotated))
        check(
            "JPEG EXIF orientation is applied",
            prepared_rotated.height > prepared_rotated.width,
            prepared_rotated.size,
        )

        # Direct conversion should create verified WebP and only then remove source.
        source = d / "convert.png"
        Image.new("RGBA", (900, 900), (10, 30, 220, 180)).save(source)
        old, new, out_name = OPT.optimise_one(str(source), str(d))
        out = d / out_name
        check("PNG converts to preferred WebP", out.suffix == ".webp" and out.exists(), out_name)
        check("superseded source is removed after conversion", not source.exists())
        with Image.open(out) as converted:
            converted.load()
            check("converted WebP stays within render ceiling", max(converted.size) <= OPT.TARGET_PX, converted.size)
            check("converted transparent art keeps alpha", "A" in converted.getbands(), converted.mode)
        check("size accounting reports real bytes", old > 0 and new == out.stat().st_size, (old, new))

        # Small WebP is already renderer-ready and should not be re-encoded.
        small = d / "ready.webp"
        Image.new("RGBA", (400, 400), (40, 40, 40, 200)).save(small, "WEBP", quality=92)
        check("small WebP is detected as already optimised", OPT._already_optimised_webp(str(small)))

        # Dry run must never touch the source.
        dry = d / "dry.png"
        Image.new("RGBA", (700, 700), (100, 20, 30, 220)).save(dry)
        before = dry.read_bytes()
        _old, preview_size, preview_name = OPT.optimise_one(str(dry), str(d), dry_run=True)
        check("dry-run predicts WebP output", preview_name == "dry.webp", preview_name)
        check("dry-run produces a non-empty estimate", preview_size > 0, preview_size)
        check("dry-run leaves source unchanged", dry.read_bytes() == before)

        collisions = OPT._stem_collisions(["Valkyrie.png", "VALKYRIE.jpg", "Dragon.webp"])
        check("duplicate output stems are detected case-insensitively", "valkyrie" in collisions, collisions)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
