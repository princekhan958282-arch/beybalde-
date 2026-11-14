"""
cogs/battle/battle_skills.py
----------------------------
Unified Blader & Bit Beast Battle Skill Engine.

Architecture & Mechanics:
1. Blader Skill System:
   - Starts each battle with 100 Energy.
   - 3 skills per Blader:
     * Skill 1: 25 Energy
     * Skill 2: 50 Energy
     * Skill 3: 75 or 100 Energy (defaults to 75, or 100 if specified by card)
   - Skills are derived from the player's equipped Avatar card (or base Blader skills).
   - Energy does not automatically regenerate during battle; skills consume energy.

2. Bit Beast Skill System:
   - Starts each battle with 0 Energy.
   - Automatically gains 15 Energy at the beginning of every turn (Turn 1: 15, Turn 2: 30, Turn 3: 45, etc.).
   - Maximum Bit Beast Energy: 100 (capped).
   - Charging occurs strictly once per turn without player interaction.
   - 3 skills per Bit Beast:
     * Skill 1: 25 Energy
     * Skill 2: 50 Energy
     * Skill 3: 75 or 100 Energy
   - Skills are derived from the player's equipped Beyblade's Bit Beast spirit.

3. Battle Engine Integration:
   - Both skills can be activated during each round's decision window.
   - Energy deductions are validated, preventing negative values or duplicate activations.
   - Applied effects integrate seamlessly into combat formulas, stat modifiers, shields,
     damage filters, stamina adjustments, and battle log broadcasting.
"""

from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional, Tuple

# Standard Energy Constants
BLADER_START_ENERGY: int = 100
BIT_BEAST_START_ENERGY: int = 0
BIT_BEAST_CHARGE_PER_TURN: int = 15
MAX_ENERGY: int = 100

STANDARD_SKILL_COSTS: Tuple[int, int, int] = (25, 50, 75)
COSTS = STANDARD_SKILL_COSTS
COOLDOWNS = (3, 4, 0)


class PlayerSkillState:
    """Tracks isolated skill and energy state for one player in battle."""

    def __init__(
        self,
        energy: int = BLADER_START_ENERGY,
        pending: int = 0,
        blader_energy: Optional[int] = None,
        blader_pending: Optional[int] = None,
        bit_beast_energy: int = BIT_BEAST_START_ENERGY,
        bit_beast_pending: Optional[int] = None,
        effects: Optional[dict] = None,
        streak: int = 0,
        previous_move: Optional[str] = None,
        cooldowns: Optional[dict] = None,
        sealed: Optional[dict] = None,
        rage: int = 0,
        last_skill: int = 0,
        used_round: int = 0,
        ultimate_used: bool = False,
        previous: Optional[str] = None,
        last_charged_turn: int = 0,
    ) -> None:
        self.blader_energy = blader_energy if blader_energy is not None else energy
        self.blader_pending = blader_pending if blader_pending is not None else (pending if pending else None)
        self.blader_used_round = used_round
        self.blader_active_skill = None
        self.blader_energy_deducted = False

        self.bit_beast_energy = bit_beast_energy
        self.bit_beast_pending = bit_beast_pending
        self.bit_beast_used_round = 0
        self.bit_beast_active_skill = None
        self.last_charged_turn = last_charged_turn

        self.effects = dict(effects) if effects is not None else {}
        self.streak = streak
        self.previous_move = previous_move or previous
        self.cooldowns = dict(cooldowns) if cooldowns is not None else {}
        self.sealed = dict(sealed) if sealed is not None else {}
        self.rage = rage
        self.last_skill = last_skill
        self.used_round = used_round
        self.ultimate_used = ultimate_used
        self.previous = previous or previous_move

    @property
    def energy(self) -> int:
        return self.blader_energy

    @energy.setter
    def energy(self, value: int) -> None:
        self.blader_energy = max(0, min(MAX_ENERGY, int(value)))

    @property
    def pending(self) -> int:
        return self.blader_pending or 0

    @pending.setter
    def pending(self, value: int) -> None:
        self.blader_pending = value if value else None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PlayerSkillState):
            return False
        return (
            self.blader_energy == other.blader_energy
            and self.blader_pending == other.blader_pending
            and self.bit_beast_energy == other.bit_beast_energy
            and self.bit_beast_pending == other.bit_beast_pending
            and self.effects == other.effects
            and self.streak == other.streak
            and self.cooldowns == other.cooldowns
            and self.sealed == other.sealed
            and self.rage == other.rage
            and self.last_skill == other.last_skill
            and self.used_round == other.used_round
            and self.ultimate_used == other.ultimate_used
        )


# Legacy alias
SkillState = PlayerSkillState


# ── Archetype Bit Beast Skills ──────────────────────────────────────────────────

DEFAULT_BIT_BEAST_SKILLS = {
    "Attack": [
        {
            "name": "Beast Fang",
            "cost": 25,
            "description": "Infuses contact points with savage energy: +25 Attack damage this round.",
            "effect_type": "attack_flat",
            "value": 25,
        },
        {
            "name": "Predator Rush",
            "cost": 50,
            "description": "The Bit Beast roars and surges forward: +35% Attack power and +25% Crit chance.",
            "effect_type": "attack_pct_crit",
            "atk_pct": 0.35,
            "crit_pct": 0.25,
        },
        {
            "name": "Apex Cataclysm",
            "cost": 75,
            "description": "Unleashes supreme apex force: +50% Attack damage, ignores 30% enemy Defense, and -5 enemy Stability.",
            "effect_type": "apex_strike",
            "atk_pct": 0.50,
            "pierce": 0.30,
            "stability_drain": 5,
        },
    ],
    "Defense": [
        {
            "name": "Iron Shell",
            "cost": 25,
            "description": "Hardens the outer spin perimeter: reduces all incoming damage by 25% this round.",
            "effect_type": "mitigate_pct",
            "value": 0.25,
        },
        {
            "name": "Beast Citadel",
            "cost": 50,
            "description": "Forms a resonant barrier: grants a 40 HP shield and prevents ring-out this round.",
            "effect_type": "shield_barrier",
            "shield": 40,
        },
        {
            "name": "Impenetrable Bastion",
            "cost": 75,
            "description": "Impenetrable Bit Beast defense: reduces incoming damage by 50% and reflects 25% back.",
            "effect_type": "bastion_reflect",
            "mitigate_pct": 0.50,
            "reflect_pct": 0.25,
        },
    ],
    "Stamina": [
        {
            "name": "Spin Siphon",
            "cost": 25,
            "description": "Siphons rotational momentum: steals 3 battle stamina from the opponent.",
            "effect_type": "stamina_siphon",
            "drain": 3,
        },
        {
            "name": "Centrifugal Surge",
            "cost": 50,
            "description": "Restores 12 battle stamina and reinforces spin axis with +15 Stability.",
            "effect_type": "stamina_recover",
            "stamina": 12,
            "stability": 15,
        },
        {
            "name": "Eternal Resonance",
            "cost": 75,
            "description": "Bit Beast enters infinite spin: restores 20 battle stamina and cuts stamina drain for 2 rounds.",
            "effect_type": "eternal_spin",
            "stamina": 20,
        },
    ],
    "Balance": [
        {
            "name": "Harmonic Stance",
            "cost": 25,
            "description": "Harmonizes offensive and defensive spin: +20% Attack and +20% Defense this round.",
            "effect_type": "dual_buff",
            "atk_pct": 0.20,
            "def_pct": 0.20,
        },
        {
            "name": "Spiritual Shift",
            "cost": 50,
            "description": "Adapts to the clash: recovers 35 HP, +5 battle stamina, and adds +25 Special Gauge.",
            "effect_type": "spiritual_shift",
            "heal": 35,
            "stamina": 5,
            "gauge": 25,
        },
        {
            "name": "Dual Cataclysm",
            "cost": 75,
            "description": "Unleashes full resonance: +35% Attack, +35% Defense, and +35 Special Gauge.",
            "effect_type": "dual_cataclysm",
            "atk_pct": 0.35,
            "def_pct": 0.35,
            "gauge": 35,
        },
    ],
}

