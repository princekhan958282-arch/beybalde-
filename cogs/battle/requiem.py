"""Session-local Requiem Special forms, active on rounds after the cast."""
from .combat_rules import hp_damage, recover_resource


def state(session, key):
    return getattr(session, 'requiem_forms', {}).get(key)


def active(session, key, mode=None):
    form = state(session, key)
    return bool(form and form['start'] <= session.round <= form['end']
                and (mode is None or form['mode'] == mode))


def cast(session, key, blade, logs):
    if blade.get('id') != 'BB023':
        return
    cfg = blade.get('special_move') or {}
    mode = cfg.get('requiem_form')
    turns = {'whip': 3, 'zone': 2}.get(mode)
    if turns is None:
        return
    if not hasattr(session, 'requiem_forms'):
        session.requiem_forms = {}
    # Refresh one form instead of adding buffs or stacking recasts. Starting
    # next round makes simultaneous cast/Attack resolution independent of seat.
    session.requiem_forms[key] = {'mode': mode, 'start': session.round + 1,
                                  'end': session.round + turns}
    if mode == 'whip':
        logs.append('⚔️ **Requiem Whip** — ATK uses ATK + DEF + STM for the next **3 turns**!')
    else:
        logs.append('🌀 **Requiem Zone** — normal Attack damage converts to stamina for the next **2 turns**; Specials still deal damage!')


def apply_stats(session, key, stats):
    if active(session, key, 'whip'):
        # Derive from a fresh live snapshot. Never modify stored stats or
        # repeatedly multiply an already-converted value.
        stats['attack'] += stats.get('defense', 0) + stats.get('stamina', 0)


def absorb(session, key, move, damage, logs):
    if move != 'attack' or damage <= 0 or not active(session, key, 'zone'):
        return damage
    amount = hp_damage(damage)
    sm = session.stamina_manager
    before = sm.stamina.get(key, 0)
    # Battle stamina uses 20 stat points per resource point. Keep the same
    # unit scale and existing resource cap; absorption never heals HP.
    sm.stamina[key] = recover_resource(before, sm.cap_for(key), amount / 20,
                                       preserve_reserve=True)
    gained = sm.stamina[key] - before
    logs.append(f'🌀 **Requiem Zone** — absorbed **{amount} Attack damage**, restored **{gained:g} stamina**!')
    return 0


def expire(session, logs):
    forms = getattr(session, 'requiem_forms', {})
    for key, form in list(forms.items()):
        if session.round > form['end']:
            forms.pop(key)
            logs.append(f"🌀 **Spriggan Requiem** — Requiem {form['mode'].title()} expired.")


def tag(session, key):
    form = state(session, key)
    if not form:
        return ''
    remaining = form['end'] - max(session.round, form['start']) + 1
    return f"🌀{form['mode'].title()}({remaining}t)" if remaining > 0 else ''
