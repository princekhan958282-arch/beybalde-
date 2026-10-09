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
import copy
from dataclasses import dataclass, field, replace
from typing import Optional

from .boss_abilities import BossState
from ..type_gimmicks import (TypeGimmickEngine, GimmickState, base_damage,
                             passive_stat_multiplier, hp_damage)
from cogs.abilities.type_system import normalise_type

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
    state:   Optional[BossState] = None
    special_damage: Optional[float] = None  # Authored stat formula, when supplied.

    bey_type: str = ""
    level: int = 1
    move_power: float = 100
    stats_pretyped: bool = False
    morph_rounds: int = 0
    stability: float = 100
    max_stability: float = 100
    special_true_damage: bool = False
    special_ignores_defense: bool = False
    move_costs: Optional[dict] = None
    hp_pretyped: bool = False
    base_max_hp: Optional[float] = None
    morph_hp_factor: float = 1.0
    morph_stat_factor: float = 1.05
    gimmick_controls: list = field(default_factory=list)
    gimmick_rules: list = field(default_factory=list)
    gimmick_once: set = field(default_factory=set)
    ability_blade: Optional[dict] = None
    combat_round: int = 0
    avatar_card: dict = field(default_factory=dict)
    avatar_bonuses: object = None
    avatar_bonus_state: dict = field(default_factory=dict)
    avatar_rule_state: dict = field(default_factory=dict)
    avatar_rule_stats: dict = field(default_factory=dict)
    avatar_skill_slot: Optional[int] = 0
    avatar_rule_pierce: int = 0
    special_hits: int = 1
    avatar_combat_state: dict = field(default_factory=dict)
    avatar_stat_multipliers: dict = field(default_factory=dict)
    incoming_reduction: float = 0
    incoming_flat_reduction: float = 0
    outgoing_amp: float = 0
    special_outgoing_amp: float = 0
    outgoing_flat: float = 0
    reflected_flat: float = 0
    draciel_state: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.hp_pretyped and not self.stats_pretyped:
            factor = passive_stat_multiplier(self.bey_type, "hp")
            self.hp = hp_damage(self.hp * factor)
            self.max_hp = hp_damage(self.max_hp * factor)
        self.hp_pretyped = True
        if self.base_max_hp is None:
            self.base_max_hp = self.max_hp

    def sync_morph_hp(self):
        expected = hp_damage(self.base_max_hp * self.morph_hp_factor)
        if self.max_hp != expected:
            self.base_max_hp = self.max_hp / self.morph_hp_factor
        self.morph_hp_factor = self.morph_stat_factor if self.morph_rounds else 1.0
        self.max_hp = hp_damage(self.base_max_hp * self.morph_hp_factor)
        self.hp = min(self.hp, self.max_hp)

    def cost_for(self, move):
        from ..draciel import cost_surcharge
        if self.move_costs is not None:
            base = self.move_costs[move]
        else:
            from ..combat_rules import type_stamina_cost
            base = type_stamina_cost(self.bey_type, STAMINA_COST[move])
        return base + cost_surcharge(self.draciel_state, self.draciel_round, move)

    @property
    def draciel_round(self):
        return self.draciel_state.get('clock', self.combat_round + 1)

    @property
    def eff_stamina(self):
        return (self.stamina_stat + self.avatar_rule_stats.get('stamina', 0)) * self.stat_factor("stamina")

    def stat_factor(self, stat):
        from ..draciel import stat_multiplier
        passive = 1 if self.stats_pretyped else passive_stat_multiplier(self.bey_type, stat)
        return (passive * (self.morph_stat_factor if self.morph_rounds > 0 else 1)
                * self.avatar_stat_multipliers.get(stat, 1)
                * stat_multiplier(self.ability_blade, self.draciel_state, self.draciel_round, stat))

    def alive(self) -> bool:
        return self.hp > 0

    def can(self, move: str) -> bool:
        states=self.avatar_combat_state.get('states',{})
        st=states.get('b')
        if st and move == MOVE_DEFENSE:
            blocked=st.effects.get('no_defense',{})
            if blocked.get('from',0) <= self.combat_round+1 <= blocked.get('until',0):
                return False
        if move == MOVE_SPECIAL and self.gauge < SPECIAL_GAUGE_MAX:
            return False
        if move == MOVE_SPECIAL and self.draciel_state.get('special_ready', 0) > self.draciel_round:
            return False
        return self.sp >= self.cost_for(move)

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
                       state=self.state.copy() if self.state else None,
                       special_damage=self.special_damage,
                       bey_type=self.bey_type, level=self.level,
                       move_power=self.move_power, stats_pretyped=self.stats_pretyped,
                       morph_rounds=self.morph_rounds, stability=self.stability,
                       max_stability=self.max_stability,
                       special_true_damage=self.special_true_damage,
                       special_ignores_defense=self.special_ignores_defense,
                       move_costs=dict(self.move_costs) if self.move_costs else None,
                       hp_pretyped=True, base_max_hp=self.base_max_hp,
                       morph_hp_factor=self.morph_hp_factor,
                       morph_stat_factor=self.morph_stat_factor,
                       gimmick_controls=copy.deepcopy(self.gimmick_controls),
                       gimmick_rules=copy.deepcopy(self.gimmick_rules),
                       gimmick_once=set(self.gimmick_once),
                       ability_blade=copy.deepcopy(self.ability_blade),
                       combat_round=self.combat_round,
                       avatar_card=copy.deepcopy(self.avatar_card),
                       avatar_bonuses=copy.deepcopy(self.avatar_bonuses),
                       avatar_bonus_state=copy.deepcopy(self.avatar_bonus_state),
                       avatar_rule_state=copy.deepcopy(self.avatar_rule_state),
                       avatar_rule_stats=dict(self.avatar_rule_stats),
                       avatar_skill_slot=self.avatar_skill_slot,
                       avatar_rule_pierce=self.avatar_rule_pierce,
                       special_hits=self.special_hits,
                       avatar_combat_state=copy.deepcopy(self.avatar_combat_state),
                       avatar_stat_multipliers=dict(self.avatar_stat_multipliers),
                       incoming_reduction=self.incoming_reduction,
                       incoming_flat_reduction=self.incoming_flat_reduction,
                       outgoing_amp=self.outgoing_amp,
                       special_outgoing_amp=self.special_outgoing_amp,
                       outgoing_flat=self.outgoing_flat,
                       reflected_flat=self.reflected_flat,
                       draciel_state=copy.deepcopy(self.draciel_state))

    # ── Stance-adjusted stats (plain fighters are unaffected) ────────────────
    @property
    def eff_attack(self) -> float:
        base = self.attack + self.avatar_rule_stats.get('attack', 0)
        if not self.state:
            return base * self.stat_factor("attack")
        return (base + self.state.attack_bonus()) * self.state.stat_multiplier() * self.stat_factor("attack")

    @property
    def eff_defense(self) -> float:
        base = self.defense + self.avatar_rule_stats.get('defense', 0)
        if not self.state:
            return base * self.stat_factor("defense")
        return (base + self.state.defense_bonus()) * self.state.stat_multiplier() * self.stat_factor("defense")


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
    return 1.0  # Type identity modifies live stats instead of outgoing damage.


