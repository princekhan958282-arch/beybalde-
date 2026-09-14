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
# The old renderer is preserved byte-for-byte in info_card_legacy.py.  Every
# runtime import of utils.info_card is routed through info_card_router.py so V2
# and legacy cannot accidentally be mixed across commands.
try:
    import sys as _sys
    from . import info_card_router as _selected_info_card

    # Presentation-only V2 polish.  This is deliberately isolated so disabling
    # V2 or falling back to legacy remains a clean rollback path.
    try:
        from . import info_card_v2_fixups as _info_card_v2_fixups
        _info_card_v2_fixups.apply(_selected_info_card.v2)
    except Exception:
        pass

    info_card = _selected_info_card
    _sys.modules[f"{__name__}.info_card"] = _selected_info_card
except Exception:
    # If the router/V2 ever has an import-time problem, leave Python free to
    # import the original utils/info_card.py normally.  A visual upgrade must
    # never stop Beycord from booting.
    pass
