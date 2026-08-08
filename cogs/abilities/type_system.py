"""
battle/type_system.py
---------------------
Type advantage system.

Types affect PASSIVE PERFORMANCE, not action counters.
Actions remain: Attack > Stamina > Defense > Attack (rock-paper-scissors).

Modifier ranges (all stat-scaled):
  Attack  type: +10–15% outgoing damage     (scales with attack stat)
  Defense type: +10–15% incoming mitigation (scales with defense stat)
  Stamina type: +10–15% stamina efficiency  (scales with stamina stat)
  Balance type: small boost to all three    (scales with all three stats)

Formula:
  base_bonus = BASE_TYPE_BONUS (0.10)
  stat_bonus = stat / STAT_SCALE_DIVISOR   (adds up to 0.05 at stat=100)
  total_mod  = base_bonus + stat_bonus     (0.10 – 0.15)

Balance type uses BALANCE_SCALE_FACTOR (0.5) on each sub-modifier so it
gets ~half the bonus per dimension but benefits across all three.

Type advantage — which bonuses are active per matchup:
  Only the advantaged type activates its bonus; the loser's is suppressed.
  Triangle: Attack > Stamina > Defense > Attack
  Balance is always active regardless of opponent, and never suppresses
  the opponent's bonus either (both sides activate vs Balance).

  Matchup            Active bonuses
  Attack  vs Stamina  Attack only
  Stamina vs Defense  Stamina only
  Defense vs Attack   Defense only
  Attack  vs Defense  Defense only
  Defense vs Stamina  Stamina only  (wait — see ADVANTAGE dict; loser suppressed)
  Balance vs anyone   Balance + opponent
  Balance vs Balance  Both Balance bonuses

Use resolve_active_bonuses(type_a, type_b) → (bool, bool) to get which
side has its bonus active before applying TypeModifiers helpers.
"""

import math

from .constants import STABILITY_START_DEFAULT, STABILITY_START_DEFENSE

BASE_TYPE_BONUS      = 0.10    # minimum type bonus regardless of stat
STAT_SCALE_DIVISOR   = 2000.0  # stat / divisor → 0–0.05 bonus at stat 0–100
BALANCE_SCALE_FACTOR = 0.50    # Balance gets this fraction of each sub-bonus
TYPE_BONUS_MAX       = 0.15    # hard cap so modified stats (>100) can't inflate bonuses

# ── Late-game attrition (type-owned) ──────────────────────────────────────────
# Long matches were unbounded: nothing caps the round counter, and the Stamina
# move costs 0 stamina while restoring +3 and healing ~64 HP, so two cautious
# players could hold a stalemate indefinitely.
#
# From ATTRITION_START every Bey bleeds stamina each round and leans further
# into the stat its type is named for. This lives here rather than in the
# battle layer because both halves are decided purely by type — the same place
# atk_mult / def_mult / sta_mult and starting stability are decided.
#
# Stamina KO is already wired up (StaminaManager.check_stamina_ko drops HP to 0
# at zero stamina), so the bleed is what actually resolves the match.
ATTRITION_START = 15

# The bleed GROWS each round rather than sitting flat: a flat rate can never
# out-pace the Stamina move (0 cost), so a staller just tops the bar back up
# forever. Each band is (up_to_round, increment_added_each_round):
#
#   rounds 15-25  +0.25 per round  ->  r16 = 0.25, r17 = 0.5 ... r25 = 2.5
#   rounds 25-35  +0.5  per round  ->  r26 = 3.0,  r27 = 3.5 ... r35 = 7.5
#   beyond 35     +0.9  per round  ->  keeps climbing, so it always resolves
#
# The final band is open-ended, which is what guarantees termination — no
# separate hard cap is needed.
#
# These were 0.1 / 0.2 / 0.3, tuned when the Stamina move restored a FLAT +3
# and passive regen did nothing. The stamina overhaul made recovery scale with
# the stat (up to +9) and gave passive regen a real value (up to +2.15), which
# silently decoupled the two: a staller regenerated 3.95-11.15 per round while
# attrition still climbed at the old rate, so a stall that used to resolve on
# round 36 took 46 to 115 rounds depending on the stamina stat. Nobody plays 46
# rounds of a Discord battle with a 30-second move timer, so in practice it
# never resolved at all — the fight simply sat there.
ATTRITION_BANDS: tuple[tuple[int | None, float], ...] = (
    (25,   0.25),
    (35,   0.5),
    (None, 0.9),
)

