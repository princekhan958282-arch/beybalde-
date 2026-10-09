"""Project boss resources into the Phoenix runtime without sharing AI clones."""
from types import SimpleNamespace

from ..dranzer import DranzerRuntime, version
from ..status_manager import StatusManager


def project(a, b, engine, avatar_bridge=None):
    fighters = {'a': a, 'b': b}
    if not any(version(f.ability_blade) or f.dranzer_state.get('transfers')
               or f.dranzer_state.get('shred_until', -1) >= max(a.combat_round, b.combat_round) + 1
               for f in fighters.values()): return None
    context = SimpleNamespace(
        round=max(a.combat_round, b.combat_round) + 1,
        hp={k: f.hp for k, f in fighters.items()},
        max_hp_per_player={k: f.max_hp for k, f in fighters.items()},
        blades={k: f.ability_blade or {'name': f.name, 'type': f.bey_type} for k, f in fighters.items()},
        type_gimmicks=engine)
    context.status = avatar_bridge.og.s.status if avatar_bridge else StatusManager(context)
    identities = {f.dranzer_state.get('_source_key', k): k for k, f in fighters.items()}
    for key, f in fighters.items():
        for entry in f.dranzer_state.get('transfers', []):
            entry['other'] = identities.get(entry['other'], entry['other'])
        for entry in f.dranzer_state.get('burns', []):
            entry['target'] = identities.get(entry['target'], entry['target'])
        for attr, value in f.dranzer_state.get('statuses', {}).items():
            getattr(context.status, attr)[key] = value
    context.stamina_manager = SimpleNamespace(
        stamina={k: f.sp for k, f in fighters.items()}, cap_for=lambda k: fighters[k].sp_max)
    context.effective_stats_for = lambda k: {
        'attack': fighters[k].eff_attack + context.status.get_buff_bonus(k, 'attack'),
        'defense': fighters[k].eff_defense + context.status.get_buff_bonus(k, 'defense'),
        'stamina': fighters[k].eff_stamina, 'level': fighters[k].level}
    return Bridge(fighters, DranzerRuntime(context, {k: f.dranzer_state for k, f in fighters.items()}))


class Bridge:
    def __init__(self, fighters, runtime):
        self.fighters, self.runtime = fighters, runtime
        self.parts = {}

    def sync(self):
        self.runtime.s.hp = {k: f.hp for k, f in self.fighters.items()}
        self.runtime.s.stamina_manager.stamina = {k: f.sp for k, f in self.fighters.items()}

    def commit(self, key, damage, logs, parts=None):
        self.sync()
        actual = self.runtime.commit_damage(key, damage, logs, parts)
        self.fighters[key].hp = self.runtime.s.hp[key]
        return actual

    def terminal(self, key, damage, logs):
        self.sync()
        actual = self.runtime.terminal(key, damage, logs)
        self.fighters[key].hp = self.runtime.s.hp[key]
        return actual

    def end(self, moves, actual_a, actual_b, logs):
        self.sync()
        self.runtime.committed('a', 'b', moves['a'], actual_b)
        self.runtime.committed('b', 'a', moves['b'], actual_a)
        self.runtime.end(moves, logs)
        for key, f in self.fighters.items():
            f.hp = self.runtime.s.hp[key]
            f.sp = self.runtime.s.stamina_manager.stamina[key]
            f.dranzer_state['clock'] = self.runtime.s.round + 1
            f.dranzer_state['statuses'] = {
                attr: getattr(self.runtime.s.status, attr).get(key, [] if attr == 'active_buffs' else 0)
                for attr in ('active_buffs', 'silenced_turns', 'burn_stacks', 'burn_duration', 'burn_dmg', 'ability_2_disabled')}
