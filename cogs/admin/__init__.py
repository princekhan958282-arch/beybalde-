"""cogs/admin/__init__.py — entry point for the admin subsystem.

One package, one cog. Until v1.13 this loaded `admin.py` (22 prefix commands),
`audit.py` (a group of six) and `console.py` (a slash command) as three
separate extensions; they are now one `/admin` panel plus three prefix
survivors, split across `actions.py` (what each action does) and `panel.py`
(how one is chosen).
"""
from .panel import setup

__all__ = ["setup"]
