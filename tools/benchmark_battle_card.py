#!/usr/bin/env python3
"""Repeatable benchmark and smoke test for the Pillow battle-card renderer.

Cold-cache measurements clear the renderer's background, artwork, and font
caches before every card. Warm-cache measurements model consecutive rounds in
the same battle. Fixture creation is deliberately outside the timed sections.
"""
from __future__ import annotations

import argparse
import cProfile
import importlib.util
import io
import json
import pstats
import statistics
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw


REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "battle_card_benchmark_renderer", REPO_ROOT / "utils" / "image_generator.py"
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Unable to load battle-card renderer")
renderer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(renderer)


LEFT = {
    "name": "Challenger Alpha",
    "blade": "Benchmark Phoenix",
    "hp": 376,
    "max_hp": 500,
    "stamina": 8.4,
    "max_stamina": 12,
    "gauge": 92,
    "gauge_max": 150,
    "stability": 71,
    "stability_max": 100,
    "statuses": ["ATK +24", "BURN x2", "PIERCE", "STACK x4"],
}
RIGHT = {
    "name": "Defender Omega",
    "blade": "Benchmark Dragon",
    "hp": 441,
    "max_hp": 540,
    "stamina": 9.1,
    "max_stamina": 13,
    "gauge": 118,
    "gauge_max": 150,
    "stability": 83,
    "stability_max": 110,
    "statuses": ["SHIELD 65", "DEF +18", "REFLECT 2", "MODE AEGIS"],
}


def _make_art(path: Path, primary: tuple[int, int, int], secondary: tuple[int, int, int]) -> None:
    """Create detailed, transparent source art representative of deployed PNGs."""
    size = 1400
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    center = size // 2
    for radius in range(590, 70, -24):
        mix = (590 - radius) / 520
        color = tuple(int(primary[i] * (1 - mix) + secondary[i] * mix) for i in range(3))
        draw.ellipse(
            (center - radius, center - radius, center + radius, center + radius),
            fill=color + (210,), outline=(255, 255, 255, 190), width=5,
        )
    for angle in range(0, 360, 15):
        import math
        a = math.radians(angle)
        x = center + int(math.cos(a) * 610)
        y = center + int(math.sin(a) * 610)
        draw.line((center, center, x, y), fill=(255, 255, 255, 115), width=9)
    image.save(path, format="PNG", compress_level=6)


def _clear_renderer_caches() -> None:
    renderer._bg_cache = None
    renderer._art_cache.clear()
    renderer._art_index = None
    renderer._font_cache.clear()
    renderer._cached_text_width.cache_clear()
    renderer._cached_text_mask.cache_clear()


def _render(round_no: int) -> tuple[float, bytes]:
    started = time.perf_counter_ns()
    buf = renderer.render_battle_card(round_no, LEFT, RIGHT)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return elapsed_ms, buf.getvalue()


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    return {
        "mean_ms": round(statistics.fmean(samples), 3),
        "median_ms": round(statistics.median(samples), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
        "p95_ms": round(ordered[p95_index], 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cold-runs", type=int, default=8)
    parser.add_argument("--warm-runs", type=int, default=30)
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="battle-card-bench-") as temp:
        art_dir = Path(temp)
        _make_art(art_dir / "benchmark phoenix.png", (230, 50, 55), (255, 170, 40))
        _make_art(art_dir / "benchmark dragon.png", (40, 95, 225), (100, 220, 255))
        renderer._BEY_DIR = str(art_dir)

        cold_samples: list[float] = []
        cold_payload = b""
        for round_no in range(1, args.cold_runs + 1):
            _clear_renderer_caches()
            elapsed, cold_payload = _render(round_no)
            cold_samples.append(elapsed)

        _clear_renderer_caches()
        _render(0)  # warm all renderer-owned caches; not included in results
        warm_samples: list[float] = []
        warm_payload = b""
        for round_no in range(1, args.warm_runs + 1):
            elapsed, warm_payload = _render(round_no)
            warm_samples.append(elapsed)

        decoded = Image.open(io.BytesIO(warm_payload))
        decoded.load()
        if args.preview:
            args.preview.parent.mkdir(parents=True, exist_ok=True)
            args.preview.write_bytes(warm_payload)

        result = {
            "cold_cache": _summary(cold_samples),
            "warm_cache": _summary(warm_samples),
            "consecutive_rounds_total_ms": round(sum(warm_samples), 3),
            "encoded_bytes": len(warm_payload),
            "image": {"format": decoded.format, "mode": decoded.mode, "size": list(decoded.size)},
            "runs": {"cold": args.cold_runs, "warm": args.warm_runs},
        }
        print(json.dumps(result, indent=2, sort_keys=True))

        if args.profile:
            _clear_renderer_caches()
            profiler = cProfile.Profile()
            profiler.enable()
            renderer.render_battle_card(1, LEFT, RIGHT)
            profiler.disable()
            print("\nCold-render profile (top 30 by cumulative time):")
            pstats.Stats(profiler).strip_dirs().sort_stats("cumulative").print_stats(30)

            renderer.render_battle_card(0, LEFT, RIGHT)
            profiler = cProfile.Profile()
            profiler.enable()
            renderer.render_battle_card(2, LEFT, RIGHT)
            profiler.disable()
            print("\nWarm-render profile (top 30 by cumulative time):")
            pstats.Stats(profiler).strip_dirs().sort_stats("cumulative").print_stats(30)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
