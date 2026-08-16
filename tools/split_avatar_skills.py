#!/usr/bin/env python3
"""
tools/split_avatar_skills.py — one-shot data migration.

The nine signature avatars each carry three skills whose effects were summed
into ONE card-level `bonuses` block. The new battle system activates exactly
one skill per battle, so each skill needs to own its own slice of that block.

This script writes `skills[].bonuses` for the nine cards, then ORDERS the three
skills weakest → strongest, because slot position sets the energy price
(25 / 50 / 75). A card whose 75-energy skill was weaker than its 25-energy one
would be a trap, and Mare, Cobra Titan and Freya were all authored that way.

Ordering is by measured score, not by eye — see `score()`. Run it again after
editing a card and it is idempotent: the split is declared here, so re-running
reproduces the same file.

Run:  python3 tools/split_avatar_skills.py [--check]
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "cogs", "avatar", "avatar_data.json")

sys.path.insert(0, ROOT)
# Imported, not restated. Two copies of "which keys are outside the split"
# would be one copy too many the first time either changed.
from cogs.avatar.avatar_skills import CARD_LEVEL_BONUS_KEYS      # noqa: E402

# Which bonus keys each named skill owns. Every non-zero key on a card must
# appear exactly once across its three skills — verified below, so a skill
# cannot silently lose an effect or two skills claim the same one.
SPLIT: dict[str, dict[str, dict]] = {
    "Argus": {
        "Titan's Might":  {"attack_percent": 0.30},
        "Piercing Gaze":  {"crit_percent": 0.40, "gauge_on_crit": 50},
        "Deathless":      {"immortal_rounds": 2},
    },
    "Dyrroth": {
        "Rhythm of Ruin": {"nth_hit_interval": 3, "nth_hit_attack_percent": 0.20},
        "Guard Breaker":  {"defence_break_min": 0.40, "defence_break_max": 0.76,
                           "defence_break_rounds": 3},
        "Abyssal Verdict": {"special_move_percent": 1.11,
                            "ult_adds_attack_stat": True},
    },
    "Mare": {
        "Bulwark":        {"defence_flat": 209.0},
        "Iron Verdict":   {"defence_percent": 0.30},
        "Standing Ground": {"attack_flat": 40.0, "stamina_flat": 40.0},
    },
    "Cobra Titan": {
        "Venom Strike":   {"attack_flat": 58.0},
        "Endless Coil":   {"stamina_flat": 169.0},
        "Scaled Guard":   {"stamina_percent": 1.20,
                           "resistance_damage_percent": 0.12},
    },
    "Miffy": {
        "Soft Power":     {"attack_percent": 0.25},
        "Plush Armour":   {"defence_percent": 0.50},
        "Unbothered":     {"resistance_damage_percent": 0.08},
    },
    "Freya": {
        "Spirit Blade":     {"attack_flat": 96.0},
        "Valkyrie Descent": {"attack_percent": 0.22},
        # counter_chance was unreachable: the engine only rolled a counter
        # inside the dodge-success branch and Freya has no dodge. The skill
        # text says "a blocked hit answers back", so absorb_incoming now rolls
        # it after a RESISTED hit too. Keeping both keys on one skill is what
        # makes that pairing legible.
        "Aegis of Asgard":  {"resistance_damage_percent": 0.15,
                             "counter_chance": 0.25},
    },
    "Vexana": {
        "Cursed Oath":    {"special_move_percent": 0.64},
        "Deathly Grasp":  {"attack_flat": 61.0, "defence_flat": 61.0},
        "Soul Steal":     {"nth_hit_interval": 3, "nth_hit_attack_percent": 0.18},
    },
    "Helcurt": {
        "Deadly Stinger":     {"attack_flat": 96.0, "attack_percent": 0.20},
        "Shadow Transition":  {"crit_percent": 0.30, "dodge_chance": 0.05},
        "Nightfall Arrival":  {"defence_break_min": 0.55, "defence_break_max": 0.80,
                               "defence_break_rounds": 3},
    },
    "Eudora": {
        "Forked Lightning": {"attack_flat": 62.0, "attack_percent": 0.18},
        "Electric Arrow":   {"special_move_percent": 0.88},
        "Thunder's Wrath":  {"crit_percent": 0.26, "multi_hit_extra_hits": True},
    },
}

# Rough worth of one point of each effect, on a reference bey with ~120 attack
# and ~120 defence. Only the ORDER this produces matters — it decides which
# skill is the 25-energy pick and which is the 75. Percent terms are scored
# against the reference stat so a flat and a percent line are comparable.
REF = 120.0
WEIGHT: dict[str, float] = {
    "attack_flat":               1.0,
    "attack_percent":            REF,
    "defence_flat":              0.8,
    "defence_percent":           REF * 0.8,
    "stamina_flat":              0.35,
    "stamina_percent":           REF * 0.35,
    "special_move_percent":      90.0,
    "special_move_flat":         0.9,
    "crit_percent":              110.0,
    "dodge_chance":              900.0,
    "counter_chance":            120.0,
    "resistance_damage_percent": 420.0,
    # Shrugging off a status is worth less per point than cutting damage:
    # it is a CHANCE at avoiding one effect, where resistance_damage_percent
    # is a certainty applied to every incoming hit.
    "resistance_status_chance":  110.0,
    "gauge_on_crit":             0.35,
    "immortal_rounds":           60.0,
    "nth_hit_interval":          0.0,       # priced through its percent
    "nth_hit_attack_percent":    REF * 1.6,
    "defence_break_min":         70.0,
    "defence_break_max":         70.0,
    "defence_break_rounds":      6.0,
    "ult_adds_attack_stat":      95.0,
    "multi_hit_extra_hits":      120.0,
    "multi_hit_power_double":    130.0,
}


def score(bonuses: dict) -> float:
    """Comparable worth of one skill's bonus slice."""
    total = 0.0
    for key, val in bonuses.items():
        w = WEIGHT.get(key)
        if w is None:
            raise SystemExit(f"no weight for bonus key {key!r} — add one")
        total += w * (1.0 if val is True else float(val))
    return round(total, 2)