# How much of a PLAYER's Special reaches a boss.
#
# The Special path is `attack x DMG_SCALE x SPECIAL_MULT x special_mult`, and
# `special_mult` grows with the wielder's levelled `special` stat — so the best
# blades in the game were arriving with enough on one move to end a boss
# outright. A boss that dies to a single button is not a boss.
#
# Applied to the player's branch only. The boss's own Special uses
# `special_atk_pct` and is untouched, and ordinary attacks are untouched — the
# cut is aimed at the one thing that was one-shotting, so pressure and attrition
# still work exactly as before.
PLAYER_SPECIAL_VS_BOSS = 0.20

# Crit damage, as a multiplier on the hit that crits.
#
# Ability text across the roster talks about "crit chance", and until now the
# engine had no crit of any kind — `crit` did not appear once in this file, so
# every point of it was decoration. Argus advertises +60% crit at full sight
# and got nothing for it.
#
# It is folded in as EXPECTED VALUE, not rolled: `resolve` is the function the
# 2-ply search evaluates, and its docstring is explicit that randomness lives in
# move selection so the search stays honest. A rolled crit would make the AI
# plan against a fight that doesn't happen and turn the whole battle swingy.
# A 40% chance at +CRIT_DAMAGE_BONUS is therefore applied as a flat
# 1 + 0.40 * CRIT_DAMAGE_BONUS on every hit — same long-run damage, no dice.
CRIT_DAMAGE_BONUS = 0.55


