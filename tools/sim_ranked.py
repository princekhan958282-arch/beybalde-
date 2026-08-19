#!/usr/bin/env python3
"""
tools/sim_ranked.py — the ranked ladder.

The rule that everything else hangs off: **a casual battle must not move a
single competitive number.** A ladder that counts friendly matches is not a
ladder — two players can trade wins to farm rank score, and a win rate that
includes practice games measures nothing.

Also covers the verification gate, the five leaderboard categories, and the
owner reset, whose blast radius is every profile in the database and which
therefore must never touch coins, inventory or levels.

Run:  python3 tools/sim_ranked.py
"""
import os
import sys

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


from utils import ranked as RK                        # noqa: E402
from utils.ranks import WIN_SCORE, LOSS_SCORE         # noqa: E402

# The boards that ranked play alone can put you on. `level` and `money` are
# boards too, but they measure playing at all — the default `player()` fixture
# is level 5 with 1,000 coins, so it belongs on both and says nothing about
# whether a CASUAL battle leaked into the ladder, which is what these checks
# are for.
RANKED_BOARDS = ("rank", "winrate", "wins", "streak", "catches")

# Ranked has no settings of its own left to configure — verification and the
# control-server lock both went in v1.18 — so an empty config is the only
# config there is. `build_board` still takes one, because the signature is
# public and half the callers pass it.
CFG_OFF = {RK.CONFIG_KEY: {}}


def player(uid, **kw):
    p = {"user_id": str(uid), "coins": 1000, "inventory": ["A", "B"],
         "wins": 0, "losses": 0, "level": 5, "xp": 400}
    p.update(kw)
    return p


print("\n── 1. a casual battle moves no competitive number ───────────────")
p = player(1)
before = dict(p)
# What session.py does on a CASUAL win: lifetime wins + coins only.
p["wins"] += 1
p["coins"] += 50
check("rank score untouched", RK.rank_score(p) == 0)
check("ranked wins untouched", RK.ranked_wins(p) == 0)
check("ranked losses untouched", RK.ranked_losses(p) == 0)
check("win rate stays 0 with no ranked games", RK.win_rate(p) == 0.0)
check("best streak untouched", RK.best_streak(p) == 0)
check("lifetime `wins` still counts it — profile card and achievements read it",
      p["wins"] == before["wins"] + 1)
check("a casual player is on NO ranked leaderboard",
      all(not RK.build_board([p], k) for k in RANKED_BOARDS if k != "catches"),
      [k for k in RANKED_BOARDS if RK.build_board([p], k)])

print("\n── 2. a ranked battle moves them ────────────────────────────────")
w, l = player(2), player(3)
streak = RK.apply_ranked_win(w)
RK.apply_ranked_loss(l)
check("winner gains rank score", RK.rank_score(w) == WIN_SCORE, RK.rank_score(w))
check("winner's ranked wins increment", RK.ranked_wins(w) == 1)
check("winner's streak starts at 1", streak == 1 and RK.best_streak(w) == 1)
check("loser's ranked losses increment", RK.ranked_losses(l) == 1)
check("loser's streak is broken", l[RK.K_WIN_STREAK] == 0)
check("rank score floors at 0, never negative", RK.rank_score(l) == 0,
      RK.rank_score(l))

big = player(4, rank_score=500)
RK.apply_ranked_loss(big)
check("a real loss subtracts", RK.rank_score(big) == 500 - LOSS_SCORE,
      RK.rank_score(big))

s = player(5)
for _ in range(7):
    RK.apply_ranked_win(s)
check("streak accumulates over consecutive wins", RK.best_streak(s) == 7)
RK.apply_ranked_loss(s)
check("best streak survives a loss", RK.best_streak(s) == 7)
check("current streak resets", s[RK.K_WIN_STREAK] == 0)
RK.apply_ranked_win(s)
check("best streak is not overwritten by a smaller new one",
      RK.best_streak(s) == 7, RK.best_streak(s))