# The bleed also scales with the stamina ENGINE it has to out-pace, so this can
# never decouple again: a blade whose recovery is bigger bleeds proportionally
# harder. Without this the constants above have to be re-tuned by hand every
# time a stamina number moves, and the failure mode when somebody forgets is
# invisible — the game just stops ending.
ATTRITION_PER_STAMINA_STAT = 0.010

# Stamina types are built to outlast: they take this fraction of the bleed.
ATTRITION_STAMINA_MULT = 0.5

# Stat gained per attrition round by the type that owns that stat.
ATTRITION_STAT_PER_ROUND = 3
# Balance leans both ways, at half rate each.
ATTRITION_BALANCE_MULT   = 0.5


def attrition_active(round_no: int) -> bool:
    return int(round_no) >= ATTRITION_START


def attrition_base_drain(round_no: int) -> float:
    """Cumulative bleed at this round, before the type multiplier."""
    r = int(round_no)
    if r <= ATTRITION_START:
        return 0.0
    total = 0.0
    prev  = ATTRITION_START
    for upto, step in ATTRITION_BANDS:
        end = r if upto is None else min(r, upto)
        if end > prev:
            total += (end - prev) * step
            prev = end
        if upto is not None and r <= upto:
            break
    return round(total, 2)


def attrition_drain_for(btype: str, round_no: int,
                        sta_stat: float = 0.0) -> float:
    """Stamina this type loses this round. 0 before attrition starts.

    `sta_stat` is the blade's EFFECTIVE stamina stat — the same number that
    drives how much the Stamina move restores. Scaling the bleed by it is what
    keeps attrition tied to the engine it exists to out-pace; leaving it at the
    default 0 reproduces the old flat behaviour exactly, so an older caller
    that has not been updated still works.
    """
    if not attrition_active(round_no):
        return 0.0
    drain = attrition_base_drain(round_no)
    drain *= 1.0 + max(0.0, float(sta_stat or 0.0)) * ATTRITION_PER_STAMINA_STAT
    if "stamina" in str(btype or "").lower():
        drain *= ATTRITION_STAMINA_MULT
    return round(drain, 2)


def attrition_stat_gains(btype: str, round_no: int) -> dict[str, int]:
    """Stat bonuses this type earns this round, keyed by stat name."""
    if not attrition_active(round_no):
        return {}
    t    = str(btype or "").lower()
    step = ATTRITION_STAT_PER_ROUND
    if "attack" in t:
        return {"attack": step}
    if "defense" in t or "defence" in t:
        return {"defense": step}
    if "stamina" in t:
        return {}                    # its bonus is the halved drain
    half = max(1, int(round(step * ATTRITION_BALANCE_MULT)))
    return {"attack": half, "defense": half}


# Type advantage triangle: key beats value (key's bonus activates, value's suppressed)
ADVANTAGE: dict[str, str] = {
    "attack":  "stamina",
    "stamina": "defense",
    "defense": "attack",
}


def resolve_active_bonuses(type_a: str, type_b: str) -> tuple[bool, bool]:
    """Return (a_active, b_active) — whether each side's type bonus is active.

    Rules:
      • Balance always activates its bonus and never suppresses the opponent's.
      • Among Attack / Stamina / Defense, only the advantaged side activates;
        the disadvantaged side's bonus is suppressed.
      • Unknown / mirror non-Balance types → both inactive (no advantage).

    Examples:
      ("attack",  "stamina")  → (True,  False)
      ("defense", "attack")   → (True,  False)
      ("balance", "attack")   → (True,  True)
      ("balance", "balance")  → (True,  True)
      ("attack",  "attack")   → (False, False)
    """
    a = type_a.lower()
    b = type_b.lower()

    a_balance = "balance" in a
    b_balance = "balance" in b

    # Balance is always active; it never suppresses the other side either
    if a_balance or b_balance:
        return True, True

    # Triangle advantage — only the winner activates
    a_active = ADVANTAGE.get(a) == b
    b_active = ADVANTAGE.get(b) == a
    return a_active, b_active


