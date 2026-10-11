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
    "bonus_damage": {"label":"Bonus Damage","cost":50,"description":"Adds 15 damage when triggered.","op":{"op":"bonus_damage","value":15}},
    "bonus_damage_pct": {"label":"Damage Boost","cost":50,"description":"Adds 15% damage when triggered.","op":{"op":"bonus_damage_pct","value":15}},
    "true_damage": {"label":"True Damage","cost":50,"description":"Deals 12 true damage when triggered.","op":{"op":"true_damage","value":12}},
    "heal": {"label":"Heal","cost":50,"description":"Restores 18 HP when triggered.","op":{"op":"heal","value":18}},
    "heal_pct": {"label":"Percent Heal","cost":50,"description":"Restores 8% max HP when triggered.","op":{"op":"heal_pct","value":8}},
    "shield": {"label":"Shield","cost":50,"description":"Gains a 20 HP shield when triggered.","op":{"op":"shield","value":20}},
    "gain_stamina": {"label":"Stamina Recovery","cost":50,"description":"Restores 3 Stamina when triggered.","op":{"op":"gain_stamina","value":3}},
    "gain_stability": {"label":"Stability Recovery","cost":50,"description":"Restores 8 Stability when triggered.","op":{"op":"gain_stability","value":8}},
    "enemy_lose_stability": {"label":"Stability Break","cost":50,"description":"Removes 8 enemy Stability when triggered.","op":{"op":"enemy_lose_stability","value":8}},
    "drain_stamina": {"label":"Stamina Drain","cost":50,"description":"Drains 2 enemy Stamina when triggered.","op":{"op":"drain_stamina","value":2}},
    "reduce_damage_pct_turns": {"label":"Guard Window","cost":50,"description":"Take 15% less damage for 2 turns.","op":{"op":"reduce_damage_pct_turns","value":15,"turns":2}},
    "reflect_pct_turns": {"label":"Reflect Window","cost":50,"description":"Reflect 15% damage for 2 turns.","op":{"op":"reflect_pct_turns","value":15,"turns":2}},
    "ignore_defense": {"label":"Defense Pierce","cost":50,"description":"Ignore enemy defense for 1 turn.","op":{"op":"ignore_defense","value":15,"turns":1}},
    "crit_chance": {"label":"Critical Focus","cost":50,"description":"Gain 10% critical chance.","op":{"op":"crit_chance","value":10}},
    "crit_damage": {"label":"Critical Power","cost":50,"description":"Increase critical damage.","op":{"op":"crit_damage","value":0.2}},
    "cleanse": {"label":"Cleanse","cost":50,"description":"Cleanse negative effects when triggered.","op":{"op":"cleanse"}},
}
# Effects that modify the current damage packet need a combat/damage trigger.
# Stateful/recovery effects can safely activate from any exposed player trigger.
DAMAGE_TRIGGERS = ("attack_win", "attack_hit", "defense_win", "stamina_win", "any_win", "special")
ABILITY_TRIGGER_COMPAT = {
    "bonus_damage": DAMAGE_TRIGGERS,
    "bonus_damage_pct": DAMAGE_TRIGGERS,
    "true_damage": DAMAGE_TRIGGERS,
    "ignore_defense": DAMAGE_TRIGGERS,
    "crit_chance": DAMAGE_TRIGGERS,
    "crit_damage": DAMAGE_TRIGGERS,
}
ALL_CUSTOM_TRIGGER_KEYS = tuple(CUSTOM_TRIGGERS)

def allowed_triggers(effect_key: str) -> tuple[str, ...]:
    return ABILITY_TRIGGER_COMPAT.get(effect_key, ALL_CUSTOM_TRIGGER_KEYS)

ABILITY_BUDGET = 100

# Forty additional bounded presets; operations are shared with the battle engine.
def _preset(label, cost, description, op, **params):
    return {"label": label, "cost": cost, "description": description,
            "op": {"op": op, **params}}

