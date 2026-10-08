# utils package

# Apply small built-in roster additions before any cog reads beyblades.json.
# The migration is idempotent: once the bey exists, later imports are read-only.
try:
    from .roster_migrations import apply_roster_migrations as _apply_roster_migrations
    _roster_added = _apply_roster_migrations()
except Exception:
    # Do not silently hide roster-repair failures. A swallowed exception can
    # leave player-owned Bey names orphaned while the bot appears healthy.
    import logging as _logging
    _logging.getLogger("beyblade_bot").exception(
        "[roster] built-in roster migration failed"
    )


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
    # The public compatibility renderer accepts the same cosmetic arguments
    # and retries routing after bootstrap has finished. Log the boot failure
    # so a production import/dependency problem can be diagnosed.
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "[info-card] selector import failed; compatibility renderer will retry lazily",
        exc_info=True,
    )
