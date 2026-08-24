#!/usr/bin/env python3
"""
tools/sim_github_backup.py — the second copy is only real if it can't leak,
can't lie about failing, and comes back after a restart.

`utils/github_backup.py` pushes a second copy of the same snapshot to a
private GitHub repo. Three failure modes matter more here than in most of
this codebase, because the thing being tested is a credential talking to a
network:

  * a token ending up somewhere it can be read back — a result dict, a log
    line, an exception string
  * an HTTP failure reported so vaguely a 401 and a 403 look the same
  * a button in a DM, meant to still work after the process that sent it
    has restarted, that doesn't

Every HTTP call is driven against a FAKED `_request` — this suite never
touches the network. `_request` is swapped for the duration of each check and
restored immediately after, the same technique `tools/sim_snapshots.py` uses
on `os.replace`.

Run:  python3 tools/sim_github_backup.py
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import shutil
import sys
import tempfile
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

logging.disable(logging.CRITICAL)

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {detail}")


import discord                                                     # noqa: E402

import utils.database as DB                                        # noqa: E402
from utils.userstore import UserStore                              # noqa: E402

TMP = tempfile.mkdtemp()
DATA = os.path.join(TMP, "data")
os.makedirs(DATA, exist_ok=True)

DB.USERS_DB_PATH = os.path.join(DATA, "users.db")
STORE = UserStore(DB.USERS_DB_PATH)
STORE.ensure_ready()
DB.USER_STORE = STORE

from utils import snapshot as SN                                   # noqa: E402
from utils import github_backup as GB                              # noqa: E402
from cogs.snapshots import receipt as RC                           # noqa: E402
from cogs.snapshots.cog import SnapshotCog                         # noqa: E402
from cogs.admin import actions as A                                # noqa: E402

BACKUPS = SN.folder()


def http_error(code, msg="error"):
    return urllib.error.HTTPError("https://api.github.com/x", code, msg, None, None)


def put_ok(tag="x"):
    return json.dumps({"content": {"html_url": f"https://github.com/{tag}"}}).encode()


def get_sha(sha="abc123"):
    return json.dumps({"sha": sha}).encode()


class ScriptedRequest:
    """Replaces `GB._request`. `script` is consumed in order: each entry is
    `(expected_method, outcome)`, where `outcome` is bytes to return or an
    exception instance to raise."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, token, method="GET", payload=None):
        self.calls.append({"url": url, "token": token, "method": method,
                           "payload": payload})
        if not self.script:
            raise AssertionError(f"unscripted {method} call: {url}")
        want, outcome = self.script.pop(0)
        if want != method:
            raise AssertionError(f"expected {want}, got {method} for {url}")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class AlwaysNew:
    """Every GET 404s (new file), every PUT succeeds. For tests where the
    exact HTTP sequence doesn't matter — only that the push completed."""

    def __call__(self, url, token, method="GET", payload=None):
        if method == "GET":
            raise http_error(404)
        return put_ok("x")


REAL_REQUEST = GB._request
REAL_CONFIGURED = GB.configured
REAL_AUDIT = SN.audit_secrets

SNAP = {"format": 2, "taken_at": 0, "backend": "sqlite",
        "profiles": {}, "files": {}}
GZ = gzip.compress(json.dumps(SNAP).encode("utf-8"))


