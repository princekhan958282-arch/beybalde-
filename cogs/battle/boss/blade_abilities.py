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
            label = ability.get("name", "?")
            (self.applied if touched else self.dormant).append(label)

    # ── Use ──────────────────────────────────────────────────────────────────
    def stats_for(self, hp_fraction: float) -> dict:
        """Multipliers right now — low-HP abilities arm below the threshold."""
        out = dict(self.stat_mult)
        if hp_fraction <= LOW_HP_THRESHOLD:
            for k, v in self.low_hp_mult.items():
                out[k] = min(1 + MAX_STAT_BONUS, out[k] * v)
        return out

    def summary(self) -> str:
        bits = []
        for s, v in self.stat_mult.items():
            if v > 1.0:
                bits.append(f"+{(v - 1) * 100:.0f}% {s[:3].upper()}")
        if self.dmg_amp:    bits.append(f"+{self.dmg_amp * 100:.0f}% dmg")
        if self.reduction:  bits.append(f"−{self.reduction * 100:.0f}% taken")
        if self.reflect:    bits.append(f"{self.reflect:.0f} reflect")
        if self.ignore_def: bits.append(f"pierce {self.ignore_def * 100:.0f}%")
        if self.crit_chance:bits.append(f"{self.crit_chance * 100:.0f}% crit")
        if self.heal_pct:   bits.append(f"{self.heal_pct * 100:.0f}% lifesteal")
        if any(v > 1.0 for v in self.low_hp_mult.values()):
            bits.append("low-HP surge")
        return " · ".join(bits) or "no boss-compatible effects"

    def unsupported(self) -> list[str]:
        """Abilities whose effects this engine deliberately doesn't run."""
        return list(self.dormant)


def kit_for(blade: Optional[dict]) -> BladeKit:
    return BladeKit(blade)