def _raw_damage(src: Fighter, special: bool = False,
                vs_boss: bool = False, dst: Optional[Fighter] = None,
                logs: Optional[list] = None, simulate=False) -> float:
    base = src.eff_attack * DMG_SCALE * src.dmg_mult
    if not special:
        pierce = src.state.pierce() if src.state else 0
        defense = dst.eff_defense * (1 - pierce) if dst else 50
        if src.avatar_rule_pierce > 0:
            defense = 0
        av = src.avatar_bonuses
        if av is not None and av.defence_break_rounds > src.combat_round:
            fraction = ((av.defence_break_min + av.defence_break_max) / 2
                        if simulate else av.roll_defence_break(src.combat_round + 1))
            defense *= max(0, 1 - fraction)
            if fraction > 0 and logs is not None:
                logs.append(f'🗡️ **Avatar** — Guard Breaker shreds {fraction:.0%} DEF!')
        return base_damage(src.level, src.move_power, src.eff_attack, defense) * src.dmg_mult
    if src.special_damage is not None:
        out = src.special_damage * src.dmg_mult
    # Bosses replace the formula outright with a flat percentage of attack.
    elif src.special_atk_pct is not None:
        out = src.eff_attack * src.special_atk_pct * src.dmg_mult
    # special_mult is the wielder's `special` stat relative to its printed
    # value, so a levelled bey's Special grows. Applied on this branch only —
    # ordinary attacks must not inherit it.
    else:
        out = base * SPECIAL_MULT * max(1.0, src.special_mult)
    if src.avatar_bonuses is not None and out > 0:
        from types import SimpleNamespace
        from ..avatar_combat import apply_ult_bonus, multi_hit_shape
        context = SimpleNamespace(avatar_bonuses={'src': src.avatar_bonuses})
        native_hits = max(1, src.special_hits)
        hits, per_hit, hit_logs = multi_hit_shape(context, 'src', native_hits, out / native_hits)
        out = hits * per_hit
        out, bonus_logs = apply_ult_bonus(
            context,
            'src', out, src.eff_attack)
        if logs is not None:
            logs.extend(hit_logs)
            logs.extend(bonus_logs)
    if vs_boss:
        out *= PLAYER_SPECIAL_VS_BOSS
    return out


