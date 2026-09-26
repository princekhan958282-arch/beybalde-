"""Tournament-only host authorization.

These IDs may open/host tournaments without receiving general BEYcord admin
permissions. Keep this list deliberately narrow; role-based Tournament Admin
access is still handled by tournament.py.
"""

# Discord user IDs explicitly trusted to host tournaments.
TOURNAMENT_HOSTER_IDS: frozenset[int] = frozenset({
    1030250591651385354,
})


def is_tournament_hoster(user) -> bool:
    """Return True when *user* is an explicitly configured Tournament Hoster."""
    return getattr(user, "id", None) in TOURNAMENT_HOSTER_IDS
