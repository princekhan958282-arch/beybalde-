"""Battle-local type identity. No database writes or Discord dependencies."""
from dataclasses import dataclass
import random
from cogs.abilities.type_system import normalise_type
from .combat_rules import base_damage, hp_damage

GIMMICKS = {"attack": ("critical_strike", "attack"),
            "defense": ("kinetic_counter", "defense"),
            "stamina": ("overdrive", "stamina"),
            "balance": ("adaptive_morph", None)}
MORPH_ROUNDS = 3
MORPH_BONUS = .05
DEFENSE_REDUCTION = .14
GIMMICK_OPS = {'gimmick_chance', 'gimmick_force', 'gimmick_suppress', 'gimmick_bonus'}
GIMMICK_NAMES = tuple(value[0] for value in GIMMICKS.values())


def defense_reduction(bey_type):
    return DEFENSE_REDUCTION if normalise_type(bey_type) == 'defense' else 0


def passive_stat_multiplier(bey_type, stat):
    t = normalise_type(bey_type)
    if t == "balance" and stat in ("hp", "attack", "defense", "stamina"):
        return 1.033
    return 1.10 if t == "attack" and stat == "attack" else 1.0



@dataclass
class GimmickState:
    critical_strike_active: bool = False
    kinetic_counter_active: bool = False
    overdrive_active: bool = False
    adaptive_morph_rounds: int = 0

    @property
    def adaptive_morph_active(self):
        return self.adaptive_morph_rounds > 0


