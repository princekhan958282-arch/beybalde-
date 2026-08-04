"""
boss_ai.py  —  the brain behind boss battles

Design note, because this is the part people usually get wrong:

An LLM is a bad move-picker for a game like this. It can't reliably compare
"attack for 84 expected damage" against "charge, then special next turn for
210" — that's arithmetic over a known rule set, and a two-ply search does it
perfectly, instantly, and for free. What an LLM *is* good at is voice: taunts,
phase transitions, reacting to a comeback.

So the split is:
    boss_ai.py  → decides the move   (pure logic, no network, always available)
    gemini.py   → decides what to SAY (optional, degrades to canned lines)

That also means a dead API key, a rate limit, or a network blip can never make
the boss play badly — the worst case is a quieter boss.

How the AI plays "like a pro":
  1. It models you. Every move you make updates an exponentially-weighted
     frequency table, so it picks up on habits within a few turns.
  2. It searches two plies: for each legal move it has, it takes the expected
     outcome across your predicted move distribution, then statically evaluates
     the resulting position.
  3. It mixes. Picking the argmax every time makes a bot that a human can learn
     and beat on rails; it samples from a softmax over move values instead, so
     it is strong but not a fixed pattern.
  4. It plans around the gauge. Charge is only worth it when the Special that
     follows actually lands, so charging is valued by what it unlocks rather
     than by its immediate (zero) damage.
"""

import math
import random
from dataclasses import dataclass, field
from typing import Optional

from .boss_abilities import BossState

# ── Rules mirrored from cogs/core/constants.py ───────────────────────────────
MOVE_ATTACK  = "attack"
MOVE_DEFENSE = "defense"
MOVE_STAMINA = "stamina"
MOVE_CHARGE  = "charge"
MOVE_SPECIAL = "special"

ALL_MOVES = [MOVE_ATTACK, MOVE_DEFENSE, MOVE_STAMINA, MOVE_CHARGE, MOVE_SPECIAL]

STAMINA_COST = {
    MOVE_ATTACK: 1.5, MOVE_DEFENSE: 1.5, MOVE_STAMINA: 0.0,
    MOVE_CHARGE: 1.0, MOVE_SPECIAL: 3.0,
}
STAMINA_MAX        = 15.0
SPECIAL_GAUGE_MAX  = 150
GAUGE_PER = {
    MOVE_ATTACK: 15, MOVE_DEFENSE: 20, MOVE_STAMINA: 10,
    MOVE_CHARGE: 50, MOVE_SPECIAL: 0,
}
GAUGE_PER_DMG_TAKEN    = 12
ATTACK_VS_STAMINA_MULT = 1.2

# Tuned by simulation (see the sanity script in the bug-check notes). The first
# pass used the live game's 0.70 heal ratio against a much lower damage scale,
# which made healing out-pace damage — nobody died and every fight hit the turn
# cap. A heal has to be worth clearly LESS than a hit or Stamina becomes a
# dominant strategy and the fight stalls.
#   damage/turn ≈ atk × 0.90 × defence_mod  ≈ 70-75 at typical stats
#   heal        ≈ sta × 0.30                ≈ 28
# → ~40% of a hit, and a boss fight lands in the 14-22 turn range.
DMG_SCALE       = 0.90
DEFENSE_SOAK    = 0.45     # fraction of raw damage a Defense move absorbs
CLASH_SOAK      = 0.30     # Attack vs Attack — both connect, both reduced
SPECIAL_MULT    = 2.6
SPECIAL_PIERCE  = 0.5      # Special ignores half of a Defense soak

# ── Boss Special ──────────────────────────────────────────────────────────────
# A boss Special is a flat multiple of its attack stat: 340% plus a 50% rider,
# so 390% in total. DMG_SCALE is deliberately NOT applied on this path — the
# percentages are the whole formula, and folding in another 0.90 would mean the
# number in the config isn't the number the fight uses.
#
# Against the ordinary path (attack x DMG_SCALE x SPECIAL_MULT = attack x 2.34)
# this is a 1.67x buff. Only fighters that set Fighter.special_atk_pct get it.
BOSS_SPECIAL_ATK_PCT   = 3.40    # 340% of attack
BOSS_SPECIAL_ATK_RIDER = 0.50    # + 50% of attack
BOSS_SPECIAL_TOTAL     = BOSS_SPECIAL_ATK_PCT + BOSS_SPECIAL_ATK_RIDER

