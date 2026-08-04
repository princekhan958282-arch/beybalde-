"""
battle/stamina_manager.py
-------------------------
Dedicated Stamina Manager.

Centralises all stamina cost deduction, passive regen, action recovery,
and the Special Gauge. No damage or ability logic lives here.

Action Stamina Costs
--------------------
  Attack  = 1.5
  Defense = 1.5
  Stamina = 0   (the action earns stamina instead)
  Charge  = 1.0 (fills Special Gauge by +50)
  Special = 3.0

Special Gauge
-------------
The gauge fills during battle:
  • Attacking       → +GAUGE_PER_ATTACK
  • Defending       → +GAUGE_PER_DEFENSE
  • Using Stamina   → +GAUGE_PER_STAMINA
  • Taking damage   → +GAUGE_PER_DMG_TAKEN
  • Using Charge    → +GAUGE_PER_CHARGE (50 flat)

Special is unlocked at gauge >= SPECIAL_GAUGE_MAX (150).
Using Special resets the gauge to 0.
"""

import math
import random
from .constants import (
    BASE_HP,
    MOVE_ATTACK, MOVE_DEFENSE, MOVE_STAMINA, MOVE_SPECIAL, MOVE_CHARGE,
    SPECIAL_GAUGE_MAX,
    GAUGE_PER_ATTACK, GAUGE_PER_DEFENSE, GAUGE_PER_STAMINA, GAUGE_PER_DMG_TAKEN, GAUGE_PER_CHARGE,
)


# ── Per-action stamina costs ──────────────────────────────────────────────────
# Raised alongside the per-bey bar below. Bars now run ~13 at a low stamina stat
# to ~26 at a maxed one, where they used to be a flat 15 for everybody; leaving
# costs at the old 1.5/3.0 would have meant nobody ever runs out and the whole
# stamina economy quietly stops existing. These are scaled so a BASELINE bey
# gets about the same number of actions before exhaustion as it does today,
# while a stamina blade genuinely gets more out of its stat.
STAMINA_COST: dict[str, float] = {
    MOVE_ATTACK:  2.2,
    MOVE_DEFENSE: 2.2,
    MOVE_STAMINA: 0.0,   # the action earns stamina instead
    MOVE_CHARGE:  1.5,
    MOVE_SPECIAL: 4.4,
}

# ── Maximum battle stamina ────────────────────────────────────────────────────
# THE BUG THIS REPLACES: this used to be a flat 15.0 for every blade, and
# starting stamina was `3 + stat * 0.05` clamped to it — so any bey with a
# stamina stat of 240 or more started at exactly 15. At level 100 nearly every
# bey is over 240, which meant every maxed bey had an identical stamina bar and
# the stamina stat stopped paying entirely partway up the level curve.
#
# The bar is now derived per bey, so the stat keeps mattering all the way to
# level 100 and a stamina blade visibly out-lasts an attack blade.
STAMINA_MAX_BASE     = 11.0   # bar for a bey with no stamina stat at all
STAMINA_MAX_PER_STAT = 0.05   # +1 bar per 20 stat
STAMINA_MAX_CEILING  = 30.0   # hard stop, so no future stat curve runs away

# Kept as the legacy name at its legacy value. Nothing in the manager uses it
# any more — it is the fallback for display code that has no per-player bar to
# read, and boss_ai keeps its own copy for boss fighters.
STAMINA_MAX = 15.0


def max_stamina_for(sta_stat: float) -> float:
    """The battle stamina ceiling for a bey with this stamina stat."""
    raw = STAMINA_MAX_BASE + max(0.0, float(sta_stat)) * STAMINA_MAX_PER_STAT
    return round(min(STAMINA_MAX_CEILING, raw), 2)

