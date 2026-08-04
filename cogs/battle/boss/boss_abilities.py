"""
boss_abilities.py  —  god-tier boss mechanics

Why this file exists instead of a beyblades.json entry
------------------------------------------------------
NEMESIS ÆTHERION is deliberately NOT in data/beyblades.json.

Putting it there would mean:
  * cogs/abilities/ability_engine.py parses its abilities in every live PvP
    battle — a malformed trigger or an unsupported chain effect would throw
    inside real fights, not just boss ones
  * it would have to be excluded by hand from ;list, the spawn pool, the shop,
    boosters, the marketplace, ;beypedia AND the spawn-quiz decoys. Six
    exclusions, and a leak anywhere makes an unobtainable boss blade visible.

Keeping it in its own file means the ability engine never sees it, the live
battle path is untouched, and there is nothing to exclude because it was never
in the pool to begin with.

State model
-----------
Everything lives on BossState, which hangs off the Fighter. The boss AI clones
fighters to search ahead, so BossState MUST deep-copy — if a clone shared the
real object, the AI's lookahead would mutate the live fight (accumulate debt,
burn ultimates, flip stances) and the visible battle would drift from what the
player is actually doing. That is the single most dangerous bug in this file
and there's a test pinning it.
"""

import random
from dataclasses import dataclass, field
from typing import Optional

# ── Stances ───────────────────────────────────────────────────────────────────
WRATH     = "wrath"       # 🟣 left head — offence, pierces guard
JUDGEMENT = "judgement"   # 🔵 right head — defence, reflects

STANCE_META = {
    WRATH:     ("🟣", "Wrath",     "+25 ATK · ignores defence"),
    JUDGEMENT: ("🔵", "Judgement", "+30 DEF · reflects 20"),
}

# ── Tuning ────────────────────────────────────────────────────────────────────
WRATH_ATTACK_BONUS   = 25.0
WRATH_PIERCE         = 0.30     # fraction of the target's defence ignored
JUDGEMENT_DEF_BONUS  = 30.0
JUDGEMENT_REFLECT    = 20.0

DEBT_RATIO           = 0.15     # of damage taken, banked into the next Special
DEBT_CAP             = 400.0    # so a very long fight can't produce a one-shot

ASCEND_HP_THRESHOLD  = 0.40
# Ascension is also time-gated. HP alone never fired it in testing: the player
# died around turn 13 with NEMESIS still on ~78% health, so both Ultimates and
# the freeze were content nobody would ever see. Real boss fights gate phase
# transitions on time OR health precisely so the phase always happens.
ASCEND_TURN_GATE     = 10
ASCEND_STAT_BONUS    = 0.20     # +20% to attack/defence/stamina
ASCEND_GAUGE_MULT    = 2.0

FREEZE_TURNS         = 2        # Glacial Verdict locks the player's gauge
DRAIN_RATIO          = 0.50     # Sigil heals for half the damage it deals


class BaseBossState:
    """Neutral defaults every boss state must answer to.

    boss_ai.resolve() asks the state for stance bonuses, pierce, reflect and so
    on. Rather than teach it about each boss, every state subclasses this and
    overrides only what it actually uses — so a new boss can't break an
    existing one by omitting a method, and boss_ai needs no per-boss branches.
    """

    freeze_turns: int = 0

    def attack_bonus(self) -> float:      return 0.0
    def defense_bonus(self) -> float:     return 0.0
    def pierce(self) -> float:            return 0.0
    def reflect(self) -> float:           return 0.0
    def stat_multiplier(self) -> float:   return 1.0
    def gauge_multiplier(self) -> float:  return 1.0
    def bank_debt(self, damage_taken: float) -> None: return None
    def flip_stance(self) -> None:        return None
    def should_ascend(self, hp_fraction: float) -> bool: return False
    def ascend(self) -> None:             return None

    def tick(self) -> None:
        if self.freeze_turns > 0:
            self.freeze_turns -= 1

    def copy(self):
        raise NotImplementedError


