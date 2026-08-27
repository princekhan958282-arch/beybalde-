"""
battle/constants.py
-------------------
Central configuration for all global balance knobs, move names,
stamina costs, and counter relationships.
"""

# ── HP & Damage Knobs ─────────────────────────────────────────────────────────
# Starting HP for a battle. History: 600 -> 4000 -> 2000.
#
# 4000 made a PvP bar far too deep to chew through. Measured over 400 random
# blade pairings, average damage lands around 21 a turn, which is ~189 turns to
# clear a 4000 bar and ~94 to clear this one. Real play is quicker than a random
# policy, but the ratio holds: at 4000 the fight outlasted anybody's patience.
#
# Everything expressed as a FRACTION of BASE_HP — percentage heals, the Special
# fallback (SPECIAL_FALLBACK_HP_FRACTION), chain heals — rescales with this
# automatically, which is why they are all written as fractions. The two things
# that do NOT are MIRROR_CHIP_DAMAGE below (a flat 32, so it now bites twice as
# hard relative to the bar) and boss_battle.BASE_PLAYER_HP, which is a
# deliberately separate pool and stays at 4000.
BASE_HP              = 2000
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

# How long the pre-battle avatar-skill picker waits for both players. Longer
# than ChallengeView's 30s — they have to read three skill descriptions, not
# press yes — while both players sit watching an empty channel. Timing out
# never blocks the battle; it just keeps whatever each player already picked.
#
# 60s was too tight for the reason that is easy to miss: the ephemeral dropdown
# gets its OWN full window starting when the button is pressed, and once a view
# times out discord.py drops later interactions on it silently — no log, and
# "The application did not respond" on the player's screen. Anyone who read the
# card for longer than the window would hit exactly the error this constant is
# meant to avoid.
SKILL_PROMPT_SECONDS = 120

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

# Deliberately WITHOUT a stamina cost in the text.
#
# These used to read "⚔️ Attack (-1.5 Stamina)" and friends, and every one of
# those numbers was wrong: they are boss_ai's table, not this game's, so the
# real prices were already 2.2 / 2.2 / 1.5 / 4.4 when the label said
# 1.5 / 1.5 / 1 / 3. Attack and Defense now scale with the blade's own stats
# on top of that, so no constant here CAN be right — the honest answer is per
# blade and per moment.
#
# `session.py` appends the real figure from `StaminaManager.cost_for`, which is
# the same call that charges it, so what the player is told and what they pay
# cannot drift. Anything else that shows a move name gets the name alone,
# which is at least true.
MOVE_LABELS = {
    MOVE_ATTACK:  "⚔️ Attack",
    MOVE_DEFENSE: "🛡️ Defense (Reduce dmg)",
    MOVE_STAMINA: "⚡ Use Stamina (Recover, Heal)",
    MOVE_SPECIAL: "🌟 SPECIAL",
    MOVE_CHARGE:  "🔋 Charge (+50 Gauge)",
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

# ── Button rework ─────────────────────────────────────────────────────────────
# The five buttons were thin: Charge was a skip-turn that no blade in the
# roster reacted to, Stability was a cliff at exactly 0 rather than a resource,
# and Special cost the same 150 gauge for everybody. The rework adds real
# mechanics to all of them WITHOUT moving any existing blade: every new
# mechanic below is neutral by default and a blade only gets it by authoring a
# `button_profile` block in beyblades.json (see cogs/battle/button_profile.py).
#
# This switch is the one lever that would apply the new baseline to the whole
# roster at once. It stays False deliberately: flipping it rebalances every
# existing matchup and re-tunes every sim, which is its own task and its own
# decision, not a side effect of shipping the capability.
BUTTON_REWORK_GLOBAL = False

# Stability tiers — the gradient that makes the whole bar matter instead of
# only its last point. Each entry is (fraction_of_max, effects) and the FIRST
# entry whose fraction the defender is at or below applies, so they must be
# ordered high to low.
#
# Empty by default: with no tiers, stability behaves exactly as it always has
# (invisible until it hits 0, then an instant ring-out). Populate this, or set
# a per-blade `button_profile.stability.tiers`, to switch the gradient on.
#
#   e.g. ((0.30, {"incoming_mult": 1.15}), (0.15, {"incoming_mult": 1.30}))
STABILITY_TIERS: tuple = ()

# ── Stat-scaled action cost ───────────────────────────────────────────────────
# Attack and Defense used to cost a flat 2.2 stamina each, so a 500-Attack
# monster paid exactly what a 47-Attack starter paid and stacking a stat cost
# nothing anywhere in the economy. Cost now scales with the stat the button
# actually uses, measured against the blade's OWN stamina stat:
#
#   cost = STAMINA_COST[move] * (1 + K * (stat / stamina_stat - 1))
#
# The ratio is the load-bearing part. A flat per-point curve
# (`2.2 + (stat - median) * 0.012`) was tried against real roster numbers and
# rejected: by level 100 every maxed blade pins the ceiling and converges on
# one clamped cost — the same "the stat stops paying partway up the curve"
# failure the STAMINA_MAX_* block below already had to fix once. Because both
# stats grow together, the ratio is level-invariant: Blood Dragon pays 3.25 at
# level 1 and 3.38 at level 100.
#
# Unlike everything else in this block, this one IS roster-wide — a cost curve
# only half the roster obeys is not a cost curve.
STAMINA_COST_STAT_WEIGHT = 0.5    # K: 0 disables scaling entirely
STAMINA_COST_MIN         = 1.2    # floor, so a glass cannon still pays something
STAMINA_COST_MAX         = 4.5    # ceiling, so a wall can still act
