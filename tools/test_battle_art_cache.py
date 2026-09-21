#!/usr/bin/env python3
"""Focused regression checks for the bounded Pillow blade-art cache."""
from __future__ import annotations

from PIL import Image

import utils.image_generator as renderer


def check(label: str, ok: bool, detail=None) -> None:
    if not ok:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS  {label}")


def main() -> int:
    original_bytes = renderer._ART_CACHE_MAX_BYTES
    original_items = renderer._ART_CACHE_MAX_ITEMS
    try:
        renderer.clear_cache()
        one = 64 * 64 * 4
        renderer._ART_CACHE_MAX_BYTES = one * 3
        renderer._ART_CACHE_MAX_ITEMS = 3

        with renderer._cache_lock:
            for i in range(3):
                renderer._art_cache_put_locked(
                    f"bey-{i}@64", Image.new("RGBA", (64, 64), (i, i, i, 255))
                )

            check("cache fills to configured item limit", len(renderer._art_cache) == 3)
            check(
                "decoded byte accounting matches RGBA pixels",
                renderer._art_cache_bytes_locked() == one * 3,
                renderer._art_cache_bytes_locked(),
            )

            # Touch the oldest entry. A true LRU must now preserve it and evict
            # bey-1 when the next image arrives.
            hit, _image = renderer._art_cache_get_locked("bey-0@64")
            check("existing art is a cache hit", hit)

            renderer._art_cache_put_locked(
                "bey-3@64", Image.new("RGBA", (64, 64), (3, 3, 3, 255))
            )
            keys = list(renderer._art_cache)
            check("LRU access protects recently used art", "bey-0@64" in keys, keys)
            check("least-recently-used art is evicted", "bey-1@64" not in keys, keys)
            check(
                "cache remains inside byte budget",
                renderer._art_cache_bytes_locked() <= renderer._ART_CACHE_MAX_BYTES,
                renderer._art_cache_bytes_locked(),
            )

            # Missing-art negative entries use no pixel memory, so the item cap
            # separately prevents an unlimited number of None keys.
            renderer._art_cache.clear()
            renderer._ART_CACHE_MAX_BYTES = one * 100
            renderer._ART_CACHE_MAX_ITEMS = 2
            for i in range(8):
                renderer._art_cache_put_locked(f"missing-{i}@64", None)
            check("negative cache entries are item-bounded", len(renderer._art_cache) == 2)

        renderer.clear_cache()
        check("normal cache clear still empties bounded cache", not renderer._art_cache)
    finally:
        renderer._ART_CACHE_MAX_BYTES = original_bytes
        renderer._ART_CACHE_MAX_ITEMS = original_items
        renderer.clear_cache()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
