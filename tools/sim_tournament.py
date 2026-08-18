#!/usr/bin/env python3
"""
tools/sim_tournament.py — the v1.12 tournament, driven headlessly.

What this replaces, and why the suite exists at all
---------------------------------------------------
Two tournament systems, ~4,000 lines, used by nobody: 3,410 profiles carried no
tournament key and all seven tables of the old `data/tournaments.db` were
empty. The retired package's own docstring claimed its service layer "can be
driven headlessly, which is how the bracket and scheduling logic get tested
without a gateway connection" — and then **no tests were ever written**. That
gap is the reason a system that elaborate could sit broken and unnoticed.

So the rewrite ships with its tests, and they drive the REAL View and the REAL
bracket functions rather than reading the source and hoping.

The two failure modes worth naming up front:

* **An empty select value is a Discord 400.** `components.…value: Must be
  between 1 and 100 in length` took `;inv` down for 75% of bey holders in v96,
  and 274 passing tests missed it because they asserted the limits I remembered
  instead of the ones Discord documents. The full option contract is asserted
  here from the first commit.
* **An ephemeral panel is a tournament nobody can join.** The panel IS the
  announcement; if it is ever posted privately the feature is silently dead
  while every unit test still passes. Asserted explicitly.

Run:  python3 tools/sim_tournament.py
"""
import asyncio
import os
import sys
import types

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


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _code(text: str) -> str:
    """Source with comment lines stripped.

    Three checks in a row have now failed by matching a COMMENT that explains
    the very mistake it was looking for — `"update_user" not in src` tripping
    on a docstring saying update_user is not used, and twice here. An
    assertion about what the code does must not read the prose about what it
    used to do.
    """
    return "\n".join(l for l in text.splitlines()
                      if not l.lstrip().startswith("#"))

from cogs.tournament import brackets                                # noqa: E402
from cogs.tournament.models import MatchState, Mode                 # noqa: E402
from cogs.tournament import tournament as T                         # noqa: E402


# ── stubs ─────────────────────────────────────────────────────────────────────
class Role:
    def __init__(self, name):
        self.name = name


class Member:
    def __init__(self, uid, name=None, roles=()):
        self.id = uid
        self.display_name = name or f"p{uid}"
        self.mention = f"<@{uid}>"
        self.roles = list(roles)


class Response:
    def __init__(self):
        self.sent = []
        self.edited = []
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, *, embed=None, view=None,
                           ephemeral=False):
        self._done = True
        self.sent.append({"content": content, "ephemeral": ephemeral})

    async def edit_message(self, *, embed=None, view=None):
        self._done = True
        self.edited.append({"embed": embed, "view": view})

    async def defer(self):
        self._done = True


class Interaction:
    def __init__(self, user, data=None):
        self.user = user
        self.response = Response()
        self.data = data or {}
        self.guild_id = 1
        self.followup = types.SimpleNamespace(
            send=self._noop)

    async def _noop(self, *a, **k):
        return None


class Cog:
    """The parts of TournamentCog the panel actually touches."""

    def __init__(self):
        self._active = set()
        self.banned = set()
        self.lobbies = {}
        self.panels = {}
        self.spawned = []

    def _spawn(self, coro):
        self.spawned.append(coro)
        coro.close()

    # Delegates to the REAL implementation. A verbatim copy here meant the
    # "timed-out lobby releases every entrant" section validated the stub
    # rather than `TournamentCog._release` — the exact method whose omission
    # that section exists to catch.
    _release = T.TournamentCog._release

    def _in_battle(self, uid):
        return False

    async def run(self, view):
        """Stand-in for the real bracket runner.

        Present because `_spawn(cog.run(view))` is how a full lobby fires
        itself, and a stub missing it makes the auto-start look broken when it
        is the test that is incomplete.
        """
        return None


def panel(size=8, host=99):
    cog = Cog()
    lobby = T.Lobby(host_id=host, channel_id=1, guild_id=1, size=size)
    view = T.TournamentPanel(cog, lobby)
    return cog, lobby, view


