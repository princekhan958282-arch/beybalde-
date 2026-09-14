"""cogs/spawn/__init__.py - Entry point for spawn subsystem."""

from . import spawn as _spawn
from .limited_guard import install as _install_limited_guard

# Limited is an acquisition/display flag, not a wild-spawn rarity. Install the
# guard before the cog starts so both normal and hidden spawn rolls exclude it.
_install_limited_guard(_spawn)

setup = _spawn.setup
__all__ = ["setup"]