print("\n── 3. win rate ──────────────────────────────────────────────────")
wr = player(6, ranked_wins=7, ranked_losses=3)
check("7W/3L is 70%", abs(RK.win_rate(wr) - 70.0) < 1e-9, RK.win_rate(wr))
check("no games is 0%, not a crash", RK.win_rate(player(7)) == 0.0)
check("undefeated is 100%", RK.win_rate(player(8, ranked_wins=4)) == 100.0)

perfect = player(9, ranked_wins=1)          # 1-0, a perfect record
grinder = player(10, ranked_wins=30, ranked_losses=20)
board = RK.build_board([perfect, grinder], "winrate", config=CFG_OFF)
check("a 1-0 record cannot top the win-rate board",
      [p["user_id"] for p, _ in board] == ["10"],
      [p["user_id"] for p, _ in board])
check(f"...because {RK.MIN_RANKED_GAMES} games are required",
      RK.MIN_RANKED_GAMES >= 10)

print("\n── 4. the seven categories ──────────────────────────────────────")
check("the five ranked boards, plus level and money",
      set(RK.CATEGORIES) == {"rank", "winrate", "wins", "streak", "catches",
                             "level", "money"},
      set(RK.CATEGORIES))
for key, spec in RK.CATEGORIES.items():
    for field in ("label", "emoji", "describe", "value", "format",
                  "eligible", "empty"):
        check(f"{key} defines {field}", field in spec)

pool = [
    player(100, rank_score=900, ranked_wins=30, ranked_losses=10,
           best_streak=9, beys_caught=12),
    player(101, rank_score=400, ranked_wins=50, ranked_losses=40,
           best_streak=4, beys_caught=99),
    player(102, rank_score=700, ranked_wins=12, ranked_losses=1,
           best_streak=12, beys_caught=3),
]
tops = {k: RK.build_board(pool, k, config=CFG_OFF)[0][0]["user_id"]
        for k in RK.CATEGORIES}
check("rank board tops on score", tops["rank"] == "100", tops)
check("wins board tops on ranked wins", tops["wins"] == "101", tops)
check("streak board tops on best streak", tops["streak"] == "102", tops)
check("catches board tops on catches", tops["catches"] == "101", tops)
check("winrate board tops on rate, not volume", tops["winrate"] == "102", tops)
check("the four boards genuinely differ", len(set(tops.values())) >= 3, tops)

# Level and money are not ranked stats — they come from playing at all — so
# they are the two boards a player can be on without ever queuing for ranked.
rich = player(150, coins=999_999, level=2)
poor = player(151, coins=1, level=80)
check("the money board sorts on coins",
      [p["user_id"] for p, _ in RK.build_board([poor, rich], "money",
                                               config=CFG_OFF)] == ["150", "151"])
check("the level board sorts on level",
      [p["user_id"] for p, _ in RK.build_board([poor, rich], "level",
                                               config=CFG_OFF)] == ["151", "150"])
check("a player with no coins is off the money board",
      not RK.build_board([player(152, coins=0)], "money", config=CFG_OFF))
check("...and level 1 is off the level board",
      not RK.build_board([player(153, level=1)], "level", config=CFG_OFF))
check("neither board is resettable — 'reset a leaderboard' must not be a "
      "route to wiping every wallet in the store",
      "level" not in RK.RESETTABLE and "money" not in RK.RESETTABLE,
      sorted(RK.RESETTABLE))

print("\n── 5. eligibility and ordering ──────────────────────────────────")
# A brand-new account: no ranked games AND no beys. The default `player()`
# helper has two in its inventory, which correctly places it on the catches
# board — a caught bey is a caught bey whether or not you have ever battled.
fresh = player(200, inventory=[])
on = [k for k in RANKED_BOARDS
      if any(p["user_id"] == "200"
             for p, _ in RK.build_board(pool + [fresh], k, config=CFG_OFF))]
check("a brand-new account appears on no ranked board at all", not on, on)