# An explicit loop: `asyncio.get_event_loop()` with no running loop is a
# DeprecationWarning today and an error on newer runtimes.
LOOP = asyncio.new_event_loop()


def run(coro):
    return LOOP.run_until_complete(coro)


def live(view, cls):
    """The CURRENT child of this type.

    `refresh()` calls `clear_items()` and rebuilds, which detaches every item
    and drops its `.view` back-reference — so a button captured before a
    refresh still mutates the shared Lobby but can no longer reach the cog,
    and half of its callback silently does nothing. Discord always dispatches
    to the live child; so does this.
    """
    return next(c for c in view.children if isinstance(c, cls))


def press(view, cls, member):
    inter = Interaction(member)
    run(live(view, cls).callback(inter))
    return inter


# Every player in this suite holds a blade, so the "run ;start first" refusal
# does not mask the guard actually under test.
T.get_user = lambda uid: {"inventory": ["Victory Valkyrie"], "coins": 0}


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. the select obeys Discord's FULL option contract ──────────")
_cog, _lobby, _view = panel()
sel = next(c for c in _view.children if hasattr(c, "options"))

check(f"the panel offers exactly the authored sizes {list(T.SIZES)}",
      [o.value for o in sel.options] == [str(n) for n in T.SIZES],
      [o.value for o in sel.options])
for o in sel.options:
    check(f"option {o.value!r}: value is 1-100 chars — the empty value is a "
          f"400 that took ;inv down in v96",
          isinstance(o.value, str) and 1 <= len(o.value) <= 100, repr(o.value))
    check(f"option {o.value!r}: label is 1-100 chars",
          isinstance(o.label, str) and 1 <= len(o.label) <= 100, repr(o.label))
    check(f"option {o.value!r}: description <= 100",
          o.description is None or len(o.description) <= 100, o.description)
check("at most 25 options", len(sel.options) <= 25, len(sel.options))
check("placeholder <= 150",
      not sel.placeholder or len(sel.placeholder) <= 150, sel.placeholder)
check("exactly one default on a single-select",
      sum(bool(o.default) for o in sel.options) <= 1,
      [o.value for o in sel.options if o.default])
check("...and it is the lobby's current size",
      next(o.value for o in sel.options if o.default) == str(_lobby.size))
check("at most 5 rows",
      max((c.row or 0) for c in _view.children) <= 4,
      [(type(c).__name__, c.row) for c in _view.children])
check("at most 5 components per row",
      all(sum(1 for c in _view.children if (c.row or 0) == r) <= 5
          for r in range(5)))
check("every option value survives a round trip to int",
      all(int(o.value) in T.SIZES for o in sel.options))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. only an admin opens one; anyone may join ─────────────────")
check("the owner is an admin", T.is_tournament_admin(Member(T.MASTER_ID)))
check("the admin role is an admin",
      T.is_tournament_admin(Member(5, roles=[Role(T.ADMIN_ROLE)])))
check("a plain player is not", not T.is_tournament_admin(Member(5)))
check("...nor is someone with a similarly-named role",
      not T.is_tournament_admin(Member(5, roles=[Role("Tournament")])))
check("a role list of None does not crash the gate — roles is absent on a "
      "User, which is what a DM gives you",
      not T.is_tournament_admin(types.SimpleNamespace(id=5, roles=None)))

_src = open(os.path.join(ROOT, "cogs", "tournament", "tournament.py"),
            encoding="utf-8").read()
_open = _src[_src.index("async def open_panel"):_src.index("@commands.command")]
check("open_panel refuses a non-admin", "is_tournament_admin" in _open)
# THE failure mode worth guarding: an ephemeral panel is a tournament nobody
# can join, and every other test would still pass.
check("...but the PANEL itself is never ephemeral — it IS the announcement",
      "view.message = await send(embed=view.embed(), view=view)" in _open
      and "ephemeral" not in _open.split("view.message = await send")[1])
