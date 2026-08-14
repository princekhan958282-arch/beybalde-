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

  Matchup             Active bonuses
  Attack  vs Stamina   Attack only
  Stamina vs Defense   Stamina only
  Defense vs Attack    Defense only
  Stamina vs Attack    Attack only        (the same row, read the other way)
  Defense vs Stamina   Stamina only
  Attack  vs Defense   Defense only
  any mirror           neither
  Balance vs anyone    Balance + opponent
  Balance vs Balance   both

Use resolve_active_bonuses(type_a, type_b) → (bool, bool) to get which
side has its bonus active before applying TypeModifiers helpers.

Signature effects
-----------------
Having the advantage does more than scale a stat. Each type gets one effect
that fires only while it is the advantaged side:

  Attack  (vs Stamina)  every landing Attack strips ATTACK_STABILITY_STRIP
                        stability — it knocks a grinder off its axis.
  Stamina (vs Defense)  its own move costs are cut STAMINA_COST_CUT — the
                        answer to a type that wins by outlasting you.
  Defense (vs Attack)   DEFENSE_REFLECT of the damage it mitigates is
                        returned to the attacker.
  Balance               takes only BALANCE_EFFECT_SCALE of any of the above
                        aimed at it. That, plus its halved multipliers, is
                        what "never fully advantaged, never fully
                        disadvantaged" means in code.
"""

import math

from .constants import STABILITY_START_DEFAULT, STABILITY_START_DEFENSE

BASE_TYPE_BONUS      = 0.10    # minimum type bonus regardless of stat
STAT_SCALE_DIVISOR   = 2000.0  # stat / divisor → 0–0.05 bonus at stat 0–100
BALANCE_SCALE_FACTOR = 0.50    # Balance gets this fraction of each sub-bonus
TYPE_BONUS_MAX       = 0.15    # hard cap so modified stats (>100) can't inflate bonuses

# ── Signature effects ─────────────────────────────────────────────────────────
# One per type, fired only while that type holds the advantage. Sized so none
# of them decides a fight on its own:
#
#   6 stability is about a tenth of a normal blade's 100-point pool per landing
#   Attack — real pressure on the burst gauge over a few rounds, not a kill.
#   Deliberately stability ONLY: an earlier draft also drained stamina, which
#   meant Attack's answer to a Stamina type was to attack the resource that
#   type is named for, on top of already beating it.
#
#   A 25% cost cut is the quietest and the most durable of the three. Against
#   Defence — which wins by grinding — it is close to two extra actions across
#   a long fight.
#
#   Mitigation is 10-15% of a hit, so returning half of it is roughly 5-7% of
#   incoming damage: it compounds over a long fight and never spikes. It also
#   costs nothing to compute — the mitigated amount is already worked out at
#   the call site.
ATTACK_STABILITY_STRIP = 6      # per landing normal Attack
STAMINA_COST_CUT       = 0.25   # fraction off this blade's own move costs
DEFENSE_REFLECT        = 0.50   # fraction of mitigated damage sent back
BALANCE_EFFECT_SCALE   = 0.50   # Balance takes half of any of the above

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


# ── The type chart. There is only one. ────────────────────────────────────────
# Attack beats Stamina, Stamina beats Defence, Defence beats Attack. Balance
# sits outside the triangle: it is never fully advantaged and never fully
# disadvantaged (see resolve_active_bonuses and BALANCE_SCALE_FACTOR).
#
# This used to be one of THREE charts that disagreed with each other. A second
# lived in battle/stability_manager.py and was inverted for attack-vs-stamina —
# an Attack bey's stability effects were switched OFF in the exact matchup it
# is supposed to dominate — and a third, in ui/help_cog.py, was hand-written,
# omitted Balance entirely, and is now generated from this dict so it cannot
# drift again. Anything that needs to know who beats whom reads it from here.
ADVANTAGE: dict[str, str] = {
    "attack":  "stamina",
    "stamina": "defense",
    "defense": "attack",
}

TYPES = ("attack", "defense", "stamina", "balance")


def normalise_type(value) -> str:
    """One of TYPES, or "" — the single way to read a blade's type string.

    THE BUG THIS REPLACES: every consumer normalised differently.
    `TypeModifiers` matched by substring (`"attack" in btype`) while
    `resolve_active_bonuses` did an exact dict lookup (`ADVANTAGE.get(a)`), so
    a blade typed "Attack Type" was handed atk_mult 1.15 by one half of the
    system and no advantage at all by the other — the bonus was computed and
    then never switched on, silently, for the whole fight. A third scheme lived
    in stability_manager and a fourth (Title-case keys) in utils/hp_system.

    Order matters: "balance" is checked LAST so a composite like
    "attack/balance" resolves to its primary type rather than to Balance.
    """
    t = str(value or "").strip().lower()
    if not t:
        return ""
    if t in TYPES:
        return t
    for known in ("attack", "defense", "stamina", "balance"):
        if known in t:
            return known
    # "defence" is the British spelling and appears in prose all over this
    # codebase; accepting it here costs nothing and fails less surprisingly.
    if "defen" in t:
        return "defense"
    return ""


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
    a = normalise_type(type_a)
    b = normalise_type(type_b)

    # Balance is always active; it never suppresses the other side either.
    #
    # Deliberate: making Balance suppress its opponent would hand it a bonus
    # nobody can answer, and Balance would simply be the best type. What keeps
    # it fair is the other half of the rule — Balance's own multipliers are
    # halved (BALANCE_SCALE_FACTOR), and it takes only half of any type
    # signature effect aimed at it (BALANCE_EFFECT_SCALE). Never fully
    # advantaged, never fully disadvantaged.
    if a == "balance" or b == "balance":
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
        # The SAME normaliser resolve_active_bonuses uses. When these two
        # disagreed, a blade could be given a multiplier that was never
        # switched on.
        btype = normalise_type(blade.get("type"))
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

        if btype == "attack":
            bonus         = _bonus(atk)
            self.atk_mult = round(1.0 + bonus, 4)
            self.def_mult = 1.0
            self.sta_mult = 1.0

        elif btype == "defense":
            bonus         = _bonus(defn)
            self.atk_mult = 1.0
            self.def_mult = round(1.0 + bonus, 4)
            self.sta_mult = 1.0

        elif btype == "stamina":
            bonus         = _bonus(sta)
            self.atk_mult = 1.0
            self.def_mult = 1.0
            self.sta_mult = round(1.0 + bonus, 4)

        elif btype == "balance":
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
        self.stability_start: int = STABILITY_START_DEFENSE if btype == "defense" else STABILITY_START_DEFAULT

    # There was a `suppress_bonus()` here that zeroed all three multipliers.
    # Nothing ever called it — all three sites (attack_manager's two type-mod
    # helpers and the Special barrage) resolve the gate with
    # resolve_active_bonuses and return early instead. A second, unused way to
    # express the same rule is a second way for it to drift.

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
