"""
blade_abilities.py  —  making a player's blade abilities count in boss fights

The boss engine is deliberately separate from cogs/abilities/ability_engine.py
so that a boss can never break live PvP. The cost of that isolation was real
though: your blade's abilities did nothing in a boss fight, so a Mythic with a
worked-out kit fought exactly like a Common with the same raw stats.

This closes the gap WITHOUT importing the live engine. It reads the ability
definitions straight out of the blade dict and translates the effects that map
cleanly onto the boss combat model into a small set of modifiers.

What is supported
-----------------
    passive            stat boosts applied for the whole fight
    on_low_hp          the same, armed once you drop below the threshold
    on_take_damage     reflect, damage_reduction
    on_attack_win /    dmg_amp, ignore_def, true_damage, crit, heal_pct
    on_special

What is NOT supported, and why
------------------------------
The live engine runs a rules DSL (steal_hp, stacking counters, mode switches,
ability-disabling, multi-turn burns). Re-implementing that faithfully in a
second engine is how the two drift apart and start disagreeing about what a
blade does. So the DSL is left alone: unsupported effects are ignored rather
than half-implemented, and `unsupported()` reports them so it's visible which
parts of a kit are dormant here.
"""

from typing import Optional
from cogs.abilities.extended_effects import OPS as EXTENDED_OPS

# effect name → how the boss engine consumes it
STAT_EFFECTS = {
    "all_stats_boost": ("all", 1),
    "attack_boost":    ("attack", 1),
    "atk_buff":        ("attack", 1),
    "defense_boost":   ("defense", 1),
    "def_buff":        ("defense", 1),
    "special_boost":   ("special", 1),
}

COMBAT_EFFECTS = {
    "reflect", "damage_reduction", "dmg_amp", "ignore_def",
    "true_damage", "crit", "guaranteed_crit", "heal_pct", "shield",
}

# Everything the live engine does that this one deliberately doesn't touch.
UNSUPPORTED = {
    "activate_mode", "burn", "invulnerable", "disable_ability_2",
    "special_damage_amp", "heal", "steal_hp",
}

PASSIVE_TRIGGERS = {"passive"}
LOW_HP_TRIGGERS  = {"on_low_hp"}
HIT_TRIGGERS     = {"on_attack_win", "on_attack_hit", "on_hit", "on_special",
                    "on_defense_win", "on_stamina_win"}
DAMAGE_TRIGGERS  = {"on_take_damage"}

LOW_HP_THRESHOLD = 0.35

# Caps. A blade with three stacking passives shouldn't out-scale a boss's whole
# design; these keep abilities meaningful without letting them decide the fight.
MAX_STAT_BONUS   = 0.30      # +30% to any one stat
MAX_DMG_AMP      = 0.35
MAX_REDUCTION    = 0.35
MAX_REFLECT      = 40.0


def _num(chain_item: dict, *keys, default=0.0) -> float:
    for k in keys:
        v = chain_item.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return default


