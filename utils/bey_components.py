"""Physical Disk/Driver ownership in additive profile JSON fields.

Inventory/progression stay in their legacy format. Only this module creates
component instances; reconciliation is idempotent under database's profile lock.
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


def _grant(profile, definition):
    item = {"instance_id": uuid4().hex, "definition_id": definition["id"]}
    profile.setdefault("part_instances", []).append(item)
    return item["instance_id"]


def _ledger(profile):
    result = {}
    for item in profile.get("part_instances", []):
        ident = item["instance_id"]
        if ident in result or not REGISTRY.part(item["definition_id"]):
            raise EquipmentError("Duplicate or unknown physical part record.")
        result[ident] = item
    return result


def definition_for(profile, ident):
    item = _ledger(profile).get(ident)
    return REGISTRY.part(item["definition_id"]) if item else None


def reconcile(profile: dict) -> None:
    """Grant each copy's defaults once and adapt legacy purchased equipment.

    Generic inventory consumption releases components to available inventory.
    Trades/listings instead explicitly move the copy and attached components.
    A components_granted marker survives transfers so defaults cannot respawn.
    """
    inventory = profile.get("inventory")
    if not isinstance(inventory, list):
        return
    names = [_name(n) for n in inventory]
    old = profile.get("bey_instances") or []
    remove = Counter(e["name"] for e in old) - Counter(names)
    buckets = defaultdict(deque)
    for entry in old:
        if remove[entry["name"]] > 0:
            remove[entry["name"]] -= 1
        else:
            buckets[entry["name"]].append(entry)
    entries = []
    for name in names:
        entries.append(buckets[name].popleft() if buckets[name] else {
            "instance_id": uuid4().hex, "name": name, "parts": {}})
    profile["bey_instances"] = entries
    active_name = str(profile.get("active_beyblade") or "")
    selected = next((e for e in entries if e["instance_id"] == profile.get("active_bey_instance")
                     and e["name"] == active_name), None)
    if selected is None:
        selected = next((e for e in entries if e["name"] == active_name), None)
    profile["active_bey_instance"] = selected["instance_id"] if selected else None
    legacy = profile.get("component_equipment_version", 0) < 2
    profile.setdefault("part_instances", [])
    ledger = _ledger(profile)
    # Names remain the ownership API for legacy shop parts/custom Beys only.
    owned = {str(n).casefold() for n in profile.get("parts", [])}
    for name in profile.get("parts", []):
        part = REGISTRY.part(name)
        if part and part.get("source") != "beyblade_default" and not any(
            i["definition_id"] == part["id"] for i in ledger.values()
        ):
            ident = _grant(profile, part)
            ledger[ident] = profile["part_instances"][-1]
    sold = {ident for ident, item in ledger.items() if
            REGISTRY.part(item["definition_id"]).get("source") != "beyblade_default"
            and REGISTRY.part(item["definition_id"])["name"].casefold() not in owned}
    profile["part_instances"] = [i for i in profile["part_instances"] if i["instance_id"] not in sold]
    for ident in sold:
        del ledger[ident]
    seen = set()
    for entry in entries:
        blade = REGISTRY.find_bey(entry["name"])
        if not blade:
            continue
        refs = entry.setdefault("parts", {})
        previous = dict(refs)
        if not entry.get("components_granted"):
            bundled = {}
            for slot in ("disk", "driver"):
                ident = _grant(profile, REGISTRY.part(blade["default_parts"][slot]))
                ledger[ident] = profile["part_instances"][-1]
                bundled[slot] = ident
            entry["bundled_parts"] = bundled
            entry["components_granted"] = True
            refs.update(bundled)
            if legacy:
                choices = list(previous.values())
                if not profile.get("component_equipment_version") and entry is selected:
                    choices += profile.get("equipped_parts", [])
                for value in choices:
                    part = REGISTRY.part(value)
                    if part and part["name"].casefold() in owned and compatible(part, blade):
                        ident = next((i for i, item in ledger.items() if item["definition_id"] == part["id"] and i not in seen), None)
                        if ident:
                            refs[part["category"]] = ident
        for slot in ("disk", "driver"):
            ident = refs.get(slot)
            part = REGISTRY.part(ledger[ident]["definition_id"]) if ident in ledger else None
            if ident in seen or not part or part["category"] != slot or not compatible(part, blade):
                # A sold purchased part returns the available bundled stock.
                fallback = entry.get("bundled_parts", {}).get(slot)
                if fallback in ledger and fallback not in seen and compatible(REGISTRY.part(ledger[fallback]["definition_id"]), blade):
                    refs[slot] = ident = fallback
                else:
                    raise EquipmentError(f"Invalid or multiply equipped {slot} on {entry['instance_id']}")
            seen.add(ident)
    profile["component_equipment_version"] = 2
    if REGISTRY.find_bey(active_name):
        rings = [name for name in profile.get("equipped_parts", []) if not REGISTRY.part(name)]
        profile["equipped_parts"] = rings + [REGISTRY.part(ledger[i]["definition_id"])["name"]
                                            for i in (selected["parts"].values() if selected else [])]


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
    profile["active_custom_bey"] = entry["name"] == (profile.get("custom_bey") or {}).get("name")
    reconcile(profile)
    return entry


def owned_parts(profile):
    """Physical item IDs for UI/commands; equipped state derives from slots."""
    reconcile(profile)
    equipped = {i: e["instance_id"] for e in profile["bey_instances"] for i in e["parts"].values()}
    return [dict(REGISTRY.part(i["definition_id"]), instance_id=i["instance_id"],
                 equipped_on=equipped.get(i["instance_id"])) for i in profile["part_instances"]]


def equip(profile: dict, part_name: str, *, remove: bool = False) -> dict:
    reconcile(profile)
    blade = REGISTRY.find_bey(profile.get("active_beyblade", ""))
    entry = active_instance(profile, blade or {})
    if profile.get("active_copy") or not blade or not entry:
        raise EquipmentError("Equip an owned official Bey copy first.")
    ledger = _ledger(profile)
    part = definition_for(profile, part_name) or REGISTRY.part(part_name)
    if not part:
        raise EquipmentError("Unknown Disk or Driver.")
    slot = part["category"]
    previous = entry["parts"][slot]
    if remove:
        if definition_for(profile, previous)["id"] != part["id"] or (part_name in ledger and previous != part_name):
            raise EquipmentError("That part is not equipped on this Bey copy.")
        candidate = entry.get("bundled_parts", {}).get(slot)
        if candidate == previous:
            raise EquipmentError("A Bey must always have a Disk and Driver. Select a replacement.")
        ident = candidate
    elif part_name in ledger:
        ident = part_name
    else:
        matches = [i for i, item in ledger.items() if item["definition_id"] == part["id"]]
        used = {i for e in profile["bey_instances"] for i in e["parts"].values()}
        ident = previous if previous in matches else next((i for i in matches if i not in used), matches[0] if matches else None)
    if ident not in ledger:
        raise EquipmentError("You do not own an available replacement part.")
    replacement = definition_for(profile, ident)
    if not compatible(replacement, blade):
        raise EquipmentError("This part is incompatible with the equipped Main Frame.")
    if any(e is not entry and ident in e["parts"].values() for e in profile["bey_instances"]):
        raise EquipmentError("This owned part is equipped on another Bey copy. Select a replacement there first.")
    entry["parts"][slot] = ident
    reconcile(profile)
    return {"part": replacement["name"], "previous": previous, "part_instance_id": ident,
            "instance_id": entry["instance_id"]}


def part_stats(profile: dict, blade: dict) -> dict:
    entry = active_instance(profile, blade)
    stats = dict.fromkeys(CORE_STATS, 0)
    for slot in ("disk", "driver"):
        part = definition_for(profile, entry["parts"][slot]) if entry else REGISTRY.part(blade["default_parts"][slot])
        if not part or part["category"] != slot or not compatible(part, blade):
            raise EquipmentError(f"Invalid {slot} reference")
        for stat in CORE_STATS:
            stats[stat] += part["stats"][stat]
    return stats


def assemble(profile: dict, blade: dict) -> tuple[dict, dict]:
    """Fresh frame+disk+driver; never add to a previous assembled statline."""
    if profile.get("component_equipment_version") != 2:
        profile = copy.deepcopy(profile)
        reconcile(profile)
    contribution = part_stats(profile, blade)
    canonical = REGISTRY.find_bey(blade.get("id") or blade["name"])
    # Mounted spin/form changes remain frame modifiers, preserving stock forms.
    frame = {s: blade["main_frame"][s] + blade["stats"][s] - canonical["stats"][s] for s in CORE_STATS}
    from cogs.economy.shop import get_part_stat_deltas
    rings = [name for name in profile.get("equipped_parts", []) if not REGISTRY.part(name)]
    modifiers = get_part_stat_deltas(rings)
    result = copy.deepcopy(blade)
    delta = {}
    for stat in CORE_STATS:
        frame[stat] += modifiers.get(stat, 0)
        result["stats"][stat] = frame[stat] + contribution[stat]
        # Existing level engines use full stock totals: return only replacement
        # delta (rings are applied separately by the shared loadout adapter).
        stock = sum(REGISTRY.part(blade["default_parts"][slot])["stats"][stat] for slot in ("disk", "driver"))
        delta[stat] = contribution[stat] - stock
    entry = active_instance(profile, blade)
    result["component_snapshot"] = {"instance_id": entry["instance_id"] if entry else None,
        "main_frame": frame, "parts": copy.deepcopy(entry["parts"] if entry else blade["default_parts"]),
        "part_stats": contribution, "legacy_frame_modifiers": modifiers,
        "part_names": {slot: (definition_for(profile, entry["parts"][slot]) if entry else REGISTRY.part(blade["default_parts"][slot]))["name"] for slot in ("disk", "driver")}}
    return result, delta


def detach_bey(profile, name):
    """Remove first named copy into escrow with its two attached components.

    Available components remain with the sender; purchased parts move their
    legacy ownership name too. Caller must save/transfer the returned bundle.
    """
    reconcile(profile)
    index = next((i for i, item in enumerate(profile["inventory"]) if _name(item) == name), None)
    if index is None:
        raise EquipmentError("That Bey is no longer owned.")
    item = profile["inventory"].pop(index)
    entry = profile["bey_instances"].pop(index)
    attached = set(entry["parts"].values())
    components = [i for i in profile["part_instances"] if i["instance_id"] in attached]
    profile["part_instances"] = [i for i in profile["part_instances"] if i["instance_id"] not in attached]
    for component in components:
        part = REGISTRY.part(component["definition_id"])
        if part.get("source") != "beyblade_default":
            profile["parts"].remove(part["name"])
    if profile.get("active_bey_instance") == entry["instance_id"]:
        profile["active_beyblade"] = None
        profile["active_bey_instance"] = None
    reconcile(profile)
    return {"item": item, "bey_instance": entry, "part_instances": components}


def attach_bey(profile, bundle):
    """Restore escrow/receive trade without minting components. Atomic caller."""
    reconcile(profile)
    if {i["instance_id"] for i in bundle["part_instances"]} & _ledger(profile).keys():
        raise EquipmentError("Transferred component already exists.")
    if any(e["instance_id"] == bundle["bey_instance"]["instance_id"] for e in profile["bey_instances"]):
        raise EquipmentError("Transferred Bey already exists.")
    for item in bundle["part_instances"]:
        part = REGISTRY.part(item["definition_id"])
        if part.get("source") != "beyblade_default":
            if part["name"] in profile.get("parts", []):
                raise EquipmentError("Recipient already owns that purchased part; replace it before trading.")
            profile.setdefault("parts", []).append(part["name"])
    profile["inventory"].append(copy.deepcopy(bundle["item"]))
    profile["bey_instances"].append(copy.deepcopy(bundle["bey_instance"]))
    profile["part_instances"].extend(copy.deepcopy(bundle["part_instances"]))
    reconcile(profile)
