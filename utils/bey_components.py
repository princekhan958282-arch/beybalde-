"""Component assembly and additive per-copy equipment persistence.

Inventory names remain untouched for existing acquisition/trade/level APIs.
`bey_instances` is an ordered sidecar with stable UUIDs and owned part IDs.
Legacy Rings remain frame modifiers; unknown stock components are unassigned,
not fabricated shop items. Existing custom Beys retain their old loadout path.
"""
from __future__ import annotations

import copy
from collections import Counter, defaultdict, deque
from uuid import uuid4

from .character_registry import CORE_STATS, REGISTRY


class EquipmentError(ValueError):
    pass


def _name(item):
    return str(item.get("name", "")) if isinstance(item, dict) else str(item)


def compatible(part: dict, blade: dict) -> bool:
    rules = part["compatibility"]
    for field, blade_field in (("bey_ids", "id"), ("types", "type"), ("generations", "generation")):
        allowed = rules.get(field)
        if allowed and str(blade.get(blade_field)).casefold() not in {str(x).casefold() for x in allowed}:
            return False
    return str(blade.get("id")) not in rules.get("excluded_bey_ids", [])


def reconcile(profile: dict) -> None:
    """Idempotent sidecar migration; called under the existing profile lock.

    Name-only legacy removals remove the first matching copy (list.remove's
    semantics). Transfers release parts back to the seller's inventory; a
    recipient gets a stock build. No progress/stat records are rewritten.
    """
    inventory = profile.get("inventory")
    if not isinstance(inventory, list):
        return
    names = [_name(n) for n in inventory]
    old = profile.get("bey_instances") or []
    counts = Counter(names)
    old_counts = Counter(e["name"] for e in old)
    remove = old_counts - counts
    buckets = defaultdict(deque)
    for entry in old:
        if remove[entry["name"]] > 0:
            remove[entry["name"]] -= 1
        else:
            buckets[entry["name"]].append(entry)
    entries = []
    for name in names:
        entry = buckets[name].popleft() if buckets[name] else {
            "instance_id": uuid4().hex, "name": name, "parts": {}}
        entries.append(entry)
    profile["bey_instances"] = entries
    active_name = str(profile.get("active_beyblade") or "")
    selected = next((e for e in entries if e["instance_id"] == profile.get("active_bey_instance")
                     and e["name"] == active_name), None)
    if selected is None:
        selected = next((e for e in entries if e["name"] == active_name), None)
    profile["active_bey_instance"] = selected["instance_id"] if selected else None

    if not profile.get("component_equipment_version"):
        # Only the active official copy receives the former global loadout.
        blade = REGISTRY.find_bey(active_name)
        if selected and blade:
            for name in profile.get("equipped_parts") or []:
                part = REGISTRY.part(name)
                if part and part["name"] in (profile.get("parts") or []) and compatible(part, blade):
                    selected["parts"][part["category"]] = part["id"]
        profile["component_equipment_version"] = 1

    # Selling a part or removing a Bey releases every affected reference.
    owned = {str(n).casefold() for n in profile.get("parts") or []}
    seen = set()
    for entry in entries:
        for slot, ident in list(entry["parts"].items()):
            part = REGISTRY.part(ident)
            if not part or part["name"].casefold() not in owned or ident in seen:
                del entry["parts"][slot]
            else:
                seen.add(ident)
    # Compatibility mirror for existing displays, custom Beys and snapshots.
    if REGISTRY.find_bey(active_name):
        rings = [name for name in profile.get("equipped_parts") or [] if not REGISTRY.part(name)]
        profile["equipped_parts"] = rings + [REGISTRY.part(i)["name"] for i in
                                             (selected["parts"].values() if selected else [])]


def active_instance(profile: dict, blade: dict):
    if not REGISTRY.find_bey(blade.get("id") or blade.get("name", "")):
        return None
    records = profile.get("bey_instances") or []
    name = blade.get("name")
    return next((e for e in records if e["name"] == name and
                 e["instance_id"] == profile.get("active_bey_instance")), None) or \
        next((e for e in records if e["name"] == name), None)


