#!/usr/bin/env python3
"""
tools/sim_battle_ticks.py — timed status effects survive exactly N real rounds.

Why this suite exists
----------------------
A repo-wide audit found that EVERY timed buff, debuff, silence, and universal
duration (`ignore_invuln_turns`, `true_damage_turns`) in the entire roster was
being ticked down TWICE per round instead of once: `DamageFilter._step1_tick`
already ticks each player once per round (reached through `AbilityEngine.apply()`
during that player's own `resolve_pair()` call), and `BattleSession`'s
end-of-round loop in `cogs/battle/session.py` ticked both players AGAIN,
unconditionally. A "for 2 turns" buff only actually lasted ~1 round in real
play — silently, roster-wide, since it shipped.

It was never caught because EVERY other battle-related sim_*.py (every
per-blade suite included) builds a minimal `FakeSession` and calls
`AbilityEngine.apply()`/`StatusManager.tick_buffs()` directly, in isolation —
structurally unable to see a bug that only exists in the interaction between
`damage_filter.py`'s per-mover tick and `session.py`'s end-of-round tick. Only
`sim_story.py` and `sim_school_avatars.py` drive a real `BattleSession` round
loop, and neither happens to assert buff-duration exactness.

This suite closes that gap: it drives a REAL `BattleSession` (the same class
`;battle` and Story Mode both use — nothing in `cogs/battle/` is stubbed,
following the pattern `sim_story.py` established) through real rounds and
asserts a granted buff's `rounds_left` decrements by exactly 1 per round.

Proof this test would have caught the bug: before the fix (restoring the
`tick_buffs(key)`/`tick_silence(key)`/`tick_universal(key)` calls that used to
sit in `session.py`'s end-of-round loop, duplicating `damage_filter.py`'s
per-mover tick), a 3-round buff granted before round 1 read `rounds_left == 1`
after round 1 resolved instead of `2` — confirmed directly against the real
`StatusManager`/`AbilityEngine` classes during the audit, and independently
reproduced by a second read of the same code. Section 1 below is that same
check, kept in the suite so a regression trips it again.

Run:  python3 tools/sim_battle_ticks.py
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {detail}")


# ── The in-memory store — installed BEFORE anything imports utils.database ───
import utils.database as DB                                      # noqa: E402


class MemoryStore:
    """Minimal UserStore stand-in — same shape as sim_story.py's, so a
    BattleSession's profile reads/writes (payout=False, but level/energy
    lookups can still happen) land somewhere real rather than on disk."""

    def __init__(self) -> None:
        self.data: dict = {}

    def get_one(self, uid):
        v = self.data.get(str(uid))
        return copy.deepcopy(v) if v is not None else None

    def put_one(self, uid, prof, touch=True):
        self.data[str(uid)] = copy.deepcopy(prof)

    def has(self, uid):
        return str(uid) in self.data

    def load_all(self):
        return copy.deepcopy(self.data)

    def save_all(self, data):
        self.data = copy.deepcopy(data)


STORE = MemoryStore()
DB.USER_STORE = STORE

from cogs.battle.session import BattleSession                     # noqa: E402
from cogs.core.constants import MOVE_ATTACK, MOVE_DEFENSE         # noqa: E402
from utils.database import get_beyblade                           # noqa: E402


class FakeMessage:
    def __init__(self, payload):
        self.payload = payload

    async def edit(self, **kw):
        self.payload.update(kw)
        return self

    async def delete(self):
        pass


class FakeChannel:
    id = 555666777

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, *a, **kw):
        self.sent.append(kw)
        return FakeMessage(dict(kw))


class FakePlayer:
    def __init__(self, uid, name):
        self.id = int(uid)
        self.display_name = name
        self.bot = False
        self.mention = f"<@{uid}>"


async def _no_card(self):
    """The session's own documented Pillow-less fallback — no PNG needed."""
    return None


BattleSession._battle_card_file = _no_card