class TypeModifiers:
    """Compute and cache per-Bey type modifiers for one BattleSession.

    Attributes (all floats, 1.0 = no bonus):
      atk_mult  – multiply outgoing damage by this
      def_mult  – reduce incoming damage by (def_mult - 1); e.g. 1.12 → 12% cut
      sta_mult  – multiply stamina action recovery by this
    """

    def __init__(self, blade: dict, stats: dict | None = None):
        btype = str(blade.get("type", "")).lower()
        # Modified stats (base + parts + avatar + level mult) can be supplied
        # by the session so type bonuses scale with the player's REAL stats.
        # Falls back to raw blade stats when not provided.
        stats = stats if stats else blade.get("stats", {})
        atk   = stats.get("attack",  50)
        defn  = stats.get("defense", 50)
        sta   = stats.get("stamina", 50)

        def _bonus(stat: float) -> float:
            """Stat-scaled type bonus, capped at TYPE_BONUS_MAX (0.15)."""
            return min(TYPE_BONUS_MAX, BASE_TYPE_BONUS + max(0.0, stat) / STAT_SCALE_DIVISOR)

        self.btype = btype

        if "attack" in btype:
            bonus         = _bonus(atk)
            self.atk_mult = round(1.0 + bonus, 4)
            self.def_mult = 1.0
            self.sta_mult = 1.0

        elif "defense" in btype:
            bonus         = _bonus(defn)
            self.atk_mult = 1.0
            self.def_mult = round(1.0 + bonus, 4)
            self.sta_mult = 1.0

        elif "stamina" in btype:
            bonus         = _bonus(sta)
            self.atk_mult = 1.0
            self.def_mult = 1.0
            self.sta_mult = round(1.0 + bonus, 4)

        elif "balance" in btype:
            a_b = _bonus(atk)  * BALANCE_SCALE_FACTOR
            d_b = _bonus(defn) * BALANCE_SCALE_FACTOR
            s_b = _bonus(sta)  * BALANCE_SCALE_FACTOR
            self.atk_mult = round(1.0 + a_b, 4)
            self.def_mult = round(1.0 + d_b, 4)
            self.sta_mult = round(1.0 + s_b, 4)

        else:
            # Unknown / no type — neutral modifiers
            self.atk_mult = 1.0
            self.def_mult = 1.0
            self.sta_mult = 1.0

        # ── Stability starting value ──────────────────────────────────────────
        # Defense type starts at 150; all others start at 100.
        # Owned here because starting stability is a type-based property,
        # consistent with how atk/def/sta multipliers are type-based.
        self.stability_start: int = STABILITY_START_DEFENSE if "defense" in btype else STABILITY_START_DEFAULT

    # ── Advantage activation ─────────────────────────────────────────────────

    def suppress_bonus(self) -> None:
        """Neutralise all type bonuses (called when this Bey is the loser in
        the type-advantage matchup).  Balance types are never suppressed."""
        if "balance" not in self.btype:
            self.atk_mult = 1.0
            self.def_mult = 1.0
            self.sta_mult = 1.0

    # ── Convenience helpers ───────────────────────────────────────────────────

    def apply_attack(self, raw_dmg: int) -> int:
        """Scale outgoing attack damage by this Bey's attack-type multiplier."""
        return math.ceil(raw_dmg * self.atk_mult)

    def apply_defense(self, incoming_dmg: int) -> int:
        """Reduce incoming damage by this Bey's defense-type mitigation."""
        if self.def_mult <= 1.0:
            return incoming_dmg
        reduction = self.def_mult - 1.0
        return max(1, math.ceil(incoming_dmg * (1.0 - reduction)))

    def apply_stamina(self, base_recovery: int) -> int:
        """Scale stamina recovery by this Bey's stamina-type multiplier."""
        return math.ceil(base_recovery * self.sta_mult)

    def summary(self) -> str:
        """One-line description of all active bonuses (for battle start log)."""
        parts = []
        if self.atk_mult > 1.0:
            parts.append(f"ATK ×{self.atk_mult:.3f}")
        if self.def_mult > 1.0:
            parts.append(f"DEF -{int((self.def_mult - 1) * 100)}% dmg taken")
        if self.sta_mult > 1.0:
            parts.append(f"STA ×{self.sta_mult:.3f} recovery")
        return ", ".join(parts) if parts else "no type bonus"