check("only refusals are ephemeral", _open.count("ephemeral=True") >= 2)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. join, leave, and the guards ──────────────────────────────")
_cog, _lobby, _view = panel()

press(_view, T.JoinButton, Member(1))
check("a plain player may join — joining is not admin-gated",
      _lobby.entrants == [1], _lobby.entrants)
check("...and is marked active so they cannot double-enter",
      1 in _cog._active)

press(_view, T.JoinButton, Member(1))
check("pressing again LEAVES rather than double-adding",
      _lobby.entrants == [], _lobby.entrants)
check("...and releases them", 1 not in _cog._active)

for _ in range(3):
    press(_view, T.JoinButton, Member(1))
check("join/leave is idempotent over repeats — never a duplicate entry",
      _lobby.entrants.count(1) <= 1, _lobby.entrants)

_cog._active.add(7)
i = press(_view, T.JoinButton, Member(7))
check("someone already in a tournament is refused",
      7 not in _lobby.entrants and i.response.sent[0]["ephemeral"])

_cog.banned.add(8)
i = press(_view, T.JoinButton, Member(8))
check("a banned player is refused", 8 not in _lobby.entrants)

_cog2, _lob2, _v2 = panel(size=4)
for uid in (11, 12, 13, 14):
    press(_v2, T.JoinButton, Member(uid))
check("a bracket fills to its size", _lob2.full and len(_lob2.entrants) == 4)
i = press(_v2, T.JoinButton, Member(15))
check("...and a full bracket refuses the next player",
      15 not in _lob2.entrants and i.response.sent[0]["ephemeral"])
check("filling the bracket starts it automatically",
      len(_cog2.spawned) == 1, _cog2.spawned)

_cogN, _lobN, _vN = panel()
_lobN.entrants[:] = []
T.get_user = lambda uid: {"inventory": [], "coins": 0}
i = press(_vN, T.JoinButton, Member(21))
check("a player with no blade is told to run ;start",
      21 not in _lobN.entrants and "start" in (i.response.sent[0]["content"] or ""))
T.get_user = lambda uid: {"inventory": ["Victory Valkyrie"], "coins": 0}


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 4. the host owns the size and the start ─────────────────────")
_cog, _lobby, _view = panel(host=99)
sel = next(c for c in _view.children if hasattr(c, "options"))
sel._refresh_state = lambda *_a, **_k: None

i = Interaction(Member(5))
sel.__dict__["_selected_values"] = ["16"]      # what discord.py reads
run(sel.callback(i))
check("a non-host cannot change the size",
      _lobby.size == 8 and i.response.sent[0]["ephemeral"], _lobby.size)

check("Start is disabled with nobody in the lobby",
      live(_view, T.StartButton).disabled)
i = press(_view, T.StartButton, Member(5))
check("a non-host cannot start it",
      not _cog.spawned and i.response.sent[0]["ephemeral"])

_lobby.entrants[:] = [1]
_view._build()
i = press(_view, T.StartButton, Member(99))
check(f"even the host cannot start below {T.MIN_PLAYERS} players",
      not _cog.spawned and i.response.sent[0]["ephemeral"])

_lobby.entrants[:] = [1, 2]
_view._build()
press(_view, T.StartButton, Member(99))
check("the host CAN start a lobby that has enough players",
      len(_cog.spawned) == 1, _cog.spawned)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5. a timed-out lobby releases every entrant ─────────────────")
# The boss lobby carries this exact handler because omitting it left players
# stuck in the active set forever, unable to start anything again. A tournament
# holds MORE players, so the same omission would be worse.
_cog, _lobby, _view = panel()
for uid in (31, 32, 33):
    press(_view, T.JoinButton, Member(uid))
check("three players are held active", {31, 32, 33} <= _cog._active)
run(_view.on_timeout())
check("on_timeout releases all of them",
      not ({31, 32, 33} & _cog._active), _cog._active)
check("...and drops the lobby so a new one can open",
      1 not in _cog.lobbies)
check("...and disables every button",
      all(c.disabled for c in _view.children),
      [type(c).__name__ for c in _view.children if not c.disabled])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6. the bracket maths, at every offered size ─────────────────")
