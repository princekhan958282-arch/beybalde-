"""
cogs/abilities/constants.py
---------------------------
Compatibility shim.

constants.py was moved to cogs/core/constants.py during folder reorganization.
This file re-exports everything from the canonical location so that files inside
cogs/abilities/ that use:
    from .constants import <name>
and files inside cogs/abilities/ability_handlers/ that use:
    from ..constants import <name>
continue to work without modification.
"""

from cogs.core.constants import *  # noqa: F401, F403
from cogs.core.constants import (
    BASE_HP,
    WINNING_BONUS_MULT,
    LOSING_PENALTY_MULT,
    MIRROR_CHIP_DAMAGE,
    BATTLE_TIMEOUT,
    SPECIAL_GAUGE_MAX,
    ATTACK_VS_STAMINA_MULT,
    GAUGE_PER_ATTACK,
    GAUGE_PER_DEFENSE,
    GAUGE_PER_STAMINA,
    GAUGE_PER_DMG_TAKEN,
    GAUGE_PER_CHARGE,
    SPECIAL_CHARGE_TURNS,
    COINS_WIN,
    COINS_LOSS,
    MOVE_ATTACK,
    MOVE_DEFENSE,
    MOVE_STAMINA,
    MOVE_SPECIAL,
    MOVE_CHARGE,
    MOVE_LABELS,
    COUNTER,
    STABILITY_START_DEFAULT,
    STABILITY_START_DEFENSE,
    STABILITY_ATTACK_HIT,
    STABILITY_ATTACK_MISS,
    STABILITY_COUNTER_PENALTY,
    STABILITY_CLASH_PENALTY,
    STABILITY_DEF_PASSIVE,
    STABILITY_DEF_VS_ATTACK,
    STABILITY_DEF_VS_DEFENSE,
    STABILITY_DEF_VS_STAMINA,
    STABILITY_DEF_BLOCKED,
    STABILITY_STAMINA_RECOVERY,
)
