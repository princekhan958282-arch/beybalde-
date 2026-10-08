"""Resolve display names without changing equipment or player persistence."""
from __future__ import annotations


def card_parts(blade: dict, *, profile: dict | None = None, parts: dict | None = None) -> dict:
    from .character_registry import REGISTRY
    result = {}
    aliases = {"disk": "ratchet", "driver": "bit"}
    for slot, alias in aliases.items():
        default = (blade.get("default_parts") or {}).get(slot)
        definition = REGISTRY.part(default) if default else None
        result[slot] = (definition["name"] if definition else blade.get(slot) or blade.get(alias))
    if profile is not None:
        from .bey_components import active_instance, definition_for
        entry = active_instance(profile, blade)
        if entry:
            for slot in aliases:
                definition = definition_for(profile, entry["parts"].get(slot))
                if definition:
                    result[slot] = definition["name"]
        elif not REGISTRY.find_bey(blade.get("id") or blade.get("name", "")):
            # Custom Beys keep their existing global purchased equipment path.
            for name in profile.get("equipped_parts", []):
                definition = REGISTRY.part(name)
                if definition:
                    result[definition["category"]] = definition["name"]
    snapshot = (blade.get("component_snapshot") or {}).get("part_names") or {}
    for slot, alias in aliases.items():
        value = (parts or {}).get(slot) or (parts or {}).get(alias) or snapshot.get(slot)
        if value:
            result[slot] = str(value)
    return result


def parts_slots(blade: dict, parts: dict | None = None) -> list[dict]:
    names = card_parts(blade, parts=parts)
    return [{"slot": "BLADE", "value": blade.get("blade_part") or blade.get("name") or "—"},
            {"slot": "DISK", "value": names.get("disk") or "—"},
            {"slot": "DRIVER", "value": names.get("driver") or "—"}]