for stat, title in (("attack", "Attack"), ("defense", "Defense"), ("stamina", "Stamina")):
    ABILITY_PRESETS[f"{stat}_boost"] = _preset(f"{title} Boost", 50, f"Gain 12 {title} for 2 turns.", "buff", stat=stat, amount=12, turns=2)
    ABILITY_PRESETS[f"{stat}_surge"] = _preset(f"{title} Surge", 50, f"Gain 15% {title} for 2 turns.", "buff", stat=stat, pct=15, turns=2)
    ABILITY_PRESETS[f"{stat}_pressure"] = _preset(f"{title} Pressure", 50, f"Enemy loses 12 {title} for 2 turns.", "enemy_debuff", stat=stat, amount=12, turns=2)
    ABILITY_PRESETS[f"{stat}_shatter"] = _preset(f"{title} Shatter", 50, f"Enemy loses 15% {title} for 2 turns.", "enemy_debuff_pct", stat=stat, value=15, turns=2)

ABILITY_PRESETS.update({
    "balanced_boost": _preset("Balanced Boost", 50, "Gain 8 ATK, DEF and STM for 2 turns.", "buff_all", value=8, turns=2),
    "balanced_surge": _preset("Balanced Surge", 50, "Gain 10% ATK, DEF and STM for 2 turns.", "buff_all_pct", value=10, turns=2),
    "attack_growth": _preset("Attack Growth", 50, "Gain 4 ATK per activation, at most 4 stacks.", "stacking_buff", name="custom_attack_growth", stat="attack", per_stack=4, max=4),
    "defense_growth": _preset("Defense Growth", 50, "Gain 4 DEF per activation, at most 4 stacks.", "stacking_buff", name="custom_defense_growth", stat="defense", per_stack=4, max=4),
    "stamina_growth": _preset("Stamina Growth", 50, "Gain 4 STM per activation, at most 4 stacks.", "stacking_buff", name="custom_stamina_growth", stat="stamina", per_stack=4, max=4),
    "hp_barrier": _preset("HP Barrier", 50, "Gain a shield worth 10% of current HP.", "shield_pct", value=10, of="current_hp"),
    "max_hp_barrier": _preset("Maximum HP Barrier", 50, "Gain a shield worth 8% of max HP.", "shield_pct", value=8, of="max_hp"),
    "lifesteal": _preset("Life Steal", 50, "Gain 10% lifesteal for the battle.", "lifesteal_pct", value=10),
    "second_wind": _preset("Second Wind", 50, "Arm a 15% max HP revival; activates once per battle.", "revive_pct", value=15),
    "hp_regeneration": _preset("HP Regeneration", 50, "Regenerate 4 HP per turn for the battle.", "hp_regen", value=4),
    "special_charge": _preset("Special Charge", 50, "Add 12 damage to the next Special.", "prime_bonus", value=12),
    "hit_charge": _preset("Hit Charge", 50, "Add 10 damage to the next Attack or Special.", "prime_hit_bonus", value=10),
    "extra_special_hit": _preset("Extra Special Hit", 50, "Arm one extra hit on the next Special.", "bonus_special_hits", hits=1),
    "finishing_blow": _preset("Finishing Blow", 50, "Deal 20 true damage if enemy HP is below 25%.", "execute", value=20, enemy_hp_below_pct=.25),
    "enemy_hp_strike": _preset("Enemy HP Strike", 50, "Add damage equal to 4% of enemy current HP.", "bonus_damage_enemy_hp_pct", value=4),
    "missing_hp_drive": _preset("Missing HP Drive", 50, "Add up to 25% damage as your HP falls.", "damage_boost", based_on="missing_hp", scale=.25),
    "enemy_wound_drive": _preset("Enemy Wound Drive", 50, "Add up to 20% damage as enemy HP falls.", "damage_boost", based_on="enemy_missing_hp", scale=.20),
    "stamina_drive": _preset("Stamina Drive", 50, "Add up to 15% damage based on your stamina ratio.", "damage_boost", based_on="stamina", scale=.15),
    "attack_true_strike": _preset("Attack True Strike", 50, "Deal true damage equal to 6% of your ATK.", "true_damage_stat_pct", stat="attack", scale=.06),
    "defense_true_strike": _preset("Defense True Strike", 50, "Deal true damage equal to 6% of your DEF.", "true_damage_stat_pct", stat="defense", scale=.06),
    "stamina_true_strike": _preset("Stamina True Strike", 50, "Deal true damage equal to 6% of your STM.", "true_damage_stat_pct", stat="stamina", scale=.06),
    "heavy_guard": _preset("Heavy Guard", 50, "Take 25% less damage for 2 turns.", "reduce_damage_pct_turns", value=25, turns=2),
    "long_guard": _preset("Long Guard", 50, "Take 12% less damage for 4 turns.", "reduce_damage_pct_turns", value=12, turns=4),
    "heavy_reflect": _preset("Heavy Reflect", 50, "Reflect 25% damage for 2 turns.", "reflect_pct_turns", value=25, turns=2),
    "long_reflect": _preset("Long Reflect", 50, "Reflect 12% damage for 4 turns.", "reflect_pct_turns", value=12, turns=4),
    "burst_recovery": _preset("Burst Recovery", 50, "Restore 15 Stability.", "gain_stability", value=15),
    "burst_pressure": _preset("Burst Pressure", 50, "Remove 12 enemy Stability.", "enemy_lose_stability", value=12),
    "deep_drain": _preset("Deep Drain", 50, "Drain 3 enemy Stamina.", "drain_stamina", value=3),
})
# Restrict outgoing packet modifiers to hooks that actually carry damage.
for key, cfg in ABILITY_PRESETS.items():
    if cfg["op"]["op"] in {"damage_boost", "bonus_damage_enemy_hp_pct", "execute", "true_damage_stat_pct"}:
        ABILITY_TRIGGER_COMPAT[key] = DAMAGE_TRIGGERS
