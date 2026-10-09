"""Phoenix kits shared by live PvP/Story sessions and boss/search projections.

Rounds are absolute: a two-round grant lasts through the next two rounds;
a cooldown of N blocks the next N rounds. Terminal damage never dispatches
offensive procs. Stat theft stores equal, fixed transfers with individual expiry.
"""
import math

from .combat_rules import damage_hp, recover_hp, recover_resource

KINDS = {'G', 'Black', 'F', 'V', 'V2', 'GT', 'MS'}
SPECIAL_COOLDOWNS = {'G': 4, 'Black': 5, 'F': 3, 'V': 4, 'V2': 4, 'GT': 4, 'MS': 5}


def version(blade):
    blade = blade or {}
    kind = blade.get('dranzer_kit')
    expected = 'Black Dranzer' if kind == 'Black' else 'Dranzer ' + str(kind)
    return kind if kind in KINDS and blade.get('name') == expected else None


def adjust_stat(blade, state, round_number, stat, hp, maximum, value, enabled=True, secondary=True):
    kind = version(blade) if enabled else None
    pct = 0
    if stat == 'attack':
        if kind == 'G': pct += .04 * state.get('gravity', 0)
        if kind == 'GT' and state.get('turbo_until', -1) >= round_number:
            pct += .06 * state.get('turbo', 0)
        if kind == 'V2' and secondary and state.get('rebirth_until', -1) >= round_number: pct += .15
    if stat == 'defense':
        if kind == 'G' and secondary and state.get('rotation_until', -1) >= round_number: pct += .10
        if kind == 'V' and secondary and state.get('guard_until', -1) >= round_number: pct += .25
        if state.get('shred_until', -1) >= round_number: pct -= .12
    if kind == 'MS' and secondary and stat in ('attack', 'defense'):
        lost = max(0, 1 - hp / max(1, maximum))
        tiers = min(4, math.floor((lost + 1e-9) / .20))
        pct += tiers * (.05 if stat == 'attack' else .04)
    delta = sum(e['amount'] for e in state.get('transfers', [])
                if e['stat'] == stat and e['until'] >= round_number)
    return max(0, value * (1 + pct) + delta)


def stamina_cost(blade, state, round_number, move, base, enabled=True, secondary=True):
    if not enabled or version(blade) != 'GT' or state.get('turbo_until', -1) < round_number:
        return base
    stacks = (state.get('spent_turbo', 0) if move == 'special' and state.get('spent_round') == round_number
              else state.get('turbo', 0))
    return round((base + (1 if secondary and stacks == 3 and move == 'attack' else 0)) * (1 - .05 * stacks), 2)


