"""Project boss resources into the same Draciel runtime used by PvP."""
from types import SimpleNamespace
from ..draciel import DracielRuntime, version
from ..status_manager import StatusManager


def project(a, b, engine, moves, avatar_bridge=None):
    fighters = {'a': a, 'b': b}
    if not any(version(f.ability_blade) or f.draciel_state for f in fighters.values()):
        return None
    context = SimpleNamespace(
        round=max(a.combat_round, b.combat_round) + 1,
        hp={k:f.hp for k,f in fighters.items()},
        max_hp_per_player={k:f.max_hp for k,f in fighters.items()},
        blades={k:f.ability_blade or {'name':f.name, 'type':f.bey_type} for k,f in fighters.items()},
        type_gimmicks=engine)
    context.status = avatar_bridge.og.s.status if avatar_bridge else StatusManager(context)
    context.stamina_manager = SimpleNamespace()
    context.stability_manager = SimpleNamespace()
    def stability(key, delta):
        f = fighters[key]
        f.stability = max(0, min(f.max_stability, f.stability + delta))
        return []
    context.stability_manager._apply = stability
    context.effective_stats_for = lambda k: {
        'attack':fighters[k].eff_attack, 'defense':fighters[k].eff_defense,
        'stamina':fighters[k].eff_stamina, 'level':fighters[k].level}
    context.attack_manager = SimpleNamespace(_apply_passive_reduction=lambda key, blade, damage, logs: (
        max(0,damage*(1-min(.95,max(0,fighters[key].incoming_reduction)))-max(0,fighters[key].incoming_flat_reduction)),logs))
    def absorb(key, damage):
        state = fighters[key].state
        return state.absorb(damage)[0] if state and hasattr(state, 'absorb') and damage > 0 else damage
    context.rider_absorb = absorb
    runtime = DracielRuntime(context, {k:f.draciel_state for k,f in fighters.items()})
    return Bridge(fighters, runtime)


class Bridge:
    def __init__(self, fighters, runtime):
        self.fighters, self.runtime = fighters, runtime

    def end(self, moves, actual_a, actual_b, logs):
        self.runtime.s.hp = {k:f.hp for k,f in self.fighters.items()}
        self.runtime.committed('a', 'b', moves['a'], actual_b)
        self.runtime.committed('b', 'a', moves['b'], actual_a)
        self.runtime.end(moves, logs)
        for key, f in self.fighters.items():
            f.hp = self.runtime.s.hp[key]
            f.draciel_state['clock'] = self.runtime.s.round + 1
