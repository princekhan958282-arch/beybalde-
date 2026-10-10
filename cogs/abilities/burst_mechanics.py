"""Opt-in authored Burst mechanics; state belongs to one battle, never a profile.

Registration uses effect identifiers rather than character names. Special riders
return damage to the shared pipeline, preserving shields, guard and HP commits.
"""
import math

EFFECTS = frozenset({
    'speed_accumulation', 'consecutive_edge', 'breakable_barrier', 'shield_vault',
    'orbit_accumulation', 'orbit_immunity', 'verdict_tokens', 'paired_verdict',
    'landing_debuff', 'healthy_counter', 'halo_accumulation', 'threshold_reversal',
    'guard_arms', 'action_discipline', 'third_deflection', 'mirror_spheres',
    'loss_pursuit', 'charge_momentum',
    'loss_adaptation', 'charge_seal', 'shield_excavation', 'shield_breakthrough',
    'feather_recovery', 'win_slipstream', 'attack_heat', 'heat_ignition',
})
SPECIALS = frozenset({
    'speed_finisher', 'fortress_finisher', 'orbit_finisher', 'verdict_finisher',
    'hammer_finisher', 'halo_finisher', 'sixfold_finisher', 'sphere_finisher',
    'pursuit_finisher',
    'eclipse_finisher', 'crusher_finisher', 'ascension_finisher', 'burn_finisher',
})


