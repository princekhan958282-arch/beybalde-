"""Compatibility exports for the former startup roster migration.

Unlock Unicorn is authored once in beys/unlock_unicorn.json. Startup must never
write character definitions or resurrect the removed monolithic catalogue.
"""
import copy
from .character_registry import REGISTRY

UNLOCK_UNICORN = copy.deepcopy(REGISTRY.find_bey('Unlock Unicorn'))
ROSTER_ADDITIONS = (UNLOCK_UNICORN,)


def apply_roster_migrations() -> list[str]:
    if UNLOCK_UNICORN is None:
        raise ValueError('Missing required authored Bey: Unlock Unicorn')
    return []
