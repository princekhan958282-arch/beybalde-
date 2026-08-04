"""
Avatar combat hooks.

Only the stat bonuses (attack/defence/stamina/HP) were ever read by the battle
code — everything with its own trigger or timing was parsed into AvatarBonuses
and then never consulted. This module is the single place those are applied, so
the call sites in attack_manager / defense_manager stay one line each.

Currently wired:
  crit_percent            extra crit roll on top of the blade's own chance
  gauge_on_crit           Special Gauge refunded when a crit lands   (Argus)
  immortal_rounds         first lethal hit is survived at 1 HP       (Argus)
  nth_hit_interval/pct    every Nth hit adds a slice of total attack (Dyrroth)
  defence_break_*         ignores part of enemy DEF for N rounds     (Dyrroth)
  special_move_percent    Special damage multiplier
  ult_adds_attack_stat    attacker's ATK stat added to the Special   (Dyrroth)
  dodge_chance            incoming hit avoided entirely
  counter_chance          counter-hit after a successful dodge
  multi_hit_power_double  each Special hit deals double
  multi_hit_extra_hits    one extra Special hit
  resistance_damage_pct   flat reduction on every incoming hit
  resistance_status_chance    chance to shrug off silence / burn
"""

from __future__ import annotations

import random


def _av(session, key: str):
    """Avatar bonuses for a player, or None."""
    try:
        av = session.avatar_bonuses.get(key)
    except AttributeError:
        return None
    return av if av is not None and getattr(av, "has_any_bonus", False) else None


# ── Crit ─────────────────────────────────────────────────────────────────────

def roll_extra_crit(session, key: str, already_crit: bool) -> bool:
    """Give the avatar a second chance at a crit the blade did not roll."""
    if already_crit:
        return True
    av = _av(session, key)
    if av is None or av.crit_percent <= 0.0:
        return False
    return random.random() < av.crit_percent


def on_crit(session, key: str) -> list[str]:
    """Rewards that fire when a crit lands. Returns log lines."""
    av = _av(session, key)
    if av is None or av.gauge_on_crit <= 0:
        return []
    sm = getattr(session, "stamina_manager", None)
    if sm is None:
        return []
    try:
        from cogs.core.constants import SPECIAL_GAUGE_MAX as _MAX
    except Exception:
        _MAX = 150
    before = sm.gauge.get(key, 0)
    sm.gauge[key] = min(_MAX, before + int(av.gauge_on_crit))
    gained = sm.gauge[key] - before
    if gained <= 0:
        return []
    return [f"  🔆 **Avatar** — critical hit charges the gauge +{gained}!"]


# ── Defence break ────────────────────────────────────────────────────────────

def apply_defence_break(session, key: str, ostats: dict) -> tuple[dict, list[str]]:
    """Shave a rolled fraction off the defender's DEF for the opening rounds."""
    av = _av(session, key)
    if av is None or av.defence_break_rounds <= 0:
        return ostats, []
    frac = av.roll_defence_break(getattr(session, "round", 1))
    if frac <= 0.0:
        return ostats, []
    out = dict(ostats)
    before = int(out.get("defense", 0))
    out["defense"] = max(0, int(round(before * (1.0 - frac))))
    if before <= 0 or out["defense"] == before:
        return ostats, []
    return out, [f"  🗡️ **Avatar** — Guard Breaker shreds {frac:.0%} DEF "
                 f"({before} → {out['defense']})!"]


# ── Nth-hit rider ────────────────────────────────────────────────────────────

def nth_hit_bonus(session, key: str, total_attack: float) -> tuple[int, list[str]]:
    """Count this hit and return the bonus damage when it is the Nth."""
    av = _av(session, key)
    if av is None or av.nth_hit_interval <= 0:
        return 0, []
    counts = getattr(session, "_avatar_hit_counts", None)
    if counts is None:
        counts = {}
        session._avatar_hit_counts = counts
    counts[key] = counts.get(key, 0) + 1
    bonus = av.nth_hit_bonus(counts[key], total_attack)
    if bonus <= 0:
        return 0, []
    return bonus, [f"  ⛓️ **Avatar** — every {av.nth_hit_interval}th strike! "
                   f"+{bonus} bonus damage."]


# ── Special / ultimate ───────────────────────────────────────────────────────

