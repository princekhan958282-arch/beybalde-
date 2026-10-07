"""Permanent ;info-card cosmetics sold from the Profiles shop section."""
from __future__ import annotations

INFO_CARD_COSMETICS = {
    "beycbot_2ability": {
        "name": "BEYCBOT ;info Card",
        "price": 67_000,
        "description": "Futuristic cyan/gold ;info HUD made for Beys with exactly 2 abilities.",
        "required_abilities": 2,
    },
}
DEFAULT_INFO_CARD_THEME = "default"


class InfoCardCosmeticError(Exception):
    pass


def owned_info_themes(profile: dict) -> list[str]:
    raw = profile.get("owned_info_card_themes")
    if not isinstance(raw, list):
        raw = []
    owned = [DEFAULT_INFO_CARD_THEME]
    for key in raw:
        if isinstance(key, str):
            key = key.strip().lower()
            if key in INFO_CARD_COSMETICS and key not in owned:
                owned.append(key)
    return owned


def equipped_info_theme(profile: dict) -> str:
    key = str(profile.get("equipped_info_card_theme") or DEFAULT_INFO_CARD_THEME).strip().lower()
    return key if key in owned_info_themes(profile) else DEFAULT_INFO_CARD_THEME


def apply_info_card_purchase(profile: dict, theme_key: str) -> dict:
    key = str(theme_key).strip().lower()
    item = INFO_CARD_COSMETICS.get(key)
    if not item:
        raise InfoCardCosmeticError("That ;info design is not in the shop.")
    owned = owned_info_themes(profile)
    if key in owned:
        raise InfoCardCosmeticError(f"You already own **{item['name']}**.")
    try:
        coins = int(profile.get("coins", 0))
    except (TypeError, ValueError):
        raise InfoCardCosmeticError("Your balance could not be verified.") from None
    price = int(item["price"])
    if coins < price:
        raise InfoCardCosmeticError(
            f"**{item['name']}** costs 🪙 **{price:,}** — you have **{coins:,}**, "
            f"short by **{price - coins:,}**."
        )
    profile["coins"] = coins - price
    profile["owned_info_card_themes"] = owned + [key]
    profile["equipped_info_card_theme"] = key
    return {"theme": key, "name": item["name"], "spent": price, "coins": profile["coins"]}