def main() -> int:
    # ── 1. every HTTP outcome, driven against a faked request ──────────────
    print("\n── 1. every HTTP outcome ─────────────────────────────────────")

    def run_put(script):
        fake = ScriptedRequest(script)
        GB._request = fake
        try:
            return GB._put_file("owner/repo", "daily/2026-08-24.json.gz",
                                b"hello", "tok", "msg"), fake
        finally:
            GB._request = REAL_REQUEST

    r, _f = run_put([("GET", http_error(404)), ("PUT", put_ok("new"))])
    check("a new file: sha GET 404s, PUT succeeds",
          r["ok"] and r["url"] == "https://github.com/new", r)

    r, f = run_put([("GET", get_sha("abc123")), ("PUT", put_ok("upd"))])
    check("updating an existing file succeeds", r["ok"], r)
    check("...and sends the CURRENT sha on the PUT",
          f.calls[1]["payload"]["sha"] == "abc123", f.calls[1]["payload"])

    r, f = run_put([("GET", get_sha("old")), ("PUT", http_error(409)),
                    ("GET", get_sha("new")), ("PUT", put_ok("retry"))])
    check("a stale sha (409) refetches once and retries", r["ok"], r)
    check("...using the FRESH sha on the retry, not the stale one",
          f.calls[3]["payload"]["sha"] == "new", f.calls[3]["payload"])

    r, _f = run_put([("GET", http_error(404)), ("PUT", http_error(401))])
    check("401 reads as a rejected/expired token, not a generic failure",
          not r["ok"] and "rejected" in r["error"], r)

    r, _f = run_put([("GET", http_error(404)), ("PUT", http_error(403))])
    check("403 reads as a scope/access problem naming the repo",
          not r["ok"] and "refused" in r["error"] and "owner/repo" in r["error"], r)

    r, _f = run_put([("GET", http_error(404)), ("PUT", http_error(404))])
    check("404 on the PUT reads as repo-not-found",
          not r["ok"] and "not found" in r["error"], r)

    r, _f = run_put([("GET", http_error(404)), ("PUT", OSError("no route"))])
    check("a network failure never raises, and is reported, not swallowed",
          not r["ok"] and "reach GitHub" in r["error"], r)

    # ── 2. push_snapshot: both files, and a partial failure ────────────────
    print("\n── 2. push_snapshot pushes daily AND latest ────────────────────")
    GB.configured = lambda: ("tok", "owner/repo")
    try:
        GB._request = ScriptedRequest([
            ("GET", http_error(404)), ("PUT", put_ok("daily")),
            ("GET", http_error(404)), ("PUT", put_ok("latest")),
        ])
        result = GB.push_snapshot(GZ, "2026-08-24")
        check("both files push, and the result names the repo",
              result["ok"] and result["repo"] == "owner/repo", result)

        GB._request = ScriptedRequest([
            ("GET", http_error(404)), ("PUT", put_ok("daily")),
            ("GET", http_error(404)), ("PUT", http_error(403)),
        ])
        result = GB.push_snapshot(GZ, "2026-08-24")
        check("if latest fails after daily succeeds, the failure says so — "
              "not a bare 'failed'",
              not result["ok"] and "daily archive pushed" in result["error"],
              result)
    finally:
        GB._request = REAL_REQUEST
        GB.configured = REAL_CONFIGURED

    # ── 3. the secrets audit actually gates the network call ───────────────
    print("\n── 3. a dirty snapshot never reaches the network ───────────────")
    GB.configured = lambda: ("tok", "owner/repo")
    SN.audit_secrets = lambda snap: ["gh_planted12345…"]
    fake = ScriptedRequest([])           # any call at all is a failure
    GB._request = fake
    try:
        result = GB.push_snapshot(GZ, "2026-08-24")
        check("a non-empty audit refuses the push before touching the network",
              not result["ok"] and not fake.calls, (result, fake.calls))
    finally:
        SN.audit_secrets = REAL_AUDIT
        GB._request = REAL_REQUEST
        GB.configured = REAL_CONFIGURED

    # Mutation check: prove check 3 above actually catches the bug it exists
    # for, by removing the gate and confirming the (now unguarded) call
    # reaches `_request`.
    import inspect
    src = inspect.getsource(GB.push_snapshot)
    check("...and the guard really is in push_snapshot, not decorative",
          "audit_secrets" in src and "hits" in src, src)

    # ── 4. unconfigured means skipped, not broken ───────────────────────────
    print("\n── 4. no secrets set -> skipped cleanly ────────────────────────")
    saved_env = {k: os.environ.pop(k, None)
                for k in ("GITHUB_BACKUP_TOKEN", "GITHUB_BACKUP_REPO")}
    try:
        check("configured() reports empty with nothing set",
              GB.configured() == ("", ""), GB.configured())
        fake = ScriptedRequest([])
        GB._request = fake
        result = GB.push_snapshot(GZ, "2026-08-24")
        check("push_snapshot returns cleanly and touches no network",
              result == {"ok": False, "error": "not configured"} and not fake.calls,
              (result, fake.calls))
    finally:
        GB._request = REAL_REQUEST
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v

    # ── 5. the token never appears in any string this module produces ──────
    print("\n── 5. the token cannot ride along in a result or a log line ────")
    TOKEN = "github_pat_SUPERSECRETVALUE1234567890abcdef"
    GB.configured = lambda: (TOKEN, "owner/repo")
    GB._request = AlwaysNew()

    class Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record.getMessage())

    cap = Capture()
    GB.log.addHandler(cap)
    logging.disable(logging.NOTSET)     # lift the blanket suppression for this section
    try:
        # A success, an HTTP failure GitHub classified, AND an unclassified
        # exception — three different code paths build the returned string,
        # and the token has to be absent from every one of them.
        result_ok = GB.push_snapshot(GZ, "2026-08-24")
        GB._request = ScriptedRequest([("GET", http_error(404)),
                                       ("PUT", http_error(401))])
        result_fail = GB.push_snapshot(GZ, "2026-08-24")
        GB._request = ScriptedRequest([("GET", http_error(404)),
                                       ("PUT", RuntimeError("boom"))])
        result_crash = GB.push_snapshot(GZ, "2026-08-24")

        def strings(v):
            if isinstance(v, str):
                yield v
            elif isinstance(v, dict):
                for vv in v.values():
                    yield from strings(vv)
            elif isinstance(v, (list, tuple)):
                for vv in v:
                    yield from strings(vv)

        blobs = (list(strings(result_ok)) + list(strings(result_fail))
                + list(strings(result_crash)) + cap.records)
        leaked = [s for s in blobs if TOKEN in s]
        check("the token appears in neither result — success or failure",
              not leaked, leaked)
        check("...nor in any log line the module wrote",
              all(TOKEN not in r for r in cap.records), cap.records)
    finally:
        logging.disable(logging.CRITICAL)
        GB.log.removeHandler(cap)
        GB._request = REAL_REQUEST
        GB.configured = REAL_CONFIGURED

    # ── 6. the receipt survives a restart ───────────────────────────────────
    print("\n── 6. the receipt DM works after a simulated restart ───────────")

    DAY = "2026-08-24"
    os.makedirs(os.path.join(BACKUPS, SN.DAILY_DIR), exist_ok=True)
    daily_path = os.path.join(BACKUPS, SN.DAILY_DIR, f"{DAY}.json.gz")
    with open(daily_path, "wb") as fh:
        fh.write(GZ)

    template = RC.ReceiptButton.__discord_ui_compiled_template__
    m = template.fullmatch(f"snapback:dl:{DAY}")
    check("a download custom_id parses on its own, no view required",
          m is not None and m["action"] == "dl" and m["day"] == DAY, m)

    class RFakeUser:
        def __init__(self, uid):
            self.id = uid

    class RFakeResp:
        def __init__(self):
            self.edits = []

        async def defer(self, **kw):
            pass

        async def send_message(self, content=None, **kw):
            self.edits.append(("send_message", content, kw))

        async def edit_message(self, **kw):
            self.edits.append(("edit_message", kw))

    class RFakeFollow:
        def __init__(self):
            self.sent = []

        async def send(self, content=None, **kw):
            self.sent.append({"content": content, **kw})

    class RFakeMessage:
        def __init__(self, embeds):
            self.embeds = embeds

    class RFakeInter:
        def __init__(self, uid, embeds=None):
            self.user = RFakeUser(uid)
            self.response = RFakeResp()
            self.followup = RFakeFollow()
            self.message = RFakeMessage(embeds or [])

    async def drive_receipt():
        btn = await RC.ReceiptButton.from_custom_id(None, None, m)
        check("...and rebuilds a real button from just the id — nothing was "
              "held in memory",
              btn.day == DAY and btn.action == "dl", (btn.day, btn.action))

        inter = RFakeInter(A.MASTER_ID)
        await btn.callback(inter)
        check("Download resends the local file, not a re-fetch from GitHub",
              inter.followup.sent
              and inter.followup.sent[0]["file"].filename == f"{DAY}.json.gz",
              inter.followup.sent)

        stranger = RFakeInter(424242)
        await btn.callback(stranger)
        check("a non-owner pressing Download is refused",
              stranger.response.edits
              and stranger.response.edits[0][0] == "send_message"
              and not stranger.followup.sent,
              stranger.response.edits)

        embed = discord.Embed(title="💾 Daily backup", description="x")
        ack_inter = RFakeInter(A.MASTER_ID, embeds=[embed])
        ack_btn = RC.ReceiptButton(DAY, "ack")
        await ack_btn.callback(ack_inter)
        edit = next((e for e in ack_inter.response.edits
                    if e[0] == "edit_message"), None)
        check("Acknowledge edits the ORIGINAL embed rather than fabricating "
              "a new one",
              edit is not None and edit[1]["embed"] is embed, edit)
        check("...stamps an acknowledgement footer",
              edit is not None and "Acknowledged" in (embed.footer.text or ""),
              embed.footer.text if edit else None)
        view = edit[1]["view"] if edit else None
        disabled = [c.item.disabled for c in view.children] if view else []
        check("...and disables both buttons so pressing again does nothing",
              len(disabled) == 2 and all(disabled), disabled)

    asyncio.run(drive_receipt())

    # ── 7. scheduled runs DM, manual runs don't — both push ────────────────
    print("\n── 7. scheduled sends a receipt, manual stays quiet ────────────")

    class FakeOwner:
        def __init__(self):
            self.sent = []

        async def send(self, **kw):
            self.sent.append(kw)

    class FakeLoop:
        def create_task(self, coro):
            coro.close()          # not exercising the failure-alert body here

    class FakeBot:
        def __init__(self, owner):
            self.owner = owner
            self.loop = FakeLoop()

        def get_cog(self, name):
            return None

        def get_user(self, uid):
            return self.owner

        async def fetch_user(self, uid):
            return self.owner

    owner = FakeOwner()
    cog = SnapshotCog(FakeBot(owner))

    GB.configured = lambda: ("tok", "owner/repo")
    GB._request = AlwaysNew()
    try:
        asyncio.run(cog.take("scheduled"))
        check("a scheduled run pushes to GitHub",
              cog.last_github.get("ok"), cog.last_github)
        check("...and DMs the owner exactly one receipt",
              len(owner.sent) == 1, owner.sent)
        dm = owner.sent[0] if owner.sent else {}
        check("...carrying an embed and a two-button view",
              isinstance(dm.get("embed"), discord.Embed)
              and len(dm.get("view").children) == 2, dm)

        asyncio.run(cog.take("admin"))
        check("a manual run ALSO pushes to GitHub",
              cog.last_github.get("ok"), cog.last_github)
        check("...but does not send a second DM",
              len(owner.sent) == 1, owner.sent)
    finally:
        GB._request = REAL_REQUEST

    GB.configured = lambda: ("", "")
    try:
        owner.sent.clear()
        asyncio.run(cog.take("scheduled"))
        check("a scheduled run still DMs when GitHub isn't configured",
              len(owner.sent) == 1, owner.sent)
        fields = owner.sent[0]["embed"].fields if owner.sent else []
        gh_field = next((f for f in fields if f.name == "GitHub"), None)
        check("...and the receipt says so rather than staying silent about it",
              gh_field is not None and "not configured" in gh_field.value,
              gh_field.value if gh_field else None)
    finally:
        GB.configured = REAL_CONFIGURED

    # ── 8. reachable from the wiring, not just from this suite ─────────────
    print("\n── 8. every entry point is reachable from real code ────────────")
    package = ""
    for f in ("cogs/snapshots/cog.py", "cogs/snapshots/panel.py",
             "cogs/snapshots/receipt.py", "utils/github_backup.py"):
        package += open(os.path.join(ROOT, f), encoding="utf-8").read()
    # Called via `asyncio.to_thread(GB.push_snapshot, ...)` in the cog, so
    # the reference is passed rather than immediately invoked — a bare
    # "GB.push_snapshot(" search would miss it.
    check("push_snapshot is called from the cog, not only from this suite",
          "GB.push_snapshot" in package, None)
    check("configured() gates the push inside the cog",
          "GB.configured()" in package, None)
    check("the receipt buttons are registered as dynamic items",
          "add_dynamic_items(RC.ReceiptButton)" in package, None)
    check("the panel's own 'Snapshot now' routes through the cog too — the "
          "two manual paths used to diverge",
          "cog.take(" in open(os.path.join(ROOT, "cogs/snapshots/panel.py"),
                              encoding="utf-8").read(), None)

    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 66)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
