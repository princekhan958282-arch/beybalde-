"""Approved V4 avatar progression balancing."""
MAX_CARD_LEVEL = 5
MAX_SKILL_LEVEL = 10
CARD_LEVEL_COSTS = (23_000, 40_000, 70_000, 123_000)
SKILL_COST_BASE = 12_000
SKILL_COST_RATIO = 1.35
SKILL_COST_ROUNDING = 500
SKILL_MAGNITUDE_STEP = .08
RESET_REFUND = .70
GROWTH = {
    'attack': {'attack': 9, 'defense': 3, 'stamina': 3},
    'defense': {'attack': 3, 'defense': 9, 'stamina': 3},
    'stamina': {'attack': 3, 'defense': 3, 'stamina': 9},
    'balance': {'attack': 5, 'defense': 5, 'stamina': 5},
}
MAX_STARS = 15
SAFE_STARS = 5
# Required cards, not a second fee after feeding.
STAR_COPY_COST = {star: (star - 1) * 5 for star in range(2, MAX_STARS + 1)}
STAR_SUCCESS = {2: 1., 3: 1., 4: 1., 5: 1., 6: .50, 7: .25, 8: .05}
STAR_STAT_GAIN = {1: 0, 2: 5, 3: 7, 4: 10, 5: 15, 6: 40, 7: 65,
                  8: 110, 9: 140, 10: 175, 11: 210, 12: 250,
                  13: 300, 14: 350, 15: 450}
REWARD_TIMEOUT = 180
REWARD_POLL_SECONDS = 30


def star_stat_total(star):
    return sum(STAR_STAT_GAIN[s] for s in range(2, max(1, min(MAX_STARS, int(star))) + 1))


# All skill types gain these stats per skill level above 1 while selected.
# Interactive skills receive them for EMPOWER_ROUNDS after activation.
SKILL_STAT_GAIN = {'attack': 2, 'defense': 2, 'stamina': 2}
EMPOWER_ROUNDS = 2
CONFIRM_TIMEOUT = 30
PROFILE_TIMEOUT = 180
EMPOWER_PERCENT_STEP = .02
SKILL_CAP_PER_CARD_LEVEL = 2
