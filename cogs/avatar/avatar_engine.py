"""
avatar_engine.py
----------------
Core avatar logic.

Responsibilities:
  - Load and cache avatar_data.json
  - Manage equip/unequip per player
  - Cache bonuses at battle start
  - Expose a single clean API to the combat engine

The combat engine (session.py) should ONLY call:
  - AvatarEngine.get_battle_bonuses(player_id)  → called once at battle start
  - AvatarBonuses (dataclass)                   → used throughout the fight

No Beyblade logic lives here. No battle state is modified here.
"""

from __future__ import annotations

import json
import os
import random
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

from .avatar_utils import validate_avatar_data

# JSON-based database helpers (Beybot uses flat-file storage, not SQL)
from utils.database import (
    get_avatar_inventory,
    add_avatar_to_inventory,
    player_owns_avatar,
    get_equipped_avatar,
    set_equipped_avatar,
)

# ── Path to data file ─────────────────────────────────────────────────────────

_DATA_PATH = os.path.join(os.path.dirname(__file__), "avatar_data.json")


# ── Bonus dataclass (what session.py receives) ────────────────────────────────

@dataclass
class AvatarBonuses:
    """
    A flat snapshot of all avatar bonuses for one battle.
    Created once at battle start and cached for the entire fight.
    All values are additive on top of base Bey stats.
    """
    # Attack
    attack_flat:    float = 0.0
    attack_percent: float = 0.0   # e.g. 0.10 = +10%

    # Defence
    defence_flat:    float = 0.0
    defence_percent: float = 0.0

    # Stamina
    stamina_flat:    float = 0.0
    stamina_percent: float = 0.0

    # Stability
    stability_flat:    float = 0.0
    stability_percent: float = 0.0

    # Charge
    charge_flat:    float = 0.0
    charge_percent: float = 0.0

    # Special Move
    special_move_flat:    float = 0.0
    special_move_percent: float = 0.0

    # Crit
    crit_percent: float = 0.0     # added to existing crit chance

    # Dodge (new mechanic — avatar introduces this)
    dodge_chance: float = 0.0     # 0.0–1.0 probability per incoming hit

    # Counter (new mechanic — avatar introduces this)
    counter_chance: float = 0.0   # 0.0–1.0 probability on dodge success

    # Multi-hit modifiers
    multi_hit_power_double: bool = False  # doubles damage on each hit
    multi_hit_extra_hits:   bool = False  # adds one extra hit per multi-hit move

    # HP
    hp_flat:    float = 0.0   # bonus HP added at battle start (e.g. +50 HP)
    hp_percent: float = 0.0   # percent bonus to starting HP (e.g. 0.10 = +10%)

    # Resistance
    resistance_damage_percent: float = 0.0   # reduces all incoming damage
    resistance_status_chance:  float = 0.0   # chance to resist status effects

    # ── Signature skills ──────────────────────────────────────────────────────
    # The flat bonus fields above cannot express effects with their own timing
    # or trigger, so these carry the extras the Exclusive avatars need. All
    # default to "off" so every existing avatar is unaffected.

    # Argus — a crit refunds this much Special Gauge.
    gauge_on_crit: float = 0.0

    # Argus — cannot be reduced below 1 HP for this many rounds, once per
    # battle, triggered the first time a hit would be lethal.
    immortal_rounds: int = 0

    # Dyrroth — every Nth hit lands bonus damage worth this fraction of the
    # wielder's total attack.
    nth_hit_interval: int = 0
    nth_hit_attack_percent: float = 0.0

    # Dyrroth — ignores this fraction of enemy defence (rolled between min and
    # max each hit) while the round number is <= defence_break_rounds.
    defence_break_min: float = 0.0
    defence_break_max: float = 0.0
    defence_break_rounds: int = 0

    # Dyrroth — the wielder's total attack stat is added to Special Move damage
    # on top of special_move_percent.
    ult_adds_attack_stat: bool = False

    # ── Convenience methods called by combat engine ───────────────────────────

    def roll_defence_break(self, round_no: int) -> float:
        """Fraction of enemy defence ignored this hit (0.0 when inactive)."""
        if self.defence_break_rounds <= 0 or int(round_no) > self.defence_break_rounds:
            return 0.0
        lo, hi = self.defence_break_min, max(self.defence_break_min,
                                             self.defence_break_max)
        if hi <= 0.0:
            return 0.0
        return random.uniform(lo, hi)

    def nth_hit_bonus(self, hit_count: int, total_attack: float) -> int:
        """Bonus damage for landing the Nth hit (0 when it is not the Nth)."""
        if self.nth_hit_interval <= 0 or self.nth_hit_attack_percent <= 0.0:
            return 0
        if int(hit_count) <= 0 or int(hit_count) % self.nth_hit_interval != 0:
            return 0
        return int(round(float(total_attack) * self.nth_hit_attack_percent))

    def apply_attack_bonus(self, base_attack: float) -> float:
        """Return attack after flat + percent bonuses."""
        return (base_attack + self.attack_flat) * (1.0 + self.attack_percent)

    def apply_defence_bonus(self, base_defence: float) -> float:
        return (base_defence + self.defence_flat) * (1.0 + self.defence_percent)

    def apply_stamina_bonus(self, base_stamina: float) -> float:
        return (base_stamina + self.stamina_flat) * (1.0 + self.stamina_percent)

    def apply_stability_bonus(self, base_stability: float) -> float:
        return (base_stability + self.stability_flat) * (1.0 + self.stability_percent)

    def apply_charge_bonus(self, base_charge: float) -> float:
        return (base_charge + self.charge_flat) * (1.0 + self.charge_percent)

    def apply_special_move_bonus(self, base_damage: float) -> float:
        return (base_damage + self.special_move_flat) * (1.0 + self.special_move_percent)

    def apply_hp_bonus(self, base_hp: float) -> float:
        """Return starting HP after flat + percent bonuses."""
        return (base_hp + self.hp_flat) * (1.0 + self.hp_percent)

    def apply_damage_resistance(self, incoming_damage: float) -> float:
        """Reduce incoming damage by resistance percent."""
        return incoming_damage * (1.0 - self.resistance_damage_percent)

    def apply_multi_hit_damage(self, hit_damage: float) -> float:
        """Double damage per hit if multi_hit_power_double is active."""
        if self.multi_hit_power_double:
            return hit_damage * 2.0
        return hit_damage

    def extra_hits(self, base_hits: int) -> int:
        """Return adjusted hit count if multi_hit_extra_hits is active."""
        if self.multi_hit_extra_hits:
            return base_hits + 1
        return base_hits

    def roll_dodge(self) -> bool:
        """Roll whether this avatar dodges an incoming attack."""
        if self.dodge_chance <= 0.0:
            return False
        return random.random() < self.dodge_chance

    def roll_counter(self) -> bool:
        """Roll whether a dodge triggers a counter-attack."""
        if self.counter_chance <= 0.0:
            return False
        return random.random() < self.counter_chance

    def roll_status_resist(self) -> bool:
        """Roll whether this avatar resists an incoming status effect."""
        if self.resistance_status_chance <= 0.0:
            return False
        return random.random() < self.resistance_status_chance

    @property
    def has_any_bonus(self) -> bool:
        return any([
            self.attack_flat, self.attack_percent,
            self.defence_flat, self.defence_percent,
            self.stamina_flat, self.stamina_percent,
            self.stability_flat, self.stability_percent,
            self.charge_flat, self.charge_percent,
            self.special_move_flat, self.special_move_percent,
            self.hp_flat, self.hp_percent,
            self.crit_percent,
            self.dodge_chance, self.counter_chance,
            self.multi_hit_power_double, self.multi_hit_extra_hits,
            self.resistance_damage_percent, self.resistance_status_chance,
            self.gauge_on_crit, self.immortal_rounds,
            self.nth_hit_interval, self.defence_break_rounds,
            self.ult_adds_attack_stat,
        ])