def select_instance(profile: dict, instance_id: str) -> dict:
    reconcile(profile)
    entry = next((e for e in profile["bey_instances"] if e["instance_id"] == instance_id), None)
    if entry is None:
        raise EquipmentError("That Bey copy is no longer in your inventory.")
    profile["active_beyblade"] = entry["name"]
    profile["active_bey_instance"] = entry["instance_id"]
    profile["active_copy"] = None
    custom = profile.get("custom_bey") or {}
    profile["active_custom_bey"] = entry["name"] == custom.get("name")
    reconcile(profile)
    return entry


def equip(profile: dict, part_name: str, *, remove: bool = False) -> dict:
    reconcile(profile)
    part = REGISTRY.part(part_name)
    if not part:
        raise EquipmentError("Unknown Disk or Driver.")
    blade = REGISTRY.find_bey(profile.get("active_beyblade", ""))
    entry = active_instance(profile, blade or {})
    if profile.get("active_copy") or not blade or not entry:
        raise EquipmentError("Equip an owned official Bey copy first.")
    if part["name"].casefold() not in {str(n).casefold() for n in profile.get("parts") or []}:
        raise EquipmentError("You do not own this part.")
    if not compatible(part, blade):
        raise EquipmentError("This part is incompatible with the equipped Main Frame.")
    slot = part["category"]
    previous = entry["parts"].get(slot)
    if remove:
        if previous != part["id"]:
            raise EquipmentError("That part is not equipped on this Bey copy.")
        del entry["parts"][slot]
    else:
        if any(e is not entry and part["id"] in e["parts"].values() for e in profile["bey_instances"]):
            raise EquipmentError("This owned part is equipped on another Bey copy. Unequip it there first.")
        entry["parts"][slot] = part["id"]
    reconcile(profile)
    return {"part": part["name"], "previous": previous, "instance_id": entry["instance_id"]}


def part_stats(profile: dict, blade: dict) -> dict:
    """Fresh disk+driver delta, never a stored/previous assembled total."""
    entry = active_instance(profile, blade)
    refs = entry["parts"] if entry else {}
    defaults = blade.get("default_parts") or {}
    stats = dict.fromkeys(CORE_STATS, 0)
    for slot in ("disk", "driver"):
        ident = refs.get(slot, defaults.get(slot))
        if ident is None:
            continue  # unknown stock component: documented zero-delta adapter
        part = REGISTRY.part(ident)
        if not part or part["category"] != slot or not compatible(part, blade):
            raise EquipmentError(f"Invalid {slot} reference: {ident}")
        for stat in CORE_STATS:
            stats[stat] += part["stats"][stat]
    return stats


def assemble(profile: dict, blade: dict) -> tuple[dict, dict]:
    """Copy and assemble the mounted frame + current disk + current driver."""
    if "component_equipment_version" not in profile:
        profile = copy.deepcopy(profile)
        reconcile(profile)
    delta = part_stats(profile, blade)
    # resolve_spin's mounted form owns the printed frame statline.
    frame = {s: blade["stats"][s] for s in CORE_STATS}
    from cogs.economy.shop import get_part_stat_deltas
    rings = [name for name in profile.get("equipped_parts") or [] if not REGISTRY.part(name)]
    frame_modifiers = get_part_stat_deltas(rings)
    for stat in CORE_STATS:
        frame[stat] += frame_modifiers.get(stat, 0)
    result = copy.deepcopy(blade)
    result["stats"] = dict(blade["stats"])
    for stat in CORE_STATS:
        result["stats"][stat] = frame[stat] + delta[stat]
    entry = active_instance(profile, blade)
    result["component_snapshot"] = {"instance_id": entry["instance_id"] if entry else None,
                                    "main_frame": frame, "parts": copy.deepcopy(entry["parts"] if entry else {}),
                                    "part_stats": delta, "legacy_frame_modifiers": frame_modifiers}
    return result, delta
