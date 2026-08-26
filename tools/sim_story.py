#!/usr/bin/env python3
"""
sim_story.py — the School League, driven through the REAL PvP battle engine.

What changed, and why this file was rewritten from scratch
----------------------------------------------------------
Story Mode used to run on `cogs/story/story_engine.py`, a second combat
resolver built on the boss AI. In that engine a blade's ability did nothing and
its Special was `attack x 2.6`. As of v1.22 a School League battle IS a
`BattleSession` — the same class `;battle` uses — so stability, ring-outs, the
whole ability DSL, named Special moves, status effects and the real damage
pipeline are all in play. The old suite tested a module that no longer exists.

The harness, and why it is trustworthy
--------------------------------------
Nothing in `cogs/battle/` is stubbed. A real `BattleSession` is constructed and
driven to a real finish. Exactly three things are faked, and each is faked at a
boundary that is not the thing under test:

  * `utils.database.USER_STORE` — an in-memory store. `get_user`,
    `update_user` and `grant_xp` keep their real bodies; only the disk goes
    away. Every read and write is recorded, which is what makes the "an NPC
    never gets a profile" check possible at all.
  * the channel — `send` returns a message object with `edit`. The session
    posts embeds; the harness keeps them and reads them.
  * `_battle_card_file` — Pillow renders a PNG per round. Returning None is the
    session's own documented fallback path, and the checks that care about the
    panel build the real `_InChannelControlPanel` instead.

The human's moves come from the same `boss_ai` search the opponent uses, so the
win rates below are "a competent player", not "a player who mashes Attack" —
that distinction is worth about sixty percentage points, which is why it is
stated here rather than left for someone to discover.

    python3 tools/sim_story.py            # the suite
    python3 tools/sim_story.py --table    # the win-rate table only
    python3 tools/sim_story.py --table --trials 20

Exits non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import inspect
import os
import random
import statistics
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


# ══════════════════════════════════════════════════════════════════════════════
#  The in-memory store — installed BEFORE anything imports from utils.database
# ══════════════════════════════════════════════════════════════════════════════
import utils.database as DB                                     # noqa: E402


class MemoryStore:
    """A UserStore that lives in a dict and remembers who touched it.

    `utils.database.get_user` on an id it has never seen builds a default
    profile and PERSISTS it. That is the exact behaviour the NPC guards in
    `BattleSession` exist to route around, so the store has to be real enough
    for that write to happen — and observable enough to prove it does not.
    """

    def __init__(self) -> None:
        self.data: dict = {}
        self.reads: list[str] = []
        self.writes: list[str] = []

    def get_one(self, uid):
        self.reads.append(str(uid))
        v = self.data.get(str(uid))
        return copy.deepcopy(v) if v is not None else None

    def put_one(self, uid, prof, touch=True):
        self.writes.append(str(uid))
        self.data[str(uid)] = copy.deepcopy(prof)

    def has(self, uid):
        return str(uid) in self.data

    def load_all(self):
        return copy.deepcopy(self.data)

    def save_all(self, data):
        self.writes.extend(str(k) for k in data)
        self.data = copy.deepcopy(data)

    def touched(self, uid) -> bool:
        return str(uid) in self.reads or str(uid) in self.writes

    def clear_log(self):
        self.reads.clear()
        self.writes.clear()


STORE = MemoryStore()
DB.USER_STORE = STORE

from cogs.battle import session as SESSION                       # noqa: E402
from cogs.battle import stamina_manager as SM                    # noqa: E402
from cogs.battle.boss import boss_ai as ai                       # noqa: E402
from cogs.battle.session import BattleSession                    # noqa: E402
from cogs.core.constants import (                                # noqa: E402
    MOVE_ATTACK, MOVE_CHARGE, MOVE_DEFENSE, MOVE_SPECIAL, MOVE_STAMINA,
)
from cogs.story import story_ai as SA                            # noqa: E402
from cogs.story import story_data as SD                          # noqa: E402
from cogs.story import story_match as SMatch                     # noqa: E402
from cogs.story.story_ai import LeagueOpponent                   # noqa: E402
from cogs.story.story_match import LeagueMatch, NPCFighter       # noqa: E402
from utils import bey_levels as BL                               # noqa: E402
from utils import ranked as RK                                   # noqa: E402
from utils.database import get_beyblade                          # noqa: E402


class _NoCardSession:
    """Just enough session for `_bonuses_for` to answer for a card-less NPC."""

    npc_controller = type("C", (), {"key": "999", "avatar_id": None})()

    def _is_npc(self, pid):
        return True

    def _avatar_card_for(self, pid):
        return {}


# ── Discord stand-ins ─────────────────────────────────────────────────────────
class FakeMessage:
    def __init__(self, payload):
        self.payload = payload
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)
        self.payload.update(kw)
        return self

    async def delete(self):
        pass


class FakeChannel:
    id = 111222333

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, *a, **kw):
        self.sent.append(kw)
        return FakeMessage(dict(kw))

    def text(self) -> str:
        """Every embed body the battle posted, as one blob."""
        out = []
        for m in self.sent:
            e = m.get("embed")
            if e is None:
                continue
            out.append(str(getattr(e, "title", "") or ""))
            out.append(str(getattr(e, "description", "") or ""))
        return "\n".join(out)


class FakePlayer:
    def __init__(self, uid, name):
        self.id = int(uid)
        self.display_name = name
        self.bot = False
        self.mention = f"<@{uid}>"


HUMAN_ID = 956773141265391676


def seed_profile(uid=HUMAN_ID, blade="Void Longinus", **extra) -> dict:
    prof = {"user_id": str(uid), "coins": 0, "xp": 0, "level": 0,
            "wins": 0, "losses": 0, "inventory": [], "parts": [],
            "active_beyblade": blade}
    prof.update(extra)
    STORE.data[str(uid)] = copy.deepcopy(prof)
    return prof


def levelled(name: str, level: int = 100):
    """`(blade, hp_gain)` — the same pair `story_cog._opponent_blade` builds."""
    base = get_beyblade(name)
    blade = dict(base)
    printed = dict(base.get("stats") or {})
    blade["stats"] = BL.stats_at(base, level, {})
    gain = int(blade["stats"].get("hp", 0)) - int(printed.get("hp", 0))
    return blade, max(0, gain)


async def _no_card(self):
    """The session's own documented fallback — a battle with no PNG."""
    return None


SESSION.BattleSession._battle_card_file = _no_card


