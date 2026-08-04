"""
clan_data.py  —  storage layer for clans

Clans live in their own small JSON file. Unlike users.json this stays tiny
(a few hundred rows at most), so the atomic-write JSON pattern is still the
right tool here — no need for SQLite.

Shape:
    {
      "clans": {
        "<clan_id>": {
          "id", "name", "tag", "owner_id", "created_at",
          "members": [user_id, ...],
          "treasury": int,
          "description": str,
          "invites": [user_id, ...],
          "open": bool,
          "wins": int, "losses": int
        }
      },
      "members": { "<user_id>": "<clan_id>" }
    }
"""

import os
import re
import threading
import time
import uuid
from typing import Optional

from utils.database import BASE_DIR, _atomic_write_json, _read_json

CLANS_PATH = os.path.join(BASE_DIR, "data", "clans.json")

_lock = threading.Lock()

# ── Rules ─────────────────────────────────────────────────────────────────────
CREATE_COST   = 25_000     # a real coin sink — this is half the point
MAX_MEMBERS   = 20
NAME_MIN      = 3
NAME_MAX      = 24
TAG_MIN       = 2
TAG_MAX       = 5
NAME_RE       = re.compile(r"^[A-Za-z0-9 '\-]+$")
TAG_RE        = re.compile(r"^[A-Za-z0-9]+$")


def _blank() -> dict:
    return {"clans": {}, "members": {}}


def _load() -> dict:
    data = _read_json(CLANS_PATH, _blank)
    if not isinstance(data, dict):
        return _blank()
    data.setdefault("clans", {})
    data.setdefault("members", {})
    return data


def _save(data: dict) -> None:
    _atomic_write_json(CLANS_PATH, data)


# ── Validation ────────────────────────────────────────────────────────────────
def validate_name(name: str) -> Optional[str]:
    name = name.strip()
    if not (NAME_MIN <= len(name) <= NAME_MAX):
        return f"Name must be {NAME_MIN}-{NAME_MAX} characters."
    if not NAME_RE.match(name):
        return "Name can only use letters, numbers, spaces, apostrophes and hyphens."
    return None


def validate_tag(tag: str) -> Optional[str]:
    if not (TAG_MIN <= len(tag) <= TAG_MAX):
        return f"Tag must be {TAG_MIN}-{TAG_MAX} characters."
    if not TAG_RE.match(tag):
        return "Tag must be letters and numbers only."
    return None


# ── Reads ─────────────────────────────────────────────────────────────────────
def get_clan(clan_id: str) -> Optional[dict]:
    return _load()["clans"].get(clan_id)


def clan_of(user_id: int) -> Optional[dict]:
    data = _load()
    cid  = data["members"].get(str(user_id))
    return data["clans"].get(cid) if cid else None


def find_by_name(query: str) -> Optional[dict]:
    q = query.strip().lower()
    for c in _load()["clans"].values():
        if c["name"].lower() == q or c["tag"].lower() == q:
            return c
    return None


def all_clans() -> list[dict]:
    return list(_load()["clans"].values())


def name_taken(name: str, tag: str) -> Optional[str]:
    n, t = name.strip().lower(), tag.strip().lower()
    for c in _load()["clans"].values():
        if c["name"].lower() == n:
            return "A clan with that name already exists."
        if c["tag"].lower() == t:
            return "A clan with that tag already exists."
    return None


# ── Writes ────────────────────────────────────────────────────────────────────
def create_clan(owner_id: int, name: str, tag: str) -> dict:
    with _lock:
        data = _load()
        clan = {
            "id":          uuid.uuid4().hex[:12],
            "name":        name.strip(),
            "tag":         tag.strip().upper(),
            "owner_id":    owner_id,
            "created_at":  time.time(),
            "members":     [owner_id],
            "treasury":    0,
            "description": "",
            "invites":     [],
            "open":        True,
            "wins":        0,
            "losses":      0,
        }
        data["clans"][clan["id"]]      = clan
        data["members"][str(owner_id)] = clan["id"]
        _save(data)
        return clan