DEFAULT_BLADER_SKILLS = [
    {
        "name": "Blader Focus",
        "cost": 25,
        "description": "Sharpens focus: increases Attack and Defense by +15% this round.",
        "effect_type": "dual_buff",
        "atk_pct": 0.15,
        "def_pct": 0.15,
    },
    {
        "name": "Tactical Guard",
        "cost": 50,
        "description": "Strategic positioning: reduces incoming damage by 30% and adds +10 Stability.",
        "effect_type": "tactical_guard",
        "mitigate_pct": 0.30,
        "stability": 10,
    },
    {
        "name": "Blader's Awakening",
        "cost": 75,
        "description": "Unleashes full blader spirit: +35% Attack, +15 Stamina, and +25 Special Gauge.",
        "effect_type": "blader_awakening",
        "atk_pct": 0.35,
        "stamina": 15,
        "gauge": 25,
    },
]


BIT_BEAST_SIGNATURE_SKILLS = {
    "Wonder Valkyrie": [
        {
            "name": "Wonder Attack",
            "cost": 25,
            "description": "Unleashes an aggressive aerial assault: +25 Attack damage this round.",
            "effect_type": "attack_flat",
            "value": 25,
        },
        {
            "name": "Wonder Reboot",
            "cost": 50,
            "description": "Reboots spin velocity and triggers spring-loaded blades: +35% Attack power and +25% Crit chance.",
            "effect_type": "attack_pct_crit",
            "atk_pct": 0.35,
            "crit_pct": 0.25,
        },
        {
            "name": "Valkyrie Winged Storm",
            "cost": 75,
            "description": "Spreads divine wings in a catastrophic whirlwind: +50% Attack damage and pierces 30% enemy Defense.",
            "effect_type": "apex_strike",
            "atk_pct": 0.50,
            "pierce": 0.30,
        },
    ],
    "Dragoon": [
        {
            "name": "Hyper Tornado",
            "cost": 25,
            "description": "Conjures a swirling vortex: +25 Attack damage this round.",
            "effect_type": "attack_flat",
            "value": 25,
        },
        {
            "name": "Storm Surge",
            "cost": 50,
            "description": "A torrential gale surges across the arena: +35% Attack power and +25% Crit chance.",
            "effect_type": "attack_pct_crit",
            "atk_pct": 0.35,
            "crit_pct": 0.25,
        },
        {
            "name": "Galaxy Storm",
            "cost": 75,
            "description": "Unleashes the supreme dragon hurricane: +50% Attack damage, pierces 30% Defense, and drains 5 enemy Stability.",
            "effect_type": "apex_strike",
            "atk_pct": 0.50,
            "pierce": 0.30,
            "stability_drain": 5,
        },
    ],
    "Driger": [
        {
            "name": "Tiger Claw",
            "cost": 25,
            "description": "Swift tiger strike: +20% Attack and +20% Defense this round.",
            "effect_type": "dual_buff",
            "atk_pct": 0.20,
            "def_pct": 0.20,
        },
        {
            "name": "Vulcan Claw",
            "cost": 50,
            "description": "Spiritual balance surge: recovers 35 HP, +5 battle stamina, and adds +25 Special Gauge.",
            "effect_type": "spiritual_shift",
            "heal": 35,
            "stamina": 5,
            "gauge": 25,
        },
        {
            "name": "Thunder Slash Claw",
            "cost": 75,
            "description": "Fierce lightning claws: +35% Attack, +35% Defense, and +35 Special Gauge.",
            "effect_type": "dual_cataclysm",
            "atk_pct": 0.35,
            "def_pct": 0.35,
            "gauge": 35,
        },
    ],
    "Dranzer": [
        {
            "name": "Flame Saber",
            "cost": 25,
            "description": "Blazing fire slash: +25 Attack damage this round.",
            "effect_type": "attack_flat",
            "value": 25,
        },
        {
            "name": "Dranzer Rebirth",
            "cost": 50,
            "description": "The sacred phoenix rises from the ashes: grants a 40 HP shield and recovers 25 HP.",
            "effect_type": "phoenix_rebirth",
            "shield": 40,
            "heal": 25,
        },
        {
            "name": "Plasma Saber",
            "cost": 75,
            "description": "Incandescent plasma strike: +40% Attack damage and reduces incoming damage by 35%.",
            "effect_type": "plasma_saber",
            "atk_pct": 0.40,
            "mitigate_pct": 0.35,
        },
    ],
}


# ── BattleSkillManager ─────────────────────────────────────────────────────────