# ── Null bonus (used when player has no avatar equipped) ─────────────────────

NULL_BONUSES = AvatarBonuses()


# ── Avatar Engine ─────────────────────────────────────────────────────────────

class AvatarEngine:
    """
    Manages avatar data and per-player equip state.

    Usage in session.py:
        bonuses = avatar_engine.get_battle_bonuses(player_id)
        # Store bonuses on the session object for the whole fight.

    Usage in db layer:
        avatar_engine.equip(player_id, avatar_id)
        avatar_engine.unequip(player_id)
    """

    def __init__(self) -> None:
        self._avatars: dict[str, dict] = {}   # id → avatar dict
        self._loaded: bool = False

    # ── Data loading ──────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load and validate avatar_data.json. Call once at bot startup."""
        with open(_DATA_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)

        self._avatars = {}
        errors = []
        for entry in raw.get("avatars", []):
            ok, msg = validate_avatar_data(entry)
            if not ok:
                errors.append(f"  [{entry.get('id', '?')}] {msg}")
                continue
            self._avatars[entry["id"]] = entry

        if errors:
            raise ValueError(
                f"avatar_data.json has {len(errors)} invalid entries:\n" + "\n".join(errors)
            )

        self._loaded = True

    def reload(self) -> None:
        """Hot-reload avatar data without restarting the bot."""
        self._loaded = False
        self.load()

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ── Public data accessors ─────────────────────────────────────────────────

    def get_avatar(self, avatar_id: str) -> Optional[dict]:
        self._ensure_loaded()
        return self._avatars.get(avatar_id)

    def get_all_avatars(self) -> list[dict]:
        self._ensure_loaded()
        return list(self._avatars.values())

    def avatar_exists(self, avatar_id: str) -> bool:
        self._ensure_loaded()
        return avatar_id in self._avatars

    # ── Equip management (requires db interaction from caller) ────────────────

    def equip(self, player_id: int, avatar_id: str) -> tuple[bool, str]:
        """
        Equip an avatar for a player.
        Returns (success, message).
        """
        self._ensure_loaded()

        if not self.avatar_exists(avatar_id):
            return False, f"Avatar `{avatar_id}` does not exist."

        if not player_owns_avatar(player_id, avatar_id):
            return False, "You don't own this avatar."

        set_equipped_avatar(player_id, avatar_id)
        avatar = self._avatars[avatar_id]
        return True, f"Equipped **{avatar['name']}**!"

    def unequip(self, player_id: int) -> tuple[bool, str]:
        """Remove equipped avatar for a player."""
        set_equipped_avatar(player_id, None)
        return True, "Avatar unequipped."

    def get_equipped_avatar_id(self, player_id: int) -> Optional[str]:
        return get_equipped_avatar(player_id)

    # ── Battle API (the only method session.py needs to call) ─────────────────

    def get_battle_bonuses(self, player_id: int) -> AvatarBonuses:
        """
        Called ONCE at battle start by session.py.
        Returns an AvatarBonuses snapshot for the entire fight.
        If no avatar is equipped, returns NULL_BONUSES (no effect).
        """
        avatar_id = self.get_equipped_avatar_id(player_id)
        if not avatar_id:
            return NULL_BONUSES

        avatar = self.get_avatar(avatar_id)
        if not avatar:
            return NULL_BONUSES

        b = avatar["bonuses"]
        return AvatarBonuses(
            attack_flat=b.get("attack_flat", 0.0),
            attack_percent=b.get("attack_percent", 0.0),
            defence_flat=b.get("defence_flat", 0.0),
            defence_percent=b.get("defence_percent", 0.0),
            stamina_flat=b.get("stamina_flat", 0.0),
            stamina_percent=b.get("stamina_percent", 0.0),
            stability_flat=b.get("stability_flat", 0.0),
            stability_percent=b.get("stability_percent", 0.0),
            charge_flat=b.get("charge_flat", 0.0),
            charge_percent=b.get("charge_percent", 0.0),
            special_move_flat=b.get("special_move_flat", 0.0),
            special_move_percent=b.get("special_move_percent", 0.0),
            hp_flat=b.get("hp_flat", 0.0),
            hp_percent=b.get("hp_percent", 0.0),
            crit_percent=b.get("crit_percent", 0.0),
            dodge_chance=b.get("dodge_chance", 0.0),
            counter_chance=b.get("counter_chance", 0.0),
            multi_hit_power_double=b.get("multi_hit_power_double", False),
            multi_hit_extra_hits=b.get("multi_hit_extra_hits", False),
            resistance_damage_percent=b.get("resistance_damage_percent", 0.0),
            resistance_status_chance=b.get("resistance_status_chance", 0.0),
            # Signature skills — absent on every pre-existing avatar, so the
            # defaults keep them switched off.
            gauge_on_crit=b.get("gauge_on_crit", 0.0),
            immortal_rounds=int(b.get("immortal_rounds", 0) or 0),
            nth_hit_interval=int(b.get("nth_hit_interval", 0) or 0),
            nth_hit_attack_percent=b.get("nth_hit_attack_percent", 0.0),
            defence_break_min=b.get("defence_break_min", 0.0),
            defence_break_max=b.get("defence_break_max", 0.0),
            defence_break_rounds=int(b.get("defence_break_rounds", 0) or 0),
            ult_adds_attack_stat=bool(b.get("ult_adds_attack_stat", False)),
        )


# ── Limited-time banner availability ─────────────────────────────────────────

def avatar_is_available(avatar: dict, now: "datetime | None" = None) -> bool:
    """Is this avatar currently pullable?

    Non-limited avatars are always available. A limited one stays available
    until ``available_until`` (ISO-8601) passes; leave that field null to run
    the banner open-ended and set a date when you want it to close.
    """
    if not avatar.get("limited"):
        return True
    until = avatar.get("available_until")
    if not until:
        return True
    try:
        end = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
    except ValueError:
        return True
    now = now or datetime.now(end.tzinfo)
    return now <= end


# ── Singleton (import this everywhere) ───────────────────────────────────────

avatar_engine = AvatarEngine()