# ── Stamina action heal tuning ────────────────────────────────────────────────
# Base heal = ceil(sta_stat × STAMINA_HEAL_RATIO)
# Tuned to 0.7 (Stamina System Update):
# At STA 80:  56 HP base
# At STA 100: 70 HP base
# At STA 120: 84 HP base
# Low HP bonus (below 40% HP) scales up to ×1.2 on top of this.
# If the opponent ATTACKS (or uses Special) during your Stamina move,
# the final heal is cut by 50% (interrupted heal).
STAMINA_HEAL_RATIO = 0.7
STAMINA_HEAL_MIN   = 20    # floor so even low-STA blades always get something
STAMINA_HEAL_INTERRUPT_MULT = 0.5  # heal multiplier when attacked mid-heal

# ── Stamina action recovery tuning ────────────────────────────────────────────
# Stamina restored by the Stamina move. This used to be a flat +3 for everyone,
# with a comment claiming "stat scaling lives in starting stamina" — which was
# exactly the part that got clamped to 15 and stopped scaling. It scales here
# now, so a stamina blade recovers faster as well as holding more.
# Stamina-type bonus (+1) stacks on top when type advantage is active.
STAMINA_RECOVERY_BASE     = 3.0
STAMINA_RECOVERY_PER_STAT = 0.012   # +1.2 recovery per 100 stat

# ── Passive regen tuning ──────────────────────────────────────────────────────
# A small per-round trickle so a bey is never permanently stranded at zero.
# Deliberately far below the Stamina move's recovery: this is a floor against
# lockout, not an alternative to actually spending a turn recovering.
STAMINA_REGEN_BASE     = 0.15
STAMINA_REGEN_PER_STAT = 0.004      # +0.4 per 100 stat

# ── Starting stamina tuning ───────────────────────────────────────────────────
# Battle-start stamina = START_BASE + (sta_stat × START_PER_STAT), clamped to
# the bey's OWN ceiling (max_stamina_for) rather than a global 15.
# Continuous per-point scaling: 1 stat = 0.05 stamina, so 20 stat = +1.
# At STA 1: 3.05 | STA 100: 8 | STA 240: 15 | STA 400: 23 — no longer flat-topped.
STAMINA_START_BASE     = 3
STAMINA_START_PER_STAT = 0.05


