"""Original boss Bey rewards use the ordinary inventory/component pipeline."""
from utils.database import get_beyblade
from utils.inventory import can_add
from utils.bey_components import reconcile
from utils import bey_levels
from . import boss_info


def grant_original(profile, key):
    name = boss_info.REGISTRY[key]["name"]
    blade = get_beyblade(name)
    if not blade or blade.get("boss_source") != key:
        raise ValueError(f"Original boss definition missing for {key}")
    if not can_add(profile):
        profile.setdefault("pending_boss_rewards", []).append(name)
        return name, True
    profile.setdefault("inventory", []).append(name)
    reconcile(profile)
    bey_levels.entry_for(profile, name)
    return name, False


def claim_pending(profile):
    pending = list(profile.get("pending_boss_rewards") or [])
    granted = []
    while pending and can_add(profile):
        name = pending[0]
        if not get_beyblade(name):
            break  # Keep a missing definition safe until it is restored.
        pending.pop(0)
        profile.setdefault("inventory", []).append(name)
        bey_levels.entry_for(profile, name)
        granted.append(name)
    profile["pending_boss_rewards"] = pending
    if granted:
        reconcile(profile)
    return granted, len(pending)
