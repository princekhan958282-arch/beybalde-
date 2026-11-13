"""
cogs/battle/original_generation.py
----------------------------------
Compatibility forwarding to the new unified battle_skills engine.
"""

from .battle_skills import (
    BattleSkillManager,
    BattleSkillManager as OriginalGeneration,
    PlayerSkillState,
    PlayerSkillState as SkillState,
    BLADER_START_ENERGY,
    BIT_BEAST_START_ENERGY,
    BIT_BEAST_CHARGE_PER_TURN,
    MAX_ENERGY,
    STANDARD_SKILL_COSTS,
    COSTS,
    COOLDOWNS,
    get_battle_skills,
    get_battle_skills as runtime,
)

__all__ = [
    "BattleSkillManager",
    "OriginalGeneration",
    "PlayerSkillState",
    "SkillState",
    "BLADER_START_ENERGY",
    "BIT_BEAST_START_ENERGY",
    "BIT_BEAST_CHARGE_PER_TURN",
    "MAX_ENERGY",
    "STANDARD_SKILL_COSTS",
    "COSTS",
    "COOLDOWNS",
    "get_battle_skills",
    "runtime",
]