class BurstMechanics:
    def __init__(self, engine):
        self.engine = engine
        self.session = engine.session
        self.effects = {}
        self.state = {}
        self.effect_sources = {}

    def data(self, key):
        return self.state.setdefault(key, {})

    def has(self, key, effect):
        if effect not in self.effects.get(key, set()) or self.session.status.is_silenced(key):
            return False
        source = self.effect_sources.get((key, effect))
        return not (source == 1 and self.engine.ability_2_disabled.get(key))

    def stat(self, key, stat):
        base = (getattr(self.session, 'battle_stats', {}) or {}).get(key)
        return (base or self.session.blades[key]['stats']).get(stat, 0)

    def maximum(self, key):
        return self.session.max_hp_per_player[key]

    def round(self):
        return getattr(self.session, 'round', 0)

    def buff(self, key, stat, pct, duration, source):
        st = self.session.status
        st.active_buffs[key] = [b for b in st.active_buffs.get(key, []) if b.get('source') != source]
        # Refresh the named source rather than stacking repeated activations.
        # The shared engine expires old buffs BEFORE the next exchange. Reserve
        # the grant round so an end-of-round award survives two full exchanges.
        st.add_buff(key, stat, round(self.stat(key, stat) * pct / 100), duration + 1, source=source)

    def shield(self, key, pct, duration=None):
        d, st = self.data(key), self.session.status
        # Replace only this mechanic's remaining shield, preserving other sources.
        st.shield_hp[key] = max(0, st.get_shield(key) - d.get('barrier', 0))
        amount = math.ceil(self.maximum(key) * pct / 100)
        st.add_shield(key, amount)
        d['barrier'] = amount
        d['barrier_until'] = None if duration is None else self.round() + duration - 1

    def register(self, key, effect, ability_index=None):
        if effect not in EFFECTS:
            raise ValueError(f'Unknown Burst mechanic: {effect}')
        registered = self.effects.setdefault(key, set())
        if effect in registered:
            return
        registered.add(effect)
        self.effect_sources[(key, effect)] = ability_index
        if effect == 'breakable_barrier':
            self.shield(key, 15)

    def apply_stats(self, key, stats):
        """Shared by normal attacks, Special formulas and AI stat readers."""
        d = self.data(key)
        if self.has(key, 'speed_accumulation'):
            stats['attack'] += self.stat(key, 'attack') * .05 * d.get('speed', 0)
        if self.has(key, 'halo_accumulation'):
            for stat in ('attack', 'defense'):
                stats[stat] += self.stat(key, stat) * .03 * d.get('halo', 0)
        return stats

    def round_start(self, key, other, move, enemy_move, stats, enemy_stats, logs):
        d = self.data(key)
        if self.has(key, 'consecutive_edge') and move == 'attack' and d.pop('edge', False):
            enemy_stats['defense'] *= .80
            logs.append('🪽 Valkyrie’s Edge ignores 20% DEF.')
        if self.has(key, 'orbit_immunity'):
            d['orbit_protected'] = d.get('orbit', 0) >= 3
            d['orbit_protected_round'] = self.round()
        if self.has(key, 'third_deflection') and enemy_move == 'attack':
            d['attacks'] = d.get('attacks', 0) + 1
            d['deflect_round'] = self.round() if d['attacks'] % 3 == 0 else -1

    def outgoing(self, key, move, matchup, damage, first, logs):
        d = self.data(key)
        if not first:
            if move == 'special' and d.get('heat_special_round') == self.round():
                damage += d.get('heat_special_bonus', 0)
            return damage
        if move == 'attack':
            if self.has(key, 'shield_breakthrough') and d.pop('breakthrough', False):
                damage = math.ceil(damage * 1.25)
                logs.append('🐉 Grand Breakthrough grants +25% damage.')
            if (damage > 0 and self.has(key, 'heat_ignition')
                    and d.get('ignition_until', -1) >= self.round()):
                damage = math.ceil(damage * 1.30)
            if self.has(key, 'paired_verdict') and d.get('red', 0) and d.get('black', 0):
                d['red'] -= 1
                d['black'] -= 1
                damage = math.ceil(damage * 1.15)
                self.session.status.add_shield(key, math.ceil(self.maximum(key) * .08))
                logs.append('🃏 Double Jeopardy consumes one token of each colour.')
            if self.has(key, 'loss_pursuit') and matchup == 'win' and damage > 0:
                stacks = d.pop('pursuit', 0)
                damage = math.ceil(damage * (1 + .10 * stacks))
                if stacks:
                    logs.append(f'🐉 Dragon Pursuit consumes {stacks} stacks.')
            if (damage > 0 and self.has(key, 'charge_momentum')
                    and d.get('momentum_until', -1) >= self.round()):
                damage += round(self.stat(key, 'attack') * .15)
                d['momentum_until'] = -1
                logs.append('🐉 Ace Momentum adds 15% ATK damage.')
        if move == 'defense' and matchup == 'win':
            if self.has(key, 'shield_vault'):
                damage += int(d.pop('vault', 0))
            if self.has(key, 'healthy_counter') and self.session.hp[key] > self.maximum(key) * .60:
                damage += round(self.stat(key, 'defense') * .15)
        return damage

    def shield_bonus(self, key, move, damage, first):
        """Return extra shield-only damage; never convert this amount to HP."""
        if move == 'attack' and self.has(key, 'shield_excavation'):
            return math.ceil(damage * .50)
        if move == 'special' and first:
            d = self.data(key)
            return d.pop('crusher_shield_bonus', 0) if d.get('crusher_shield_round') == self.round() else 0
        return 0

    def shield_broken(self, key, move, logs):
        if self.has(key, 'shield_breakthrough'):
            self.data(key)['breakthrough'] = True
            logs.append('🐉 Grand Breakthrough arms the next normal Attack.')

    def action_heal(self, key, amount):
        if self.has(key, 'feather_recovery'):
            amount *= 1 + .05 * self.data(key).get('feathers', 0)
        return amount

    def heal_multiplier(self, key):
        return .75 if self.data(key).get('eclipse_until', -1) >= self.round() else 1

    def hit_committed(self, attacker, defender, move, actual, logs):
        if move != 'attack' or actual <= 0:
            return
        d = self.data(attacker)
        if self.has(attacker, 'charge_seal') and d.pop('seal_ready', False):
            if not self.engine.debuff_immune.get(defender, False):
                from cogs.battle.purification import reduce_debuff
                duration = int(reduce_debuff(self.session, defender, 2))
                self.data(defender)['eclipse_until'] = self.round() + duration
                logs.append('🌑 Eclipse Seal reduces enemy healing by 25%.')
        if self.has(attacker, 'attack_heat'):
            d['heat'] = min(4, d.get('heat', 0) + 1)
        if self.has(attacker, 'heat_ignition') and d.get('ignition_until', -1) >= self.round():
            d['ignition_until'] = -1
            logs.append('🔥 Xceed Ignition consumes its +30% Attack boost.')

    def incoming(self, key, move, damage, logs):
        if damage <= 0:
            return damage
        d = self.data(key)
        reduction = 0
        if self.has(key, 'orbit_accumulation'):
            reduction += 4 * d.get('orbit', 0)
        if self.has(key, 'guard_arms') and move == 'attack':
            arms = d.pop('arms', 0)
            reduction += 7 * arms
            if arms:
                logs.append(f'🛡️ Six-Armed Guard consumes {arms} arms.')
        if self.has(key, 'third_deflection') and move == 'attack' and d.get('deflect_round', -1) == self.round():
            reduction += 30
        if d.get('reversal_until', -1) >= self.round():
            reduction += 20
        if d.get('sphere_until', -1) == self.round():
            reduction += d.get('sphere_reduction', 0)
        return math.ceil(damage * (1 - min(90, reduction) / 100))

    def absorbed(self, key, amount, logs):
        d = self.data(key)
        if self.has(key, 'shield_vault'):
            d['vault'] = min(60, d.get('vault', 0) + amount * .20)
        own = min(amount, d.get('barrier', 0))
        d['barrier'] = max(0, d.get('barrier', 0) - own)
        if own and not d['barrier'] and self.has(key, 'breakable_barrier'):
            self.buff(key, 'defense', 15, 2, 'arc_barrier')
            logs.append('🛡️ Arc Barrier breaks: +15% DEF for 2 rounds.')

    def stability_delta(self, key, delta, action=False):
        d = self.data(key)
        if (delta < 0 and not action and self.has(key, 'orbit_immunity')
                and d.get('orbit_protected') and d.get('orbit_protected_round') == self.round()):
            return 0
        return delta

    def committed(self, key, logs):
        if not self.has(key, 'threshold_reversal'):
            return
        d = self.data(key)
        if not d.get('reversed') and 0 < self.session.hp[key] < self.maximum(key) * .40:
            d['reversed'] = True
            self.session.status.active_buffs[key] = [
                b for b in self.session.status.active_buffs.get(key, []) if b.get('amount', 0) >= 0]
            d['reversal_until'] = self.round() + 2
            logs.append('😇 Angelic Reversal cleanses stat debuffs and grants 20% damage reduction.')

    def round_end(self, key, other, move, enemy_move, matchup, logs):
        d = self.data(key)
        win = matchup == 'win'
        loss = matchup in ('lose', 'lose_grind')
        if self.has(key, 'loss_adaptation') and loss and move in ('attack', 'defense'):
            stat = 'defense' if move == 'attack' else 'attack'
            self.buff(key, stat, 12, 2, f'shadow_adaptation:{stat}')
        if self.has(key, 'charge_seal') and move == 'charge':
            d['seal_ready'] = True
        if self.has(key, 'feather_recovery') and move == 'stamina' and win:
            d['feathers'] = min(3, d.get('feathers', 0) + 1)
        if self.has(key, 'win_slipstream'):
            if move == 'stamina' and win:
                d['slipstream_until'] = self.round() + 2
            elif move == 'charge' and d.get('slipstream_until', -1) >= self.round():
                sm = self.session.stamina_manager
                stolen = min(.8, max(0, sm.stamina.get(other, 0)))
                sm.stamina[other] -= stolen
                sm.stamina[key] = min(sm.cap_for(key), sm.stamina[key] + stolen)
                d['slipstream_until'] = -1
                logs.append(f'🪽 Pegasus Slipstream steals {stolen:g} stamina.')
        if self.has(key, 'attack_heat') and move == 'stamina':
            d['heat'] = max(0, d.get('heat', 0) - 1)
        if self.has(key, 'heat_ignition') and move == 'charge' and d.get('heat', 0) >= 2:
            d['heat'] -= 2
            d['ignition_until'] = self.round() + 2
            logs.append('🔥 Xceed Ignition consumes 2 Heat.')
        if self.has(key, 'speed_accumulation') and move == 'attack':
            d['speed'] = max(0, min(4, d.get('speed', 0) + (1 if win else -1 if loss else 0)))
        if self.has(key, 'consecutive_edge'):
            d['streak'] = d.get('streak', 0) + 1 if move == 'attack' and win else 0
            if d['streak'] >= 2:
                d['edge'], d['streak'] = True, 0
        # Keep protection until the next round-start snapshot: enemy counter
        # stability penalties can resolve after the tactical round-end hook.
        consume_orbit = self.has(key, 'orbit_immunity') and d.get('orbit_protected', False)
        if self.has(key, 'orbit_accumulation'):
            if move == 'defense' and win:
                d['orbit'] = min(3, d.get('orbit', 0) + 1)
            elif move == 'attack' and loss:
                d['orbit'] = max(0, d.get('orbit', 0) - 1)
        if consume_orbit:
            d['orbit'] = 0
        if self.has(key, 'verdict_tokens') and win:
            token = {'attack': 'red', 'defense': 'black'}.get(move)
            if token:
                d[token] = min(2, d.get(token, 0) + 1)
        if self.has(key, 'landing_debuff') and move == 'defense' and win:
            if not self.engine.debuff_immune.get(other, False):
                self.buff(other, 'attack', -8, 2, f'heavy_landing:{key}')
        if self.has(key, 'halo_accumulation'):
            d['halo'] = min(4, d.get('halo', 0) + 1)
        if self.has(key, 'guard_arms') and move == 'defense' and win:
            d['arms'] = min(3, d.get('arms', 0) + 1)
        if self.has(key, 'action_discipline'):
            sequence = d.setdefault('sequence', [])
            if move not in ('attack', 'defense', 'stamina', 'charge'):
                sequence.clear()
            elif move in sequence:
                sequence[:] = [move]
            else:
                sequence.append(move)
                if len(sequence) == 3:
                    logs.extend(self.session.stability_manager._apply(key, 6))
                    self.buff(key, 'defense', 12, 2, 'bushin_discipline')
                    sequence.clear()
        if self.has(key, 'mirror_spheres') and move == enemy_move == 'defense':
            logs.extend(self.session.stability_manager._apply(key, 5))
            d['spheres'] = min(3, d.get('spheres', 0) + 1)
        if self.has(key, 'loss_pursuit') and move == 'attack' and loss:
            d['pursuit'] = min(3, d.get('pursuit', 0) + 1)
        if self.has(key, 'charge_momentum') and move == 'charge':
            d['momentum_until'] = self.round() + 2
        until = d.get('barrier_until')
        if until is not None and until <= self.round():
            st = self.session.status
            st.shield_hp[key] = max(0, st.get_shield(key) - d.pop('barrier', 0))
            d['barrier_until'] = None

    def special(self, key, other, effect, damage, logs):
        if effect not in SPECIALS:
            raise ValueError(f'Unknown Burst finisher: {effect}')
        d = self.data(key)
        if effect == 'speed_finisher':
            damage += 12 * d.pop('speed', 0)
        elif effect == 'fortress_finisher':
            self.shield(key, 20, 2)
        elif effect == 'orbit_finisher':
            logs.extend(self.session.stability_manager._apply(key, 12))
            d['orbit'] = min(3, d.get('orbit', 0) + 2)
        elif effect == 'verdict_finisher':
            damage += 15 * d.pop('red', 0)
            self.session.status.add_shield(key, math.ceil(self.maximum(key) * .05 * d.pop('black', 0)))
        elif effect == 'hammer_finisher':
            source = f'heavy_landing:{key}'
            buffs = [b for b in self.session.status.active_buffs.get(other, []) if b.get('source') == source]
            if buffs:
                for b in buffs:
                    b['rounds_left'] += 1
                logs.extend(self.session.stability_manager._apply(key, 8))
        elif effect == 'halo_finisher':
            heal = math.ceil(self.maximum(key) * .03 * d.pop('halo', 0))
            self.engine._heal(key, heal, logs, 'Greatest Halo')
        elif effect == 'sphere_finisher':
            d['sphere_reduction'] = 8 * d.pop('spheres', 0)
            d['sphere_until'] = self.round() + 1
        elif effect == 'pursuit_finisher':
            self.engine.special_pierce_pct[key] = 5 * d.pop('pursuit', 0)
        elif effect == 'eclipse_finisher':
            enemy = self.data(other)
            if enemy.get('eclipse_until', -1) >= self.round():
                damage += 35
                enemy.pop('eclipse_until', None)
                logs.append('🌑 Black Eclipse consumes Eclipse Seal for +35 damage.')
        elif effect == 'crusher_finisher':
            d['crusher_shield_bonus'] = 40
            d['crusher_shield_round'] = self.round()
        elif effect == 'ascension_finisher':
            feathers = d.pop('feathers', 0)
            sm = self.session.stamina_manager
            sm.stamina[key] = min(sm.cap_for(key), sm.stamina[key] + feathers)
            logs.extend(self.session.stability_manager._apply(key, 4 * feathers))
        elif effect == 'burn_finisher':
            d['heat_special_bonus'] = 6 * d.pop('heat', 0)
            d['heat_special_round'] = self.round()
            damage += d['heat_special_bonus']
        # sixfold_finisher deliberately has no normal-Attack side effects.
        self.engine.cooldowns[(key, 'burst_finisher')] = 4
        return damage
