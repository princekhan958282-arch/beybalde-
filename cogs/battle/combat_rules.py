"""Pure combat arithmetic shared by live sessions and boss/search adapters.

Adapters supply authored move costs and normal regeneration. These helpers own
type discounts, resource bounds and actual HP changes; they never write Bey data.
"""
import math


def base_damage(level, power, attack, defense):
    """One standard formula; preserve precision until HP is committed."""
    return (((2 * max(1, float(level)) / 5 + 2) * max(0, float(power))
             * max(0, float(attack)) / max(1.0, float(defense))) / 50) + 2


def hp_damage(value):
    return max(0, math.floor(float(value)))


def damage_hp(current, damage, *, already_final=False):
    # Exact returns have already passed the source's rounding/HP cap. A
    # fractional HP remainder from an authored heal must be returned intact.
    amount = max(0, damage) if already_final else hp_damage(damage)
    actual = min(max(0, current), amount)
    return max(0, current - actual), actual


def recover_hp(current, maximum, healing):
    if current <= 0:
        return current, 0
    actual = min(max(0, maximum - current), hp_damage(healing))
    return current + actual, actual


def recover_resource(current, maximum, recovery, *, preserve_reserve=False):
    ceiling = max(maximum, current) if preserve_reserve else maximum
    return max(0, min(ceiling, current + max(0, recovery)))


def spend_resource(current, cost):
    return max(0, current - max(0, cost))


def starting_stamina(stamina_stat, maximum):
    return round(min(maximum, max(0, stamina_stat) / 20), 2)


def type_stamina_cost(bey_type, cost):
    # Import locally: type_system's legacy display adapter delegates back to
    # type_gimmicks, so it must not create a module-initialization cycle.
    from cogs.abilities.type_system import normalise_type
    return max(0, cost) * (.8 if normalise_type(bey_type) == 'stamina' else 1)
