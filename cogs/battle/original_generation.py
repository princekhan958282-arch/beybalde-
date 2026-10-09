"""Opt-in, battle-local active skills for Original Generation avatars.

No profile energy is spent: all three skills use a separate round-earned pool.
Effects expire by absolute round, and riders/counters never dispatch abilities.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

COSTS = (40, 60, 100)
COOLDOWNS = (3, 4, 0)


@dataclass
class SkillState:
    energy: int = 0
    pending: int = 0
    used_round: int = 0
    cooldowns: dict = field(default_factory=dict)
    ultimate_used: bool = False
    effects: dict = field(default_factory=dict)
    previous: str | None = None
    streak: int = 0
    last_skill: int = 0
    sealed: dict = field(default_factory=dict)
    rage: int = 0


class OriginalGeneration:
    def __init__(self, session, *, states=None, rng=None):
        self.s = session
        self.states = states if states is not None else {
            k: SkillState() for k, card in session.avatar_cards.items()
            if card.get('active_battle_skills')}
        self.rng = rng or random.random
        self.incoming = {}
        self.guard_saved = {}
        self.retaliation = []
        self.debuffs = {}
        self.prepared_round = 0
        self.completed_round = 0
        self.split_running = False
        self.split_success = {}
        self.action_cutter = {}
        self.action_bonus = {}
        self.action_pierce = {}
        self.sealed_bonus_snapshots = {}

    @property
    def round(self):
        return self.s.round

    def effect(self, key, name):
        st = self.states.get(key)
        effect = st.effects.get(name) if st else None
        return effect if effect and effect.get('from', 0) <= self.round <= effect['until'] else None

    def put(self, key, name, turns=1, **values):
        self.states[key].effects[name] = dict(until=self.round + turns - 1, **values)

    def reason(self, key, slot):
        st = self.states.get(key)
        if not st or not 1 <= slot <= 3:
            return 'No active Original Generation avatar skill.'
        if self.s.finished or self.s.hp.get(key, 0) <= 0:
            return 'This battle has ended.'
        if self.s.moves.get(key) is not None:
            return 'Choose your avatar skill before your normal move.'
        if st.pending or st.used_round == self.round:
            return 'You already selected an avatar skill this round.'
        if st.sealed.get(slot, 0) >= self.round:
            return 'That avatar skill is sealed this round.'
        if slot == 3 and st.ultimate_used:
            return 'Your ultimate has already been used this battle.'
        if st.cooldowns.get(slot, 0) > self.round:
            return f"Ready in round {st.cooldowns[slot]}."
        if st.energy < COSTS[slot - 1]:
            return f'Need {COSTS[slot - 1]} Avatar Energy; you have {st.energy}.'
        return None

    def select(self, key, slot):
        reason = self.reason(key, slot)
        if reason:
            return reason
        self.states[key].pending = slot
        return None

    def heal(self, key, amount, logs, label):
        from .combat_rules import recover_hp
        from .purification import heal_amount
        amount = heal_amount(self.s, key, amount)
        before = self.s.hp[key]
        self.s.hp[key], actual = recover_hp(before, self.s.max_hp_per_player[key], amount)
        if actual:
            logs.append(f'✨ **{label}** — restores {actual} HP!')

    def stamina(self, key, amount):
        if self.s.hp[key] > 0:
            sm = self.s.stamina_manager
            sm.stamina[key] = min(sm.cap_for(key), sm.stamina[key] + amount)

    def begin_round(self, logs):
        if self.prepared_round == self.round:
            return
        self.prepared_round = self.round
        for key, original in list(self.sealed_bonus_snapshots.items()):
            if self.debuffs.get((key, 'skill_seal'), 0) < self.round:
                self.s.avatar_bonuses[key] = original
                self.sealed_bonus_snapshots.pop(key)
        self.incoming.clear()
        self.guard_saved.clear()
        self.retaliation.clear()
        self.action_bonus.clear()
        self.action_pierce.clear()
        self.split_success.clear()
        self.action_cutter.clear()
        for key, st in self.states.items():
            move = self.s.moves[key]
            st.streak = st.streak + 1 if move == 'attack' else 0
            slot, st.pending = st.pending, 0
            if not slot:
                continue
            card = self.s.avatar_cards[key]
            name = card['skills'][slot - 1]['name']
            if card['character'] == 'tyson' and slot == 3 and move != 'special':
                logs.append('🌪️ **Galaxy Turbo Twister** needs Special; energy was not spent.')
                continue
            st.energy -= COSTS[slot - 1]
            st.used_round = self.round
            st.cooldowns[slot] = self.round + COOLDOWNS[slot - 1]
            st.ultimate_used |= slot == 3
            st.last_skill = slot
            logs.append(f'⚡ **{card["name"]}: {name}** — {COSTS[slot - 1]} energy!')
            self.activate(key, card['character'], slot, logs)
            from cogs.avatar import avatar_config as C
            levels = card.get('_skill_levels', [])
            steps = levels[slot - 1] - 1 if slot <= len(levels) else 0
            if steps:
                self.put(key, 'skill_empower', C.EMPOWER_ROUNDS,
                         attack=C.EMPOWER_PERCENT_STEP * steps,
                         defense=C.EMPOWER_PERCENT_STEP * steps)
        # Snapshot conditional stat and pierce modifiers before either side attacks.
        for key, st in self.states.items():
            other = self.enemy(key)
            if self.s.moves[key] == 'attack' and self.effect(key, 'cutter'):
                self.action_cutter[key] = True
                st.effects.pop('cutter', None)
            if self.effect(key, 'technique') and st.previous and st.previous != self.s.moves[key]:
                self.put(key, 'technique_stats', attack=.15, defense=.15)
                logs.extend(self.s.stability_manager._apply(key, 3))
            if self.effect(key, 'focus'):
                previous = self.states.get(other)
                prev_move = previous.previous if previous else getattr(self.s, 'og_previous_moves', {}).get(other)
                if prev_move and prev_move == self.s.moves[other]:
                    self.action_pierce[key] = .25
            if self.effect(key, 'feint') and self.s.moves[other] == 'defense':
                self.guard_saved.setdefault(other, 0)
            if st.rage:
                if self.s.moves[key] in ('stamina', 'charge'):
                    st.rage = 0
                elif self.s.moves[key] == 'attack':
                    self.action_bonus[key] = .15
                    st.rage -= 1

    def enemy(self, key):
        return next(k for k in self.s.blades if k != key)

    def activate(self, key, character, slot, logs):
        other = self.enemy(key)
        if character == 'tyson':
            if slot == 1: self.put(key, 'comeback', 2)
            if slot == 2: self.put(key, 'storm', 99)
            if slot == 3: self.put(key, 'tornado', 2, enemy=other, start=self.round)
        elif character == 'kai':
            if slot == 1: self.put(key, 'focus')
            if slot == 2: self.put(key, 'saber', 99)
            if slot == 3: self.put(key, 'tempest', 99)
        elif character == 'ray':
            if slot == 1: self.put(key, 'tiger_claw', 99)
            if slot == 2: self.put(key, 'technique')
            if slot == 3: self.put(key, 'vulcan', 99)
        elif character == 'max':
            if slot == 1: self.put(key, 'shield', 2, hp=math.floor(self.s.max_hp_per_player[key] * .10))
            if slot == 2: self.put(key, 'fortress', 2, stored=0)
            if slot == 3: self.put(key, 'unbreakable', 2)
        elif character == 'mariah':
            if slot == 1: self.put(key, 'footwork')
            if slot == 2: self.put(key, 'scratch', 99)
            if slot == 3: self.put(key, 'ambush', 2)
        elif character == 'daichi':
            if slot == 1:
                # Cleanse only hostile stability-recovery modifiers.
                buffs = self.s.status.active_buffs.get(key, [])
                self.s.status.active_buffs[key] = [b for b in buffs if not (
                    b.get('amount', 0) < 0 and b.get('stat') in ('stability_regen', 'stability_recovery'))]
                logs.extend(self.s.stability_manager._apply(key, 6))
            if slot == 2: self.put(key, 'cutter', 99)
            if slot == 3: self.put(key, 'great_cutter', 99)
        elif character == 'lee':
            if slot == 1: self.put(key, 'mark', 2)
            if slot == 2: self.put(key, 'lightning')
            if slot == 3:
                enemy_st = self.states.get(other)
                if enemy_st and enemy_st.last_skill:
                    enemy_st.sealed[enemy_st.last_skill] = self.round + 1
                    # A skill already committed for this exchange is simultaneous;
                    # its seal starts next round rather than cancelling a pending pick.
                else:
                    # Legacy avatars have one preselected skill, suppressed by
                    # AbilityEngine._avatar_rules_for for the same duration.
                    self.debuffs[(other, 'skill_seal')] = self.round + 1
                    card = self.s.avatar_cards.get(other) or {}
                    selected = getattr(self.s, 'avatar_skill_slots', {}).get(other, 0) or 0
                    av = self.s.avatar_bonuses.get(other)
                    if av and selected and not card.get('stats_always_on'):
                        import copy
                        blank = copy.copy(av)
                        bonuses = card['skills'][selected - 1].get('bonuses') or {}
                        for field, amount in bonuses.items():
                            if field in ('hp_flat', 'hp_percent') or not hasattr(blank, field):
                                continue
                            if field.endswith('_flat'):
                                setattr(blank, field, max(0, getattr(blank, field) - amount))
                            else:
                                setattr(blank, field, False if isinstance(amount, bool) else 0)
                        self.sealed_bonus_snapshots.setdefault(other, av)
                        self.s.avatar_bonuses[other] = blank
        elif character == 'gary':
            if slot == 1: self.put(key, 'endurance', 2)
            if slot == 2: self.put(key, 'axe', 99)
            if slot == 3: self.states[key].rage = 3
        elif character == 'kevin':
            if slot == 1: self.put(key, 'feint')
            if slot == 2:
                self.put(key, 'monkey', 99)
            if slot == 3: self.put(key, 'trickery', 2)

    def stat_multiplier(self, key, stat):
        e = self.effect(key, 'technique_stats')
        empower = self.effect(key, 'skill_empower')
        return 1 + (e.get(stat, 0) if e else 0) + (empower.get(stat, 0) if empower else 0)

    def pierce(self, key):
        return self.action_pierce.get(key, 0)

    def cost(self, key, move, cost):
        if self.action_cutter.get(key) and move == 'attack': cost += .3
        other = self.enemy(key)
        tornado = self.effect(other, 'tornado')
        if tornado and self.round > tornado['start']: cost += .7
        prev = getattr(self.s, 'og_previous_moves', {}).get(key)
        if self.effect(other, 'trickery') and prev and prev == move: cost += .4
        return max(0, round(cost, 2))

    def blocked_move(self, key, move):
        if move == 'defense' and self.effect(key, 'no_defense'):
            return 'Bear Axe Attack prevents Defense this round.'
        return None

    def before_damage(self, key, other, move, damage, logs, first=True):
        if move not in ('attack', 'special') or damage <= 0:
            return damage
        bonus = self.action_bonus.get(key, 0)
        if self.effect(key, 'storm') and move == 'attack':
            bonus += min(.30, .10 * self.states[key].streak)
            if first: self.states[key].effects.pop('storm', None)
            self.action_bonus[key] = bonus
        if self.effect(key, 'ambush') and move == 'attack' and self.s.moves[other] in ('stamina', 'charge'):
            bonus += .40
            if first: self.states[key].effects.pop('ambush', None)
            self.action_bonus[key] = bonus
        if self.effect(key, 'axe') and move == 'attack':
            bonus += .35
            if first:
                self.states[key].effects.pop('axe', None)
                self.states[key].effects['no_defense'] = {'until': self.round + 1, 'from': self.round + 1}
            self.action_bonus[key] = bonus
        if self.effect(key, 'great_cutter'):
            sm = self.s.stability_manager
            bonus += min(.35, max(0, sm.max[other] - sm.stability[other]) * .01)
            if first: self.states[key].effects.pop('great_cutter', None)
            self.action_bonus[key] = bonus
        if self.effect(key, 'tempest') and first:
            buffs = self.s.status.active_buffs.get(other, [])
            positive = next((b for b in buffs if b.get('amount', 0) > 0), None)
            if positive:
                buffs.remove(positive)
                bonus += .25
                # Normal stat mitigation has already been computed; recalculate
                # in preprocess() instead. Special stat buffs are likewise
                # removed before _resolve_special builds its payload.
            self.states[key].effects.pop('tempest', None)
            self.action_bonus[key] = bonus
        if self.effect(key, 'vulcan') and self.s.moves[other] == 'defense' and first:
            logs.extend(self.s.stability_manager._apply(other, -5))
        return math.floor(damage * (1 + bonus) + 1e-9)

    def preprocess(self, key, other, stats):
        """Remove a buff before formula evaluation, retaining the bonus rider."""
        if self.s.moves.get(key) not in ('attack', 'special'):
            return stats
        if self.effect(key, 'tempest'):
            buffs = self.s.status.active_buffs.get(other, [])
            positive = next((b for b in buffs if b.get('amount', 0) > 0), None)
            if positive:
                buffs.remove(positive)
                self.action_bonus[key] = self.action_bonus.get(key, 0) + .25
                if positive.get('stat') == 'defense':
                    stats = dict(stats)
                    stats['defense'] = max(0, stats['defense'] - positive['amount'])
            self.states[key].effects.pop('tempest', None)
        pierce = self.pierce(key)
        if pierce:
            stats = dict(stats)
            stats['defense'] *= 1 - pierce
        return stats

    def shield(self, key, other, damage, logs):
        bypass = 0
        if self.effect(key, 'vulcan'):
            total = self.s.status.get_shield(other)
            total += (self.effect(other, 'shield') or {}).get('hp', 0)
            bypass = min(damage, math.floor(total * .5))
        effect = self.effect(other, 'shield')
        remaining = max(0, damage - bypass)
        if effect:
            absorbed = min(remaining, effect['hp'])
            effect['hp'] -= absorbed
            remaining -= absorbed
            if absorbed: logs.append(f'🛡️ **Draciel Shield** absorbs {absorbed}!')
        return remaining, bypass

    def mitigate(self, key, other, move, damage, logs):
        if move not in ('attack', 'special') or damage <= 0:
            return damage
        if self.effect(other, 'footwork') and not self.effect(other, 'footwork').get('used'):
            damage = math.floor(damage * .65)
            self.effect(other, 'footwork')['used'] = True
        return damage

    def stability_delta(self, key, old, delta, logs, *, incoming=True):
        if delta >= 0: return delta
        if incoming and self.effect(key, 'endurance'):
            converted = abs(delta) * .5
            delta = -math.ceil(abs(delta) - converted)
            self.s.hp[key] = max(0, self.s.hp[key] - math.floor(converted * 3))
        if old + delta <= 0 and self.effect(key, 'unbreakable'):
            self.states[key].effects.pop('unbreakable', None)
            logs.append('🛡️ **Unbreakable Fortress** prevents a stability finish; +8 stability!')
            return max(0, min(self.s.stability_manager.max[key], 9)) - old
        return delta

    def committed(self, key, other, move, actual, logs):
        if actual <= 0 or move not in ('attack', 'special'): return
        self.incoming[other] = self.incoming.get(other, 0) + actual
        if self.split_success.pop(key, False) and self.s.hp[key] > 0:
            self.stamina(key, .4)
            logs.append('🐯 **Tiger Claw** — all 3 strikes connect; +0.4 stamina!')
        if self.effect(key, 'saber'):
            self.states[key].effects.pop('saber', None)
            self.debuffs[(other, 'scorch')] = self.round + 2
            self.debuffs[(other, 'scorch_start')] = self.round + 1
        if self.effect(key, 'scratch'):
            self.states[key].effects.pop('scratch', None)
            self.debuffs[(other, 'wound')] = self.round + 1
        if self.effect(key, 'monkey'):
            self.states[key].effects.pop('monkey', None)
            self.debuffs[(other, 'energy_denial')] = self.round
        if move == 'attack' and self.action_cutter.pop(key, False):
            logs.extend(self.s.stability_manager._apply(other, -4))
        if self.effect(key, 'vulcan'):
            self.states[key].effects.pop('vulcan', None)
        if self.effect(other, 'mark'):
            self.states[other].effects.pop('mark', None)
            self.retaliation.append((other, key, .15 * self.attack(other), 'Tiger Claw'))
        if self.effect(other, 'lightning') and self.s.moves[other] == 'defense':
            self.retaliation.append((other, key, min(40, .30 * actual), 'Dark Lightning'))

    def attack(self, key):
        from .purification import effective_stats
        return effective_stats(self.s, key)['attack']

    def rider(self, key, other, raw, label, logs, critical=False):
        """Normal DEF/type/status mitigation, with no offensive proc dispatch."""
        from .purification import effective_stats
        from .combat_rules import damage_hp
        from . import avatar_combat
        defense = effective_stats(self.s, other)['defense']
        # The formula's inverse-DEF response, normalized at 100 DEF.
        damage = raw * 100 / max(1, defense)
        damage, _ = self.s.attack_manager._apply_passive_reduction(other, self.s.blades[other], damage, [])
        if self.s.status.is_invulnerable(other): damage = 0
        absorb = getattr(self.s, 'rider_absorb', None)
        if absorb:
            damage = absorb(other, damage)
        damage -= self.s.status.absorb_shield(other, math.floor(damage))
        effect = self.effect(other, 'shield')
        if effect:
            absorbed = min(damage, effect['hp'])
            effect['hp'] -= absorbed
            damage -= absorbed
        tactical = getattr(getattr(self.s, 'ability', None), 'tactical', None)
        if tactical:
            damage = tactical.mitigate(other, key, 'special', damage, logs)
        damage = self.s.type_gimmicks.mitigate(key, other, 'special', damage)
        av = self.s.avatar_bonuses.get(other)
        if av: damage = av.apply_damage_resistance(damage)
        damage, lethal_logs = avatar_combat.guard_lethal(self.s, other, math.floor(damage))
        logs.extend(lethal_logs)
        phoenix = getattr(getattr(self.s, 'ability', None), 'dranzer', None)
        if phoenix:
            actual = phoenix.terminal(other, damage, logs)
        else:
            self.s.hp[other], actual = damage_hp(self.s.hp[other], damage)
        logs.append(f'🌪️ **{label}** — {actual} damage' + (' (CRITICAL ×2)!' if critical else '!'))

    def end_round(self, logs):
        if self.completed_round == self.round: return
        self.completed_round = self.round
        # Roll and capture tornado payloads before applying either side's tick.
        tornadoes = []
        for key in self.states:
            tornado = self.effect(key, 'tornado')
            if tornado and self.s.hp[key] > 0:
                crit = self.rng() < .44
                tornadoes.append((key, tornado['enemy'], self.attack(key) * .32 * (2 if crit else 1), crit))
        for key, other, raw, crit in tornadoes:
            self.rider(key, other, raw, 'Galaxy Turbo Twister', logs, crit)
        for key, other, raw, label in self.retaliation:
            self.rider(key, other, raw, label, logs)
        for key, st in self.states.items():
            if self.effect(key, 'comeback'):
                self.heal(key, min(self.incoming.get(key, 0) * .25,
                                  self.s.max_hp_per_player[key] * .08), logs, 'Champion’s Spirit')
            fortress = self.effect(key, 'fortress')
            if fortress:
                fortress['stored'] += self.guard_saved.get(key, 0) * .30
                if fortress['until'] == self.round:
                    self.heal(key, min(fortress['stored'], self.s.max_hp_per_player[key] * .12), logs, 'Fortress Defense')
            if self.debuffs.get((key, 'energy_denial'), 0) < self.round:
                st.energy = min(100, st.energy + 20)
            st.previous = self.s.moves[key]
        for key in self.s.blades:
            if self.debuffs.get((key, 'scorch_start'), 999999) <= self.round <= self.debuffs.get((key, 'scorch'), 0) and self.s.moves[key] in ('attack', 'special'):
                self.s.hp[key] = max(0, self.s.hp[key] - 8)
                logs.append('🔥 **Scorch** — 8 HP lost after attacking!')
        self.s.og_previous_moves = dict(self.s.moves)


def runtime(session):
    return getattr(session, 'original_generation', None)
