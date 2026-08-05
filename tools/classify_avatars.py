#!/usr/bin/env python3
"""
tools/classify_avatars.py — derive an avatar's TYPE from the bonuses it already has.

Run once to stamp `"type"` onto every entry in avatar_data.json, then never again:
the field becomes authored data that a designer can override by hand. Keeping this
script around is what makes those 29 hand-checkable decisions reproducible, and
what lets a newly authored avatar get a suggested type instead of a guess.

    python3 tools/classify_avatars.py            # report only
    python3 tools/classify_avatars.py --write     # stamp the file

Why derive rather than assign: the 29 cards were authored over months with no type
concept, and their bonuses already encode an identity — Dyrroth is 100% offensive
weights, Marina is stamina and charge, Historia is defence and HP. Assigning types
by vibe would contradict what the cards actually DO, and the contradiction would
only surface later as "why is my Attack avatar bad at attacking".
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "cogs", "avatar", "avatar_data.json")

# Weight every bonus field into one of three identities.
#
# Percent fields are multiplied by 100 so they share a scale with flat fields —
# the roster's mean attack stat is ~97, so "+30 flat" and "+30%" are worth roughly
# the same and should count roughly the same here. The non-stat mechanics are
# weighted by which identity they SERVE, not by where they are implemented:
# dodge and counter are survival, crit and multi-hit are offence, gauge and
# stability are the engine that keeps you swinging.
ATTACK_WEIGHTS = {
    "attack_flat": 1.0, "attack_percent": 100.0,
    "crit_percent": 60.0,
    "special_move_flat": 0.5, "special_move_percent": 80.0,
    "nth_hit_attack_percent": 60.0, "defence_break_max": 60.0,
    "multi_hit_power_double": 40.0, "multi_hit_extra_hits": 30.0,
    "ult_adds_attack_stat": 40.0,
}
DEFENSE_WEIGHTS = {
    "defence_flat": 1.0, "defence_percent": 100.0,
    "resistance_damage_percent": 120.0, "resistance_status_chance": 40.0,
    "hp_flat": 0.35, "hp_percent": 100.0,
    "immortal_rounds": 25.0,
    "dodge_chance": 80.0, "counter_chance": 50.0,
}
STAMINA_WEIGHTS = {
    "stamina_flat": 1.0, "stamina_percent": 100.0,
    "charge_flat": 1.0, "charge_percent": 80.0,
    "stability_flat": 1.0, "stability_percent": 80.0,
    "gauge_on_crit": 0.4,
}

# A card is only given an identity when one actually dominates. Below either
# threshold it is Balance — which is a real answer here, not a fallback: a card
# with 187/148/121 across the three is genuinely a generalist.
MIN_SHARE = 0.45      # the leader must hold this much of the total weight
MIN_MARGIN = 0.12     # ...and beat the runner-up by this much of the total

# Hand overrides, applied after the derivation. Empty today; this exists so a
# designer disagreeing with one card does not have to fight the weights.
OVERRIDES: dict[str, str] = {}


def score(bonuses: dict) -> dict[str, float]:
    return {
        "attack":  sum(float(bonuses.get(k, 0) or 0) * w
                       for k, w in ATTACK_WEIGHTS.items()),
        "defense": sum(float(bonuses.get(k, 0) or 0) * w
                       for k, w in DEFENSE_WEIGHTS.items()),
        "stamina": sum(float(bonuses.get(k, 0) or 0) * w
                       for k, w in STAMINA_WEIGHTS.items()),
    }


def classify(bonuses: dict) -> tuple[str, float, dict]:
    s = score(bonuses)
    total = sum(s.values())
    if total <= 0:
        return "balance", 0.0, s
    ranked = sorted(s.items(), key=lambda kv: -kv[1])
    (top_name, top_val), (_, second_val) = ranked[0], ranked[1]
    share = top_val / total
    margin = (top_val - second_val) / total
    if share < MIN_SHARE or margin < MIN_MARGIN:
        return "balance", share, s
    return top_name, share, s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="stamp the derived type onto avatar_data.json")
    args = ap.parse_args()

    with open(DATA, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    counts: dict[str, int] = {}
    changed = 0
    print(f"{'id':<16}{'name':<18}{'rarity':<11}{'type':<9}{'lead':>5}"
          f"   atk / def / sta")
    for entry in data.get("avatars", []):
        derived, share, s = classify(entry.get("bonuses") or {})
        typ = OVERRIDES.get(entry["id"], derived)
        existing = entry.get("type")
        mark = " " if existing == typ else ("+" if existing is None else "!")
        counts[typ] = counts.get(typ, 0) + 1
        print(f"{mark}{entry['id']:<15}{entry['name'][:17]:<18}"
              f"{entry.get('rarity', '?'):<11}{typ:<9}{share * 100:4.0f}%"
              f"   {s['attack']:5.0f} /{s['defense']:6.0f} /{s['stamina']:6.0f}")
        if existing != typ:
            changed += 1
            if args.write:
                # Insert `type` directly after `rarity` so the file stays
                # readable — identity fields together, then economy, then art.
                rebuilt = {}
                for k, v in entry.items():
                    if k == "type":
                        continue
                    rebuilt[k] = v
                    if k == "rarity":
                        rebuilt["type"] = typ
                if "type" not in rebuilt:
                    rebuilt["type"] = typ
                entry.clear()
                entry.update(rebuilt)

    print(f"\ndistribution: {dict(sorted(counts.items()))}")
    print(f"{changed} entr{'y' if changed == 1 else 'ies'} differ from the file")

    if args.write and changed:
        with open(DATA, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"wrote {DATA}")
    elif args.write:
        print("nothing to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
