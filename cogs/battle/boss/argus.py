"""
argus.py  —  ⚔️ ARGUS PRIME, the Hundred-Eyed

The third boss with a full kit, alongside NEMESIS and Drakos, and the most
straightforward of the three to read: it does one thing, it does it harder
every round, and once a fight it becomes unhittable.

The kit
-------
    Hundred-Eyed Vigil   Argus watches. Every round it is not interrupted it
                         opens another eye, and each eye is +Attack and +crit.
                         At full sight the ability reaches its stated +60% on
                         both — the number the brief asked for is the CEILING,
                         reached over six rounds, not a flat opening bonus.

    Aegis of the Sleepless  The Special. 155-scale damage, and then five rounds
                            during which nothing touches it.

Why the immunity is a WINDOW, not a wall
----------------------------------------
"Immune to all damage for 5 turns" is the whole fight if it can be re-cast, so
it cannot be. `ultimate: True` plus `ultimate_used` makes it once per battle,
which is the same gate NEMESIS's Zero Hour and Drakos's Overdrive use. Five
rounds of a ~25-round fight is a fifth of the clock spent hitting a wall —
brutal, which is what was asked for, and survivable, which is what makes it a
boss instead of a stalemate.

The immunity is implemented in `absorb()` because that is the ONE place every
damage path already funnels through: boss_ai calls it for both fighters
(lines 306-311) and boss_battle calls it again for the counter-hit during a
boss Special. A flag checked anywhere else would leave one of those three
routes still landing damage.

Eyes are knocked shut by committed hits, the same lesson Drakos's Stars
learned: a threshold below a real hit and above chip damage, or the mechanic
does nothing and the fight is just a stat block.
"""

from dataclasses import dataclass, field
from typing import Optional

from .boss_abilities import BaseBossState

# ── Ability 1: Hundred-Eyed Vigil ─────────────────────────────────────────────
EYE_MAX              = 6
EYE_GAIN_PER_ROUND   = 1
# +60% attack and +60% crit at full sight, spread across the six eyes. Stated
# as the ceiling rather than the opening value on purpose: a boss that starts
# at +60% is a boss the player never sees ramp, and the ramp is the tell that
# says "stop letting it breathe".
EYE_ATTACK_PCT_MAX   = 0.60
EYE_CRIT_PCT_MAX     = 0.60
EYE_ATTACK_PER       = EYE_ATTACK_PCT_MAX / EYE_MAX     # 0.10 each
EYE_CRIT_PER         = EYE_CRIT_PCT_MAX / EYE_MAX       # 0.10 each
# A hit worth this much of max HP closes an eye. Same calibration as Drakos's
# STAR_BREAK_DAMAGE — under a committed hit, over chip damage.
EYE_BREAK_DAMAGE     = 0.028
EYES_LOST_PER_BREAK  = 2

# ── Ability 2 / Ultimate: Aegis of the Sleepless ──────────────────────────────
AEGIS_TURNS          = 5        # rounds of total immunity
AEGIS_HP_GATE        = 0.65     # armed once Argus is bloodied this far
AEGIS_TURN_GATE      = 8        # ...or once the fight has run this long


