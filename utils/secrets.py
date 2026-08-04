"""
utils/secrets.py
----------------
Where credentials come from, in order of preference.

The hosting panel doesn't need to support "environment variables" for any of
this to work — two of the three options below are just files sitting next to
app.py.

    1. Real environment variables
       Pterodactyl sets these from the Startup tab. Cleanest if you have it.

    2. A .env file  ← recommended
       app.py already calls load_dotenv(), and python-dotenv is already in
       requirements.txt, so this works today with zero code changes. Create the
       file once through the panel's file manager. Because it is never included
       in a zip, re-uploading the bot can't overwrite it and it can't leak when
       you share a build.

    3. config_local.py  ← if you really want it hardcoded
       Copy config_local.example.py to config_local.py and paste your keys in.
       It is excluded from every zip and listed in .gitignore, so the secret
       stays on your server.

Why not hardcode straight into app.py: that file goes into every zip, every
screenshot and every copy you share. A bot token in app.py has already leaked
once from this project — anyone holding it can read your servers' messages,
kick members and rename things, and the only fix is a token reset that takes
the bot offline until you redeploy. Option 3 gives you the same convenience
without shipping the secret.
"""

import importlib.util
import logging
import os
from typing import Optional

log = logging.getLogger("beyblade_bot")

# Project root — utils/secrets.py -> utils/ -> <root>
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCAL_PATH = os.path.join(_ROOT, "config_local.py")


def _load_local():
    """Load config_local.py by absolute path.

    A plain `import config_local` would depend on the current working
    directory being the project root. Panels don't guarantee that — start the
    bot from anywhere else and the import fails silently, the token comes back
    empty, and the only symptom is a confusing "not set" message. Resolving the
    path from this file's own location removes that failure mode entirely.
    """
    if not os.path.exists(_LOCAL_PATH):
        return None
    try:
        spec = importlib.util.spec_from_file_location("config_local", _LOCAL_PATH)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as exc:
        log.warning(f"[secrets] config_local.py exists but couldn't be read: {exc}")
        return None


_local = _load_local()


def reload_local() -> None:
    """Re-read config_local.py without restarting (handy after editing it)."""
    global _local
    _local = _load_local()


def get(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read a secret from env → .env → config_local.py, in that order."""
    value = os.getenv(name)
    if value:
        return value
    if _local is not None:
        value = getattr(_local, name, None)
        if value:
            return str(value)
    return default


def source_of(name: str) -> str:
    """Where a secret was found — handy for a startup log line."""
    if os.getenv(name):
        return "environment/.env"
    if _local is not None and getattr(_local, name, None):
        return "config_local.py"
    return "not set"


def diagnose() -> dict:
    """What the loader can actually see. Used by ;audit db status so a
    misconfigured secret is a readable report instead of a guessing game."""
    keys = []
    if _local is not None:
        keys = [k for k in dir(_local)
                if k.isupper() and isinstance(getattr(_local, k, None), str)]
    return {
        "local_path":   _LOCAL_PATH,
        "local_exists": os.path.exists(_LOCAL_PATH),
        "local_loaded": _local is not None,
        "local_keys":   sorted(keys),
        "env_keys":     sorted(k for k in ("BOT_TOKEN", "GEMINI_API_KEY",
                                           "GEMINI_MODEL", "MYSQL_URL")
                               if os.getenv(k)),
    }


def require(name: str) -> Optional[str]:
    """Like get(), but logs a clear message naming all three options."""
    value = get(name)
    if not value:
        log.critical(
            f"{name} is not set. Set it in ANY of these:\n"
            f"  1. a .env file next to app.py:   {name}=your_value_here\n"
            f"  2. your panel's Startup variables\n"
            f"  3. config_local.py (copy config_local.example.py)"
        )
    return value
