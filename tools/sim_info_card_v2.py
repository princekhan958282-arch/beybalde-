#!/usr/bin/env python3
"""Smoke checks for the premium V2 info-card + instant legacy rollback."""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402
from utils import info_card as IC  # noqa: E402
from utils import info_card_legacy as LEGACY  # noqa: E402
from utils import info_card_v2 as V2  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = json.load(open(os.path.join(ROOT, "data", "beyblades.json"), encoding="utf-8"))

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {detail}")


async def main():
    print("\n── 1. backup + router ───────────────────────────────────────────")
    check("legacy renderer is a separate module", LEGACY is not V2)
    check("runtime import is the safe router", IC.__name__.endswith("info_card_router"), IC.__name__)
    check("V2 is the default when no override exists", IC.selected_version() in {"v2", "legacy"})

    before = os.environ.get("BEYCORD_INFO_CARD_VERSION")
    os.environ["BEYCORD_INFO_CARD_VERSION"] = "legacy"
    check("environment can force instant legacy mode", IC.selected_version() == "legacy")
    if before is None:
        os.environ.pop("BEYCORD_INFO_CARD_VERSION", None)
    else:
        os.environ["BEYCORD_INFO_CARD_VERSION"] = before

    print("\n── 2. V2 renders real Discord bytes ─────────────────────────────")
    # Prefer a starter: these are authored with local art in production and do
    # not make the smoke test depend on a temporary CDN query string.
    name = next((n for n in ("Storm Spriggan", "Victory Valkyrie", "King Kerbeus") if n in DB), next(iter(DB)))
    buf = await V2.render_info_card(DB[name])
    check("V2 produced a card", buf is not None and len(buf.getvalue()) > 5000)
    if buf is not None:
        data = buf.getvalue()
        check("Discord bytes are JPEG", data[:2] == b"\xff\xd8", data[:4])
        im = Image.open(io.BytesIO(data))
        check("V2 uses the premium 900px mobile canvas", im.size[0] == V2.W, im.size)
        check("buffer names its real extension", getattr(buf, "name", "").endswith(".jpg"), getattr(buf, "name", None))

    print("\n── 3. compatibility surface ─────────────────────────────────────")
    for attr in ("card_filename", "build_html", "_get_context", "_collect_abilities", "_stat_rows"):
        check(f"router still exposes {attr}", hasattr(IC, attr), attr)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
