"""All avatar progression balancing; level-one behaviour stays unchanged."""
MAX_CARD_LEVEL = 5
MAX_SKILL_LEVEL = 10
CARD_LEVEL_COSTS = (23_000, 40_000, 70_000, 123_000)
SKILL_COST_BASE = 12_000
SKILL_COST_RATIO = 1.35
SKILL_COST_ROUNDING = 500
SKILL_MAGNITUDE_STEP = .08
RESET_REFUND = .70
GROWTH = {
    'attack': {'attack': 12, 'defense': 5, 'stamina': 6},
    'defense': {'attack': 5, 'defense': 12, 'stamina': 6},
    'stamina': {'attack': 6, 'defense': 6, 'stamina': 12},
    'balance': {'attack': 8, 'defense': 8, 'stamina': 8},
}
MAX_STARS = 7
SAFE_STARS = 5
STAGE_COUNT = 5
STAGE_COPY_COST = 1
STAR_COPY_COST = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6}
STAR_SUCCESS = {2: 1., 3: 1., 4: 1., 5: 1., 6: .50, 7: .25}
STAR_STAT_GAIN = {'attack': 3, 'defense': 3, 'stamina': 3}
# All skill types gain these stats per skill level above 1 while selected.
# Interactive skills receive them for EMPOWER_ROUNDS after activation.
SKILL_STAT_GAIN = {'attack': 2, 'defense': 2, 'stamina': 2}
EMPOWER_ROUNDS = 2
CONFIRM_TIMEOUT = 30
PROFILE_TIMEOUT = 180
EMPOWER_PERCENT_STEP = .02
SKILL_CAP_PER_CARD_LEVEL = 2
