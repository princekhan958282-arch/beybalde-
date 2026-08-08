"""
battle/damage_rules.py
----------------------
Pure damage calculation functions. No session state, no side effects.

calc_damage()     — resolves one attacker-vs-defender move pair into raw numbers.
resolve_special() — reads a blade's special_move block into (hits, per_hit, flavours).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFENSE SYSTEM  (replaces old flat-mitigation approach)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. SHIELD GATE
   Any hit whose raw value is below  def_stat × SHIELD_GATE_RATIO  is
   completely nullified (0 damage).  Hits that break through are reduced
   by a flat  def_stat × SHIELD_FLAT_RATIO  amount.

2. DEFENSE COUNTER HIT
   When Defense wins vs Attack, the counter scales with how much of the
   incoming hit was absorbed:
       counter = (absorbed × COUNTER_ABSORB_RATIO) + (def_stat × COUNTER_STAT_RATIO)
   Minimum is COUNTER_MIN so there is always some pushback.

3. DEFENSE DEFICIT BLEED MULTIPLIER
   If the attacker's ATK stat exceeds the defender's DEF stat, the gap
   amplifies outgoing damage:
       bleed_mult = 1.0 + (deficit / DEFICIT_DIVISOR)
   Applied after shield-gate / flat reduction — low-DEF blades take
   escalating punishment against high-ATK opponents.

4. CRIT VS DEFENSE
   Crits bypass Shield Gate (they always land something) but defense
   mitigation still applies — crits are no longer a full pierce.

5. GRIND DEBUFF (flag only — applied in session.py)
   When Defense loses to Stamina, calc_damage returns matchup="lose_grind"
   instead of plain "lose".  session.py watches for this string and applies
   the stamina-regen reduction to the opponent for:
       1 + floor(def_stat / GRIND_LEVEL_DIVISOR)  rounds

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import math
import random
from typing import Optional

from .constants import (
    MOVE_ATTACK, MOVE_DEFENSE, MOVE_STAMINA, MOVE_SPECIAL, MOVE_CHARGE,
    COUNTER, MIRROR_CHIP_DAMAGE, WINNING_BONUS_MULT, LOSING_PENALTY_MULT,
    ATTACK_VS_STAMINA_MULT, NORMAL_ATTACK_DAMAGE_SCALE,
    ATTACK_CLASH_DEF_CONVERSION, ATTACK_CLASH_MULT,
    BASE_HP, SPECIAL_FALLBACK_HP_FRACTION,
)

# ── Defense tuning knobs ──────────────────────────────────────────────────────

# Shield Gate: hits below (def_stat × this) are fully blocked
SHIELD_GATE_RATIO    = 0.6

# Flat damage reduction on hits that break through the gate
SHIELD_FLAT_RATIO    = 0.3

# Fraction of a fully-gated hit that still gets through. Defence stays
# overwhelmingly strong — 94% of the hit is gone — but "strong" and
# "mathematically immune" are different things, and only one of them ends a
# battle. See the comment in _shield_gate for the bug this fixes.
SHIELD_GATE_CHIP_RATIO = 0.06

# Counter hit: flat base + DEF-scaled bonus (continuous per-point scaling)
# counter = COUNTER_BASE + min(COUNTER_EXTRA_MAX, def_stat × COUNTER_PER_DEF)
# 1 DEF = +0.25, so 20 DEF = +5.
# DEF 1 → 10.25≈10  |  DEF 40 → 20  |  DEF 100 → 35  |  DEF 135 → 44  |  DEF 200+ → 60 (cap)
COUNTER_BASE         = 10
COUNTER_PER_DEF      = 0.25
COUNTER_EXTRA_MAX    = 50
# Legacy knobs kept for import compatibility (no longer used in formula)
COUNTER_ABSORB_RATIO = 0.4
COUNTER_STAT_RATIO   = 0.25
COUNTER_MIN          = 10          # always deal at least this much on counter

# Deficit bleed: 1 + (atk_gap / this) → 200 = every 20-point gap adds 10%
DEFICIT_DIVISOR      = 200

# Grind debuff: Defense vs Stamina applies regen reduction for this many rounds
# Final duration = 1 + floor(def_stat / GRIND_LEVEL_DIVISOR)
GRIND_LEVEL_DIVISOR  = 50

# Crit chance on Attack moves: base 10%, scales with ATK stat up to 12%.
# crit = min(CRIT_MAX, CRIT_BASE + atk_stat / CRIT_STAT_DIVISOR)
# ATK 0 → 10%  |  ATK 50 → 11%  |  ATK 100+ → 12% (cap)
CRIT_BASE            = 0.10
CRIT_MAX             = 0.12
CRIT_STAT_DIVISOR    = 5000.0


def _crit_chance(atk_stat: int) -> float:
    """Stat-scaled crit chance: 10% base, +ATK/5000, capped at 12%."""
    return min(CRIT_MAX, CRIT_BASE + max(0, int(atk_stat)) / CRIT_STAT_DIVISOR)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _shield_gate(raw_hit: int, def_stat: int) -> tuple[int, int]:
    """Apply Shield Gate + flat reduction to an incoming hit.

    Returns (damage_after_mitigation, absorbed_amount).
    absorbed_amount is used by the counter-hit formula.
    """
    def_stat       = max(0, def_stat)   # negative DEF must never ADD damage
    gate_threshold = def_stat * SHIELD_GATE_RATIO
    if raw_hit < gate_threshold:
        # Blocked — but never to literally zero.
        #
        # Returning a true 0 made high-DEF blades UNKILLABLE by anything that
        # could not clear the gate. Against a Nemesis boss copy (DEF 90-147)
        # the gate sits at 54-88 while a normal Attack lands at
        # atk x LOSING_PENALTY_MULT, so an attacker needed roughly 135+ ATK to
        # deal ANY damage at all — on a roster whose blades average 97. Every
        # other exchange dealt nothing, and with the Stamina move healing ~80
        # for zero cost the defender's HP simply stopped moving. The reported
        # symptom was a copy frozen at 223 HP that would not die.
        #
        # A percentage of the raw hit rather than a flat number, so a strong
        # attacker is not reduced to the same trickle as a weak one, and so the
        # chip scales with any future change to attack values.
        chip = max(1, math.ceil(raw_hit * SHIELD_GATE_CHIP_RATIO))
        return chip, max(0, raw_hit - chip)

    flat_reduction = math.ceil(def_stat * SHIELD_FLAT_RATIO)
    mitigated      = max(5, raw_hit - flat_reduction)
    absorbed       = raw_hit - mitigated
    return mitigated, absorbed


def _deficit_bleed(raw_hit: int, atk_stat: int, def_stat: int) -> int:
    """Amplify raw_hit when attacker ATK exceeds defender DEF.

    The multiplier is applied AFTER shield-gate so defense still does its job
    first — but the gap punishes under-investment on top of whatever lands.
    """
    deficit    = max(0, atk_stat - def_stat)
    bleed_mult = 1.0 + (deficit / DEFICIT_DIVISOR)
    return math.ceil(raw_hit * bleed_mult)


def _counter_hit(absorbed: int, def_stat: int) -> int:
    """Counter-hit damage dealt back by a successful Defense.

    counter = 10 + def_stat × 0.25 (every DEF point counts; 20 DEF = +5),
    extra capped at +50 (total max 60).
    ``absorbed`` is kept in the signature for API compatibility but no
    longer affects the counter value.
    """
    extra = min(COUNTER_EXTRA_MAX, max(0.0, float(def_stat)) * COUNTER_PER_DEF)
    return int(round(COUNTER_BASE + extra))


def _grind_duration(def_stat: int) -> int:
    """Number of rounds the Grind debuff lasts (scales with DEF stat)."""
    return 1 + (def_stat // GRIND_LEVEL_DIVISOR)


# ── Public API ────────────────────────────────────────────────────────────────

def special_scale(blade: dict, special_stat: Optional[float]) -> float:
    """How much to multiply a blade's authored Special damage by.

    Specials used to be a flat number out of beyblades.json, which meant the
    `special` stat on every blade fed nothing in combat and levelling never
    touched a Special. This is the hook that connects them: damage scales by
    how far the blade's CURRENT special stat has grown past its printed one.

    Returns exactly 1.0 when the stat is absent, unknown or still at its
    printed value — so a level-1 blade deals precisely its authored damage and
    nothing about the existing roster is rebalanced by this landing.
    """
    if special_stat is None:
        return 1.0
    try:
        base = float((blade.get("stats") or {}).get("special") or 0.0)
        now = float(special_stat)
    except (TypeError, ValueError):
        return 1.0
    if base <= 0.0 or now <= 0.0:
        return 1.0
    # Never scale DOWN. A debuff that drops the stat below its printed value
    # should not also retroactively weaken the authored Special.
    return max(1.0, now / base)


def resolve_special(blade: dict,
                    special_stat: Optional[float] = None
                    ) -> tuple[int, int, list[str], bool]:
    """Return (hits, damage_per_hit, flavour_texts, ignores_defense).

    Preserves multi-hit structure so the ability engine can apply per-hit
    modifiers (passives, rage stacks, ATK buffs, etc.) to each individual
    hit rather than a pre-collapsed total.

    ``special_stat`` is the wielder's EFFECTIVE special stat — levelled, with
    parts and avatar folded in. Pass it and the Special scales with it; omit it
    and you get the authored damage unchanged. `blade` must carry its PRINTED
    stats for the ratio to mean anything, which is what session.blades holds.

    ``ignores_defense`` is True when the special_move block carries
    ``"ignores_defense": true`` (e.g. Xcalibur Dragon Cleave).
    """
    scale = special_scale(blade, special_stat)
    sm = blade.get("special_move")
    if sm:
        hits = sm.get("hits", 1)
        # Guard: hits must be int (JSON may store as string)
        if not isinstance(hits, int):
            try:
                hits = int(hits)
            except (TypeError, ValueError):
                hits = 1
        if "total_damage" in sm and "damage_per_hit" not in sm:
            per_hit = max(1, sm["total_damage"] // max(hits, 1))
        else:
            raw_per_hit = sm.get("damage_per_hit", 25)
            # Guard: per_hit may be a list (e.g. [40, 70] for range) or string
            if isinstance(raw_per_hit, list):
                per_hit = int(sum(raw_per_hit) / len(raw_per_hit)) if raw_per_hit else 25
            else:
                try:
                    per_hit = int(raw_per_hit)
                except (TypeError, ValueError):
                    per_hit = 25
        # Applied here, before the flavour text is built, so an auto-generated
        # line quotes the damage the hit will actually deal.
        if scale > 1.0:
            per_hit = max(1, math.ceil(per_hit * scale))
        # Accept both "flavour_texts" (list) and "flavour_text" (str, legacy)
        flavour = sm.get("flavour_texts") or sm.get("flavour_text")
        if isinstance(flavour, str):
            flavour = [flavour]
        if not flavour:
            flavour = [f"💥 {sm.get('name', 'Special')} — {hits}×{per_hit} damage!"]
        ignores_defense = bool(sm.get("ignores_defense", False))
        return hits, per_hit, flavour, ignores_defense

    # Fallback for a blade with no special_move data. Scaled FROM BASE_HP
    # rather than hardcoded: it used to be a flat 360, which was 60% of the
    # old 600 HP bar and became 9% of the 4000 one — a blade with missing
    # special data silently lost its Special the moment HP was rescaled.
    fallback = max(5, math.ceil(BASE_HP * SPECIAL_FALLBACK_HP_FRACTION * scale))
    return 1, fallback, [f"🌟 Special move deals **{fallback} damage**!"], False


def calc_damage(
    attacker_move:  str,
    attacker_stats: dict,
    defender_stats: dict,
    attacker_blade: dict,
    defender_move:  str,
) -> tuple[int, int, str, bool]:
    """Return (dmg_dealt, dmg_taken, matchup, is_crit).

    matchup is one of: 'win', 'lose', 'mirror', 'lose_grind'.
    'lose_grind' means Defense lost to Stamina and session.py should
    apply the Grind debuff to the Stamina user.

    MOVE_SPECIAL is NOT handled here — it is resolved per-hit in session.py
    so passive, ATK-buff, and rage modifiers apply to each individual hit.
    """
    is_crit = False

    # ── Stamina / Charge: no outgoing damage ─────────────────────────────────
    if attacker_move in (MOVE_STAMINA, MOVE_CHARGE):
        if attacker_move == MOVE_STAMINA and defender_move == MOVE_DEFENSE:
            return 0, 0, "win", False
        if attacker_move == MOVE_STAMINA and defender_move == MOVE_ATTACK:
            return 0, 0, "lose", False
        return 0, 0, "mirror", False

    # ── Attack ────────────────────────────────────────────────────────────────
    if attacker_move == MOVE_ATTACK:
        atk_stat = attacker_stats.get("attack", 50)

        if attacker_move == defender_move:
            # ── Attack vs Attack ──────────────────────────────────────────
            # Previously a flat MIRROR_CHIP_DAMAGE both ways: the same 32
            # damage whether a 47-attack blade or a 300-attack one threw it,
            # so the clash ignored everything the player had built.
            #
            # Now the attacker converts a slice of the enemy's DEFENCE into
            # bonus attack — heavier armour gives more to bite into — and the
            # result is a real damage roll, so stats finally matter here.
            clash_def = defender_stats.get("defense", 50)
            atk_stat  = atk_stat + clash_def * ATTACK_CLASH_DEF_CONVERSION

            raw = atk_stat * ATTACK_CLASH_MULT
            if random.random() < _crit_chance(atk_stat):
                raw     = raw * 1.6
                is_crit = True
            raw = raw * NORMAL_ATTACK_DAMAGE_SCALE
            # Symmetric: the caller resolves the other half from the other
            # side, so only outgoing damage is returned here.
            return max(5, math.ceil(raw)), 0, "mirror", is_crit

        # ── Attack vs Defense ─────────────────────────────────────────────────
        if defender_move == MOVE_DEFENSE:
            def_stat = defender_stats.get("defense", 50)
            # When defense-pierce is active, defense_manager zeroes out DEF for
            # gate/mitigation but stores the real value under "_real_defense".
            # Use it for deficit_bleed so pierce doesn't artificially inflate
            # the bleed multiplier by treating the opponent as having 0 DEF.
            real_def_stat = defender_stats.get("_real_defense", def_stat)

            # Crits bypass the Shield Gate but mitigation still applies
            if random.random() < _crit_chance(atk_stat):
                raw_crit          = max(5, math.ceil(atk_stat * 1.6))
                flat_red          = math.ceil(def_stat * SHIELD_FLAT_RATIO)
                crit_dmg          = max(5, raw_crit - flat_red)
                crit_dmg          = _deficit_bleed(crit_dmg, atk_stat, real_def_stat)
                crit_dmg          = max(5, math.ceil(crit_dmg * NORMAL_ATTACK_DAMAGE_SCALE))
                return crit_dmg, 0, "win", True

            # Normal Attack vs Defense
            raw_hit             = math.ceil(atk_stat * LOSING_PENALTY_MULT)
            mitigated, absorbed = _shield_gate(raw_hit, def_stat)
            mitigated           = _deficit_bleed(mitigated, atk_stat, real_def_stat)
            counter_dmg         = _counter_hit(absorbed, def_stat)
            # Scale only the attacker's outgoing damage — the counter keeps its
            # own COUNTER_MIN floor so defense pushback isn't nerfed.
            if mitigated > 0:
                mitigated = max(1, math.ceil(mitigated * NORMAL_ATTACK_DAMAGE_SCALE))

            return mitigated, counter_dmg, "lose", False

        # ── Attack vs everything else ─────────────────────────────────────────
        if COUNTER.get(attacker_move) == defender_move:   # Attack beats Stamina
            mult, matchup = ATTACK_VS_STAMINA_MULT, "win"
        elif defender_move == MOVE_CHARGE:
            mult, matchup = WINNING_BONUS_MULT, "win"
        else:
            mult, matchup = LOSING_PENALTY_MULT, "lose"

        raw = atk_stat * mult
        if random.random() < _crit_chance(atk_stat):
            raw     = raw * 1.6
            is_crit = True
        raw = raw * NORMAL_ATTACK_DAMAGE_SCALE
        return max(5, math.ceil(raw)), 0, matchup, is_crit

    # ── Defense ───────────────────────────────────────────────────────────────
    if attacker_move == MOVE_DEFENSE:
        def_stat = attacker_stats.get("defense", 50)

        if attacker_move == defender_move:
            return MIRROR_CHIP_DAMAGE, MIRROR_CHIP_DAMAGE, "mirror", False

        # ── Defense vs Attack — counter hit (handled in Attack branch) ──
        # This branch fires when ATTACKER pressed Defense and OPPONENT pressed Attack.
        # The real counter-hit damage is already returned by resolve_pair for the opponent
        # (Attack vs Defense → dmg_taken = counter), so we return 0 here to avoid a double hit.
        if defender_move == MOVE_ATTACK:
            return 0, 0, "win", False

        # ── Defense vs Stamina: Stamina wins but Grind debuff triggers ─────────
        raw = def_stat * LOSING_PENALTY_MULT
        return max(5, math.ceil(raw)), 0, "lose_grind", False

    # ── Fallback ──────────────────────────────────────────────────────────────
    if attacker_move == defender_move:
        return MIRROR_CHIP_DAMAGE, MIRROR_CHIP_DAMAGE, "mirror", False
    if COUNTER.get(attacker_move) == defender_move:
        mult, matchup = WINNING_BONUS_MULT, "win"
    else:
        mult, matchup = LOSING_PENALTY_MULT, "lose"
    raw = attacker_stats.get("attack", 50) * mult * NORMAL_ATTACK_DAMAGE_SCALE
    return max(5, math.ceil(raw)), 0, matchup, False
