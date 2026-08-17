"""
lionheart.py  —  🦁 LIONHEART SOVEREIGN, the Unmoved

The fourth boss with a full kit, and the first DEFENCE-type one. Drakos,
NEMESIS and Argus all answer pressure with more pressure; Lionheart answers it
by not moving, and then by charging you with everything it absorbed.

The problem a defence boss has to solve
---------------------------------------
185 Defence with an ordinary damage formula is not a fight, it is a wait. The
boss soaks, the player chips, and twenty minutes later somebody wins on a
timer. Every boss in this file that felt like filler felt like it for that
reason — a stat block is not a mechanic.

So Lionheart's defence is its OFFENCE. `stat_multiplier` does not touch attack
at all; `bulwark_damage()` adds a share of the DEFENCE stat to every swing, and
that share grows with the Bulwark. The 185 is the threat, not the wall, and a
player who lets it stand there is not stalling the fight — they are loading it.

The kit
-------
    Mane of Iron        A Bulwark plate every round it is not broken, to 5.
                        Each plate is damage reduction, reflect, and — the part
                        that matters — more of its Defence stat added to its
                        attacks. Two plates come off any hit worth 3% of its
                        health, so pressure is still the answer; it just has to
                        be sustained, not saved up.

    Pride's Reprisal    The counter. Everything Lionheart soaks is banked, and
                        the bank is paid out on its next Special. Sitting on a
                        turtle and poking is the losing line against it.

    HEART OF THE PRIDE  Ultimate, once per battle, below 55%. It plants,
                        restores a fifth of its health, and for four rounds
                        every plate counts double. Not immunity — Argus already
                        owns that, and two bosses with the same wall is one
                        boss written twice.

Why the bank has a ceiling
--------------------------
`REPRISAL_CAP` exists because a long fight against a soak boss otherwise
produces one unanswerable Special at round 30. Drakos's DEBT_CAP is there for
the same reason and for the same measured reason: without it the fight is fine
for 25 rounds and then decided by a number nobody could see accumulating.
"""

from dataclasses import dataclass, field
from typing import Optional

from .boss_abilities import BaseBossState

# ── Ability 1: Mane of Iron ───────────────────────────────────────────────────
BULWARK_MAX            = 5
BULWARK_GAIN_PER_ROUND = 1
# Per plate. Kept small individually because five of them stack, and because
# reduction compounds against a player whose damage is already being divided by
# a 185 Defence — see `damage_reduction()`, which is capped for that reason.
BULWARK_REDUCE_PCT     = 0.06          # 30% off at full plate
BULWARK_REFLECT        = 9.0           # flat, per plate, on a hit that lands
# The part that makes it a fight. Each plate converts this much of the DEFENCE
# stat into damage on every swing — at full plate, 25% of 185.
BULWARK_DEF_TO_ATK     = 0.05
# A hit worth this much of max HP strips plates. Slightly above Argus's
# EYE_BREAK_DAMAGE: Lionheart has half again its health and the plates are
# worth more, so the bar to shift it is higher.
PLATE_BREAK_DAMAGE     = 0.030
PLATES_LOST_PER_BREAK  = 2

# ── Ability 2: Pride's Reprisal ───────────────────────────────────────────────
REPRISAL_RATIO         = 0.22          # of damage soaked, banked for the Special
REPRISAL_CAP           = 520.0         # so round 30 cannot produce a one-shot

# ── Ultimate: Heart of the Pride ──────────────────────────────────────────────
ROAR_HP_GATE           = 0.55
ROAR_HEAL_PCT          = 0.20
ROAR_TURNS             = 4             # rounds during which plates count double
ROAR_PLATE_MULT        = 2.0