class Brain:
    """A competent human: the same search the opponent runs, at its own rung.

    Not a stand-in for a good player so much as a floor on one — it is blind to
    stability, exactly as the opponent is.
    """

    def __init__(self, rung="elite", seed=0):
        self.rung = rung
        self.model = ai.OpponentModel()
        self.rng = random.Random(seed)
        self.seen = 0
        self.picked: list[str] = []

    def __call__(self, session, key):
        okey = next((k for k in session.moves if k != key), None)
        try:
            last = (getattr(session, "last_moves", None) or {}).get(okey)
            if last and session.round > self.seen:
                self.model.observe(last)
                self.seen = session.round
        except Exception:                                # noqa: BLE001
            pass
        try:
            mv, _ = ai.choose_move(SA.project(session, key, okey),
                                   SA.project(session, okey, key),
                                   self.model, rng=self.rng,
                                   difficulty=self.rung)
        except Exception:                                # noqa: BLE001
            mv = None
        legal = SA.legal_moves(session, key)
        mv = mv if mv in legal else legal[-1]
        self.picked.append(mv)
        return mv


async def build_session(player, pblade, npc_blade, hp_gain, rung, *,
                  payout=False, spend_energy=True, seed=0, battle_no=1,
                  victory_points=None):
    """A League round, built the way `LeagueMatch._rounds` builds one.

    Kept deliberately in step with the real constructor — including the blader
    card and the Victory-Point score — because a harness that drops an argument
    tests a battle nobody plays.
    """
    ch = FakeChannel()
    npc = NPCFighter(battle_no, npc_blade["name"])
    entry = SD.battle(battle_no) or {}
    ctrl = LeagueOpponent(npc, npc_blade, difficulty=rung,
                          level=SD.OPPONENT_LEVEL, hp_gain=hp_gain,
                          rng=random.Random(seed),
                          avatar_id=entry.get("avatar"))
    s = await BattleSession.create(bot=None, channel=ch, p1=player, p2=npc,
                                   blade1=pblade, blade2=npc_blade, ranked=False,
                                   npc_controller=ctrl, payout=payout,
                                   spend_energy=spend_energy,
                                   victory_points=victory_points)
    return s, ch, npc, ctrl


async def drive(session, player_key, policy, cap=80):
    """Run a real session to its finish. Returns the number of rounds played."""
    turns = 0
    await session._prime_npc_move()
    while not session.finished and turns < cap:
        turns += 1
        session.moves[player_key] = policy(session, player_key)
        await session._resolve_round()
    return turns


async def play_match(player, pblade, npc_blade, hp_gain, rung, seed=0,
                     hum="elite", battle_no=1):
    """The Victory-Point loop, run the way `LeagueMatch` runs it.

    `battle_no` is NOT decorative and defaulting it silently was a measurement
    bug: `build_session` looks the opponent's blader card up by battle number,
    so a table that leaves it at 1 fights Rantaro's card in all eight cells and
    reports one opponent eight times.
    """
    pts = {"p": 0, "n": 0}
    rounds = turns = 0
    finishes = []
    while max(pts.values()) < SD.VICTORY_TARGET and rounds < SD.MAX_ROUNDS:
        rounds += 1
        s, _ch, npc, _c = await build_session(player, pblade, npc_blade, hp_gain,
                                        rung, seed=seed * 100 + rounds,
                                        battle_no=battle_no)
        pk, nk = str(player.id), str(npc.id)
        turns += await drive(s, pk, Brain(hum, seed * 100 + rounds))
        w = getattr(s, "winner_id", None)
        if not w:
            continue
        side = "p" if str(w) == pk else "n"
        kind = s.finish_for(nk if side == "p" else pk)
        finishes.append(kind)
        pts[side] += RK.finish_points(kind)
    return pts, rounds, turns, finishes


# ══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", action="store_true",
                    help="print the win-rate table and nothing else")
    ap.add_argument("--trials", type=int, default=6)
    args = ap.parse_args()

    if args.table:
        asyncio.run(win_rate_table(args.trials))
        return 0
    asyncio.run(suite(args.trials))
    print("\n" + "=" * 66)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 66)
    return 1 if FAIL else 0


