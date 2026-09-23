"""Pure validation/building helpers for player-created CUSTOM Beys."""
from __future__ import annotations
import copy
import re
from urllib.parse import urlparse

STAT_TOTAL = 395
STAT_MIN = 20
TYPES = ("Attack", "Defense", "Stamina", "Balance")
MAX_ABILITIES = 2
CUSTOM_TRIGGERS = {
    "attack_win": "on_attack_win", "attack_loss": "on_attack_loss",
    "attack_hit": "on_attack_hit", "defense_win": "on_defense_win",
    "defense_loss": "on_defense_loss", "stamina_win": "on_stamina_win",
    "stamina_loss": "on_stamina_loss", "any_win": "on_any_win",
    "any_loss": "on_any_loss", "special": "on_special",
    "low_hp": "on_low_hp", "low_stamina": "on_low_stamina",
    "low_stability": "on_low_stability", "charge": "on_charge",
    "turn_start": "turn_start", "turn_end": "turn_end",
}

# Player-authorable effects. Each entry owns a bounded operation template;
# clients choose only the effect and trigger, never arbitrary op parameters.
ABILITY_PRESETS = {
    "bonus_damage": {"label":"Bonus Damage","cost":15,"description":"Adds 15 damage when triggered.","op":{"op":"bonus_damage","value":15}},
    "bonus_damage_pct": {"label":"Damage Boost","cost":20,"description":"Adds 15% damage when triggered.","op":{"op":"bonus_damage_pct","value":15}},
    "true_damage": {"label":"True Damage","cost":25,"description":"Deals 12 true damage when triggered.","op":{"op":"true_damage","value":12}},
    "heal": {"label":"Heal","cost":15,"description":"Restores 18 HP when triggered.","op":{"op":"heal","value":18}},
    "heal_pct": {"label":"Percent Heal","cost":20,"description":"Restores 8% max HP when triggered.","op":{"op":"heal_pct","value":8}},
    "shield": {"label":"Shield","cost":15,"description":"Gains a 20 HP shield when triggered.","op":{"op":"shield","value":20}},
    "gain_stamina": {"label":"Stamina Recovery","cost":15,"description":"Restores 3 Stamina when triggered.","op":{"op":"gain_stamina","value":3}},
    "gain_stability": {"label":"Stability Recovery","cost":15,"description":"Restores 8 Stability when triggered.","op":{"op":"gain_stability","value":8}},
    "enemy_lose_stability": {"label":"Stability Break","cost":20,"description":"Removes 8 enemy Stability when triggered.","op":{"op":"enemy_lose_stability","value":8}},
    "drain_stamina": {"label":"Stamina Drain","cost":20,"description":"Drains 2 enemy Stamina when triggered.","op":{"op":"drain_stamina","value":2}},
    "reduce_damage_pct_turns": {"label":"Guard Window","cost":20,"description":"Take 15% less damage for 2 turns.","op":{"op":"reduce_damage_pct_turns","value":15,"turns":2}},
    "reflect_pct_turns": {"label":"Reflect Window","cost":25,"description":"Reflect 15% damage for 2 turns.","op":{"op":"reflect_pct_turns","value":15,"turns":2}},
    "ignore_defense": {"label":"Defense Pierce","cost":25,"description":"Gain 15% defense pierce for 1 turn.","op":{"op":"ignore_defense","value":15,"turns":1}},
    "crit_chance": {"label":"Critical Focus","cost":15,"description":"Gain 10% critical chance.","op":{"op":"crit_chance","value":10}},
    "crit_damage": {"label":"Critical Power","cost":20,"description":"Increase critical damage.","op":{"op":"crit_damage","value":0.2}},
    "cleanse": {"label":"Cleanse","cost":20,"description":"Cleanse negative effects when triggered.","op":{"op":"cleanse"}},
}
ABILITY_BUDGET = 40

SPECIAL_EFFECTS = {
    "none": ("No secondary effect", 0, []),
    "heal": ("Restore 20 HP", 10, [{"op": "heal", "value": 20}]),
    "shield": ("Gain a 20-point shield", 10, [{"op": "shield", "value": 20}]),
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
             special_damage: int, special_effect: str, image_url: str = "",
             ability_triggers: list[str] | None = None) -> None:
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
    triggers = [str(t).strip().lower() for t in (ability_triggers or []) if str(t).strip()]
    if keys and len(triggers) != len(keys):
        raise CustomBeyError("Choose one trigger for every selected ability.")
    unknown_triggers = [t for t in triggers if t not in CUSTOM_TRIGGERS]
    if unknown_triggers:
        raise CustomBeyError("Unknown ability trigger: " + ", ".join(unknown_triggers))
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
          special_damage: int, special_effect: str, image_url: str = "",
          ability_triggers: list[str] | None = None) -> dict:
    name = name.strip()
    keys = [str(k).strip().lower() for k in ability_keys if str(k).strip()]
    special_effect = special_effect.strip().lower()
    triggers = [str(t).strip().lower() for t in (ability_triggers or []) if str(t).strip()]
    validate(name, bey_type, hp, attack, defense, stamina, keys,
             special_name, special_damage, special_effect, image_url, triggers)

    abilities = []
    for index, key in enumerate(keys):
        cfg = ABILITY_PRESETS[key]
        rule = {
            "when": CUSTOM_TRIGGERS[triggers[index]],
            "do": [copy.deepcopy(cfg["op"])],
            "_name": cfg["label"],
        }
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
            "ability_triggers": triggers,
            "ability_cost": sum(ABILITY_PRESETS[k]["cost"] for k in keys),
            "special_effect": special_effect,
        },
    }


def from_profile(profile: dict):
    blade = (profile or {}).get("custom_bey")
    return copy.deepcopy(blade) if isinstance(blade, dict) else None
