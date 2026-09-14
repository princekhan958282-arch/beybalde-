#!/usr/bin/env python3
"""Render the exact V2 Discord info-card bytes for Unlock Unicorn."""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image
from utils.roster_migrations import UNLOCK_UNICORN
from utils import info_card_v2

OUT = ROOT / "artifacts" / "unlock_unicorn_info_v2.jpg"


def main() -> None:
    blade = dict(UNLOCK_UNICORN)
    blade.setdefault("ratchet", "Down")
    blade.setdefault("bit", "Needle")

    buf = info_card_v2.render_info_card_pillow(
        blade,
        parts={"ratchet": "Down", "bit": "Needle"},
    )
    if buf is None:
        raise SystemExit("V2 renderer returned no card")

    data = buf.getvalue()
    if not data.startswith(b"\xff\xd8"):
        raise SystemExit("V2 preview is not JPEG")

    img = Image.open(io.BytesIO(data))
    if img.width != info_card_v2.W:
        raise SystemExit(f"wrong width: {img.width} != {info_card_v2.W}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(data)
    print(f"preview={OUT}")
    print(f"size={img.size[0]}x{img.size[1]}")
    print(f"bytes={len(data)}")
    print(f"format={img.format}")


if __name__ == "__main__":
    main()