# ══════════════════════════════════════════════════════════════════════════════
async def suite(trials: int) -> None:
    # ── 0. the harness is not lying ───────────────────────────────────────────
    print("\n── 0. the harness is honest about what it is testing ───────────")
    check("the fake store IS what utils.database writes through",
          DB.USER_STORE is STORE)
    STORE.clear_log()
    await DB.get_user(4242)
    check("...proved: get_user on an unknown id really does persist a row",
          STORE.has(4242) and "4242" in STORE.writes, STORE.writes[-3:])
    del STORE.data["4242"]

    check("a School League battle is a real BattleSession, not a copy",
          SMatch.BattleSession is BattleSession)
    check("...and it is the same class the PvP cog uses",
          SESSION.BattleSession is BattleSession)
    src = inspect.getsource(LeagueMatch._rounds)
    check("the match loop builds one session per Victory-Point round",
          "BattleSession.create(" in src)
    check("...with payout off and energy on",
          "payout=False" in src and "spend_energy=True" in src)

    # ── 1. the League table ───────────────────────────────────────────────────
    print("\n── 1. the eight battles ────────────────────────────────────────")
    check("there are eight of them", SD.total_battles() == 8,
          SD.total_battles())
    missing = [b["blade"] for b in SD.SCHOOL_LEAGUE
               if get_beyblade(b["blade"]) is None]
    check("every opponent names a blade that is really in the roster",
          not missing, missing)
    check("battle numbers are 1..8 with no gaps",
          [b["n"] for b in SD.SCHOOL_LEAGUE] == list(range(1, 9)))
    check("the rewards climb — battle 8 pays more than battle 1",
          SD.reward_for(8, SD.NORMAL) > SD.reward_for(1, SD.NORMAL))
    check("Normal pays exactly 12,000 across the League",
          SD.total_reward(SD.NORMAL) == 12000, SD.total_reward(SD.NORMAL))
    check("Nightmare pays exactly 30,000",
          SD.total_reward(SD.NIGHTMARE) == 30000,
          SD.total_reward(SD.NIGHTMARE))
    check("...which is the x2.5 the data says it is",
          SD.total_reward(SD.NIGHTMARE)
          == int(round(SD.total_reward(SD.NORMAL) * SD.NIGHTMARE_REWARD_MULT)))
    check("an unknown battle number pays nothing rather than raising",
          SD.reward_for(99, SD.NORMAL) == 0)
    check("a non-numeric battle id is a miss, not a ValueError",
          SD.battle("kenta") is None and SD.battle(None) is None)

    # ── 2. the unlock chain ───────────────────────────────────────────────────
    print("\n── 2. locks, clears and replays ────────────────────────────────")
    prof: dict = {}
    check("battle 1 is open to a brand-new player",
          SD.is_unlocked(prof, 1, SD.NORMAL))
    check("battle 2 is not", not SD.is_unlocked(prof, 2, SD.NORMAL))
    check("...and says why", "Beat battle" in SD.lock_reason(prof, 2, SD.NORMAL),
          SD.lock_reason(prof, 2, SD.NORMAL))
    check("clearing 1 is recorded as a FIRST clear",
          SD.record_clear(prof, SD.NORMAL, 1) is True)
    check("...and clearing it again is not",
          SD.record_clear(prof, SD.NORMAL, 1) is False)
    check("battle 2 opens once 1 is down",
          SD.is_unlocked(prof, 2, SD.NORMAL))
    check("Nightmare battle 1 is still shut",
          not SD.is_unlocked(prof, 1, SD.NIGHTMARE))
    check("...and says the League has to fall on Normal first",
          "Normal" in SD.lock_reason(prof, 1, SD.NIGHTMARE),
          SD.lock_reason(prof, 1, SD.NIGHTMARE))
    for n in range(2, 9):
        SD.record_clear(prof, SD.NORMAL, n)
    check("eight Normal clears completes the League",
          SD.normal_complete(prof))
    check("...which is what opens Nightmare battle 1",
          SD.is_unlocked(prof, 1, SD.NIGHTMARE))
    check("Nightmare then runs its own chain — battle 2 still shut",
          not SD.is_unlocked(prof, 2, SD.NIGHTMARE))
    check("`next_battle` points at the first uncleared one",
          SD.next_battle(prof, SD.NIGHTMARE) == 1
          and SD.next_battle(prof, SD.NORMAL) is None)
    check("Normal progress and Nightmare progress are separate keys",
          SD.cleared(prof, SD.NORMAL) != SD.cleared(prof, SD.NIGHTMARE))
    check("the old campaign's profile keys are left alone",
          "story_cleared" not in prof and "story_stats" not in prof)

    # ── 3. the PvE hooks default to exactly today's PvP ──────────────────────
    print("\n── 3. the three PvE parameters change nothing by default ────────")
    sig = inspect.signature(BattleSession.__init__).parameters
    check("BattleSession takes npc_controller, payout and spend_energy",
          {"npc_controller", "payout", "spend_energy"} <= set(sig))
    check("npc_controller defaults to None — no PvP battle has one",
          sig["npc_controller"].default is None)
    check("payout defaults to True — PvP still pays",
          sig["payout"].default is True)
    check("spend_energy defaults to None, i.e. 'follow ranked'",
          sig["spend_energy"].default is None)
    isrc = inspect.getsource(BattleSession.__init__)
    check("...and that default really resolves to `ranked`",
          "ranked if spend_energy is None else spend_energy" in isrc)

    player = FakePlayer(HUMAN_ID, "Tester")
    seed_profile()
    pvp_other = FakePlayer(HUMAN_ID + 1, "Rival")
    seed_profile(HUMAN_ID + 1, "Storm Spriggan")
    pb, _pg = levelled("Void Longinus", 60)
    ob, _og = levelled("Storm Spriggan", 60)
    plain = await BattleSession.create(bot=None, channel=FakeChannel(), p1=player,
                                       p2=pvp_other, blade1=pb, blade2=ob)
    check("a plain PvP session has no NPC and pays",
          plain.npc_controller is None and plain.payout is True)
    check("...and spends no energy, because it is not ranked",
          plain.spend_energy is False)
    check("_is_npc is False for both sides of a PvP battle",
          not plain._is_npc(player.id) and not plain._is_npc(pvp_other.id))

    # ── 4. a real battle, with everything the old engine did not have ────────
    print("\n── 4. a real School League battle ──────────────────────────────")
    seed_profile()
    pblade, pgain = levelled("Void Longinus", SD.OPPONENT_LEVEL)
    nblade, ngain = levelled("Wild Wyvern", SD.OPPONENT_LEVEL)
    s, ch, npc, ctrl = await build_session(player, pblade, nblade, ngain,
                                     "nightmare", seed=3, battle_no=5)
    brain = Brain("elite", 7)
    turns = await drive(s, str(player.id), brain)
    blob = ch.text()
    check("the battle actually finished", s.finished, turns)
    # `_resolve_round_inner` catches everything and posts this instead. A round
    # that raised therefore LOOKS like a round that happened, which is exactly
    # how an empty NPC profile hid a KeyError on every battle the player won.
    check("no round fell into the session's 'Battle Error' fallback",
          "Battle Error" not in blob,
          blob[blob.find("Battle Error"):][:200])
    check("stability is tracked and moves", "Stability -" in blob)
    check("blade abilities fire — the attacker's",
          "Longinus Strike" in blob, blob[:0])
    check("...and the opponent's, which is an NPC's blade",
          "Wyvern Wall" in blob)
    check("the type system is in play", "Type " in blob)
    check("HP really moved", s.hp[str(player.id)] != s.max_hp_per_player[
        str(player.id)] or s.hp[str(npc.id)] != s.max_hp_per_player[
        str(npc.id)])
    check("both sides paid stamina through the real manager",
          all(v <= SM.max_stamina_for(200) + 50
              for v in s.stamina_manager.stamina.values()))

    panel = SESSION._InChannelControlPanel(s)
    labels = [getattr(c, "label", "") for c in panel.children]
    check("the move panel has five buttons", len(labels) == 5, labels)
    check("...and one of them is the Charge button asked for",
          any("Charge" in x for x in labels), labels)
    check("...with the 🔋 emoji, not a text-only button",
          any("🔋" in x for x in labels), labels)
    check("the other four are Attack, Defense, Stamina and SPECIAL",
          all(any(w in x for x in labels)
              for w in ("Attack", "Defense", "Stamina", "SPECIAL")), labels)

    # ── 5. the NPC never becomes a player ────────────────────────────────────
    print("\n── 5. the opponent never reaches the store ─────────────────────")
    seed_profile()
    STORE.clear_log()
    s2, ch2, npc2, _c2 = await build_session(player, pblade, nblade, ngain,
                                       "elite", seed=11, battle_no=2)
    await drive(s2, str(player.id), Brain("elite", 11))
    check("the NPC id was never written to the store",
          str(npc2.id) not in STORE.writes, STORE.writes)
    check("...and was never read from it either",
          str(npc2.id) not in STORE.reads, STORE.reads[:8])
    check("...so no profile exists for it", not STORE.has(npc2.id))
    check("the human's own profile WAS reachable — the store is live",
          str(player.id) in STORE.reads)
    check("_is_npc identifies the opponent and not the player",
          s2._is_npc(npc2.id) and not s2._is_npc(player.id))
    stub = s2._profile_for(npc2.id)
    check("_profile_for hands the NPC a throwaway, not a stored row",
          stub.get("wins") == 0 and stub.get("losses") == 0
          and stub.get("coins") == 0 and not STORE.has(npc2.id), stub)
    check("...shaped like a profile, so _end_battle cannot KeyError on a win",
          {"wins", "losses", "coins", "rank_score", "bey_progress"}
          <= set(stub))
    stub["coins"] = 999999
    check("...and mutating it changes nothing anyone can see",
          s2._profile_for(npc2.id)["coins"] == 0)
    # v1.25: an opponent DOES get avatar bonuses now — its blader card. What
    # must stay true is that it reads them off the card and never off a
    # profile, which is what the store-log checks above prove.
    check("the NPC's bonuses come from its blader card",
          s2._bonuses_for(npc2.id).has_any_bonus
          and s2._avatar_card_for(npc2.id).get("rarity") == "Blader",
          s2._avatar_card_for(npc2.id).get("id"))
    check("...and an opponent with no card configured still gets nothing",
          not SESSION.BattleSession._bonuses_for(
              _NoCardSession(), 999).has_any_bonus)
    check("the NPC gets no trainer/mastery multiplier",
          s2._stat_mult_for(npc2.id) == 1.0)
    check("the NPC's blade level is the League's, not a profile's",
          s2.bey_levels[str(npc2.id)] == SD.OPPONENT_LEVEL,
          s2.bey_levels.get(str(npc2.id)))

    # ── 6. payout=False really skips the payout ──────────────────────────────
    print("\n── 6. Story pays once, PvP pays nothing ────────────────────────")
    # BOTH outcomes, forced. Driving one battle and hoping it went the right
    # way is how this check goes vacuous: if the human happens to lose, the
    # winner branch never runs for them and a broken payout gate looks fine.
    for who_wins in ("human", "npc"):
        seed_profile(coins=500, xp=1000, wins=3, losses=1)
        before = copy.deepcopy(STORE.data[str(player.id)])
        s3, _ch3, npc3, _c3 = await build_session(player, pblade, nblade, ngain,
                                            "elite", seed=21, battle_no=3)
        loser = npc3 if who_wins == "human" else player
        s3.hp[str(loser.id)] = 0
        await s3._end_battle()
        after = STORE.data[str(player.id)]
        check(f"[{who_wins} wins] the battle reached a result", s3.finished)
        check(f"[{who_wins} wins] no PvP coins were paid",
              after["coins"] == before["coins"],
              (before["coins"], after["coins"]))
        check(f"[{who_wins} wins] no trainer XP was granted",
              after["xp"] == before["xp"], (before["xp"], after["xp"]))
        check(f"[{who_wins} wins] the win/loss record did not move",
              (after["wins"], after["losses"])
              == (before["wins"], before["losses"]),
              (before["wins"], before["losses"],
               after["wins"], after["losses"]))
        check(f"[{who_wins} wins] and the NPC still has no profile",
              not STORE.has(npc3.id))
    # and one full driven battle, so the forced-HP shortcut above is not the
    # only thing this section ever exercises
    seed_profile(coins=500, xp=1000)
    before = copy.deepcopy(STORE.data[str(player.id)])
    s3b, _c3b, npc3b, _cc = await build_session(player, pblade, nblade, ngain,
                                          "elite", seed=22, battle_no=3)
    await drive(s3b, str(player.id), Brain("elite", 22))
    check("a battle played out end to end also paid nothing",
          STORE.data[str(player.id)]["coins"] == before["coins"]
          and STORE.data[str(player.id)]["xp"] == before["xp"])
    check("...and did not error its way there either",
          "Battle Error" not in _c3b.text())

    # the win path, forced and observed: it is the one an empty NPC profile
    # used to break, and a driven battle may not happen to reach it.
    seed_profile()
    s3c, ch3c, npc3c, _cc2 = await build_session(player, pblade, nblade, ngain,
                                           "elite", seed=23, battle_no=3)
    s3c.hp[str(npc3c.id)] = 0
    await s3c._end_battle()
    check("a Story win renders a result card naming the human",
          any(player.display_name in (getattr(m.get("embed"), "title", "") or "")
              for m in ch3c.sent), [str(getattr(m.get("embed"), "title", ""))
                                    for m in ch3c.sent])
    check("...and records the human as the winner",
          s3c.winner_id == player.id, s3c.winner_id)
    esrc = inspect.getsource(BattleSession._end_battle)
    check("the payout is gated on the flag, not on the id",
          "if self.payout:" in esrc)
    check("the draw branch skips the NPC as well as the payout",
          "if not self.payout or self._is_npc(pid):" in esrc)
    check("the battle-end dispatch is gated too",
          "if not self.payout:" in esrc and "return" in esrc)
    check("quests/mastery/clan-war events stay off for PvE",
          __import__("cogs.story.story_cog", fromlist=["x"]
                     ).DISPATCH_BATTLE_EVENTS is False)

    # a PvP session, same code, DOES pay — otherwise the check above is vacuous
    seed_profile(HUMAN_ID + 1, "Storm Spriggan", coins=0)
    seed_profile(coins=0)
    pvp = await BattleSession.create(bot=None, channel=FakeChannel(), p1=player,
                                     p2=pvp_other, blade1=pblade, blade2=nblade)
    pvp.hp[str(pvp_other.id)] = 0
    await pvp._end_battle()
    check("...and the same _end_battle with payout=True DOES pay",
          STORE.data[str(player.id)]["coins"] > 0,
          STORE.data[str(player.id)]["coins"])

    # ── 7. every AI move is affordable in the REAL session ───────────────────
    print("\n── 7. the stale stamina table cannot bite ──────────────────────")
    check("boss_ai carries its own, different cost table",
          ai.STAMINA_COST != SM.STAMINA_COST,
          (ai.STAMINA_COST, SM.STAMINA_COST))
    check("story_ai prices moves off the REAL table",
          SA.REAL_COST is SM.STAMINA_COST)
    asrc = inspect.getsource(SA)
    check("...and never imports boss_ai's copy",
          "boss_ai.STAMINA_COST" not in asrc.split('"""', 2)[-1])

    illegal = []
    for rung in (SD.AI_RUNG[SD.NORMAL], SD.AI_RUNG[SD.NIGHTMARE]):
        for seed in range(3):
            s4, _c4, npc4, ctrl4 = await build_session(
                player, pblade, nblade, ngain, rung, seed=seed, battle_no=4)
            nkey = str(npc4.id)
            await s4._prime_npc_move()
            t = 0
            while not s4.finished and t < 60:
                t += 1
                mv = s4.moves[nkey]
                if mv is not None and not SA.affordable(s4, nkey, mv):
                    illegal.append((rung, seed, t, mv,
                                    s4.stamina_manager.stamina[nkey]))
                s4.moves[str(player.id)] = Brain("elite", seed)(s4,
                                                                str(player.id))
                await s4._resolve_round()
    check("across six full battles the opponent never picked a move it "
          "could not pay for", not illegal, illegal[:3])

    # the clamp is what makes that true — prove it fires
    s5, _c5, npc5, ctrl5 = await build_session(player, pblade, nblade, ngain,
                                         "elite", seed=99, battle_no=1)
    nkey5 = str(npc5.id)
    s5.stamina_manager.stamina[nkey5] = 0.0
    s5.stamina_manager.gauge[nkey5] = 0.0
    check("with an empty bar, only Stamina is legal",
          SA.legal_moves(s5, nkey5) == [MOVE_STAMINA],
          SA.legal_moves(s5, nkey5))
    check("...and the clamp turns an unaffordable Special into it",
          ctrl5.clamp(s5, MOVE_SPECIAL) == MOVE_STAMINA)
    check("...while an affordable move is passed straight through",
          ctrl5.clamp(s5, MOVE_STAMINA) == MOVE_STAMINA)
    s5.stamina_manager.stamina[nkey5] = 30.0
    check("a full bar makes Attack legal again",
          ctrl5.clamp(s5, MOVE_ATTACK) == MOVE_ATTACK)

    # ── 8. avatar skill energy: once per BATTLE, not per round ──────────────
    print("\n── 8. avatar skill energy is spent for the match ───────────────")
    from cogs.avatar import avatar_skills as AS
    check("Story asks for energy to be spent with ranked OFF",
          "spend_energy=True" in inspect.getsource(LeagueMatch._rounds)
          and "ranked=False" in inspect.getsource(LeagueMatch._rounds))
    check("the session routes that flag into begin_battle_for",
          "ranked=_spend_energy" in
          inspect.getsource(BattleSession.create)
          and "_spend_energy = bool(ranked if spend_energy is None "
              "else spend_energy)" in inspect.getsource(BattleSession.create))
    check("...and into end_battle_for, so casual refills and Story does not",
          "ranked=self.spend_energy" in
          inspect.getsource(BattleSession._release_skills))
    check("the match ends the energy lifecycle exactly once, in a finally",
          "end_match_for" in inspect.getsource(LeagueMatch.run)
          and "finally:" in inspect.getsource(LeagueMatch.run))
    check("_commit_skill skips the NPC entirely",
          "_is_npc" in inspect.getsource(BattleSession._commit_skill))
    check("_release_skills skips it too",
          "_is_npc" in inspect.getsource(BattleSession._release_skills))
    check("AS.end_match_for exists to be called",
          callable(getattr(AS, "end_match_for", None)))

    # ── 9. the Victory-Point loop ────────────────────────────────────────────
    print("\n── 9. first to three Victory Points ────────────────────────────")
    check("the target is three", SD.VICTORY_TARGET == 3, SD.VICTORY_TARGET)
    check("...and it is Story's own constant, not the ranked ladder's",
          "RK.MATCH_TARGET" not in inspect.getsource(SMatch)
          and "SD.VICTORY_TARGET" in inspect.getsource(LeagueMatch.decided))
    sd_imports = [ln for ln in inspect.getsource(SD).splitlines()
                  if ln.startswith(("import ", "from "))]
    check("...so the two can be changed apart even while they agree at 3",
          not any("ranked" in ln for ln in sd_imports), sd_imports)
    check("how a point is scored IS the ranked rule",
          "RK.finish_points" in inspect.getsource(LeagueMatch._rounds))
    check("burst 2, survival 1, ring-out 1",
          (RK.finish_points(RK.FINISH_BURST),
           RK.finish_points(RK.FINISH_SURVIVAL),
           RK.finish_points(RK.FINISH_RINGOUT)) == (2, 1, 1))
    check("a draw scores nothing for either side",
          "no points" in inspect.getsource(LeagueMatch._rounds))

    seed_profile()
    pts, rounds, turns, finishes = await play_match(
        player, pblade, nblade, ngain, "elite", seed=5, battle_no=5)
    check("a real match reached the target",
          max(pts.values()) >= SD.VICTORY_TARGET, pts)
    check("...and stopped there rather than playing on",
          rounds <= SD.MAX_ROUNDS, (pts, rounds))
    check("the loser did not also reach it",
          min(pts.values()) < SD.VICTORY_TARGET, pts)
    check("every finish it recorded is one the ranked table knows",
          all(f in RK.FINISH_POINTS for f in finishes), finishes)
    check("the points add up from the finishes that happened",
          sum(RK.finish_points(f) for f in finishes) == sum(pts.values()),
          (finishes, pts))

    m = LeagueMatch(None, FakeChannel(), player, pblade, 1, SD.NORMAL)
    m.points = {"player": 3, "npc": 1}
    check("`decided` is true at the target", m.decided())
    m.points = {"player": 2, "npc": 2}
    check("...and false below it", not m.decided())

    # ── 10. difficulty is decisions, not stat inflation ─────────────────────
    print("\n── 10. Normal and Nightmare differ by AI, not by stats ─────────")
    check("Normal is the IQ 3 rung", ai.DIFFICULTY[SD.AI_RUNG[SD.NORMAL]]["iq"]
          == 3)
    check("Nightmare is IQ 5", ai.DIFFICULTY[SD.AI_RUNG[SD.NIGHTMARE]]["iq"]
          == 5)
    check("both difficulties field the opponent at the same level",
          SD.OPPONENT_LEVEL == 100)
    dsrc = inspect.getsource(SD)
    for word in ("stat_mult", "hp_mult", "damage_mult", "NIGHTMARE_STAT"):
        check(f"...and no `{word}` anywhere in the League data",
              word not in dsrc)
    n_blade, n_gain = levelled(SD.SCHOOL_LEAGUE[0]["blade"])
    a1, _, _, c1 = await build_session(player, pblade, n_blade, n_gain, "elite")
    a2, _, _, c2 = await build_session(player, pblade, n_blade, n_gain, "nightmare")
    check("the two rungs produce identical opponent stats",
          a1.battle_stats[str(c1.key)] == a2.battle_stats[str(c2.key)])
    check("...identical HP",
          a1.max_hp_per_player[str(c1.key)]
          == a2.max_hp_per_player[str(c2.key)])
    check("...and identical stamina",
          a1.stamina_manager.stamina[str(c1.key)]
          == a2.stamina_manager.stamina[str(c2.key)])

    check("the ring-out guard is Nightmare's, not Normal's",
          SA.STABILITY_GUARD_RUNGS == frozenset({"nightmare"}),
          SA.STABILITY_GUARD_RUNGS)
    check("...so the Normal opponent does not use it", not c1.guards_stability)
    check("...and the Nightmare one does", c2.guards_stability)
    check("its numbers come from the real stability table, not a copy",
          SA._SELF_STABILITY[MOVE_ATTACK] == -10
          and SA._SELF_STABILITY[MOVE_STAMINA] == 25,
          SA._SELF_STABILITY)

    # The guard is gated on the SAME `is_effects_active` the engine gates the
    # stability cost on (attack_manager.py:303). So it needs a matchup where
    # stability is really in play for the opponent — an Attack blade against a
    # Stamina one — and the inert case is worth asserting in its own right.
    live_blade, live_gain = levelled("Omni Odax")             # Attack
    stam_blade, _sg = levelled("Rising Ragnaruk")             # Stamina
    g, _cg, gnpc, gctrl = await build_session(player, stam_blade, live_blade,
                                        live_gain, "nightmare", battle_no=6)
    gk = str(gnpc.id)
    ok = str(player.id)
    legal = [MOVE_ATTACK, MOVE_DEFENSE, MOVE_STAMINA]
    check("stability really is live for this matchup",
          g.stability_manager.is_effects_active(gk, ok))
    g.stability_manager.stability[gk] = 8       # one Attack from a ring-out
    check("with 8 stability left, Attack would ring the opponent out",
          SA.stability_after(g, gk, MOVE_ATTACK) <= 0)
    check("...so the guard swaps it for something survivable",
          SA.guard_ringout(g, gk, ok, MOVE_ATTACK, legal) != MOVE_ATTACK)
    check("...and the swap is itself survivable",
          SA.stability_after(
              g, gk, SA.guard_ringout(g, gk, ok, MOVE_ATTACK, legal)) > 0)
    g.stability_manager.stability[gk] = 90
    check("with 90 left it leaves the search's choice alone",
          SA.guard_ringout(g, gk, ok, MOVE_ATTACK, legal) == MOVE_ATTACK)

    # inert matchup: the engine will not charge the stability, so neither may
    # the guard predict it — otherwise Nightmare plays scared for no reason.
    inert = str(c1.key)                                       # Ragnaruk, Stamina
    check("...and when stability is inert for the matchup, so is the guard",
          not a1.stability_manager.is_effects_active(inert, str(player.id))
          and SA.guard_ringout(a1, inert, str(player.id), MOVE_ATTACK,
                               legal) == MOVE_ATTACK)

    # Does the guard change what the opponent actually PLAYS? Asked of
    # `choose` — the real entry point — from a forced position, over 20 seeds
    # per rung, and NOT inferred from a win rate. A win-rate gap needs a sample
    # in the hundreds to mean anything, so a suite that asserts on one fails
    # builds at random; this is deterministic and it separates the two rungs
    # completely.
    picks = {}
    for rung in ("elite", "nightmare"):
        chosen, unsafe = [], 0
        for seed in range(20):
            gs, _gc, gnpc2, gctrl2 = await build_session(
                player, stam_blade, live_blade, live_gain, rung,
                seed=seed, battle_no=6)
            gk2 = str(gnpc2.id)
            gs.stability_manager.stability[gk2] = 5      # one hit from out
            mv = await gctrl2.choose(gs)
            chosen.append(mv)
            if SA.stability_after(gs, gk2, mv) <= 0:
                unsafe += 1
        picks[rung] = (chosen, unsafe)
    check("on 5 stability the Nightmare opponent never plays itself off the "
          "ring, over 20 seeds",
          picks["nightmare"][1] == 0, picks["nightmare"][1])
    check("...and the Normal one does, most of the time — so the guard is "
          "what makes the difference, not the seed",
          picks["elite"][1] >= 15, picks["elite"][1])
    check("...and what Nightmare plays instead is still a legal move",
          set(picks["nightmare"][0]) <= {MOVE_ATTACK, MOVE_DEFENSE,
                                         MOVE_STAMINA, MOVE_CHARGE,
                                         MOVE_SPECIAL},
          set(picks["nightmare"][0]))

    # ── 11. the cog surface ──────────────────────────────────────────────────
    print("\n── 11. the commands and the picker ─────────────────────────────")
    import cogs.story.story_cog as SC
    for name in ("story", "storymap", "storyinfo", "storystats"):
        check(f"`;{name}` is still a command",
              any(getattr(v, "name", None) == name
                  for v in vars(SC.StoryCog).values()))
    check("`;story` with no argument opens the chapter picker",
          "ChapterPickView" in inspect.getsource(SC.StoryCog.story.callback))
    check("the picker offers the School and the Xender Dojo",
          {c["key"] for c in SD.CHAPTERS} == {SD.SCHOOL, SD.XENDER})
    check("...the School is playable",
          next(c for c in SD.CHAPTERS if c["key"] == SD.SCHOOL)["available"])
    check("...and the Dojo is shown as coming soon, not hidden",
          not next(c for c in SD.CHAPTERS
                   if c["key"] == SD.XENDER)["available"]
          and "Coming Soon" in next(c for c in SD.CHAPTERS
                                    if c["key"] == SD.XENDER)["name"])
    check("`/story` reaches that same picker through the cog",
          "picker(" in inspect.getsource(
              __import__("cogs.ui.panels", fromlist=["x"])
              .PanelCommands.story.callback))
    check("StoryCog.picker exists for it to call",
          callable(getattr(SC.StoryCog, "picker", None)))
    check("a battle that cannot be built is reported, not crashed into",
          "isn't in the roster" in inspect.getsource(SC.StoryCog._fight))
    check("the reward is only paid on a FIRST clear",
          "if first:" in inspect.getsource(SC.StoryCog._finish))

    # the deleted engine really is gone
    for dead in ("story_engine", "story_avatar"):
        check(f"the old {dead}.py is deleted",
              not os.path.exists(os.path.join(ROOT, "cogs", "story",
                                              f"{dead}.py")))

    # ── 12. every menu click acknowledges before it touches the store ───────
    print("\n── 12. the panel acks before it does any I/O ───────────────────")
    #
    # Reported live: picking a chapter gave "BEYCBOT didn't respond in time".
    # Discord kills an interaction three seconds after the CLICK, not after the
    # first await, and every one of these callbacks read the player's profile
    # before acknowledging. On a remote store that is the whole budget.
    #
    # So this records the ORDER of two things — acks and store reads — and
    # asserts the ack comes first. A source-level "does it call defer" check
    # would pass on code that defers in the wrong place.
    seed_profile()

    class Recorder:
        """Interleaves ack events with store reads on one timeline."""

        def __init__(self):
            self.events: list[str] = []

        def hook_store(self):
            real = STORE.get_one
            rec = self

            def get_one(uid, _real=real):
                rec.events.append("READ")
                return _real(uid)

            STORE.get_one = get_one
            return real

    class FakeResponse:
        def __init__(self, rec):
            self.rec = rec
            self._done = False

        def is_done(self):
            return self._done

        async def defer(self, *a, **kw):
            self.rec.events.append("ACK:defer")
            self._done = True

        async def send_message(self, *a, **kw):
            self.rec.events.append("ACK:send")
            self._done = True

        async def edit_message(self, *a, **kw):
            self.rec.events.append("ACK:edit")
            self._done = True

    class FakeFollowup:
        def __init__(self, rec):
            self.rec = rec

        async def send(self, *a, **kw):
            self.rec.events.append("followup")

    class FakeInteraction:
        def __init__(self, rec, user):
            self.rec = rec
            self.user = user
            self.response = FakeResponse(rec)
            self.followup = FakeFollowup(rec)
            self.channel = FakeChannel()

        async def edit_original_response(self, *a, **kw):
            self.rec.events.append("edit_original")

    import cogs.story.story_cog as SCOG

    async def timeline(make_and_click):
        rec = Recorder()
        real_get_one = rec.hook_store()
        try:
            await make_and_click(rec)
        finally:
            STORE.get_one = real_get_one
        return rec.events

    def first_ack_before_first_read(events) -> bool:
        acks = [i for i, e in enumerate(events) if e.startswith("ACK")]
        reads = [i for i, e in enumerate(events) if e == "READ"]
        if not reads:
            return True                      # no I/O at all is also fine
        return bool(acks) and acks[0] < reads[0]

    async def click_chapter(rec):
        view = SCOG.ChapterPickView(cog_stub, player)
        sel = view.children[0]
        sel._values = [SD.SCHOOL]
        await sel.callback(FakeInteraction(rec, player))

    class _CogStub:
        _active: set = set()

        def release(self, uid):
            pass

    cog_stub = _CogStub()
    ev = await timeline(click_chapter)
    check("picking a chapter reads the profile at all — otherwise this "
          "proves nothing", "READ" in ev, ev)
    check("...and acknowledges BEFORE that read",
          first_ack_before_first_read(ev), ev)

    async def click_difficulty(rec):
        view = await SCOG.LeagueView.create(cog_stub, player)
        rec.events.clear()                   # the constructor's read is not a click
        btn = next(c for c in view.children
                   if isinstance(c, SCOG.DifficultyButton)
                   and c.difficulty == SD.NIGHTMARE)
        await btn.callback(FakeInteraction(rec, player))

    ev = await timeline(click_difficulty)
    check("the difficulty toggle acknowledges before its read",
          first_ack_before_first_read(ev), ev)

    real_launch = SCOG.StoryCog.launch

    async def click_battle(rec):
        view = await SCOG.LeagueView.create(cog_stub, player)
        rec.events.clear()
        sel = next(c for c in view.children
                   if isinstance(c, SCOG.BattleSelect))
        sel._values = ["1"]

        async def fake_launch(self, interaction, member, n, difficulty):
            # the real gate, without starting a battle
            await interaction.response.defer()
            await SCOG.StoryCog._can_fight(self, member.id, n, difficulty)

        view.cog = SCOG.StoryCog.__new__(SCOG.StoryCog)
        view.cog._active = set()
        view.cog.launch = fake_launch.__get__(view.cog)
        await sel.callback(FakeInteraction(rec, player))

    ev = await timeline(click_battle)
    check("choosing a battle acknowledges before the unlock check reads",
          first_ack_before_first_read(ev), ev)

    launch_src = inspect.getsource(real_launch)
    # `self._can_fight(`, not `_can_fight` — the comment above the defer names
    # it too, and matching that would compare the wrong two positions.
    check("the real `launch` defers before it calls the gate",
          launch_src.index("response.defer")
          < launch_src.index("self._can_fight("),
          (launch_src.index("response.defer"),
           launch_src.index("self._can_fight(")))
    check("...and reports a refusal through followup, since the response is "
          "already spent",
          "followup.send" in inspect.getsource(real_launch))
    check("a League battle runs as its own task, so a fight does not block "
          "the panel for minutes",
          "create_task" in inspect.getsource(real_launch))

    lv_src = inspect.getsource(SCOG.LeagueView)
    check("the League view reads the profile once per render, not twice",
          lv_src.count("get_user(") == 1          # refresh() only —
          # __init__ can't await (BUG-02); construction goes through create()
          and "profile = self.profile" in lv_src, lv_src.count("get_user("))

    # ── 13. the blade the player actually fights with ───────────────────────
    print("\n── 13. the REAL blade path, driven ─────────────────────────────")
    #
    # This section exists because 155 checks passed over a line that raised
    # every single time it ran. Every battle above builds the player's blade
    # with `levelled()` — the harness's own helper — so `story_cog._fight`'s
    # construction was never once executed by the suite, and a `TypeError`
    # swallowed by a bare `except` handed four releases of players a blade at
    # PRINTED stats while its HP and Special were correctly levelled.
    #
    # So: run what `_fight` runs, and compare against the level curve.
    import cogs.story.story_cog as SC13
    from utils.database import get_beyblade as _get_blade

    LVL = 50
    BLADE = "Storm Spriggan"
    want = BL.stats_at(_get_blade(BLADE), LVL, {})

    prof = seed_profile(blade=BLADE)
    prof["bey_progress"] = {BLADE: {"xp": BL.xp_for_level(LVL)}}
    prof["inventory"] = [BLADE]
    STORE.data[str(HUMAN_ID)] = copy.deepcopy(prof)

    built, _copy = await SC13.player_blade(HUMAN_ID)
    check("a player's blade is built at THEIR level, not its printed stats",
          built is not None
          and all(built["stats"][k] == want[k]
                  for k in ("attack", "defense", "stamina")),
          (built or {}).get("stats"))
    check("...and the printed stats are not what comes back",
          built["stats"]["attack"] != _get_blade(BLADE)["stats"]["attack"],
          built["stats"]["attack"])

    fsrc = inspect.getsource(SC13.StoryCog._fight)
    check("`_fight` no longer swallows a failure to build the blade — that is "
          "what turned a TypeError into a silent quarter-strength nerf",
          "bey_level_and_stats" not in fsrc)
    check("it goes through the same helper PvP and the tournament use",
          "_apply_parts" in inspect.getsource(SC13.player_blade))

    # …and it has to survive into the session, which is where it was invisible
    nb13, ng13 = levelled("Rising Ragnaruk", 100)
    s13, _c13, npc13, _x13 = await build_session(player, built, nb13, ng13, "elite",
                                           seed=1, battle_no=1)
    pk13 = str(player.id)
    check("the levelled stats reach the battle itself",
          all(s13.battle_stats[pk13][k] == want[k]
              for k in ("attack", "defense", "stamina")),
          s13.battle_stats[pk13])
    check("...and the stamina bar follows the stamina stat up with it",
          abs(s13.stamina_manager.cap_for(pk13)
              - SM.max_stamina_for(want["stamina"])) < 0.01,
          (s13.stamina_manager.cap_for(pk13),
           SM.max_stamina_for(want["stamina"])))
    check("...which is a bigger bar than the printed blade would have given",
          s13.stamina_manager.cap_for(pk13)
          > SM.max_stamina_for(_get_blade(BLADE)["stats"]["stamina"]))

    # a dual-spin blade arrives with its mode resolved
    from utils.spin_mode import is_dual as _is_dual
    duals = [b["name"] for b in
             [_get_blade(n) for n in ("Master Diabolos", "Janus Bahamut",
                                      "Cho-Z Achilles")] if _is_dual(b)]
    check("the three dual-spin blades are still dual", len(duals) == 3, duals)
    for dname in duals:
        dprof = seed_profile(blade=dname)
        dprof["inventory"] = [dname]
        STORE.data[str(HUMAN_ID)] = copy.deepcopy(dprof)
        dbuilt, _dc = await SC13.player_blade(HUMAN_ID)
        check(f"{dname} reaches Story with its spin mode resolved",
              (dbuilt or {}).get("active_spin_mode"),
              (dbuilt or {}).get("active_spin_mode"))

    # a boss copy is already resolved and must not be levelled a second time
    check("a boss copy is passed through untouched, as PvP does",
          "if copy" in inspect.getsource(SC13.player_blade)
          or "copy else" in inspect.getsource(SC13.player_blade),
          inspect.getsource(SC13.player_blade))

    # ── the skill picker, and the gate ──────────────────────────────────────
    print("\n   the avatar skill picker, and the equipped-blade gate:")
    from cogs.battle import skill_prompt as SP13
    check("Story offers the skill picker at all — it never used to",
          "resolve_avatar_skills_solo" in inspect.getsource(SC13.StoryCog._fight))
    check("...through the SOLO entry point, because `participants()` resolves "
          "a player through get_user and would create a profile for the NPC",
          callable(getattr(SP13, "resolve_avatar_skills_solo", None)))
    solo = inspect.getsource(SP13.resolve_avatar_skills_solo)
    check("...and it is handed one player, not a pair",
          "participants((member,))" in solo, solo[:0])
    check("it is asked once per BATTLE, before the match runs",
          inspect.getsource(SC13.StoryCog._fight).index(
              "resolve_avatar_skills_solo")
          < inspect.getsource(SC13.StoryCog._fight).index("LeagueMatch("))

    # the prompt must never touch the opponent's id
    seed_profile()
    STORE.clear_log()
    entries = await SP13.participants((player,))
    check("asking for the human's card reads the human and nobody else",
          all(r == str(player.id) for r in STORE.reads), STORE.reads)
    check("a player with no avatar gets no prompt at all — silence is the "
          "common case", entries == [], entries)

    from cogs.battle.boss.boss_copy import has_equipped_blade
    check("the equipped-blade gate asks what actually arms the player",
          "has_equipped_blade" in inspect.getsource(SC13.StoryCog._can_fight))
    check("...and PvP asks the same question",
          "has_equipped_blade" in inspect.getsource(
              __import__("cogs.battle.battle", fromlist=["x"])))
    STORE.data["7770000000000001"] = {"user_id": "7770000000000001",
                                      "active_beyblade": "Storm Spriggan"}
    STORE.data["7770000000000002"] = {"user_id": "7770000000000002",
                                      "active_beyblade": None}
    check("a player with a blade passes the gate",
          await has_equipped_blade(7770000000000001))
    check("...and a player with nothing at all still does not",
          not await has_equipped_blade(7770000000000002))

    seed_profile()          # leave the store as section 14 expects it

    # ── 14. win rates, measured ──────────────────────────────────────────────
    print("\n── 14. win rates, measured rather than assumed ─────────────────")
    # This section REPORTS. It asserts nothing about the numbers, and that is
    # deliberate — twice now a threshold here has failed a build for a reason
    # that was not a regression:
    #
    #   * `nightmare < normal` was a coin flip at the default sample (81.2%
    #     vs 81.2% at --trials 2);
    #   * `normal > 0.35` broke once the v1.25 blader cards landed, because
    #     the real figure is 38.3% at n=128 — right on top of the threshold,
    #     so a small sample lands either side of it. It came out 12.5% on a
    #     run of eight.
    #
    # A win rate is a BALANCE measurement and balance is a design decision,
    # not a correctness property. What must not regress is checked where it
    # can be checked deterministically: section 13 pins that the player's
    # blade arrives at its real level, and section 10 pins that Nightmare
    # plays differently from Normal.
    normal, nightmare = await win_rate_table(trials)
    print(f"\n     Normal {100 * normal:.1f}%  ·  Nightmare "
          f"{100 * nightmare:.1f}%  (n={trials * SD.total_battles()} each)")
    print("     Needs --trials 20 to mean anything. Measured at 16, Normal,")
    print("     with the v1.22 blade bug vs fixed, and the v1.25 cards off "
          "vs on:")
    print("       blade bug, no cards   25.0%   <- what v1.25 shipped on")
    print("       blade bug, cards      11.7%")
    print("       blade FIXED, no cards 90.6%   <- the fix is worth ~65 points")
    print("       blade FIXED, cards    38.3%   <- what ships now")
    print("     Battles 3, 4, 6 and 8 sit at 6-12% in the last column: the")
    print("     card statlines are still a wall, which is an open question")
    print("     for the owner, not a regression.")