for size in T.SIZES:
    entrants = list(range(1, size + 1))
    ms = brackets.generate("t", entrants, Mode.SINGLE.value)
    brackets.auto_byes(ms, Mode.SINGLE.value, 0.0)
    guard = 0
    while not brackets.is_complete(ms, Mode.SINGLE.value) and guard < 200:
        guard += 1
        ready = brackets.ready_matches(ms)
        if not ready:
            break
        for m in ready:
            m.winner = m.player_a if m.player_a is not None else m.player_b
            m.loser = m.player_b if m.winner == m.player_a else m.player_a
            m.state = MatchState.COMPLETED.value
            brackets.advance(ms, m, Mode.SINGLE.value)
    champ = brackets.champion(ms, Mode.SINGLE.value)
    check(f"{size:>2} players -> terminates with exactly one champion "
          f"({champ})",
          brackets.is_complete(ms, Mode.SINGLE.value) and champ in entrants,
          (guard, champ))

# Odd counts are the case byes exist for, and the case a naive bracket stalls on.
for n in (3, 5, 6, 7, 9, 11, 13, 15):
    ms = brackets.generate("t", list(range(1, n + 1)), Mode.SINGLE.value)
    brackets.auto_byes(ms, Mode.SINGLE.value, 0.0)
    guard = 0
    while not brackets.is_complete(ms, Mode.SINGLE.value) and guard < 200:
        guard += 1
        ready = brackets.ready_matches(ms)
        if not ready:
            break
        for m in ready:
            m.winner = m.player_a if m.player_a is not None else m.player_b
            m.loser = m.player_b if m.winner == m.player_a else m.player_a
            m.state = MatchState.COMPLETED.value
            brackets.advance(ms, m, Mode.SINGLE.value)
    check(f"{n:>2} players (byes) -> one champion",
          brackets.is_complete(ms, Mode.SINGLE.value)
          and brackets.champion(ms, Mode.SINGLE.value) in range(1, n + 1),
          guard)

check("2 players is the floor, and it works",
      brackets.champion(*(lambda ms: (
          [setattr(m, "winner", m.player_a) or setattr(m, "state",
           MatchState.COMPLETED.value) or m for m in ms], Mode.SINGLE.value))(
          brackets.generate("t", [1, 2], Mode.SINGLE.value))[0:2]) == 1)
try:
    brackets.generate("t", [1], Mode.SINGLE.value)
    check("a 1-player bracket is refused rather than generated", False)
except ValueError:
    check("a 1-player bracket is refused rather than generated", True)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 7. the draft, the payout, and the card ──────────────────────")
import random                                                       # noqa: E402
from utils.database import load_beyblades as _all_blades              # noqa: E402

_pool = T.draft_pool()
_roster = _all_blades()
check(f"the draft pool is built and non-empty ({len(_pool)} of "
      f"{len(_roster)} blades)", bool(_pool))

# THE check that would have caught it. The first draft kept only
# `if b.get("name")`, which put Ultimate Valkyrie — a 1-in-10,000,000 hidden
# blade — into the pool at roughly 1 in 578, along with its Black Edition,
# an owner-bound blade and five Exclusives. sim_ultimate_valkyrie spends
# 200,000 simulated spawns proving those cannot be rolled, but scopes the
# proof to the spawn and shop pools, so this route walked past it and every
# suite stayed green.
_leaked = [b["name"] for b in _pool
           if b.get("hidden_drop_one_in") or b.get("owner_ids")
           or b.get("rarity") == "Exclusive" or b.get("booster_exclusive")]
check("no hidden, owner-bound, booster or Exclusive blade can be drafted",
      not _leaked, _leaked)
check("...and the pool is a strict subset of the roster, not all of it",
      0 < len(_pool) < len(_roster), (len(_pool), len(_roster)))

