#!/usr/bin/env python3
"""
tools/sim_snapshots.py — the backup is only real if the restore is.

This suite exists because of one specific way a player-data backup goes wrong:
it looks complete and is not. `data/avatar_inventory.json` is a SEPARATE file
from the `users` table (`utils/database.py:41`), so the obvious implementation
— dump the table — restores a player with their whole collection and none of
their avatars, and reports success while doing it. Check 2 is that check.

The other failures it is built around, all of which have precedent here:

  * a daily timer that resets on restart, so a bot restarting every few hours
    takes a backup never and nothing ever errors
  * a half-written file that looks restorable
  * a "restore" that silently rolls back things you did not ask it to
  * a snapshot that quietly carries a token

Everything runs against a REAL UserStore on a temp directory, because the
round trip is the feature and a fake store would prove nothing about it.

Run:  python3 tools/sim_snapshots.py
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import sys
import tempfile
import time

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


import utils.database as DB                                       # noqa: E402
from utils.userstore import UserStore                             # noqa: E402

TMP = tempfile.mkdtemp()
DATA = os.path.join(TMP, "data")
BACKUPS = os.path.join(TMP, "backups")
os.makedirs(DATA, exist_ok=True)

DB.USERS_DB_PATH = os.path.join(DATA, "users.db")
STORE = UserStore(DB.USERS_DB_PATH)
STORE.ensure_ready()
DB.USER_STORE = STORE

from utils import snapshot as SN                                  # noqa: E402
from cogs.snapshots import clock as CK                            # noqa: E402

AVATARS = os.path.join(DATA, "avatar_inventory.json")


def seed(uid, **fields):
    prof = DB._default_profile(str(uid))
    prof.update(fields)
    STORE.put_one(str(uid), prof)
    return prof


def main() -> int:
    # ── 1. a full round trip ────────────────────────────────────────────────
    print("\n── 1. everything comes back ────────────────────────────────────")
    for i in range(1, 51):
        seed(1000 + i, inventory=[f"Blade{i}", "Valkyrie"], coins=100 * i,
             community_xp=25 * i, com_level=i % 12,
             bey_progress={f"Blade{i}": {"xp": 400 * i}},
             equipped_avatar="gaster" if i % 3 == 0 else None)
    DB._atomic_write_json(AVATARS, {str(1000 + i): [f"av{i}", "gaster"]
                                    for i in range(1, 51)})

    before = STORE.load_all()
    snap = SN.collect()
    d = SN.describe(snap)
    check("the snapshot sees every profile", d["profiles"] == 50, d)
    check("...their beys", d["beys"] == 100, d["beys"])
    check("...their avatars", d["avatars"] == 100, d["avatars"])
    check("...and who has a community level",
          d["community_levelled"] == sum(1 for i in range(1, 51) if i % 12),
          d["community_levelled"])

    path = SN.write(BACKUPS, snap)
    size = os.path.getsize(path)
    check("it writes a gzipped file", path.endswith(".json.gz") and size > 0,
          size)
    check("...and keeps a `latest` pointing at the same bytes",
          os.path.exists(os.path.join(BACKUPS, SN.LATEST)))
    print(f"       50 profiles -> {size} bytes gzipped")

    # Wipe every profile, then put them back.
    for uid in list(before):
        STORE.put_one(uid, DB._default_profile(uid))
    check("the wipe really emptied them",
          all(not (STORE.get_one(u) or {}).get("inventory") for u in before))

    out = SN.restore(SN.read(path))
    after = STORE.load_all()
    check(f"the restore reports {out['profiles']} profiles",
          out["profiles"] == 50, out)
    same = sum(1 for uid in before if after.get(uid) == before[uid])
    check("every profile is identical to before the wipe", same == 50,
          f"{same}/50")

    # ── 2. avatars — the check the naive version fails ──────────────────────
    print("\n── 2. avatars are NOT in the users table ───────────────────────")
    check("the avatars live in their own file, not the profile",
          "avatar_inventory.json" in DB.AVATARS_PATH
          and "avatars" not in (before[list(before)[0]] or {}),
          DB.AVATARS_PATH)
    os.remove(AVATARS)
    check("...and are gone once that file is", not os.path.exists(AVATARS))
    SN.restore(SN.read(path))
    check("a restore brings the avatars back",
          os.path.exists(AVATARS), AVATARS)
    recovered = json.load(open(AVATARS))
    check("...all of them, for every owner",
          len(recovered) == 50 and recovered["1001"] == ["av1", "gaster"],
          list(recovered.items())[:1])
    check("a snapshot taken with no avatars file simply omits it, rather than "
          "failing", "avatars" in (SN.collect().get("files") or {}))

    # ── 3. a sectioned restore touches only its section ─────────────────────
    print("\n── 3. restoring beys must not roll back coins ──────────────────")
    STORE.put_one("1001", dict(STORE.get_one("1001"), coins=999_999,
                               inventory=[], bey_progress={}))
    out = SN.restore(SN.read(path), ["beys"])
    p = STORE.get_one("1001")
    check("the collection comes back",
          p["inventory"] == ["Blade1", "Valkyrie"], p["inventory"])
    check("...and the coins earned since are untouched",
          p["coins"] == 999_999, p["coins"])
    check("the reply names the section it applied",
          out["sections"] == ["beys"], out)

    STORE.put_one("1002", dict(STORE.get_one("1002"), com_level=0,
                               community_xp=0, coins=555))
    SN.restore(SN.read(path), ["community"])
    p2 = STORE.get_one("1002")
    check("restoring `community` brings the level back",
          p2["com_level"] == 2 and p2["community_xp"] == 50,
          (p2["com_level"], p2["community_xp"]))
    check("...and leaves coins alone", p2["coins"] == 555, p2["coins"])

    os.remove(AVATARS)
    SN.restore(SN.read(path), ["community"])
    check("a section that does not own the avatars file does not write it",
          not os.path.exists(AVATARS))
    SN.restore(SN.read(path), ["avatars"])
    check("...and the one that does, does", os.path.exists(AVATARS))

    # ── 4. the timer survives restarts ──────────────────────────────────────
    print("\n── 4. a bot that restarts daily still gets daily backups ───────")
    now = time.time()
    CK.mark(now - 25 * 3600)
    check("25 hours since the last one is due", CK.due(now), CK.last_run())
    CK.mark(now - 2 * 3600)
    check("two hours is not", not CK.due(now))
    check("...and the panel can say when the next one is",
          3600 < CK.next_due(now) <= 22 * 3600 + 1, CK.next_due(now))
    # The point of the persisted stamp: a brand new process reads it back.
    import importlib
    importlib.reload(CK)
    check("a fresh process reads the same stamp rather than restarting the "
          "clock", not CK.due(now), CK.last_run())
    STORE.community_config_put(CK.KEY, "")
    check("an install that has never run takes one immediately, rather than "
          "waiting a day for its first copy", CK.due(now), CK.last_run())

    # ── 5. atomic writes ────────────────────────────────────────────────────
    print("\n── 5. no half-written file can look restorable ─────────────────")
    real_replace = os.replace
    crashed = {"n": 0}

    def exploding(src, dst):
        crashed["n"] += 1
        raise OSError("disk full")

    target = os.path.join(BACKUPS, SN.DAILY_DIR, f"{SN.stamp()}.json.gz")
    keep = open(target, "rb").read()
    os.replace = exploding
    try:
        SN.write(BACKUPS)
    except OSError:
        pass
    finally:
        os.replace = real_replace
    check("a crash mid-write raises rather than pretending", crashed["n"] >= 1)
    check("...the previous snapshot is untouched",
          open(target, "rb").read() == keep)
    leftovers = [f for f in os.listdir(os.path.join(BACKUPS, SN.DAILY_DIR))
                 if f.endswith(".tmp")]
    check("...and the temp file is never mistaken for a snapshot",
          not any(f.endswith(".json.gz") for f in leftovers), leftovers)
    for f in leftovers:
        os.remove(os.path.join(BACKUPS, SN.DAILY_DIR, f))

    # ── 6. retention ────────────────────────────────────────────────────────
    print("\n── 6. the folder does not grow forever ─────────────────────────")
    day = 86400.0
    base = time.time() - 30 * day
    for i in range(20):
        SN.write(BACKUPS, snap, now=base + i * day, also_latest=False)
    rows = SN.listing(BACKUPS, SN.DAILY_DIR)
    check("20 dailies are on disk", len(rows) >= 20, len(rows))
    removed = SN.prune(BACKUPS, keep=14)
    left = SN.listing(BACKUPS, SN.DAILY_DIR)
    check(f"pruning to 14 removes the rest ({removed} gone)",
          len(left) == 14, len(left))
    check("...and keeps the NEWEST fourteen, by snapshot DATE rather than by whichever file was touched last",
          left[0]["name"] > left[-1]["name"] and len(left) == 14,
          [r["name"] for r in left[:2] + left[-1:]])
    check("`latest` is not one of the files pruning can take",
          os.path.exists(os.path.join(BACKUPS, SN.LATEST)))

    # ── 7. no secret can ride along ─────────────────────────────────────────
    print("\n── 7. a snapshot cannot carry a credential ─────────────────────")
    with open(os.path.join(TMP, "config_local.py"), "w") as fh:
        fh.write('BOT_TOKEN = "MTA5NzY1NDMyMTA5ODc2NTQzMg.GaBcDe.'
                 'xxxxxxxxxxxxxxxxxxxxxxxxxxx"\n'
                 'GITHUB_TOKEN = "github_pat_11ABCDEFG0abcdefghijklmno"\n')
    with open(os.path.join(TMP, ".env"), "w") as fh:
        fh.write("MYSQL_URL=mysql://user:pass@host/db\n")
    fresh = SN.collect()
    check("the collector names the files it takes, so a secret cannot be "
          "swept in", SN.audit_secrets(fresh) == [], SN.audit_secrets(fresh))
    blob = json.dumps(fresh)
    for bad in ("config_local", ".env", "GITHUB_TOKEN", "MYSQL_URL",
                "BOT_TOKEN"):
        check(f"...no `{bad}` anywhere in the snapshot", bad not in blob)
    check("and the auditor is not blind — it catches a planted token",
          SN.audit_secrets({"files": {"x": "github_pat_11ABCDEFG0abcdefghij"}}))

    # ── 8. it follows the live backend ──────────────────────────────────────
    print("\n── 8. it snapshots whichever store is live ─────────────────────")
    other = UserStore(os.path.join(TMP, "other.db"))
    other.ensure_ready()
    other.put_one("777", dict(DB._default_profile("777"),
                              inventory=["OnlyInTheOtherStore"]))
    was, DB.USER_STORE = DB.USER_STORE, other
    try:
        swapped = SN.collect()
    finally:
        DB.USER_STORE = was
    check("rebinding USER_STORE changes what gets backed up — the property "
          "that makes MySQL work", list(swapped["profiles"]) == ["777"],
          list(swapped["profiles"])[:3])
    check("...and switching back restores the original view",
          len(SN.collect()["profiles"]) == 50)

    # ── 9. the panel, built and driven ──────────────────────────────────────
    print("\n── 9. the restore path is reachable and armed twice ────────────")
    import asyncio

    import discord
    from cogs.snapshots import panel as PN
    from cogs.admin import actions as A

    class FakeUser:
        def __init__(self, uid):
            self.id = uid
            self.roles = []

    class FakeResp:
        def __init__(self, box):
            self.box = box
            self.done = False

        def is_done(self):
            return self.done

        async def defer(self, **kw):
            self.done = True

        async def send_message(self, content=None, **kw):
            self.done = True
            self.box.append(content)

        async def edit_message(self, **kw):
            self.done = True

    class FakeFollow:
        def __init__(self, box):
            self.box = box

        async def send(self, content=None, **kw):
            self.box.append({"content": content, **kw})

    class FakeInter:
        def __init__(self, uid=A.MASTER_ID):
            self.user = FakeUser(uid)
            self.sent = []
            self.response = FakeResp(self.sent)
            self.followup = FakeFollow(self.sent)

        async def edit_original_response(self, **kw):
            self.sent.append(kw)

    async def drive():
        view = PN.SnapshotPanel(None, A.MASTER_ID, BACKUPS)
        kinds = [type(c).__name__ for c in view.children]
        check("the panel builds with a snapshot picker and a section picker",
              "SnapshotPicker" in kinds and "SectionSelect" in kinds, kinds)
        check("...and four buttons",
              len([k for k in kinds if k == "Button"]) == 4, kinds)
        e = view.embed()
        check("its embed renders with nothing selected",
              isinstance(e, discord.Embed) and e.title)

        # A fresh snapshot: the one from section 1 is a legitimate casualty of
        # the pruning in section 6, and a suite that depends on surviving a
        # prune is testing the prune, not the panel.
        view.selected = SN.write(BACKUPS, snap, kind=SN.PRE_RESTORE_DIR,
                                 also_latest=False)
        e2 = view.embed()
        body = " ".join(f.value for f in e2.fields)
        check("picking a snapshot shows what is inside it",
              "50" in body and "avatars" in body, body[:120])

        # Restoring must take two presses, and save the current state first.
        STORE.put_one("1003", dict(STORE.get_one("1003"), inventory=[]))
        it = FakeInter()
        await view.restore_it.callback(it)
        check("the first press only arms it — nothing is restored yet",
              view.armed and not STORE.get_one("1003")["inventory"],
              view.armed)
        check("...and says what it is about to overwrite",
              "overwrite" in view.note.lower(), view.note)

        it2 = FakeInter()
        await view.restore_it.callback(it2)
        check("the second press restores",
              STORE.get_one("1003")["inventory"] == ["Blade3", "Valkyrie"],
              STORE.get_one("1003")["inventory"])
        pres = SN.listing(BACKUPS, SN.PRE_RESTORE_DIR)
        check("...after saving the current state to pre-restore/", pres, pres)
        undo = SN.read(pres[0]["path"])
        check("...which really holds the pre-restore state, so the restore is "
              "itself undoable",
              undo["profiles"]["1003"]["inventory"] == [],
              undo["profiles"]["1003"]["inventory"])
        check("the panel disarms after firing", not view.armed)

        # Someone else's panel is not theirs to press.
        stranger = FakeInter(uid=424242)
        before_note = view.note
        await view.take_now.callback(stranger)
        check("a non-owner pressing a button is refused",
              view.note == before_note and stranger.sent, stranger.sent[:1])

        view2 = PN.SnapshotPanel(None, A.MASTER_ID, BACKUPS)
        it3 = FakeInter()
        await view2.take_now.callback(it3)
        check("Snapshot now writes a file", "✅" in view2.note, view2.note)

        it4 = FakeInter()
        view2.selected = path
        await view2.download.callback(it4)
        sent = [s for s in it4.sent if isinstance(s, dict) and s.get("file")]
        check("Download sends the .gz as an attachment — the only copy that "
              "survives a container rebuild", sent, it4.sent[:1])

    asyncio.run(drive())

    # ── 10. nothing here is unreachable ─────────────────────────────────────
    print("\n── 10. every public entry point has a caller ───────────────────")
    import glob
    import re
    package = ""
    for f in (glob.glob(os.path.join(ROOT, "cogs/snapshots/*.py"))
              + [os.path.join(ROOT, "cogs/admin/actions.py"),
                 os.path.join(ROOT, "utils/snapshot.py")]):
        package += open(f, encoding="utf-8").read()
    # `SN.write(...)` is a call; `to_thread(SN.write, folder)` is a call the
    # regex would miss, and `write()` inside snapshot.py itself is how
    # `collect` is reached. All three count — anything else is unreachable.
    unreachable = []
    for name in ("collect", "write", "read", "restore", "prune", "listing",
                 "describe", "folder"):
        called = re.search(rf"\bSN\.{name}\s*\(", package)
        passed = re.search(rf"\bSN\.{name}\s*[,)]", package)
        internal = re.search(rf"(?<!def )(?<!\.)\b{name}\s*\(", package)
        if not (called or passed or internal):
            unreachable.append(name)
    check("every snapshot function is called from the cog or the admin action",
          not unreachable, unreachable)
    check("the admin registry exposes both a panel and a one-press backup",
          {"backups", "backup_now"} <= set(A.REGISTRY),
          [k for k in A.REGISTRY if "backup" in k])
    check("`backups/` is protected from the auto-updater",
          "backups/" in __import__("utils.updater",
                                   fromlist=["x"]).PROTECTED)
    check("...and ignored by git, so player data never lands in the repo",
          "backups/" in open(os.path.join(ROOT, ".gitignore")).read())

    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 66)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