async def win_rate_table(trials: int) -> tuple[float, float]:
    """Play the whole League on both difficulties and print what happened."""
    player = FakePlayer(HUMAN_ID, "Tester")
    seed_profile()
    pname = "Void Longinus"
    pblade, _ = levelled(pname, SD.OPPONENT_LEVEL)
    print(f"\n  player: {pname} at level {SD.OPPONENT_LEVEL}, no avatar, no "
          f"parts · human plays at IQ 3 · {trials} matches per cell")
    print(f"  {'battle':<26}{'Normal IQ3':>12}{'Nightmare IQ5':>15}"
          f"{'turns':>8}")
    tot = {SD.NORMAL: 0, SD.NIGHTMARE: 0}
    for e in SD.SCHOOL_LEAGUE:
        nb, ng = levelled(e["blade"], SD.OPPONENT_LEVEL)
        cell, med = {}, 0
        for diff in (SD.NORMAL, SD.NIGHTMARE):
            wins, turns = 0, []
            for t in range(trials):
                pts, _r, tn, _f = await play_match(
                    player, pblade, nb, ng, SD.AI_RUNG[diff], seed=t + 1,
                    battle_no=e["n"])
                wins += pts["p"] > pts["n"]
                turns.append(tn)
            cell[diff] = wins
            tot[diff] += wins
            if diff == SD.NORMAL:
                med = statistics.median(turns)
        print(f"  {e['n']}. {e['blade']:<23}"
              f"{100 * cell[SD.NORMAL] / trials:>11.0f}%"
              f"{100 * cell[SD.NIGHTMARE] / trials:>14.0f}%{med:>8.0f}")
    n = trials * SD.total_battles()
    normal = tot[SD.NORMAL] / n
    nightmare = tot[SD.NIGHTMARE] / n
    print(f"  {'OVERALL':<26}{100 * normal:>11.1f}%{100 * nightmare:>14.1f}%"
          f"      (n={n} each)")
    return normal, nightmare


if __name__ == "__main__":
    sys.exit(main())