class BladeKit:
    """The modifiers one blade contributes to a boss fight."""

    def __init__(self, blade: Optional[dict]):
        self.blade = blade or {}
        self.name = self.blade.get("name", "?")

        self.stat_mult = {"attack": 1.0, "defense": 1.0, "stamina": 1.0}
        self.low_hp_mult = {"attack": 1.0, "defense": 1.0, "stamina": 1.0}
        self.dmg_amp = 0.0
        self.reduction = 0.0
        self.reflect = 0.0
        self.ignore_def = 0.0
        self.crit_chance = 0.0
        self.heal_pct = 0.0
        self.true_damage = False
        self.flat_damage = 0.0
        self.flat_reduction = 0.0

        self.applied: list[str] = []
        self.dormant: list[str] = []

        self._parse()

    # Most abilities in the database don't use a chain at all — they carry
    # flat tuning keys on the ability dict itself (passive_atk_bonus appears on
    # 14 blades, passive_dmg_reduction on 8). Reading only `chain` saw 25 of 89
    # abilities; these keys cover the rest.
    FLAT_KEYS = {
        # key                            (kind,        stat/target,  is_pct)
        "passive_atk_bonus":             ("stat",      "attack",     False),
        "attack_boost":                  ("stat",      "attack",     False),
        "mode_atk_bonus":                ("stat",      "attack",     False),
        "stack_attack":                  ("stat",      "attack",     False),
        "permanent_atk_boost":           ("stat",      "attack",     False),
        "defense_boost":                 ("stat",      "defense",    False),
        "initial_defense_buff":          ("stat",      "defense",    False),
        "low_spin_def_bonus":            ("stat",      "defense",    False),
        "mode_stamina_bonus":            ("stat",      "stamina",    False),
        "stamina_per_turn":              ("stat",      "stamina",    False),
        "passive_dmg_reduction":         ("reduction", None,         True),
        "damage_reduction":              ("reduction", None,         True),
        "burst_resistance_pct":          ("reduction", None,         True),
        "knockback_reduction_pct":       ("reduction", None,         True),
        "spin_versatility_dmg_reduction_pct": ("reduction", None,    True),
        "knockout_resistance_pct":       ("reduction", None,         True),
        "smash_dmg_amp_pct":             ("amp",       None,         True),
        "diabolos_dmg_amp_pct":          ("amp",       None,         True),
        "first_strike_dmg_amp_pct":      ("amp",       None,         True),
        "bonus_damage":                  ("amp",       None,         False),
        "ignore_def_pct":                ("pierce",    None,         True),
        "vs_defense_ignore_def_pct":     ("pierce",    None,         True),
        "crit_chance":                   ("crit",      None,         True),
        "stack_crit_rate":               ("crit",      None,         True),
        "claw_crit_rate":                ("crit",      None,         True),
        "evasion_chance":                ("reduction", None,         True),
        "reflect_damage":                ("reflect",   None,         False),
        "reflect_pct":                   ("reflect",   None,         False),
        "vs_attack_reflect_pct":         ("reflect",   None,         False),
        "heal_per_turn":                 ("heal",      None,         True),
        "streak_heal":                   ("heal",      None,         True),
        "absorb_pct":                    ("heal",      None,         True),
        "spin_steal_pct":                ("heal",      None,         True),
    }

    def _flat(self, ability: dict) -> bool:
        """Read the flat tuning keys. Returns True if anything applied."""
        touched = False
        low = (ability.get("trigger") or "").lower() in LOW_HP_TRIGGERS
        for key, (kind, stat, is_pct) in self.FLAT_KEYS.items():
            raw = ability.get(key)
            if not isinstance(raw, (int, float)) or raw <= 0:
                continue
            val = float(raw)
            if kind == "stat":
                # Flat bonuses are points; express them as a modest percentage
                # so a +20 ATK perk doesn't read as +2000%.
                pct = min(MAX_STAT_BONUS, (val / 100.0) if is_pct else (val / 120.0))
                target = self.low_hp_mult if low else self.stat_mult
                target[stat] = min(1 + MAX_STAT_BONUS, target[stat] + pct)
            elif kind == "reduction":
                self.reduction = min(MAX_REDUCTION, self.reduction + val / 100.0)
            elif kind == "amp":
                self.dmg_amp = min(MAX_DMG_AMP, self.dmg_amp
                                   + (val / 100.0 if is_pct else val / 100.0))
            elif kind == "pierce":
                self.ignore_def = max(self.ignore_def, min(0.6, val / 100.0))
            elif kind == "crit":
                self.crit_chance = max(self.crit_chance, min(0.35, val / 100.0))
            elif kind == "reflect":
                self.reflect = min(MAX_REFLECT, self.reflect + val)
            elif kind == "heal":
                self.heal_pct = min(0.25, self.heal_pct + val / 100.0)
            touched = True
        return touched

    # ── Parsing ──────────────────────────────────────────────────────────────
    def _parse(self) -> None:
        for ability in (self.blade.get("abilities") or []):
            trig = (ability.get("trigger") or "").lower()
            chain = ability.get("chain") or []
            touched = False

            for item in chain:
                eff = (item.get("effect") or "").lower()
                if eff in UNSUPPORTED:
                    continue

                if eff in STAT_EFFECTS:
                    stat, _ = STAT_EFFECTS[eff]
                    pct = _num(item, "value", "amount", "pct", default=10.0) / 100.0
                    pct = max(0.0, min(MAX_STAT_BONUS, pct))
                    target = (self.low_hp_mult if trig in LOW_HP_TRIGGERS
                              else self.stat_mult)
                    for s in (("attack", "defense", "stamina")
                              if stat in ("all", "special") else (stat,)):
                        target[s] = min(1 + MAX_STAT_BONUS, target[s] + pct)
                    touched = True

                elif eff == "reflect" and trig in DAMAGE_TRIGGERS | PASSIVE_TRIGGERS:
                    self.reflect = min(MAX_REFLECT,
                                       self.reflect + _num(item, "value", "amount",
                                                           default=15.0))
                    touched = True

                elif eff == "damage_reduction":
                    self.reduction = min(MAX_REDUCTION, self.reduction
                                         + _num(item, "value", "amount",
                                                default=10.0) / 100.0)
                    touched = True

                elif eff == "dmg_amp":
                    self.dmg_amp = min(MAX_DMG_AMP, self.dmg_amp
                                       + _num(item, "value", "amount",
                                              default=15.0) / 100.0)
                    touched = True

                elif eff == "ignore_def":
                    self.ignore_def = max(self.ignore_def,
                                          min(0.6, _num(item, "value", "amount",
                                                        default=30.0) / 100.0))
                    touched = True

                elif eff == "true_damage":
                    self.true_damage = True
                    touched = True

                elif eff in ("crit", "guaranteed_crit"):
                    self.crit_chance = max(
                        self.crit_chance,
                        1.0 if eff == "guaranteed_crit"
                        else min(0.35, _num(item, "chance", "value",
                                            default=15.0) / 100.0))
                    touched = True

                elif eff == "heal_pct":
                    self.heal_pct = min(0.25, self.heal_pct
                                        + _num(item, "value", "amount",
                                               default=5.0) / 100.0)
                    touched = True

            touched = self._flat(ability) or touched
            touched = self._rules(ability) or touched
            label = ability.get("name", "?")
            (self.applied if touched else self.dormant).append(label)

    # The DSL every blade in the database is actually authored in.
    #
    # `chain` and FLAT_KEYS above read two older shapes. Measured against the
    # live roster: `chain` covers 28 blades and FLAT_KEYS covers more, but 42
    # of 103 — 41%, including Void Longinus, Aetherion Vortex, Tartarus Reaper
    # and Revive Phoenix — matched neither shape and walked into a boss fight
    # with a completely empty kit. Their abilities were listed on the info
    # card, printed in the lobby, and did nothing.
    #
    # This reads `rules[].do[].op`, the shape `cogs/abilities/ability_engine`
    # runs. Only the ops that map cleanly onto the boss combat model are
    # translated; the rest still fall through to `dormant` and are reported by
    # `unsupported()`, because half-implementing a stacking counter here is how
    # the two engines start disagreeing about what a blade does.
    OP_TO_EFFECT = {
        "bonus_damage_pct":     "amp",
        "damage_boost":         "amp",
        "special_boost":        "amp",
        "crit_damage":          "amp",
        "reduce_damage_pct":    "reduction",
        "reflect_pct":          "reflect_pct",
        "reflect_flat":         "reflect",
        "reflect_pct_turns":    "reflect_pct",
        "crit_chance":          "crit",
        "guaranteed_crit":      "crit",
        "ignore_defense_pct":   "pierce",
        "ignore_defense":       "pierce",
        "lifesteal_pct":        "heal",
        "heal_pct":             "heal",
        "true_damage":          "true",
        "true_damage_turns":    "true",
        "buff":                 "stat",
        "buff_all":             "stat_all",
        "buff_all_pct":         "stat_all_pct",
        "stacking_buff":        "stat_stack",
        # Flat numbers, not percentages. `bonus_damage` alone appears on seven
        # of the blades that had no working kit at all, so folding it into the
        # percentage amp would have been wrong twice over — wrong units, and it
        # would have silently under-reported what those blades do.
        "bonus_damage":         "flat_damage",
        "reduce_damage_flat":   "flat_reduction",
        # A resistance that builds per impact. Translated at its FULL stacked
        # value (per_stack x max) rather than its opening value: a boss fight
        # runs long enough for a 5-stack cap to be reached almost immediately,
        # so reporting 7% here when the blade actually reaches 35% would
        # under-state the kit by a factor of five. `_rules` reads the two keys.
        "stacking_resist":      "reduction",
        # A percentage shield has no boss-side equivalent — the boss model has
        # no shield pool — so it maps to the same place a flat `shield` does.
        "shield_pct":           "shield",
        # A TIMED damage amp. Distinct from `bonus_damage_pct`, which is a
        # one-shot on the hit that fires it: `dmg_amp` is the only op that
        # actually tracks an expiry, so anything written as "x1.5 for N turns"
        # uses it — and it was missing from this table, which left the whole
        # ability dormant in a boss fight.
        "dmg_amp":              "amp",
    }

    # Caps for the two flat channels. Flat damage does not scale with the
    # wielder's stats, so at boss HP it is a modest, reliable top-up rather
    # than something that decides a fight.
    MAX_FLAT_DAMAGE    = 90.0
    MAX_FLAT_REDUCTION = 60.0

    def _rules(self, ability: dict) -> bool:
        """Read the rules/do/op DSL. Returns True if anything applied."""
        touched = False
        for rule in ability.get("rules") or []:
            when = str(rule.get("when") or "").lower()
            low = when in LOW_HP_TRIGGERS
            target = self.low_hp_mult if low else self.stat_mult
            for op in rule.get("do") or []:
                if op.get("op") in EXTENDED_OPS:
                    note = f"{ability.get('name', 'Ability')}: {op['op']} (shared engine only)"
                    if note not in self.dormant:
                        self.dormant.append(note)
                kind = self.OP_TO_EFFECT.get(str(op.get("op") or ""))
                if kind is None:
                    continue
                # `value` is the house default; `amount` and `per_stack` are
                # what buff and stacking_buff carry instead.
                val = _num(op, "value", "amount", "per_stack", default=0.0)
                # A stacking resistance is worth per_stack x max once it has
                # built, and a boss fight is long enough that it always does.
                # Its crit half is left out: the boss model has no crit
                # resistance to translate it into, and inflating the ordinary
                # reduction with it would over-report the kit.
                if str(op.get("op")) == "stacking_resist":
                    val *= max(1, int(op.get("max", 1) or 1))
                # `dmg_amp` carries a FRACTION (0.5 = +50%) where every other
                # op on this table carries a percentage. Normalising here rather
                # than in the "amp" branch keeps the unit conversion next to the
                # op that is unusual, instead of burying a special case inside
                # shared arithmetic.
                elif str(op.get("op")) == "dmg_amp" and val <= 1:
                    val *= 100.0
                if kind == "amp":
                    self.dmg_amp = min(MAX_DMG_AMP, self.dmg_amp + val / 100.0)
                elif kind == "reduction":
                    self.reduction = min(MAX_REDUCTION,
                                         self.reduction + val / 100.0)
                elif kind == "reflect":
                    self.reflect = min(MAX_REFLECT, self.reflect + val)
                elif kind == "reflect_pct":
                    # A percentage of an incoming hit, expressed as the flat
                    # number this engine reflects. Scaled off a mid-sized hit
                    # rather than guessed: the boss engine has no per-hit hook
                    # to read, so a percent has to become a constant somewhere.
                    self.reflect = min(MAX_REFLECT,
                                       self.reflect + val * 1.6)
                elif kind == "crit":
                    got = 1.0 if op.get("op") == "guaranteed_crit" \
                        else min(0.35, val / 100.0)
                    self.crit_chance = max(self.crit_chance, got)
                elif kind == "pierce":
                    got = 0.6 if op.get("op") == "ignore_defense" \
                        else min(0.6, val / 100.0)
                    self.ignore_def = max(self.ignore_def, got)
                elif kind == "heal":
                    self.heal_pct = min(0.25, self.heal_pct + val / 100.0)
                elif kind == "true":
                    self.true_damage = True
                elif kind == "flat_damage":
                    self.flat_damage = min(self.MAX_FLAT_DAMAGE,
                                           self.flat_damage + val)
                elif kind == "flat_reduction":
                    self.flat_reduction = min(self.MAX_FLAT_REDUCTION,
                                              self.flat_reduction + val)
                elif kind == "stat":
                    stat = str(op.get("stat") or "attack").lower()
                    if stat in target:
                        # Flat points, expressed as a modest percentage, the
                        # same conversion `_flat` uses — a +20 ATK buff must
                        # not read as +2000%.
                        target[stat] = min(1 + MAX_STAT_BONUS,
                                           target[stat] + val / 120.0)
                elif kind == "stat_stack":
                    stat = str(op.get("stat") or "attack").lower()
                    if stat in target:
                        stacks = max(1, int(op.get("max", 1) or 1))
                        target[stat] = min(1 + MAX_STAT_BONUS,
                                           target[stat] + val * stacks / 120.0)
                elif kind in ("stat_all", "stat_all_pct"):
                    per = val / (100.0 if kind == "stat_all_pct" else 120.0)
                    for s in target:
                        target[s] = min(1 + MAX_STAT_BONUS, target[s] + per)
                else:
                    continue
                touched = True
        return touched

    # ── Use ──────────────────────────────────────────────────────────────────
    def stats_for(self, hp_fraction: float) -> dict:
        """Multipliers right now — low-HP abilities arm below the threshold."""
        out = dict(self.stat_mult)
        if hp_fraction <= LOW_HP_THRESHOLD:
            for k, v in self.low_hp_mult.items():
                out[k] = min(1 + MAX_STAT_BONUS, out[k] * v)
        return out

    def summary(self) -> str:
        # Rounded to whole percents, so anything under 0.5% is dropped rather
        # than printed as "+0% STA" — a real kit should not read as broken
        # because one channel rounds to nothing.
        bits = []
        for s, v in self.stat_mult.items():
            if round((v - 1) * 100) >= 1:
                bits.append(f"+{(v - 1) * 100:.0f}% {s[:3].upper()}")
        if round(self.dmg_amp * 100) >= 1:
            bits.append(f"+{self.dmg_amp * 100:.0f}% dmg")
        if round(self.reduction * 100) >= 1:
            bits.append(f"−{self.reduction * 100:.0f}% taken")
        if round(self.reflect) >= 1:
            bits.append(f"{self.reflect:.0f} reflect")
        if round(self.ignore_def * 100) >= 1:
            bits.append(f"pierce {self.ignore_def * 100:.0f}%")
        if round(self.crit_chance * 100) >= 1:
            bits.append(f"{self.crit_chance * 100:.0f}% crit")
        if self.flat_damage:    bits.append(f"+{self.flat_damage:.0f} dmg")
        if self.flat_reduction: bits.append(f"−{self.flat_reduction:.0f} taken")
        if round(self.heal_pct * 100) >= 1:
            bits.append(f"{self.heal_pct * 100:.0f}% lifesteal")
        if any(v > 1.0 for v in self.low_hp_mult.values()):
            bits.append("low-HP surge")
        return " · ".join(bits) or "no boss-compatible effects"

    def unsupported(self) -> list[str]:
        """Abilities whose effects this engine deliberately doesn't run."""
        return list(self.dormant)


def kit_for(blade: Optional[dict]) -> BladeKit:
    return BladeKit(blade)