class DranzerRuntime:
    def __init__(self, session, states=None):
        self.s = session
        self.states = states if states is not None else {}
        for key in getattr(session, 'blades', {}): self.states.setdefault(key, {})
        self.hits = {}
        self.specials = {}
        self.completed = None
        sm = getattr(session, 'stamina_manager', None)
        if sm is not None: sm.dranzer_runtime = self

    def owned(self, key):
        return version(self.s.blades.get(key))

    def enabled(self, key):
        return self.owned(key) and not self.s.status.is_silenced(key)

    def secondary(self, key):
        return self.enabled(key) and not self.s.status.ability_2_disabled.get(key, False)

    def stats(self, key):
        from .purification import effective_stats
        return effective_stats(self.s, key)

    def stat(self, key, stat, value):
        return adjust_stat(self.s.blades[key], self.states[key], self.s.round,
                           stat, self.s.hp[key], self.s.max_hp_per_player[key], value,
                           self.enabled(key), self.secondary(key))

    def cost(self, key, move, base):
        return stamina_cost(self.s.blades[key], self.states[key], self.s.round, move, base,
                            self.enabled(key), self.secondary(key))

    def cooldown_left(self, key):
        return max(0, self.states.get(key, {}).get('special_ready', 0) - self.s.round)

    def begin(self, moves, logs):
        self.hits, self.specials = {}, {}
        for key, d in self.states.items():
            d['transfers'] = [e for e in d.get('transfers', []) if e['until'] >= self.s.round]
            if d.get('turbo_until', -1) < self.s.round: d['turbo'] = 0
            if moves.get(key) in ('defense', 'stamina'): d['volcano'] = 0
            self.threshold(key, logs)
        # Snapshot both actors before either Special spends its stacks.
        attacks = {key: self.stats(key)['attack'] for key in moves}
        for key, move in moves.items():
            kind, d = self.owned(key), self.states[key]
            if move != 'special' or not kind or self.cooldown_left(key): continue
            d['special_ready'] = self.s.round + SPECIAL_COOLDOWNS[kind] + 1
            self.specials[key] = {'attack': attacks[key], 'gravity': d.get('gravity', 0),
                                  'turbo': d.get('turbo', 0), 'energy': d.get('energy', 0),
                                  'volcano': d.get('volcano', 0)}
            if kind == 'G': d['gravity'] = 0
            if kind == 'GT':
                d['spent_turbo'], d['spent_round'] = d.get('turbo', 0), self.s.round
                d['turbo'] = 0
            if kind == 'Black': d['energy'] = 0
            if kind == 'V':
                other = next(k for k in moves if k != key)
                buffs = self.s.status.active_buffs.get(other, [])
                removable = [b for b in buffs if b['stat'] == 'defense' and b['amount'] > 0
                             and b.get('rounds_left', 0) < 99 and b.get('removable', True)]
                candidates = [(b['amount'], 'status', b) for b in removable]
                defense = self.stats(other)['defense']
                for field, pct in (('rotation_until', .10), ('guard_until', .25)):
                    if self.states[other].get(field, -1) >= self.s.round:
                        candidates.append((defense * pct / (1 + pct), 'phoenix', field))
                if candidates:
                    _, source, effect = max(candidates, key=lambda c: c[0])
                    if source == 'status': buffs.remove(effect)
                    else: self.states[other].pop(effect, None)
                    logs.append('🔥 Volcanic Eruption removes the strongest removable DEF buff!')

    def outgoing(self, key, move, damage):
        if damage <= 0 or not self.enabled(key) or move != 'attack': return damage
        kind, d = self.owned(key), self.states[key]
        if kind == 'V': damage *= 1 + .07 * d.get('volcano', 0)
        if kind == 'GT' and self.secondary(key) and d.get('turbo', 0) == 3: damage *= 1.18
        return damage

    def attack_stats(self, key, defender_stats):
        if self.enabled(key) and self.owned(key) == 'MS':
            return {**defender_stats, 'defense': defender_stats.get('defense', 1) * .90}
        return defender_stats

    def attack_bonus(self, key, damage):
        if damage > 0 and self.enabled(key) and self.owned(key) == 'MS':
            return damage + self.stats(key)['attack'] * .08
        return damage

    def special_hits(self, key):
        return 2 if self.owned(key) == 'V2' else 1

    def special_damage(self, key, other, hit=0):
        kind = self.owned(key)
        cfg = self.s.blades[key]['special_move']
        bank = self.specials.get(key, {})
        attack = bank.get('attack', self.stats(key)['attack'])
        formula = cfg['damage_formula']
        damage = formula['base'] + formula['attack'] * attack
        pierce = cfg.get('pierce_defense_pct', 0) / 100
        if kind == 'G': damage += 12 * bank.get('gravity', 0)
        if kind == 'Black': damage += bank.get('energy', 0)
        if kind == 'V': damage *= 1 + .10 * bank.get('volcano', 0)
        if kind == 'GT':
            stacks = bank.get('turbo', 0)
            damage *= 1 + .12 * stacks
            if stacks == 3: pierce = .18
        if kind == 'MS':
            missing = max(0, 1 - self.s.hp[key] / max(1, self.s.max_hp_per_player[key]))
            damage *= 1 + min(.35, missing * .50)
        # Same inverse-DEF normalization as existing authored stat damage.
        defense = self.stats(other)['defense'] * (1 - pierce)
        return damage * 100 / max(1, defense)

    def special_landed(self, key, other, hit, damage):
        if damage > 0 and hit == 0 and self.owned(key) == 'V2':
            self.states[other]['shred_until'] = self.s.round + 2

    def mitigate(self, defender, move, damage, logs):
        if damage <= 0 or not self.secondary(defender): return damage
        kind, d = self.owned(defender), self.states[defender]
        if kind == 'V' and d.get('guard_until', -1) >= self.s.round: damage *= .90
        if kind == 'F' and move == 'special' and d.get('resist_ready', 0) <= self.s.round:
            # One Special activation (all its hits), then three blocked rounds.
            d['resist_round'] = self.s.round
            d['resist_ready'] = self.s.round + 4
            self.stamina(defender, 2)
            logs.append('🔥 Fire Resistance reduces Special damage by 15%; restores 2 stamina!')
        if kind == 'F' and move == 'special' and d.get('resist_round') == self.s.round:
            damage *= .85
        return damage

    def receive(self, key, damage, logs):
        if damage <= 0: return 0
        if self.secondary(key) and self.owned(key) == 'Black':
            d = self.states[key]
            capacity = max(0, self.s.max_hp_per_player[key] * .35 - d.get('energy', 0))
            absorbed = min(damage * .15, capacity)
            d['energy'] = d.get('energy', 0) + absorbed
            damage -= absorbed
            if absorbed: logs.append(f'🌑 Forbidden Phoenix stores {absorbed:.1f} Dark Energy!')
        return damage

    def threshold(self, key, logs):
        d = self.states[key]
        if (self.secondary(key) and self.owned(key) == 'V' and not d.get('guard_used')
                and 0 < self.s.hp[key] < self.s.max_hp_per_player[key] * .45):
            d['guard_used'], d['guard_until'] = True, self.s.round + 2
            logs.append('🔥 Phoenix Guard grants 25% DEF and 10% damage reduction for 2 rounds!')

    def after_incoming(self, key, logs):
        d = self.states.setdefault(key, {})
        if self.s.hp[key] <= 0 and self.secondary(key) and self.owned(key) == 'V2' and not d.get('rebirth_used'):
            d['rebirth_used'], d['rebirth_until'] = True, self.s.round + 2
            self.s.hp[key] = max(1, math.floor(self.s.max_hp_per_player[key] * .20))
            protected = [b for b in self.s.status.active_buffs.get(key, [])
                         if b['amount'] < 0 and not b.get('removable', True)]
            self.s.status.active_buffs[key] = [b for b in self.s.status.active_buffs.get(key, []) if b not in protected]
            while self.s.status.cleanse_one(key): pass
            self.s.status.active_buffs[key].extend(protected)
            og = getattr(self.s, 'original_generation', None)
            if og:
                og.debuffs = {token: value for token, value in og.debuffs.items() if token[0] != key}
            d.pop('shred_until', None)
            # Cleansing a stolen reduction also revokes its matching gain.
            for entry in list(d.get('transfers', [])):
                if entry['amount'] < 0:
                    counterpart = self.states.get(entry['other'])
                    if counterpart is not None:
                        counterpart['transfers'] = [e for e in counterpart.get('transfers', []) if e.get('token') != entry['token']]
            d['transfers'] = [e for e in d.get('transfers', []) if e['amount'] >= 0]
            for state in self.states.values():
                state['burns'] = [e for e in state.get('burns', []) if e['target'] != key]
            logs.append('🔥 Rebirth Flame restores 20% maximum HP, cleanses debuffs and grants 15% ATK!')
        self.threshold(key, logs)

    def terminal(self, key, damage, logs, *, mitigated=False):
        """A final hit, burn or reflection: defensive hooks only, no recursion."""
        if not mitigated: damage = self.mitigate(key, 'terminal', damage, logs)
        damage = self.receive(key, damage, logs)
        self.s.hp[key], actual = damage_hp(self.s.hp[key], damage)
        self.after_incoming(key, logs)
        return actual

    def tags(self, key):
        d, kind = self.states.get(key, {}), self.owned(key)
        tags = []
        if kind == 'G' and d.get('gravity'): tags.append(f"🔥 Gravity {d['gravity']}/5")
        if kind == 'Black' and d.get('energy'): tags.append(f"🌑 Energy {d['energy']:.0f}")
        if kind == 'V' and d.get('volcano'): tags.append(f"🌋 Volcano {d['volcano']}/3")
        if kind == 'GT' and d.get('turbo') and d.get('turbo_until', -1) >= self.s.round:
            tags.append(f"🔥 Turbo {d['turbo']}/3")
        if d.get('shred_until', -1) >= self.s.round: tags.append('🛡️ DEF −12%')
        burns = sum(e['target'] == key and e['until'] >= self.s.round
                    for state in self.states.values() for e in state.get('burns', []))
        if burns: tags.append(f'🔥 Phoenix Burn ×{burns}')
        return tags

    def commit_damage(self, key, damage, logs, parts=None):
        """Commit resolved hits in order, so rebirth cannot erase later hits."""
        if not parts or sum(parts) <= 0:
            return self.terminal(key, damage, logs, mitigated=True)
        total = sum(parts)
        whole = math.floor(damage)
        amounts = [math.floor(whole * part / total) for part in parts]
        amounts[-1] += whole - sum(amounts)
        return sum(self.terminal(key, part, logs, mitigated=True) for part in amounts)

    def committed(self, key, other, move, actual):
        if actual > 0 and move in ('attack', 'special'):
            self.hits[(key, other, move)] = self.hits.get((key, other, move), 0) + actual

    def heal(self, key, amount, logs, label):
        from .purification import heal_amount
        self.s.hp[key], actual = recover_hp(self.s.hp[key], self.s.max_hp_per_player[key], heal_amount(self.s, key, amount))
        if actual: logs.append(f'🔥 {label} restores {actual} HP!')

    def stamina(self, key, amount):
        sm = self.s.stamina_manager
        sm.stamina[key] = recover_resource(sm.stamina[key], sm.cap_for(key), amount)

    def burn(self, key, other):
        if self.s.status.is_invulnerable(other): return
        burns = self.states[key].setdefault('burns', [])
        live = [e for e in burns if e['target'] == other and e['until'] >= self.s.round]
        entry = {'target': other, 'until': self.s.round + 1}
        if len(live) >= 2:
            burns.remove(min(live, key=lambda e: e['until']))
        burns.append(entry)

    def steal(self, key, other):
        # The cap is 16% of the opponent's pre-theft effective stat; each
        # transfer is 4% of its CURRENT value, and expires independently.
        d = self.states[key]
        stats = self.stats(other)
        for stat in ('attack', 'defense'):
            entries = [e for e in d.get('transfers', []) if e['stat'] == stat and e['other'] == other
                       and e['amount'] > 0 and e['until'] >= self.s.round]
            total = sum(e['amount'] for e in entries)
            amount = min(stats[stat] * .04, max(0, (stats[stat] + total) * .16 - total))
            if amount <= 0: continue
            token = (key, other, stat, self.s.round)
            for owner, counterpart, delta in ((key, other, amount), (other, key, -amount)):
                self.states[owner].setdefault('transfers', []).append(
                    {'stat': stat, 'amount': delta, 'other': counterpart,
                     'until': self.s.round + 3, 'token': token})

    def end(self, moves, logs):
        if self.completed == self.s.round: return
        self.completed = self.s.round
        # Ability procs happen once per successful move, using capped actual HP damage.
        extra_hits, burn_ticks = [], []
        for key, move in moves.items():
            if self.s.hp[key] <= 0: continue
            other = next(k for k in moves if k != key)
            kind, d = self.owned(key), self.states[key]
            actual = self.hits.get((key, other, move), 0)
            if kind == 'Black' and move == 'special' and actual > 0:
                self.heal(key, actual * .30, logs, 'Abyssal Phoenix Devourer')
            if kind == 'F' and move == 'special':
                for entry in d.get('burns', []):
                    if entry['target'] == other and entry['until'] >= self.s.round:
                        burn_ticks.append((other, self.stats(key)['attack'] * .12))
                self.burn(key, other)
            if not self.enabled(key): continue
            if kind == 'G':
                if actual > 0 and move == 'attack': d['gravity'] = min(5, d.get('gravity', 0) + 1)
                if move == 'stamina' and self.secondary(key) and d.get('rotation_ready', 0) <= self.s.round:
                    self.heal(key, (self.s.max_hp_per_player[key] - self.s.hp[key]) * .06, logs, 'Phoenix Rotation')
                    d['rotation_until'], d['rotation_ready'] = self.s.round + 2, self.s.round + 4
            if kind == 'Black' and actual > 0:
                self.heal(key, actual * .20, logs, 'Dark Absorption')
                self.steal(key, other)
            if kind == 'V' and move == 'attack':
                d['volcano'] = min(3, d.get('volcano', 0) + 1) if actual > 0 else 0
            if kind == 'V2' and move == 'attack' and actual > 0 and d.get('wing_ready', 0) <= self.s.round:
                extra_hits.append((other, actual * .22))
                d['wing_ready'] = self.s.round + 3
            if kind == 'GT' and move == 'charge':
                d['turbo'] = min(3, d.get('turbo', 0) + 1)
                d['turbo_until'] = self.s.round + 3
            if kind == 'F':
                if move == 'attack' and actual > 0: self.burn(key, other)
        for other, amount in extra_hits:
            self.terminal(other, amount, logs, mitigated=True)
            logs.append('🔥 Twin Phoenix Wings lands an additional strike!')
        for other, amount in burn_ticks: self.terminal(other, amount, logs)
        # Each stack ticks independently for two round ends. No offensive hooks.
        for key, d in self.states.items():
            for entry in d.get('burns', []):
                if entry['until'] >= self.s.round:
                    actual = self.terminal(entry['target'], self.stats(key)['attack'] * .12, logs)
                    logs.append(f'🔥 Phoenix Burn deals {actual} damage!')
            d['burns'] = [e for e in d.get('burns', []) if e['until'] > self.s.round]
