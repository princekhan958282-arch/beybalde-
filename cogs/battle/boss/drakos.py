"""
drakos.py  —  🐉 Aetherion Drakos  (Ultimate tier boss)

Deliberately the mirror of NEMESIS ÆTHERION.

NEMESIS punishes aggression: every hit you land is banked as Debt and comes
back at you, so the correct play is restraint. If Drakos worked the same way it
would just be a reskin, so it inverts the pressure — Drakos punishes PASSIVITY.
It gains a Star every round, its damage climbs with each one, and the only way
to knock Stars off is to hit it hard. Stall against Drakos and it snowballs
past you.

That gives the two bosses genuinely opposite counterplay:

    NEMESIS  →  don't over-commit, pick your moments
    DRAKOS   →  keep the pressure on, never let it breathe

Crystalline Aegis adds the second layer of that lesson: shard layers each eat a
slice of ONE incoming hit and then shatter, so chip damage is wasted on it and
committed hits are rewarded. Same message, different mechanism.

Like NEMESIS, this lives entirely outside data/beyblades.json — the ability
engine never sees it, and there is nothing to exclude from ;list, spawns, the
shop, the marketplace or the spawn-quiz decoys.
"""

import random
from dataclasses import dataclass, field
from typing import Optional

from .boss_abilities import BaseBossState

# ── Ability 1: Astral Ascendance ──────────────────────────────────────────────
STAR_MAX             = 6
STAR_ATTACK_PER      = 4.5      # flat attack per Star
STAR_GAIN_PER_ROUND  = 1
# A hit worth this much of its max HP knocks a Star loose. The first cut used
# 0.045 (65 damage on 1450 HP), but a clean player hit is ~65 BEFORE Crystalline
# Aegis takes its 35%, so only ~42 ever landed and Stars were never knocked off
# — aggression and turtling both left the boss on 5.9 of 6 Stars and the whole
# premise of the fight did nothing. Threshold now sits below a committed hit
# and above chip damage, which is exactly the line the design wanted.
STAR_BREAK_DAMAGE    = 0.026
STARS_LOST_PER_BREAK = 2

# ── Ability 2: Crystalline Aegis ──────────────────────────────────────────────
AEGIS_LAYERS_MAX     = 3
AEGIS_ABSORB         = 0.35     # each intact layer eats 35% of one hit
AEGIS_REGEN_ROUNDS   = 4        # a layer grows back this often

# ── Ultimate gate ─────────────────────────────────────────────────────────────
OVERDRIVE_HP_GATE    = 0.50
OVERDRIVE_TURN_GATE  = 12       # HP-or-turn, same lesson NEMESIS taught us


