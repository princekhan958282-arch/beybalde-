"""
battle/constants.py
-------------------
Central configuration for all global balance knobs, move names,
stamina costs, and counter relationships.
"""

# ── HP & Damage Knobs ─────────────────────────────────────────────────────────
# Starting HP for a battle. Raised 600 -> 4000; the boss pool
# (BASE_PLAYER_HP in boss_battle.py) moved with it so PvP and boss fights stay
# on the same scale. Anything that heals a PERCENTAGE of BASE_HP now heals
# proportionally more, which is why both were changed together.
BASE_HP              = 4000
WINNING_BONUS_MULT   = 1.5
LOSING_PENALTY_MULT  = 0.5
MIRROR_CHIP_DAMAGE   = 32     # still used by the Defense/Stamina mirrors

# Attack vs Attack: the attacker converts this fraction of the DEFENDER's
# defence stat into bonus attack before the damage roll. Armour gives the
# clash something to bite into instead of both sides trading a flat chip.
ATTACK_CLASH_DEF_CONVERSION = 0.20
# Both blades commit fully in a clash, so neither gets the winner's bonus nor
# the loser's penalty.
ATTACK_CLASH_MULT           = 1.0

# What a Special is worth when a blade has no special_move data at all, as a
# fraction of BASE_HP. Kept as a fraction so rescaling HP can't quietly turn
# the fallback into a rounding error.
SPECIAL_FALLBACK_HP_FRACTION = 0.60
BATTLE_TIMEOUT       = 300   # seconds per round before forfeit

# Normal-attack normalization.
# Normal-attack damage is  atk_stat × matchup_mult × (crit) .  Because attack
# stats reach 120–160 on high-tier blades, an un-normalized winning hit (or
# crit) could deal 300–500 on a 600-HP pool — more than a fully-charged Special
# (~130 total).  This scalar compresses the NORMAL-attack channel only
# (Specials, counters, and defense mitigation are unaffected) so Specials stay
# the biggest payoff.  Tune between 0.4 (harder-hitting defense meta) and 0.6
# (swingier).  1.0 restores the old un-normalized behaviour.
NORMAL_ATTACK_DAMAGE_SCALE = 0.5

# ── Special Gauge ─────────────────────────────────────────────────────────────
# Gauge fills from actions and damage taken.
# Full SPECIAL is usable at SPECIAL_GAUGE_MAX (150).
SPECIAL_GAUGE_MAX    = 150

# Attack counter bonus: Attack vs Stamina — kept above base win multiplier but not excessive
ATTACK_VS_STAMINA_MULT = 1.2   # Attack beating Stamina deals 1.2×

# Gauge generation per action / event
GAUGE_PER_ATTACK     = 15   # attacking
GAUGE_PER_DEFENSE    = 20   # defending
GAUGE_PER_STAMINA    = 10   # using the stamina action
GAUGE_PER_DMG_TAKEN  = 12   # each time you take damage
GAUGE_PER_CHARGE     = 50   # using the Charge action

# Legacy constant kept for any code that referenced SPECIAL_CHARGE_TURNS
SPECIAL_CHARGE_TURNS = 2

# ── Coin Rewards ──────────────────────────────────────────────────────────────
COINS_WIN  = 150
COINS_LOSS = 50

# ── Move Constants ────────────────────────────────────────────────────────────
MOVE_ATTACK  = "attack"
MOVE_DEFENSE = "defense"
MOVE_STAMINA = "stamina"
MOVE_SPECIAL = "special"
MOVE_CHARGE  = "charge"

MOVE_LABELS = {
    MOVE_ATTACK:  "⚔️ Attack (-1.5 Stamina)",
    MOVE_DEFENSE: "🛡️ Defense (-1.5 Stamina, Reduce dmg)",
    MOVE_STAMINA: "⚡ Use Stamina (+3 Stamina, Heal)",
    MOVE_SPECIAL: "🌟 SPECIAL (-3 Stamina)",
    MOVE_CHARGE:  "🔋 Charge (+50 Gauge, -1 Stamina)",
}

# ── Counter Relationships (rock-paper-scissors) ───────────────────────────────
# Key beats Value (1.5× damage via WINNING_BONUS_MULT; Attack vs Stamina uses
# ATTACK_VS_STAMINA_MULT = 1.2× instead).
# Defense does NOT counter Attack — it only mitigates damage.
COUNTER = {
    MOVE_ATTACK:  MOVE_STAMINA,   # Attack beats Stamina
    MOVE_STAMINA: MOVE_DEFENSE,   # Stamina beats Defense
}

# ── Stability ─────────────────────────────────────────────────────────────────
STABILITY_START_DEFAULT   = 100   # Attack / Stamina / Balance starting stability
STABILITY_START_DEFENSE   = 150   # Defense type starting stability

STABILITY_ATTACK_HIT      = -10   # attacker dealt damage
STABILITY_ATTACK_MISS     =  -4   # attacker dealt 0 damage
STABILITY_COUNTER_PENALTY =  -5   # attacker got countered
STABILITY_CLASH_PENALTY   = -10   # Attack vs Attack clash (applied to both)

STABILITY_DEF_PASSIVE     =  -6   # default defense tick
STABILITY_DEF_VS_ATTACK   =  -6   # hit by an attack move
STABILITY_DEF_VS_DEFENSE  =  -3   # mirror matchup
STABILITY_DEF_VS_STAMINA  =   0   # no effect
STABILITY_DEF_BLOCKED     =  +5   # absorbed all damage

STABILITY_STAMINA_RECOVERY = +25  # reward for using Stamina move
