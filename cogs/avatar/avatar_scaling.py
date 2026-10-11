"""Copy-on-read skill scaling; never edit shared roster definitions."""
from copy import deepcopy
from . import avatar_progress as AP, avatar_levels as AL, avatar_config as C

# Scale effect magnitudes, not timers, costs, gates, caps, stack counts or IDs.
OP_MAGNITUDES = {'value', 'pct', 'amount', 'scale'}
BONUS_GATES = {'immortal_rounds', 'defence_break_rounds', 'nth_hit_interval'}
PROBABILITIES = {'crit_percent', 'dodge_chance', 'counter_chance',
                 'resistance_damage_percent', 'resistance_status_chance',
                 'defence_break_min', 'defence_break_max'}


def scaled_card(profile, card):
    out = deepcopy(card)
    levels = []
    for skill in out.get('skills', []):
        level = AP.skill_level(profile, out['id'], AP.slugify(skill['name']))
        levels.append(level)
        mult = AL.skill_magnitude_mult(level)
        for k, v in skill.get('bonuses', {}).items():
            if k not in BONUS_GATES and isinstance(v, (int, float)) and not isinstance(v, bool):
                value = v * mult
                skill['bonuses'][k] = min(1.0, max(0.0, value)) if k in PROBABILITIES else value
        for rule in skill.get('rules', []):
            for op in rule.get('do', []):
                if op.get('op') in {'gain_stack', 'spend_stacks', 'set_counter', 'prime_crit', 'add_counter', 'consume_counter', 'reset_counter'}:
                    continue
                for k in OP_MAGNITUDES:
                    if isinstance(op.get(k), (int, float)) and not isinstance(op[k], bool):
                        op[k] *= mult
    out['_skill_levels'] = levels
    return out


def selected_stats(card, slot):
    if card.get('active_battle_skills'):
        return dict.fromkeys(C.SKILL_STAT_GAIN, 0)
    levels = card.get('_skill_levels', [])
    steps = levels[slot - 1] - 1 if 1 <= slot <= len(levels) else 0
    return {s: n * steps for s, n in C.SKILL_STAT_GAIN.items()}