@dataclass
class ArgusState(BaseBossState):
    """Argus-specific state.

    `copy()` must be a real deep copy: the AI clones fighters to search ahead,
    and a shared state would let the lookahead open eyes, spend the Aegis and
    burn the once-per-fight Ultimate on moves that are never actually played.
    """

    eyes:          int  = 0
    aegis_turns:   int  = 0
    aegis_used:    bool = False
    overdriven:    bool = False
    ultimate_used: bool = False
    freeze_turns:  int  = 0
    _log:          list = field(default_factory=list)

    # ── the vigil ────────────────────────────────────────────────────────────
    def open_eye(self) -> None:
        self.eyes = min(EYE_MAX, self.eyes + EYE_GAIN_PER_ROUND)

    def close_eyes(self, n: int) -> int:
        """Knock eyes shut. Returns how many actually closed."""
        before = self.eyes
        self.eyes = max(0, self.eyes - int(n))
        return before - self.eyes

    def attack_pct(self) -> float:
        return self.eyes * EYE_ATTACK_PER

    def crit_pct(self) -> float:
        return self.eyes * EYE_CRIT_PER

    # ── BaseBossState hooks ──────────────────────────────────────────────────
    def stat_multiplier(self) -> float:
        # Attack scales with sight. Returned as a multiplier rather than a flat
        # bonus so the +60% is genuinely 60% of whatever Argus's attack is,
        # including any party scaling applied above it.
        return 1.0 + self.attack_pct()

    def absorb(self, damage: float) -> tuple[float, bool]:
        """The single gate every damage path already runs through.

        Returns (damage_through, something_broke). While the Aegis is up NOTHING
        gets through — not attacks, not Specials, not the counter-hit during
        Argus's own Special.
        """
        if self.aegis_turns > 0:
            return 0.0, False
        return damage, False

    def is_immune(self) -> bool:
        return self.aegis_turns > 0

    def bank_debt(self, damage_taken: float) -> None:
        """Called with every hit that lands. A committed blow closes eyes."""
        return None

    def should_ascend(self, hp_fraction: float) -> bool:
        return (not self.overdriven) and hp_fraction <= AEGIS_HP_GATE

    def ascend(self) -> None:
        self.overdriven = True

    def tick(self) -> None:
        if self.freeze_turns > 0:
            self.freeze_turns -= 1
        if self.aegis_turns > 0:
            self.aegis_turns -= 1
        self.open_eye()

    def copy(self) -> "ArgusState":
        return ArgusState(
            eyes=self.eyes, aegis_turns=self.aegis_turns,
            aegis_used=self.aegis_used, overdriven=self.overdriven,
            ultimate_used=self.ultimate_used, freeze_turns=self.freeze_turns,
            _log=list(self._log),
        )


# ── Specials ──────────────────────────────────────────────────────────────────

SPECIALS = {
    "spearfall": {
        "name":     "Hundred-Eyed Spearfall",
        "emoji":    "👁️",
        "mult":     1.55,          # the 155 the brief asked for, as a scale
        "hits":     3,
        "ultimate": False,
        "eye_amp":  9.0,           # extra damage per open eye
        "pierce":   0.30,
        "text":     "Every eye fixes on the same point. Three spears follow.",
    },
    "watchfire": {
        "name":     "Watchfire Sweep",
        "emoji":    "🔥",
        "mult":     1.20,
        "hits":     4,
        "ultimate": False,
        "drain":    0.40,
        "text":     "The ring of eyes turns outward and burns the whole rim.",
    },
    "aegis": {
        "name":     "AEGIS OF THE SLEEPLESS",
        "emoji":    "🛡️",
        "mult":     1.55,
        "hits":     1,
        "ultimate": True,
        "grant_aegis": AEGIS_TURNS,
        "true_damage": True,
        "text":     ("A hundred eyes close at once — and nothing that follows "
                     "reaches what is behind them."),
    },
}


def available_specials(state: ArgusState, gauge_ready: bool) -> list[str]:
    if not gauge_ready:
        return []
    out = []
    for key, spec in SPECIALS.items():
        if spec["ultimate"] and (not state.overdriven or state.ultimate_used):
            continue
        out.append(key)
    return out


def pick_special(state: ArgusState, gauge_ready: bool,
                 foe_hp_fraction: float, self_hp_fraction: float = 1.0
                 ) -> Optional[str]:
    """Every branch has a condition it genuinely wins.

    An option only reachable "if nothing else applied" fires zero times in
    practice — the lesson NEMESIS taught and Drakos was written around.
    """
    options = available_specials(state, gauge_ready)
    if not options:
        return None

    # The Aegis is the answer to being hurt, so it goes off when Argus is
    # actually in trouble — not held for a perfect moment that never comes.
    if "aegis" in options and self_hp_fraction <= AEGIS_HP_GATE:
        return "aegis"
    # Fully sighted, the spear is the biggest number on the card.
    if "spearfall" in options and state.eyes >= EYE_MAX - 1:
        return "spearfall"
    # Blind and pressured: sweep, drain, and buy rounds to open eyes again.
    if "watchfire" in options and state.eyes <= 2:
        return "watchfire"
    if "spearfall" in options:
        return "spearfall"
    return options[0]


