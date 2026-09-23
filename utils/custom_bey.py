"""Pure validation/building helpers for player-created CUSTOM Beys."""
from __future__ import annotations
import copy
import re
from urllib.parse import urlparse

STAT_TOTAL = 395
STAT_MIN = 20
TYPES = ("Attack", "Defense", "Stamina", "Balance")
MAX_ABILITIES = 2

# Curated engine-native effects. Keeping authoring here prevents clients from
# submitting arbitrary ops/values while still using the normal AbilityEngine.
ABILITY_PRESETS = {
    "pattern_reader": {
        "label": "Pattern Reader", "cost": 20,
        "description": "Repeating an action becomes easier to read and resist.",
        "rule": {"when": "setup", "do": [{"op": "pattern_reader"}]},
    },
    "second_wind": {
        "label": "Second Wind", "cost": 25,
        "description": "Once per battle, recover Stamina after falling below 30% HP.",
        "rule": {"when": "setup", "do": [{"op": "second_wind"}]},
    },
    "opening_gambit": {
        "label": "Opening Gambit", "cost": 15,
        "description": "Your first Attack is a high-risk opening play.",
        "rule": {"when": "setup", "do": [{"op": "opening_gambit"}]},
    },
    "lasting_guard": {
        "label": "Lasting Guard", "cost": 20,
        "description": "Successful guarding can carry protection forward.",
        "rule": {"when": "setup", "do": [{"op": "lasting_guard"}]},
    },
    "battle_tempo": {
        "label": "Battle Tempo", "cost": 15,
        "description": "Charge becomes stronger after going unused.",
        "rule": {"when": "setup", "do": [{"op": "battle_tempo"}]},
    },
    "measured_strike": {
        "label": "Measured Strike", "cost": 20,
        "description": "Heavy hits prepare a controlled follow-up.",
        "rule": {"when": "setup", "do": [{"op": "measured_strike"}]},
    },
}
ABILITY_BUDGET = 40

SPECIAL_EFFECTS = {
    "none": ("No secondary effect", 0, []),
    "heal": ("Restore 20 HP", 10, [{"op": "heal", "value": 20}]),
    "shield": ("Gain a 20-point shield", 10, [{"op": "shield", "value": 20}]),
    "gauge": ("Recover 15 Special Gauge", 10, [{"op": "add_gauge", "value": 15}]),
}
SPECIAL_DAMAGE_MIN = 80
SPECIAL_DAMAGE_MAX = 140

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '\-]{2,31}$")


class CustomBeyError(ValueError):
    pass


def _image_ok(value: str) -> bool:
    if not value:
        return True
    try:
        p = urlparse(value)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def validate(name: str, bey_type: str, hp: int, attack: int, defense: int,
             stamina: int, ability_keys: list[str], special_name: str,
             special_damage: int, special_effect: str, image_url: str = "") -> None:
    if not _NAME_RE.fullmatch(name.strip()):
        raise CustomBeyError("Name must be 3-32 characters using letters, numbers, spaces, apostrophes or hyphens.")
    if bey_type not in TYPES:
        raise CustomBeyError("Type must be Attack, Defense, Stamina, or Balance.")
    stats = [int(hp), int(attack), int(defense), int(stamina)]
    if any(v < STAT_MIN for v in stats):
        raise CustomBeyError(f"Every stat must be at least {STAT_MIN}.")
    if sum(stats) != STAT_TOTAL:
        raise CustomBeyError(f"HP + ATK + DEF + STM must equal exactly {STAT_TOTAL}; yours totals {sum(stats)}.")
    keys = [str(k).strip().lower() for k in ability_keys if str(k).strip()]
    if len(keys) > MAX_ABILITIES or len(set(keys)) != len(keys):
        raise CustomBeyError("Choose up to two different abilities.")
    unknown = [k for k in keys if k not in ABILITY_PRESETS]
    if unknown:
        raise CustomBeyError("Unknown ability: " + ", ".join(unknown))
    cost = sum(ABILITY_PRESETS[k]["cost"] for k in keys)
    if cost > ABILITY_BUDGET:
        raise CustomBeyError(f"Ability cost is {cost}/{ABILITY_BUDGET}; choose a cheaper combination.")
    if not (3 <= len(special_name.strip()) <= 32):
        raise CustomBeyError("Special name must be 3-32 characters.")
    if not SPECIAL_DAMAGE_MIN <= int(special_damage) <= SPECIAL_DAMAGE_MAX:
        raise CustomBeyError(f"Special base damage must be {SPECIAL_DAMAGE_MIN}-{SPECIAL_DAMAGE_MAX}.")
    if special_effect not in SPECIAL_EFFECTS:
        raise CustomBeyError("Unknown Special secondary effect.")
    if not _image_ok(image_url.strip()):
        raise CustomBeyError("Image must be a valid http/https URL.")


def build(name: str, bey_type: str, hp: int, attack: int, defense: int,
          stamina: int, ability_keys: list[str], special_name: str,
          special_damage: int, special_effect: str, image_url: str = "") -> dict:
    name = name.strip()
    keys = [str(k).strip().lower() for k in ability_keys if str(k).strip()]
    special_effect = special_effect.strip().lower()
    validate(name, bey_type, hp, attack, defense, stamina, keys,
             special_name, special_damage, special_effect, image_url)

    abilities = []
    for key in keys:
        cfg = ABILITY_PRESETS[key]
        rule = copy.deepcopy(cfg["rule"])
        rule["_name"] = cfg["label"]
        abilities.append({
            "name": cfg["label"],
            "trigger": rule["when"],
            "description": cfg["description"],
            "rules": [rule],
        })

    effect_label, _cost, ops = SPECIAL_EFFECTS[special_effect]
    if ops:
        abilities.append({
            "name": special_name.strip(),
            "trigger": "on_special",
            "description": effect_label,
            "rules": [{"when": "on_special", "do": copy.deepcopy(ops),
                       "_name": special_name.strip()}],
            "_custom_special_effect": True,
        })

    return {
        "id": "CUSTOM",
        "name": name,
        "rarity": "Custom",
        "type": bey_type,
        "image_url": image_url.strip(),
        "description": "A player-created Bey built with /custombey.",
        "stats": {
            "hp": int(hp), "attack": int(attack), "defense": int(defense),
            "stamina": int(stamina), "special": 100,
        },
        "abilities": abilities,
        "ability": abilities[0] if abilities else None,
        "special_move": {
            "name": special_name.strip(), "hits": 1,
            "damage_per_hit": int(special_damage),
            "total_damage": int(special_damage),
            "description": effect_label,
            "flavour_texts": [f"✨ **{special_name.strip()}**!"],
        },
        "custom": True,
        "custom_meta": {
            "stat_total": STAT_TOTAL,
            "ability_keys": keys,
            "ability_cost": sum(ABILITY_PRESETS[k]["cost"] for k in keys),
            "special_effect": special_effect,
        },
    }


def from_profile(profile: dict):
    blade = (profile or {}).get("custom_bey")
    return copy.deepcopy(blade) if isinstance(blade, dict) else None