@dataclass
class BossState(BaseBossState):
    """All NEMESIS-specific state. Deep-copied on clone (see copy())."""

    # Which crown it opens on. Gauge accrues on a fixed cadence, so a fixed
    # starting stance locked the Special to one parity — Voidfang fired 224
    # times to Glacial Verdict's 2 across 250 test fights, hiding half of
    # Twin Crowns. Randomising the opening crown fixes the skew.
    stance:         str   = field(default_factory=lambda: random.choice([WRATH, JUDGEMENT]))
    debt:           float = 0.0
    ascended:       bool  = False
    ultimates_used: set   = field(default_factory=set)
    freeze_turns:   int   = 0        # applied TO the player
    round_no:       int   = 0
    last_move_name: str   = ""
    # NEMESIS PROTOCOL is the opening statement: it fires on the FIRST fully
    # charged gauge, once, and never again. Tracked separately from
    # ultimates_used because it deliberately bypasses the Ascension gate that
    # normally locks Ultimates — without this flag it could only appear late,
    # which is the opposite of an opener.
    protocol_fired: bool  = False

    def copy(self) -> "BossState":
        # Explicit rather than dataclasses.replace: ultimates_used is a set and
        # a shallow copy would let the AI's lookahead consume the real fight's
        # ultimates.
        return BossState(
            stance=self.stance,
            debt=self.debt,
            ascended=self.ascended,
            ultimates_used=set(self.ultimates_used),
            freeze_turns=self.freeze_turns,
            round_no=self.round_no,
            last_move_name=self.last_move_name,
            protocol_fired=self.protocol_fired,
        )

    # ── Ability 1: Twin Crowns ───────────────────────────────────────────────
    def flip_stance(self) -> str:
        self.round_no += 1
        self.stance = JUDGEMENT if self.stance == WRATH else WRATH
        return self.stance

    def attack_bonus(self) -> float:
        return WRATH_ATTACK_BONUS if self.stance == WRATH else 0.0

    def defense_bonus(self) -> float:
        return JUDGEMENT_DEF_BONUS if self.stance == JUDGEMENT else 0.0

    def pierce(self) -> float:
        return WRATH_PIERCE if self.stance == WRATH else 0.0

    def reflect(self) -> float:
        return JUDGEMENT_REFLECT if self.stance == JUDGEMENT else 0.0

    # ── Ability 2: Law of Retribution ────────────────────────────────────────
    def bank_debt(self, damage_taken: float) -> None:
        if damage_taken > 0:
            self.debt = min(DEBT_CAP, self.debt + damage_taken * DEBT_RATIO)

    def spend_debt(self) -> float:
        owed, self.debt = self.debt, 0.0
        return owed

    # ── Ability 3: Ætheric Ascension ─────────────────────────────────────────
    def should_ascend(self, hp_fraction: float) -> bool:
        if self.ascended:
            return False
        return (hp_fraction <= ASCEND_HP_THRESHOLD
                or self.round_no >= ASCEND_TURN_GATE)

    def ascend(self) -> None:
        self.ascended = True

    def stat_multiplier(self) -> float:
        return 1.0 + ASCEND_STAT_BONUS if self.ascended else 1.0

    def gauge_multiplier(self) -> float:
        return ASCEND_GAUGE_MULT if self.ascended else 1.0

    # Turn bookkeeping is inherited from BaseBossState.tick().


# ── Specials & ultimates ──────────────────────────────────────────────────────
#   mult      damage multiplier applied to the boss's attack stat
#   hits      flavour only — the damage is dealt as one figure
#   needs     "wrath" / "judgement" / None (any stance)
#   ultimate  gated behind Ascension, once each per battle