class BattleSkillManager:
    """
    Unified manager for Blader and Bit Beast skills in battle.
    Completely replaces legacy OriginalGeneration while preserving all combat hooks.
    """

    def __init__(self, session, *, states=None, rng=None):
        self.s = session
        self.rng = rng or random.random

        # Initialize PlayerSkillState per participant
        if states is not None:
            self.states = states
        elif not getattr(session, "avatars_enabled", True):
            self.states = {}
        else:
            self.states: Dict[str, PlayerSkillState] = {}
            for p in getattr(session, "players", []):
                key = str(p.id)
                self.states[key] = PlayerSkillState()
            # Also handle npc_controller if present
            if getattr(session, "npc_controller", None):
                npc_key = str(getattr(session.npc_controller, "key", "npc"))
                if npc_key not in self.states:
                    self.states[npc_key] = PlayerSkillState()

        # Combat hook mappings
        self.incoming = {}
        self.guard_saved = {}
        self.retaliation = []
        self.debuffs = {}
        self.prepared_round = 0
        self.completed_round = 0
        self.split_running = False
        self.split_success = {}
        self.action_cutter = {}
        self.action_bonus = {}
        self.action_pierce = {}
        self.sealed_bonus_snapshots = {}

    @property
    def round(self) -> int:
        return getattr(self.s, "round", 1)

    def _state_for(self, key: str) -> PlayerSkillState:
        if key not in self.states:
            if not getattr(self.s, "avatars_enabled", True):
                return PlayerSkillState(energy=0, blader_energy=0, bit_beast_energy=0)
            self.states[key] = PlayerSkillState()
        return self.states[key]

    def enemy(self, key: str) -> str:
        blades = getattr(self.s, "blades", {})
        return next((k for k in blades if k != key), key)

    # ── Turn-by-Turn Energy Charging ──────────────────────────────────────────

    def on_turn_start(self, turn_no: int) -> None:
        """
        Automatic Bit Beast Energy charging at the start of each turn:
        - Turn 1: 15 Energy
        - Turn 2: 30 Energy
        - Turn 3: 45 Energy
        - ...
        - Turn 7+: 100 Energy (capped at 100)
        Guaranteed to run strictly once per turn.
        """
        for key, st in self.states.items():
            if self.debuffs.get((key, 'energy_denial'), 0) >= turn_no:
                continue
            if st.last_charged_turn < turn_no:
                st.bit_beast_energy = min(MAX_ENERGY, st.bit_beast_energy + BIT_BEAST_CHARGE_PER_TURN)
                st.last_charged_turn = turn_no

    # ── Skill Resolvers ───────────────────────────────────────────────────────

    def get_blader_skills(self, key: str) -> List[dict]:
        """Return 3 Blader skills for this player (from Avatar card or defaults)."""
        card = (getattr(self.s, "avatar_cards", None) or {}).get(key) or {}
        if card.get("rarity") == "Bit Beast":
            out = []
            for i in range(1, 4):
                d = DEFAULT_BLADER_SKILLS[i - 1]
                out.append({
                    "slot": i,
                    "name": d["name"],
                    "cost": d["cost"],
                    "description": d["description"],
                    "raw": d,
                    "type": "blader",
                })
            return out
        skills = card.get("skills") or []
        out = []
        for i in range(1, 4):
            if i <= len(skills):
                raw = skills[i - 1]
                default_cost = 25 if i == 1 else (50 if i == 2 else 75)
                authored_cost = raw.get("energy_cost")
                if i == 3 and authored_cost in (75, 100):
                    cost = authored_cost
                elif authored_cost in (25, 50, 75, 100):
                    cost = authored_cost
                else:
                    cost = default_cost

                out.append({
                    "slot": i,
                    "name": raw.get("name", f"Blader Skill {i}"),
                    "cost": cost,
                    "description": raw.get("description", "A signature blader ability."),
                    "raw": raw,
                    "type": "blader",
                })
            else:
                d = DEFAULT_BLADER_SKILLS[i - 1]
                out.append({
                    "slot": i,
                    "name": d["name"],
                    "cost": d["cost"],
                    "description": d["description"],
                    "raw": d,
                    "type": "blader",
                })
        return out

    def get_bit_beast_skills(self, key: str) -> List[dict]:
        """Return 3 Bit Beast skills for this player (from equipped Bit Beast, Avatar card, or Beyblade archetype)."""
        bb_card = (getattr(self.s, "bit_beast_cards", None) or {}).get(key) or {}
        card = (getattr(self.s, "avatar_cards", None) or {}).get(key) or {}
        blade = (getattr(self.s, "blades", None) or {}).get(key) or {}

        # 1. Check if an equipped Bit Beast exists (or Avatar is a Bit Beast)
        target_bb = bb_card if bb_card else (card if card.get("rarity") == "Bit Beast" else {})
        if target_bb:
            if target_bb.get("skills"):
                out = []
                for i, raw in enumerate(target_bb["skills"][:3], 1):
                    cost = raw.get("cost") or raw.get("energy_cost") or (25 if i == 1 else (50 if i == 2 else 75))
                    out.append({
                        "slot": i,
                        "name": raw.get("name", f"Bit Beast Skill {i}"),
                        "cost": cost,
                        "description": raw.get("description", "A signature Bit Beast power."),
                        "raw": raw,
                        "type": "bit_beast",
                    })
                return out
            bb_name = target_bb.get("name", "")
            for sig_name, templates in BIT_BEAST_SIGNATURE_SKILLS.items():
                if sig_name.lower() in bb_name.lower():
                    out = []
                    for i, t in enumerate(templates, 1):
                        out.append({
                            "slot": i,
                            "name": t["name"],
                            "cost": t["cost"],
                            "description": t["description"],
                            "raw": t,
                            "type": "bit_beast",
                        })
                    return out

        # 2. Check for signature Bit Beast matching by blade name, character, or partner
        bname = (blade.get("name") or "").lower()
        c_anime = (card.get("anime_bey") or card.get("character") or "").lower()
        c_name = (card.get("name") or "").lower()

        matched_templates = None
        for bb_name, templates in BIT_BEAST_SIGNATURE_SKILLS.items():
            bb_key = bb_name.lower()
            if bb_key in bname or bb_key in c_anime or bb_key in c_name:
                matched_templates = templates
                break
            if bb_name == "Wonder Valkyrie" and "valkyrie" in bname:
                matched_templates = templates
                break

        if matched_templates:
            out = []
            for i, t in enumerate(matched_templates, 1):
                out.append({
                    "slot": i,
                    "name": t["name"],
                    "cost": t["cost"],
                    "description": t["description"],
                    "raw": t,
                    "type": "bit_beast",
                })
            return out

        # 3. Fall back to Beyblade archetype templates
        btype = (blade.get("type") or "Balance").capitalize()
        if btype not in DEFAULT_BIT_BEAST_SKILLS:
            btype = "Balance"

        templates = DEFAULT_BIT_BEAST_SKILLS[btype]
        out = []
        for i, t in enumerate(templates, 1):
            out.append({
                "slot": i,
                "name": t["name"],
                "cost": t["cost"],
                "description": t["description"],
                "raw": t,
                "type": "bit_beast",
            })
        return out

    # ── Energy Check Helpers ──────────────────────────────────────────────────

    def get_blader_energy(self, key: str) -> int:
        return self._state_for(key).blader_energy

    def get_bit_beast_energy(self, key: str) -> int:
        return self._state_for(key).bit_beast_energy

    def can_activate_blader_skill(self, key: str, slot: int) -> Tuple[bool, Optional[str]]:
        st = self._state_for(key)
        if getattr(self.s, "finished", False) or self.s.hp.get(key, 0) <= 0:
            return False, "This battle has ended."
        if st.blader_used_round == self.round or st.blader_pending:
            return False, "You already selected a Blader skill this round."
        skills = self.get_blader_skills(key)
        if not (1 <= slot <= len(skills)):
            return False, "Invalid skill slot."
        cost = skills[slot - 1]["cost"]
        if st.blader_energy < cost:
            return False, f"Need {cost}⚡ Blader Energy; you have {st.blader_energy}⚡."
        return True, None

    def can_activate_bit_beast_skill(self, key: str, slot: int) -> Tuple[bool, Optional[str]]:
        st = self._state_for(key)
        if getattr(self.s, "finished", False) or self.s.hp.get(key, 0) <= 0:
            return False, "This battle has ended."
        if st.bit_beast_used_round == self.round or st.bit_beast_pending:
            return False, "You already selected a Bit Beast skill this round."
        skills = self.get_bit_beast_skills(key)
        if not (1 <= slot <= len(skills)):
            return False, "Invalid skill slot."
        cost = skills[slot - 1]["cost"]
        if st.bit_beast_energy < cost:
            return False, f"Need {cost}⚡ Bit Beast Energy; you have {st.bit_beast_energy}⚡ (charging +15/turn)."
        return True, None

    # ── Skill Selection & Immediate Deduction ─────────────────────────────────

    def select_blader_skill(self, key: str, slot: int) -> Tuple[bool, str]:
        ok, reason = self.can_activate_blader_skill(key, slot)
        if not ok:
            return False, reason or "Cannot activate skill."
        st = self._state_for(key)
        skills = self.get_blader_skills(key)
        sk = skills[slot - 1]
        cost = sk["cost"]
        st.blader_energy = max(0, st.blader_energy - cost)
        st.blader_energy_deducted = True
        st.blader_pending = slot
        st.pending = slot
        st.blader_used_round = self.round
        st.used_round = self.round
        st.blader_active_skill = sk
        st.last_skill = slot
        return True, f"✅ Activated Blader Skill: **{sk['name']}** (-{cost}⚡)!"

    def select_bit_beast_skill(self, key: str, slot: int) -> Tuple[bool, str]:
        ok, reason = self.can_activate_bit_beast_skill(key, slot)
        if not ok:
            return False, reason or "Cannot activate skill."
        st = self._state_for(key)
        skills = self.get_bit_beast_skills(key)
        sk = skills[slot - 1]
        cost = sk["cost"]
        st.bit_beast_energy = max(0, st.bit_beast_energy - cost)
        st.bit_beast_pending = slot
        st.bit_beast_used_round = self.round
        st.bit_beast_active_skill = sk
        return True, f"✅ Activated Bit Beast Skill: **{sk['name']}** (-{cost}⚡)!"

    # ── Legacy OriginalGeneration API Compatibility ───────────────────────────

    def reason(self, key: str, slot: int) -> Optional[str]:
        st = self.states.get(key)
        if not st or not 1 <= slot <= 3:
            return 'No active Original Generation avatar skill.'
        if getattr(self.s, "finished", False) or self.s.hp.get(key, 0) <= 0:
            return 'This battle has ended.'
        if getattr(self.s, "moves", {}).get(key) is not None:
            return 'Choose your avatar skill before your normal move.'
        if st.pending or st.used_round == self.round or st.blader_pending or st.blader_used_round == self.round:
            return 'You already selected an avatar skill this round.'
        if st.sealed.get(slot, 0) >= self.round:
            return 'That avatar skill is sealed this round.'
        if slot == 3 and st.ultimate_used:
            return 'Your ultimate has already been used this battle.'
        if st.cooldowns.get(slot, 0) > self.round:
            return f"Ready in round {st.cooldowns[slot]}."
        skills = self.get_blader_skills(key)
        cost = skills[slot - 1]["cost"] if 1 <= slot <= len(skills) else STANDARD_SKILL_COSTS[slot - 1]
        if st.blader_energy < cost:
            return f'Need {cost} Avatar Energy; you have {st.blader_energy}.'
        return None

    def select(self, key: str, slot: int) -> Optional[str]:
        r = self.reason(key, slot)
        if r:
            return r
        st = self._state_for(key)
        st.pending = slot
        st.blader_pending = slot
        skills = self.get_blader_skills(key)
        sk = skills[slot - 1] if 1 <= slot <= len(skills) else None
        st.blader_active_skill = sk
        return None

    def effect(self, key: str, name: str) -> Optional[dict]:
        st = self.states.get(key)
        effect = st.effects.get(name) if st else None
        return effect if effect and effect.get('from', 0) <= self.round <= effect['until'] else None

    def put(self, key: str, name: str, turns: int = 1, **values) -> None:
        st = self._state_for(key)
        st.effects[name] = dict(until=self.round + turns - 1, **values)

    def heal(self, key: str, amount: int, logs: list[str], label: str) -> None:
        from .combat_rules import recover_hp
        from .purification import heal_amount
        amount = heal_amount(self.s, key, amount)
        before = self.s.hp.get(key, 0)
        max_hp = getattr(self.s, "max_hp_per_player", {}).get(key, getattr(self.s, "max_hp", 100))
        self.s.hp[key], actual = recover_hp(before, max_hp, amount)
        if actual:
            logs.append(f"✨ **{label}** — restores {actual} HP!")

    def stamina(self, key: str, amount: float) -> None:
        if self.s.hp.get(key, 0) > 0:
            sm = getattr(self.s, "stamina_manager", None)
            if sm:
                sm.stamina[key] = min(sm.cap_for(key), sm.stamina.get(key, 0) + amount)

    # ── Round Execution Hooks ─────────────────────────────────────────────────

    def begin_round(self, logs: list[str]) -> None:
        if self.prepared_round == self.round:
            return
        self.prepared_round = self.round

        # Ensure turn energy is charged
        self.on_turn_start(self.round)

        for key, original in list(self.sealed_bonus_snapshots.items()):
            if self.debuffs.get((key, 'skill_seal'), 0) < self.round:
                self.s.avatar_bonuses[key] = original
                self.sealed_bonus_snapshots.pop(key, None)

        self.incoming.clear()
        self.guard_saved.clear()
        self.retaliation.clear()
        self.action_bonus.clear()
        self.action_pierce.clear()
        self.split_success.clear()
        self.action_cutter.clear()

        # Apply Blader and Bit Beast skills activated for this round
        for key, st in self.states.items():
            other = self.enemy(key)
            moves = getattr(self.s, "moves", {}) or {}
            move = moves.get(key)
            st.streak = st.streak + 1 if move == 'attack' else 0

            # 1. Blader Skill Activation
            slot = st.blader_pending or st.pending
            st.pending = 0
            st.blader_pending = None
            if slot:
                card = (getattr(self.s, "avatar_cards", None) or {}).get(key) or {}
                skills = self.get_blader_skills(key)
                sk = skills[slot - 1] if 1 <= slot <= len(skills) else None
                cost = sk["cost"] if sk else STANDARD_SKILL_COSTS[slot - 1]
                name = sk["name"] if sk else f"Skill {slot}"

                if card.get('character') == 'tyson' and slot == 3 and move != 'special':
                    logs.append('🌪️ **Galaxy Turbo Twister** needs Special; energy was not spent.')
                    if getattr(st, "blader_energy_deducted", False):
                        st.blader_energy = min(MAX_ENERGY, st.blader_energy + cost)
                        st.blader_energy_deducted = False
                else:
                    if not getattr(st, "blader_energy_deducted", False):
                        st.blader_energy = max(0, st.blader_energy - cost)
                    st.blader_energy_deducted = False
                    st.used_round = self.round
                    st.blader_used_round = self.round
                    st.cooldowns[slot] = self.round + COOLDOWNS[slot - 1]
                    st.ultimate_used |= slot == 3
                    st.last_skill = slot
                    logs.append(f"⚡ **{card.get('name', 'Blader')}: {name}** — {cost} energy!")
                    self.activate(key, card.get('character'), slot, logs)
                    from cogs.avatar import avatar_config as C
                    levels = card.get('_skill_levels', [])
                    steps = levels[slot - 1] - 1 if slot <= len(levels) else 0
                    if steps:
                        self.put(key, 'skill_empower', C.EMPOWER_ROUNDS,
                                 attack=C.EMPOWER_PERCENT_STEP * steps,
                                 defense=C.EMPOWER_PERCENT_STEP * steps)

            # 2. Bit Beast Skill Activation
            if st.bit_beast_active_skill or st.bit_beast_pending:
                bbslot = st.bit_beast_pending
                st.bit_beast_pending = None
                bbsk = st.bit_beast_active_skill or (self.get_bit_beast_skills(key)[bbslot - 1] if bbslot else None)
                if bbsk:
                    blade = (getattr(self.s, "blades", None) or {}).get(key) or {}
                    bname = blade.get("name") or "Bit Beast"
                    logs.append(f"🐉 **{bname}: {bbsk['name']}** — {bbsk['cost']}⚡ Bit Beast Energy!")
                    self._apply_bit_beast_skill_effect(key, other, bbsk, logs)

        # Execute conditional skill effects
        for key, st in self.states.items():
            other = self.enemy(key)
            moves = getattr(self.s, "moves", {}) or {}
            if moves.get(key) == 'attack' and self.effect(key, 'cutter'):
                self.action_cutter[key] = True
                st.effects.pop('cutter', None)
            if self.effect(key, 'technique') and st.previous and st.previous != moves.get(key):
                self.put(key, 'technique_stats', attack=.15, defense=.15)
                if hasattr(self.s, "stability_manager"):
                    logs.extend(self.s.stability_manager._apply(key, 3))
            if self.effect(key, 'focus'):
                prev_move = getattr(self.s, 'og_previous_moves', {}).get(other)
                if prev_move and prev_move == moves.get(other):
                    self.action_pierce[key] = .25
            if self.effect(key, 'feint') and moves.get(other) == 'defense':
                self.guard_saved.setdefault(other, 0)
            if st.rage:
                if moves.get(key) in ('stamina', 'charge'):
                    st.rage = 0
                elif moves.get(key) == 'attack':
                    self.action_bonus[key] = .15
                    st.rage -= 1

    def activate(self, key: str, character: Optional[str], slot: int, logs: list[str]) -> None:
        other = self.enemy(key)
        if character == 'tyson':
            if slot == 1: self.put(key, 'comeback', 2)
            elif slot == 2: self.put(key, 'storm', 99)
            elif slot == 3: self.put(key, 'tornado', 2, enemy=other, start=self.round)
        elif character == 'kai':
            if slot == 1: self.put(key, 'focus')
            elif slot == 2: self.put(key, 'saber', 99)
            elif slot == 3: self.put(key, 'tempest', 99)
        elif character == 'ray':
            if slot == 1: self.put(key, 'tiger_claw', 99)
            elif slot == 2: self.put(key, 'technique')
            elif slot == 3: self.put(key, 'vulcan', 99)
        elif character == 'max':
            if slot == 1: self.put(key, 'shield', 2, hp=math.floor(getattr(self.s, "max_hp_per_player", {}).get(key, getattr(self.s, "max_hp", 100)) * .10))
            elif slot == 2: self.put(key, 'fortress', 2, stored=0)
            elif slot == 3: self.put(key, 'unbreakable', 2)
        elif character == 'mariah':
            if slot == 1: self.put(key, 'footwork')
            elif slot == 2: self.put(key, 'scratch', 99)
            elif slot == 3: self.put(key, 'ambush', 2)
        elif character == 'daichi':
            if slot == 1:
                buffs = getattr(self.s.status, "active_buffs", {}).get(key, [])
                self.s.status.active_buffs[key] = [b for b in buffs if not (
                    b.get('amount', 0) < 0 and b.get('stat') in ('stability_regen', 'stability_recovery'))]
                if hasattr(self.s, "stability_manager"):
                    logs.extend(self.s.stability_manager._apply(key, 6))
            elif slot == 2: self.put(key, 'cutter', 99)
            elif slot == 3: self.put(key, 'great_cutter', 99)
        elif character == 'lee':
            if slot == 1: self.put(key, 'mark', 2)
            elif slot == 2: self.put(key, 'lightning')
            elif slot == 3:
                enemy_st = self.states.get(other)
                if enemy_st and enemy_st.last_skill:
                    enemy_st.sealed[enemy_st.last_skill] = self.round + 1
                else:
                    self.debuffs[(other, 'skill_seal')] = self.round + 1
                    card = self.s.avatar_cards.get(other) or {}
                    selected = getattr(self.s, 'avatar_skill_slots', {}).get(other, 0) or 0
                    av = self.s.avatar_bonuses.get(other)
                    if av and selected and not card.get('stats_always_on'):
                        import copy
                        blank = copy.copy(av)
                        bonuses = card['skills'][selected - 1].get('bonuses') or {}
                        for field, amount in bonuses.items():
                            if field in ('hp_flat', 'hp_percent') or not hasattr(blank, field):
                                continue
                            if field.endswith('_flat'):
                                setattr(blank, field, max(0, getattr(blank, field) - amount))
                            else:
                                setattr(blank, field, False if isinstance(amount, bool) else 0)
                        self.sealed_bonus_snapshots.setdefault(other, av)
                        self.s.avatar_bonuses[other] = blank
        elif character == 'gary':
            if slot == 1: self.put(key, 'endurance', 2)
            elif slot == 2: self.put(key, 'axe', 99)
            elif slot == 3: self.states[key].rage = 3
        elif character == 'kevin':
            if slot == 1: self.put(key, 'feint')
            elif slot == 2: self.put(key, 'monkey', 99)
            elif slot == 3: self.put(key, 'trickery', 2)
        else:
            skills = self.get_blader_skills(key)
            sk = skills[slot - 1] if 1 <= slot <= len(skills) else {"slot": slot, "raw": {}}
            self._apply_blader_skill_effect(key, other, sk, logs)

    def _apply_blader_skill_effect(self, key: str, other: str, skill: dict, logs: list[str]) -> None:
        raw = skill.get("raw") or {}
        bonuses = raw.get("bonuses") or {}
        if bonuses:
            for bstat, bval in bonuses.items():
                if bstat == "attack_flat":
                    self.action_bonus[key] = self.action_bonus.get(key, 0) + (bval / 100.0)
                elif bstat == "attack_percent":
                    self.action_bonus[key] = self.action_bonus.get(key, 0) + bval
                elif bstat == "defence_flat":
                    self.put(key, 'guard_def', 1, defense=bval)

        eff_type = raw.get("effect_type")
        if eff_type == "dual_buff":
            self.action_bonus[key] = self.action_bonus.get(key, 0) + raw.get("atk_pct", 0.15)
            self.put(key, 'guard_def_pct', 1, pct=raw.get("def_pct", 0.15))
        elif eff_type == "tactical_guard":
            self.put(key, 'tactical_mitigate', 1, pct=raw.get("mitigate_pct", 0.30))
            if hasattr(self.s, "stability_manager"):
                self.s.stability_manager._apply(key, raw.get("stability", 10))
        elif eff_type == "blader_awakening":
            self.action_bonus[key] = self.action_bonus.get(key, 0) + raw.get("atk_pct", 0.35)
            self.stamina(key, raw.get("stamina", 15))
            if hasattr(self.s, "stamina_manager"):
                sm = self.s.stamina_manager
                sm.gauge[key] = min(150, sm.gauge.get(key, 0) + raw.get("gauge", 25))

    def _apply_bit_beast_skill_effect(self, key: str, other: str, skill: dict, logs: list[str]) -> None:
        raw = skill.get("raw") or {}
        eff_type = raw.get("effect_type")

        if eff_type == "attack_flat":
            self.action_bonus[key] = self.action_bonus.get(key, 0) + (raw.get("value", 25) / 100.0)
        elif eff_type == "attack_pct_crit":
            self.action_bonus[key] = self.action_bonus.get(key, 0) + raw.get("atk_pct", 0.35)
            self.put(key, 'crit_boost', 1, crit=raw.get("crit_pct", 0.25))
        elif eff_type == "apex_strike":
            self.action_bonus[key] = self.action_bonus.get(key, 0) + raw.get("atk_pct", 0.50)
            self.action_pierce[key] = max(self.action_pierce.get(key, 0), raw.get("pierce", 0.30))
            if hasattr(self.s, "stability_manager"):
                self.s.stability_manager._apply(other, -raw.get("stability_drain", 5))
        elif eff_type == "mitigate_pct":
            self.put(key, 'bb_mitigate', 1, pct=raw.get("value", 0.25))
        elif eff_type == "shield_barrier":
            self.put(key, 'shield', 1, hp=raw.get("shield", 40))
        elif eff_type == "bastion_reflect":
            self.put(key, 'bb_mitigate', 1, pct=raw.get("mitigate_pct", 0.50), reflect=raw.get("reflect_pct", 0.25))
        elif eff_type == "stamina_siphon":
            drain = raw.get("drain", 3)
            sm = getattr(self.s, "stamina_manager", None)
            if sm:
                actual = min(drain, sm.stamina.get(other, 0))
                sm.stamina[other] = max(0.0, sm.stamina.get(other, 0) - actual)
                sm.stamina[key] = min(sm.cap_for(key), sm.stamina.get(key, 0) + actual)
                logs.append(f"🌀 **Spin Siphon** — siphons {actual:g} battle stamina from opponent!")
        elif eff_type == "stamina_recover":
            self.stamina(key, raw.get("stamina", 12))
            if hasattr(self.s, "stability_manager"):
                self.s.stability_manager._apply(key, raw.get("stability", 15))
        elif eff_type == "eternal_spin":
            self.stamina(key, raw.get("stamina", 20))
            self.put(key, 'drain_immunity', 2)
        elif eff_type == "dual_buff":
            self.action_bonus[key] = self.action_bonus.get(key, 0) + raw.get("atk_pct", 0.20)
            self.put(key, 'guard_def_pct', 1, pct=raw.get("def_pct", 0.20))
        elif eff_type == "spiritual_shift":
            self.heal(key, raw.get("heal", 35), logs, "Spiritual Shift")
            self.stamina(key, raw.get("stamina", 5))
            if hasattr(self.s, "stamina_manager"):
                sm = self.s.stamina_manager
                sm.gauge[key] = min(150, sm.gauge.get(key, 0) + raw.get("gauge", 25))
        elif eff_type == "dual_cataclysm":
            self.action_bonus[key] = self.action_bonus.get(key, 0) + raw.get("atk_pct", 0.35)
            self.put(key, 'guard_def_pct', 1, pct=raw.get("def_pct", 0.35))
            if hasattr(self.s, "stamina_manager"):
                sm = self.s.stamina_manager
                sm.gauge[key] = min(150, sm.gauge.get(key, 0) + raw.get("gauge", 35))
        elif eff_type == "phoenix_rebirth":
            self.put(key, 'shield', 1, hp=raw.get("shield", 40))
            self.heal(key, raw.get("heal", 25), logs, "Dranzer Rebirth")
        elif eff_type == "plasma_saber":
            self.action_bonus[key] = self.action_bonus.get(key, 0) + raw.get("atk_pct", 0.40)
            self.put(key, 'bb_mitigate', 1, pct=raw.get("mitigate_pct", 0.35))

    # ── Combat Modifiers for Managers ─────────────────────────────────────────

    def stat_multiplier(self, key: str, stat: str) -> float:
        e = self.effect(key, 'technique_stats')
        empower = self.effect(key, 'skill_empower')
        gdef = self.effect(key, 'guard_def_pct')
        mult = 1.0 + (e.get(stat, 0) if e else 0) + (empower.get(stat, 0) if empower else 0)
        if stat == 'defense' and gdef:
            mult += gdef.get('pct', 0)
        return mult

    def pierce(self, key: str) -> float:
        return self.action_pierce.get(key, 0.0)

    def cost(self, key: str, move: str, current_cost: float) -> float:
        if self.action_cutter.get(key) and move == 'attack':
            current_cost += 0.3
        other = self.enemy(key)
        tornado = self.effect(other, 'tornado')
        if tornado and self.round > tornado.get('start', 0):
            current_cost += 0.7
        prev = getattr(self.s, 'og_previous_moves', {}).get(key)
        if self.effect(other, 'trickery') and prev and prev == move:
            current_cost += 0.4
        return max(0.0, round(current_cost, 2))

    def blocked_move(self, key: str, move: str) -> Optional[str]:
        if move == 'defense' and self.effect(key, 'no_defense'):
            return 'Attack prevents Defense this round.'
        return None

    def before_damage(self, key: str, other: str, move: str, damage: int, logs: list[str], first: bool = True) -> int:
        if move not in ('attack', 'special') or damage <= 0:
            return damage
        bonus = self.action_bonus.get(key, 0.0)
        st = self._state_for(key)
        if self.effect(key, 'storm') and move == 'attack':
            bonus += min(0.30, 0.10 * st.streak)
            if first: st.effects.pop('storm', None)
            self.action_bonus[key] = bonus
        if self.effect(key, 'ambush') and move == 'attack':
            other_move = (getattr(self.s, "moves", {}) or {}).get(other)
            if other_move in ('stamina', 'charge'):
                bonus += 0.40
                if first: st.effects.pop('ambush', None)
                self.action_bonus[key] = bonus
        if self.effect(key, 'axe') and move == 'attack':
            bonus += 0.35
            if first:
                st.effects.pop('axe', None)
                st.effects['no_defense'] = {'until': self.round + 1, 'from': self.round + 1}
            self.action_bonus[key] = bonus
        if self.effect(key, 'great_cutter'):
            sm = getattr(self.s, "stability_manager", None)
            if sm:
                bonus += min(0.35, max(0, sm.max[other] - sm.stability[other]) * 0.01)
            if first: st.effects.pop('great_cutter', None)
            self.action_bonus[key] = bonus
        if self.effect(key, 'tempest') and first:
            buffs = getattr(self.s.status, "active_buffs", {}).get(other, [])
            positive = next((b for b in buffs if b.get('amount', 0) > 0), None)
            if positive:
                buffs.remove(positive)
                bonus += 0.25
            st.effects.pop('tempest', None)
            self.action_bonus[key] = bonus
        if self.effect(key, 'vulcan') and (getattr(self.s, "moves", {}) or {}).get(other) == 'defense' and first:
            if hasattr(self.s, "stability_manager"):
                logs.extend(self.s.stability_manager._apply(other, -5))
        return math.floor(damage * (1 + bonus) + 1e-9) if bonus else damage

    def preprocess(self, key: str, other: str, stats: dict) -> dict:
        if (getattr(self.s, "moves", {}) or {}).get(key) not in ('attack', 'special'):
            return stats
        if self.effect(key, 'tempest'):
            buffs = getattr(self.s.status, "active_buffs", {}).get(other, [])
            positive = next((b for b in buffs if b.get('amount', 0) > 0), None)
            if positive:
                buffs.remove(positive)
                self.action_bonus[key] = self.action_bonus.get(key, 0) + 0.25
                if positive.get('stat') == 'defense':
                    stats = dict(stats)
                    stats['defense'] = max(0.0, stats.get('defense', 0) - positive['amount'])
            self._state_for(key).effects.pop('tempest', None)
        pierce = self.pierce(key)
        if pierce:
            stats = dict(stats)
            stats['defense'] = max(0.0, stats.get('defense', 0) * (1.0 - pierce))
        return stats

    def shield(self, key: str, other: str, damage: int, logs: list[str]) -> Tuple[int, int]:
        bypass = 0
        if self.effect(key, 'vulcan'):
            total = self.s.status.get_shield(other) if hasattr(self.s, "status") else 0
            total += (self.effect(other, 'shield') or {}).get('hp', 0)
            bypass = min(damage, math.floor(total * 0.5))
        effect = self.effect(other, 'shield')
        remaining = max(0, damage - bypass)
        if effect:
            absorbed = min(remaining, effect.get('hp', 0))
            effect['hp'] -= absorbed
            remaining -= absorbed
            if absorbed:
                logs.append(f"🛡️ **Draciel Shield** absorbs {absorbed}!")
        return remaining, bypass

    def mitigate(self, key: str, other: str, move: str, damage: int, logs: list[str]) -> int:
        if move not in ('attack', 'special') or damage <= 0:
            return damage
        if self.effect(other, 'footwork') and not self.effect(other, 'footwork').get('used'):
            damage = math.floor(damage * 0.65)
            self.effect(other, 'footwork')['used'] = True
        bb_mit = self.effect(other, 'bb_mitigate')
        if bb_mit:
            pct = bb_mit.get('pct', 0)
            reduced = math.floor(damage * (1 - pct))
            absorbed = damage - reduced
            if absorbed > 0:
                logs.append(f"🛡️ **Bit Beast Armor** absorbs {absorbed} damage ({pct:.0%})!")
                damage = reduced
                if bb_mit.get('reflect'):
                    ref_dmg = max(1, math.floor(absorbed * bb_mit['reflect']))
                    self.s.hp[key] = max(0, self.s.hp.get(key, 0) - ref_dmg)
                    logs.append(f"↩️ **Bastion Reflect** strikes back for {ref_dmg} damage!")
        tac = self.effect(other, 'tactical_mitigate')
        if tac:
            pct = tac.get('pct', 0)
            reduced = math.floor(damage * (1 - pct))
            if reduced < damage:
                logs.append(f"🛡️ **Tactical Guard** absorbs {damage - reduced} damage!")
                damage = reduced
        return damage

    def stability_delta(self, key: str, old: int, delta: int, logs: list[str], *, incoming: bool = True) -> int:
        if delta >= 0:
            return delta
        if incoming and self.effect(key, 'endurance'):
            converted = abs(delta) * 0.5
            delta = -math.ceil(abs(delta) - converted)
            self.s.hp[key] = max(0, self.s.hp.get(key, 0) - math.floor(converted * 3))
        if old + delta <= 0 and self.effect(key, 'unbreakable'):
            self._state_for(key).effects.pop('unbreakable', None)
            logs.append('🛡️ **Unbreakable Fortress** prevents a stability finish; +8 stability!')
            sm = getattr(self.s, "stability_manager", None)
            max_stab = sm.max[key] if sm else 100
            return max(0, min(max_stab, 9)) - old
        return delta

    def committed(self, key: str, other: str, move: str, actual: int, logs: list[str]) -> None:
        if actual <= 0 or move not in ('attack', 'special'):
            return
        self.incoming[other] = self.incoming.get(other, 0) + actual
        if self.split_success.pop(key, False) and self.s.hp.get(key, 0) > 0:
            self.stamina(key, 0.4)
            logs.append('🐯 **Tiger Claw** — all 3 strikes connect; +0.4 stamina!')
        if self.effect(key, 'saber'):
            self.states[key].effects.pop('saber', None)
            self.debuffs[(other, 'scorch')] = self.round + 2
            self.debuffs[(other, 'scorch_start')] = self.round + 1
        if self.effect(key, 'scratch'):
            self.states[key].effects.pop('scratch', None)
            self.debuffs[(other, 'wound')] = self.round + 1
        if self.effect(key, 'monkey'):
            self.states[key].effects.pop('monkey', None)
            self.debuffs[(other, 'energy_denial')] = self.round + 1
        if move == 'attack' and self.action_cutter.pop(key, False):
            if hasattr(self.s, "stability_manager"):
                logs.extend(self.s.stability_manager._apply(other, -4))
        if self.effect(key, 'vulcan'):
            self.states[key].effects.pop('vulcan', None)
        if self.effect(other, 'mark'):
            self.states[other].effects.pop('mark', None)
            self.retaliation.append((other, key, 0.15 * self.attack(other), 'Tiger Claw'))
        if self.effect(other, 'lightning') and getattr(self.s, "moves", {}).get(other) == 'defense':
            self.retaliation.append((other, key, min(40, 0.30 * actual), 'Dark Lightning'))

    def attack(self, key: str) -> float:
        from .purification import effective_stats
        return effective_stats(self.s, key)['attack']

    def rider(self, key: str, other: str, raw: float, label: str, logs: list[str], critical: bool = False) -> None:
        """Normal DEF/type/status mitigation, with no offensive proc dispatch."""
        from .purification import effective_stats
        from .combat_rules import damage_hp
        from . import avatar_combat
        defense = effective_stats(self.s, other)['defense']
        damage = raw * 100 / max(1, defense)
        if hasattr(self.s, "attack_manager"):
            damage, _ = self.s.attack_manager._apply_passive_reduction(other, self.s.blades[other], damage, [])
        if hasattr(self.s, "status") and self.s.status.is_invulnerable(other):
            damage = 0
        tactical = getattr(getattr(self.s, 'ability', None), 'tactical', None)
        if tactical:
            damage = tactical.engine.burst.incoming(other, 'special', damage, logs)
        absorb = getattr(self.s, 'rider_absorb', None)
        if absorb:
            damage = absorb(other, damage)
        if hasattr(self.s, "status"):
            absorbed = self.s.status.absorb_shield(other, math.floor(damage))
            damage -= absorbed
            if absorbed and tactical:
                tactical.shield_absorbed(key, other, absorbed, damage, logs)
        effect = self.effect(other, 'shield')
        if effect:
            absorbed = min(damage, effect.get('hp', 0))
            effect['hp'] -= absorbed
            damage -= absorbed
        tactical = getattr(getattr(self.s, 'ability', None), 'tactical', None)
        if tactical:
            damage = tactical.mitigate(other, key, 'special', damage, logs, include_burst=False)
        if hasattr(self.s, "type_gimmicks"):
            damage = self.s.type_gimmicks.mitigate(key, other, 'special', damage)
        av = getattr(self.s, "avatar_bonuses", {}).get(other)
        if av:
            damage = av.apply_damage_resistance(damage)
        damage, lethal_logs = avatar_combat.guard_lethal(self.s, other, math.floor(damage))
        logs.extend(lethal_logs)
        phoenix = getattr(getattr(self.s, 'ability', None), 'dranzer', None)
        if phoenix:
            actual = phoenix.terminal(other, damage, logs)
        else:
            self.s.hp[other], actual = damage_hp(self.s.hp[other], damage)
        burst = getattr(getattr(self.s, 'ability', None), 'burst', None)
        if actual > 0 and burst is not None:
            burst.committed(other, logs)
        logs.append(f'🌪️ **{label}** — {actual} damage' + (' (CRITICAL ×2)!' if critical else '!'))

    def end_round(self, logs: list[str]) -> None:
        if self.completed_round == self.round:
            return
        self.completed_round = self.round

        tornadoes = []
        for key in list(self.states.keys()):
            tornado = self.effect(key, 'tornado')
            if tornado and self.s.hp.get(key, 0) > 0:
                crit = self.rng() < 0.44
                tornadoes.append((key, tornado.get('enemy', self.enemy(key)), self.attack(key) * 0.32 * (2 if crit else 1), crit))
        for key, other, raw, crit in tornadoes:
            self.rider(key, other, raw, 'Galaxy Turbo Twister', logs, crit)
        for key, other, raw, label in self.retaliation:
            self.rider(key, other, raw, label, logs)
        self.retaliation.clear()

        for key, st in self.states.items():
            if self.effect(key, 'comeback'):
                max_hp = getattr(self.s, "max_hp_per_player", {}).get(key, getattr(self.s, "max_hp", 100))
                self.heal(key, min(self.incoming.get(key, 0) * 0.25, max_hp * 0.08), logs, "Champion's Spirit")
            fortress = self.effect(key, 'fortress')
            if fortress:
                fortress['stored'] += self.guard_saved.get(key, 0) * 0.30
                if fortress.get('until') == self.round:
                    max_hp = getattr(self.s, "max_hp_per_player", {}).get(key, getattr(self.s, "max_hp", 100))
                    self.heal(key, min(fortress['stored'], max_hp * 0.12), logs, 'Fortress Defense')

            moves = getattr(self.s, "moves", {}) or {}
            st.previous = moves.get(key)
            st.previous_move = moves.get(key)
            st.blader_active_skill = None
            st.bit_beast_active_skill = None
            st.blader_energy_deducted = False

            # Expire finished effects
            for name, effect in list(st.effects.items()):
                if effect.get('until', 0) <= self.round:
                    st.effects.pop(name, None)

        for key in list(getattr(self.s, "blades", {}).keys()):
            if (self.debuffs.get((key, 'scorch_start'), 999999) <= self.round <= self.debuffs.get((key, 'scorch'), 0)
                    and (getattr(self.s, "moves", {}) or {}).get(key) in ('attack', 'special')):
                self.s.hp[key] = max(0, self.s.hp[key] - 8)
                logs.append('🔥 **Scorch** — 8 HP lost after attacking!')

        self.s.og_previous_moves = dict(getattr(self.s, "moves", {}) or {})


def get_battle_skills(session) -> Optional[BattleSkillManager]:
    return getattr(session, "skills_manager", None) or getattr(session, "original_generation", None)