caught_only = player(201, inventory=["A", "B", "C"])
on = [k for k in RANKED_BOARDS
      if any(p["user_id"] == "201"
             for p, _ in RK.build_board(pool + [caught_only], k, config=CFG_OFF))]
check("a player who has only CAUGHT beys is on the catches board and no other",
      on == ["catches"], on)
check("limit is honoured",
      len(RK.build_board(pool, "rank", limit=2, config=CFG_OFF)) == 2)
check("a limit of 0 does not return an empty board silently",
      len(RK.build_board(pool, "rank", limit=0, config=CFG_OFF)) == 1)
check("an unknown category falls back rather than raising",
      RK.build_board(pool, "nonsense", config=CFG_OFF)
      == RK.build_board(pool, RK.DEFAULT_CATEGORY, config=CFG_OFF))
check("non-dict rows are skipped, not crashed on",
      len(RK.build_board(pool + ["junk", None], "rank", config=CFG_OFF)) == 3)
check("position_of finds a placed player",
      RK.position_of(pool, "100", "rank", config=CFG_OFF) == 1)
check("position_of returns None for the unplaced",
      RK.position_of(pool, "999", "rank", config=CFG_OFF) is None)

tie_a = player(300, rank_score=100, best_streak=5, ranked_wins=9)
tie_b = player(301, rank_score=800, best_streak=5, ranked_wins=2)
order = [p["user_id"] for p, _ in
         RK.build_board([tie_a, tie_b], "streak", config=CFG_OFF)]
check("ties break deterministically on rank score", order == ["301", "300"],
      order)

print("\n── 6. verification is gone, root and branch ────────────────────")
# It defaulted to off, no install ever turned it on, and it cost a command, a
# profile key, a filter inside `build_board`, three admin actions and a slice
# of `rank_settings`. The control-server lock went with it: with the four
# verify actions gone it guarded one owner-only action, which `owner_only`
# was already doing.
#
# Checked by NAME rather than by behaviour, because the failure being guarded
# against is a caller that survived the removal — `AttributeError` in front of
# a player, at the moment they try to play ranked.

GONE = ("verify_required", "is_verified", "eligibility_error", "DEFAULT_INVITE",
        "control_guild_id", "is_control_guild", "control_error")
for name in GONE:
    check(f"`RK.{name}` is gone", not hasattr(RK, name))

check("the ranked config has no keys left to set",
      RK.get_config({RK.CONFIG_KEY: {}}) == {},
      RK.get_config({RK.CONFIG_KEY: {}}))

# The one thing deliberately NOT removed: the key on 3,400 live profiles.
check("the profile key is still named, so nothing re-uses it by accident",
      RK.K_VERIFIED == "ranked_verified")

unv = player(403, rank_score=9999, ranked_wins=99)
ver = player(402, ranked_verified=True, rank_score=10, ranked_wins=1)
board = RK.build_board([unv, ver], "rank", config=CFG_OFF)
check("the board no longer filters on it — the top score is top",
      [p["user_id"] for p, _ in board] == ["403", "402"],
      [p["user_id"] for p, _ in board])
check("...and everyone eligible is on it", len(board) == 2, len(board))

csrc = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "cogs", "admin", "actions.py"),
    encoding="utf-8").read()
check("the ranked settings are still owner-only — that is the gate that was "
      "doing the work", "def may_run(" in csrc
      and "owner_only: bool = True" in csrc)
check("the control lock is gone with it", "def _rank_locked(" not in csrc)

import cogs.admin.actions as ADMIN                      # noqa: E402
for key in ("rank_verify", "rank_server", "rank_invite", "rank_control"):
    check(f"the `{key}` admin action is gone", key not in ADMIN.REGISTRY)
check("the leaderboard reset stayed", "rank_reset" in ADMIN.REGISTRY)
check("...and is still owner-only and still asks twice",
      ADMIN.REGISTRY["rank_reset"].owner_only
      and bool(ADMIN.REGISTRY["rank_reset"].confirm))

rsrc = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "cogs", "ranked", "ranked_cog.py"),
    encoding="utf-8").read()