SPECIALS = {
    "voidfang": {
        "name":     "Voidfang Requiem",
        "emoji":    "🟣",
        "mult":     2.35,
        "hits":     4,
        "needs":    WRATH,
        "ultimate": False,
        "pierce":   1.0,          # ignores defence entirely
        "text":     "The left crown opens. Four fangs of nothing tear the air.",
    },
    "verdict": {
        "name":     "Glacial Verdict",
        "emoji":    "🔵",
        "mult":     2.60,
        "hits":     2,
        "needs":    JUDGEMENT,
        "ultimate": False,
        "freeze":   FREEZE_TURNS,
        "text":     "Judgement is passed. Your gauge crusts over and stops.",
    },
    "sigil": {
        "name":     "Sigil of the Fallen Crown",
        "emoji":    "🟪",
        "mult":     2.10,
        "hits":     3,
        "needs":    None,
        "ultimate": False,
        "drain":    DRAIN_RATIO,
        "text":     "The centre sigil drinks. What it takes from you, it keeps.",
    },
    "eclipse": {
        "name":     "ÆTHERION: TOTAL ECLIPSE",
        "emoji":    "🌑",
        "mult":     3.20,
        "hits":     5,
        "needs":    None,
        "ultimate": True,
        "debt_amp": 2.0,          # every point of banked Debt hits twice
        "text":     "Both crowns wake at once. Everything you dealt, returned.",
    },
    "zerohour": {
        "name":     "NEMESIS PROTOCOL — ZERO HOUR",
        "emoji":    "💀",
        # 300%. Note this is a small NERF from the 3.60 it carried before —
        # "300% damage" reads as a 3.0x multiplier, and `mult` is exactly that
        # multiplier on boss_attack * DMG_SCALE.
        "mult":     3.00,
        "hits":     1,
        "needs":    None,
        "ultimate": True,
        "true_damage": True,      # ignores defence and any block
        "strip":    True,         # clears the player's gauge
        "execute_below": 0.35,    # +60% damage if the player is already low
        "text":     "Zero hour. There is no guard that matters now.",
    },
}


def available_specials(state: BossState, gauge_ready: bool) -> list[str]:
    """Which Special/Ultimate keys the boss may fire right now."""
    if not gauge_ready:
        return []
    # The opening Protocol. Available before Ascension precisely once, so the
    # fight starts with the boss's signature rather than saving it for a late
    # game most players never reach.
    if not state.protocol_fired and "zerohour" not in state.ultimates_used:
        return ["zerohour"]

    out = []
    for key, spec in SPECIALS.items():
        if spec["ultimate"]:
            if not state.ascended or key in state.ultimates_used:
                continue
        elif spec["needs"] and spec["needs"] != state.stance:
            continue
        out.append(key)
    return out


def pick_special(state: BossState, gauge_ready: bool,
                 foe_hp_fraction: float,
                 self_hp_fraction: float = 1.0) -> Optional[str]:
    """Choose which Special to fire.

    The first version listed the stance moves before Sigil and gated Eclipse on
    Debt >= 160, so across 200 test fights Sigil fired 0 times and Eclipse 0
    times — two of the five designed moves were unreachable. Ordering now goes
    by role rather than by a fixed list.
    """
    options = available_specials(state, gauge_ready)
    if not options:
        return None

    # Zero Hour closes a fight out.
    if "zerohour" in options and foe_hp_fraction <= 0.35:
        return "zerohour"

    # Eclipse cashes in the ledger. Gate it on Debt actually being worth
    # spending — the first cut used DEBT_CAP*0.4 (160), but real fights bank
    # 50-70, so it never qualified.
    if "eclipse" in options and state.debt >= 40 and foe_hp_fraction > 0.35:
        return "eclipse"

    # Sigil sustains, and it has to be checked BEFORE the stance moves: every
    # stance already has its own Special, so an "any stance" option placed last
    # is dominated in every branch and fires literally never.
    if "sigil" in options and self_hp_fraction <= 0.75:
        return "sigil"

    if "zerohour" in options:
        return "zerohour"
    if "eclipse" in options:
        return "eclipse"

    for key in ("voidfang", "verdict", "sigil"):
        if key in options:
            return key
    return options[0]