# Defence used to be a strict loss: block a hit for 25 instead of trading 51
# for 51, and you came out 25 behind while dealing nothing. Every policy
# therefore collapsed into attack-spam and the fight had no decisions in it.
# A riposte fixes that — blocking an attack throws damage back, so Defence is
# the right read against an aggressive opponent and the wrong one against a
# charge. That's an actual rock-paper-scissors instead of a dominant strategy.
RIPOSTE_RATIO   = 0.55     # of the blocker's attack stat
HEAL_RATIO      = 0.30
HEAL_INTERRUPT  = 0.50     # healing while being attacked is halved

# Anti-stall. Without this a high-stamina fighter that is ahead just heals every
# turn: a 950 HP NEMESIS absorbed 1,625 damage across 25 turns and finished on
# 57% health, because the AI correctly worked out that healing was the safest
# play. Repeated healing therefore decays, and resets the moment you commit to
# an attack — so Stamina stays a real option for recovering, and stops being a
# way to win by attrition alone.
HEAL_DECAY      = 0.55     # each consecutive heal is worth 55% of the last
HEAL_DECAY_FLOOR = 0.12    # never fully useless

# Attacking used to reset heal_streak to 0, which restored a heal to FULL value.
# Alternating attack / Stamina therefore healed more than spamming Stamina ever
# could — 3 alternating heals were worth 144 against 108 for six in a row — so
# the anti-stall rule punished the honest stall and rewarded the efficient one.
# A Stamina blade could not lose: measured 118-120 wins out of 120 against both
# bosses with random moves, while every other type lost essentially always.
# Attacking now WALKS the streak back one step instead of clearing it, so
# recovery still works and sustained healing still costs you.
# Deliberately less than the +1 a Stamina move adds. At 1.0 an attack refunded
# exactly what the heal cost, so alternating held the streak at 0 forever and
# healed at full value indefinitely — the same exploit in a new coat.
HEAL_STREAK_RECOVERY = 0.5 # steps of decay refunded per committed attack

# A heal is also capped against the healer's own bar. Heal scaled off
# stamina_stat with no ceiling, so the best Stamina blades simply out-healed
# boss damage per turn regardless of decay.
MAX_HEAL_FRACTION = 0.06   # at most 6% of max HP from one Stamina move

# Bosses are held to a much tighter ceiling than players. A boss that heals is
# not defending a lead, it is refusing to close: against a Stamina blade the AI
# spent a THIRD of its turns healing, stretching a fight built as a damage race
# out to 93 turns. That handed the win to whatever could outlast it, so the one
# type that could not out-damage the boss beat it 100% of the time while every
# other type lost. A boss may still top up; it may no longer stall.
BOSS_MAX_HEAL_FRACTION = 0.012

# Hard lifetime cap: across a WHOLE fight a fighter may recover at most this
# fraction of its own bar. The per-heal caps above limited how big one heal
# was, never how many you got, so a Stamina blade still just outlasted the
# boss — it beat both bosses 100% of the time while every other type won 0-5%.
# A budget makes Stamina a real recovery tool with a real cost: you can undo
# about a quarter of a health bar per fight and not one point more.
HEAL_BUDGET_FRACTION = 0.26

# Special deals roughly 2.6x an attack, so a full bar is worth about the extra
# ~85 damage it represents — call it a tenth of a health bar. Derived, not
# guessed: (SPECIAL_MULT - 1) * typical_hit / typical_max_hp.
GAUGE_WEIGHT    = 0.10


