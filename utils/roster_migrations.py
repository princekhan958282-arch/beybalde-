"""Small, idempotent roster additions applied before Beycord loads its cogs.

Why this exists
---------------
`data/beyblades.json` is a large monolithic registry.  Tiny roster additions can
be expressed here as one-time migrations without making startup depend on a
manual panel edit.  `apply_roster_migrations()` only writes when an entry is
actually missing, uses an atomic replace, and never overwrites an existing bey.
"""

from __future__ import annotations

import copy
import json
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA = os.path.join(_ROOT, "data", "beyblades.json")


UNLOCK_UNICORN = {
    "id": "BB118",
    "name": "Unlock Unicorn",
    "rarity": "Rare",
    "type": "Defense",
    "spin_direction": "Right",
    "burst_height": "Low",
    "image_url": "https://cdn.discordapp.com/attachments/1510856884943454208/1548959220303396894/BB_Unlock_Unicorn_Down_Needle_Beyblade.webp?ex=6aa8f430&is=6aa7a2b0&hm=4b90cfc63ee6940d832fabe869efea0432df10fcd358b2912cb24513371e9e52&",
    "description": (
        "A defensive unicorn built around deflection and counter-impact. Its "
        "rounded mane turns attacks aside while the raised horn stores that "
        "pressure for a sudden reversal."
    ),
    "stats": {
        "attack": 70,
        "defense": 112,
        "stamina": 82,
        "special": 100,
        "hp": 112,
    },
    "ability": {
        "name": "Unicorn Reversal",
        "trigger": "on_defense_win",
        "description": (
            "Every Defense win grants 1 Horn Charge (max 2). At 2 charges, "
            "the next hit Unicorn takes is softened by 25%, 35% of that hit "
            "is reflected back, and Unicorn restores 2 Stability. The Horn "
            "Charges are then consumed."
        ),
        "rules": [
            {
                "when": "on_defense_win",
                "do": [
                    {
                        "op": "gain_counter",
                        "name": "horn_charge",
                        "amount": 1,
                        "max": 2,
                        "label": "Horn Charge",
                        "emoji": "🦄",
                    }
                ],
                "_name": "Unicorn Reversal — Charge",
            },
            {
                "when": "on_take_damage",
                "if": [
                    {
                        "cond": "counter_at_least",
                        "name": "horn_charge",
                        "value": 2,
                    }
                ],
                "do": [
                    {"op": "reduce_damage_pct", "value": 25},
                    {"op": "reflect_pct", "value": 35},
                    {"op": "gain_stability", "value": 2},
                    {"op": "reset_counter", "name": "horn_charge"},
                    {
                        "op": "log",
                        "text": "🦄 **Unicorn Reversal** — the mane deflects the blow and the horn snaps back!",
                    },
                ],
                "_name": "Unicorn Reversal — Counter",
            },
        ],
    },
    "abilities": [
        {
            "name": "Unicorn Reversal",
            "trigger": "on_defense_win",
            "description": (
                "Every Defense win grants 1 Horn Charge (max 2). At 2 charges, "
                "the next hit Unicorn takes is softened by 25%, 35% of that hit "
                "is reflected back, and Unicorn restores 2 Stability. The Horn "
                "Charges are then consumed."
            ),
            "rules": [
                {
                    "when": "on_defense_win",
                    "do": [
                        {
                            "op": "gain_counter",
                            "name": "horn_charge",
                            "amount": 1,
                            "max": 2,
                            "label": "Horn Charge",
                            "emoji": "🦄",
                        }
                    ],
                    "_name": "Unicorn Reversal — Charge",
                },
                {
                    "when": "on_take_damage",
                    "if": [
                        {
                            "cond": "counter_at_least",
                            "name": "horn_charge",
                            "value": 2,
                        }
                    ],
                    "do": [
                        {"op": "reduce_damage_pct", "value": 25},
                        {"op": "reflect_pct", "value": 35},
                        {"op": "gain_stability", "value": 2},
                        {"op": "reset_counter", "name": "horn_charge"},
                        {
                            "op": "log",
                            "text": "🦄 **Unicorn Reversal** — the mane deflects the blow and the horn snaps back!",
                        },
                    ],
                    "_name": "Unicorn Reversal — Counter",
                },
            ],
        },
        {
            "name": "Alicorn Launch",
            "trigger": "on_special",
            "description": (
                "A grounding horn-first launch that restores 3 Stability. If "
                "Unicorn fires it while holding 2 Horn Charges, the strike adds "
                "50% of its Defense as bonus damage and grants 20% damage "
                "reduction for 1 turn, consuming the charges."
            ),
            "rules": [
                {
                    "when": "on_special",
                    "do": [{"op": "gain_stability", "value": 3}],
                    "_name": "Alicorn Launch — Stabilise",
                },
                {
                    "when": "on_special",
                    "if": [
                        {
                            "cond": "counter_at_least",
                            "name": "horn_charge",
                            "value": 2,
                        }
                    ],
                    "do": [
                        {
                            "op": "bonus_damage_stat",
                            "stat": "defense",
                            "scale": 0.50,
                        },
                        {
                            "op": "reduce_damage_pct_turns",
                            "value": 20,
                            "turns": 1,
                        },
                        {"op": "reset_counter", "name": "horn_charge"},
                    ],
                    "_name": "Alicorn Launch — Horn Release",
                },
            ],
        },
    ],
    "special_move": {
        "name": "🦄 Alicorn Launch",
        "description": (
            "Unlock Unicorn drives its horn through the clash and re-centres. "
            "Base damage 105; restores 3 Stability. At 2 Horn Charges it also "
            "adds 50% of Defense as bonus damage and gains 20% damage reduction "
            "for 1 turn."
        ),
        "hits": 1,
        "damage_per_hit": 105,
        "total_damage": 105,
        "flavour_texts": [
            "🦄 **Alicorn Launch** — Unicorn catches the impact on its horn and fires back!"
        ],
    },
}


ROSTER_ADDITIONS = (UNLOCK_UNICORN,)


def _next_free_id(doc: dict, requested: str) -> str:
    """Return requested unless another blade already owns it; then pick next."""
    used = {str(v.get("id", "")) for v in doc.values() if isinstance(v, dict)}
    if requested not in used:
        return requested

    nums = []
    for value in used:
        if value.startswith("BB") and value[2:].isdigit():
            nums.append(int(value[2:]))
    n = max(nums or [0]) + 1
    candidate = f"BB{n:03d}"
    while candidate in used:
        n += 1
        candidate = f"BB{n:03d}"
    return candidate


def apply_roster_migrations() -> list[str]:
    """Insert missing built-in roster entries and return names added."""
    if not os.path.isfile(_DATA):
        return []

    with open(_DATA, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        return []

    added: list[str] = []
    for template in ROSTER_ADDITIONS:
        name = str(template["name"])
        if name in doc:
            continue
        entry = copy.deepcopy(template)
        entry["id"] = _next_free_id(doc, str(entry["id"]))
        doc[name] = entry
        added.append(name)

    if not added:
        return []

    tmp = f"{_DATA}.roster.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, _DATA)

    # Verify the repair from disk. This catches deployment/filesystem cases
    # where the replace did not leave the roster in the state we intended.
    with open(_DATA, "r", encoding="utf-8") as fh:
        verified = json.load(fh)
    missing = [str(t["name"]) for t in ROSTER_ADDITIONS
               if str(t["name"]) not in verified]
    if missing:
        raise RuntimeError(
            "roster migration verification failed: " + ", ".join(missing)
        )
    return added
