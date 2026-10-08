"""Validated flat-file authored content. Cached until explicit reload/restart.

The public dictionaries retain the old name/ID interfaces. No player-created
records belong here. A reload builds a complete replacement before publication.
"""
from __future__ import annotations

import copy
import json
import math
import re
import threading
import io
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_STATS = ("hp", "attack", "defense", "stamina")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")


def validate_stats(stats, where: str) -> None:
    if not isinstance(stats, dict):
        raise ValueError(f"{where}: expected a stats object")
    for key in CORE_STATS:
        value = stats.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{where}: missing/invalid {key}")


class CharacterRegistry:
    def __init__(self, root: Path = ROOT):
        self.root = Path(root)
        self._cache = {}
        self._indexes = {}
        self._lock = threading.RLock()

    def _read(self, folder: str, kind: str) -> dict:
        directory = self.root / folder
        files = sorted(directory.glob("*.json"))
        if not files:
            raise ValueError(f"{directory}: no {kind} definitions")
        if kind in ("bey", "avatar") and any(p.is_dir() for p in directory.iterdir()):
            raise ValueError(f"{directory}: character subfolders are forbidden")
        result, ids, names = {}, set(), set()
        for path in files:
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(entry, dict):
                    raise ValueError("expected an object")
                for field in ("id", "name"):
                    if not isinstance(entry.get(field), str) or not entry[field].strip():
                        raise ValueError(f"missing/invalid {field}")
                ident, name = entry["id"].casefold(), entry["name"].casefold()
                if ident in ids or name in names:
                    raise ValueError(f"duplicate ID or name: {entry['id']} / {entry['name']}")
                ids.add(ident)
                names.add(name)
                if not isinstance(entry.get("aliases", []), list) or any(
                    not isinstance(alias, str) or not alias.strip() for alias in entry.get("aliases", [])
                ):
                    raise ValueError("aliases must be nonempty strings")
                if kind == "bey":
                    for field in ("type", "rarity", "generation", "stats", "main_frame", "default_parts"):
                        if field not in entry:
                            raise ValueError(f"missing {field}")
                    validate_stats(entry["main_frame"], "main_frame")
                    validate_stats(entry["stats"], "stats")
                    if set(entry["default_parts"]) != {"disk", "driver"}:
                        raise ValueError("default_parts requires disk and driver slots")
                elif kind == "avatar":
                    for field in ("rarity", "price", "description", "bonuses", "generation"):
                        if field not in entry:
                            raise ValueError(f"missing {field}")
                else:
                    if entry.get("category") != kind or entry.get("type") != kind:
                        raise ValueError(f"expected {kind} category")
                    validate_stats(entry.get("stats"), "stats")
                    if not isinstance(entry.get("compatibility"), dict):
                        raise ValueError("missing compatibility rules")
                    if entry.get("source") == "beyblade_default":
                        if entry.get("shop_available") is not False or entry.get("tradable") is not False or "price" in entry:
                            raise ValueError("bundled defaults cannot have a price or be purchasable/tradable")
                    elif entry.get("price", -1) < 0:
                        raise ValueError("invalid price")
                result[entry["name"] if kind == "bey" else entry["id"]] = entry
            except (ValueError, TypeError, KeyError, OSError) as exc:
                raise ValueError(f"{path}: {exc}") from exc
        return dict(sorted(result.items(), key=lambda item: item[1].get("_registry_order", 0)))

    def load(self, kind: str, reload: bool = False) -> dict:
        folders = {"bey": "beys", "avatar": "avatars", "disk": "parts/disks", "driver": "parts/drivers"}
        with self._lock:
            if reload or kind not in self._cache:
                candidate = self._read(folders[kind], kind)
                index = {}
                for entry in candidate.values():
                    for alias in (entry["id"], entry["name"], *entry.get("aliases", [])):
                        key = alias.casefold()
                        if key in index and index[key] is not entry:
                            raise ValueError(f"{folders[kind]}: ambiguous ID/name/alias {alias}")
                        index[key] = entry
                if kind == "bey":
                    disks, drivers = self.load("disk"), self.load("driver")
                    for entry in candidate.values():
                        for slot, parts in (("disk", disks), ("driver", drivers)):
                            ref = entry["default_parts"][slot]
                            if ref not in parts:
                                raise ValueError(f"{entry['id']}: unknown default {slot} {ref}")
                        for stat in CORE_STATS:
                            total = entry["main_frame"][stat] + disks[entry["default_parts"]["disk"]]["stats"][stat] + drivers[entry["default_parts"]["driver"]]["stats"][stat]
                            if total != entry["stats"][stat]:
                                raise ValueError(f"{entry['id']}: default components do not preserve {stat}")
                self._cache[kind] = candidate
                self._indexes[kind] = index
            return self._cache[kind]

    def find_bey(self, value: str):
        self.load("bey")
        return self._indexes["bey"].get(str(value).casefold())

    def part(self, value: str):
        value = str(value).casefold()
        matches = []
        for kind in ("disk", "driver"):
            self.load(kind)
            if value in self._indexes[kind]:
                matches.append(self._indexes[kind][value])
        if len(matches) > 1:
            raise ValueError(f"ambiguous part ID/name/alias {value}")
        return matches[0] if matches else None


