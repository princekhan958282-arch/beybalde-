"""Wild-spawn guard for Limited Beys.

`utils.availability.obtainable()` intentionally allows a Limited item while its
window is open. That is correct for event acquisition, but wild spawns are a
different route: Limited Beys must never enter either the normal weighted pool
or the hidden 1-in-N pool.
"""
from __future__ import annotations

from utils.availability import is_limited


def install(spawn_module) -> None:
    """Wrap every random wild-spawn picker with a Limited-content filter."""
    if getattr(spawn_module, "_limited_guard_installed", False):
        return

    original_pick = spawn_module._pick_random_beyblade
    original_hidden = spawn_module._roll_hidden_spawn

    def eligible_pool(beyblades: dict) -> dict:
        return {
            name: data
            for name, data in (beyblades or {}).items()
            if not is_limited(data)
        }

    def pick_random_beyblade(beyblades: dict):
        return original_pick(eligible_pool(beyblades))

    def roll_hidden_spawn(beyblades: dict):
        return original_hidden(eligible_pool(beyblades))

    spawn_module._pick_random_beyblade = pick_random_beyblade
    spawn_module._roll_hidden_spawn = roll_hidden_spawn
    spawn_module._limited_guard_installed = True
