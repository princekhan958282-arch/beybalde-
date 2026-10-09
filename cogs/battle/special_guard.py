"""Defense-button guard against an opponent's Special in live PvP/Story."""
import math
from .constants import MOVE_DEFENSE, MOVE_SPECIAL


def guarded(session, attacker, defender, move):
    moves = getattr(session, 'moves', {}) or {}
    if move != MOVE_SPECIAL or moves.get(defender) != MOVE_DEFENSE:
        return False
    special = (getattr(session, 'blades', {}).get(attacker, {}).get('special_move') or {})
    status = getattr(session, 'status', None)
    true_turns = status.get_duration('true_damage_turns', attacker) if status is not None else 0
    return not (special.get('true_damage', False) or true_turns > 0)


def reduce_special(session, attacker, defender, move, damage):
    if guarded(session, attacker, defender, move):
        # Keep 40%; round down per hit, including direct Special damage.
        return math.floor(max(0, damage) * 2 / 5)
    return damage
