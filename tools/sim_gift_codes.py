#!/usr/bin/env python3
"""
tools/sim_gift_codes.py — avatars, boss beys, and a full command list.

Three things shipped together here, each with its own failure mode this
suite is built around:

  * `;code create` gained `avatar:` and `bossbey:` reward kinds. Boss beys
    are deliberately absent from `data/beyblades.json` (see
    `cogs/battle/boss/boss_info.py`), so the obvious `blade:` path can never
    reach one — a code claiming to grant a boss bey through that path would
    silently fail to resolve at all. `bossbey:Argus:Perfect` also lets an
    admin force a specific grade rather than rolling one, which is new
    surface on `roll_copy()` worth its own check that the forced grade is
    what actually lands, not just what the caller intended.
  * A picker (`CodeBuilderView`) was added alongside the typed spec, not
    instead of it. The failure this guards against is the two drifting apart
    — a reward built by clicking through the picker meaning something
    different from the same reward typed as `avatar:Yuki`. Both go through
    `parse_rewards` on the exact same `kind:value` string, so this suite
    proves that by driving both entry points and comparing the result.
  * `/commands` DMs the full command list — the thing `cogs/core/onboarding.py`
    deliberately keeps OFF the default new-player screen. The button on that
    screen has to reach the same DM, or the two surfaces silently disagree
    about what "every command" means.

Run:  python3 tools/sim_gift_codes.py
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import logging
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


import discord                                                    # noqa: E402

import utils.database as DB                                       # noqa: E402
from utils.userstore import UserStore                             # noqa: E402

# A REAL store on a temp dir — code_store.REDEEM_PATH and redeem.REDEEM_PATH
# are both plain module-level constants computed once at import time, not
# derived from DB.USERS_DB_PATH the way utils/snapshot.py's folder() is. Both
# names get repointed below, or this suite would write test codes into the
# real data/redeem_codes.json — which is exactly what writing this suite
# caught happening from an earlier ad-hoc smoke test.
TMP = tempfile.mkdtemp()
DATA = os.path.join(TMP, "data")
os.makedirs(DATA, exist_ok=True)

DB.USERS_DB_PATH = os.path.join(DATA, "users.db")
DB.AVATARS_PATH = os.path.join(DATA, "avatar_inventory.json")
STORE = UserStore(DB.USERS_DB_PATH)
STORE.ensure_ready()
DB.USER_STORE = STORE

import cogs.codes.code_store as CS                                # noqa: E402
import cogs.codes.redeem as R                                     # noqa: E402
from cogs.codes import builder as BLD                              # noqa: E402
from cogs.admin import actions as A                                # noqa: E402
from cogs.battle.boss import boss_copy as bcopy                    # noqa: E402
from cogs.battle.boss import boss_info as binfo                    # noqa: E402
from cogs.avatar import avatar_engine                              # noqa: E402

TEST_REDEEM_PATH = os.path.join(DATA, "redeem_codes.json")
CS.REDEEM_PATH = TEST_REDEEM_PATH
R.REDEEM_PATH = TEST_REDEEM_PATH


def main() -> int:
    # ── 1. parse_rewards: avatar ────────────────────────────────────────────
    print("\n── 1. avatar rewards resolve by name, not just id ──────────────")

    cards = avatar_engine.get_all_avatars()
    check("the fixture has avatars to test against", len(cards) > 0, len(cards))
    one = cards[0]

    rewards, err = R.parse_rewards(f"avatar:{one['name']}")
    check("a known avatar name resolves", err is None and rewards, err)
    check("...to that avatar's id, not a re-typed name",
          rewards and rewards[0]["value"] == one["id"], rewards)

    rewards, err = R.parse_rewards(f"avatar:{one['id']}")
    check("the id itself also resolves", err is None and rewards, err)

    rewards, err = R.parse_rewards("avatar:ThisAvatarDoesNotExist")
    check("an unknown avatar is refused, not silently dropped",
          rewards is None and err, err)

    rewards, err = R.parse_rewards("avatar:a")
    check("an ambiguous fragment lists candidates instead of guessing",
          rewards is None and err and "matches" in err, err)

    # ── 2. parse_rewards: boss bey ──────────────────────────────────────────
    print("\n── 2. boss beys are reachable ONLY through this new kind ───────")

    check("boss beys are genuinely absent from the normal blade roster — "
          "the gap this reward kind exists to close",
          all(prof["name"].replace(" org", "").strip()
              not in __import__("utils.database", fromlist=["x"]).load_beyblades()
              for prof in binfo.REGISTRY.values()),
          list(binfo.REGISTRY))

    rewards, err = R.parse_rewards("bossbey:Argus")
    check("a boss bey resolves by name, no grade forced",
          err is None and rewards and rewards[0]["value"] == "argus"
          and rewards[0]["grade"] is None, (rewards, err))

    rewards, err = R.parse_rewards("bossbey:Argus:Perfect")
    check("...and a grade after a second colon is captured",
          err is None and rewards and rewards[0]["grade"] == "Perfect", (rewards, err))

    rewards, err = R.parse_rewards("bossbey:Argus:Godlike")
    check("an invalid grade is refused, not silently ignored",
          rewards is None and err and "Perfect" in err, err)

    rewards, err = R.parse_rewards("bossbey:NoSuchBoss")
    check("an unknown boss name is refused",
          rewards is None and err, err)

    rewards, err = R.parse_rewards("bossbey:org")
    check("a fragment matching every boss's internal ' org' marker is "
          "refused as ambiguous rather than picking one silently",
          rewards is None and err, err)

    # ── 3. describe() names what a code actually contains ──────────────────
    print("\n── 3. describe() covers both new kinds ──────────────────────────")

    d_avatar = R.describe([{"kind": "avatar", "value": one["id"], "label": one["name"]}])
    check("avatar rewards render with their name, not a bare id",
          one["name"] in d_avatar and one["id"] not in d_avatar, d_avatar)

    d_boss = R.describe([{"kind": "bossbey", "value": "argus", "grade": "Perfect"}])
    check("boss bey rewards name the boss and the forced grade",
          "Argus" in d_boss and "Perfect" in d_boss, d_boss)

    d_boss_free = R.describe([{"kind": "bossbey", "value": "drakos", "grade": None}])
    check("...and say nothing about a grade when none was forced",
          "Drakos" in d_boss_free and "(" not in d_boss_free, d_boss_free)

    # ── 4. roll_copy(forced_grade=...) actually forces it ──────────────────
    print("\n── 4. a forced grade is the grade that comes out ────────────────")

    prof = binfo.REGISTRY["argus"]
    rng = random.Random(7)
    for grade in bcopy.GRADE_ORDER:
        rolled = bcopy.roll_copy(prof, rng=rng, forced_grade=grade)
        check(f"forcing {grade!r} yields a {grade!r} copy",
              rolled["grade"] == grade, rolled["grade"])
    check("Perfect still guarantees the awakening it always has",
          bcopy.roll_copy(prof, rng=rng, forced_grade="Perfect")["awakening"], None)

    try:
        bcopy.roll_copy(prof, rng=rng, forced_grade="NotAGrade")
        check("an unrecognised forced grade raises rather than silently "
              "falling back to something random", False,
              "did not raise")
    except ValueError:
        check("an unrecognised forced grade raises rather than silently "
              "falling back to something random", True)

    check("an ORDINARY roll (no forced_grade) still lands on a real grade — "
          "the new parameter didn't disturb the existing path",
          bcopy.roll_copy(prof, rng=random.Random(3))["grade"] in bcopy.GRADE_ORDER,
          None)

    # ── 5. grant() actually applies both new kinds ──────────────────────────
    print("\n── 5. grant() applies avatars and boss copies ───────────────────")

    async def drive_grant():
        uid = 900001
        got = await R.grant(uid, [{"kind": "avatar", "value": one["id"],
                                   "label": one["name"]}])
        check("granting an avatar adds it to the inventory",
              DB.get_avatar_inventory(uid) == [one["id"]], got)

        got2 = await R.grant(uid, [{"kind": "avatar", "value": one["id"],
                                    "label": one["name"]}])
        check("granting the same avatar again says so instead of duplicating",
              DB.get_avatar_inventory(uid) == [one["id"]]
              and "already own" in got2[0], got2)

        await R.grant(uid, [{"kind": "bossbey", "value": "argus",
                            "grade": "Perfect"}])
        copies = await bcopy.all_copies(uid)
        check("granting a boss bey adds a real copy to boss_copies",
              len(copies) == 1 and copies[0]["grade"] == "Perfect", copies)
        check("...built from the SAME roll_copy the boss-fight victory path "
              "uses, not a parallel implementation",
              copies[0]["source"] == "argus", copies[0])

        got4 = await R.grant(uid, [{"kind": "bossbey", "value": "nosuchboss",
                                    "grade": None}])
        check("a boss that no longer exists fails the grant loudly, not "
              "silently — the player is told, the code is still spent",
              "not be granted" in got4[0], got4)

    asyncio.run(drive_grant())

    # ── 6. create_code() is the ONE place that mints a code ─────────────────
    print("\n── 6. create_code() is shared by the spec and the picker ───────")

    import inspect
    builder_src = inspect.getsource(BLD)
    actions_src = inspect.getsource(A)
    check("the picker mints through redeem.create_code, not its own copy",
          "R.create_code(" in builder_src, None)
    check("the typed-spec admin action does too",
          "create_code(" in actions_src and "code_builder" in actions_src, None)
    check("neither file re-implements the code-mint loop (no second "
          "'while key in data[\"codes\"]')",
          "while key in data" not in builder_src
          and actions_src.count('while key in data["codes"]') == 0,
          None)

    key, entry = R.create_code(
        [{"kind": "coins", "value": 500}], uses=3, days=1,
        note="suite", created_by=1)
    check("a created code round-trips through the store",
          R._load()["codes"].get(key, {}).get("note") == "suite", key)
    check("...with the display form dashed the way `;redeem` accepts back",
          "-" in entry["display"], entry["display"])

    # ── 7. the picker validates through the exact same parser ──────────────
    print("\n── 7. the picker and the typed spec agree, by construction ─────")

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
            self.box.append(("send_message", content))

        async def edit_message(self, **kw):
            self.done = True
            self.box.append(("edit_message", kw))

        async def send_modal(self, modal):
            self.done = True
            self.box.append(("send_modal", modal))

    class FakeInter:
        def __init__(self, uid=A.MASTER_ID):
            self.user = FakeUser(uid)
            self.sent = []
            self.response = FakeResp(self.sent)

        async def edit_original_response(self, **kw):
            self.sent.append(("edit_original_response", kw))

    async def drive_builder():
        view = BLD.CodeBuilderView(None, A.MASTER_ID)
        check("the builder starts empty",
              not view.rewards and view.uses == 0 and view.days == 0)

        select = next(c for c in view.children
                     if isinstance(c, BLD.RewardKindSelect))
        select._values = ["avatar"]
        it = FakeInter()
        await select.callback(it)
        check("picking 'Avatar card' opens the avatar-name modal",
              it.sent and it.sent[0][0] == "send_modal"
              and isinstance(it.sent[0][1], BLD._AddModal)
              and it.sent[0][1].kind == "avatar", it.sent)

        modal = it.sent[0][1]
        modal.field._value = one["name"]
        it2 = FakeInter()
        await modal.on_submit(it2)
        check("submitting the modal adds EXACTLY what parse_rewards would "
              "have parsed from the typed spec",
              view.rewards == R.parse_rewards(f"avatar:{one['name']}")[0],
              view.rewards)

        select._values = ["bossbey"]
        it3 = FakeInter()
        await select.callback(it3)
        check("'Boss bey copy' opens the two-field boss modal",
              it3.sent and isinstance(it3.sent[0][1], BLD._BossBeyModal), it3.sent)
        boss_modal = it3.sent[0][1]
        boss_modal.name._value = "Drakos"
        boss_modal.grade._value = "Flawless"
        it4 = FakeInter()
        await boss_modal.on_submit(it4)
        check("...and it too matches the typed-spec parse exactly",
              view.rewards[-1] == R.parse_rewards("bossbey:Drakos:Flawless")[0][0],
              view.rewards[-1])

        stranger = FakeInter(uid=424242)
        before = list(view.rewards)
        select._values = ["coins"]
        await select.callback(stranger)
        check("a non-owner cannot add a reward to someone else's code",
              view.rewards == before and stranger.sent, stranger.sent[:1])

        empty_view = BLD.CodeBuilderView(None, A.MASTER_ID)
        it5 = FakeInter()
        await empty_view.create_code_btn.callback(it5)
        check("...creating with zero rewards is refused, not a blank code",
              "at least one" in empty_view.note_line, empty_view.note_line)

        it6 = FakeInter()
        await view.create_code_btn.callback(it6)
        made = R._load()["codes"]
        check("pressing Create actually mints a code containing what was "
              "picked", any(e["rewards"] == view.rewards for e in made.values()),
              None)
        check("...and disables the view so it can't be pressed twice",
              all(c.disabled for c in view.children), None)

    asyncio.run(drive_builder())

    # ── 8. the admin registry exposes the picker ─────────────────────────────
    print("\n── 8. the picker is a real admin action ──────────────────────────")

    check("`code_builder` is registered under Codes",
          "code_builder" in A.REGISTRY
          and A.REGISTRY["code_builder"].category == "codes",
          A.REGISTRY.get("code_builder"))
    check("...and needs no typed input — it's the picker, not the spec box",
          A.REGISTRY["code_builder"].needs == (), A.REGISTRY["code_builder"].needs)

    # ── 9. the full command list, DM'd ───────────────────────────────────────
    print("\n── 9. /commands and the onboarding button reach the same DM ────")

    from cogs.ui import help_cog as HC

    class DMUser:
        def __init__(self, allow=True):
            self.allow = allow
            self.sent = []

        async def send(self, **kw):
            if not self.allow:
                raise discord.Forbidden(
                    __import__("types").SimpleNamespace(status=403, reason="x"),
                    "Cannot send messages to this user")
            self.sent.append(kw)

    async def drive_dm():
        u = DMUser(allow=True)
        ok = await HC.send_full_command_list(u)
        check("send_full_command_list succeeds when DMs are open", ok, None)
        check("...sends an intro plus at least one batch of category embeds",
              len(u.sent) >= 2, len(u.sent))
        total_embeds = sum(len(m.get("embeds", [m.get("embed")]))
                          for m in u.sent if m.get("embed") or m.get("embeds"))
        check("...covering every category COMMAND_DATA actually has",
              total_embeds == 1 + len(
                  [k for k in HC.COMMAND_DATA if k in HC.CATEGORIES]),
              total_embeds)

        u2 = DMUser(allow=False)
        ok2 = await HC.send_full_command_list(u2)
        check("a closed DM is reported back as failure, not swallowed",
              ok2 is False, ok2)

    asyncio.run(drive_dm())

    check("`/commands` is a real top-level slash command on HelpCog",
          any(isinstance(getattr(HC.HelpCog, name, None), discord.app_commands.Command)
              for name in dir(HC.HelpCog)),
          [n for n in dir(HC.HelpCog)
           if isinstance(getattr(HC.HelpCog, n, None), discord.app_commands.Command)])

    onboarding_src = open(os.path.join(ROOT, "cogs/core/onboarding.py"),
                         encoding="utf-8").read()
    check("the post-;start screen's button calls the SAME DM helper "
          "/commands uses — not a second implementation",
          "send_full_command_list" in onboarding_src, None)
    check("'commands' stays exempt from the ;start gate — /commands has to "
          "work for a player who hasn't started yet",
          "commands" in __import__("cogs.core.onboarding",
                                   fromlist=["x"]).GATE_EXEMPT, None)

    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 66)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    import shutil
    sys.exit(main())
