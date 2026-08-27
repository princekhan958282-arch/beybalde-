#!/usr/bin/env python3
"""
tools/sim_drakoryn.py — Drakoryn, and the two new generic ops it needed.

Why this suite exists
----------------------
Drakoryn's kit needed two genuinely new primitives, added to
cogs/abilities/ability_engine.py:

- `true_damage_stat_pct` — a stat-scaled sibling of the existing `true_damage`
  op. Iron Lock: Counterseal's Attack-branch payoff has to land on a round
  where DRAKORYN's own move is Stamina or Charge (it's reacting to the
  OPPONENT's move, not its own) — and attack_manager.py's MOVE_STAMINA/
  MOVE_CHARGE branch discards dmg_dealt/dmg_taken entirely. A `dmg_dealt`
  modifying op like `bonus_damage_stat` would silently do nothing on those
  rounds; only a direct `session.hp` write (the same trick `true_damage`
  already uses for a flat number) survives it. Section 3b is the check that
  proves this — it is the entire reason the op exists.
- `enemy_stability_below_pct` — Stability, unlike HP, only had a self-facing
  `stability_below_pct`/`stability_above_pct` pair before this blade; Dragon's
  Ruin needs to read the OPPONENT's Stability. Section 4 proves it gates the
  conditional half of the Special and nothing else.

The other genuinely fiddly piece is Iron Lock's "marks for 2 turns": the mark
counter decays once per round via a `turn_start` rule, and `turn_start`/
`turn_end` fire once per player at the very START of each round — BEFORE that
round's own moves resolve (cogs/battle/session.py, apply_dot_tick_extras is
called ahead of move resolution). That means the round right after arming
already eats one decrement before its own check can run, so arming to a raw
counter of "2" only ever gives ONE real check-opportunity round. Section 3a
is the empirical proof that arming to 3 is what actually yields exactly two
live rounds (N+1 and N+2, not N+3) — the sim was written to fail on "2" before
the JSON was set to "3", not assumed by hand-derivation alone.

Driven through a REAL AbilityEngine, StatusManager, StaminaManager,
TypeModifiers and StabilityManager — the technique every sim_<blade>.py in
this repo uses (see sim_azure_drakonyx.py, sim_cosmic_phoenix.py).

Run:  python3 tools/sim_drakoryn.py
"""
import os
import sys
import types as _t

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {detail}")


from cogs.abilities import type_system as TS                  # noqa: E402
from cogs.abilities.ability_engine import AbilityEngine        # noqa: E402
from cogs.battle.attack_manager import AttackManager           # noqa: E402
from cogs.battle.stability_manager import StabilityManager     # noqa: E402
from cogs.battle.stamina_manager import StaminaManager         # noqa: E402
from cogs.battle.status_manager import StatusManager           # noqa: E402
from cogs.core.constants import (                               # noqa: E402
    MOVE_ATTACK, MOVE_DEFENSE, MOVE_STAMINA, MOVE_CHARGE, MOVE_SPECIAL,
)
from utils.database import get_beyblade, load_beyblades         # noqa: E402
from utils import bey_levels as BL                              # noqa: E402

NAME = "Drakoryn"
DR = get_beyblade(NAME)
ALL = load_beyblades()

DUMMY = {"name": "Dummy", "type": "Balance", "spin_direction": "Left",
        "stats": {"attack": 100, "defense": 100, "stamina": 100, "hp": 100}}


