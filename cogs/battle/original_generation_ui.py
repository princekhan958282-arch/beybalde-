"""
cogs/battle/original_generation_ui.py
-------------------------------------
Compatibility adapter forwarding to the new unified skill_views module.
"""

from .skill_views import BladerSkillPicker, BitBeastSkillPicker, SkillPicker
from .battle_skills import COSTS

__all__ = ["BladerSkillPicker", "BitBeastSkillPicker", "SkillPicker", "COSTS"]