def add_member(clan_id: str, user_id: int) -> tuple[bool, str]:
    with _lock:
        data = _load()
        clan = data["clans"].get(clan_id)
        if clan is None:
            return False, "That clan no longer exists."
        if str(user_id) in data["members"]:
            return False, "You're already in a clan — leave it first."
        if len(clan["members"]) >= MAX_MEMBERS:
            return False, f"That clan is full ({MAX_MEMBERS} members)."
        if not clan.get("open", True) and user_id not in clan.get("invites", []):
            return False, "That clan is invite-only."

        clan["members"].append(user_id)
        clan["invites"] = [i for i in clan.get("invites", []) if i != user_id]
        data["members"][str(user_id)] = clan_id
        _save(data)
        return True, f"Joined **{clan['name']}**!"


def remove_member(user_id: int) -> tuple[bool, str, Optional[dict]]:
    """Leave a clan. If the owner leaves, ownership passes to the next member;
    if they were the last member the clan is disbanded and its treasury is lost."""
    with _lock:
        data = _load()
        cid  = data["members"].get(str(user_id))
        if not cid:
            return False, "You're not in a clan.", None
        clan = data["clans"].get(cid)
        if clan is None:
            data["members"].pop(str(user_id), None)
            _save(data)
            return False, "That clan no longer exists.", None

        clan["members"] = [m for m in clan["members"] if m != user_id]
        data["members"].pop(str(user_id), None)

        if not clan["members"]:
            data["clans"].pop(cid, None)
            _save(data)
            return True, f"You left **{clan['name']}** — it had no one else, so it's disbanded.", clan

        if clan["owner_id"] == user_id:
            clan["owner_id"] = clan["members"][0]
            _save(data)
            return True, (f"You left **{clan['name']}**. Ownership passed to "
                          f"<@{clan['owner_id']}>."), clan

        _save(data)
        return True, f"You left **{clan['name']}**.", clan


def kick_member(clan_id: str, actor_id: int, target_id: int) -> tuple[bool, str]:
    with _lock:
        data = _load()
        clan = data["clans"].get(clan_id)
        if clan is None:
            return False, "That clan no longer exists."
        if clan["owner_id"] != actor_id:
            return False, "Only the clan owner can kick."
        if target_id == actor_id:
            return False, "Use `;clan leave` to leave your own clan."
        if target_id not in clan["members"]:
            return False, "They're not in your clan."

        clan["members"] = [m for m in clan["members"] if m != target_id]
        data["members"].pop(str(target_id), None)
        _save(data)
        return True, f"Kicked <@{target_id}> from **{clan['name']}**."


def invite_member(clan_id: str, actor_id: int, target_id: int) -> tuple[bool, str]:
    with _lock:
        data = _load()
        clan = data["clans"].get(clan_id)
        if clan is None:
            return False, "That clan no longer exists."
        if actor_id not in clan["members"]:
            return False, "You're not in that clan."
        if str(target_id) in data["members"]:
            return False, "They're already in a clan."
        if target_id in clan.get("invites", []):
            return False, "They already have a pending invite."
        clan.setdefault("invites", []).append(target_id)
        _save(data)
        return True, f"Invited <@{target_id}> to **{clan['name']}**."


def update_treasury(clan_id: str, delta: int) -> tuple[bool, int]:
    """Returns (ok, new_balance). Fails if a withdrawal would go negative."""
    with _lock:
        data = _load()
        clan = data["clans"].get(clan_id)
        if clan is None:
            return False, 0
        new = clan.get("treasury", 0) + delta
        if new < 0:
            return False, clan.get("treasury", 0)
        clan["treasury"] = new
        _save(data)
        return True, new


def set_field(clan_id: str, field: str, value) -> bool:
    allowed = {"description", "open", "wins", "losses"}
    if field not in allowed:
        return False
    with _lock:
        data = _load()
        clan = data["clans"].get(clan_id)
        if clan is None:
            return False
        clan[field] = value
        _save(data)
        return True
