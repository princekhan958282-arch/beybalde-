#!/usr/bin/env python3
"""
tools/add_limited_bey.py — flag a blade as limited-time and/or owner-bound.

The category is machinery, not a blade: `limited` / `available_until` and
`owner_ids` gate acquisition wherever it happens, and every card and list
badges them. This script is how you attach those flags to a blade without
hand-editing 900 KB of JSON and hoping.

It does NOT invent blades. Point it at one that exists.

    # A booster-exclusive Ultimate, available for two weeks:
    python3 tools/add_limited_bey.py "Ultimate Valkyrie (Black Edition)" \\
        --until 2026-09-30T23:59:59Z

    # A personal blade, obtainable by nobody but its owner:
    python3 tools/add_limited_bey.py "Some Blade" --owner 956773141265391676

    # Open-ended limited (badged now, closing date set later):
    python3 tools/add_limited_bey.py "Some Blade" --limited

    # Take the flags back off:
    python3 tools/add_limited_bey.py "Some Blade" --clear

`--check` prints what would change and writes nothing.

Note on what the flags mean, because it is easy to get backwards:
a closed window stops the blade being OBTAINED. It never removes one from
anybody's inventory — see utils/availability.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "beyblades.json")

sys.path.insert(0, ROOT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="blade name, exactly as it appears in the roster")
    ap.add_argument("--until", help="ISO-8601 close time, e.g. 2026-09-30T23:59:59Z")
    ap.add_argument("--limited", action="store_true",
                    help="mark limited with no closing date yet")
    ap.add_argument("--owner", action="append", type=int, default=None,
                    help="Discord id allowed to own it (repeatable)")
    ap.add_argument("--clear", action="store_true",
                    help="remove every availability flag")
    ap.add_argument("--check", action="store_true", help="print, don't write")
    args = ap.parse_args()

    with open(DATA, encoding="utf-8") as fh:
        doc = json.load(fh)

    blade = doc.get(args.name)
    if blade is None:
        near = [n for n in doc if args.name.lower() in n.lower()]
        print(f"No blade named {args.name!r}."
              + (f" Did you mean: {', '.join(near[:5])}?" if near else ""))
        return 1

    before = {k: blade.get(k) for k in ("limited", "available_until", "owner_ids")}

    if args.clear:
        for k in ("limited", "available_until", "owner_ids"):
            blade.pop(k, None)
    else:
        if args.until:
            blade["limited"] = True
            blade["available_until"] = args.until
        elif args.limited:
            blade["limited"] = True
            blade.setdefault("available_until", None)
        if args.owner:
            blade["owner_ids"] = sorted(set(args.owner))

    after = {k: blade.get(k) for k in ("limited", "available_until", "owner_ids")}
    if before == after:
        print("Nothing to change — pass --until, --limited, --owner or --clear.")
        return 0

    from utils.availability import is_available, is_owner_bound, expires_at
    print(f"{args.name}  [{blade.get('rarity')}]")
    print(f"  before: {before}")
    print(f"  after:  {after}")
    print(f"  obtainable right now: {is_available(blade)}"
          + ("  (owner-bound — only its named owners, ever)"
             if is_owner_bound(blade) else ""))
    end = expires_at(blade)
    if end:
        print(f"  window closes at unix {int(end)}")
    # The whole point of the flags is that rarity is untouched.
    print(f"  still sorts and colours as: {blade.get('rarity')}")

    if args.check:
        print("\ncheck only — nothing written")
        return 0

    with open(DATA, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"\nwrote {DATA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
