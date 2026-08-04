"""
Deployment check — run this ON THE VPS, next to the bot, to see what the
running install actually has on disk.

    python3 tools/check_deploy.py

If this prints the NEW values but Discord still shows the old ones, the files
landed but the bot process is still running the copy it loaded at startup —
restart it. If this prints the OLD values, the upload did not reach this
directory (wrong folder, or the panel extracted somewhere else).
"""

import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB   = os.path.join(ROOT, "data", "beyblades.json")

# (blade, dotted path, expected value) — the changes that should be live
CHECKS = [
    ("Vanish Fafnir",     "rarity",                    "Legendary"),
    ("Vanish Fafnir",     "spin_direction",            "Left"),
    ("Rage Longinus",     "abilities.0.max_stacks",    6),
    ("Ronin Dragoon",     "abilities.0.stamina_gain",  0.5),
]


def dig(obj, path):
    for part in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(part)]
        else:
            obj = obj.get(part)
        if obj is None:
            return None
    return obj


def main() -> int:
    print(f"reading: {DB}")
    if not os.path.exists(DB):
        print("  !! file does not exist — wrong directory?")
        return 1
    st = os.stat(DB)
    print(f"  size    {st.st_size:,} bytes")
    print(f"  mtime   {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}")
    print()

    data = json.load(open(DB, encoding="utf-8"))
    ok = True
    for blade, path, expect in CHECKS:
        got = dig(data.get(blade, {}), path)
        good = got == expect
        ok &= good
        print(f"  [{'OK ' if good else 'OLD'}] {blade:18} {path:26} "
              f"= {got!r}   (expected {expect!r})")

    faf = data.get("Vanish Fafnir", {})
    ab  = (faf.get("abilities") or [{}])[0]
    print()
    print(f"  Spin Absorb rules: {len(ab.get('rules') or [])} "
          f"(expected 4 — 0 means the old legacy-field version)")
    print(f"  blade description: {faf.get('description','')[:70]}...")
    print()
    print("RESULT:", "files are up to date — restart the bot if Discord still "
          "shows old data" if ok else "THIS COPY IS OLD — the upload did not "
          "land here")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
