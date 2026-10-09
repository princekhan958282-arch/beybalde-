"""Project selected rule-based avatar skills into the shared DSL interpreter.

Only avatar rules are projected; the boss and Bey kits keep their existing
resolvers. Rule counters, buffs and move history live in cloned fighter state.
"""
from copy import deepcopy
from types import SimpleNamespace
from cogs.abilities.ability_engine import AbilityEngine
from ..status_manager import StatusManager
from ..damage_rules import calc_damage

ENGINE_STATE = ('counters', 'once_fired', 'primed_bonus', 'crit_chance_bonus',
                'crit_damage_mult', 'cooldowns', 'primed_hit_bonus', 'primed_crit')


def project(fighters, moves, *, simulate=False):
    if not any(any(s.get('rules') for s in f.avatar_card.get('skills', []))
               for f in fighters.values()):
        return None
    return RuleBridge(fighters, moves, simulate=simulate)


class RuleBridge:
    def __init__(self, fighters, moves, *, simulate=False):
        self.fighters, self.moves, self.simulate = fighters, moves, simulate
        self.logs = []
        self.context = c = SimpleNamespace(
            round=max(f.combat_round for f in fighters.values()) + 1,
            moves=moves, hp={}, max_hp=max(f.max_hp for f in fighters.values()),
            max_hp_per_player={k: f.max_hp for k, f in fighters.items()},
            blades={k: {'name': f.name, 'type': f.bey_type, 'abilities': [],
                        'stats': {'attack': f.attack, 'defense': f.defense,
                                  'stamina': f.stamina_stat}}
                    for k, f in fighters.items()},
            avatar_cards={k: f.avatar_card for k, f in fighters.items()},
            avatar_skill_slots={k: f.avatar_skill_slot for k, f in fighters.items()},
            avatar_bonuses={k: f.avatar_bonuses for k, f in fighters.items()},
            move_counts={k: f.avatar_rule_state.setdefault('moves', {})
                         for k, f in fighters.items()},
            stat_mult={k: 1 for k in fighters},
            chain_handler=SimpleNamespace(queue=lambda *args: None))
        c.stamina_manager = SimpleNamespace(stamina={}, gauge={}, max_stamina={
            k: f.sp_max for k, f in fighters.items()})
        c.stability_manager = SimpleNamespace(
            stability={}, max={k: f.max_stability for k, f in fighters.items()})
        c.stability_manager.pct = lambda k: c.stability_manager.stability[k] / max(1, c.stability_manager.max[k])
        def stability(key, delta):
            c.stability_manager.stability[key] = max(0, min(
                c.stability_manager.max[key], c.stability_manager.stability[key] + delta))
            return []
        c.stability_manager._apply = stability
        c.status = StatusManager(c)
        self.refresh()
        for f in fighters.values():
            for name, value in f.avatar_rule_state.get('status', {}).items():
                # Each snapshot stores just that side's entries in status maps.
                if not hasattr(c.status, name):
                    setattr(c.status, name, {})
                getattr(c.status, name).update(deepcopy(value))
        c.ability = self.ability = AbilityEngine(c)
        for f in fighters.values():
            for name, value in f.avatar_rule_state.get('engine', {}).items():
                target = getattr(self.ability, name)
                target.update(deepcopy(value))
        c.effective_stats_for = lambda k: {'attack': fighters[k].eff_attack,
                                         'defense': fighters[k].eff_defense,
                                         'stamina': fighters[k].eff_stamina}
        if simulate:
            original = self.ability._rule_fires
            def expected_gate(rid, rule, when, key, other, move, matchup):
                chance = rule.get('chance')
                if chance is not None and chance < 1:
                    return False  # Search must not roll stochastic rule gates.
                return original(rid, rule, when, key, other, move, matchup)
            self.ability._rule_fires = expected_gate

    def refresh(self):
        for key, f in self.fighters.items():
            self.context.hp[key] = f.hp
            self.context.stamina_manager.stamina[key] = f.sp
            self.context.stamina_manager.gauge[key] = f.gauge
            self.context.stability_manager.stability[key] = f.stability

    def sync(self):
        c = self.context
        for key, f in self.fighters.items():
            f.hp = c.hp[key]
            f.sp = c.stamina_manager.stamina[key]
            f.gauge = c.stamina_manager.gauge[key]
            f.stability = c.stability_manager.stability[key]
            f.avatar_rule_stats = {stat: c.status.get_buff_bonus(key, stat)
                                   for stat in ('attack', 'defense', 'stamina')}
            f.avatar_rule_pierce = c.status.get_duration('ignore_defense_turns', key)

    def fire(self, key, event, damage=0, move=None, matchup='mirror'):
        other = 'b' if key == 'a' else 'a'
        damage, _ = self.ability._fire(
            event, key, other, self.context.blades[key], move or self.moves[key],
            matchup, damage, 0, self.logs)
        self.sync()
        return damage

    def begin(self):
        self.refresh()
        for key, f in self.fighters.items():
            self.context.status.tick_buffs(key, self.logs)
            self.context.status.tick_universal(key)
            tally = self.context.move_counts[key]
            tally[self.moves[key]] = tally.get(self.moves[key], 0) + 1
            if f.combat_round == 0:
                self.fire(key, 'setup')
        # Passive and thresholds run in the action phase, as in PvP. Running
        # them here as well would double their buffs/stability each round.
        for key, move in self.moves.items():
            if move not in ('attack', 'special'):
                self.offensive(key, move, 0)

    def offensive(self, key, move, damage):
        self.refresh()
        other = 'b' if key == 'a' else 'a'
        matchup = 'win' if move == 'special' else calc_damage(
            move, {}, {}, {}, self.moves[other])[2]
        result = {'lose': 'loss', 'lose_grind': 'loss'}.get(matchup, matchup)
        if move == 'attack' and damage > 0:
            armed = self.ability.primed_crit.pop(key, 0)
            if armed:
                self.context.status.guaranteed_crit_turns[key] = max(
                    self.context.status.guaranteed_crit_turns.get(key, 0), armed)
        if move in ('attack', 'special') and damage > 0:
            damage += self.ability.primed_hit_bonus.pop(key, 0)
        events = ['passive', 'on_low_hp', 'on_high_hp', 'on_low_stamina',
                  'on_high_stamina', 'on_low_stability', 'on_gauge_full',
                  'on_' + move + '_' + result]
        if result in ('win', 'loss'):
            events.append('on_any_' + result)
        if matchup == 'mirror':
            events.append('on_mirror')
        if move in ('special', 'charge'):
            events.append('on_' + move)
        for event in events:
            damage = self.fire(key, event, damage, move, matchup)
        if move == 'special':
            damage += self.ability.primed_bonus.pop(key, 0)
        if move == 'attack' and damage > 0:
            chance = self.ability.crit_chance_bonus.get(key, 0)
            forced = self.context.status.guaranteed_crit_turns.get(key, 0)
            if forced:
                damage *= 2
                self.context.status.guaranteed_crit_turns[key] = forced - 1
            elif chance > 0:
                import random
                mult = self.ability.crit_damage_mult.get(key, 1.5)
                if self.simulate:
                    damage *= 1 + min(.95, chance) * (mult - 1)
                elif random.random() < min(.95, chance):
                    damage *= mult
        return damage

    def defensive(self, key, other, move, damage):
        self.refresh()
        matchup = 'win' if move == 'special' else calc_damage(
            move, {}, {}, {}, self.moves[key])[2]
        damage, _ = self.ability._fire_defensive(
            key, other, self.context.blades[key], move, matchup,
            damage, 0, self.logs)
        self.sync()
        return damage

    def end(self):
        for key, f in self.fighters.items():
            f.avatar_rule_state['status'] = {
                name: {k: deepcopy(v) for k, v in value.items()
                       if k == key or isinstance(k, tuple) and k[0] == key}
                for name, value in vars(self.context.status).items()
                if isinstance(value, dict)}
            f.avatar_rule_state['engine'] = {
                name: ({k: deepcopy(v) for k, v in getattr(self.ability, name).items()
                        if k == key or isinstance(k, tuple) and k[0] == key}
                       if isinstance(getattr(self.ability, name), dict)
                       else {item for item in getattr(self.ability, name) if item[0] == key})
                for name in ENGINE_STATE}