@dataclass
class Fighter:
    name:    str
    hp:      float
    max_hp:  float
    attack:  float
    defense: float
    stamina_stat: float
    sp:      float = 10.0          # battle stamina resource
    # Per-fighter stamina ceiling. Defaults to the module STAMINA_MAX so every
    # boss behaves exactly as before — boss_battle deliberately opens its
    # bosses ABOVE the ceiling as a one-off reserve, and regen only ever adds,
    # so a boss must keep the shared value. Players get a bar derived from
    # their stamina stat instead, matching the PvP StaminaManager.
    sp_max:  float = STAMINA_MAX
    gauge:   float = 0.0
    is_boss: bool = False
    heal_streak: float = 0.0       # Stamina pressure — see HEAL_DECAY. Float
                                   # because an attack refunds a HALF step.
    healed_total: float = 0.0      # spent against HEAL_BUDGET_FRACTION
    dmg_mult: float = 1.0          # blade-type damage multiplier — see
                                   # TYPE_DAMAGE_MULT. 1.0 for bosses.
    # Multiplier applied to Specials only, carrying the wielder's `special`
    # stat. 1.0 leaves the Special exactly as it was, which is what every boss
    # uses — a boss has no special stat and its damage is authored directly.
    special_mult: float = 1.0
    # Set to override the Special formula entirely with a flat multiple of the
    # attack stat (see BOSS_SPECIAL_ATK_PCT). None keeps the standard
    # DMG_SCALE x SPECIAL_MULT path.
    #
    # This exists instead of gating on `is_boss` because STORY opponents are
    # also is_boss=True — they set it for the tighter heal ceiling — so keying
    # the buff off that flag would silently re-tune the whole story campaign.
    # Only real bosses set this.
    special_atk_pct: Optional[float] = None
    # Only NEMESIS-class bosses carry this. None for everything else, so the
    # ordinary bosses behave exactly as they did before.
    state:   Optional[BossState] = None   # keep last: positional order matters

    def alive(self) -> bool:
        return self.hp > 0

    def can(self, move: str) -> bool:
        if move == MOVE_SPECIAL and self.gauge < SPECIAL_GAUGE_MAX:
            return False
        return self.sp >= STAMINA_COST[move]

    def legal_moves(self) -> list[str]:
        return [m for m in ALL_MOVES if self.can(m)] or [MOVE_STAMINA]

    def clone(self) -> "Fighter":
        # state.copy() is not optional. The AI clones fighters to search ahead;
        # if the clone shared the BossState object, every simulated turn would
        # flip the real stance, bank real Debt and burn the real Ultimates, and
        # the visible fight would silently desync from the player's actions.
        # Keyword arguments, not positional. Adding healed_total ahead of
        # `state` silently shifted every argument after it: the BossState went
        # into healed_total and state came back None, so the AI searched against
        # a stanceless boss and the budget arithmetic raised TypeError inside
        # the lookahead. Keywords make the next field addition harmless.
        return Fighter(name=self.name, hp=self.hp, max_hp=self.max_hp,
                       attack=self.attack, defense=self.defense,
                       stamina_stat=self.stamina_stat, sp=self.sp,
                       sp_max=self.sp_max,
                       gauge=self.gauge, is_boss=self.is_boss,
                       heal_streak=self.heal_streak,
                       healed_total=self.healed_total,
                       dmg_mult=self.dmg_mult,
                       special_mult=self.special_mult,
                       special_atk_pct=self.special_atk_pct,
                       state=self.state.copy() if self.state else None)

    # ── Stance-adjusted stats (plain fighters are unaffected) ────────────────
    @property
    def eff_attack(self) -> float:
        if not self.state:
            return self.attack
        return (self.attack + self.state.attack_bonus()) * self.state.stat_multiplier()

    @property
    def eff_defense(self) -> float:
        if not self.state:
            return self.defense
        return (self.defense + self.state.defense_bonus()) * self.state.stat_multiplier()


# Blade type had NO mechanical effect in boss fights — only raw stats mattered,
# so an Attack blade was just a stat spread and its whole identity did nothing.
# Attack types now hit meaningfully harder, which is the thing that was supposed
# to make aggression the answer to a boss.
TYPE_DAMAGE_MULT = {
    "attack":  1.40,
    "balance": 1.00,
    "defense": 1.00,
    "stamina": 1.00,
}


def type_damage_mult(blade_type: Optional[str]) -> float:
    return TYPE_DAMAGE_MULT.get(str(blade_type or "").strip().lower(), 1.0)


def _raw_damage(src: Fighter, special: bool = False) -> float:
    base = src.eff_attack * DMG_SCALE * src.dmg_mult
    if not special:
        return base
    # Bosses replace the formula outright with a flat percentage of attack.
    if src.special_atk_pct is not None:
        return src.eff_attack * src.special_atk_pct * src.dmg_mult
    # special_mult is the wielder's `special` stat relative to its printed
    # value, so a levelled bey's Special grows. Applied on this branch only —
    # ordinary attacks must not inherit it.
    return base * SPECIAL_MULT * max(1.0, src.special_mult)