@dataclass
class DrakosState(BaseBossState):
    """Drakos-specific state. copy() must be a real deep copy — the AI clones
    fighters to search ahead, and a shared state would let the lookahead spend
    the live Stars, shatter live shards and burn the live Ultimate."""

    stars:          int   = 0
    aegis:          int   = AEGIS_LAYERS_MAX
    round_no:       int   = 0
    overdriven:     bool  = False
    ultimate_used:  bool  = False
    freeze_turns:   int   = 0
    last_break:     int   = 0     # rounds since a Star was knocked off

    def copy(self) -> "DrakosState":
        return DrakosState(
            stars=self.stars, aegis=self.aegis, round_no=self.round_no,
            overdriven=self.overdriven, ultimate_used=self.ultimate_used,
            freeze_turns=self.freeze_turns, last_break=self.last_break,
        )

    # ── Astral Ascendance ────────────────────────────────────────────────────
    def attack_bonus(self) -> float:
        return self.stars * STAR_ATTACK_PER

    def stat_multiplier(self) -> float:
        return 1.15 if self.overdriven else 1.0

    def gauge_multiplier(self) -> float:
        # More Stars, faster it charges — the snowball has to be visible.
        return 1.0 + 0.08 * self.stars

    def gain_star(self) -> None:
        self.stars = min(STAR_MAX, self.stars + STAR_GAIN_PER_ROUND)

    def break_stars(self, damage: float, max_hp: float) -> int:
        """A big enough hit shakes Stars loose. Returns how many were lost."""
        if max_hp <= 0 or damage < max_hp * STAR_BREAK_DAMAGE:
            return 0
        lost = min(self.stars, STARS_LOST_PER_BREAK)
        self.stars -= lost
        if lost:
            self.last_break = 0
        return lost

    # ── Crystalline Aegis ────────────────────────────────────────────────────
    def absorb(self, damage: float) -> tuple[float, bool]:
        """Run an incoming hit through the shards. Returns (damage, shattered)."""
        if self.aegis <= 0:
            return damage, False
        self.aegis -= 1
        return damage * (1.0 - AEGIS_ABSORB), True

    def regen_aegis(self) -> bool:
        if (self.aegis < AEGIS_LAYERS_MAX
                and self.round_no > 0
                and self.round_no % AEGIS_REGEN_ROUNDS == 0):
            self.aegis += 1
            return True
        return False

    # ── Overdrive ────────────────────────────────────────────────────────────
    def should_ascend(self, hp_fraction: float) -> bool:
        if self.overdriven:
            return False
        return hp_fraction <= OVERDRIVE_HP_GATE or self.round_no >= OVERDRIVE_TURN_GATE

    def ascend(self) -> None:
        self.overdriven = True

    # ── Turn bookkeeping ─────────────────────────────────────────────────────
    def flip_stance(self) -> None:
        """Called once per exchange by resolve(). Drakos has no stances, so it
        uses the hook for its own per-round upkeep."""
        self.round_no += 1
        self.last_break += 1
        self.gain_star()
        self.regen_aegis()


# ── Specials & the single Ultimate ────────────────────────────────────────────
SPECIALS = {
    "starfall": {
        "name":     "Starfall Lance",
        "emoji":    "💫",
        "mult":     2.20,
        "hits":     3,
        "ultimate": False,
        "star_amp": 11.0,        # extra damage per Star held
        "pierce":   0.45,
        "text":     "The constellation fixes on you. Three lances fall.",
    },
    "bulwark": {
        "name":     "Emerald Bulwark",
        "emoji":    "💚",
        "mult":     1.30,
        "hits":     1,
        "ultimate": False,
        "restore":  2,           # shard layers rebuilt
        "drain":    0.35,
        "text":     "The emeralds flare. Broken crystal knits itself whole.",
    },
    "roar": {
        "name":     "Drakos Roar",
        "emoji":    "🐉",
        "mult":     1.80,
        "hits":     5,
        "ultimate": False,
        "consume_stars": 17.0,   # spends the whole sky for damage
        "text":     "The golden dragon opens its jaws and the sky answers.",
    },
    "overdrive": {
        "name":     "AETHERION OVERDRIVE",
        "emoji":    "🌌",
        "mult":     3.10,
        "hits":     7,
        "ultimate": True,
        "consume_stars": 26.0,
        "consume_aegis": 30.0,   # burns its own armour for power
        "true_damage": True,
        "text":     "Every shard, every star, spent at once. Nothing is held back.",
    },
}


def available_specials(state: DrakosState, gauge_ready: bool) -> list[str]:
    if not gauge_ready:
        return []
    out = []
    for key, spec in SPECIALS.items():
        if spec["ultimate"] and (not state.overdriven or state.ultimate_used):
            continue
        out.append(key)
    return out


def pick_special(state: DrakosState, gauge_ready: bool,
                 foe_hp_fraction: float, self_hp_fraction: float = 1.0
                 ) -> Optional[str]:
    """Role-ordered, learning from NEMESIS: an option that is only reachable
    'if nothing else applied' ends up firing zero times, so every branch here
    has a condition it genuinely wins."""
    options = available_specials(state, gauge_ready)
    if not options:
        return None

    # The Ultimate is a finisher — spend it when the sky is full or the fight
    # is nearly over.
    if "overdrive" in options and (state.stars >= 4 or foe_hp_fraction <= 0.40):
        return "overdrive"

    # A full sky is worth cashing in.
    if "roar" in options and state.stars >= 4:
        return "roar"

    # Rebuild when the armour is actually gone and it's hurting.
    if "bulwark" in options and (state.aegis == 0 or self_hp_fraction <= 0.55):
        return "bulwark"

    if "starfall" in options:
        return "starfall"
    return options[0]


