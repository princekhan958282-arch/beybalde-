"""Record repeat-action battle evidence; optionally sweep the official roster.

Run from the repository root:
  python -m tools.audit_repeated_battles --out /tmp/battle-audit.json --all
No Discord login or production profile writes. Uses the real async factory,
round body, owned progression, equipment and avatar handlers.
"""
import argparse
import asyncio
import itertools
import json
from pathlib import Path
from dataclasses import replace

from tools.test_repeated_battle_audit import session, round_trace
from cogs.avatar import NULL_BONUSES
from utils.database import load_beyblades


def serializable(value):
    if isinstance(value, dict):
        return {':'.join(map(str, k)) if isinstance(k, tuple) else str(k): serializable(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(v) for v in value]
    return value


async def evidence():
    cases = {}
    s, a, e = await session('Janus Bahamut', 'Noctilune', level=100)
    cases['janus_loss_special_attack'] = []
    for move, reply in [('attack', 'defense')] * 4 + [('special', 'charge'), ('attack', 'charge')]:
        cases['janus_loss_special_attack'].append(await round_trace(s, a, e, move, reply))
    for name, key in [('Master Diabolos', 'master_one_win'), ('Aetherion Drakos org', 'star_one_round')]:
        s, a, e = await session(name, level=1)
        cases[key] = [await round_trace(s, a, e, 'special', 'charge')]
    for name, key in [('Cobalt Dragoon', 'cobalt_dodge'), ('Blood Dragon', 'blood_dodge')]:
        s, a, e = await session(name, level=1)
        s.hp[a] -= 100
        s.avatar_bonuses[e] = replace(NULL_BONUSES, dodge_chance=1)
        cases[key] = [await round_trace(s, a, e, 'attack', 'charge', roll=0)]
    s, a, e = await session('Cure Black', level=1)
    s.hp[a] -= 100
    cases['cure_cooldown'] = [await round_trace(s, a, e, 'special', 'charge') for _ in range(5)]
    s, a, e = await session('Janus Bahamut', 'Noctilune', level=100)
    s.status.silenced_turns[a] = 3
    cases['janus_silence'] = [await round_trace(s, a, e, 'attack', 'defense')]
    s, a, e = await session('Victory Valkyrie', 'Twin Nemesis', level=1)
    s.avatar_bonuses[e] = replace(NULL_BONUSES, dodge_chance=1)
    cases['twin_dodged_reflect'] = [await round_trace(s, a, e, 'attack', 'charge', roll=0)]
    s, a, e = await session('Janus Bahamut', 'Noctilune', level=1)
    s.ability.reflect_windows[a] = [50, 3]
    s.ability.reflect_windows[e] = [50, 3]
    cases['mirror_reflect_windows'] = [await round_trace(s, a, e, 'attack', 'attack')]
    s, a, e = await session('Blood Dragon', level=1)
    s.avatar_bonuses[e] = replace(NULL_BONUSES, dodge_chance=1)
    cases['dodged_special_gauge'] = [await round_trace(s, a, e, 'special', 'charge', roll=0)]
    return cases


async def sweep():
    records, errors = [], []
    roster = load_beyblades()
    variants = [(name, None) for name in roster]
    variants += [(name, mode) for name, b in roster.items() for mode in b.get('spin_modes', {})]
    for (name, mode), level, reverse, finisher in itertools.product(variants, (1, 100), (False, True), ('attack', 'special')):
        definition = roster[name]
        if mode:
            from utils.spin_mode import resolve
            definition = resolve({'spin_mode': {name: mode}}, definition)
        try:
            s, a, e = await session(definition, 'Noctilune', level=level, reverse=reverse)
            trajectory = []
            for move, reply in [('attack', 'defense')] * 4 + [(finisher, 'charge')]:
                if s.finished:
                    break
                # Keep enough stamina to inspect the fifth action. Costs and
                # post-action resource values are still recorded by the round.
                s.stamina_manager.stamina[a] = max(s.stamina_manager.stamina[a], 20)
                trace = await round_trace(s, a, e, move, reply)
                trajectory.append({'round': trace['round'], 'damage': trace['damage'][a],
                    'attack': trace['stats'][a]['attack'], 'defense': trace['stats'][e]['defense'],
                    'hp_delta': trace['hp_before'][e] - trace['hp_after'][e],
                    'stacks': trace['stacks'], 'cooldowns': trace['cooldowns']})
            records.append({'name': name, 'mode': mode, 'level': level,
                'reverse': reverse, 'finisher': finisher, 'trajectory': trajectory,
                'completed': len(trajectory) == 5, 'finished': s.finished})
        except Exception as exc:
            errors.append({'name': name, 'mode': mode, 'level': level, 'reverse': reverse,
                           'finisher': finisher, 'error': repr(exc)})
    return {'roster_count': len(roster), 'variant_count': len(variants),
            'sessions': len(records), 'errors': errors, 'records': records}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--all', action='store_true')
    args = parser.parse_args()
    result = {'cases': await evidence()}
    if args.all:
        result['sweep'] = await sweep()
    args.out.write_text(json.dumps(serializable(result), indent=2) + '\n')
    print(f'Evidence written: {args.out}')
    if args.all:
        result = result['sweep']
        print(f"Roster: {result['roster_count']}; variants: {result['variant_count']}; sessions: {result['sessions']}; errors: {len(result['errors'])}")


if __name__ == '__main__':
    asyncio.run(main())
