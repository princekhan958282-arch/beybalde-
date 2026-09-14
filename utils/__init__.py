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
