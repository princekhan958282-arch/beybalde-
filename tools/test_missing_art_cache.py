#!/usr/bin/env python3
"""Focused checks for the bounded missing-art cache."""
from __future__ import annotations

import tempfile

import utils.image_generator as renderer


def check(label: str, ok: bool, detail=None) -> None:
    if not ok:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS  {label}")


def main() -> int:
    old_dir = renderer._BEY_DIR
    old_max = renderer._MISSING_ART_CACHE_MAX
    try:
        with tempfile.TemporaryDirectory(prefix="missing-art-cache-") as temp:
            renderer._BEY_DIR = temp
            renderer.clear_cache()

            first = renderer._blade_art("Missing Alpha", 420)
            check("missing art returns None", first is None)
            check(
                "missing name is cached once independent of render size",
                list(renderer._missing_art_cache) == ["missing alpha"],
                list(renderer._missing_art_cache),
            )
            check(
                "missing art does not pollute decoded image cache",
                not renderer._art_cache,
                list(renderer._art_cache),
            )

            second = renderer._blade_art("Missing Alpha", 208)
            check("same missing art is reused across sizes", second is None)
            check(
                "cross-size miss still uses one negative-cache entry",
                len(renderer._missing_art_cache) == 1,
                list(renderer._missing_art_cache),
            )

            renderer._MISSING_ART_CACHE_MAX = 3
            with renderer._cache_lock:
                for name in ("one", "two", "three", "four"):
                    renderer._remember_missing_art_locked(name)
            keys = list(renderer._missing_art_cache)
            check("negative cache is hard-bounded", len(keys) == 3, keys)
            check("oldest missing entry is evicted first", "one" not in keys, keys)

            renderer._art_index = {"stale": "/tmp/stale.png"}
            renderer.clear_cache()
            check("clear_cache drops negative misses", not renderer._missing_art_cache)
            check("clear_cache refreshes the art index", renderer._art_index is None)
    finally:
        renderer._BEY_DIR = old_dir
        renderer._MISSING_ART_CACHE_MAX = old_max
        renderer.clear_cache()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