# Revival is armed once at the start, never rearmed by repeatable win hooks.
ABILITY_TRIGGER_COMPAT["second_wind"] = ("turn_start",)

# V3 balance: narrow the outliers and prevent stored shield/charge stacking.
RULES_VERSION = 3
ABILITY_COOLDOWN = 4
_REBALANCE = {
    "bonus_damage": ("Adds 10 damage when triggered.", {"value": 10}),
    "bonus_damage_pct": ("Adds 10% damage when triggered.", {"value": 10}),
    "true_damage": ("Deals 8 true damage when triggered.", {"value": 8}),
    "heal": ("Restores 12 HP when triggered.", {"value": 12}),
    "heal_pct": ("Restores 4% max HP when triggered.", {"value": 4}),
    "shield": ("Gains a 12 HP shield; total shields cap at 10% max HP.", {"value": 12, "cap_max_hp_pct": 10}),
    "gain_stamina": ("Restores 2 Stamina when triggered.", {"value": 2}),
    "gain_stability": ("Restores 5 Stability when triggered.", {"value": 5}),
    "enemy_lose_stability": ("Removes 5 enemy Stability when triggered.", {"value": 5}),
    "drain_stamina": ("Drains 1 enemy Stamina when triggered.", {"value": 1}),
    "reduce_damage_pct_turns": ("Take 10% less damage for 2 turns.", {"value": 10}),
    "reflect_pct_turns": ("Reflect 10% damage for 2 turns.", {"value": 10}),
    "ignore_defense": ("Reduce enemy Defense by 10% for 1 turn; does not bypass defense or counters.", {"op": "enemy_debuff_pct", "stat": "defense", "value": 10, "turns": 1}),
    "crit_chance": ("Gain 5% critical chance.", {"value": 5}),
    "crit_damage": ("Critical multiplier becomes 1.6x.", {"value": .1}),
    "balanced_boost": ("Gain 5 ATK, DEF and STM for 2 turns.", {"value": 5}),
    "balanced_surge": ("Gain 6% ATK, DEF and STM for 2 turns.", {"value": 6}),
    "hp_barrier": ("Gain a shield worth 5% current HP; total shields cap at 10% max HP.", {"value": 5, "cap_max_hp_pct": 10}),
    "max_hp_barrier": ("Gain a shield worth 4% max HP; total shields cap at 10% max HP.", {"value": 4, "cap_max_hp_pct": 10}),
    "lifesteal": ("Gain 5% lifesteal for the battle.", {"value": 5}),
    "second_wind": ("Arm a 10% max HP revival, once per battle.", {"value": 10}),
    "hp_regeneration": ("Regenerate 2 HP per turn for the battle.", {"value": 2}),
    "special_charge": ("Add 8 damage to the next Special; stored Special bonuses cap at 25.", {"value": 8, "cap": 25}),
    "hit_charge": ("Add 6 damage to the next Attack or Special; stored hit bonuses cap at 12.", {"value": 6, "cap": 12}),
    "extra_special_hit": ("Add 25 damage to the next Special; stored Special bonuses cap at 25. No full-strength extra hit.", {"op": "prime_bonus", "value": 25, "cap": 25}),
    "finishing_blow": ("Deal 12 true damage if enemy HP is below 25%.", {"value": 12}),
    "enemy_hp_strike": ("Add damage equal to 2% of enemy current HP.", {"value": 2}),
    "missing_hp_drive": ("Add up to 12% damage as your HP falls.", {"scale": .12}),
    "enemy_wound_drive": ("Add up to 10% damage as enemy HP falls.", {"scale": .10}),
    "stamina_drive": ("Add up to 8% damage based on your stamina ratio.", {"scale": .08}),
    "heavy_guard": ("Take 15% less damage for 2 turns.", {"value": 15}),
    "long_guard": ("Take 8% less damage for 2 turns.", {"value": 8, "turns": 2}),
    "heavy_reflect": ("Reflect 15% damage for 2 turns.", {"value": 15}),
    "long_reflect": ("Reflect 8% damage for 2 turns.", {"value": 8, "turns": 2}),
    "burst_recovery": ("Restore 8 Stability.", {"value": 8}),
    "burst_pressure": ("Remove 7 enemy Stability.", {"value": 7}),
    "deep_drain": ("Drain 2 enemy Stamina.", {"value": 2}),
}
for stat, title in (("attack", "Attack"), ("defense", "Defense"), ("stamina", "Stamina")):
    _REBALANCE.update({
        f"{stat}_boost": (f"Gain 8 {title} for 2 turns.", {"amount": 8}),
        f"{stat}_surge": (f"Gain 10% {title} for 2 turns.", {"pct": 10}),
        f"{stat}_pressure": (f"Enemy loses 8 {title} for 2 turns.", {"amount": 8}),
        f"{stat}_shatter": (f"Enemy loses 10% {title} for 2 turns.", {"value": 10}),
        f"{stat}_growth": (f"Gain 3 {title} per activation, at most 3 stacks.", {"per_stack": 3, "max": 3}),
        f"{stat}_true_strike": (f"Deal true damage equal to 3% of your {title}.", {"scale": .03}),
    })