def resolve(a: Fighter, b: Fighter, move_a: str, move_b: str, *, simulate=False) -> dict:
    """Apply one exchange. Mutates both fighters. Returns a small report.

    Live avatar effects roll their authored chances. AI search sets simulate
    to use expected bonus values and deterministic rule gates on cloned
    snapshots, leaving the real fighter's resources and skill state untouched.
    """
    log = {"dmg_to_a": 0.0, "dmg_to_b": 0.0, "heal_a": 0.0, "heal_b": 0.0,
           "note_a": "", "note_b": ""}

    engine = TypeGimmickEngine({"a": a.bey_type, "b": b.bey_type},
                                rng=(lambda: .99) if simulate else None)
    for key, fighter in (("a", a), ("b", b)):
        engine.states[key].adaptive_morph_rounds = fighter.morph_rounds
        engine.restore_controls(key, 'b' if key == 'a' else 'a', fighter.gimmick_controls)
    engine.start_round()
    from .gimmick_adapter import prepare
    ability_logs = []
    from .avatar_bonus_adapter import BonusBridge
    bonus_bridge = BonusBridge({'a': a, 'b': b}, {'a': move_a, 'b': move_b},
                               simulate=simulate)
    fire_controls = prepare(engine, a, b, {'a': move_a, 'b': move_b}, ability_logs, simulate=simulate)
    ability_logs.extend(engine.begin_round({"a": move_a, "b": move_b}, started=True))
    log["gimmicks"] = ability_logs
    from .original_generation_adapter import project
    avatar_bridge = project(a,b,engine,{'a':move_a,'b':move_b})
    if avatar_bridge:
        if simulate: avatar_bridge.og.rng=lambda: .99
        avatar_bridge.begin()
        ability_logs.extend(avatar_bridge.logs)
        avatar_bridge.logs.clear()
    from .draciel_adapter import project as project_draciel
    draciel_bridge = project_draciel(a, b, engine, {'a':move_a, 'b':move_b}, avatar_bridge)
    if draciel_bridge:
        draciel_bridge.runtime.begin({'a':move_a, 'b':move_b}, ability_logs)
    for key, fighter, move in (("a", a, move_a), ("b", b, move_b)):
        fighter.morph_rounds = engine.states[key].adaptive_morph_rounds
        fighter.morph_stat_factor = engine.stat_multiplier(key)
        fighter.sync_morph_hp()
        from ..combat_rules import spend_resource
        base_cost = fighter.cost_for(move)
        if avatar_bridge:
            base_cost = avatar_bridge.cost(key,move,base_cost)
        fighter.sp = spend_resource(fighter.sp, base_cost)

    from .avatar_rule_adapter import project as project_avatar_rules
    rule_bridge = project_avatar_rules({'a': a, 'b': b},
                                      {'a': move_a, 'b': move_b}, simulate=simulate)
    if rule_bridge:
        rule_bridge.begin()

    def offence(src, dst, move, other_move, tag):
        if move not in (MOVE_ATTACK, MOVE_SPECIAL):
            return 0.0
        special = move == MOVE_SPECIAL
        if special and (src.ability_blade or {}).get('draciel_kit'):
            return 0.0  # these Specials open a defensive field at round start
        # NOT `dst.is_boss` — Story opponents set that too, and cutting their
        # incoming Specials by 80% turned the Story finale into a guaranteed
        # loss (measured: 0% win rate, every stage unwinnable). The real
        # discriminator is `special_atk_pct`, which boss_battle sets on actual
        # bosses and Story deliberately leaves unset; the comment at that
        # assignment says so in as many words.
        if avatar_bridge:
            other = 'b' if tag == 'a' else 'a'
            avatar_bridge.og.preprocess(tag,other,{'defense':dst.eff_defense})
        dmg = _raw_damage(src, special,
                          vs_boss=getattr(dst, "special_atk_pct", None) is not None,
                          dst=dst, logs=ability_logs, simulate=simulate)
        dmg = bonus_bridge.offensive(tag, move, dmg,
                                     engine.critical_applies(tag, move))
        if rule_bridge:
            dmg = rule_bridge.offensive(tag, move, dmg)

        if avatar_bridge and not special:
            pierce=avatar_bridge.og.pierce(tag)
            if pierce: dmg /= max(.01,1-pierce)
        if special and src.special_true_damage:
            pass
        elif other_move == MOVE_DEFENSE:
            soak = DEFENSE_SOAK * (1 + dst.eff_defense / 200.0)
            if (special and src.special_ignores_defense) or src.avatar_rule_pierce > 0:
                soak = 0
            if special:
                soak *= (1 - SPECIAL_PIERCE)
            guarded = dmg * max(0.15, 1 - soak)
            if avatar_bridge:
                other = 'b' if tag == 'a' else 'a'
                saved = max(0,dmg-guarded)
                if avatar_bridge.og.effect(tag,'feint'):
                    guarded += saved*.30
                    saved *= .70
                avatar_bridge.og.guard_saved[other] = saved
            dmg = guarded
            log[f"note_{tag}"] = "blocked"
        elif other_move == MOVE_STAMINA:
            dmg *= ATTACK_VS_STAMINA_MULT
            log[f"note_{tag}"] = "punished a heal"
        elif other_move in (MOVE_ATTACK, MOVE_SPECIAL):
            dmg *= (1 - CLASH_SOAK)
            log[f"note_{tag}"] = "clash"
        elif other_move == MOVE_CHARGE:
            log[f"note_{tag}"] = "caught mid-charge"

        # Crit. Applied after mitigation so it is a clean multiplier on what
        # actually lands, and read from the state so a boss without a crit
        # ability is bit-for-bit unchanged (BaseBossState.crit_mult returns 1.0).
        if src.state is not None:
            crit = getattr(src.state, "crit_mult", None)
            if crit is not None:
                dmg *= max(1.0, float(crit()))
        other = "b" if tag == "a" else "a"
        if dmg > 0:
            dmg = dmg * max(0, 1 + src.outgoing_amp + (src.special_outgoing_amp if special else 0)) + max(0, src.outgoing_flat)
        if fire_controls:
            from ..damage_rules import calc_damage
            matchup = 'win' if special else calc_damage(move, {}, {}, {}, other_move)[2]
            result = {'lose': 'loss', 'lose_grind': 'loss'}.get(matchup, matchup)
            fire_controls(tag, 'on_' + move + '_' + result, matchup=matchup)
            fire_controls(tag, 'on_any_' + result, matchup=matchup)
            fire_controls(tag, 'on_special' if special else 'on_attack_hit', matchup=matchup)
        if avatar_bridge:
            dmg = avatar_bridge.offensive(tag,other,move,dmg)
            dmg = avatar_bridge.defensive(tag,other,move,dmg)
        if rule_bridge:
            dmg = rule_bridge.defensive(other, tag, move, dmg)
        dmg = engine.critical(tag, move, dmg, [])
        if draciel_bridge:
            dmg = draciel_bridge.runtime.outgoing(tag, move, dmg)
        if fire_controls and dmg > 0:
            fire_controls(other, 'on_take_damage', move, matchup)
            if other_move == MOVE_DEFENSE:
                fire_controls(other, 'on_defend', move, matchup)
        if not (special and (src.special_true_damage or src.special_ignores_defense)):
            dmg = max(0, dmg * (1 - min(.95, max(0, dst.incoming_reduction)))
                      - max(0, dst.incoming_flat_reduction))
        return max(0, dmg)

    dmg_b = offence(a, b, move_a, move_b, "a")
    dmg_a = offence(b, a, move_b, move_a, "b")
    if engine.nullifies('a', 'b', move_a, true_damage=move_a == MOVE_SPECIAL and a.special_true_damage):
        dmg_b = 0
    if engine.nullifies('b', 'a', move_b, true_damage=move_b == MOVE_SPECIAL and b.special_true_damage):
        dmg_a = 0

    def barrier(src_key, dst, damage):
        split = bool(avatar_bridge and src_key in avatar_bridge.split_keys)
        parts = [damage]
        if split:
            whole = hp_damage(damage)
            quotient,remainder=divmod(whole,3)
            parts=[quotient+(i < remainder) for i in range(3)]
        resolved=[]
        shattered=False
        for part in parts:
            if dst.state is not None and hasattr(dst.state, 'absorb') and part > 0:
                part,broke=dst.state.absorb(part)
                shattered |= bool(broke)
            resolved.append(part)
        if split and all(part > 0 for part in resolved):
            actor=a if src_key=='a' else b
            actor.sp=min(actor.sp_max,actor.sp+.4)
            ability_logs.append('🐯 **Tiger Claw** — 3 strikes connect; +0.4 stamina!')
        return sum(resolved),shattered

    # Crystal layers eat a slice of one incoming hit, then shatter.
    if b.state is not None and hasattr(b.state, "absorb") and dmg_b > 0 and not (move_a == MOVE_SPECIAL and a.special_true_damage):
        dmg_b, shattered = barrier("a",b,dmg_b)
        if shattered:
            log["note_b"] = "shard shattered"
    if a.state is not None and hasattr(a.state, "absorb") and dmg_a > 0 and not (move_b == MOVE_SPECIAL and b.special_true_damage):
        dmg_a, shattered = barrier("b",a,dmg_a)
        if shattered:
            log["note_a"] = "shard shattered"

    if avatar_bridge:
        if 'a' in avatar_bridge.split_keys and not (b.state and hasattr(b.state,'absorb')):
            dmg_b,_=barrier('a',b,dmg_b)
        if 'b' in avatar_bridge.split_keys and not (a.state and hasattr(a.state,'absorb')):
            dmg_a,_=barrier('b',a,dmg_a)

    # Defensive effects precede the shared type reduction/counter stage.
    dmg_b = engine.mitigate('a', 'b', move_a, dmg_b,
                           true_damage=move_a == MOVE_SPECIAL and a.special_true_damage,
                           bypass_reduction=a.avatar_rule_pierce > 0 or
                           move_a == MOVE_SPECIAL and a.special_ignores_defense)
    dmg_a = engine.mitigate('b', 'a', move_b, dmg_a,
                           true_damage=move_b == MOVE_SPECIAL and b.special_true_damage,
                           bypass_reduction=b.avatar_rule_pierce > 0 or
                           move_b == MOVE_SPECIAL and b.special_ignores_defense)
    if draciel_bridge:
        dmg_b = draciel_bridge.runtime.mitigate('a', 'b', move_a, dmg_b, ability_logs,
            true_damage=move_a == MOVE_SPECIAL and a.special_true_damage)
        dmg_a = draciel_bridge.runtime.mitigate('b', 'a', move_b, dmg_a, ability_logs,
            true_damage=move_b == MOVE_SPECIAL and b.special_true_damage)

    # Riposte: a successful block punishes the attacker.
    if move_a == MOVE_DEFENSE and move_b in (MOVE_ATTACK, MOVE_SPECIAL) and not engine.returns_damage("b", "a", move_b):
        # dmg_mult belongs here too. The type bonus was only reaching attacks
        # and Specials via _raw_damage, so an Attack blade's riposte — a real
        # damage source at 0.55 of its attack stat — was still unbuffed and the
        # blade got noticeably less than the advertised 40% overall.
        dmg_b += a.eff_attack * DMG_SCALE * RIPOSTE_RATIO * a.dmg_mult
        log["note_a"] = "riposte"
    if move_b == MOVE_DEFENSE and move_a in (MOVE_ATTACK, MOVE_SPECIAL) and not engine.returns_damage("a", "b", move_a):
        dmg_a += b.eff_attack * DMG_SCALE * RIPOSTE_RATIO * b.dmg_mult
        log["note_b"] = "riposte"

    # Judgement stance reflects a slice of whatever connected.
    if a.state and a.state.reflect() and move_b in (MOVE_ATTACK, MOVE_SPECIAL) and not engine.returns_damage("b", "a", move_b):
        dmg_b += a.state.reflect()
        log["note_a"] = "reflected"
    if b.state and b.state.reflect() and move_a in (MOVE_ATTACK, MOVE_SPECIAL) and not engine.returns_damage("a", "b", move_a):
        dmg_a += b.state.reflect()
        log["note_b"] = "reflected"

    def healing(src, own_move, other_move):
        if own_move != MOVE_STAMINA or not src.alive():
            return 0.0
        key = "a" if src is a else "b"
        heal, stability, recovery = engine.recovery(key, src.eff_stamina)
        from ..combat_rules import recover_resource
        src.stability = recover_resource(src.stability, src.max_stability, stability)
        if recovery is not None:
            src.sp = recover_resource(src.sp, src.sp_max, recovery)
        if avatar_bridge:
            from ..purification import heal_amount
            heal=heal_amount(avatar_bridge.og.s,key,heal)
        return min(max(0, src.max_hp - src.hp), hp_damage(heal))

    # Final HP rounding and capped actual damage, before terminal counter.
    dmg_a, counter_b = bonus_bridge.incoming('a', 'b', dmg_a)
    dmg_b, counter_a = bonus_bridge.incoming('b', 'a', dmg_b)
    # Mirror the PvP avatar layer: counter riders precede lethal protection.
    from ..combat_rules import damage_hp
    if counter_a and not engine.returns_damage('b', 'a', move_b):
        a.hp, _ = damage_hp(a.hp, counter_a)
    if counter_b and not engine.returns_damage('a', 'b', move_a):
        b.hp, _ = damage_hp(b.hp, counter_b)
    dmg_a = bonus_bridge.lethal('a', dmg_a)
    dmg_b = bonus_bridge.lethal('b', dmg_b)
    dmg_a, dmg_b = hp_damage(dmg_a), hp_damage(dmg_b)
    from ..combat_rules import damage_hp, recover_hp, recover_resource
    a.hp, actual_a = damage_hp(a.hp, dmg_a)
    b.hp, actual_b = damage_hp(b.hp, dmg_b)
    if engine.returns_damage("a", "b", move_a):
        a.hp, _ = damage_hp(a.hp, actual_b, already_final=True)
        log["kinetic_return_a"] = actual_b
    if engine.returns_damage("b", "a", move_b):
        b.hp, _ = damage_hp(b.hp, actual_a, already_final=True)
        log["kinetic_return_b"] = actual_a
    if actual_a > 0 and a.reflected_flat and not engine.returns_damage('b', 'a', move_b):
        b.hp, reflected = damage_hp(b.hp, a.reflected_flat)
        log['ability_reflect_a'] = reflected
    if actual_b > 0 and b.reflected_flat and not engine.returns_damage('a', 'b', move_a):
        a.hp, reflected = damage_hp(a.hp, b.reflected_flat)
        log['ability_reflect_b'] = reflected
    # Recovery uses HP after direct and returned damage, just like live PvP.
    # A fighter knocked out by this exchange cannot recover resources.
    heal_a = healing(a, move_a, move_b)
    heal_b = healing(b, move_b, move_a)
    a.hp, heal_a = recover_hp(a.hp, a.max_hp, heal_a)
    b.hp, heal_b = recover_hp(b.hp, b.max_hp, heal_b)
    a.healed_total += heal_a
    b.healed_total += heal_b

    # Immortality, applied AFTER hp is written rather than before the damage is
    # computed. A state that claims to be unkillable has to survive damage that
    # never asked `absorb()` — true damage, a drain, anything that reaches hp by
    # another road. Optional hook: a state without `guard_hp` is untouched.
    for f in (a, b):
        guard = getattr(f.state, "guard_hp", None) if f.state else None
        if guard is not None:
            try:
                f.hp = float(guard(f.hp))
            except Exception:                            # noqa: BLE001
                pass

    for f, mv, took in ((a, move_a, dmg_a), (b, move_b, dmg_b)):
        gmult = f.state.gauge_multiplier() if f.state else 1.0
        frozen = bool(f.state is None and (
            (a.state and a is not f and a.state.freeze_turns > 0) or
            (b.state and b is not f and b.state.freeze_turns > 0)))

        if mv == MOVE_SPECIAL:
            f.gauge = 0.0
        elif not frozen:
            gain = GAUGE_PER[mv] * gmult
            key = 'a' if f is a else 'b'
            if mv == MOVE_CHARGE:
                gain = bonus_bridge.charge(key, gain)
            f.gauge = min(SPECIAL_GAUGE_MAX, f.gauge + gain)
        if took > 0 and not frozen:
            f.gauge = min(SPECIAL_GAUGE_MAX, f.gauge + GAUGE_PER_DMG_TAKEN * gmult)
        key = 'a' if f is a else 'b'
        dealt = dmg_b if key == 'a' else dmg_a
        if bonus_bridge.crit_refunds[key] and dealt > 0 and not frozen:
            f.gauge = min(SPECIAL_GAUGE_MAX, f.gauge + bonus_bridge.crit_refunds[key])

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
        key = "a" if f is a else "b"
        if mv == MOVE_STAMINA:
            if f.alive() and not engine.states[key].overdrive_active:
                f.sp = recover_resource(f.sp, f.sp_max, 2.5, preserve_reserve=True)
            f.heal_streak += 1
        else:
            f.sp = recover_resource(f.sp, f.sp_max, .5, preserve_reserve=True)
            if mv in (MOVE_ATTACK, MOVE_SPECIAL):
                # Walk the streak back rather than clearing it — see
                # HEAL_STREAK_RECOVERY.
                f.heal_streak = max(0, f.heal_streak - HEAL_STREAK_RECOVERY)

    # Twin Crowns flips after the exchange; Ascension checks the new HP.
    bonus_bridge.finish()
    ability_logs.extend(bonus_bridge.logs)
    if rule_bridge:
        rule_bridge.end()
        ability_logs.extend(rule_bridge.logs)
    if avatar_bridge:
        avatar_bridge.end({'a':move_a,'b':move_b},actual_a,actual_b)
        ability_logs.extend(avatar_bridge.logs)
    if draciel_bridge:
        draciel_bridge.end({'a':move_a,'b':move_b},actual_a,actual_b,ability_logs)

    for f in (a, b):
        if not f.state:
            continue
        f.state.tick()
        f.state.flip_stance()
        if f.alive() and f.state.should_ascend(f.hp / f.max_hp):
            f.state.ascend()
            log["ascended"] = f.name

    engine.end_round()
    for key, fighter in (("a", a), ("b", b)):
        fighter.gimmick_controls = engine.export_controls(key)
        fighter.combat_round += 1
    log['mitigation_applied'] = True
    a.morph_rounds = engine.states["a"].adaptive_morph_rounds
    b.morph_rounds = engine.states["b"].adaptive_morph_rounds
    a.morph_stat_factor = engine.stat_multiplier('a')
    b.morph_stat_factor = engine.stat_multiplier('b')
    a.sync_morph_hp()
    b.sync_morph_hp()

    log.update(dmg_to_a=dmg_a, dmg_to_b=dmg_b, heal_a=heal_a, heal_b=heal_b)
    return log