def resolve(a: Fighter, b: Fighter, move_a: str, move_b: str) -> dict:
    """Apply one exchange. Mutates both fighters. Returns a small report.

    Deterministic on purpose — the AI searches over this exact function, so
    randomness lives in move *selection*, not in resolution. That keeps the
    search honest and makes the fight feel fair rather than swingy.
    """
    log = {"dmg_to_a": 0.0, "dmg_to_b": 0.0, "heal_a": 0.0, "heal_b": 0.0,
           "note_a": "", "note_b": ""}

    a.sp = max(0.0, a.sp - STAMINA_COST[move_a])
    b.sp = max(0.0, b.sp - STAMINA_COST[move_b])

    def offence(src, dst, move, other_move, tag):
        if move not in (MOVE_ATTACK, MOVE_SPECIAL):
            return 0.0
        special = move == MOVE_SPECIAL
        dmg = _raw_damage(src, special)

        if other_move == MOVE_DEFENSE:
            soak = DEFENSE_SOAK * (1 + dst.eff_defense / 200.0)
            if special:
                soak *= (1 - SPECIAL_PIERCE)
            dmg *= max(0.15, 1 - soak)
            log[f"note_{tag}"] = "blocked"
        elif other_move == MOVE_STAMINA:
            dmg *= ATTACK_VS_STAMINA_MULT
            log[f"note_{tag}"] = "punished a heal"
        elif other_move in (MOVE_ATTACK, MOVE_SPECIAL):
            dmg *= (1 - CLASH_SOAK)
            log[f"note_{tag}"] = "clash"
        elif other_move == MOVE_CHARGE:
            log[f"note_{tag}"] = "caught mid-charge"

        # Wrath ignores part of the target's guard.
        pierce = src.state.pierce() if src.state else 0.0
        eff_def = dst.eff_defense * (1.0 - pierce)
        dmg *= max(0.4, 1 - eff_def / 400.0)
        return max(1.0, dmg)

    dmg_b = offence(a, b, move_a, move_b, "a")
    dmg_a = offence(b, a, move_b, move_a, "b")

    # Crystal layers eat a slice of one incoming hit, then shatter.
    if b.state is not None and hasattr(b.state, "absorb") and dmg_b > 0:
        dmg_b, shattered = b.state.absorb(dmg_b)
        if shattered:
            log["note_b"] = "shard shattered"
    if a.state is not None and hasattr(a.state, "absorb") and dmg_a > 0:
        dmg_a, shattered = a.state.absorb(dmg_a)
        if shattered:
            log["note_a"] = "shard shattered"

    # Riposte: a successful block punishes the attacker.
    if move_a == MOVE_DEFENSE and move_b in (MOVE_ATTACK, MOVE_SPECIAL):
        # dmg_mult belongs here too. The type bonus was only reaching attacks
        # and Specials via _raw_damage, so an Attack blade's riposte — a real
        # damage source at 0.55 of its attack stat — was still unbuffed and the
        # blade got noticeably less than the advertised 40% overall.
        dmg_b += a.eff_attack * DMG_SCALE * RIPOSTE_RATIO * a.dmg_mult
        log["note_a"] = "riposte"
    if move_b == MOVE_DEFENSE and move_a in (MOVE_ATTACK, MOVE_SPECIAL):
        dmg_a += b.eff_attack * DMG_SCALE * RIPOSTE_RATIO * b.dmg_mult
        log["note_b"] = "riposte"

    # Judgement stance reflects a slice of whatever connected.
    if a.state and a.state.reflect() and move_b in (MOVE_ATTACK, MOVE_SPECIAL):
        dmg_b += a.state.reflect()
        log["note_a"] = "reflected"
    if b.state and b.state.reflect() and move_a in (MOVE_ATTACK, MOVE_SPECIAL):
        dmg_a += b.state.reflect()
        log["note_b"] = "reflected"

    def healing(src, own_move, other_move):
        if own_move != MOVE_STAMINA:
            return 0.0
        heal = src.stamina_stat * HEAL_RATIO
        if other_move in (MOVE_ATTACK, MOVE_SPECIAL):
            heal *= HEAL_INTERRUPT
        if src.hp < src.max_hp * 0.4:
            heal *= 1.2
        decay = max(HEAL_DECAY_FLOOR, HEAL_DECAY ** src.heal_streak)
        # Cap any single heal against the fighter's own health bar. Heal scaled
        # off stamina_stat with nothing holding it down, so a top Stamina blade
        # simply out-healed the boss's damage per turn and could not lose.
        ceiling = (BOSS_MAX_HEAL_FRACTION if src.is_boss else MAX_HEAL_FRACTION)
        amount  = min(src.max_hp * ceiling, heal * decay)
        # Spend from the lifetime budget; once it's gone, Stamina still
        # restores sp and still dodges damage, it just stops giving HP back.
        budget  = max(0.0, src.max_hp * HEAL_BUDGET_FRACTION - src.healed_total)
        return min(amount, budget)

    heal_a = healing(a, move_a, move_b)
    heal_b = healing(b, move_b, move_a)
    # Spend the lifetime budget. Without this healed_total stayed 0 forever and
    # HEAL_BUDGET_FRACTION never bound — the cap existed but did nothing.
    a.healed_total += heal_a
    b.healed_total += heal_b

    b.hp = max(0.0, min(b.max_hp, b.hp - dmg_b + heal_b))
    a.hp = max(0.0, min(a.max_hp, a.hp - dmg_a + heal_a))

    for f, mv, took in ((a, move_a, dmg_a), (b, move_b, dmg_b)):
        gmult = f.state.gauge_multiplier() if f.state else 1.0
        frozen = bool(f.state is None and (
            (a.state and a is not f and a.state.freeze_turns > 0) or
            (b.state and b is not f and b.state.freeze_turns > 0)))

        if mv == MOVE_SPECIAL:
            f.gauge = 0.0
        elif not frozen:
            f.gauge = min(SPECIAL_GAUGE_MAX, f.gauge + GAUGE_PER[mv] * gmult)
        if took > 0 and not frozen:
            f.gauge = min(SPECIAL_GAUGE_MAX, f.gauge + GAUGE_PER_DMG_TAKEN * gmult)

        # Law of Retribution banks what it suffers.
        if f.state and took > 0:
            f.state.bank_debt(took)
            # Crystalline Aegis / Astral Ascendance react to real hits too,
            # not only to Specials.
            if hasattr(f.state, "break_stars"):
                f.state.break_stars(took, f.max_hp)

        # Regen tops up TOWARD the ceiling but never confiscates a surplus.
        #
        # `min(STAMINA_MAX, sp + regen)` also clamps DOWNWARD, so any fighter
        # starting above the ceiling — the boss opens with a reserve — lost the
        # entire surplus on its very first turn. Capping at max(ceiling,
        # current) means regen can only ever add, which is what regeneration
        # is supposed to mean.
        if mv == MOVE_STAMINA:
            f.sp = min(max(f.sp_max, f.sp), f.sp + 2.5)
            f.heal_streak += 1
        else:
            f.sp = min(max(f.sp_max, f.sp), f.sp + 0.5)
            if mv in (MOVE_ATTACK, MOVE_SPECIAL):
                # Walk the streak back rather than clearing it — see
                # HEAL_STREAK_RECOVERY.
                f.heal_streak = max(0, f.heal_streak - HEAL_STREAK_RECOVERY)

    # Twin Crowns flips after the exchange; Ascension checks the new HP.
    for f in (a, b):
        if not f.state:
            continue
        f.state.tick()
        f.state.flip_stance()
        if f.alive() and f.state.should_ascend(f.hp / f.max_hp):
            f.state.ascend()
            log["ascended"] = f.name

    log.update(dmg_to_a=dmg_a, dmg_to_b=dmg_b, heal_a=heal_a, heal_b=heal_b)
    return log