def special_damage(spec_key: str, boss_attack: float, state: BossState,
                   foe_defense: float, foe_hp_fraction: float,
                   dmg_scale: float) -> tuple[float, dict]:
    """Damage for one Special, plus the side effects it applies.

    Returns (damage, effects) where effects may contain:
        drain   → heal the boss for this much
        freeze  → freeze the player's gauge for N turns
        strip   → clear the player's gauge
    """
    spec = SPECIALS[spec_key]
    base = boss_attack * dmg_scale * spec["mult"]

    # Banked Debt is spent here — this is Law of Retribution paying out.
    owed = state.spend_debt()
    base += owed * spec.get("debt_amp", 1.0)

    if spec.get("execute_below") and foe_hp_fraction <= spec["execute_below"]:
        base *= 1.60

    if spec.get("true_damage"):
        mitigation = 1.0
    else:
        pierce = max(spec.get("pierce", 0.0), state.pierce())
        effective_def = foe_defense * (1.0 - pierce)
        mitigation = max(0.40, 1.0 - effective_def / 400.0)

    damage = max(1.0, base * mitigation)

    effects = {"debt_spent": owed}
    if spec.get("drain"):
        effects["drain"] = damage * spec["drain"]
    if spec.get("freeze"):
        effects["freeze"] = spec["freeze"]
    if spec.get("strip"):
        effects["strip"] = True

    return damage, effects


# ── Profile ───────────────────────────────────────────────────────────────────
NEMESIS = {
    "key":        "nemesis",
    "name":       "NEMESIS ÆTHERION org",
    "title":      "The First God",
    "emoji":      "👁️",
    "tier":       "God",
    "type":       "Balance",
    "rarity":     "Exclusive",
    "event_limited": True,
    "spin":       "Dual",
    # Paste a Discord CDN link here (upload the art, right-click → Copy Link)
    # and it shows on the info card automatically.
    "image_url":  "",
    # Violet crown + gold, straight off the art. Nothing else in the game
    # uses this palette, so the card is unmistakable.
    "card_theme": {"accent": "#c77dff", "glow": "#6a0dad", "tint": "#150920"},
    "difficulty": "legend",
    "persona":    ("an ancient divine arbiter who speaks in verdicts, not threats; "
                   "utterly certain, never raises its voice"),
    # SOLO hp; a party scales it via boss_battle.PARTY_HP_MULT.
    "hp":         3000,
    "attack":     146,
    "defense":    165,
    "stamina":    130,
    "colour":     0x7B1FA2,
    "reward":     {"coins": 250_000, "casino": 60_000},
    # A copy rolls a card TOTAL (hp+atk+def+sta) somewhere in this band.
    "copy_total_range": (400, 533),
    "blurb":      "Two crowns. One verdict. The first god-tier blade.",
    "description": (
        "Twin crowns fused around a single sigil — one half wrath, one half "
        "judgement. NEMESIS ÆTHERION does not simply strike back; it keeps a "
        "ledger. Every blow it takes is remembered, and every blow it "
        "remembers is returned with interest."
    ),
    "abilities": [
        {
            "name":  "Twin Crowns",
            "emoji": "👑",
            "desc":  ("Alternates stance every round. **Wrath** grants +25 ATK and "
                      "ignores 60% of your defence; **Judgement** grants +30 DEF "
                      "and reflects 20 damage. Read the stance or pay for it."),
        },
        {
            "name":  "Law of Retribution",
            "emoji": "⚖️",
            "desc":  (f"Banks {int(DEBT_RATIO * 100)}% of all damage it takes as "
                      f"**Debt** (max {int(DEBT_CAP)}). Its next Special adds the "
                      f"entire ledger to its damage. Spamming attacks arms it."),
        },
        {
            "name":  "Ætheric Ascension",
            "emoji": "✨",
            "desc":  (f"Below {int(ASCEND_HP_THRESHOLD * 100)}% HP **or** after "
                      f"{ASCEND_TURN_GATE} rounds, both crowns wake: "
                      f"+{int(ASCEND_STAT_BONUS * 100)}% stats, double gauge gain, "
                      f"and its two Ultimates unlock. Once per battle."),
        },
    ],
}