check("`;verify` is gone from the ranked cog",
      'name="verify"' not in rsrc)


print("\n── 7. catches ──────────────────────────────────────────────────")
c = player(500)
check("falls back to inventory size for a pre-counter profile",
      RK.beys_caught(c) == 2, RK.beys_caught(c))
RK.record_catch(c)
check("recording a catch starts the real counter", RK.beys_caught(c) == 3)
c["inventory"] = []          # sold everything
check("selling the inventory does not un-count a catch",
      RK.beys_caught(c) == 3, RK.beys_caught(c))
check("a profile with neither reads 0",
      RK.beys_caught({"user_id": "1"}) == 0)

print("\n── 8. the reset, and what it must never touch ───────────────────")
check("'all' covers every resettable key",
      set(RK.reset_keys_for("all")) ==
      {k for keys in RK.RESETTABLE.values() for k in keys})
check("an unknown board clears NOTHING rather than guessing",
      RK.reset_keys_for("coins") == () and RK.reset_keys_for("") == ())

PROTECTED_FIELDS = ("coins", "inventory", "level", "xp", "wins", "losses",
                    "bey_progress", "equipped_avatar", "avatar")
for board in list(RK.RESETTABLE) + ["all"]:
    keys = RK.reset_keys_for(board)
    bad = [f for f in PROTECTED_FIELDS if f in keys]
    check(f"reset '{board}' cannot touch coins/inventory/levels", not bad, bad)

full = player(600, rank_score=500, ranked_wins=20, ranked_losses=5,
              best_streak=8, win_streak=3, beys_caught=42, coins=99999)
RK.apply_reset(full, "all")
check("a full reset zeroes rank score", RK.rank_score(full) == 0)
check("...ranked W/L", RK.ranked_games(full) == 0)
check("...streaks", RK.best_streak(full) == 0)
check("...catches", RK.beys_caught(full) == 0)
check("coins survive a full reset", full["coins"] == 99999, full["coins"])
check("inventory survives", full["inventory"] == ["A", "B"])
check("trainer level survives", full["level"] == 5 and full["xp"] == 400)
check("lifetime wins survive", "wins" in full and full["wins"] == 0)

partial = player(601, rank_score=500, beys_caught=42, best_streak=8)
RK.apply_reset(partial, "catches")
check("a category reset only clears its own keys",
      RK.beys_caught(partial) == 0 and RK.rank_score(partial) == 500
      and RK.best_streak(partial) == 8,
      (RK.beys_caught(partial), RK.rank_score(partial), RK.best_streak(partial)))

print("\n── 8b. match format: finishes, points, first to 3 ───────────────")
check("burst is worth 2", RK.finish_points(RK.FINISH_BURST) == 2)
check("survival is worth 1", RK.finish_points(RK.FINISH_SURVIVAL) == 1)
check("ring-out is worth 1", RK.finish_points(RK.FINISH_RINGOUT) == 1)
check("a match needs 3 points", RK.MATCH_TARGET == 3)
check("an unknown finish scores the minimum, never zero",
      RK.finish_points("mystery") == 1 and RK.finish_points(None) == 1)
check("every finish has a label", all(RK.finish_label(k)
      for k in (RK.FINISH_BURST, RK.FINISH_SURVIVAL, RK.FINISH_RINGOUT)))
check("an unknown finish still labels rather than raising",
      isinstance(RK.finish_label("mystery"), str))

# The shapes a match can actually take.
check("two bursts win a match", 2 * RK.finish_points(RK.FINISH_BURST)
      >= RK.MATCH_TARGET)
check("one burst alone does NOT", RK.finish_points(RK.FINISH_BURST)
      < RK.MATCH_TARGET)
check("three ring-outs win", 3 * RK.finish_points(RK.FINISH_RINGOUT)
      >= RK.MATCH_TARGET)
check("two ring-outs do not", 2 * RK.finish_points(RK.FINISH_RINGOUT)
      < RK.MATCH_TARGET)
