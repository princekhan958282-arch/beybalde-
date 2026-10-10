"""Readable skill effects derived from the same scaling used in battles."""
import re
from . import avatar_config as C, avatar_progress as AP
from .avatar_scaling import scaled_card, OP_MAGNITUDES

BONUS_NAMES = {
    'attack': 'Attack', 'defence': 'Defense', 'stamina': 'Stamina',
    'stability': 'Stability', 'charge': 'Charge', 'special_move': 'Special Move damage',
    'crit': 'Crit chance', 'dodge': 'Dodge chance', 'counter': 'Counter chance',
    'resistance_damage': 'Damage resistance', 'resistance_status': 'Status resistance',
    'hp': 'HP',
}
PERCENT_OPS = {'crit_chance', 'bonus_damage_pct', 'bonus_damage_stat',
               'ignore_defense', 'reduce_damage_pct'}
FIXED_OPS = {'gain_stack', 'spend_stacks', 'set_counter', 'prime_crit',
             'add_counter', 'consume_counter', 'reset_counter'}


def number(value):
    return f'{value:.2f}'.rstrip('0').rstrip('.')


def skill_level(card, levels, slot, card_level=C.MAX_CARD_LEVEL):
    profile = {'avatar': {'cards': {card['id']: {'level': card_level, 'skills': levels or {}}}}}
    return AP.skill_level(profile, card['id'], AP.slugify(card['skills'][slot - 1]['name']))


def scaled_skill(card, slot, level):
    # Reuse battle scaling rather than multiplying display values independently.
    profile = {'avatar': {'cards': {card['id']: {'level': C.MAX_CARD_LEVEL,
        'skills': {AP.slugify(card['skills'][slot - 1]['name']): level}}}}}
    return scaled_card(profile, card)['skills'][slot - 1]


def skill_description(card, slot, level):
    original = card['skills'][slot - 1]
    scaled = scaled_skill(card, slot, level)
    bonuses = scaled.get('bonuses') or {}
    lines = []
    handled = set()
    if bonuses.get('nth_hit_interval'):
        lines.append(f"Every {int(bonuses['nth_hit_interval'])} hits adds "
                     f"{number(bonuses.get('nth_hit_attack_percent', 0) * 100)}% of your Attack as damage.")
        handled.update({'nth_hit_interval', 'nth_hit_attack_percent'})
    if bonuses.get('defence_break_rounds'):
        lines.append(f"Ignores {number(bonuses.get('defence_break_min', 0) * 100)}–"
                     f"{number(bonuses.get('defence_break_max', 0) * 100)}% of enemy Defense "
                     f"for the first {int(bonuses['defence_break_rounds'])} rounds.")
        handled.update({'defence_break_min', 'defence_break_max', 'defence_break_rounds'})
    if bonuses.get('immortal_rounds'):
        lines.append(f"The first lethal hit cannot kill — immortal for "
                     f"{int(bonuses['immortal_rounds'])} rounds. Once per battle.")
        handled.add('immortal_rounds')
    if bonuses.get('gauge_on_crit'):
        lines.append(f"Every crit restores {number(bonuses['gauge_on_crit'])} Special Gauge.")
        handled.add('gauge_on_crit')
    for key, value in bonuses.items():
        if key in handled:
            continue
        if value is True:
            flag = {
                'ult_adds_attack_stat': "The Bey's full Attack stat is added on top.",
                'multi_hit_extra_hits': 'Every Special fires an extra hit.',
                'multi_hit_power_double': 'Double damage per hit.',
            }.get(key)
            if flag:
                lines.append(flag)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not value:
            continue
        suffix = next((s for s in ('_percent', '_chance', '_flat') if key.endswith(s)), '')
        name = BONUS_NAMES.get(key[:-len(suffix)] if suffix else key, key.replace('_', ' ').title())
        if suffix in ('_percent', '_chance'):
            lines.append(f'{name} increased by {number(value * 100)}%.')
        else:
            lines.append(f'{name} increased by {number(value)}.')
    if lines:
        return '\n'.join(lines)

    text = original.get('description') or 'No description.'
    replacements = {}
    for rule, scaled_rule in zip(original.get('rules', []), scaled.get('rules', [])):
        for op, scaled_op in zip(rule.get('do', []), scaled_rule.get('do', [])):
            if op.get('op') in FIXED_OPS:
                continue
            for key in OP_MAGNITUDES:
                value = op.get(key)
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                percent = op.get('op') in PERCENT_OPS or key == 'pct'
                old, new = value, scaled_op[key]
                # Rule percentage operations use whole percentages in value.
                if key == 'pct':
                    old, new = old * 100, new * 100
                replacements[(float(old), percent)] = number(new)
    # One pass avoids replacing an already-scaled value again. Durations,
    # energy costs, thresholds expressed in %, and counters stay unchanged.
    def replace(match):
        raw, percent = match.group(1), bool(match.group(2))
        tail = text[match.end():]
        if not percent and re.match(r'\s*(?:rounds?|turns?|hits?|stacks?|energy)\b', tail):
            return match.group(0)
        new = replacements.get((float(raw), percent))
        return new + ('%' if percent else '') if new is not None else match.group(0)
    return re.sub(r'(?<![\w.])(\d+(?:\.\d+)?)(%?)(?!\w|\.\d)', replace, text)


def skill_stat_text(card, level):
    steps = level - 1
    if card.get('active_battle_skills'):
        pct = number(C.EMPOWER_PERCENT_STEP * steps * 100)
        return f'After activation: +{pct}% ATK / DEF for {C.EMPOWER_ROUNDS} rounds.'
    return ('While selected: ' + ' / '.join(
        f'+{number(gain * steps)} {stat}' for stat, gain in
        [('ATK', C.SKILL_STAT_GAIN['attack']), ('DEF', C.SKILL_STAT_GAIN['defense']),
         ('STM', C.SKILL_STAT_GAIN['stamina'])]) + '.')


def skill_preview(card, slot, current, target):
    now = skill_description(card, slot, current)
    if target == current:
        return f'{now}\n{skill_stat_text(card, current)}'
    return (f'Current (Lv{current}): {now}\nNext (Lv{target}): '
            f'{skill_description(card, slot, target)}\n'
            f'{skill_stat_text(card, current)} → {skill_stat_text(card, target)}')