# ── Opponent model ────────────────────────────────────────────────────────────

@dataclass
class OpponentModel:
    """Exponentially-weighted move frequencies, so recent habits dominate."""

    decay: float = 0.82
    counts: dict = field(default_factory=lambda: {m: 1.0 for m in ALL_MOVES})
    history: list = field(default_factory=list)

    def observe(self, move: str) -> None:
        for m in self.counts:
            self.counts[m] *= self.decay
        self.counts[move] = self.counts.get(move, 0.0) + 1.0
        self.history.append(move)

    def predict(self, opponent: Fighter) -> dict[str, float]:
        legal = set(opponent.legal_moves())
        weights = {m: max(0.01, self.counts.get(m, 0.0)) for m in legal}

        # Priors that hold for almost every human: a full gauge gets spent, and
        # a hurt player reaches for the heal.
        if MOVE_SPECIAL in legal:
            weights[MOVE_SPECIAL] = weights.get(MOVE_SPECIAL, 0.0) + 3.0
        if opponent.hp < opponent.max_hp * 0.35 and MOVE_STAMINA in legal:
            weights[MOVE_STAMINA] = weights.get(MOVE_STAMINA, 0.0) + 1.5
        if opponent.sp < 2.0:
            weights[MOVE_STAMINA] = weights.get(MOVE_STAMINA, 0.0) + 2.0

        total = sum(weights.values())
        return {m: w / total for m, w in weights.items()}


