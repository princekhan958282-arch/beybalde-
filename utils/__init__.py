# utils package

# Apply small built-in roster additions before any cog reads beyblades.json.
# The migration is idempotent: once the bey exists, later imports are read-only.
try:
    from .roster_migrations import apply_roster_migrations as _apply_roster_migrations
    _apply_roster_migrations()
except Exception:
    # A roster migration must never stop the bot from booting. Any failure is
    # intentionally non-fatal; the normal data file remains untouched.
    pass


# ── Info-card renderer selector ───────────────────────────────────────────────
# The pre-redesign renderer is frozen in info_card_legacy.py.  V2 is the
# default, but rejection/rollback is one setting + restart rather than a revert:
#
#   data/config.json  ->  {"info_card_version": "legacy"}
# or
#   BEYCORD_INFO_CARD_VERSION=legacy
#
# The sys.modules alias is deliberate.  Some older modules import
# ``utils.info_card`` directly while others use ``from utils import info_card``;
# both must resolve to the same selected implementation or the bot can show two
# different card designs in different commands.
try:
    import json as _json
    import os as _os
    import sys as _sys

    def _selected_info_card_version() -> str:
        env = _os.getenv("BEYCORD_INFO_CARD_VERSION", "").strip().lower()
        if env:
            return env
        try:
            _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            _cfg_path = _os.path.join(_root, "data", "config.json")
            with open(_cfg_path, "r", encoding="utf-8") as _fh:
                _cfg = _json.load(_fh)
            return str((_cfg or {}).get("info_card_version", "v2")).strip().lower()
        except Exception:
            return "v2"

    _info_style = _selected_info_card_version()
    if _info_style in {"legacy", "old", "v1", "1"}:
        from . import info_card_legacy as _selected_info_card
    else:
        from . import info_card_v2 as _selected_info_card

    info_card = _selected_info_card
    _sys.modules[f"{__name__}.info_card"] = _selected_info_card
except Exception:
    # If V2 itself ever has an import-time problem, leave Python free to import
    # the original utils/info_card.py normally.  A visual upgrade must never
    # stop Beycord from booting.
    pass