def main() -> int:
    check_only = "--check" in sys.argv
    with open(DATA, encoding="utf-8") as fh:
        doc = json.load(fh)

    problems: list[str] = []
    changed = 0

    for card in doc["avatars"]:
        split = SPLIT.get(card["name"])
        if not split:
            continue
        skills = card.get("skills") or []
        names = [s["name"] for s in skills]
        if sorted(names) != sorted(split):
            problems.append(f"{card['name']}: skills {names} != split {list(split)}")
            continue

        # Every non-zero card bonus must be claimed exactly once.
        claimed: dict[str, int] = {}
        for slice_ in split.values():
            for k in slice_:
                claimed[k] = claimed.get(k, 0) + 1
        dupes = [k for k, n in claimed.items() if n > 1]
        if dupes:
            problems.append(f"{card['name']}: {dupes} claimed by more than one skill")
        # Card-level bonuses are deliberately outside the split: they belong
        # to the avatar, not to whichever skill is equipped, and bonuses_for
        # carries them through rather than zeroing them. Excluded here so the
        # partition rule keeps binding on every key that IS split.
        live = {k for k, v in card["bonuses"].items()
                if v and k not in CARD_LEVEL_BONUS_KEYS}
        missing = live - set(claimed)
        extra = set(claimed) - live
        if missing:
            problems.append(f"{card['name']}: unclaimed card bonuses {sorted(missing)}")
        if extra:
            problems.append(f"{card['name']}: skills claim absent bonuses {sorted(extra)}")

        # Attach the slice, then sort weakest → strongest so slot order is the
        # price ladder the battle system charges against.
        by_name = {s["name"]: s for s in skills}
        for name, slice_ in split.items():
            by_name[name]["bonuses"] = dict(slice_)
        ordered = sorted(skills, key=lambda s: score(s["bonuses"]))
        if [s["name"] for s in ordered] != names:
            changed += 1
        card["skills"] = ordered

        print(f"{card['name']:14}", " → ".join(
            f"{s['name']} ({score(s['bonuses']):.0f})" for s in ordered))

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print("  ", p)
        return 1

    if check_only:
        print("\ncheck only — nothing written")
        return 0

    with open(DATA, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"\nwrote {DATA}  ({changed} card(s) reordered)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