# ── Evaluation & search ───────────────────────────────────────────────────────

def evaluate(boss: Fighter, foe: Fighter) -> float:
    """Static score of a position from the boss's point of view."""
    hp_term = (boss.hp / boss.max_hp) - (foe.hp / foe.max_hp)

    # A charged gauge is worth exactly what the Special buys you: the damage
    # ABOVE a normal attack, as a fraction of the target's health. Weighting it
    # higher than that (the first version used 0.28) made spending the gauge
    # look like a loss, so the AI hoarded a full bar and never fired it.
    def gauge_value(f: Fighter) -> float:
        ready = min(1.0, f.gauge / SPECIAL_GAUGE_MAX)
        return ready ** 1.5

    gauge_term = gauge_value(boss) - gauge_value(foe)

    # Stamina only matters when it's about to run out and lock you into Stamina.
    def sp_value(f: Fighter) -> float:
        return min(1.0, f.sp / 5.0)

    sp_term = sp_value(boss) - sp_value(foe)

    # Finishing beats everything else.
    if not foe.alive():
        return 100.0
    if not boss.alive():
        return -100.0

    return hp_term * 1.0 + gauge_term * GAUGE_WEIGHT + sp_term * 0.12


def move_values(boss: Fighter, foe: Fighter, model: OpponentModel,
                depth: int = 2) -> dict[str, float]:
    """Expected value of each legal boss move against the predicted foe."""
    prediction = model.predict(foe)
    values: dict[str, float] = {}

    for my_move in boss.legal_moves():
        total = 0.0
        for their_move, p in prediction.items():
            if p <= 0:
                continue
            b, f = boss.clone(), foe.clone()
            resolve(b, f, my_move, their_move)

            score = evaluate(b, f)
            if depth > 1 and b.alive() and f.alive():
                # One more ply: assume both sides then play their own best
                # immediate reply. Cheap, and it's what stops the AI from
                # walking into an obvious counter-punch.
                score += 0.6 * _best_reply_score(b, f, model)
            total += p * score
        values[my_move] = total

    return values


def _best_reply_score(boss: Fighter, foe: Fighter, model: OpponentModel) -> float:
    prediction = model.predict(foe)
    best = -math.inf
    for my_move in boss.legal_moves():
        total = 0.0
        for their_move, p in prediction.items():
            b, f = boss.clone(), foe.clone()
            resolve(b, f, my_move, their_move)
            total += p * evaluate(b, f)
        best = max(best, total)
    return best if best > -math.inf else 0.0


def outcome(a: Fighter, b: Fighter,
            hp_before: Optional[tuple[float, float]] = None) -> Optional[str]:
    """'a', 'b', 'draw', or None if the fight continues.

    Simultaneous knockouts are real here: resolution is simultaneous, so a
    double Special can drop both fighters on the same exchange. Checking
    `if not b.alive(): return "a"` before checking a — the obvious way to
    write it — silently hands every double-KO to whoever is tested first.
    That alone skewed a mirror match to 71/29.

    Fixing that honestly produced 46% draws in a mirror, which is a worse
    watch than a bad tiebreak. So double-KOs are settled by who was healthier
    going into the exchange: you survive on fumes. Pass `hp_before` as the
    (a, b) HP fractions from before resolve() to enable it; an exact tie is
    still a genuine draw.
    """
    a_out, b_out = not a.alive(), not b.alive()

    if a_out and b_out:
        if hp_before:
            fa, fb = hp_before
            if fa > fb:
                a.hp = 1.0
                return "a"
            if fb > fa:
                b.hp = 1.0
                return "b"
        return "draw"
    if b_out:
        return "a"
    if a_out:
        return "b"
    return None


def hp_fractions(a: Fighter, b: Fighter) -> tuple[float, float]:
    """Snapshot for outcome()'s double-KO tiebreak. Call before resolve()."""
    return (a.hp / a.max_hp, b.hp / b.max_hp)