def special_damage(spec_key: str, boss_attack: float, state: DrakosState,
                   foe_defense: float, foe_hp_fraction: float,
                   dmg_scale: float) -> tuple[float, dict]:
    """Damage plus side effects: drain / restore / star spend."""
    spec = SPECIALS[spec_key]
    base = boss_attack * dmg_scale * spec["mult"]

    effects: dict = {"stars_spent": 0}

    if spec.get("star_amp"):
        base += state.stars * spec["star_amp"]
    if spec.get("consume_stars"):
        effects["stars_spent"] = state.stars
        base += state.stars * spec["consume_stars"]
        state.stars = 0
    if spec.get("consume_aegis"):
        base += state.aegis * spec["consume_aegis"]
        effects["aegis_spent"] = state.aegis
        state.aegis = 0

    if spec.get("true_damage"):
        mitigation = 1.0
    else:
        eff_def = foe_defense * (1.0 - spec.get("pierce", 0.0))
        mitigation = max(0.40, 1.0 - eff_def / 400.0)

    damage = max(1.0, base * mitigation)

    if spec.get("drain"):
        effects["drain"] = damage * spec["drain"]
    if spec.get("restore"):
        gained = min(AEGIS_LAYERS_MAX - state.aegis, spec["restore"])
        state.aegis += gained
        effects["restore"] = gained

    return damage, effects


# ── Profile ───────────────────────────────────────────────────────────────────
DRAKOS = {
    "key":        "drakos",
    "name":       "Aetherion Drakos org",
    "title":      "The Star-Crowned",
    "emoji":      "🐉",
    "tier":       "Ultimate",
    "type":       "Stamina",
    "rarity":     "Exclusive",
    "event_limited": True,
    "spin":       "Right",
    "image_url":  "",
    # Sapphire and gold with an emerald cast, matching the crystal shards.
    "card_theme": {"accent": "#4d9fff", "glow": "#0b3ea8", "tint": "#060e22"},
    "difficulty": "elite",
    "persona":    ("a proud celestial dragon, grand and unhurried, who treats the "
                   "duel as a rite rather than a fight"),
    "hp":         1250,
    "attack":     120,
    "defense":    155,
    "stamina":    112,
    "colour":     0x1A237E,
    "reward":     {"coins": 70_000, "casino": 16_000},
    "copy_total_range": (380, 479),
    "blurb":      "Gathers a star every round. Let it breathe and you lose.",
    "description": (
        "Sapphire crystal over gunmetal, a gold dragon coiled around a "
        "star-chart core. Aetherion Drakos does not counter-punch — it "
        "ascends. Every round unanswered is another star in its crown, and "
        "every star is more force behind the next blow."
    ),
    "abilities": [
        {
            "name":  "Astral Ascendance",
            "emoji": "⭐",
            "desc":  (f"Gains a Star every round, up to {STAR_MAX}. Each Star adds "
                      f"+{int(STAR_ATTACK_PER)} ATK and speeds its gauge. A hit worth "
                      f"{STAR_BREAK_DAMAGE * 100:.1f}% of its health knocks "
                      f"{STARS_LOST_PER_BREAK} Stars loose — **pressure is the only "
                      f"answer.**"),
        },
        {
            "name":  "Crystalline Aegis",
            "emoji": "🛡️",
            "desc":  (f"{AEGIS_LAYERS_MAX} crystal layers. Each absorbs "
                      f"{int(AEGIS_ABSORB * 100)}% of one incoming hit and then "
                      f"shatters, regrowing every {AEGIS_REGEN_ROUNDS} rounds. "
                      f"Chip damage is wasted on it; commit or don't bother."),
        },
    ],
}