def apply_ult_bonus(session, key: str, damage: int,
                    total_attack: float) -> tuple[int, list[str]]:
    """special_move_percent, plus the attacker's ATK stat when the avatar adds it."""
    av = _av(session, key)
    if av is None:
        return damage, []
    logs: list[str] = []
    out = damage
    if av.special_move_percent or av.special_move_flat:
        boosted = int(round(av.apply_special_move_bonus(out)))
        if boosted != out:
            logs.append(f"  🌟 **Avatar** — Special damage {out} → {boosted}!")
            out = boosted
    if av.ult_adds_attack_stat and total_attack > 0:
        add = int(round(total_attack))
        out += add
        logs.append(f"  💠 **Avatar** — full Attack stat added: +{add}!")
    return out, logs


# ── Defensive layer: dodge, counter, damage resistance ───────────────────────

def absorb_incoming(session, defender: str, attacker: str,
                    damage: int) -> tuple[int, int, list[str]]:
    """Run an incoming hit through the defender's avatar.

    Returns (damage_through, counter_damage, logs). Order matters: a dodge
    zeroes the hit outright, and only a successful dodge can roll a counter —
    that is the contract the dodge/counter fields were written against.
    """
    av = _av(session, defender)
    if av is None or damage <= 0:
        return damage, 0, []

    logs: list[str] = []

    if av.roll_dodge():
        counter = 0
        logs.append(f"  💨 **Avatar** — dodged the hit completely!")
        if av.roll_counter():
            counter = max(1, int(round(damage * 0.5)))
            logs.append(f"  ↩️ **Avatar** — counter-strike for {counter}!")
        return 0, counter, logs

    if av.resistance_damage_percent > 0:
        reduced = int(round(av.apply_damage_resistance(damage)))
        if reduced < damage:
            logs.append(f"  🧱 **Avatar** — resistance absorbs "
                        f"{damage - reduced} ({av.resistance_damage_percent:.0%})!")
            damage = reduced

    return damage, 0, logs


def resist_status(session, key: str, label: str) -> tuple[bool, list[str]]:
    """True when the defender's avatar shrugs the status off."""
    av = _av(session, key)
    if av is None or av.resistance_status_chance <= 0:
        return False, []
    if not av.roll_status_resist():
        return False, []
    return True, [f"  🚫 **Avatar** — resisted {label}!"]


# ── Multi-hit ────────────────────────────────────────────────────────────────

def multi_hit_shape(session, key: str, hits: int,
                    per_hit: int) -> tuple[int, int, list[str]]:
    """Apply the avatar's multi-hit modifiers to a Special's shape."""
    av = _av(session, key)
    if av is None or hits <= 1:
        return hits, per_hit, []
    logs: list[str] = []
    if av.multi_hit_extra_hits:
        hits = av.extra_hits(hits)
        logs.append(f"  ➕ **Avatar** — one extra hit ({hits} total)!")
    if av.multi_hit_power_double:
        per_hit = int(round(av.apply_multi_hit_damage(per_hit)))
        logs.append(f"  ✳️ **Avatar** — every hit doubled ({per_hit} each)!")
    return hits, per_hit, logs


# ── Immortality ──────────────────────────────────────────────────────────────

def guard_lethal(session, key: str, incoming: int) -> tuple[int, list[str]]:
    """Cap damage so it cannot kill while the avatar's immortality is up.

    Triggers on the first hit that would be lethal, then stays active for
    ``immortal_rounds`` rounds. Once per battle.
    """
    av = _av(session, key)
    if av is None or av.immortal_rounds <= 0:
        return incoming, []

    state = getattr(session, "_avatar_immortal", None)
    if state is None:
        state = {}
        session._avatar_immortal = state

    hp    = int(session.hp.get(key, 0))
    now   = int(getattr(session, "round", 1))
    entry = state.get(key)
    logs: list[str] = []

    if entry is None:
        if incoming < hp:
            return incoming, []           # not lethal yet, save the trigger
        state[key] = now + av.immortal_rounds - 1
        entry = state[key]
        logs.append(f"  ✨ **Avatar** — DEATHLESS! Refuses to fall — "
                    f"immortal for {av.immortal_rounds} rounds!")
    elif entry == "spent" or now > entry:
        state[key] = "spent"
        return incoming, []

    if incoming >= hp:
        survived = incoming
        incoming = max(0, hp - 1)
        logs.append(f"  🛡️ **Avatar** — survives a lethal {survived} hit at 1 HP!")
    return incoming, logs
