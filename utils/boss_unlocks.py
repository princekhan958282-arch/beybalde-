"""Permanent boss access earned by defeating specific official Beys in PvP."""
NEMESIS_UNLOCK_KEY = "defeated_void_longinus_pvp"
VOID_LONGINUS_ID = "BB079"
NEMESIS_REQUIREMENT = "Win a PvP battle against a player using **Void Longinus**."


def nemesis_unlocked(profile):
    return profile.get(NEMESIS_UNLOCK_KEY) is True


def record_pvp_unlock(profile, opponent_blade):
    """Called only for a real PvP winner; ownership/name similarity is insufficient."""
    if (opponent_blade or {}).get("id") == VOID_LONGINUS_ID:
        profile[NEMESIS_UNLOCK_KEY] = True
