"""Tournament host authorization helpers.

NOTE: This file is temporarily minimal while tournament host authorization is
being wired. The full tournament implementation must be restored before merge.
"""
from .hosters import is_tournament_hoster

MASTER_ID = 956773141265391676
ADMIN_ROLE = "Tournament Admin"


def is_tournament_admin(user) -> bool:
    """Owner, Tournament Hoster, or anyone holding the Tournament Admin role."""
    if getattr(user, "id", None) == MASTER_ID:
        return True
    if is_tournament_hoster(user):
        return True
    roles = getattr(user, "roles", None) or []
    return any(getattr(r, "name", "") == ADMIN_ROLE for r in roles)