check("a burst plus a survival wins",
      RK.finish_points(RK.FINISH_BURST) + RK.finish_points(RK.FINISH_SURVIVAL)
      >= RK.MATCH_TARGET)


def play(seq):
    """Score a sequence of (winner, finish) and return points + the round it
    ended on, exactly as the match loop does."""
    pts = {"a": 0, "b": 0}
    for i, (who, kind) in enumerate(seq, 1):
        if who is None:
            continue                 # a draw scores nobody
        pts[who] += RK.finish_points(kind)
        if max(pts.values()) >= RK.MATCH_TARGET:
            return pts, i
    return pts, len(seq)


pts, ended = play([("a", "burst"), ("a", "burst")])
check("a 2-burst match ends on round 2", ended == 2 and pts["a"] == 4, (pts, ended))
pts, ended = play([("a", "ringout"), ("a", "survival"), ("a", "ringout")])
check("a 3-lesser-finish match ends on round 3", ended == 3, (pts, ended))
# a: 2, b: 2, a: +1 = 3 → the match stops the instant someone reaches the
# target, so the two later rounds are never played.
pts, ended = play([("a", "burst"), ("b", "burst"), ("a", "survival"),
                   ("b", "ringout"), ("a", "ringout")])
check("a traded match ends the moment someone reaches 3",
      ended == 3 and pts == {"a": 3, "b": 2}, (pts, ended))
check("...and the trailing rounds are never played", ended < 5, ended)
pts, ended = play([(None, ""), ("a", "burst"), (None, ""), ("a", "survival")])
check("draws score nobody and do not end the match", pts["a"] == 3, pts)

print("\n── 8c. ONE match per opponent per day ───────────────────────────")
# Was 2. Without a cap the cheapest way to climb is to find one willing partner
# and farm them; at 1 a pairing is spent the moment it is used.
check("the cap is 1", RK.PAIR_DAILY_LIMIT == 1, RK.PAIR_DAILY_LIMIT)
pr = player(700)
check("a fresh pairing has the full allowance",
      RK.pair_remaining(pr, 800) == 1 and RK.pair_limit_error(pr, 800) == "")
RK.record_pair_match(pr, 800)
check("one played spends it", RK.pair_remaining(pr, 800) == 0)
check("...and is refused", RK.pair_limit_error(pr, 800) != "")
# The message is built from the constant, so it has to stay grammatical at 1.
check("the refusal reads '1 ranked match', not 'matches'",
      "1 ranked match " in RK.pair_limit_error(pr, 800),
      RK.pair_limit_error(pr, 800))
check("the refusal says when it resets",
      "midnight" in RK.pair_limit_error(pr, 800).lower())
check("the refusal names the opponent",
      "Rival" in RK.pair_limit_error(pr, 800, "Rival"))
check("a DIFFERENT opponent is unaffected — the cap is per pairing",
      RK.pair_remaining(pr, 801) == RK.PAIR_DAILY_LIMIT,
      RK.pair_remaining(pr, 801))

stale = player(701)
stale[RK.K_PAIRS] = {"800": {"day": "1999-01-01", "count": 99}}
check("yesterday's tally does not count today",
      RK.pair_count(stale, 800) == 0
      and RK.pair_remaining(stale, 800) == RK.PAIR_DAILY_LIMIT,
      (RK.pair_count(stale, 800), RK.pair_remaining(stale, 800)))
RK.record_pair_match(stale, 802)
check("recording prunes other opponents' expired entries",
      "800" not in stale[RK.K_PAIRS], stale[RK.K_PAIRS])

junk = player(702, ranked_pairs="not-a-dict")
check("a junk pairs blob reads as zero rather than crashing",
      RK.pair_count(junk, 1) == 0)
junk2 = player(703)
junk2[RK.K_PAIRS] = {"1": {"day": RK._today(), "count": "lots"}}
check("a junk count reads as zero", RK.pair_count(junk2, 1) == 0)
check("recording over a junk count recovers to 1",
      RK.record_pair_match(junk2, 1) == 1)