class TypeGimmickEngine:
    def __init__(self, types, rng=None):
        self.types = {k: normalise_type(v) for k, v in types.items()}
        self.states = {k: GimmickState() for k in types}
        self.rng = rng or (lambda: random.random())
        # Extension points, keyed (player, gimmick). Force/disable can override
        # natural type/action eligibility explicitly; suppression always wins.
        self.chance_bonus = {}
        self.suppressed = set()
        self.forced = set()
        self.morph_bonus = {}
        self.overdrive_bonus = {}
        self.critical_multiplier = {}
        self.effects = {}
        self.round_index = 0
        self.action_overrides = {}

    def register_op(self, op, key, other_key, source, logs):
        """Timed authored controls. Same source refreshes instead of stacking.

        Chances and percentage bonuses use percentage points in authored data.
        A force can grant another type's gimmick; allow_actions explicitly
        permits Critical Strike on additional direct actions (e.g. Special).
        Suppression wins over natural and forced activation.
        """
        name = op.get('gimmick')
        if name not in GIMMICK_NAMES:
            raise ValueError(f'Unknown type gimmick: {name!r}')
        target = other_key if op.get('target') == 'enemy' else key
        kind = op['op']
        field = op.get('bonus', '')
        valid_bonuses = {'critical_strike': {'damage'}, 'kinetic_counter': set(),
                         'overdrive': {'heal', 'stability', 'stamina'},
                         'adaptive_morph': {'stats'}}
        if kind == 'gimmick_bonus' and field not in valid_bonuses[name]:
            raise ValueError(f'Unsupported {name} bonus: {field!r}')
        value = float(op.get('value', 0))
        if kind == 'gimmick_chance' or (kind == 'gimmick_bonus' and field in {'damage', 'heal', 'stats'}):
            value /= 100
        turns = max(1, int(op.get('turns', 1)))
        token = (target, name, kind, field, (key, source))
        self.effects[token] = (value, max(1, self.round_index) + turns - 1,
                               tuple(op.get('allow_actions') or ('attack',)))
        if kind == 'gimmick_suppress':
            if name == 'adaptive_morph':
                self.states[target].adaptive_morph_rounds = 0
            else:
                setattr(self.states[target], name + '_active', False)
        elif kind == 'gimmick_force' and not self.is_suppressed(target, name):
            self._activate(target, name)
            self.action_overrides[(target, name)] = self.allowed_actions(target, name)
        label = op.get("label") or source.rsplit(":", 1)[0]
        logs.append(f"✨ **{label}** — {name.replace('_', ' ').title()} {kind.removeprefix('gimmick_')} ({turns} rounds)!")

    def _values(self, key, name, kind, field=''):
        return [entry[0] for token, entry in self.effects.items()
                if token[:4] == (key, name, kind, field)]

    def is_suppressed(self, key, name):
        return (key, name) in self.suppressed or bool(self._values(key, name, 'gimmick_suppress'))

    def is_forced(self, key, name):
        return (key, name) in self.forced or bool(self._values(key, name, 'gimmick_force'))

    def allowed_actions(self, key, name):
        actions = {'attack'}
        for token, entry in self.effects.items():
            if token[:3] == (key, name, 'gimmick_force'):
                actions.update(entry[2])
        return actions

    def _activate(self, key, name):
        if name == 'adaptive_morph':
            self.states[key].adaptive_morph_rounds = MORPH_ROUNDS
        else:
            setattr(self.states[key], name + '_active', True)

    def start_round(self):
        self.round_index += 1
        self.action_overrides = {}

    def export_controls(self, key):
        """Detached relative state for search clones and changing actor seats."""
        return [(token[1], token[2], token[3],
                 'self' if token[4][0] == key else 'enemy', token[4][1],
                 entry[0], entry[1] - self.round_index, entry[2])
                for token, entry in self.effects.items() if token[0] == key]

    def restore_controls(self, key, other_key, entries):
        for name, kind, field, owner, source, value, remaining, actions in entries:
            token = (key, name, kind, field, (key if owner == 'self' else other_key, source))
            self.effects[token] = (value, self.round_index + remaining, tuple(actions))

    def set_type(self, key, bey_type):
        new_type = normalise_type(bey_type)
        if self.types[key] != new_type:
            self.types[key] = new_type
            self.states[key] = GimmickState()

    def begin_round(self, actions, *, started=False):
        if not started:
            self.start_round()
        logs = []
        for key, action in actions.items():
            state = self.states[key]
            state.critical_strike_active = False
            state.kinetic_counter_active = False
            state.overdrive_active = False
            natural, required = GIMMICKS.get(self.types[key], (None, None))
            for name in GIMMICK_NAMES:
                forced = self.is_forced(key, name)
                valid = name == natural and (required is None or action == required)
                if self.is_suppressed(key, name):
                    if name == 'adaptive_morph':
                        state.adaptive_morph_rounds = 0
                    continue
                if not (valid or forced):
                    continue
                chance = min(1.0, max(0.0, .05 + self.chance_bonus.get((key, name), 0)
                                      + sum(self._values(key, name, 'gimmick_chance'))))
                if not forced and self.rng() >= chance:
                    continue
                self._activate(key, name)
                if forced:
                    self.action_overrides[(key, name)] = self.allowed_actions(key, name)
                logs.append(f"✨ **{name.replace('_', ' ').title()}** — {key} activates!")
        return logs

    def end_round(self):
        for state in self.states.values():
            state.adaptive_morph_rounds = max(0, state.adaptive_morph_rounds - 1)
        self.effects = {token: entry for token, entry in self.effects.items()
                        if entry[1] > self.round_index}

    def stat_multiplier(self, key):
        state = self.states[key]
        bonus = self.morph_bonus.get(key, 0) + sum(self._values(key, 'adaptive_morph', 'gimmick_bonus', 'stats'))
        return 1 + max(0, MORPH_BONUS + bonus) if state.adaptive_morph_active else 1

    def critical_applies(self, key, move):
        return (self.states[key].critical_strike_active
                and not self.is_suppressed(key, 'critical_strike')
                and move in self.action_overrides.get((key, 'critical_strike'), {'attack'}))

    def critical(self, key, move, damage, logs):
        if self.critical_applies(key, move):
            multiplier = max(0, self.critical_multiplier.get(key, 2.0)
                             + sum(self._values(key, 'critical_strike', 'gimmick_bonus', 'damage')))
            logs.append(f"💥 **Critical Strike** — damage ×{multiplier:g}!")
            return damage * multiplier
        return damage

    def mitigate(self, attacker, defender, move, damage, *, true_damage=False,
                 bypass_reduction=False, pierce_pct=0):
        if true_damage:
            return damage
        if not bypass_reduction and self.types[defender] == "defense":
            damage *= 1 - defense_reduction(self.types[defender]) * (1 - min(100, max(0, pierce_pct)) / 100)
        state = self.states[defender]
        if state.kinetic_counter_active and not self.is_suppressed(defender, 'kinetic_counter'):
            if move == "attack" and not self.critical_applies(attacker, move):
                return 0.0
            if move == "special" and not bypass_reduction:
                damage *= .70
        return max(0, damage)

    def returns_damage(self, attacker, defender, move):
        return (move == "attack" and self.critical_applies(attacker, move)
                and self.states[defender].kinetic_counter_active
                and not self.is_suppressed(defender, 'kinetic_counter'))

    def nullifies(self, attacker, defender, move, *, true_damage=False):
        return (not true_damage and move == 'attack'
                and self.states[defender].kinetic_counter_active
                and not self.is_suppressed(defender, 'kinetic_counter')
                and not self.critical_applies(attacker, move))

    def recovery(self, key, stamina_stat):
        if self.states[key].overdrive_active:
            bonus = self.overdrive_bonus.get(key, {})
            bonus = {field: bonus.get(field, 0) + sum(self._values(key, 'overdrive', 'gimmick_bonus', field))
                     for field in ('heal', 'stability', 'stamina')}
            return (max(0, stamina_stat * (1 + bonus['heal'])),
                    max(0, 30 + bonus['stability']), max(0, 6 + bonus['stamina']))
        return stamina_stat * (.70 if self.types[key] == "stamina" else .50), 8, None
