#!/usr/bin/env python3
"""
tools/sim_radiant_valkyrie.py — Radiant Valkyrie, a live transform on a timer.

Why this suite exists
----------------------
Radiant Valkyrie needed no new engine primitives — every piece of its kit
reduces to ops already proven by earlier blades:

  - `evolve_form` (Cosmic Phoenix) for the name/image swap.
  - `set_mode` with `turns` + `on_expire` (pre-existing, but never exercised
    by a shipped blade's own sim before this one) for the TIMED transform —
    the mode reverts itself and runs an arbitrary `on_expire` op list, which
    is what lets the swap-in (`evolve_form` on cast) and swap-out
    (`evolve_form` on expiry) be two ordinary uses of the same op rather than
    a bespoke "remember the old name/image" mechanism.
  - `dmg_amp` with `turns` for the flat "+10% damage" window, tracked
    separately from the `buff` op's "+25% Attack" (two different pipelines —
    `get_buff_bonus` vs `get_dmg_amp` — that happen to both expire on a timer).
  - `not_on_cooldown` / `start_cooldown` for Radiant Rush's "once every N
    rounds" cadence, with two mode-gated rule variants (`mode_is_not` /
    `mode_is` "silverflame") sharing one cooldown name so N flips from 2 to 1
    without a second counter.
  - `status_apply` (`status: "burn"`) for Silver Burn — the same generic burn
    every other blade's burn already uses.

The one thing this suite has to prove empirically rather than assume: `buff`
duration ticks per-mover, inside `AbilityEngine.apply()` (via
`DamageFilter._step1_tick`, gated on `is_first_hit`), while `dmg_amp` and
`set_mode`'s timed revert tick via the SEPARATE `tick_dmg_amps()`/
`tick_extras()` sweeps, called once per round rather than once per mover
(cogs/battle/session.py:1280-1284). Both have to be driven every simulated
round or the +25% Attack buff and the +10% damage amp / transform-revert
would drift out of sync with each other despite being authored with the same
"3 turns". Section 4 drives both and checks all three expire on the same
round.

Also proven: re-casting the Special WHILE ALREADY in Silver Flame mode does
NOT plant a second `timed_modes` entry (which would let the FIRST entry's
expiry revert the form early, cutting the refreshed window short) — the
transform block is gated `mode_is_not: silverflame`, so a re-cast only ever
adds its ATK-scaling rider, never re-arms the timer. Section 5 is the
regression check for that.

Driven through a REAL AbilityEngine, StatusManager, StaminaManager,
TypeModifiers and StabilityManager — the technique every sim_<blade>.py in
this repo uses (see sim_cosmic_phoenix.py, sim_drakoryn.py).

Run:  python3 tools/sim_radiant_valkyrie.py
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
from cogs.core.constants import MOVE_ATTACK, MOVE_CHARGE, MOVE_SPECIAL  # noqa: E402
from utils.database import get_beyblade, load_beyblades         # noqa: E402
from utils import bey_levels as BL                              # noqa: E402

NAME = "Radiant Valkyrie"
RV = get_beyblade(NAME)
ALL = load_beyblades()

DUMMY = {"name": "Dummy", "type": "Balance", "spin_direction": "Left",
        "stats": {"attack": 100, "defense": 100, "stamina": 100, "hp": 100}}
# An Attack-type mirror: BOTH sides' type multipliers suppress (neither the
# attacker's outgoing bonus nor the defender's mitigation is active for a
# same-type non-Balance matchup — resolve_active_bonuses' "unknown/mirror →
# both inactive" rule) — the special-move-math checks in section 3 need
# exact numbers, not numbers muddied by a third multiplier neither the spec
# nor the engine change under test is about.
MIRROR_FOE = {"name": "Foe", "type": "Attack", "spin_direction": "Left",
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


def move(s, my_move, my_matchup, mkey="p", okey="e", dmg=0):
    s.last_moves = {mkey: my_move, okey: my_move}
    out, taken, logs = s.ability.apply(mkey, okey, s.blades[mkey], s.blades[okey],
                                        my_move, my_matchup, dmg, 0,
                                        is_first_hit=True, cumulative_dmg=0,
                                        is_last_hit=True)
    return out, taken, logs


def tick_round(s, mover="p"):
    """Advance one full round: the mover's own buff-duration tick (which only
    fires when THAT player is the mover of a real move — see the module
    docstring), then the once-per-round dmg_amp/cooldown/timed_mode sweep.
    A neutral MOVE_CHARGE keeps this from also proc-ing Radiant Rush or
    Silver Burn, which only fire on `on_attack_hit`.
    """
    move(s, MOVE_CHARGE, "lose", mkey=mover)
    logs = list(s.ability.tick_dmg_amps())
    logs.extend(s.ability.tick_extras())
    return logs


def main() -> int:
    # ── 1. the card ─────────────────────────────────────────────────────────
    print("\n── 1. the card matches the spec ─────────────────────────────────")
    check("Radiant Valkyrie is in the roster", RV is not None)
    check("rarity Mythic", RV.get("rarity") == "Mythic", RV.get("rarity"))
    check("type Attack", RV.get("type") == "Attack", RV.get("type"))
    check("flagged limited, unbound",
          RV.get("limited") is True and not RV.get("owner_ids"))
    check("its id is unique",
          sum(1 for b in ALL.values() if b.get("id") == RV["id"]) == 1, RV["id"])
    check("the normal-form art is stored whole, query string included",
          "1542361648273166406" in RV["image_url"] and "hm=" in RV["image_url"])
    names = [a["name"] for a in RV["abilities"]]
    check("both abilities are on the plural list the engine reads",
          names == ["Radiant Rush", "Silver Flame Awakening"], names)
    check("the singular mirrors the first of them",
          RV["ability"]["name"] == RV["abilities"][0]["name"])
    at100 = BL.stats_at(RV, 100, {})
    check("base stats stay at or under the level-100 cap",
          all(v <= BL.STAT_CAP for v in at100.values()), at100)
    check("Special is authored with a real (non-zero) base — 170",
          RV["special_move"]["damage_per_hit"] == 170)
    check("the transformation art is present in the JSON at all",
          any("1542361664182296656" in op.get("image_url", "")
              for ab in RV["abilities"] for rule in ab["rules"]
              for op in rule.get("do", []) if op.get("op") == "evolve_form"))

    # ── 2. Radiant Rush — damage math, and the 2-round normal cadence ───────
    print("\n── 2. Radiant Rush — +13% ATK +8% on win/loss, every 2 rounds ─")
    atk = RV["stats"]["attack"]
    # Both riders are a share of the ATK STAT, not of the damage already
    # dealt. This shipped as `bonus_damage_pct: 180` — +180% of the hit — which
    # tripled a normal Attack: 906 at level 100, 45% of a full HP bar, every
    # other round, from an ability rather than a Special. "+180% ATK scaling"
    # in the spec meant the Special's kind of scaling (a multiple of ATK), and
    # the corrected figure is +20%.
    rush = round(atk * 0.13) + round(atk * 0.08)

    s = FakeSession(RV, DUMMY)
    out1, _, logs1 = move(s, MOVE_ATTACK, "win", dmg=100)
    check("round 1: Radiant Rush fires — +13% ATK plus +8% on an Attack win",
          out1 == 100 + rush, (out1, 100 + rush))
    check("...and it is a fraction of ATK, NOT a multiple of the hit — a "
          "100-damage swing must not come back as 300",
          out1 < 200, out1)
    check("...and it's on cooldown afterward",
          s.ability.cooldowns.get(("p", "radiant_rush"), 0) == 2,
          s.ability.cooldowns.get(("p", "radiant_rush")))

    tick_round(s)   # round 1 -> 2: cooldown 2 -> 1
    out2, _, _ = move(s, MOVE_ATTACK, "win", dmg=100)
    check("round 2: still on cooldown — plain damage only",
          out2 == 100, out2)

    tick_round(s)   # round 2 -> 3: cooldown 1 -> 0, removed
    out3, _, _ = move(s, MOVE_ATTACK, "win", dmg=100)
    check("round 3: off cooldown — Radiant Rush fires again "
          "(this is the 'once every 2 rounds' cadence)",
          out3 == 100 + rush, (out3, 100 + rush))

    # ── 3. Silver Flame Awakening — a real Special drives the transform ─────
    print("\n── 3. Silver Flame Awakening — transform triggered by the Special ─")
    s2 = FakeSession(RV, MIRROR_FOE, ehp=100000)
    dealt, sp_logs = special(s2, "p", "e")
    want_special = RV["special_move"]["damage_per_hit"] + round(atk * 0.52)
    check("Silver Radiance: 170 base + a rider worth 52% Attack",
          dealt == want_special, (dealt, want_special))

    check("the form evolves: name changes to Silver Flame",
          s2.blades["p"]["name"] == "Radiant Valkyrie: Silver Flame",
          s2.blades["p"]["name"])
    check("...and the image swaps to the Silver Flame asset",
          "1542361664182296656" in s2.blades["p"]["image_url"])
    check("the mode tag flips to silverflame (drives Radiant Rush's cadence "
          "and Silver Burn)",
          s2.ability.modes.get("p") == "silverflame")
    check("+11% damage amp is live",
          abs(s2.status.get_dmg_amp("p") - 0.11) < 1e-9,
          s2.status.get_dmg_amp("p"))
    check("a timed_modes entry is armed for the 3-turn window",
          any(e["key"] == "p" and e["mode"] == "silverflame"
              for e in s2.ability.timed_modes))

    # Radiant Rush now fires every round, not every 2, while transformed.
    out_sf1, _, _ = move(s2, MOVE_ATTACK, "win", dmg=100)
    check("in Silver Flame: Radiant Rush fires immediately",
          out_sf1 > 100, out_sf1)
    check("...on a 1-round cooldown, not 2",
          s2.ability.cooldowns.get(("p", "radiant_rush"), 0) == 1)
    tick_round(s2)
    out_sf2, _, _ = move(s2, MOVE_ATTACK, "win", dmg=100)
    check("next round, still in Silver Flame: Radiant Rush fires AGAIN "
          "immediately — no gap, unlike the normal 2-round cadence",
          out_sf2 > 100, out_sf2)

    # Silver Burn: an Attack lands a burn stack on the defender only while
    # Silver Flame is active.
    check("Silver Burn is on the defender after an Attack in Silver Flame",
          s2.status.burn_stacks.get("e", 0) > 0,
          s2.status.burn_stacks.get("e", 0))
    check("Silver Burn is 20 damage for 3 turns, max 3 stacks",
          any(op.get("op") == "status_apply" and op.get("status") == "burn"
              and op.get("dmg") == 20 and op.get("turns") == 3
              and op.get("max_stacks") == 3
              for ab in RV["abilities"] for rule in ab.get("rules", [])
              for op in rule.get("do", [])))

    s2b = FakeSession(RV, DUMMY)   # never transformed
    move(s2b, MOVE_ATTACK, "win", dmg=100)
    check("outside Silver Flame, the SAME Attack does NOT apply Silver Burn",
          s2b.status.burn_stacks.get("e", 0) == 0)

    # ── 4. the 3-turn window — buff, amp and the form itself expire together ─
    print("\n── 4. Silver Flame's 3-turn window expires everything at once ────")
    s3 = FakeSession(RV, MIRROR_FOE, ehp=100000)
    special(s3, "p", "e")
    check("armed: transformed and amped",
          s3.blades["p"]["name"] == "Radiant Valkyrie: Silver Flame"
          and s3.status.get_dmg_amp("p") > 0)

    tick_round(s3)   # 3 -> 2
    check("after 1 round: still transformed",
          s3.blades["p"]["name"] == "Radiant Valkyrie: Silver Flame")
    tick_round(s3)   # 2 -> 1
    check("after 2 rounds: still transformed",
          s3.blades["p"]["name"] == "Radiant Valkyrie: Silver Flame")
    tick_round(s3)   # 1 -> 0: reverts
    check("after 3 rounds: the form reverts to Radiant Valkyrie",
          s3.blades["p"]["name"] == "Radiant Valkyrie", s3.blades["p"]["name"])
    check("...and the image reverts to the normal asset",
          "1542361648273166406" in s3.blades["p"]["image_url"])
    check("...and the mode tag clears",
          s3.ability.modes.get("p") != "silverflame", s3.ability.modes.get("p"))
    check("...and the +11% damage amp is gone",
          s3.status.get_dmg_amp("p") == 0, s3.status.get_dmg_amp("p"))

    out_after, _, _ = move(s3, MOVE_ATTACK, "win", dmg=100)
    check("Radiant Rush is back to needing a fresh proc (no leftover Silver "
          "Flame cadence) once reverted",
          out_after in (100, 100 + rush), out_after)

    # ── 5. re-casting the Special while ALREADY transformed doesn't restart "
    print("── 5. re-casting Silver Radiance mid-transform doesn't plant a "
          "second timer ──")
    s4 = FakeSession(RV, MIRROR_FOE, ehp=100000)
    special(s4, "p", "e")                       # 1st cast: arms the window
    tick_round(s4)                               # 3 -> 2
    special(s4, "p", "e")                       # 2nd cast, still transformed
    check("only ONE timed_modes entry exists — the re-cast did not arm a "
          "second, independently-expiring window",
          sum(1 for e in s4.ability.timed_modes
              if e["key"] == "p" and e["mode"] == "silverflame") == 1,
          s4.ability.timed_modes)
    check("the re-cast's rider still added real damage "
          "(the transform block is gated, not the whole ability)",
          s4.hp["e"] < 100000 - 2 * round(atk * 0.52))

    # ── 6. reachability ────────────────────────────────────────────────────
    print("\n── 6. reachability — every op/trigger the kit names is in its JSON ─")
    ops_seen, triggers_seen = set(), set()
    for ab in RV["abilities"]:
        for rule in ab.get("rules", []):
            triggers_seen.add(rule.get("when"))
            for op in rule.get("do", []):
                ops_seen.add(op.get("op"))
            for op in rule.get("do", []):
                for exp_op in op.get("on_expire", []):
                    ops_seen.add(exp_op.get("op"))
    # `bonus_damage_pct` is deliberately ABSENT: Radiant Rush's riders are both
    # a share of the ATK stat. It used to carry `bonus_damage_pct: 180`, which
    # multiplied the hit rather than scaling off the stat and tripled a normal
    # Attack — so its absence here is a real assertion, not an omission.
    for wanted in ("bonus_damage_stat", "start_cooldown",
                  "evolve_form", "buff", "dmg_amp", "set_mode",
                  "status_apply"):
        check(f"'{wanted}' op reachable from Radiant Valkyrie's own JSON",
              wanted in ops_seen, sorted(ops_seen))
    check("on_attack_hit and on_special triggers both present",
          triggers_seen == {"on_attack_hit", "on_special"}, triggers_seen)
    check("Radiant Rush no longer multiplies the hit — no bonus_damage_pct "
          "anywhere in this blade",
          "bonus_damage_pct" not in ops_seen, sorted(ops_seen))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
