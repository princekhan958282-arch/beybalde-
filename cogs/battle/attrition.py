"""
Attrition — the per-round applier for the late-game type attrition rules.

The RULES (when it starts, how much stamina each type bleeds, which stat each
type ramps) live in cogs/abilities/type_system.py, next to atk_mult/def_mult/
sta_mult and starting stability, because every one of them is decided purely
by type. This module only walks the session each round and applies them.

Why it exists: nothing caps the round counter, and the Stamina move costs 0
stamina while restoring +3 and healing ~64 HP — so two cautious players could
stall forever. Attrition makes standing still cost something.
"""

from __future__ import annotations

from cogs.abilities.type_system import (
    ATTRITION_START,
    attrition_active,
    attrition_drain_for,
    attrition_stat_gains,
)


def _type_of(blade: dict) -> str:
    return str((blade or {}).get("type", "Balance")).strip().lower()


class AttritionSystem:
    """Applies the per-round attrition step for one BattleSession."""

    def __init__(self, session) -> None:
        self.session = session
        # Running totals, purely for display — the stat effect itself is
        # applied through StatusManager buffs so every existing consumer
        # (damage_filter, defense_manager, the battle card) picks it up.
        self.granted: dict[str, dict[str, int]] = {}
        # The single buff entry per (player, stat) that attrition owns and
        # grows. StatusManager.add_buff APPENDS a new dict on every call, so
        # calling it each round would leave ~26 identical entries per player by
        # round 40 — every get_buff_bonus and tick_buffs then walks all of them.
        self._entries: dict[tuple[str, str], dict] = {}

    # ------------------------------------------------------------------
    def _grow_buff(self, key: str, stat: str, amount: int) -> int:
        """Add to this player's attrition buff for *stat*, returning the total."""
        st = getattr(self.session, "status", None)
        if st is None or amount <= 0:
            return self.granted.get(key, {}).get(stat, 0)

        entry = self._entries.get((key, stat))
        live  = st.active_buffs.get(key, [])
        # tick_buffs can drop the entry (or the whole player list can be
        # rebuilt), so re-create it rather than trusting a stale reference.
        if entry is None or entry not in live:
            st.add_buff(key, stat, amount, 99)
            self._entries[(key, stat)] = st.active_buffs[key][-1]
        else:
            entry["amount"] += amount
            entry["rounds_left"] = 99      # attrition never wears off

        tally = self.granted.setdefault(key, {})
        tally[stat] = tally.get(stat, 0) + amount
        return tally[stat]

    # ------------------------------------------------------------------
    def apply_round(self, round_no: int) -> list[str]:
        """Run one attrition step. Returns log lines (empty before it starts)."""
        if not attrition_active(round_no):
            return []

        logs: list[str] = []
        sm = getattr(self.session, "stamina_manager", None)
        st = getattr(self.session, "status", None)
        blades = getattr(self.session, "blades", {}) or {}

        if int(round_no) == ATTRITION_START:
            logs.append(
                "⏳ **ATTRITION** — the stadium turns hostile. Stamina now "
                "bleeds every round and each Bey leans into its nature."
            )

        for key, blade in blades.items():
            if self.session.hp.get(key, 0) <= 0:
                continue
            name = (blade or {}).get("name", "Unknown")
            parts: list[str] = []

            # ── stat ramp ────────────────────────────────────────────────
            gains = attrition_stat_gains(_type_of(blade), round_no)
            for stat, amount in gains.items():
                if amount > 0:
                    total = self._grow_buff(key, stat, amount)
                    parts.append(f"{stat[:3].upper()} +{total}")

            # ── stamina bite ─────────────────────────────────────────────
            drain = attrition_drain_for(_type_of(blade), round_no)
            if drain > 0 and sm is not None:
                before = sm.stamina.get(key, 0.0)
                sm.stamina[key] = round(max(0.0, before - drain), 2)
                taken = round(before - sm.stamina[key], 2)
                if taken > 0:
                    parts.append(f"stamina -{taken:g} → {sm.stamina[key]:g}")

            if parts:
                logs.append(f"　⏳ **{name}** — " + " · ".join(parts))

        return logs