print("\n── 8d. finish detection in the engine ───────────────────────────")
import inspect as _insp                                  # noqa: E402
import cogs.battle.session as _S                         # noqa: E402
ssrc = _insp.getsource(_S)
check("ring-outs are tagged", ssrc.count('_mark_finish(key, "ringout")') == 2,
      ssrc.count('_mark_finish(key, "ringout")'))
check("both ring-out sites are covered — ability-driven and stability-zero",
      ssrc.count('_mark_finish(key, "ringout")') == 2)
check("stamina KO is tagged as a survival",
      '_mark_finish(key, "survival")' in ssrc)
check("an unmarked loss defaults to burst — HP reduced to 0 by damage",
      "RK.FINISH_BURST" in ssrc)
check("the first mark wins, so two causes cannot relabel each other",
      "setdefault" in _insp.getsource(_S.BattleSession._mark_finish))
check("the finish is announced on EVERY battle, not just ranked",
      "finish_label(kind)" in ssrc)

sess = _S.BattleSession.__new__(_S.BattleSession)
sess.finish_type = {}
check("a clean loss reads as a burst", sess.finish_for("7") == RK.FINISH_BURST)
sess._mark_finish("7", "ringout")
check("a tagged loss keeps its tag", sess.finish_for("7") == RK.FINISH_RINGOUT)
sess._mark_finish("7", "survival")
check("a second tag does not overwrite the first",
      sess.finish_for("7") == RK.FINISH_RINGOUT)
check("other players are unaffected", sess.finish_for("8") == RK.FINISH_BURST)

bsrc = open(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "cogs", "battle", "battle.py"), encoding="utf-8").read()
check("a ranked match runs a loop, not a single fight",
      "_run_ranked_match" in bsrc)
check("each round is a FRESH session — carried damage would decide the match",
      "Each round is a fresh session" in bsrc)
check("the ladder moves once for the MATCH, not once per round",
      bsrc.count("apply_ranked_win") == 1, bsrc.count("apply_ranked_win"))
check("the pairing is charged to BOTH players",
      "record_pair_match" in bsrc and "for a, b in ((me, them), (them, me))" in bsrc)
check("the pair limit is checked from both sides before starting",
      "pair_limit_error" in bsrc)
check("the match loop has a round ceiling so a draw cannot run forever",
      "MAX_ROUNDS" in bsrc)
check("a casual battle is still exactly one fight",
      "ranked=False" in bsrc)

print("\n── 9. the engine is actually wired ──────────────────────────────")
import inspect                                          # noqa: E402
import cogs.battle.session as SESSION                   # noqa: E402
src = inspect.getsource(SESSION.BattleSession.__init__)
check("BattleSession takes a `ranked` flag", "ranked" in src)
check("...defaulting to False so existing callers stay casual",
      "ranked:  bool = False" in src or "ranked: bool = False" in src)
end_src = inspect.getsource(SESSION.BattleSession)
check("ranked wins go through utils.ranked", "apply_ranked_win" in end_src)
check("ranked losses go through utils.ranked", "apply_ranked_loss" in end_src)
check("the streak is guarded by the ranked flag",
      "if self.ranked:" in end_src)

import cogs.battle.battle as BATTLE                     # noqa: E402
bsrc = inspect.getsource(BATTLE)
check(";battle accepts a mode", "mode: str" in bsrc)
check("...and no longer asks whether a player is verified",
      "eligibility_error" not in bsrc)
check("...but still enforces the daily pair limit from both sides",
      bsrc.count("RK.pair_limit_error(") == 1 and "for a, b in ((ctx.author" in bsrc)
check("...and a ranked match builds ranked sessions",
      "ranked=True" in bsrc and "_run_ranked_match" in bsrc)

import cogs.spawn.spawn as SPAWN                        # noqa: E402
check("catching a spawn records the catch",
      "record_catch" in inspect.getsource(SPAWN))

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
