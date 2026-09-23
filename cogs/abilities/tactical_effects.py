"""Opt-in, battle-local tactical effects. Only authored setup ops activate these.

All calculations use the normal damage, HP, shield and resource paths. State is
per session; no player database fields or existing Bey definitions are touched.
"""
import math
from copy import deepcopy

NAMES = (
    "pattern_reader", "pressure_gauge", "guard_fracture", "second_wind",
    "charge_overflow", "opening_gambit", "recoil_engine", "lasting_guard",
    "staggered_rhythm", "finishers_mark", "emergency_reserve",
    "shield_momentum", "clean_break", "counterweight", "perfect_timing",
    "rising_stakes", "battle_tempo", "sacrificial_guard",
    "stability_anchor", "precision_window", "spin_siphon", "exposed_core",
    "comeback_circuit", "measured_strike", "final_rotation",
)
OPS = frozenset(NAMES)
TYPES = {"stability_anchor": "defense", "precision_window": "attack",
         "spin_siphon": "stamina"}


class TacticalEffects:
    def __init__(self, engine):
        self.engine = engine
        self.session = engine.session
        self.owned = {}             # (player, op) -> authored config
        self.state = {}             # (player, op) -> battle-local counters
        self.previous = {}          # player's last completed action
        self.history = {}           # last two completed actions
        self.round_hits = {}
        for manager in (getattr(self.session, "stamina_manager", None),
                        getattr(self.session, "stability_manager", None)):
            if manager is not None:
                manager.tactical_runtime = self

    def has(self, key, name):
        return (key, name) in self.owned

    def data(self, key, name):
        return self.state.setdefault((key, name), {})

    def register(self, op, key, logs):
        name = op["op"]
        if op.get("target", "self") != "self":
            logs.append(f"⚠️ {name}: only self targeting is supported.")
            return
        from .type_system import normalise_type
        blade_type = normalise_type(self.session.blades.get(key, {}).get("type", ""))
        if name in TYPES and blade_type != TYPES[name]:
            logs.append(f"⚠️ {name}: requires {TYPES[name]} type.")
            return
        try:
            cfg = deepcopy(op)
            for field in ("pct", "amount", "cap"):
                if field in cfg:
                    number = float(cfg[field])
                    if not math.isfinite(number) or not 0 <= number <= 100:
                        raise ValueError(field)
                    cfg[field] = number
        except (ValueError, TypeError):
            logs.append(f"⚠️ {name}: invalid numeric config.")
            return
        self.owned[(key, name)] = cfg
        self.data(key, name)

    def add_gauge(self, key, amount):
        from cogs.battle.constants import SPECIAL_GAUGE_MAX
        gauge = self.session.stamina_manager.gauge
        before = gauge.get(key, 0)
        gauge[key] = min(SPECIAL_GAUGE_MAX, before + int(amount))
        return gauge[key] - before

    def gauge_gain(self, key, gain, source):
        """Adjust an existing gauge grant, preserving the normal 150 cap."""
        if self.has(key, "comeback_circuit") and self.data(key, "comeback_circuit").get("active"):
            gain = math.ceil(gain * 1.12)
        if self.has(key, "charge_overflow"):
            from cogs.battle.constants import SPECIAL_GAUGE_MAX
            old = self.session.stamina_manager.gauge.get(key, 0)
            overflow = max(0, old + gain - SPECIAL_GAUGE_MAX)
            d = self.data(key, "charge_overflow")
            if overflow and d.get("round") != getattr(self.session, "round", 0):
                self.session.status.add_shield(key, overflow // 2)
                d["round"] = getattr(self.session, "round", 0)
        return gain

    def cost_shortfall(self, key, cost):
        if not self.can_pay_shortfall(key, cost):
            return 0
        d = self.data(key, "emergency_reserve")
        current = self.session.stamina_manager.stamina.get(key, 0)
        shortage = max(0, cost - current)
        payment = math.ceil(shortage * 2)
        d["used"] = True
        self.session.hp[key] -= payment
        return shortage

    def can_pay_shortfall(self, key, cost):
        if not self.has(key, "emergency_reserve"):
            return False
        shortage = max(0, cost - self.session.stamina_manager.stamina.get(key, 0))
        return (shortage > 0 and not self.data(key, "emergency_reserve").get("used")
                and self.session.hp.get(key, 0) > math.ceil(shortage * 2))

    def special_spent(self, key, spent):
        if not self.has(key, "perfect_timing"):
            return 0
        d = self.data(key, "perfect_timing")
        if not d.pop("newly_ready", False):
            return 0
        return self.add_gauge(key, math.floor(spent * .20))

    def cleansed(self, key, cleared):
        if cleared and self.has(key, "clean_break"):
            self.data(key, "clean_break")["through"] = getattr(self.session, "round", 0) + 1

    def prevent_ring_out(self, key, old, delta):
        if old <= 0 or old + delta > 0 or not self.has(key, "stability_anchor"):
            return delta
        d = self.data(key, "stability_anchor")
        if d.get("used"):
            return delta
        d["used"] = True
        return 1 - old

    def crit_bonus(self, key, move):
        if move == "attack" and self.has(key, "precision_window"):
            return .10 if self.data(key, "precision_window").pop("ready", False) else 0
        return 0

    def round_start(self, key, other, move, enemy_move, stats, enemy_stats, logs):
        """Before any stats are used or costs are charged for this round."""
        sm = self.session.stamina_manager
        hp = self.session.hp
        self.round_hits[key] = 0
        if self.has(key, "pattern_reader") and self.previous.get(other) == enemy_move:
            d = self.data(key, "pattern_reader")
            d["against"] = enemy_move
        if self.has(key, "counterweight"):
            diff = max(0, enemy_stats.get("attack", 0) - stats.get("attack", 0))
            stats["defense"] += min(20, math.floor(diff * .10))
        if self.has(key, "exposed_core") and move in ("attack", "special"):
            d = self.data(key, "exposed_core")
            if d.pop("ready", False):
                d["consuming"] = True
                enemy_stats["defense"] = max(0, enemy_stats["defense"] * .90)
                logs.append("🎯 Exposed Core cuts through 10% Defense.")
        if self.has(key, "guard_fracture") and enemy_move == "defense":
            if self.data(key, "guard_fracture").pop("ready", False):
                enemy_stats["defense"] = max(0, enemy_stats["defense"] * .85)
                logs.append("🛡️ Guard Fracture weakens the opponent's Defense.")
        if self.has(key, "battle_tempo") and move == "charge":
            d = self.data(key, "battle_tempo")
            if d.get("skipped", 0) >= 3:
                self.add_gauge(key, 20)
                d["skipped"] = 0
                logs.append("🔋 Battle Tempo grants 20 extra Charge.")
        if self.has(key, "final_rotation") and getattr(self.session, "round", 1) >= 8:
            d = self.data(key, "final_rotation")
            if not d.get("used"):
                d["used"] = True
                missing = sm.cap_for(key) - sm.stamina.get(key, 0)
                if missing >= 15:
                    sm.stamina[key] = min(sm.cap_for(key), sm.stamina[key] + 15)
                    logs.append("🌀 Final Rotation restores 15 Stamina.")
                else:
                    self.add_gauge(key, 25)
                    logs.append("🌀 Final Rotation grants 25 Charge.")
        if self.has(key, "comeback_circuit"):
            max_hp = self.session.max_hp_per_player
            self.data(key, "comeback_circuit")["active"] = (
                hp[key] / max(1, max_hp[key]) < hp[other] / max(1, max_hp[other])
                and sm.stamina[key] < sm.stamina[other])
        if self.has(key, "perfect_timing"):
            from cogs.battle.special_gate import ready as special_ready
            blade = self.session.blades[key]
            ready = special_ready(self.session, key, blade, sm.gauge.get(key, 0))
            d = self.data(key, "perfect_timing")
            d["newly_ready"] = bool(ready and not d.get("was_ready"))
            d["was_ready"] = ready

    def before_damage(self, key, other, move, matchup, damage, logs, first=True, last=True):
        if not first:
            if move == "special" and self.has(key, "finishers_mark"):
                d = self.data(key, "finishers_mark")
                if d.get("active_special") and damage > 0:
                    damage = math.ceil(damage * 1.25)
                if last:
                    d.pop("active_special", None)
            return damage
        if self.has(key, "opening_gambit"):
            d = self.data(key, "opening_gambit")
            if move == "attack" and not d.get("used"):
                d["used"] = True
                if matchup == "win":
                    damage = math.ceil(damage * 1.20)
                else:
                    self.add_gauge(key, 15)
        if damage <= 0:
            return damage
        for name, scale in (("pressure_gauge", .08), ("rising_stakes", .05)):
            if self.has(key, name) and matchup == "win":
                d = self.data(key, name)
                stacks = d.pop("stacks", 0)
                if stacks:
                    damage = math.ceil(damage * (1 + stacks * scale))
                    logs.append(f"✨ {name.replace('_', ' ').title()} consumes {stacks} stacks.")
        if self.has(key, "recoil_engine") and self.data(key, "recoil_engine").pop("ready", False):
            damage = math.ceil(damage * 1.15)
        if self.has(key, "measured_strike") and self.data(key, "measured_strike").pop("ready", False):
            damage = math.ceil(damage * .85)
            sm = self.session.stamina_manager
            sm.stamina[key] = min(sm.cap_for(key), sm.stamina[key] + 8)
        if self.has(key, "finishers_mark") and move == "special":
            d = self.data(key, "finishers_mark")
            if d.get("marks", 0) >= 3:
                d["marks"] = 0
                damage = math.ceil(damage * 1.25)
                d["active_special"] = not last
        return damage

    def mitigate(self, key, other, move, incoming, logs):
        if incoming <= 0:
            return incoming
        if self.has(key, "pattern_reader") and self.data(key, "pattern_reader").get("against") == move:
            incoming = math.ceil(incoming * .88)
            logs.append("🔎 Pattern Reader reduces the repeated action's damage.")
        if self.has(key, "lasting_guard") and self.data(key, "lasting_guard").get("active"):
            incoming = math.ceil(incoming * .75)
            logs.append("🛡️ Lasting Guard carries protection into this turn.")
        if (self.has(key, "clean_break") and
                self.data(key, "clean_break").get("through", -1) >= getattr(self.session, "round", 0)):
            incoming = math.ceil(incoming * .90)
        return incoming

    def shield_absorbed(self, attacker, defender, amount, through, logs):
        if amount <= 0:
            return
        if self.has(defender, "shield_momentum"):
            d = self.data(defender, "shield_momentum")
            d["absorbed"] = d.get("absorbed", 0) + amount
            gained = min(15 - d.get("gained", 0), int(d["absorbed"] // 5) - d.get("gained", 0))
            if gained > 0:
                d["gained"] = d.get("gained", 0) + gained
                self.add_gauge(defender, gained)
        if through > 0 and self.has(attacker, "exposed_core"):
            self.data(attacker, "exposed_core")["ready"] = True

    def committed(self, attacker, defender, move, actual, logs):
        if actual <= 0:
            return
        self.round_hits[attacker] = self.round_hits.get(attacker, 0) + actual
        if self.has(attacker, "exposed_core"):
            self.data(attacker, "exposed_core").pop("consuming", None)
        if self.has(attacker, "finishers_mark"):
            d = self.data(attacker, "finishers_mark")
            d["marks"] = min(3, d.get("marks", 0) + 1)
        if self.has(attacker, "measured_strike"):
            cap = self.session.max_hp_per_player.get(defender, self.session.max_hp)
            if actual > cap * .25:
                self.data(attacker, "measured_strike")["ready"] = True
        if self.has(defender, "recoil_engine"):
            cap = self.session.max_hp_per_player.get(defender, self.session.max_hp)
            if actual >= cap * .20:
                self.data(defender, "recoil_engine")["ready"] = True
        if self.has(defender, "second_wind"):
            d = self.data(defender, "second_wind")
            cap = self.session.max_hp_per_player.get(defender, self.session.max_hp)
            if not d.get("used") and 0 < self.session.hp[defender] < cap * .30:
                d["used"] = True
                sm = self.session.stamina_manager
                sm.stamina[defender] = min(sm.cap_for(defender), sm.stamina[defender] + sm.cap_for(defender) * .20)
                logs.append("💨 Second Wind restores Stamina.")

    def round_end(self, key, other, move, enemy_move, matchup, logs):
        if self.has(key, "pressure_gauge"):
            d = self.data(key, "pressure_gauge")
            if self.round_hits.get(key, 0) <= 0:
                d["stacks"] = min(3, d.get("stacks", 0) + 1)
        if self.has(key, "rising_stakes") and matchup in ("lose", "lose_grind"):
            d = self.data(key, "rising_stakes")
            d["stacks"] = min(4, d.get("stacks", 0) + 1)
        if self.has(key, "guard_fracture"):
            d = self.data(key, "guard_fracture")
            if move == "defense" and matchup == "win":
                d["wins"] = d.get("wins", 0) + 1
            if d["wins"] >= 2:
                d["ready"] = True
                d["wins"] = 0
        if self.has(key, "precision_window") and move == "attack" and matchup == "win":
            self.data(key, "precision_window")["ready"] = True
        if self.has(key, "spin_siphon") and move == "stamina" and matchup == "win":
            sm = self.session.stamina_manager
            stolen = min(2, max(0, sm.stamina.get(other, 0)))
            sm.stamina[other] -= stolen
            sm.stamina[key] = min(sm.cap_for(key), sm.stamina[key] + stolen)
            if stolen:
                logs.append(f"🌀 Spin Siphon steals {stolen:g} Stamina.")
        if self.has(key, "lasting_guard"):
            d = self.data(key, "lasting_guard")
            d["active"] = move == "defense" and matchup == "win"
        if self.has(key, "staggered_rhythm"):
            d = self.data(key, "staggered_rhythm")
            history = self.history.get(key, [])
            if len(history) >= 2 and history[-2] == move and history[-1] != move:
                d["alternations"] = d.get("alternations", 0) + 1
                if d["alternations"] >= 2:
                    self.add_gauge(key, 20)
                    d["alternations"] = 0
            elif len(history) == 1 and history[-1] != move:
                d["alternations"] = 1
            elif history and history[-1] == move:
                d["alternations"] = 0
        if self.has(key, "battle_tempo"):
            d = self.data(key, "battle_tempo")
            d["skipped"] = 0 if move == "charge" else min(3, d.get("skipped", 0) + 1)
        if self.has(key, "shield_momentum"):
            self.data(key, "shield_momentum")["gained"] = 0
            self.data(key, "shield_momentum")["absorbed"] = 0
        if self.has(key, "exposed_core"):
            d = self.data(key, "exposed_core")
            if d.pop("consuming", False):
                d["ready"] = True
        if self.has(key, "pattern_reader"):
            self.data(key, "pattern_reader").pop("against", None)
        if self.has(key, "perfect_timing") and move == "special":
            self.data(key, "perfect_timing")["was_ready"] = False
        self.previous[key] = move
        self.history[key] = (self.history.get(key, []) + [move])[-2:]

    def redirect_ally_damage(self, protector, target, damage, protection_active=False):
        """Team-mode hook: caller applies returned (target, protector) damage.

        No current one-on-one resolver has an ally target. The hook remains
        inert until a future team resolver supplies one explicitly.
        """
        if protector == target or not protection_active or not self.has(protector, "sacrificial_guard"):
            return damage, 0
        amount = min(25, max(0, int(damage)))
        return damage - amount, amount
