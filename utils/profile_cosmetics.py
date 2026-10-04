"""Profile-card cosmetics: permanent purchases + equipped theme state."""
from __future__ import annotations

PROFILE_COSMETICS = {
    "cyber_arena": {
        "name": "Cyber Arena",
        "price": 90_000,
        "description": "Neon blue arena profile frame with a futuristic BEYcord HUD.",
    },
}
DEFAULT_PROFILE_THEME = "default"


class ProfileCosmeticError(Exception):
    """A refused profile cosmetic purchase/equip operation."""


def owned_themes(profile: dict) -> list[str]:
    raw = profile.get("owned_profile_themes")
    # Only a stored list of known string IDs proves ownership. Scalars and
    # mappings are corruption, not implicit purchases.
    if not isinstance(raw, list):
        raw = []
    owned = [DEFAULT_PROFILE_THEME]
    for key in raw:
        if not isinstance(key, str):
            continue
        key = key.strip().lower()
        if key in PROFILE_COSMETICS and key not in owned:
            owned.append(key)
    return owned


def equipped_theme(profile: dict) -> str:
    key = str(profile.get("equipped_profile_theme") or DEFAULT_PROFILE_THEME).strip().lower()
    return key if key in owned_themes(profile) else DEFAULT_PROFILE_THEME


def apply_profile_purchase(profile: dict, theme_key: str) -> dict:
    key = str(theme_key).lower().strip()
    item = PROFILE_COSMETICS.get(key)
    if not item:
        raise ProfileCosmeticError("That profile design is not in the shop.")

    owned = owned_themes(profile)
    if key in owned:
        raise ProfileCosmeticError(f"You already own **{item['name']}**.")

    raw_coins = profile.get("coins", 0)
    if isinstance(raw_coins, bool) or not isinstance(raw_coins, (int, str)):
        raise ProfileCosmeticError("Your balance could not be verified. Please try again later.")
    try:
        coins = int(raw_coins)
    except (ValueError, TypeError):
        raise ProfileCosmeticError("Your balance could not be verified. Please try again later.") from None
    price = int(item["price"])
    if coins < price:
        raise ProfileCosmeticError(
            f"**{item['name']}** costs 🪙 **{price:,}** — you have "
            f"**{coins:,}**, short by **{price - coins:,}**."
        )

    profile["coins"] = coins - price
    profile["owned_profile_themes"] = owned + [key]
    profile["equipped_profile_theme"] = key
    return {"theme": key, "name": item["name"], "spent": price, "coins": profile["coins"]}


def apply_profile_equip(profile: dict, theme_key: str) -> dict:
    key = str(theme_key).lower().strip()
    if key not in owned_themes(profile):
        raise ProfileCosmeticError("You do not own that profile design.")
    profile["equipped_profile_theme"] = key
    name = "Default Profile" if key == DEFAULT_PROFILE_THEME else PROFILE_COSMETICS[key]["name"]
    return {"theme": key, "name": name}
