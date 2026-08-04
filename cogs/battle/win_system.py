"""
battle/win_system.py
--------------------
Central trigger resolver for ALL ability conditions.

AbilityEngine and round logic should NEVER hardcode trigger strings.
Instead they call check_trigger(trigger, context) and let this module decide.

Supported triggers
------------------
Matchup-result (9 combos):
    on_attack_win       on_attack_loss      on_attack_mirror
    on_defense_win      on_defense_loss     on_defense_mirror
    on_stamina_win      on_stamina_loss     on_stamina_mirror

Generic result (any matchup):
    on_win              on_loss             on_mirror

HP state:
    on_low_hp           on_high_hp

Stamina state:
    on_low_stamina      on_high_stamina

Stability state:
    on_low_stability    on_high_stability

Context keys required
---------------------
    matchup      str   "attack" | "defense" | "stamina"
    result       str   "win" | "loss" | "mirror"
    hp           int   current HP of the acting player
    max_hp       int   maximum HP of the acting player
    stamina      float current stamina of the acting player
    max_stamina  float maximum stamina of the acting player
    stability    int   current stability of the acting player
    max_stability int  maximum stability of the acting player

All stat keys are optional — missing ones simply skip the relevant checks.
"""

from __future__ import annotations

# ── Thresholds ────────────────────────────────────────────────────────────────
_LOW_THRESHOLD  = 0.30   # ≤ 30 % → "low"
_HIGH_THRESHOLD = 0.70   # ≥ 70 % → "high"


def check_trigger(trigger: str, context: dict) -> bool:
    """Return True if *trigger* fires given the current *context*.

    Parameters
    ----------
    trigger:
        The ability trigger string (e.g. ``"on_attack_win"``, ``"on_low_hp"``).
    context:
        Dict produced by the round / battle session.  See module docstring for
        required keys.  Missing keys are handled gracefully (returns False for
        the associated group of checks).
    """
    if not trigger:
        return False

    matchup = context.get("matchup")   # "attack" | "defense" | "stamina"
    result  = context.get("result")    # "win" | "loss" | "mirror"

    hp            = context.get("hp")
    max_hp        = context.get("max_hp")

    stamina       = context.get("stamina")
    max_stamina   = context.get("max_stamina")

    stability     = context.get("stability")
    max_stability = context.get("max_stability")

    # -------------------------------------------------------------------------
    # 🔥 Matchup + result triggers  (e.g. "on_attack_win", "on_defense_mirror")
    # -------------------------------------------------------------------------
    if matchup and result:
        if trigger == f"on_{matchup}_{result}":
            return True

    # -------------------------------------------------------------------------
    # 🏆 Generic result triggers  (fire for ANY matchup type)
    # -------------------------------------------------------------------------
    if result == "win"    and trigger == "on_win":
        return True
    if result == "loss"   and trigger == "on_loss":
        return True
    if result == "mirror" and trigger == "on_mirror":
        return True

    # -------------------------------------------------------------------------
    # ❤️ HP triggers
    # -------------------------------------------------------------------------
    if hp is not None and max_hp:
        ratio = hp / max_hp
        if trigger == "on_low_hp"  and ratio <= _LOW_THRESHOLD:
            return True
        if trigger == "on_high_hp" and ratio >= _HIGH_THRESHOLD:
            return True

    # -------------------------------------------------------------------------
    # ⚡ Stamina triggers
    # -------------------------------------------------------------------------
    if stamina is not None and max_stamina:
        ratio = stamina / max_stamina
        if trigger == "on_low_stamina"  and ratio <= _LOW_THRESHOLD:
            return True
        if trigger == "on_high_stamina" and ratio >= _HIGH_THRESHOLD:
            return True

    # -------------------------------------------------------------------------
    # 🛡️ Stability triggers
    # -------------------------------------------------------------------------
    if stability is not None and max_stability:
        ratio = stability / max_stability
        if trigger == "on_low_stability"  and ratio <= _LOW_THRESHOLD:
            return True
        if trigger == "on_high_stability" and ratio >= _HIGH_THRESHOLD:
            return True

    return False


# ── Convenience: check a whole ability list at once ──────────────────────────

def get_fired_abilities(abilities: list[dict], context: dict) -> list[dict]:
    """Return only the abilities whose trigger fires for *context*.

    Useful when you want to inspect which abilities will fire before applying
    them, e.g. for logging or chaining.

    Parameters
    ----------
    abilities:
        List of ability dicts, each expected to have a ``"trigger"`` key.
    context:
        Same context dict passed to :func:`check_trigger`.
    """
    return [ab for ab in abilities if check_trigger(ab.get("trigger", ""), context)]