for key, (description, params) in _REBALANCE.items():
    ABILITY_PRESETS[key]["description"] = description
    ABILITY_PRESETS[key]["op"].update(params)
ABILITY_PRESETS["ignore_defense"]["label"] = "Defense Weaken"
ABILITY_PRESETS["extra_special_hit"]["label"] = "Special Follow-Up"
for key, cfg in ABILITY_PRESETS.items():
    if key != "second_wind":
        cfg["description"] += f" Cooldown: {ABILITY_COOLDOWN} turns."

SPECIAL_EFFECTS = {
    "none": ("No secondary effect", 0, []),
    "heal": ("Restore 12 HP", 10, [{"op": "heal", "value": 12}]),
    "shield": ("Gain a 12-point shield (10% max HP shield cap)", 10, [{"op": "shield", "value": 12, "cap_max_hp_pct": 10}]),
}
SPECIAL_DAMAGE_MIN = 80
SPECIAL_DAMAGE_MAX = 140

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '\-]{2,31}$")


class CustomBeyError(ValueError):
    pass


def _image_ok(value: str) -> bool:
    if not value:
        return False
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
    incompatible = [
        f"{key}@{trigger}" for key, trigger in zip(keys, triggers)
        if trigger not in allowed_triggers(key)
    ]
    if incompatible:
        raise CustomBeyError("Incompatible ability trigger: " + ", ".join(incompatible))
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
        raise CustomBeyError("Image is required: provide a transparent PNG/WebP using a valid http/https URL.")


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
            "if": [{"cond": "not_on_cooldown", "name": f"custom_{key}"}],
        }
        thresholds = {"low_hp": ("hp_below_pct", .5), "low_stamina": ("stamina_below_pct", .35), "low_stability": ("stability_below_pct", .35)}
        if triggers[index] in thresholds:
            cond, value = thresholds[triggers[index]]
            rule["if"].append({"cond": cond, "value": value})
        rule["do"].append({"op": "start_cooldown", "name": f"custom_{key}", "turns": ABILITY_COOLDOWN})
        if key == "second_wind":
            rule["if"] = []
            rule["once"] = "battle"
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
            "ability_budget": ABILITY_BUDGET,
            "rules_version": RULES_VERSION,
            "special_effect": special_effect,
        },
    }