@dataclass
class LionheartState(BaseBossState):
    """Lionheart-specific state.

    `copy()` is a real deep copy for the same reason every other boss state is:
    the AI clones fighters to search two plies ahead, and a shared state would
    let the lookahead plate up, spend the bank and burn the once-per-fight
    Ultimate on moves that are never actually played.
    """

    plates:        int   = 0
    bank:          float = 0.0
    # Set by boss_battle._make_state from the LEVELLED defence, because that is
    # the only place both the state and the stat line exist together. A state
    # cannot read its own fighter — `attack_bonus()` is called on the state, not
    # on the Fighter — so the number has to be handed to it once at construction
    # rather than looked up per swing. Left at 0.0 the conversion simply does
    # nothing, which is the right way for this to fail.
    base_defense:  float = 0.0
    roar_turns:    int   = 0
    roar_used:     bool  = False
    overdriven:    bool  = False
    ultimate_used: bool  = False
    freeze_turns:  int   = 0
    _log:          list  = field(default_factory=list)

    # ── the mane ─────────────────────────────────────────────────────────────
    def add_plate(self) -> None:
        self.plates = min(BULWARK_MAX, self.plates + BULWARK_GAIN_PER_ROUND)

    def strip_plates(self, n: int) -> int:
        """Knock plates off. Returns how many actually came off."""
        before = self.plates
        self.plates = max(0, self.plates - int(n))
        return before - self.plates

    def plate_value(self) -> float:
        """Effective plate count. The Roar makes each one count twice."""
        mult = ROAR_PLATE_MULT if self.roar_turns > 0 else 1.0
        return self.plates * mult

    # ── BaseBossState hooks ──────────────────────────────────────────────────
    def attack_bonus(self) -> float:
        """Defence, spent as offence. The whole point of the boss.

        Flat rather than through `stat_multiplier`, which multiplies attack AND
        defence together — using it here would raise the wall every time it
        raised the charge, which is the stalemate this boss exists not to be.
        """
        return self.bulwark_damage(self.base_defense)

    def defense_bonus(self) -> float:
        # Flat, not a multiplier: 185 through `stat_multiplier` would scale the
        # whole 185 and put the boss out of reach of any real player damage.
        return self.plate_value() * 6.0

    def damage_reduction(self) -> float:
        """Fraction off every incoming hit. Capped, deliberately.

        Reduction compounds with the mitigation the 185 Defence already applies
        in `resolve`. Uncapped at five doubled plates that is 60% off a number
        that was already halved, which is the stalemate this boss was written
        to avoid.
        """
        return min(0.35, self.plate_value() * BULWARK_REDUCE_PCT)

    def reflect(self) -> float:
        return self.plate_value() * BULWARK_REFLECT

    def bulwark_damage(self, defense_stat: float) -> float:
        """How much of a defence stat the current plates convert to damage."""
        return max(0.0, float(defense_stat)) * self.plate_value() * BULWARK_DEF_TO_ATK

    def absorb(self, damage: float) -> tuple[float, bool]:
        """Plates take their cut, and what they take is banked.

        Routed through `absorb` rather than through a damage-reduction hook of
        its own because `absorb` is the one function EVERY damage path already
        calls — boss_ai for both fighters, and boss_battle again for the
        counter-hit during a boss Special. A reduction checked anywhere else
        would leave one of those routes at full damage, which is how Argus's
        immunity nearly shipped with a hole in it.
        """
        if damage <= 0:
            return damage, False
        cut = damage * self.damage_reduction()
        self.bank = min(REPRISAL_CAP, self.bank + cut * REPRISAL_RATIO)
        return damage - cut, False

    def break_stars(self, damage: float, max_hp: float) -> int:
        """A committed blow strips plates.

        Named for boss_ai's hook, not for the mechanic: `break_stars` is the
        only place in the engine that receives the damage AND the victim's max
        HP together, which a "worth 3% of its health" threshold needs.
        `bank_debt` gets the damage alone — Argus's eye-break was written
        against it, could not compute its own threshold, and shipped doing
        nothing at all for two versions.
        """
        if max_hp <= 0 or damage < max_hp * PLATE_BREAK_DAMAGE:
            return 0
        return self.strip_plates(PLATES_LOST_PER_BREAK)

    def spend_bank(self) -> float:
        out, self.bank = self.bank, 0.0
        return out

    def should_ascend(self, hp_fraction: float) -> bool:
        return (not self.overdriven) and hp_fraction <= ROAR_HP_GATE

    def ascend(self) -> None:
        self.overdriven = True

    def tick(self) -> None:
        if self.freeze_turns > 0:
            self.freeze_turns -= 1
        if self.roar_turns > 0:
            self.roar_turns -= 1
        self.add_plate()

    def copy(self) -> "LionheartState":
        return LionheartState(
            plates=self.plates, bank=self.bank, base_defense=self.base_defense,
            roar_turns=self.roar_turns,
            roar_used=self.roar_used, overdriven=self.overdriven,
            ultimate_used=self.ultimate_used, freeze_turns=self.freeze_turns,
            _log=list(self._log),
        )


