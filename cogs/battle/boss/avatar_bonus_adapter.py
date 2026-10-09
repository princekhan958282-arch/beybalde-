"""Run the shared avatar bonus hooks against boss fighter snapshots.

The live path uses avatar_combat's rolls. Search uses expected values without
rolling dice or consuming the live fighter's counters/immortality window.
"""
from types import SimpleNamespace
from .. import avatar_combat as AVC


class BonusBridge:
    def __init__(self, fighters, moves, *, simulate=False):
        self.fighters = fighters
        self.moves = moves
        self.simulate = simulate
        self.logs = []
        self.crit_refunds = {key: 0 for key in fighters}
        self.context = SimpleNamespace(
            round=max(f.combat_round for f in fighters.values()) + 1,
            avatar_bonuses={k: f.avatar_bonuses for k, f in fighters.items()},
            moves=moves,
            _damage_bypass={k: f.avatar_rule_pierce > 0 for k, f in fighters.items()},
            blades={k: {'special_move': {'true_damage': f.special_true_damage,
                                        'ignores_defense': f.special_ignores_defense}}
                    for k, f in fighters.items()},
            hp={k: f.hp for k, f in fighters.items()},
            _avatar_hit_counts={k: f.avatar_bonus_state.get('hits', 0)
                                for k, f in fighters.items()},
            _avatar_immortal={k: f.avatar_bonus_state['immortal']
                              for k, f in fighters.items()
                              if 'immortal' in f.avatar_bonus_state})

    def offensive(self, key, move, damage, natural_crit=False):
        f = self.fighters[key]
        av = f.avatar_bonuses
        if av is None or damage <= 0 or move != 'attack':
            return damage
        nth, logs = AVC.nth_hit_bonus(self.context, key, f.eff_attack)
        self.logs.extend(logs)
        damage += nth
        if natural_crit:
            self.crit_refunds[key] = av.gauge_on_crit
        elif self.simulate:
            damage *= 1 + max(0, min(1, av.crit_percent))
            self.crit_refunds[key] = av.gauge_on_crit * max(0, min(1, av.crit_percent))
        elif AVC.roll_extra_crit(self.context, key, False):
            damage *= 2
            self.crit_refunds[key] = av.gauge_on_crit
            self.logs.append('💥 **Avatar** — critical strike! Damage ×2!')
        return damage

    def incoming(self, defender, attacker, damage):
        self.context._damage_bypass[attacker] = self.fighters[attacker].avatar_rule_pierce > 0
        av = self.fighters[defender].avatar_bonuses
        if av is None or damage <= 0:
            return damage, 0
        if not self.simulate:
            damage, counter, logs = AVC.absorb_incoming(
                self.context, defender, attacker, damage)
            self.logs.extend(logs)
            return damage, counter
        # Expected outcomes let the counter AI evaluate the same effects as
        # the real exchange without random rolls corrupting its move ranking.
        dodge = max(0, min(av.DODGE_CAP, av.dodge_chance))
        resistance = max(0, min(1, av.resistance_damage_percent))
        reduced = damage * (1 - resistance)
        special = self.context.blades[attacker]['special_move']
        guard = self.moves[defender] == 'defense' and not (
            self.fighters[attacker].avatar_rule_pierce > 0 or
            self.moves[attacker] == 'special' and (
                special['true_damage'] or special['ignores_defense']))
        counter = .5 * max(0, min(1, av.counter_chance)) * (
            dodge * damage + (1 - dodge) * reduced * bool(resistance or guard))
        return (1 - dodge) * reduced, counter

    def lethal(self, key, incoming):
        self.context.hp = {k: f.hp for k, f in self.fighters.items()}
        incoming, logs = AVC.guard_lethal(self.context, key, incoming)
        self.logs.extend(logs)
        return incoming

    def charge(self, key, amount):
        av = self.fighters[key].avatar_bonuses
        if av is not None:
            return av.apply_charge_bonus(amount)
        return amount

    def finish(self):
        for key, fighter in self.fighters.items():
            fighter.avatar_bonus_state['hits'] = self.context._avatar_hit_counts.get(key, 0)
            if key in self.context._avatar_immortal:
                fighter.avatar_bonus_state['immortal'] = self.context._avatar_immortal[key]