def special_damage(spec_key: str, boss_attack: float, state: ArgusState,
                   foe_defense: float, foe_hp_fraction: float,
                   dmg_scale: float) -> tuple[float, list[str]]:
    """Damage and side effects for one Argus Special.

    Mirrors drakos.special_damage's shape so boss_battle needs no per-boss
    branch: returns (damage, effect_log_lines).
    """
    spec = SPECIALS[spec_key]
    effects: list[str] = []

    raw = boss_attack * dmg_scale * float(spec["mult"])

    # Sight is damage. Spent on the spear, kept everywhere else.
    amp = float(spec.get("eye_amp", 0.0))
    if amp and state.eyes:
        bonus = amp * state.eyes
        raw += bonus
        effects.append(f"👁️ {state.eyes} eye(s) sharpen the strike (+{bonus:.0f}).")

    pierce = float(spec.get("pierce", 0.0))
    eff_def = foe_defense * (1.0 - pierce)
    mitig = max(0.4, 1.0 - eff_def / 400.0)
    dmg = max(1.0, raw * mitig)

    if spec.get("grant_aegis"):
        turns = int(spec["grant_aegis"])
        # Set to exactly `turns`, NOT turns+1. The granting round is already
        # immune — `absorb()` is consulted before this round's `tick()` spends
        # a charge — so 5 here is five immune rounds. This is the opposite of
        # v97's ability_amp, where the granting round was deliberately dormant
        # and did need the +1; the difference is which side of the tick the
        # effect is read on, and it is worth one measurement rather than one
        # assumption. It cost a round of immunity to find out.
        state.aegis_turns = max(state.aegis_turns, turns)
        state.aegis_used = True
        state.ultimate_used = True
        effects.append(f"🛡️ Nothing reaches Argus for **{turns}** rounds.")

    if spec.get("drain"):
        effects.append(f"🔥 The sweep drags {spec['drain']:.2f} stamina away.")

    return dmg, effects


# ── Profile ───────────────────────────────────────────────────────────────────

ARGUS = {
    "key": "argus",
    "name": "ARGUS PRIME org",
    "title": "The Hundred-Eyed",
    "emoji": "👁️",
    "tier": "Ultimate",
    "type": "Attack",
    "rarity": "Exclusive",
    "event_limited": True,
    "spin": "Right",
    "image_url": ("https://cdn.discordapp.com/attachments/"
                  "1510856884943454208/1538405554756517939/1786766896617.png"
                  "?ex=6a828f52&is=6a813dd2&hm=ae86e21af79315adff421f7c5579bd"
                  "fbf04f8c1263ac4129d081bd91ba86e0c6&"),
    "card_theme": {"accent": "#ffb300", "glow": "#a35a00", "tint": "#1b1206"},
    "difficulty": "elite",
    "persona": ("a sleepless sentinel that speaks in short, certain "
                "statements and never once sounds hurried"),
    "hp": 3200,
    "attack": 130,
    "defense": 87,
    "stamina": 109,
    "colour": 0xFFB300,
    "reward": {"coins": 80000, "casino": 18000},
    "copy_total_range": [370, 469],
    "blurb": "Opens an eye every round. Close them, or meet all six.",
    "description": (
        "Amber over black iron, a ring of lenses around a core that does not "
        "blink. Argus Prime does not open a fight at full strength — it earns "
        "it, one eye a round, until every one of them is on you and its Attack "
        "and crit have risen 60%. Hit it hard enough and the eyes shut. Leave "
        "it alone and, once a battle, it closes them all itself and becomes "
        "untouchable for five rounds."
    ),
    "abilities": [
        {
            "name": "Hundred-Eyed Vigil",
            "emoji": "👁️",
            "desc": ("Opens an Eye every round, to 6. Each Eye is +10% Attack "
                     "and +10% crit — **+60% at full sight.** A hit worth 2.8% "
                     "of its health closes 2 Eyes. Pressure is the only "
                     "answer."),
        },
        {
            "name": "Aegis of the Sleepless",
            "emoji": "🛡️",
            "desc": ("Once per battle, below 65% health: **immune to all "
                     "damage for 5 rounds.** Not reduced — immune. Spend those "
                     "rounds banking stamina and gauge, because the sixth "
                     "arrives with every Eye open."),
        },
    ],
}
