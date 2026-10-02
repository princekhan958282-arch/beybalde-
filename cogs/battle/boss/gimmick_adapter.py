"""Expose shared authored gimmick operations to the existing boss adapter.

Use AbilityEngine's actual conditions and rule gates, never a second DSL.
This bridge handles only type controls; existing BladeKit effects keep their
own lifecycle. Conditions on unprojected ability counters fail closed.
"""
from types import SimpleNamespace
from ..type_gimmicks import GIMMICK_OPS


def prepare(engine, a, b, moves, logs, *, simulate=False):
    if not (a.gimmick_rules or b.gimmick_rules):
        return
    from cogs.abilities.ability_engine import AbilityEngine
    from ..status_manager import StatusManager
    fighters = {'a': a, 'b': b}
    context = SimpleNamespace(
        hp={key: f.hp for key, f in fighters.items()},
        max_hp_per_player={key: f.max_hp for key, f in fighters.items()},
        max_hp=max(a.max_hp, b.max_hp), moves=moves, last_moves=moves,
        round=max(a.combat_round, b.combat_round) + 1,
        blades={key: f.ability_blade or {'type': f.bey_type} for key, f in fighters.items()},
        bey_levels={key: f.level for key, f in fighters.items()},
        stamina_manager=SimpleNamespace(
            stamina={key: f.sp for key, f in fighters.items()},
            max_stamina={key: f.sp_max for key, f in fighters.items()},
            gauge={key: f.gauge for key, f in fighters.items()}),
        stability_manager=SimpleNamespace(pct=lambda key: fighters[key].stability / max(1, fighters[key].max_stability)),
        type_gimmicks=engine)
    context.status = StatusManager(context)
    # Initialize the real engine so new conditions can safely read its state.
    ability = AbilityEngine(context)
    for key, f in fighters.items():
        ability.once_fired.update((key, rid) for rid in f.gimmick_once)

    unavailable = {'mode_is', 'mode_is_not', 'counter_at_least', 'counter_below',
                   'not_on_cooldown', 'enemy_debuffed_stat',
                   'my_distinct_moves_at_least', 'enemy_most_used_move_is',
                   'behind_on_points'}

    def fire(key, when, move=None, matchup='mirror'):
        f = fighters[key]
        other = 'b' if key == 'a' else 'a'
        move = move or moves[key]
        context.hp = {actor: fighter.hp for actor, fighter in fighters.items()}
        context.stamina_manager.stamina = {actor: fighter.sp for actor, fighter in fighters.items()}
        for rid, original in enumerate(f.gimmick_rules):
            rule = dict(original)
            if rule.get('when') != when:
                continue
            # The boss's existing projection has no generic counters/modes.
            # Never turn missing state into a satisfied "counter below" gate.
            ops = [op for op in rule.get('do', []) if op.get('op') in GIMMICK_OPS]
            conditions = list(rule.get('if') or []) + [cond for op in ops for cond in op.get('_if', [])]
            if any(cond.get('cond') in unavailable for cond in conditions):
                logs.append(f"Type control skipped: {rule.get('_name', f.name)} requires unprojected ability state.")
                continue
            if simulate and 'chance' in rule:
                if float(rule['chance']) < 1:
                    continue
                rule.pop('chance')
            if not ability._rule_fires(rid, rule, when, key, other, move, matchup):
                continue
            ability._run_ops({**rule, 'do': ops}, rule.get('_name', f.name),
                             key, other, move, 0, 0, logs, matchup)
        f.gimmick_once = {rid for actor, rid in ability.once_fired if actor == key}

    for key, f in fighters.items():
        if f.combat_round == 0:
            fire(key, 'setup')
        fire(key, 'on_round_start')
        fire(key, 'passive')
        for threshold in ('on_low_hp', 'on_high_hp', 'on_low_stamina',
                          'on_high_stamina', 'on_low_stability', 'on_gauge_full'):
            fire(key, threshold)
        if moves[key] not in ('attack', 'special'):
            from ..damage_rules import calc_damage
            other = 'b' if key == 'a' else 'a'
            matchup = calc_damage(moves[key], {}, {}, {}, moves[other])[2]
            result = {'lose': 'loss', 'lose_grind': 'loss'}.get(matchup, matchup)
            fire(key, 'on_' + moves[key] + '_' + result, matchup=matchup)
            fire(key, 'on_any_' + result, matchup=matchup)
            if moves[key] == 'charge':
                fire(key, 'on_charge', matchup=matchup)
    return fire