# ── Specials ──────────────────────────────────────────────────────────────────

SPECIALS = {
    "sovereign": {
        "name":     "Sovereign Guard",
        "emoji":    "🛡️",
        "mult":     1.15,
        "hits":     2,
        "ultimate": False,
        "plate_amp": 14.0,        # extra damage per effective plate
        "spend_bank": True,
        "text":     "It does not step back. It steps THROUGH, and the guard "
                    "comes with it.",
    },
    "rend": {
        "name":     "Rampant Rend",
        "emoji":    "🦁",
        "mult":     1.40,
        "hits":     3,
        "ultimate": False,
        "pierce":   0.35,
        "spend_bank": True,
        "text":     "The mane opens and what was behind it is not defensive at "
                    "all.",
    },
    "roar": {
        "name":     "HEART OF THE PRIDE",
        "emoji":    "👑",
        "mult":     1.30,
        "hits":     1,
        "ultimate": True,
        "grant_roar": ROAR_TURNS,
        "heal_pct": ROAR_HEAL_PCT,
        "true_damage": True,
        "spend_bank": True,
        "text":     "Every plate it is still wearing answers at once, and the "
                    "stadium goes quiet under it.",
    },
}


def available_specials(state: LionheartState, gauge_ready: bool) -> list[str]:
    if not gauge_ready:
        return []
    out = []
    for key, spec in SPECIALS.items():
        if spec["ultimate"] and (not state.overdriven or state.ultimate_used):
            continue
        out.append(key)
    return out


def pick_special(state: LionheartState, gauge_ready: bool,
                 foe_hp_fraction: float, self_hp_fraction: float = 1.0
                 ) -> Optional[str]:
    """Every branch has a condition it genuinely wins.

    An option only reachable "if nothing else applied" fires zero times in
    practice — the lesson NEMESIS taught, Drakos was written around, and Argus
    inherited.
    """
    options = available_specials(state, gauge_ready)
    if not options:
        return None

    # The Roar is the answer to being hurt, so it goes off when Lionheart is
    # actually in trouble rather than being held for a perfect moment.
    if "roar" in options and self_hp_fraction <= ROAR_HP_GATE:
        return "roar"
    # Fully plated, Sovereign Guard is the bigger number — plate_amp beats the
    # raw multiplier once there are four or more plates on.
    if "sovereign" in options and state.plates >= BULWARK_MAX - 1:
        return "sovereign"
    # Stripped bare: the Rend does not care about plates and pierces instead.
    if "rend" in options:
        return "rend"
    return options[0]