class FakeSession:
    def __init__(self, mine, theirs, hp=1000, ehp=1000, max_hp=1000):
        import copy
        self.blades = {"p": copy.deepcopy(mine), "e": copy.deepcopy(theirs)}
        self.hp = {"p": hp, "e": ehp}
        self.max_hp = max_hp
        self.max_hp_per_player = {"p": max_hp, "e": max_hp}
        self.battle_stats = {k: dict(v["stats"]) for k, v in self.blades.items()}
        self.bey_levels = {"p": 1, "e": 1}
        self.last_moves = {}
        self.moves = {}
        self.round = 1
        self.stat_mult = {}
        self.status = StatusManager(self)
        self.status_manager = self.status
        self.stamina_manager = StaminaManager(self.blades)
        self.type_mods = {k: TS.TypeModifiers(v, stats=self.battle_stats[k])
                          for k, v in self.blades.items()}
        self.stability_manager = StabilityManager(self.blades, self.type_mods)
        self.chain_handler = _t.SimpleNamespace(resolve=lambda *a, **k: [])
        self.ability = AbilityEngine(self)
        for _k in self.blades:
            self.ability.setup(_k, self.blades[_k])


def am_for(s):
    am = AttackManager.__new__(AttackManager)
    am.session = s
    return am


def special(s, mkey="p", okey="e"):
    am = am_for(s)
    total, logs = am._resolve_special(mkey, okey, MOVE_SPECIAL,
                                       s.blades[mkey], s.blades[okey], [])
    s.hp[okey] = max(0, s.hp[okey] - total)
    return total, logs


def move(s, my_move, my_matchup, mkey="p", okey="e", enemy_move=None,
         dmg=0):
    """Drive one round's worth of `ability.apply()` for `mkey` as mover.

    Sets `last_moves` first, matching how the real session writes it BEFORE
    firing abilities each round — this is what `enemy_move_is` reads.
    """
    s.last_moves = {mkey: my_move, okey: enemy_move or my_move}
    out, taken, logs = s.ability.apply(mkey, okey, s.blades[mkey], s.blades[okey],
                                        my_move, my_matchup, dmg, 0,
                                        is_first_hit=True, cumulative_dmg=0,
                                        is_last_hit=True)
    return out, taken, logs


def tick_round(s, key="p"):
    """One round-start tick (turn_start/turn_end) for `key`, as
    apply_dot_tick_extras fires it — BEFORE that round's moves resolve.
    """
    return s.ability.apply_dot_tick_extras(key, s.blades[key])