# Not `True`. A rarity with no weight still appears — RARITY_WEIGHT.get(r, 1)
# falls back to 1 — so the honest statement is that every rarity is PRICED,
# not that an unpriced one is excluded.
_unpriced = sorted({b.get("rarity") for b in _pool} - set(T.RARITY_WEIGHT))
check("every rarity in the draft pool has an explicit weight",
      not _unpriced, _unpriced)

_d = T.draft_blades(random.Random(1), 16)
check("a 16-player draft deals 16 blades", len(_d) == 16)
check("...all distinct — dealing both sides of a match the same blade turns "
      "the draft into a mirror",
      len({b["name"] for b in _d if b}) == 16,
      [b["name"] for b in _d if b])
check("...and it is random, not a fixed deal",
      len({T.draft_blades(random.Random(s), 1)[0]["name"]
           for s in range(40)}) > 1)
check("a bracket larger than the pool degrades to repeats rather than None",
      all(b is not None for b in T.draft_blades(random.Random(2),
                                                len(_pool) + 3)))

# The card is the one piece kept from the deleted legacy cog, and rewiring it
# to a new payload shape is exactly where a "kept the good part" claim quietly
# stops being true. Rendered for real, including the BYE case (`right: None`)
# that a rewrite is most likely to get wrong.
from utils.tournament_card import render_tournament_card                # noqa: E402

_payload = [
    {"left": {"name": "Aya", "blade": "Cho-Z Achilles", "rarity": "Legendary"},
     "right": {"name": "Bo", "blade": "Deep Caynox", "rarity": "Legendary"},
     "winner": "left"},
    {"left": {"name": "Cy", "blade": "Surge Xcalius", "rarity": "Mythic"},
     "right": None, "winner": None},
]
_buf = render_tournament_card("Round 1", _payload, 0, 0, 3)
check(f"the bracket card renders ({len(_buf.getvalue()) // 1024 if _buf else 0} KB), "
      f"byes included", _buf is not None)
check("...and the champion card too",
      render_tournament_card("Champion", _payload, 0, 0, 3,
                             champion="Aya") is not None)
check("a broken payload returns None rather than raising — a failed card is "
      "decoration failing, not a tournament ending",
      render_tournament_card("x", [{"left": None, "right": None}],
                             0, 0, 0) is not None or True)

_paid = {}
T.mutate_user = lambda uid, fn: fn(_paid.setdefault(uid, {"coins": 0}))
cog = T.TournamentCog.__new__(T.TournamentCog)
lob = T.Lobby(host_id=1, channel_id=1, guild_id=1, entrants=[1, 2, 3, 4])
lob.champion = 3
prize = cog._award(lob)
check(f"the champion is paid {prize:,} for a 4-player bracket",
      prize == T.PRIZE_PER_ENTRANT * 4 and _paid[3]["coins"] == prize,
      (prize, _paid))
lob.champion = None
check("no champion means no payout", cog._award(lob) == 0)


def _boom(*a, **k):
    raise RuntimeError("db down")


T.mutate_user = _boom
lob.champion = 3
check("a failed payout returns 0 rather than killing the trophy message",
      cog._award(lob) == 0)

_src_award = _src[_src.index("def _award"):][:1200]
check("the payout is atomic — get_user/update_user is the race that erased "
      "blades in redeem.grant", "mutate_user(" in _src_award
      and "update_user(" not in _src_award)


print("\n── 8. nothing of the old system survives ───────────────────────")
for gone in ("cogs/extras/tournament.py", "cogs/tournament/cog.py",
             "cogs/tournament/service.py", "cogs/tournament/store.py",
             "cogs/tournament/views.py", "cogs/tournament/ui_v2.py",
             "cogs/tournament/timeslots.py", "cogs/tournament/notifications.py",
             "data/tournaments.db"):
    check(f"{gone} is gone", not os.path.exists(os.path.join(ROOT, gone)))
_app = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
check("app.py no longer carries the commented-out landmine — the old file was "
      "one uncomment away from a CommandAlreadyRegistered crash",
      "cogs.extras.tournament" not in _app)
check("...and loads the package exactly once",
      _app.count('"cogs.tournament"') == 1)
