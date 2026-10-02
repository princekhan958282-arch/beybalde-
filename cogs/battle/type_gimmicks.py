"""Battle-local type identity. No database writes or Discord dependencies."""
from dataclasses import dataclass
import math
import random
from cogs.abilities.type_system import normalise_type

GIMMICKS = {"attack": ("critical_strike", "attack"),
            "defense": ("kinetic_counter", "defense"),
            "stamina": ("overdrive", "stamina"),
            "balance": ("adaptive_morph", None)}
MORPH_ROUNDS = 3


def passive_stat_multiplier(bey_type, stat):
    t = normalise_type(bey_type)
    if t == "balance" and stat in ("hp", "attack", "defense", "stamina"):
        return 1.033
    return 1.10 if t == "attack" and stat == "attack" else 1.0


def base_damage(level, power, attack, defense):
    """Keep precision until HP commit. A denominator of at least 1 is mandatory."""
    return (((2 * max(1, float(level)) / 5 + 2) * max(0, float(power))
             * max(0, float(attack)) / max(1.0, float(defense))) / 50) + 2


def hp_damage(value):
    return max(0, math.floor(float(value)))


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

    def set_type(self, key, bey_type):
        new_type = normalise_type(bey_type)
        if self.types[key] != new_type:
            self.types[key] = new_type
            self.states[key] = GimmickState()

    def begin_round(self, actions):
        logs = []
        for key, action in actions.items():
            state = self.states[key]
            state.critical_strike_active = False
            state.kinetic_counter_active = False
            state.overdrive_active = False
            name, required = GIMMICKS.get(self.types[key], (None, None))
            if name is None:
                continue
            token = (key, name)
            valid = required is None or action == required
            if token in self.suppressed or not (valid or token in self.forced):
                continue
            chance = min(1.0, max(0.0, .05 + self.chance_bonus.get(token, 0)))
            if token not in self.forced and self.rng() >= chance:
                continue
            if name == "adaptive_morph":
                state.adaptive_morph_rounds = MORPH_ROUNDS
            else:
                setattr(state, name + "_active", True)
            logs.append(f"✨ **{name.replace('_', ' ').title()}** — {key} activates!")
        return logs

    def end_round(self):
        for state in self.states.values():
            state.adaptive_morph_rounds = max(0, state.adaptive_morph_rounds - 1)

    def stat_multiplier(self, key):
        state = self.states[key]
        return 1 + max(0, .05 + self.morph_bonus.get(key, 0)) if state.adaptive_morph_active else 1

    def critical(self, key, move, damage, logs):
        if move == "attack" and self.states[key].critical_strike_active:
            logs.append("💥 **Critical Strike** — Attack damage ×2!")
            return damage * self.critical_multiplier.get(key, 2.0)
        return damage

    def mitigate(self, attacker, defender, move, damage, *, true_damage=False,
                 bypass_reduction=False, pierce_pct=0):
        if true_damage:
            return damage
        if not bypass_reduction and self.types[defender] == "defense":
            damage *= 1 - .14 * (1 - min(100, max(0, pierce_pct)) / 100)
        state = self.states[defender]
        if state.kinetic_counter_active:
            if move == "attack" and not self.states[attacker].critical_strike_active:
                return 0.0
            if move == "special" and not bypass_reduction:
                damage *= .70
        return max(0, damage)

    def returns_damage(self, attacker, defender, move):
        return (move == "attack" and self.states[attacker].critical_strike_active
                and self.states[defender].kinetic_counter_active)

    def recovery(self, key, stamina_stat):
        if self.states[key].overdrive_active:
            bonus = self.overdrive_bonus.get(key, {})
            return (stamina_stat * (1 + bonus.get("heal", 0)),
                    30 + bonus.get("stability", 0), 6 + bonus.get("stamina", 0))
        return stamina_stat * (.70 if self.types[key] == "stamina" else .50), 8, None