def build_pvp_session(blade_a="Void Longinus", blade_b="Void Longinus"):
    """A plain two-human battle, built directly (no League/NPC wrapper) —
    payout/ranked/spend_energy all off so this stays a pure round-loop test."""
    ch = FakeChannel()
    pa = FakePlayer(1001, "Alice")
    pb = FakePlayer(1002, "Bob")
    ba = copy.deepcopy(get_beyblade(blade_a))
    bb = copy.deepcopy(get_beyblade(blade_b))
    s = BattleSession(bot=None, channel=ch, p1=pa, p2=pb,
                      blade1=ba, blade2=bb, ranked=False,
                      payout=False, spend_energy=False)
    return s, ch, pa, pb


async def play_round(session, move_a, move_b):
    ka, kb = str(session.players[0].id), str(session.players[1].id)
    session.moves = {ka: move_a, kb: move_b}
    await session._resolve_round()


async def suite() -> None:
    # ── 1. a timed buff survives exactly N real rounds, not N/2 ──────────────
    print("\n── 1. a timed buff decrements by exactly 1 per REAL round ───────")
    s, ch, pa, pb = build_pvp_session()
    ka, kb = str(pa.id), str(pb.id)

    s.status.add_buff(ka, "attack", 20, 3)   # a 3-round buff, granted pre-round-1
    entry = s.status.active_buffs[ka][0]
    check("the buff starts at 3 rounds_left", entry["rounds_left"] == 3,
          entry["rounds_left"])

    await play_round(s, MOVE_DEFENSE, MOVE_DEFENSE)   # round 1
    check("after 1 real round, exactly 1 tick has been spent (3 -> 2)",
          s.status.get_buff_bonus(ka, "attack") == 20
          and s.status.active_buffs[ka][0]["rounds_left"] == 2,
          [b["rounds_left"] for b in s.status.active_buffs.get(ka, [])])

    await play_round(s, MOVE_DEFENSE, MOVE_DEFENSE)   # round 2
    check("after 2 real rounds: 2 -> 1, buff still active",
          s.status.get_buff_bonus(ka, "attack") == 20
          and s.status.active_buffs[ka][0]["rounds_left"] == 1,
          [b["rounds_left"] for b in s.status.active_buffs.get(ka, [])])

    await play_round(s, MOVE_DEFENSE, MOVE_DEFENSE)   # round 3
    check("after 3 real rounds the buff has fully expired, not before",
          s.status.get_buff_bonus(ka, "attack") == 0,
          s.status.active_buffs.get(ka))

    # ── 2. silence and the universal (ignore_invuln/true_damage) counters ────
    print("\n── 2. silence and universal durations tick once per round too ───")
    s2, ch2, pa2, pb2 = build_pvp_session()
    ka2 = str(pa2.id)
    s2.status.silence(ka2, 2)
    s2.status.set_duration("true_damage_turns", ka2, 2)
    await play_round(s2, MOVE_DEFENSE, MOVE_DEFENSE)
    check("silence: 2 -> 1 after one real round, not 0",
          s2.status.silenced_turns.get(ka2, 0) == 1,
          s2.status.silenced_turns.get(ka2))
    check("true_damage_turns: 2 -> 1 after one real round, not 0",
          s2.status.get_duration("true_damage_turns", ka2) == 1,
          s2.status.get_duration("true_damage_turns", ka2))

    # ── 3. invulnerable_turns — the one tick NOT affected by this bug class ──
    print("\n── 3. invulnerable_turns (its own dict, its own tick site) ──────")
    s3, ch3, pa3, pb3 = build_pvp_session()
    ka3 = str(pa3.id)
    s3.status.set_invulnerable(ka3, 2)
    await play_round(s3, MOVE_DEFENSE, MOVE_DEFENSE)
    check("invulnerable_turns still decrements exactly once per round",
          s3.status.invulnerable_turns.get(ka3, 0) == 1,
          s3.status.invulnerable_turns.get(ka3))


def main() -> int:
    asyncio.run(suite())
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
