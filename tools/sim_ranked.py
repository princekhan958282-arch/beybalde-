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

OFF = {"verify_enabled": False, "verify_guild_id": None}
ON = {RK.CONFIG_KEY: {"verify_enabled": True, "verify_guild_id": 123,
                      "verify_invite": RK.DEFAULT_INVITE}}
CFG_OFF = {RK.CONFIG_KEY: OFF}


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
check("a casual player is on NO leaderboard",
      all(not RK.build_board([p], k) for k in RK.CATEGORIES if k != "catches"),
      [k for k in RK.CATEGORIES if RK.build_board([p], k)])

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

print("\n── 4. the five categories ───────────────────────────────────────")
check("exactly the five asked for",
      set(RK.CATEGORIES) == {"rank", "winrate", "wins", "streak", "catches"},
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

print("\n── 5. eligibility and ordering ──────────────────────────────────")
# A brand-new account: no ranked games AND no beys. The default `player()`
# helper has two in its inventory, which correctly places it on the catches
# board — a caught bey is a caught bey whether or not you have ever battled.
fresh = player(200, inventory=[])
on = [k for k in RK.CATEGORIES
      if any(p["user_id"] == "200"
             for p, _ in RK.build_board(pool + [fresh], k, config=CFG_OFF))]
check("a brand-new account appears on no board at all", not on, on)

caught_only = player(201, inventory=["A", "B", "C"])
on = [k for k in RK.CATEGORIES
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

print("\n── 6. the verification gate ─────────────────────────────────────")
check("verification is OFF by default", not RK.verify_required(CFG_OFF))
check("everyone is 'verified' while it is off",
      RK.is_verified(player(400), CFG_OFF))
check("enabled but with NO server is not armed — it cannot lock everyone out",
      not RK.verify_required({RK.CONFIG_KEY: {"verify_enabled": True,
                                              "verify_guild_id": None}}))
check("enabled WITH a server is armed", RK.verify_required(ON))

unv, ver = player(401), player(402, ranked_verified=True, rank_score=10,
                               ranked_wins=1)
check("an unverified player is blocked while armed",
      not RK.is_verified(unv, ON))
check("...with a reason naming the invite",
      RK.DEFAULT_INVITE in RK.eligibility_error(unv, ON))
check("a verified player passes", RK.is_verified(ver, ON))
check("no error text for someone who can play",
      RK.eligibility_error(ver, ON) == "")

unv2 = player(403, rank_score=9999, ranked_wins=99)
board = RK.build_board([unv2, ver], "rank", config=ON)
check("an unverified player is filtered OUT of the board while armed",
      [p["user_id"] for p, _ in board] == ["402"],
      [p["user_id"] for p, _ in board])
board_off = RK.build_board([unv2, ver], "rank", config=CFG_OFF)
check("...and back in once verification is off",
      len(board_off) == 2, len(board_off))
check("the default invite is the configured server",
      RK.DEFAULT_INVITE == "https://discord.gg/bMtyey32Ur")

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
check("...and gates ranked on eligibility", "eligibility_error" in bsrc)
check("...and passes the flag through", "ranked  = ranked" in bsrc)

import cogs.spawn.spawn as SPAWN                        # noqa: E402
check("catching a spawn records the catch",
      "record_catch" in inspect.getsource(SPAWN))

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
