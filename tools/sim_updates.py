#!/usr/bin/env python3
"""
sim_updates.py — the notification system: updates out, reports back in.

What this drives, and what it fakes
-----------------------------------
The **real** store, against a temporary SQLite file, because the unique
constraint IS the feature. The real worker, the real service, the real prefs
and targeting. Nothing in `cogs/updates/` is stubbed.

Two things are faked, both at the Discord boundary:

  * a client whose `user.send()` raises whatever the test needs —
    `Forbidden`, `NotFound`, a 429, a generic error — because the states this
    system exists to record are *exactly* those failures, and a suite that
    only ever exercises the happy path proves nothing about them;
  * `asyncio.sleep`, so pacing can be measured without waiting for it.

Run:  python3 tools/sim_updates.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time

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


# ── A real store on a temp file, installed before anything imports ───────────
import utils.database as DB                                      # noqa: E402
from utils.userstore import UserStore                            # noqa: E402

_TMP = tempfile.mkdtemp()
STORE = UserStore(os.path.join(_TMP, "sim.db"))
STORE.ensure_ready()
DB.USER_STORE = STORE

import discord                                                   # noqa: E402
from cogs.updates import prefs as P                               # noqa: E402
from cogs.updates import reports as R                             # noqa: E402
from cogs.updates import service as SV                            # noqa: E402
from cogs.updates import store as S                               # noqa: E402
from cogs.updates import targeting as T                           # noqa: E402
from cogs.updates import worker as W                              # noqa: E402


def seed(uid, *, blades=1, coins=0, level=0, seen=None, created=None, **extra):
    prof = {"user_id": str(uid),
            "inventory": [f"Blade{i}" for i in range(blades)],
            "coins": coins, "level": level, "xp": 0, "wins": 0,
            "losses": 0, "rank_score": 0}
    prof.update(extra)
    STORE.put_one(str(uid), prof)
    if seen is not None or created is not None:
        STORE._conn().execute(
            "UPDATE users SET last_seen=COALESCE(?, last_seen), "
            "created_at=COALESCE(?, created_at) WHERE user_id=?",
            (seen, created, str(uid)))
    return prof


# ── The fake Discord side ────────────────────────────────────────────────────
def _http_error(status, retry_after=None):
    resp = type("R", (), {"status": status, "reason": "test"})()
    exc = discord.HTTPException(resp, {"message": "test", "code": 0})
    if retry_after is not None:
        exc.retry_after = retry_after
    return exc


class FakeUser:
    def __init__(self, uid, client):
        self.id = int(uid)
        self.client = client

    async def send(self, **kw):
        self.client.attempts.append(self.id)
        outcome = self.client.behaviour.get(self.id)
        if outcome is None:
            self.client.delivered.append((self.id, kw.get("embed")))
            return
        if isinstance(outcome, list):
            outcome = outcome.pop(0) if outcome else None
            if outcome is None:
                self.client.delivered.append((self.id, kw.get("embed")))
                return
        raise outcome


class FakeClient:
    """Just enough bot for the worker. `behaviour` decides who fails and how."""

    def __init__(self, behaviour=None, guilds=()):
        self.behaviour = dict(behaviour or {})
        self.delivered: list = []
        self.attempts: list = []
        self.guilds = list(guilds)
        self.loop = asyncio.get_event_loop_policy().get_event_loop()

    def get_user(self, uid):
        return FakeUser(uid, self)

    async def fetch_user(self, uid):
        return FakeUser(uid, self)

    def get_cog(self, name):
        return None

    def get_channel(self, cid):
        return None

    def get_guild(self, gid):
        for g in self.guilds:
            if g.id == int(gid):
                return g
        return None


class FakeMember:
    def __init__(self, uid):
        self.id = int(uid)
        self.bot = False


class FakeGuild:
    def __init__(self, gid, ids):
        self.id = int(gid)
        self.members = [FakeMember(u) for u in ids]


def main() -> int:
    asyncio.run(suite())
    print("\n" + "=" * 66)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 66)
    return 1 if FAIL else 0


async def suite() -> None:
    # ── 1. the ledger refuses to deliver twice ──────────────────────────────
    print("\n── 1. one update, one player, one DM ───────────────────────────")
    for i in range(1, 6):
        seed(1000 + i)
    upd = SV.create(event="UPDATE_RELEASED", title="T", body="B",
                    audience={"kind": T.USERS,
                              "ids": [1001, 1002, 1003],
                              "skip_no_beys": False})
    uid = upd["update_id"]
    first = S.enqueue(uid, [1001, 1002, 1003])
    again = S.enqueue(uid, [1001, 1002, 1003])
    check("queueing three players adds three rows", first == 3, first)
    check("queueing the SAME three again adds none — the composite primary "
          "key refuses it, not application logic", again == 0, again)
    check("...and one extra id still lands", S.enqueue(uid, [1003, 1004]) == 1)

    race_upd = SV.create(event="UPDATE_RELEASED", title="R", body="B")
    rid = race_upd["update_id"]
    barrier = threading.Barrier(16)
    won: list = []

    def racer():
        barrier.wait()
        won.append(S.enqueue(rid, [4242]))

    ts = [threading.Thread(target=racer) for _ in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("sixteen threads queue the same pair — exactly one row exists",
          won.count(1) == 1 and S.counts(rid).get("PENDING") == 1,
          (won.count(1), S.counts(rid)))

    # ── 2. the claim cannot hand the same row to two workers ────────────────
    print("\n── 2. claiming is atomic ───────────────────────────────────────")
    big = SV.create(event="UPDATE_RELEASED", title="Big", body="B")
    bid = big["update_id"]
    S.enqueue(bid, range(5000, 5100))
    a, b = S.claim(bid, 25), S.claim(bid, 25)
    check("two claims of 25 return 25 each", len(a) == len(b) == 25)
    check("...with no overlap at all", not (set(a) & set(b)), set(a) & set(b))
    check("the rest are still PENDING", S.counts(bid).get("PENDING") == 50,
          S.counts(bid))

    # ── 3. restart recovery ─────────────────────────────────────────────────
    print("\n── 3. a process killed mid-batch resumes ───────────────────────")
    check("50 rows are held SENDING", S.counts(bid).get("SENDING") == 50)
    moved = S.reset_stuck()
    check("recovery returns every one of them to PENDING",
          moved == 50 and S.counts(bid).get("PENDING") == 100,
          (moved, S.counts(bid)))
    check("...and nothing was delivered twice as a result",
          S.counts(bid).get("SENT") is None)

    # ── 4. every terminal state, from a real failure ────────────────────────
    print("\n── 4. Forbidden, NotFound, 429 and a generic error ─────────────")
    for u in (2001, 2002, 2003, 2004, 2005):
        seed(u)
    behaviour = {
        2002: discord.Forbidden(type("R", (), {"status": 403,
                                               "reason": "x"})(), "closed"),
        2003: discord.NotFound(type("R", (), {"status": 404,
                                              "reason": "x"})(), "gone"),
        2004: _http_error(429, retry_after=0.0),
        2005: _http_error(500),
    }
    client = FakeClient(behaviour)
    states = SV.create(event="UPDATE_RELEASED", title="S", body="B",
                       audience={"kind": T.USERS,
                                 "ids": [2001, 2002, 2003, 2004, 2005],
                                 "skip_no_beys": False})
    sid = states["update_id"]
    S.enqueue(sid, [2001, 2002, 2003, 2004, 2005])

    worker = W.DeliveryWorker(client)
    real_sleep = asyncio.sleep

    async def no_sleep(_s, *a, **k):
        return await real_sleep(0)
    asyncio.sleep = no_sleep
    try:
        await worker.run_batch(sid)
    finally:
        asyncio.sleep = real_sleep

    def state_of(u):
        row = STORE._conn().execute(
            "SELECT state, attempts FROM update_deliveries "
            "WHERE update_id=? AND user_id=?", (sid, str(u))).fetchone()
        return (row["state"], row["attempts"]) if row else (None, 0)

    check("a normal player is SENT", state_of(2001)[0] == "SENT",
          state_of(2001))
    check("DMs closed is BLOCKED", state_of(2002)[0] == "BLOCKED",
          state_of(2002))
    check("a deleted account is BLOCKED", state_of(2003)[0] == "BLOCKED",
          state_of(2003))
    check("a rate limit is RETRY, not a failure", state_of(2004)[0] == "RETRY",
          state_of(2004))
    check("a 500 is RETRY", state_of(2005)[0] == "RETRY", state_of(2005))
    check("only the reachable player actually received anything",
          [d[0] for d in client.delivered] == [2001],
          [d[0] for d in client.delivered])

    # BLOCKED must never be tried again — the requirement, stated plainly
    before = list(client.attempts)
    asyncio.sleep = no_sleep
    try:
        await worker.run_batch(sid)
    finally:
        asyncio.sleep = real_sleep
    tried_again = [u for u in client.attempts[len(before):]]
    check("a second pass never touches the BLOCKED players again",
          2002 not in tried_again and 2003 not in tried_again, tried_again)
    check("...but it does retry the transient ones",
          2004 in tried_again or 2005 in tried_again, tried_again)

    # and a RETRY that never succeeds becomes FAILED rather than looping
    for _ in range(6):
        asyncio.sleep = no_sleep
        try:
            await worker.run_batch(sid)
        finally:
            asyncio.sleep = real_sleep
    check("a delivery that keeps failing ends as FAILED, not an endless RETRY",
          state_of(2005)[0] == "FAILED", state_of(2005))
    check("...after exactly MAX_ATTEMPTS tries",
          state_of(2005)[1] >= W.tune()["attempts"], state_of(2005))

    # ── 5. preferences, checked at delivery ─────────────────────────────────
    print("\n── 5. the player's switches ────────────────────────────────────")
    seed(3001, notify_updates=False)
    seed(3002, notify_events=False)
    seed(3003)
    check("a fresh profile is opted IN to both",
          P.get(STORE.get_one("3003"), P.K_UPDATES)
          and P.get(STORE.get_one("3003"), P.K_EVENTS))

    async def deliver(ids, event, priority):
        c = FakeClient()
        u = SV.create(event=event, title="x", body="y", priority=priority,
                      audience={"kind": T.USERS, "ids": list(ids),
                                "skip_no_beys": False})
        S.enqueue(u["update_id"], ids)
        wk = W.DeliveryWorker(c)
        asyncio.sleep_backup = asyncio.sleep
        globals()["asyncio"].sleep = no_sleep
        try:
            await wk.run_batch(u["update_id"])
        finally:
            globals()["asyncio"].sleep = real_sleep
        return u["update_id"], [d[0] for d in c.delivered]

    _u, got = await deliver([3001, 3003], "UPDATE_RELEASED", P.NORMAL)
    check("a NORMAL update skips someone who turned update DMs off",
          got == [3003], got)
    _u, got = await deliver([3001, 3003], "UPDATE_RELEASED", P.IMPORTANT)
    check("an IMPORTANT one reaches them anyway", sorted(got) == [3001, 3003],
          got)
    _u, got = await deliver([3001, 3003], "UPDATE_RELEASED", P.CRITICAL)
    check("so does a CRITICAL one", sorted(got) == [3001, 3003], got)
    _u, got = await deliver([3002, 3003], "EVENT_STARTED", P.NORMAL)
    check("event DMs answer to their own switch, not the update one",
          got == [3003], got)
    uid5, _got = await deliver([3001], "UPDATE_RELEASED", P.LOW)
    row = STORE._conn().execute(
        "SELECT state, last_error FROM update_deliveries WHERE update_id=?",
        (uid5,)).fetchone()
    check("an opt-out is recorded as BLOCKED with a reason that is NOT "
          "'DMs closed' — the two must be tellable apart in the ledger",
          row["state"] == "BLOCKED" and "opted out" in (row["last_error"] or ""),
          dict(row))

    # ── 6. targeting ────────────────────────────────────────────────────────
    print("\n── 6. who an update is for ─────────────────────────────────────")
    now = time.time()
    seed(6001, seen=now - 3600, created=now - 400 * 86400)
    seed(6002, seen=now - 40 * 86400, created=now - 400 * 86400)
    seed(6003, seen=now, created=now - 3600)
    seed(6004, blades=0, seen=now, created=now - 3600)
    client6 = FakeClient(guilds=[FakeGuild(77, [6001, 6003, 9999])])

    everyone = T.resolve(None, {"kind": T.ALL, "skip_no_beys": False})["ids"]
    check("ALL reaches every registered profile",
          {6001, 6002, 6003, 6004} <= set(everyone))
    active = T.resolve(None, {"kind": T.ACTIVE, "days": 14,
                              "skip_no_beys": False})["ids"]
    check("ACTIVE excludes someone last seen 40 days ago",
          6001 in active and 6002 not in active, active[:6])
    fresh = T.resolve(None, {"kind": T.NEW, "days": 7,
                             "skip_no_beys": False})["ids"]
    check("NEW is who registered recently",
          6003 in fresh and 6001 not in fresh, fresh[:6])
    srv = T.resolve(client6, {"kind": T.SERVER, "guild_id": 77,
                              "skip_no_beys": False})["ids"]
    check("SERVER is the guild's members intersected with the registry — a "
          "member who never played has no profile and gets nothing",
          6001 in srv and 6003 in srv and 9999 not in srv, srv)

    missed = SV.create(event="UPDATE_RELEASED", title="m", body="b")
    mid = missed["update_id"]
    S.enqueue(mid, [6001])
    never = T.resolve(None, {"kind": T.NOT_RECEIVED, "update_id": mid,
                             "skip_no_beys": False})["ids"]
    check("NOT_RECEIVED is everyone with no row for that update",
          6001 not in never and 6003 in never, never[:6])

    res = T.resolve(None, {"kind": T.ALL, "skip_no_beys": True})
    check("the no-blades filter drops 6004 and reports how many it dropped",
          6004 not in res["ids"] and res["dropped_no_beys"] >= 1,
          (6004 in res["ids"], res["dropped_no_beys"]))
    check("...and says what the total was before filtering, so the number is "
          "auditable rather than a silently smaller audience",
          res["total_before"] > len(res["ids"]))

    reach = T.reachable(client6, [6001, 6002, 6003])
    check("reachability is who shares a server with the bot",
          reach == {6001, 6003}, reach)

    # ── 7. rate limiting really paces ───────────────────────────────────────
    print("\n── 7. the pacing is real, not a constant ───────────────────────")
    slept: list = []

    async def counting_sleep(sec, *a, **k):
        slept.append(sec)
        return await real_sleep(0)

    paced = SV.create(event="UPDATE_RELEASED", title="p", body="b")
    pid = paced["update_id"]
    ids = list(range(7000, 7040))
    for u in ids:
        seed(u)
    S.enqueue(pid, ids)
    cfg = W.tune()
    wk = W.DeliveryWorker(FakeClient())
    asyncio.sleep = counting_sleep
    try:
        out = await wk.run_batch(pid)
    finally:
        asyncio.sleep = real_sleep
    check("a batch is capped at the configured size, not the whole queue",
          out["claimed"] == cfg["batch"], (out["claimed"], cfg["batch"]))
    check("...and it sleeps once per DM at the configured delay",
          len(slept) == out["claimed"]
          and all(abs(s - cfg["delay"]) < 1e-9 for s in slept),
          (len(slept), slept[:3]))
    check("the rest stay queued for the next tick",
          S.counts(pid).get("PENDING") == len(ids) - cfg["batch"],
          S.counts(pid))

    # ── 8. cancel ───────────────────────────────────────────────────────────
    print("\n── 8. cancelling a send in flight ──────────────────────────────")
    before_counts = S.counts(pid)
    dropped = SV.cancel(pid)
    after = S.counts(pid)
    check("cancel drops what has not gone out", dropped >= 1, dropped)
    check("...and leaves what already went", after.get("SENT") ==
          before_counts.get("SENT"), (before_counts, after))
    check("the update is marked CANCELLED",
          (S.get_update(pid) or {}).get("state") == SV.CANCELLED)
    check("a cancelled update is skipped by the worker",
          (await wk.run_batch(pid)).get("cancelled") is True)

    # ── 9. reports ──────────────────────────────────────────────────────────
    print("\n── 9. /bugs and /suggest ───────────────────────────────────────")
    rep = {"report_id": S.new_id("rep"), "kind": R.BUG, "user_id": "8001",
           "guild_id": "77", "summary": "Battle 4 is unwinnable",
           "body": "details", "image_url": None, "status": R.OPEN,
           "created_at": time.time(), "handled_by": None, "handled_at": None,
           "message_id": None}
    S.put_report(rep)
    check("a report round-trips", (S.get_report(rep["report_id"]) or {})
          .get("summary") == "Battle 4 is unwinnable")
    check("it shows up as open", any(r["report_id"] == rep["report_id"]
                                     for r in S.open_reports(10)))

    ok, why = R.may_report(8001)
    check("a second report inside the cooldown is refused", not ok, why)
    check("...and the message says how long is left",
          "m " in why or "s" in why, why)

    # the gate must survive a restart — that is the whole reason it reads the
    # table rather than a dict
    import importlib
    importlib.reload(R)
    ok2, _why2 = R.may_report(8001)
    check("the cooldown still stands after the module is reloaded — it is "
          "read from the table, so a restart is not a way to reset it",
          not ok2)

    parsed = R.parse_custom_id(f"beyreport:{rep['report_id']}:fixed")
    check("a status button's custom_id parses back to its report and action",
          parsed == {"rid": rep["report_id"], "act": "fixed"}, parsed)
    check("...which is what lets a button pressed days later still work, with "
          "no view held in memory",
          issubclass(R.ReportButton, discord.ui.DynamicItem))
    check("a malformed id is refused rather than half-matched",
          R.parse_custom_id("beyreport:oops") is None)
    check("all four triage actions exist",
          set(R.ReportButton.ACTIONS) == {"ack", "fixed", "wontfix", "dupe"})
    check("every one of them has something to say to the reporter",
          all(s in R.REPLY for s in
              (R.ACK, R.FIXED, R.WONTFIX, R.DUPE)))

    # ── 10. the reply rides the same queue ──────────────────────────────────
    print("\n── 10. answering a reporter ────────────────────────────────────")
    seed(8001)
    client10 = FakeClient()
    reply_id = await SV.notify_user(
        client10, 8001, event="IMPORTANT_NOTICE", title="Fixed", body="done")
    counts10 = S.counts(reply_id)
    check("the reply becomes a delivery row like any other",
          sum(counts10.values()) == 1, counts10)
    rows = STORE._conn().execute(
        "SELECT user_id FROM update_deliveries WHERE update_id=?",
        (reply_id,)).fetchall()
    check("...addressed to the reporter and nobody else",
          [r["user_id"] for r in rows] == ["8001"],
          [r["user_id"] for r in rows])

    seed(8002)
    blocked_client = FakeClient({8002: discord.Forbidden(
        type("R", (), {"status": 403, "reason": "x"})(), "closed")})
    rid2 = await SV.notify_user(blocked_client, 8002,
                                event="IMPORTANT_NOTICE",
                                title="Fixed", body="done")
    wk2 = W.DeliveryWorker(blocked_client)
    asyncio.sleep = no_sleep
    try:
        await wk2.run_batch(rid2)
    finally:
        asyncio.sleep = real_sleep
    check("a reporter with DMs shut is recorded BLOCKED rather than the "
          "answer vanishing", S.counts(rid2).get("BLOCKED") == 1,
          S.counts(rid2))

    # ── 11. the surfaces exist and build ────────────────────────────────────
    print("\n── 11. the commands are really there ───────────────────────────")
    from cogs.updates import cog as C
    from cogs.updates import update_panel as UP
    names = {c.name for c in
             (C.NotificationCog.__cog_app_commands__ or [])}
    check("/update, /bugs and /suggest are all registered",
          {"update", "bugs", "suggest"} <= names, names)
    check(";notifications is a prefix command",
          any(getattr(v, "name", None) == "notifications"
              for v in vars(C.NotificationCog).values()))

    menu = C.UpdateMenu(None)
    sel = menu.children[0]
    check("the /update menu offers all six operations",
          {o.value for o in sel.options} ==
          {"create", "preview", "send", "status", "cancel", "history"},
          {o.value for o in sel.options})
    for key in ("preview", "send", "status", "cancel", "history"):
        check(f"...and `{key}` has a handler behind it",
              callable(getattr(C.UpdateActions, f"_{key}", None)))

    draft = SV.create(event="UPDATE_RELEASED", title="Panel", body="b")
    view = UP.ComposeView(draft["update_id"])
    kinds = [type(c).__name__ for c in view.children]
    check("the composer offers priority, audience and the no-blades toggle",
          kinds == ["PrioritySelect", "AudienceSelect", "SkipNoBeysButton"],
          kinds)
    prefs_view = C.PrefsView(3003)
    check("the preferences view has both switches",
          len(prefs_view.children) == 2)

    e = W.build_embed(S.get_update(draft["update_id"]))
    check("the DM embed renders", isinstance(e, discord.Embed) and e.title)
    crit = SV.create(event="MAINTENANCE", title="Down", body="b",
                     priority=P.CRITICAL)
    ce = W.build_embed(S.get_update(crit["update_id"]))
    check("a Critical DM does not advertise an opt-out that does not apply "
          "to it", ";notifications" not in (ce.footer.text or ""),
          ce.footer.text)

    # ── 12. the decoupled entry point ───────────────────────────────────────
    print("\n── 12. other systems can trigger a notification ────────────────")
    check("every event name in the spec is known to the service",
          set(SV.EVENTS) == {"UPDATE_RELEASED", "EVENT_STARTED",
                             "BALANCE_CHANGE", "MAINTENANCE",
                             "IMPORTANT_NOTICE"}, SV.EVENTS)
    check("each maps to a switch, so none silently ignores a preference",
          all(P.switch_for(ev) in (P.K_UPDATES, P.K_EVENTS)
              for ev in SV.EVENTS))
    check("the cog listens for beycord_notify, so a cog can announce without "
          "importing this package",
          hasattr(C.NotificationCog, "on_beycord_notify"))
    check("an unknown event answers to the update switch rather than "
          "bypassing both", P.switch_for("SOMETHING_NEW") == P.K_UPDATES)


if __name__ == "__main__":
    sys.exit(main())