def from_profile(profile: dict):
    blade = (profile or {}).get("custom_bey")
    return balanced_definition(blade) if isinstance(blade, dict) else None


def balanced_definition(blade: dict) -> dict:
    """Refresh saved preset operations without editing player identity or stats.

    Names, ownership, approval and equipment remain saved as authored; battle
    and info readers get current server-owned preset magnitudes and limits.
    """
    out = copy.deepcopy(blade)
    if not out.get("custom"):
        return out
    meta = out.get("custom_meta") or {}
    keys = meta.get("ability_keys") or []
    triggers = meta.get("ability_triggers") or []
    # Older builder definitions retain cooldown names even without metadata.
    normal = [a for a in out.get("abilities", []) if not a.get("_custom_special_effect")]
    if not keys:
        for ability in normal:
            names = [op.get("name", "") for r in ability.get("rules", [])
                     for op in r.get("do", []) if op.get("op") == "start_cooldown"]
            key = next((n[7:] for n in names if n.startswith("custom_") and n[7:] in ABILITY_PRESETS), None)
            if key is None:
                return out
            keys.append(key)
    if any(k not in ABILITY_PRESETS for k in keys) or len(keys) > MAX_ABILITIES:
        return out
    rebuilt = []
    reverse = {v: k for k, v in CUSTOM_TRIGGERS.items()}
    for i, key in enumerate(keys):
        old = normal[i] if i < len(normal) else {}
        trigger = triggers[i] if i < len(triggers) else reverse.get(old.get("trigger"))
        if trigger not in allowed_triggers(key):
            trigger = allowed_triggers(key)[0]
        cfg = ABILITY_PRESETS[key]
        rule = {"when": CUSTOM_TRIGGERS[trigger], "_name": old.get("name", cfg["label"]),
                "if": [{"cond": "not_on_cooldown", "name": f"custom_{key}"}],
                "do": [copy.deepcopy(cfg["op"]), {"op": "start_cooldown", "name": f"custom_{key}", "turns": ABILITY_COOLDOWN}]}
        thresholds = {"low_hp": ("hp_below_pct", .5), "low_stamina": ("stamina_below_pct", .35), "low_stability": ("stability_below_pct", .35)}
        if trigger in thresholds:
            cond, value = thresholds[trigger]
            rule["if"].append({"cond": cond, "value": value})
        if key == "second_wind":
            rule["if"] = []
            rule["once"] = "battle"
        rebuilt.append({"name": old.get("name", cfg["label"]), "trigger": rule["when"],
                        "description": cfg["description"], "rules": [rule]})
    for old in out.get("abilities", []):
        if old.get("_custom_special_effect"):
            effect = meta.get("special_effect")
            if effect not in SPECIAL_EFFECTS:
                # Recover legacy heal/shield riders by operation name.
                ops = [op for r in old.get("rules", []) for op in r.get("do", [])]
                effect = next((op.get("op") for op in ops if op.get("op") in ("heal", "shield")), "none")
            label, _, ops = SPECIAL_EFFECTS[effect]
            if ops:
                rider = copy.deepcopy(old)
                rider.update(description=label, rules=[{"when": "on_special", "_name": old.get("name"), "do": copy.deepcopy(ops)}])
                rebuilt.append(rider)
    effect = meta.get("special_effect")
    if effect in SPECIAL_EFFECTS and isinstance(out.get("special_move"), dict):
        out["special_move"]["description"] = SPECIAL_EFFECTS[effect][0]
    out["abilities"] = rebuilt
    out["ability"] = rebuilt[0] if rebuilt else None
    out.setdefault("custom_meta", {})["rules_version"] = RULES_VERSION
    return out