# ── Opponent model ────────────────────────────────────────────────────────────

# How many recent moves a full-`read` boss effectively weighs. Ten is about
# where the exponential decay (0.82) has faded a move to under 15% of a fresh
# one, so remembering further back buys almost nothing — it is the natural
# length of this model rather than a round number.
MEMORY_FULL = 10


@dataclass
class OpponentModel:
    """Exponentially-weighted move frequencies, so recent habits dominate."""

    decay: float = 0.82
    counts: dict = field(default_factory=lambda: {m: 1.0 for m in ALL_MOVES})
    history: list = field(default_factory=list)
    # How much of what it has learned actually reaches the prediction, 0-1.
    # Set from DIFFICULTY["read"], and the single knob that makes a higher tier
    # mean "it reads you better". At 0.0 the model still OBSERVES — it just
    # cannot use any of it, so the boss is guessing from priors alone.
    read: float = 1.0

    def observe(self, move: str) -> None:
        for m in self.counts:
            self.counts[m] *= self.decay
        self.counts[move] = self.counts.get(move, 0.0) + 1.0
        self.history.append(move)

    def predict(self, opponent: Fighter) -> dict[str, float]:
        legal = set(opponent.legal_moves())
        weights = {m: max(0.01, self.counts.get(m, 0.0)) for m in legal}

        # `read` is IQ, and it works by shortening the boss's MEMORY.
        #
        # The first version blended the learned weights toward uniform, which
        # was wrong in a way worth recording: blending scales every weight
        # toward the same mean, so it preserves their ORDER and the top pick
        # never moves. Measured against a player who attacked 75% of the time,
        # IQ 2, 3, 4 and 5 all guessed right 71.7% of the time — an IQ ladder
        # with no rungs in it.
        #
        # A short memory does change the answer: a boss that has seen your last
        # two moves genuinely predicts worse than one that has seen your last
        # ten, and it degrades smoothly rather than switching off.
        # ONE estimator at every rung, with only the window length changing.
        # The first attempt kept `counts` for read >= 1.0 and used the window
        # only below it — and that inverted the top of the ladder, because
        # `counts` seeds every move at 1.0 and carries that stale uniform prior
        # forever. Measured at equal stats against a player who attacked 80% of
        # the time, the boss won 68.3% at IQ 4 and only 59.4% at IQ 5: the
        # most expensive tier read you WORSE than the one below it.
        r = max(0.0, min(1.0, float(self.read)))
        window = int(round(r * MEMORY_FULL))
        if window <= 0:
            # Reads nothing at all. Flat across every legal move, so the boss
            # is left with the priors below and nothing else — near-chance,
            # which is what tier 1 is meant to be. NOT anti-correlated: an
            # earlier version left a rookie boss guessing the same wrong move
            # every turn, which is worse than random rather than weaker.
            weights = {m: 1.0 for m in legal}
        else:
            recent = self.history[-window:]
            weights = {m: 0.01 for m in legal}
            for i, mv in enumerate(recent):
                if mv in weights:
                    # The same exponential recency the model records with, so a
                    # short window is a smaller version of the same reader
                    # rather than a different one.
                    weights[mv] += self.decay ** (len(recent) - 1 - i)

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
            resolve(b, f, my_move, their_move, simulate=True)

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
            resolve(b, f, my_move, their_move, simulate=True)
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
# `iq` and `read` were added in v1.11 so that difficulty means "the boss reads
# you better" rather than only "the boss has a bigger bar" — a damage sponge is
# a worse fight than an opponent that punishes your habits, and a sponge can be
# out-levelled by stats alone while a reader cannot.
#
#   iq    1-5, display only. The number the player is sold when they pay for a
#         harder tier, and the honest summary of the three levers below.
#   read  how much of the learned move distribution survives. 0.0 blends the
#         opponent model all the way to uniform — the boss genuinely knows
#         nothing about your habits — and 1.0 uses it whole. This is the lever
#         the spec calls "how often the boss's prediction is right", and it is
#         separate from `model` (which switched reading on or off outright and
#         gave nothing in between).
DIFFICULTY = {
    "rookie":   {"iq": 1, "depth": 1, "model": False, "blunder": 0.45,
                 "mix": 0.60, "read": 0.00},
    "veteran":  {"iq": 2, "depth": 1, "model": True,  "blunder": 0.25,
                 "mix": 0.40, "read": 0.35},
    "elite":    {"iq": 3, "depth": 2, "model": True,  "blunder": 0.12,
                 "mix": 0.30, "read": 0.60},
    "legend":   {"iq": 4, "depth": 2, "model": True,  "blunder": 0.04,
                 "mix": 0.20, "read": 0.85},
    "nightmare":{"iq": 5, "depth": 2, "model": True,  "blunder": 0.00,
                 "mix": 0.15, "read": 1.00},
}

IQ_LABELS = {
    1: "Sloppy",
    2: "Attentive",
    3: "Sharp",
    4: "Calculating",
    5: "Merciless",
}


def iq_for(difficulty: Optional[str]) -> int:
    """The boss's IQ on this rung, 1-5. Unknown rungs read as mid."""
    cfg = DIFFICULTY.get(difficulty or "")
    return int(cfg["iq"]) if cfg else 3


def iq_label(difficulty: Optional[str]) -> str:
    """`IQ 4 · Calculating` — what the player is actually buying."""
    iq = iq_for(difficulty)
    return f"IQ {iq} · {IQ_LABELS.get(iq, '?')}"


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
        else:
            # Hand the rung's read accuracy to the model for this decision
            # only. Copied rather than mutated in place: the same model object
            # is shared with the caller and with the lookahead's clones, and a
            # boss whose read strength changed permanently because one function
            # set an attribute would be a very hard bug to see.
            model = replace(model, read=float(cfg.get("read", 1.0)))
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
        resolve(b, f, MOVE_SPECIAL, MOVE_DEFENSE, simulate=True)   # even through a block
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