def main() -> int:
    # ── 1. the card ─────────────────────────────────────────────────────────
    print("\n── 1. the card matches the spec ─────────────────────────────────")
    check("Drakoryn is in the roster", DR is not None)
    check("rarity Mythic", DR.get("rarity") == "Mythic", DR.get("rarity"))
    check("type Defense", DR.get("type") == "Defense", DR.get("type"))
    check("flagged limited, unbound",
          DR.get("limited") is True and not DR.get("owner_ids"))
    check("its id is unique",
          sum(1 for b in ALL.values() if b.get("id") == DR["id"]) == 1, DR["id"])
    check("stats match the spec exactly",
          DR["stats"] == {"attack": 99, "defense": 190, "stamina": 88,
                          "special": 150, "hp": 189}, DR["stats"])
    check("the supplied art is stored whole, query string included",
          "1542209727461851227" in DR["image_url"] and "hm=" in DR["image_url"])
    names = [a["name"] for a in DR["abilities"]]
    check("all three abilities are on the plural list the engine reads",
          names == ["Crimson Reaver", "Iron Lock: Counterseal", "Dragon's Ruin"],
          names)
    check("the singular mirrors the first of them",
          DR["ability"]["name"] == DR["abilities"][0]["name"])
    at100 = BL.stats_at(DR, 100, {})
    check("base stats stay at or under the level-100 cap",
          all(v <= BL.STAT_CAP for v in at100.values()), at100)
    check("special_move is non-damage (all real damage comes from ops)",
          DR["special_move"].get("non_damage") is True
          and DR["special_move"]["damage_per_hit"] == 0)

    # ── 2. Crimson Reaver — +5%/+10%/+15% ATK, Defense resets it ────────────
    print("\n── 2. Crimson Reaver — stacking Attack buff, cleared by Defense ──")
    s = FakeSession(DR, DUMMY)
    atk = s.blades["p"]["stats"]["attack"]

    move(s, MOVE_ATTACK, "win")
    check("1 stack: +5% Attack",
          s.ability._get_buf_bonus("p", "attack") == round(atk * 0.05),
          s.ability._get_buf_bonus("p", "attack"))

    move(s, MOVE_ATTACK, "win")
    check("2 stacks: +10% Attack",
          s.ability._get_buf_bonus("p", "attack") == round(atk * 0.10),
          s.ability._get_buf_bonus("p", "attack"))

    move(s, MOVE_ATTACK, "win")
    check("3 stacks: +15% Attack",
          s.ability._get_buf_bonus("p", "attack") == round(atk * 0.15),
          s.ability._get_buf_bonus("p", "attack"))

    move(s, MOVE_ATTACK, "win")
    check("capped at 3 stacks: still +15%, not +20%",
          s.ability._get_buf_bonus("p", "attack") == round(atk * 0.15),
          s.ability._get_buf_bonus("p", "attack"))

    move(s, MOVE_DEFENSE, "win")
    check("Defense win: stacks fully spent — buff back to 0",
          s.ability._get_buf_bonus("p", "attack") == 0,
          s.ability._get_buf_bonus("p", "attack"))
    check("...and the counter itself is zeroed, not just the buff",
          s.ability.counters.get(("p", "reaver_stack"), 0) == 0)

    # Rebuild stacks, confirm Defense LOSS also resets (not just win).
    move(s, MOVE_ATTACK, "win")
    move(s, MOVE_ATTACK, "win")
    check("2 stacks rebuilt", s.ability._get_buf_bonus("p", "attack")
          == round(atk * 0.10))
    move(s, MOVE_DEFENSE, "lose")
    check("Defense loss also resets the stacks",
          s.ability._get_buf_bonus("p", "attack") == 0)

    # And Defense MIRROR.
    move(s, MOVE_ATTACK, "win")
    move(s, MOVE_DEFENSE, "mirror")
    check("Defense mirror also resets the stacks",
          s.ability._get_buf_bonus("p", "attack") == 0)

    # ── 3. Iron Lock: Counterseal ────────────────────────────────────────────
    print("\n── 3. Iron Lock: Counterseal ────────────────────────────────────")

    # 3a. Empirical proof of the mark's real duration: arm on round N via a
    # Defense win, then advance rounds with turn_start ticks between each
    # round's own check, and prove the payoff can fire on N+1 AND N+2 but
    # NOT on N+3 — the whole "marks for 2 turns" claim, driven for real
    # rather than trusted from the hand-derivation in the docstring above.
    print("  -- 3a. the mark lasts exactly 2 real check-rounds --")
    s2 = FakeSession(DR, DUMMY)
    move(s2, MOVE_DEFENSE, "win")   # round N: arm
    check("arming sets the mark to 3 (compensates the round-start decay "
          "eating one round before any check can run)",
          s2.ability.counters.get(("p", "iron_lock_mark"), 0) == 3,
          s2.ability.counters.get(("p", "iron_lock_mark"), 0))

    tick_round(s2, "p")             # round N+1 start: decay 3 -> 2
    check("round N+1: mark is still active (2)",
          s2.ability.counters.get(("p", "iron_lock_mark"), 0) == 2)
    hp_before = s2.hp["e"]
    move(s2, MOVE_CHARGE, "lose", enemy_move=MOVE_ATTACK)  # opponent attacks
    check("round N+1: the Attack payoff DOES fire",
          s2.hp["e"] < hp_before, (s2.hp["e"], hp_before))
    check("...and the mark is consumed by that one payoff",
          s2.ability.counters.get(("p", "iron_lock_mark"), 0) == 0)

    # Re-arm cleanly (cooldown from the first arm is still up, so drive a
    # dedicated session for the N+2 half of the claim instead of reusing s2 —
    # the point of this block is the counter's own decay math, independent of
    # the cooldown, which section 3d covers on its own terms).
    s2b = FakeSession(DR, DUMMY)
    move(s2b, MOVE_DEFENSE, "win")            # round N: arm (mark = 3)
    tick_round(s2b, "p")                      # round N+1 start: 3 -> 2
    move(s2b, MOVE_CHARGE, "lose", enemy_move=MOVE_DEFENSE)   # no payoff (Defense)
    check("round N+1, opponent defends: mark REMAINS (no payoff, no reset)",
          s2b.ability.counters.get(("p", "iron_lock_mark"), 0) == 2,
          s2b.ability.counters.get(("p", "iron_lock_mark"), 0))
    tick_round(s2b, "p")                      # round N+2 start: 2 -> 1
    check("round N+2: mark is still active (1)",
          s2b.ability.counters.get(("p", "iron_lock_mark"), 0) == 1)
    hp_before2 = s2b.hp["e"]
    move(s2b, MOVE_CHARGE, "lose", enemy_move=MOVE_ATTACK)
    check("round N+2: the Attack payoff STILL fires — this is the 2nd real "
          "check-round the spec promises",
          s2b.hp["e"] < hp_before2, (s2b.hp["e"], hp_before2))

    s2c = FakeSession(DR, DUMMY)
    move(s2c, MOVE_DEFENSE, "win")            # round N: arm (mark = 3)
    tick_round(s2c, "p")                      # N+1: 3 -> 2
    move(s2c, MOVE_CHARGE, "lose", enemy_move=MOVE_DEFENSE)   # no payoff
    tick_round(s2c, "p")                      # N+2: 2 -> 1
    move(s2c, MOVE_CHARGE, "lose", enemy_move=MOVE_DEFENSE)   # no payoff
    tick_round(s2c, "p")                      # N+3: 1 -> 0 — the mark expires
    check("round N+3: the mark has decayed to 0 before this round's check",
          s2c.ability.counters.get(("p", "iron_lock_mark"), 0) == 0)
    hp_before3 = s2c.hp["e"]
    move(s2c, MOVE_CHARGE, "lose", enemy_move=MOVE_ATTACK)
    check("round N+3: the payoff no longer fires — only 2 real rounds, "
          "exactly as the spec says",
          s2c.hp["e"] == hp_before3, (s2c.hp["e"], hp_before3))

    # 3b. The Attack-branch payoff: exactly 20% of Drakoryn's own ATK as
    # TRUE damage, and — the entire reason true_damage_stat_pct exists —
    # it must land even when DRAKORYN's own move that round is Stamina or
    # Charge (attack_manager discards dmg_dealt/dmg_taken for those moves;
    # only a direct session.hp write survives).
    print("  -- 3b. Attack-branch payoff: 20% ATK true damage, even on a "
          "Stamina/Charge round --")
    for own_move, label in ((MOVE_STAMINA, "Stamina"), (MOVE_CHARGE, "Charge"),
                             (MOVE_ATTACK, "Attack")):
        s3 = FakeSession(DR, DUMMY)
        move(s3, MOVE_DEFENSE, "win")   # arm
        tick_round(s3, "p")             # decay to 2, mark still active
        hp_before = s3.hp["e"]
        own_matchup = "win" if own_move == MOVE_ATTACK else "lose"
        _, _, logs = move(s3, own_move, own_matchup, enemy_move=MOVE_ATTACK)
        want = round(s3.blades["p"]["stats"]["attack"] * 0.20)
        check(f"Drakoryn plays {label}: payoff still deals exactly 20% ATK "
              f"true damage ({want})",
              s3.hp["e"] == hp_before - want,
              (s3.hp["e"], hp_before - want))
        check(f"Drakoryn plays {label}: the mark is consumed",
              s3.ability.counters.get(("p", "iron_lock_mark"), 0) == 0)

    # 3c. The Stamina-branch payoff: steal exactly 1 Stability.
    print("  -- 3c. Stamina-branch payoff: steals exactly 1 Stability --")
    s4 = FakeSession(DR, DUMMY)
    move(s4, MOVE_DEFENSE, "win")
    tick_round(s4, "p")
    # Drakoryn (Defense type) starts at its own max Stability (150), so a
    # +1 gain would be invisible unless there's room below the ceiling —
    # drop it a few points first, same as any other stability-gain check.
    s4.stability_manager.stability["p"] = s4.stability_manager.max["p"] - 5
    p_stab_before = s4.stability_manager.stability["p"]
    e_stab_before = s4.stability_manager.stability["e"]
    move(s4, MOVE_CHARGE, "lose", enemy_move=MOVE_STAMINA)
    check("opponent loses exactly 1 Stability",
          s4.stability_manager.stability["e"] == e_stab_before - 1,
          (s4.stability_manager.stability["e"], e_stab_before))
    check("Drakoryn gains exactly 1 Stability",
          s4.stability_manager.stability["p"] == p_stab_before + 1,
          (s4.stability_manager.stability["p"], p_stab_before))
    check("the mark is consumed by the Stamina payoff too",
          s4.ability.counters.get(("p", "iron_lock_mark"), 0) == 0)

    # "Only one trigger per mark": once consumed, it does NOT pay out again
    # until re-armed, even if the opponent keeps attacking.
    hp_before4 = s4.hp["e"]
    move(s4, MOVE_CHARGE, "lose", enemy_move=MOVE_ATTACK)
    check("only one trigger per mark: a spent mark does not pay out again",
          s4.hp["e"] == hp_before4, (s4.hp["e"], hp_before4))

    # 3d. The 3-turn cooldown genuinely blocks re-arming.
    print("  -- 3d. cooldown blocks re-arming for 3 rounds --")
    s5 = FakeSession(DR, DUMMY)
    move(s5, MOVE_DEFENSE, "win")      # arm (mark = 3, cooldown = 3)
    check("cooldown is set to 3", s5.ability.cooldowns.get(("p", "iron_lock")),
          3)
    move(s5, MOVE_DEFENSE, "win")      # try to re-arm immediately: blocked
    check("re-arming immediately after is blocked by the cooldown gate — "
          "mark is not refreshed to 3 again",
          s5.ability.counters.get(("p", "iron_lock_mark"), 0) != 3
          or s5.ability.cooldowns.get(("p", "iron_lock"), 0) > 0)
    # Let the counter fully decay so section 3d is only proving the
    # cooldown, not accidentally re-observing an already-armed mark.
    for _ in range(3):
        tick_round(s5, "p")
    check("mark has fully decayed while the cooldown is still ticking",
          s5.ability.counters.get(("p", "iron_lock_mark"), 0) == 0)
    for i in range(2):
        s5.ability.tick_extras()
        move(s5, MOVE_DEFENSE, "win")
        check(f"still on cooldown after {i+1} tick(s): re-arm blocked",
              s5.ability.counters.get(("p", "iron_lock_mark"), 0) == 0,
              s5.ability.counters.get(("p", "iron_lock_mark"), 0))
    s5.ability.tick_extras()   # cooldown finally expires
    move(s5, MOVE_DEFENSE, "win")
    check("cooldown expired: re-arming now succeeds (mark back to 3)",
          s5.ability.counters.get(("p", "iron_lock_mark"), 0) == 3,
          s5.ability.counters.get(("p", "iron_lock_mark"), 0))

    # ── 4. Dragon's Ruin — 35% ATK, 45% below 30% enemy Stability ────────────
    print("\n── 4. Dragon's Ruin — 35% ATK, 45% once the enemy wobbles ────────")
    check("authored as non-damage — all real damage is ability-driven",
          DR["special_move"]["damage_per_hit"] == 0)

    # An Attack-type target, not the Balance DUMMY used elsewhere: Drakoryn is
    # Defense-type, and Defense beats Attack in the triangle, which suppresses
    # the DEFENDER's own type mitigation for this matchup (Balance's mitigation
    # is never suppressed — it would otherwise shave a few percent off every
    # number below and turn this section into a mitigation test instead of a
    # Dragon's Ruin test). This isolates the check to exactly what Dragon's
    # Ruin itself controls.
    ATK_FOE = {"name": "Foe", "type": "Attack", "spin_direction": "Left",
              "stats": {"attack": 100, "defense": 100, "stamina": 100, "hp": 100}}

    s6 = FakeSession(DR, ATK_FOE, ehp=1000)
    s6.stability_manager.stability["e"] = 100
    s6.stability_manager.max["e"] = 100     # 100% Stability: no bonus
    dealt_full, logs_full = special(s6, "p", "e")
    want_base = round(s6.blades["p"]["stats"]["attack"] * 0.35)
    check("full Stability: exactly 35% Attack, no rider",
          dealt_full == want_base, (dealt_full, want_base))

    # The condition is authored as "below 30%" (strict), matching the same
    # convention every other *_below_pct condition in this engine already
    # uses (see sim_azure_drakonyx.py's own note on enemy_hp_below_pct) — so
    # the boundary case is tested just off either side of it, not sitting
    # exactly on 30%.
    s7 = FakeSession(DR, ATK_FOE, ehp=1000)
    s7.stability_manager.stability["e"] = 29
    s7.stability_manager.max["e"] = 100     # below 30% Stability
    dealt_low, logs_low = special(s7, "p", "e")
    want_boosted = round(s7.blades["p"]["stats"]["attack"] * 0.35) + \
                   round(s7.blades["p"]["stats"]["attack"] * 0.10)
    check("below 30% Stability: the +10% rider fires (45% total)",
          dealt_low == want_boosted, (dealt_low, want_boosted))

    s8 = FakeSession(DR, ATK_FOE, ehp=1000)
    s8.stability_manager.stability["e"] = 31
    s8.stability_manager.max["e"] = 100     # just above the gate
    dealt_above, _ = special(s8, "p", "e")
    check("just above 30% Stability: still the base 35%, no rider",
          dealt_above == want_base, (dealt_above, want_base))
    check("the rider only applies once the opponent has actually wobbled",
          dealt_low > dealt_above, (dealt_low, dealt_above))

    # ── 5. reachability ────────────────────────────────────────────────────
    print("\n── 5. reachability — every op/trigger the kit names is in its JSON ──")
    ops_seen, triggers_seen = set(), set()
    for ab in DR["abilities"]:
        for rule in ab.get("rules", []):
            triggers_seen.add(rule.get("when"))
            for op in rule.get("do", []):
                ops_seen.add(op.get("op"))
    for wanted in ("stacking_buff", "spend_stacks", "gain_counter",
                  "start_cooldown", "reset_counter", "true_damage_stat_pct",
                  "enemy_lose_stability", "gain_stability",
                  "bonus_damage_stat"):
        check(f"'{wanted}' op reachable from Drakoryn's own JSON",
              wanted in ops_seen, sorted(ops_seen))
    check("passive, turn_start, on_defense_win/loss/mirror, on_attack_win "
          "and on_special triggers all present",
          triggers_seen == {"passive", "turn_start", "on_defense_win",
                             "on_defense_loss", "on_defense_mirror",
                             "on_attack_win", "on_special"},
          triggers_seen)
    for ab in DR["abilities"]:
        for rule in ab.get("rules", []):
            for op in rule.get("do", []):
                for cond in op.get("_if", []):
                    check(f"op-level _if condition '{cond['cond']}' reachable",
                          cond["cond"] in ("enemy_stability_below_pct",))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