def special_damage(spec_key: str, boss_attack: float, state: LionheartState,
                   foe_defense: float, foe_hp_fraction: float,
                   dmg_scale: float) -> tuple[float, dict]:
    """Damage and side effects for one Lionheart Special.

    Returns (damage, effects) — a DICT, the shape boss_battle._fire_special
    actually reads. Argus returned a list here and every one of its Specials
    raised `AttributeError: 'list' object has no attribute 'get'`, which froze
    the fight mid-battle. Keys the engine understands:

        drain  → heal Lionheart for this much
        strip  → wipe the player's gauge
        freeze → lock the player's gauge for this many rounds
        lines  → human-readable notes, ignored by the engine
    """
    spec = SPECIALS[spec_key]
    lines: list[str] = []
    effects: dict = {"lines": lines}

    raw = boss_attack * dmg_scale * float(spec["mult"])

    amp = float(spec.get("plate_amp", 0.0))
    if amp and state.plate_value():
        bonus = amp * state.plate_value()
        raw += bonus
        lines.append(f"🛡️ {state.plate_value():.0f} plate(s) drive the guard "
                     f"forward (+{bonus:.0f}).")

    if spec.get("spend_bank"):
        banked = state.spend_bank()
        if banked > 0:
            raw += banked
            lines.append(f"🦁 Everything it soaked comes back at once "
                         f"(+{banked:.0f}).")

    pierce = float(spec.get("pierce", 0.0))
    eff_def = foe_defense * (1.0 - pierce)
    mitig = max(0.4, 1.0 - eff_def / 400.0)
    dmg = max(1.0, raw * mitig)

    if spec.get("grant_roar"):
        turns = int(spec["grant_roar"])
        # Set to exactly `turns`, not turns+1: `plate_value()` is read before
        # this round's `tick()` spends a charge, so the granting round is
        # already doubled. Argus's Aegis shipped with the +1 and gave six
        # rounds of a five-round effect; which side of the tick an effect is
        # read on is worth one measurement rather than one assumption.
        state.roar_turns = max(state.roar_turns, turns)
        state.roar_used = True
        state.ultimate_used = True
        lines.append(f"👑 Every plate counts double for **{turns}** rounds.")

    heal_pct = float(spec.get("heal_pct", 0.0))
    if heal_pct:
        # Paid through `drain`, the key _fire_special already heals from. A
        # second healing route would be a second thing to keep in step.
        effects["drain"] = heal_pct * float(LIONHEART["hp"])
        lines.append(f"❤️ It takes back {effects['drain']:.0f} health.")

    return dmg, effects


# ── Profile ───────────────────────────────────────────────────────────────────

LIONHEART = {
    "key": "lionheart",
    "name": "LIONHEART SOVEREIGN org",
    "title": "The Unmoved",
    "emoji": "🦁",
    "tier": "Ultimate",
    "type": "Defense",
    "rarity": "Exclusive",
    "event_limited": True,
    "spin": "Right",
    "image_url": ("https://cdn.discordapp.com/attachments/"
                  "1510856884943454208/1537309843801509978/Firefly_1.png"
                  "?ex=6a83301c&is=6a81de9c&hm=c679629c2d9e6f56020430204e36f6"
                  "3791890e6d39bc550f1b361cbc7dcc3c17&"),
    "card_theme": {"accent": "#f4a300", "glow": "#7a3d00", "tint": "#1a1004"},
    "difficulty": "elite",
    "persona": ("an old sovereign that speaks rarely, never raises its voice, "
                "and treats the fight as something happening in its house"),
    "hp": 3600,
    "attack": 118,
    "defense": 185,
    "stamina": 127,
    "colour": 0xF4A300,
    "reward": {"coins": 26000, "casino": 9000},
    "copy_total_range": [360, 452],
    "blurb": "Plates up every round. Its guard is where its damage comes from.",
    "description": (
        "Gold over dark iron, and a mane of overlapping plates that closes one "
        "notch tighter every round you leave it alone. Lionheart Sovereign has "
        "the highest Defence of any boss and does not use it to survive — it "
        "uses it to hit you. Every plate turns part of that 185 into damage, "
        "banks a share of whatever it soaks, and pays the whole bank out on "
        "its next Special. Hit it hard enough to strip plates, or watch the "
        "wall become the charge."
    ),
    "abilities": [
        {
            "name": "Mane of Iron",
            "emoji": "🛡️",
            "desc": ("Gains a plate every round, to 5. Each plate is **6% "
                     "damage reduction** (capped at 35%), **9 reflect**, and "
                     "**5% of its Defence added to every swing** — 25% of 185 "
                     "at full plate. A hit worth 3% of its health strips 2."),
        },
        {
            "name": "Pride's Reprisal",
            "emoji": "🦁",
            "desc": ("Banks **22% of everything its plates soak**, up to 520, "
                     "and adds the whole bank to its next Special. Poking a "
                     "plated Lionheart is feeding it."),
        },
        {
            "name": "Heart of the Pride",
            "emoji": "👑",
            "desc": ("Once per battle, below 55% health: heals **20%** and "
                     "every plate counts **double for 4 rounds** — reduction, "
                     "reflect and the Defence-to-damage conversion all at "
                     "once. Not immunity; something worse to stand in front "
                     "of."),
        },
    ],
}