class StaminaManager:
    """Manages stamina and the special gauge for both players in a BattleSession."""

    def __init__(self, blades: dict[str, dict], effective_stats: dict[str, dict] | None = None):
        self._blades = blades
        # Modified stats (base + parts + avatar + level mult) supplied by the
        # session. When present these override raw blade["stats"] for all
        # stamina-stat reads so boosted blades get their real values.
        self._eff: dict[str, dict] = effective_stats or {}
        # Per-player max stamina, derived from each bey's own stamina stat.
        # Must be built BEFORE self.stamina: starting stamina clamps to it.
        self.max_stamina: dict[str, float] = {
            key: max_stamina_for(self._sta_stat(key)) for key in blades
        }
        self.stamina: dict[str, float] = {
            key: self._initial_stamina(self._sta_stat(key), self.max_stamina[key])
            for key in blades
        }
        # Special Gauge (0–150), keyed by player ID string
        self.gauge: dict[str, int] = {key: 0 for key in blades}
        # Stamina-loss resistance (0–0.9), set by abilities via the
        # `stamina_cost_reduction` op at battle setup. Applied to per-move
        # costs here and to enemy drains inside AbilityEngine.
        self.drain_reduction: dict[str, float] = {key: 0.0 for key in blades}

    # ── Stat helper ───────────────────────────────────────────────────────────

    def _sta_stat(self, key: str) -> int:
        """Player's stamina stat — modified (parts/avatar/level) if available."""
        eff = self._eff.get(key)
        if eff and "stamina" in eff:
            return max(0, int(eff.get("stamina", 0)))
        return max(0, int(self._blades.get(key, {}).get("stats", {}).get("stamina", 0)))

    # ── Initialisation helper ─────────────────────────────────────────────────

    @staticmethod
    def _initial_stamina(sta_stat: int, ceiling: float | None = None) -> float:
        """Battle-start stamina = 3 + (stamina stat × 0.05), clamped to `ceiling`.

        Continuous scaling — every single stat point counts (1 = +0.05).
        Rounded to 2 decimals so every single point shows.

        `ceiling` is the bey's OWN maximum. It used to be the global 15, which
        is what made every bey above 240 stamina start identically.
        """
        cap = max_stamina_for(sta_stat) if ceiling is None else float(ceiling)
        start = STAMINA_START_BASE + (float(sta_stat) * STAMINA_START_PER_STAT)
        return round(min(cap, start), 2)

    def cap_for(self, key: str) -> float:
        """This player's stamina ceiling. Falls back to the legacy flat max."""
        return float(self.max_stamina.get(key, STAMINA_MAX))

    # ── Public: cost deduction ────────────────────────────────────────────────

    def deduct_cost(self, key: str, move: str) -> list[str]:
        """Deduct the stamina cost for the given move. Returns log lines."""
        cost = STAMINA_COST.get(move, 0.0)
        if cost <= 0:
            return []
        red = min(0.9, max(0.0, self.drain_reduction.get(key, 0.0)))
        note = ""
        if red > 0:
            cost = round(cost * (1.0 - red), 2)
            note = f" *(-{int(red * 100)}% drain)*"
        if cost <= 0:
            return []
        self.stamina[key] = round(max(0.0, self.stamina.get(key, 0.0) - cost), 2)
        name = self._blades.get(key, {}).get("name", "Unknown")
        return [f"💨 **{name}** stamina cost: -{cost:g} → `{self.stamina[key]:g}`{note}"]

    # ── Public: action-based stamina recovery (MOVE_STAMINA) ─────────────────

    def apply_stamina_action(self, key: str, hp: dict[str, int], type_mod, type_active: bool = False,
                             attacked: bool = False, max_hp: int | None = None) -> list[str]:
        """Process the 'Use Stamina' action: recover stamina + heal HP.

        ``type_mod``    — TypeModifiers for this player (or None).
        ``type_active`` — True when this player's type bonus is active for the
                          current matchup (resolved by session via
                          resolve_active_bonuses before calling here).
                          When False the stamina-type +1 recovery bonus and the
                          sta_mult heal scaling are both suppressed.
        ``attacked``    — True when the opponent used Attack/Special this same
                          round; the HP heal is cut by 50% (interrupted heal).
        ``max_hp``      — this player's real max HP (avatar bonuses can push it
                          above BASE_HP). Heal cap and low-HP ratio use this.
        """
        blade    = self._blades[key]
        sta_stat = self._sta_stat(key)   # modified stat (parts/avatar/level)
        cap_hp   = int(max_hp) if max_hp else BASE_HP
        # Scales with the stat now — see STAMINA_RECOVERY_PER_STAT for why the
        # old flat +3 was wrong.
        base_recovery = STAMINA_RECOVERY_BASE + sta_stat * STAMINA_RECOVERY_PER_STAT
        type_bonus = 0
        if type_mod is not None and type_active:
            btype = str(self._blades[key].get("type", "")).lower()
            if "stamina" in btype:
                type_bonus = 1
        total_recovery = round(base_recovery + type_bonus, 2)
        self.stamina[key] = round(
            min(self.cap_for(key), self.stamina.get(key, 0.0) + total_recovery), 2)
        hp_ratio  = hp.get(key, 0) / cap_hp if cap_hp else 0.0
        heal_mult = 1.0 + max(0.0, (0.40 - hp_ratio) / 0.40) * 0.2
        # Use type_mod.apply_stamina() to scale heal when the bonus is active
        if sta_stat > 0:
            raw_heal = max(STAMINA_HEAL_MIN, math.ceil(sta_stat * STAMINA_HEAL_RATIO * heal_mult))
            heal_amt = type_mod.apply_stamina(raw_heal) if (type_mod and type_active) else raw_heal
        else:
            heal_amt = 0
        # Interrupted heal: attacked mid-recovery → heal halved
        if attacked and heal_amt > 0:
            heal_amt = max(1, math.ceil(heal_amt * STAMINA_HEAL_INTERRUPT_MULT))
        if heal_amt > 0:
            hp[key] = min(cap_hp, hp.get(key, 0) + heal_amt)
        name       = blade["name"]
        type_note  = f" (+{type_bonus} stamina-type bonus)" if type_bonus > 0 else ""
        heal_note  = (
            f" and restores **{heal_amt} HP** (stamina healing"
            + (", boosted by low HP" if hp_ratio < 0.40 else "")
            + (", **halved — interrupted by attack!**" if attacked else "")
            + ")!"
        ) if heal_amt > 0 else ""
        return [
            f"⚡ **{name}** recovers **+{total_recovery:g}** stamina → "
            f"`{self.stamina[key]:g}/{self.cap_for(key):g}`"
            + type_note + heal_note
        ]

    # ── Public: passive regen ─────────────────────────────────────────────────
    def apply_passive_regen(self, key: str, hp: dict[str, int]) -> list[str]:
        """A small per-round stamina trickle, scaled by the stamina stat.

        Was a stub returning [] — it has been called every round from
        session.py since it was written and did nothing, so the stamina stat
        had no passive value at all.

        Silent by design: it fires every single round for both players, so
        logging it would bury the actual exchange under noise. The number shows
        up in the stamina bar, which is where a player reads it anyway.
        """
        cap = self.cap_for(key)
        current = self.stamina.get(key, 0.0)
        if current >= cap:
            return []
        regen = STAMINA_REGEN_BASE + self._sta_stat(key) * STAMINA_REGEN_PER_STAT
        if regen <= 0:
            return []
        self.stamina[key] = round(min(cap, current + regen), 2)
        return []

    # ── Public: Special Gauge ─────────────────────────────────────────────────

    def add_gauge(self, key: str, source: str) -> None:
        """Increment a player's Special Gauge based on what triggered it.

        source is one of: 'attack', 'defense', 'stamina', 'dmg_taken', 'charge'.
        """
        gain_map = {
            "attack":    GAUGE_PER_ATTACK,
            "defense":   GAUGE_PER_DEFENSE,
            "stamina":   GAUGE_PER_STAMINA,
            "dmg_taken": GAUGE_PER_DMG_TAKEN,
            "charge":    GAUGE_PER_CHARGE,
        }
        gain = gain_map.get(source, 0)
        self.gauge[key] = min(SPECIAL_GAUGE_MAX, self.gauge.get(key, 0) + gain)

    def gauge_ready(self, key: str) -> bool:
        return self.gauge.get(key, 0) >= SPECIAL_GAUGE_MAX

    def consume_gauge(self, key: str) -> None:
        """Reset gauge to 0 after a Special is used."""
        self.gauge[key] = 0  # Will create key if missing

    # ── Public: Stamina KO check ──────────────────────────────────────────────

    def check_stamina_ko(self, key: str, hp: dict[str, int], blades: dict[str, dict]) -> list[str]:
        """If this player's stamina hit 0, apply Stamina KO logic and return logs."""
        if self.stamina.get(key, 0) > 0 or hp.get(key, 0) <= 0:
            return []
        okey = None
        for k in hp.keys():
            if k != key:
                okey = k
                break
        if okey is None:
            return []  # FIX #4: Added missing return statement
        winner_sta = self._sta_stat(okey)  # modified stat (parts/avatar/level)
        spin_finish_chance = min(0.40, winner_sta * 0.004)
        hp[key] = 0
        blade_name = blades.get(key, {}).get("name", "Unknown")
        if spin_finish_chance > 0 and random.random() < spin_finish_chance:
            return [
                f"🌀 **SPIN FINISH!** **{blade_name}** ran out of Stamina and was "
                f"finished by superior spin! ({int(spin_finish_chance*100)}% chance)"
            ]
        return [f"💀 **{blade_name}** has run out of Stamina and stops spinning!"]

    # ── Convenience ───────────────────────────────────────────────────────────

    def can_afford(self, key: str, move: str) -> bool:
        cost = STAMINA_COST.get(move, None)
        if cost is None:
            raise ValueError(f"Unknown move: {move}. Valid moves: {list(STAMINA_COST.keys())}")
        return self.stamina.get(key, 0.0) >= cost