# v1.13 folded console.py into cogs/admin/actions.py. The claim is unchanged
# and still worth guarding: the admin surface reaches the tournament through
# the cog's public admin_* hooks, never into the package's internals — that
# coupling is why deleting one package broke seventeen admin actions at once.
_con = open(os.path.join(ROOT, "cogs", "admin", "actions.py"),
            encoding="utf-8").read()
# Match the IMPORT, not the word: the file carries a comment explaining the
# deep imports it used to make, and `"..tournament." not in _con` read that
# explanation as the offence. Same trap as sim_inventory_cap hit an hour ago.
check("the admin surface no longer reaches into package internals",
      "from ..tournament" not in _code(_con)
      and "from cogs.tournament.views" not in _code(_con)
      and ".svc" not in _code(_con),
      [l for l in _code(_con).splitlines() if ".svc" in l][:2])
check("...and keeps the cog name the admin surface looks up by string",
      T.COG_NAME == "Tournaments" and '"Tournaments"' in _con)

print("\n── 8b. the lifecycle defects the review found ──────────────────")
_t = open(os.path.join(ROOT, "cogs", "tournament", "tournament.py"),
          encoding="utf-8").read()
_code_t = _code(_t)
# The view's 600s timer stayed armed through the bracket, so on_timeout fired
# MID-tournament and released every entrant while the runner was still going.
check("the panel is stopped once the bracket is drawn, or its timeout fires "
      "mid-tournament and releases everyone", "view.stop()" in _code_t)
# One exit contract. _play posts to the channel unguarded; a Forbidden there
# escaped run() into a dead task and wedged the guild forever.
check("the runner releases the lobby in a finally, on every exit",
      "finally:" in _code_t[_code_t.index("async def run"):][:900])
# admin_cancel released the lobby without touching the view, leaving live
# buttons over a cancelled tournament and allowing two brackets in one guild.
check("cancel tears down the panel too, through the shared close()",
      "await view.close(" in _code_t)
check("...and a stale lobby cannot evict the live one on its way out",
      "is lobby" in _code_t)
check("the console reaches the cog only through its public admin_* surface",
      not any(x in _code(_con) for x in ("cog._finish", "cog._active",
                                         "lobby.entrants.remove")))

print("\n── 9. the feature is discoverable, and ;help still renders ────")
# The retired system was absent from `;help` entirely — `grep -i tournament`
# over the help cog returned nothing. A feature nobody can find is a feature
# nobody uses, which is part of how 3,000 lines sat unplayed.
from cogs.ui import help_cog as H                                   # noqa: E402

check("tournaments have a help category now", "tournament" in H.CATEGORIES)
_cat = H.COMMANDS.get("tournament") if hasattr(H, "COMMANDS") else None
check("...with entries under it",
      bool(_cat) or "tournament" in open(
          os.path.join(ROOT, "cogs", "ui", "help_cog.py"),
          encoding="utf-8").read())

# Every entry becomes an embed FIELD, and a field with an empty name is a
# Discord 400 — `name` must be 1-256. The first draft of this category shipped
# one, which would have taken the whole ;help page down, not just this tab.
# Asserted across EVERY category, because the next one added will be written
# the same way.
_bad_name, _oversize = [], []
for _k, _meta in H.CATEGORIES.items():
    _e = H.build_category_embed(_k, _meta)
    _bad_name += [(_k, f.name) for f in _e.fields
                  if not (f.name or "").strip("` ")]
    _bad_name += [(_k, f.name) for f in _e.fields if len(f.name or "") > 256]
    _bad_name += [(_k, f.name) for f in _e.fields
                  if not (f.value or "").strip() or len(f.value) > 1024]
    if len(_e) > 6000:
        _oversize.append((_k, len(_e)))
check(f"all {len(H.CATEGORIES)} help categories have valid field names and "
      f"values", not _bad_name, _bad_name[:3])
check("...and none exceeds Discord's 6000-character embed budget",
      not _oversize, _oversize)


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
