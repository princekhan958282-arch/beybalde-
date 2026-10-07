"""Shared Draciel kits for PvP, Story and projected boss fighters.

Defensive Specials open at the simultaneous round boundary. All timers use
absolute rounds; multi-hit moves aggregate actual HP damage once per action.
Terminal retaliation never dispatches crit, lifesteal or reactive abilities.
"""
import math

VERSIONS = {'F', 'V', 'V2', 'G', 'MS'}


def version(blade):
    value = (blade or {}).get('draciel_kit')
    return value if value in VERSIONS else None


def cost_surcharge(state, round_number, move):
    pressure = (state.get('pressure_active', False) if state.get('pressure_round') == round_number
                else state.get('pressure_next', False))
    return (.4 if pressure else 0) + (.6 if move in ('attack', 'special') and state.get('gravity_until', -1) >= round_number else 0)


def stat_multiplier(blade, state, round_number, stat, enabled=True):
    kind = version(blade)
    if stat == 'defense' and kind and enabled:
        active = state.get('special', {}).get('until', -1) >= round_number
        return 1 + .06 * state.get('shell', 0) + (.20 if kind == 'G' and active else 0)
    if stat == 'attack' and state.get('viper_until', -1) >= round_number:
        return .90
    return 1


class DracielRuntime:
    def __init__(self, session, states=None):
        self.s = session
        self.states = states if states is not None else {}
        for key in getattr(session, 'blades', {}):
            self.states.setdefault(key, {})
        self.hits = {}
        self.stats = {}
        self.completed = None
        for manager in (getattr(session, 'stamina_manager', None),
                        getattr(session, 'stability_manager', None)):
            if manager is not None:
                manager.draciel_runtime = self

    def owned(self, key):
        return version(self.s.blades.get(key))

    def enabled(self, key):
        return self.owned(key) and not self.s.status.is_silenced(key)

    def active(self, key):
        effect = self.states.get(key, {}).get('special', {})
        return effect if effect.get('until', -1) >= self.s.round else {}

    def cooldown_left(self, key):
        return max(0, self.states.get(key, {}).get('special_ready', 0) - self.s.round)

    def stat_multiplier(self, key, stat):
        return stat_multiplier(self.s.blades.get(key), self.states.get(key, {}), self.s.round, stat, self.enabled(key))

    def begin(self, moves, logs):
        self.hits = {}
        for key, d in self.states.items():
            d['clock'] = self.s.round
            d['pressure_active'] = d.pop('pressure_next', False)
            d['pressure_round'] = self.s.round
            d.pop('reverse_used', None)
        # Open both Specials before any effective stats or damage are read.
        activated = set()
        for key, move in moves.items():
            if move != 'special' or not self.enabled(key) or self.cooldown_left(key):
                continue
            d = self.states[key]
            activated.add(key)
            d['special_ready'] = self.s.round + 5  # skip the next four rounds
            d['special'] = {'until': self.s.round + 1, 'stored': 0.0}
            if self.owned(key) == 'G':
                for other in moves:
                    if other != key:
                        self.states[other]['gravity_until'] = self.s.round + 1
            logs.append(f"🐢 **{self.s.blades[key]['special_move']['name']}** activates for 2 rounds!")
            if self.owned(key) == 'MS':
                buffs = self.s.status.active_buffs.get(key, [])
                debuff = next((b for b in buffs if b['stat'] == 'defense' and b['amount'] < 0), None)
                if debuff:
                    buffs.remove(debuff)
                    logs.append('💧 Draciel Aqua Shield removes one DEF debuff!')
                logs.extend(self.s.stability_manager._apply(key, 6))
        from .purification import effective_stats
        self.stats = {key: effective_stats(self.s, key) for key in moves}
        for key in moves:
            if self.owned(key) == 'F' and key in activated:
                self.active(key)['shield'] = min(180, math.floor(self.stats[key]['defense'] * .60))

    def cost(self, key, move, base):
        return round(base + cost_surcharge(self.states.get(key, {}), self.s.round, move), 2)

    def outgoing(self, key, move, damage):
        return damage * .90 if self.states.get(key, {}).get('pressure_active') else damage

    def mitigate(self, attacker, defender, move, damage, logs, *, true_damage=False):
        if true_damage or damage <= 0 or move not in ('attack', 'special') or not self.enabled(defender):
            return damage
        d = self.states[defender]
        effect = self.active(defender)
        kind = self.owned(defender)
        pct = 0
        if effect:
            if kind == 'V2': pct = .35
            elif kind == 'V': pct = .25 if move == 'attack' else .15
            elif kind == 'MS': pct = .20 if move == 'attack' else .40
        if pct:
            saved = damage * pct
            damage -= saved
            if kind == 'V2': effect['stored'] = min(100, effect['stored'] + saved * .40)
            logs.append(f'🛡️ **Draciel {kind}** wall prevents {math.floor(saved)} damage!')
        if kind == 'MS' and d.get('reverse_round') == self.s.round:
            damage *= .80
            d['reverse_used'] = True
            logs.append('💧 **Draciel’s Reverse Defense** reduces damage by 20%!')
        if kind == 'F' and effect.get('shield', 0) > 0:
            absorbed = min(damage, effect['shield'])
            effect['shield'] -= absorbed
            damage -= absorbed
            logs.append(f'🏰 **Draciel Fortress Defense** absorbs {math.floor(absorbed)} damage!')
        return damage

    def stability_delta(self, key, delta, action=False):
        if delta < 0 and not action and self.owned(key) == 'F' and self.enabled(key) and self.active(key).get('shield', 0) > 0:
            return -math.ceil(abs(delta) * .60)
        return delta

    def committed(self, attacker, defender, move, actual):
        if actual > 0 and move in ('attack', 'special'):
            self.hits[(attacker, defender, move)] = self.hits.get((attacker, defender, move), 0) + actual

    def end(self, moves, logs):
        if self.completed == self.s.round:
            return
        self.completed = self.s.round
        pending = []
        for key, move in moves.items():
            if not self.enabled(key) or self.s.hp[key] <= 0:
                continue
            other = next(k for k in moves if k != key)
            enemy_move = moves[other]
            d, kind = self.states[key], self.owned(key)
            defense = self.stats[key]['defense']
            actual = self.hits.get((other, key, enemy_move), 0)
            kinetic_return = self.s.type_gimmicks.returns_damage(other, key, enemy_move)
            if actual > 0 and kind == 'V2':
                d['shell'] = min(4, d.get('shell', 0) + 1)
                logs.append(f"🐢 **Draciel’s Reinforced Shell** — {d['shell']}/4 stacks (+{6*d['shell']}% DEF)!")
            if actual > 0 and kind == 'MS' and enemy_move == 'special' and d.get('reverse_ready', 0) <= self.s.round:
                d['reverse_round'] = self.s.round + 1
                d['reverse_ready'] = self.s.round + 3
            if move == 'defense' and enemy_move == 'attack' and d.get('ability_ready', 0) <= self.s.round:
                if kind == 'F':
                    from .combat_rules import recover_hp
                    self.s.hp[key], healed = recover_hp(self.s.hp[key], self.s.max_hp_per_player[key], min(35, math.floor(defense*.08)))
                    logs.extend(self.s.stability_manager._apply(key, 3))
                    logs.append(f'🏰 **Draciel’s Fortress Foundation** restores {healed} HP!')
                elif kind == 'V':
                    if not kinetic_return:
                        pending.append((key, other, min(50, defense*.15), 'Draciel’s Viper Rebound'))
                    self.states[other]['viper_until'] = self.s.round + 1
                elif kind == 'G':
                    self.states[other]['pressure_next'] = True
                    logs.append('🌍 **Draciel’s Gravity Anchor** — enemy next move costs +0.4 stamina and deals 10% less damage!')
                d['ability_ready'] = self.s.round + 2
            effect = self.active(key)
            if effect and kind == 'V' and actual > 0 and not kinetic_return:
                pending.append((key, other, min(45, actual*.25), 'Draciel Viper Wall'))
            if effect and effect['until'] == self.s.round:
                if kind == 'V2': pending.append((key, other, defense*.40 + effect['stored'], 'Draciel Heavy Viper Wall'))
                elif kind == 'G': pending.append((key, other, defense*.65, 'Draciel Gravity Control'))
                d.pop('special', None)
        # Snapshot mitigated retaliation before either side's HP changes.
        events = [(other, self.counter_damage(key, other, raw, logs), label) for key, other, raw, label in pending]
        from .combat_rules import damage_hp
        for other, damage, label in events:
            self.s.hp[other], actual = damage_hp(self.s.hp[other], damage)
            logs.append(f'↩️ **{label}** deals {actual} counter damage!')

    def counter_damage(self, key, other, raw, logs):
        from .purification import effective_stats
        damage = raw * 100 / max(1, effective_stats(self.s, other)['defense'])
        damage, _ = self.s.attack_manager._apply_passive_reduction(other, self.s.blades[other], damage, [])
        damage = self.s.type_gimmicks.mitigate(key, other, 'special', damage)
        if self.s.status.is_invulnerable(other): return 0
        absorb = getattr(self.s, 'rider_absorb', None)
        if absorb: damage = absorb(other, damage)
        damage -= self.s.status.absorb_shield(other, math.floor(damage))
        # Retaliation can be shielded, but cannot trigger another retaliation.
        effect = self.active(other)
        if self.owned(other) == 'F' and effect.get('shield', 0) > 0:
            absorbed = min(damage, effect['shield'])
            effect['shield'] -= absorbed
            damage -= absorbed
        return max(0, damage)