REGISTRY = CharacterRegistry()


def load_beys():
    return REGISTRY.load("bey")


def load_avatars():
    return {"avatars": list(REGISTRY.load("avatar").values())}


def load_parts(*, shop_only=False):
    REGISTRY.load("driver")
    REGISTRY.load("disk")
    if REGISTRY._indexes["driver"].keys() & REGISTRY._indexes["disk"].keys():
        raise ValueError("duplicate part IDs/names across Disk and Driver catalogues")
    return [copy.deepcopy(p) for kind in ("driver", "disk") for p in REGISTRY.load(kind).values()
            if not shop_only or p.get("shop_available", True)]


class _AuthoredStream(io.StringIO):
    """Compatibility stream for repository maintenance/simulation scripts.

    There is no second on-disk catalogue. Writes validate the complete proposed
    catalogue before touching any file, and never delete existing characters.
    """
    def __init__(self, kind, mode):
        self.kind, self.mode = kind, mode
        document = load_beys() if kind == "bey" else load_avatars()
        super().__init__(json.dumps(document, ensure_ascii=False) if mode == "r" else "")

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None and self.mode == "w":
            document = json.loads(self.getvalue())
            records = document.values() if self.kind == "bey" else document["avatars"]
            folder = ROOT / ("beys" if self.kind == "bey" else "avatars")
            staged, ids, filenames = [], set(), set()
            current = REGISTRY.load(self.kind)
            for order, value in enumerate(records):
                entry = copy.deepcopy(value)
                ident = entry["id"]
                filename = slug(entry["name"]) + ".json"
                if ident in ids or filename in filenames:
                    raise ValueError("duplicate ID or filename in authored update")
                ids.add(ident)
                filenames.add(filename)
                entry.setdefault("generation", None)
                entry.setdefault("_registry_order", order)
                if self.kind == "bey":
                    validate_stats(entry["stats"], entry["name"])
                    existing = next((v for v in current.values() if v["id"] == ident), None)
                    if not existing or any(existing["stats"][s] != entry["stats"][s] for s in CORE_STATS):
                        raise ValueError("new Beys/base-stat changes require explicit component definition updates")
                    entry["main_frame"] = copy.deepcopy(existing["main_frame"])
                    entry["default_parts"] = copy.deepcopy(existing["default_parts"])
                    entry["component_migration"] = copy.deepcopy(existing["component_migration"])
                elif not all(k in entry for k in ("price", "bonuses", "rarity", "description")):
                    raise ValueError("incomplete Avatar definition")
                staged.append((folder / filename, entry))
            if not {v["id"] for v in current.values()} <= ids:
                raise ValueError("authored update cannot silently remove characters")
            for path, entry in staged:
                if path.exists():
                    old = json.loads(path.read_text())
                    if old["id"] != entry["id"]:
                        raise ValueError(f"filename collision at {path}")
                existing = next((v for v in current.values() if v["id"] == entry["id"]), None)
                if existing and existing["name"] != entry["name"]:
                    raise ValueError("rename requires an explicit filename migration")
            for path, entry in staged:
                temp = path.with_suffix(".json.tmp")
                temp.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                os.replace(temp, path)
            REGISTRY.load(self.kind, reload=True)
        return super().__exit__(exc_type, exc, tb)


def authored_open(kind, mode="r", **_kwargs):
    if kind not in ("bey", "avatar") or mode not in ("r", "w"):
        raise ValueError("authored_open supports Bey/Avatar text reads and writes only")
    return _AuthoredStream(kind, mode)
