#!/usr/bin/env python3
"""
sim_story.py — headless proof that Story Mode works, and that a worn avatar
actually changes the fight.

Runs without a bot token and without touching the user database: the player's
fighter is stubbed to a fixed statline, and the equipped avatar is chosen by
patching the one lookup avatar_engine uses, so every bonus still flows through
the real avatar code path.

    python tools/sim_story.py             # full suite
    python tools/sim_story.py --fights 400
    python tools/sim_story.py --table     # per-stage win-rate table only

Exits non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.avatar import avatar_engine                      # noqa: E402
from cogs.battle.boss import boss_ai as ai                 # noqa: E402
from cogs.story import story_data, story_engine            # noqa: E402

# cogs/avatar/__init__.py does `from .avatar_engine import avatar_engine`, which
# rebinds the package attribute `avatar_engine` from the SUBMODULE to the engine
# SINGLETON. `import cogs.avatar.avatar_engine as m` therefore hands back the
# instance, and patching it would only set a shadowing instance attribute.
# sys.modules is the only reliable handle on the real module.
AVATAR_ENGINE_MODULE = sys.modules["cogs.avatar.avatar_engine"]

# A representative mid-game player: a balance blade around the middle of
# beyblades.json, no parts, no levels. Story balance is quoted against this.
BASE_ATK, BASE_DEF, BASE_STA = 100.0, 95.0, 95.0
BASE_HP = float(story_engine.BASE_PLAYER_HP)

# Avatars exercised by the effect assertions, chosen for what they prove.
DYRROTH = "avatar_x003"     # nth-hit, defence-break, ult rider, special %
ARGUS   = "avatar_x002"     # crit, gauge-on-crit, immortality
OMEGA   = "avatar_x001"     # dodge, counter, damage resistance, multi-hit

_equipped: str | None = None


def _install_stubs() -> None:
    """Point the avatar lookup at `_equipped`, and stub the player's fighter.

    Only two things are faked. `get_equipped_avatar` is the single function
    avatar_engine.get_battle_bonuses consults, so patching it leaves the entire
    bonus-parsing path real. `build_player_fighter` is replaced to avoid
    creating database rows — it applies the avatar's stat bonuses itself, the
    same way loadout.effective_blade / effective_hp do for the real one.

    The patch goes on the MODULE, not on the engine singleton: avatar_engine.py
    does `from utils.database import get_equipped_avatar` at import time, so
    the name it actually calls lives in its own namespace.
    """
    AVATAR_ENGINE_MODULE.get_equipped_avatar = lambda _uid: _equipped

    def _player(_uid: int):
        av = avatar_engine.get_battle_bonuses(_uid)
        atk = float(av.apply_attack_bonus(BASE_ATK))
        dfn = float(av.apply_defence_bonus(BASE_DEF))
        sta = float(av.apply_stamina_bonus(BASE_STA))
        hp = float(av.apply_hp_bonus(BASE_HP))
        return ai.Fighter("SimBlade", hp, hp, atk, dfn, sta, sp=10.0,
                          dmg_mult=ai.type_damage_mult("balance")), {}

    story_engine.build_player_fighter = _player


def run_fight(stage_id: str, avatar_id: str | None, rng: random.Random,
              player_skill: str = "veteran") -> dict:
    """One full fight. The player is driven by the same search AI as the
    opponent, at `player_skill`, so both sides play sensibly and the only
    variable across runs is what the player is wearing."""
    global _equipped
    _equipped = avatar_id

    fight = story_engine.StoryFight(1, "Sim", stage_id, rng=rng)
    player_model = ai.OpponentModel()

    avatar_logs: list[str] = []
    hp_trace: list[tuple[float, float]] = []
    dead_then_alive = False

    while not fight.finished:
        # Choose from the player's side: swap the argument order so the search
        # optimises for the player instead of the opponent.
        move, _ = ai.choose_move(fight.foe, fight.npc, player_model,
                                 rng=rng, difficulty=player_skill)
        npc_hp_before, foe_hp_before = fight.npc.hp, fight.foe.hp

        report = fight.step(move)
        # The model predicts the player's OPPONENT, so it observes the
        # opponent's moves — not the player's own.
        player_model.observe(report["npc_move"])
        avatar_logs.extend(report.get("avatar_logs") or [])
        hp_trace.append((fight.npc.hp, fight.foe.hp))

        if npc_hp_before <= 0 < fight.npc.hp or foe_hp_before <= 0 < fight.foe.hp:
            # outcome()'s double-KO tiebreak legitimately revives the winner to
            # 1 HP; anything larger is the resurrection bug.
            if fight.npc.hp > 1.0 or fight.foe.hp > 1.0:
                dead_then_alive = True

    return {
        "result": fight.result,
        "turns": fight.turn,
        "avatar_logs": avatar_logs,
        "negative_hp": any(a < 0 or b < 0 for a, b in hp_trace),
        "resurrected": dead_then_alive,
    }


def win_rate(stage_id: str, avatar_id: str | None, fights: int,
             seed: int = 0) -> tuple[float, float, list[str]]:
    rng = random.Random(seed)
    wins = turns = 0
    logs: list[str] = []
    for _ in range(fights):
        out = run_fight(stage_id, avatar_id, rng)
        wins += out["result"] == "win"
        turns += out["turns"]
        logs.extend(out["avatar_logs"])
        if out["negative_hp"]:
            raise AssertionError(f"{stage_id}: HP went negative")
        if out["resurrected"]:
            raise AssertionError(f"{stage_id}: a fighter came back from 0 HP")
    return wins / fights, turns / fights, logs


# ── Checks ───────────────────────────────────────────────────────────────────

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def check_effects_fire(fights: int) -> None:
    """Every avatar effect the boss resolver can express must actually log."""
    print("\n▸ avatar effects fire in Story Mode")
    cases = [
        (DYRROTH, "Dyrroth", ["Guard Breaker", "th strike", "full Attack stat"]),
        (ARGUS,   "Argus",   ["critical hit", "charges the gauge"]),
        (OMEGA,   "Omega",   ["dodged the hit", "resistance absorbs"]),
    ]
    for avatar_id, name, needles in cases:
        _, _, logs = win_rate("2-4", avatar_id, fights, seed=7)
        blob = "\n".join(logs)
        for needle in needles:
            check(f"{name}: {needle!r} appears in the battle log",
                  needle in blob)


def check_avatar_lift(fights: int) -> None:
    """Wearing an avatar must measurably improve the outcome."""
    print("\n▸ wearing an avatar changes the fight")
    # Stages with headroom: chapter 1 is near 100% bare, so an avatar there can
    # only tie, which would make the assertion meaningless.
    for stage_id in ("2-4", "3-2", "3-4"):
        bare, _, _ = win_rate(stage_id, None, fights, seed=11)
        for avatar_id, name in ((DYRROTH, "Dyrroth"), (ARGUS, "Argus"),
                                (OMEGA, "Omega Prime")):
            worn, _, _ = win_rate(stage_id, avatar_id, fights, seed=11)
            check(f"{stage_id}: {name} beats bare",
                  worn > bare, f"{bare:.0%} → {worn:.0%}")


def check_difficulty_order(fights: int) -> None:
    """Harder difficulty must not be easier to beat, all else equal."""
    print("\n▸ difficulty ordering")
    stage = dict(story_data.stage("2-4"))
    rates = []
    for level in ("rookie", "veteran", "elite", "legend"):
        story_data._BY_ID["2-4"] = {**stage, "difficulty": level}
        rate, _, _ = win_rate("2-4", None, fights, seed=3)
        rates.append((level, rate))
    story_data._BY_ID["2-4"] = stage

    print("     " + "  ".join(f"{lv}={r:.0%}" for lv, r in rates))
    ordered = all(rates[i][1] >= rates[i + 1][1] - 0.05
                  for i in range(len(rates) - 1))
    check("win rate is non-increasing rookie → legend", ordered)


def check_progression(fights: int) -> None:
    """The campaign must get harder, and stay winnable at both ends."""
    print("\n▸ campaign difficulty curve (no avatar)")
    rates = []
    for st in story_data.all_stages():
        rate, turns, _ = win_rate(st["id"], None, fights, seed=5)
        rates.append((st["id"], rate, turns))
        s = st["stats"]
        print(f"     {st['id']}  {st['name'][:24]:<24} Lv{st['level']:>3}  "
              f"{s['hp']:>5}hp {s['attack']:>4}a {s['defense']:>4}d {s['stamina']:>4}s  "
              f"{rate:>5.0%}  ~{turns:.0f} turns")

    first, last = rates[0][1], rates[-1][1]
    check("opener is approachable (>= 60%)", first >= 0.60, f"{first:.0%}")
    check("finale is hard but possible (5%–55%)", 0.05 <= last <= 0.55,
          f"{last:.0%}")
    check("finale is harder than the opener", last < first,
          f"{first:.0%} → {last:.0%}")
    check("no stage is a guaranteed loss",
          all(r > 0.0 for _, r, _ in rates))
    check("fights resolve well inside the turn cap",
          all(t < story_engine.TURN_CAP * 0.8 for _, _, t in rates))


def check_levels() -> None:
    """Opponent levels must climb, and drive the stats they are supposed to."""
    print("\n▸ opponent level system")
    stages = story_data.all_stages()
    levels = [s["level"] for s in stages]
    check("opponent level rises every stage",
          all(b > a for a, b in zip(levels, levels[1:])),
          " → ".join(str(v) for v in levels))
    # Compared within an archetype, not between neighbours: consecutive stages
    # are usually different archetypes, so a naive neighbour check would find
    # no comparable pairs and pass vacuously.
    by_type: dict[str, list[dict]] = {}
    for st in stages:
        by_type.setdefault(st["type"], []).append(st)
    check("attack rises with level within every archetype",
          all(b["stats"]["attack"] > a["stats"]["attack"]
              for group in by_type.values()
              for a, b in zip(group, group[1:])),
          f"{len(by_type)} archetypes, "
          f"{sum(len(g) - 1 for g in by_type.values())} comparisons")
    # HP deliberately grows far more slowly than the offensive stats — that is
    # what keeps every stage inside the same turn window instead of making the
    # late campaign merely longer.
    check("HP growth rate is well under the stat growth rate",
          story_data.HP_GROWTH < story_data.STAT_GROWTH / 2,
          f"{story_data.HP_GROWTH} vs {story_data.STAT_GROWTH}")
    first, last = stages[0]["stats"], stages[-1]["stats"]
    hp_ratio = last["hp"] / first["hp"]
    atk_ratio = last["attack"] / first["attack"]
    check("realised HP spread is smaller than the attack spread",
          hp_ratio < atk_ratio,
          f"hp x{hp_ratio:.2f} vs atk x{atk_ratio:.2f}")
    check("a level change moves the stats",
          story_data.opponent_stats({"type": "balance", "level": 40})["attack"]
          > story_data.opponent_stats({"type": "balance", "level": 10})["attack"])


def check_unlocks() -> None:
    print("\n▸ unlock chain")
    ids = [s["id"] for s in story_data.all_stages()]
    check("first stage is open with nothing cleared",
          story_data.is_unlocked(ids[0], []))
    check("second stage is locked with nothing cleared",
          not story_data.is_unlocked(ids[1], []))
    check("clearing a stage opens the next",
          story_data.is_unlocked(ids[1], [ids[0]]))
    check("first_uncleared walks the chain",
          story_data.first_uncleared(ids[:3]) == ids[3])
    check("last stage has no successor",
          story_data.next_stage(ids[-1]) is None)
    check("resolve() finds a stage by name",
          (story_data.resolve("kenta") or {}).get("id") == "1-1")
    check("resolve() finds a stage by id",
          (story_data.resolve("3-4") or {}).get("id") == "3-4")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fights", type=int, default=200)
    ap.add_argument("--table", action="store_true",
                    help="only print the per-stage win-rate table")
    args = ap.parse_args()

    avatar_engine.load()
    _install_stubs()

    if args.table:
        check_progression(args.fights)
    else:
        check_unlocks()
        check_levels()
        check_effects_fire(max(60, args.fights // 3))
        check_progression(args.fights)
        check_avatar_lift(args.fights)
        check_difficulty_order(args.fights)

    print()
    if FAILURES:
        print(f"✗ {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"    - {f}")
        return 1
    print("✓ all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