# ── Difficulty ────────────────────────────────────────────────────────────────
# The unrestricted AI beat every scripted opponent 100% of the time at equal
# stats, and raising boss HP changed nothing because the fight was decided by
# policy, not by health. "Plays like a pro" can't mean "cannot be beaten" — a
# boss nobody clears is a boss nobody fights twice.
#
# So strength is dialled with three levers rather than with HP:
#   depth     2-ply lookahead vs 1-ply (1-ply walks into counter-punches)
#   model     opponent modelling on/off (off = it can't read your habits)
#   blunder   chance of deliberately taking a merely-decent move
DIFFICULTY = {
    "rookie":   {"depth": 1, "model": False, "blunder": 0.45, "mix": 0.60},
    "veteran":  {"depth": 1, "model": True,  "blunder": 0.25, "mix": 0.40},
    "elite":    {"depth": 2, "model": True,  "blunder": 0.12, "mix": 0.30},
    "legend":   {"depth": 2, "model": True,  "blunder": 0.04, "mix": 0.20},
    "nightmare":{"depth": 2, "model": True,  "blunder": 0.00, "mix": 0.15},
}


def choose_move(boss: Fighter, foe: Fighter, model: OpponentModel,
                mix: float = 0.25,
                rng: Optional[random.Random] = None,
                difficulty: Optional[str] = None) -> tuple[str, dict]:
    """Pick the boss's move. Returns (move, per-move values for debugging).

    Unpredictability without stupidity. The first version softmaxed over ALL
    moves with a fixed temperature of 0.35 — but move values here span about
    0.2 total, so a temperature of 0.35 flattened the distribution and the boss
    regularly played its *worst* option. (Observed: firing Special at value
    -0.12 while Attack sat at +0.16.)

    Instead: keep only the moves within `mix` of the best — measured against the
    actual spread of this position, not an absolute number — then softmax among
    those. The boss stays unreadable but never picks a move it knows is bad.
    """
    rng = rng or random

    # A full gauge is always spent — checked before difficulty handling so a
    # blunder roll can't skip it either.
    #
    # Previously the Special was only one candidate among the rest, so the boss
    # could sit on a charged gauge for many rounds whenever Attack scored
    # marginally higher. To a player that reads as the boss never using its
    # Special, and it wastes the whole charge mechanic: charging costs real
    # tempo, so once that cost is paid the payoff should land.
    if boss.can(MOVE_SPECIAL) and boss.gauge >= SPECIAL_GAUGE_MAX:
        return MOVE_SPECIAL, {MOVE_SPECIAL: 1.0}

    cfg = DIFFICULTY.get(difficulty or "", None)
    depth = 2
    if cfg:
        mix   = cfg["mix"]
        depth = cfg["depth"]
        if not cfg["model"]:
            model = OpponentModel()          # flat priors — reads nothing
        if cfg["blunder"] and rng.random() < cfg["blunder"]:
            # Take a legal-but-not-best move on purpose. This is what gives a
            # human the openings that make a fight winnable.
            legal = boss.legal_moves()
            if len(legal) > 1:
                vals = move_values(boss, foe, model, depth=depth)
                ranked = sorted(vals, key=lambda m: -vals[m])
                return rng.choice(ranked[1:]) if len(ranked) > 1 else ranked[0], vals

    values = move_values(boss, foe, model, depth=depth)
    if not values:
        return MOVE_STAMINA, {}

    # Never pass up a lethal special.
    if MOVE_SPECIAL in values:
        b, f = boss.clone(), foe.clone()
        resolve(b, f, MOVE_SPECIAL, MOVE_DEFENSE)   # even through a block
        if not f.alive():
            return MOVE_SPECIAL, values

    best   = max(values.values())
    worst  = min(values.values())
    spread = max(1e-6, best - worst)
    cutoff = best - spread * mix

    pool = {m: v for m, v in values.items() if v >= cutoff} or {
        max(values, key=values.get): best}

    temp   = spread * 0.12 or 1e-6
    scaled = {m: math.exp((v - best) / temp) for m, v in pool.items()}
    total  = sum(scaled.values())
    roll   = rng.random() * total
    acc    = 0.0
    for move, w in scaled.items():
        acc += w
        if roll <= acc:
            return move, values
    return max(pool, key=pool.get), values
