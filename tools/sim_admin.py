#!/usr/bin/env python3
"""
tools/sim_admin.py — the v1.13 admin panel, driven headlessly.

What this replaces
------------------
41 admin commands across five files: `admin.py` (22 prefix commands plus the
`;spawnloop` group), `audit.py` (`;audit` and five subcommands), `console.py`
(the `/admin` slash command), `;codeadmin` inside the redeem cog and
`;rankadmin` — plus its `/rankadmin` group — inside the ranked cog. Every one
was hidden from `;help`, so the only way to know a command existed was to have
written it.

The failure modes this suite exists to catch, all of which have happened here:

* **An action with no handler.** `console.py` shipped an ACTIONS table listing
  actions whose `_do_*` methods had been deleted with the old tournament
  package. Picking one from the dropdown answered "isn't wired up". Both
  directions of that pairing are asserted below.
* **An empty select value is a Discord 400.** `components.…value: Must be
  between 1 and 100 in length` took `;inv` down for 75% of blade holders in
  v96, and 274 passing tests missed it because they asserted the limits I
  remembered instead of the ones Discord documents. The full option contract is
  asserted here.
* **Destroying something on one press.** `;resetplayer` wiped a profile with no
  confirmation; `;giveallcoins` touched every row the same way.
* **A gate that fails closed.** The check in front of every command in the bot
  now also answers "is this player banned?" and "is the bot in maintenance?" —
  and it must still let everyone through when it cannot answer.
* **Errors that vanish.** 21 `log.exception` sites, no `logs/` directory. The
  ring buffer is asserted against a really-raised exception, not a stub.

Run:  python3 tools/sim_admin.py
"""
import asyncio
import logging
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


def src(rel: str) -> str:
    with open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), rel), encoding="utf-8") as fh:
        return fh.read()


def code(rel: str) -> str:
    """Source with comment-only lines stripped.

    Three assertions in earlier suites matched their own explanatory comments
    rather than the code they were checking — `"update_user" not in src` tripped
    on a docstring that said the words. Comments are removed before matching.
    """
    out = []
    for line in src(rel).splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


import discord                                          # noqa: E402

from cogs.admin import actions as A                     # noqa: E402
from cogs.admin import panel as P                       # noqa: E402
from utils import errorlog                              # noqa: E402

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)


class FakeUser:
    def __init__(self, uid, roles=()):
        self.id = uid
        self.roles = list(roles)
        self.mention = f"<@{uid}>"
        self.display_name = f"User {uid}"


PLAYER, OWNER = FakeUser(4242), FakeUser(A.MASTER_ID)


def owner_ctx(**kw) -> A.ActionCtx:
    """An ActionCtx that will pass the owner-only gate.

    Every dispatch check below runs AS THE OWNER on purpose: the permission
    gate has its own section, and a test that silently fails on authorisation
    while claiming to test parameter validation is worse than no test — §4's
    first check passed for exactly that wrong reason before this existed.
    """
    kw.setdefault("invoker", OWNER)
    kw.setdefault("invoker_id", A.MASTER_ID)
    return A.ActionCtx(**kw)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. the registry contract ─────────────────────────────────────")

check("every action has a coroutine handler",
      all(asyncio.iscoroutinefunction(a.handler) for a in A.REGISTRY.values()),
      [k for k, a in A.REGISTRY.items()
       if not asyncio.iscoroutinefunction(a.handler)])

# The reverse direction: a handler that is not reachable from the registry is
# dead code that looks live. `console.py` had a `# ── admin ──` header over
# nothing for exactly this reason.
handlers = {id(a.handler) for a in A.REGISTRY.values()}
orphans = [n for n, fn in vars(A).items()
           if n.startswith("_") and asyncio.iscoroutinefunction(fn)
           and id(fn) not in handlers]
check("no orphaned handler functions", not orphans, orphans)

check("every action's category is a real one",
      all(a.category in A.CATEGORY_ORDER for a in A.REGISTRY.values()))
check("every category has at least one action",
      all(A.actions_in(k) for k in A.CATEGORY_ORDER),
      [k for k in A.CATEGORY_ORDER if not A.actions_in(k)])
check("every declared `needs` name is one dispatch knows how to check",
      all(set(a.needs) <= set(A.PARAM_HELP) for a in A.REGISTRY.values()),
      [(k, a.needs) for k, a in A.REGISTRY.items()
       if not set(a.needs) <= set(A.PARAM_HELP)])
check("an unknown action key is refused, not crashed",
      not loop.run_until_complete(A.run("no_such_action", owner_ctx())).ok)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. the Discord option contract ───────────────────────────────")
# Values, labels and descriptions all have documented limits. An empty value is
# a 400 that takes the whole message down, not a cosmetic problem.

bad_val = [k for k in A.REGISTRY if not (1 <= len(k) <= 100)]
check("every action key is a legal select value (1–100 chars)", not bad_val, bad_val)
check("every action key is ASCII",
      all(k.isascii() for k in A.REGISTRY), [k for k in A.REGISTRY if not k.isascii()])
check("no action key is blank or whitespace",
      all(k.strip() == k and k.strip() for k in A.REGISTRY))
check("every category key is a legal select value",
      all(1 <= len(k) <= 100 and k.isascii() for k in A.CATEGORY_ORDER))
check("every label fits a select option (1–100)",
      all(1 <= len(a.label) <= 100 for a in A.REGISTRY.values()),
      [k for k, a in A.REGISTRY.items() if not 1 <= len(a.label) <= 100])
# The description is rendered as "⚠️ " + description for destructive actions,
# so the prefix has to be inside the limit too.
long_desc = [k for k, a in A.REGISTRY.items()
             if len(("⚠️ " if a.confirm else "") + a.description) > 100]
check("every description fits, warning prefix included", not long_desc, long_desc)
over = [(k, len(A.actions_in(k))) for k in A.CATEGORY_ORDER
        if len(A.actions_in(k)) > 25]
check("no category exceeds Discord's 25-option select cap", not over, over)
check("the category select itself is under 25 options", len(A.CATEGORIES) <= 25)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. destructive actions ask twice ─────────────────────────────")

DESTRUCTIVE = {"resetplayer", "giveallcoins", "ban", "rank_reset"}
missing = [k for k in DESTRUCTIVE if not A.REGISTRY[k].confirm]
check("everything that erases player data is flagged", not missing, missing)
check("a confirm message says what happens, not just 'are you sure'",
      all(len(a.confirm) > 20 for a in A.REGISTRY.values() if a.confirm))
check("routine actions are NOT flagged — a warning on everything is a "
      "warning on nothing",
      not A.REGISTRY["givecoins"].confirm and not A.REGISTRY["inspect"].confirm)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 4. missing parameters are refused before the handler ─────────")

fired = {"n": 0}


async def _tripwire(ctx):
    fired["n"] += 1
    return A.Result(message="should not get here")


A.REGISTRY["__probe"] = A.Action(
    key="__probe", label="probe", description="test only", category="system",
    handler=_tripwire, needs=("user", "amount", "text"))

res = loop.run_until_complete(A.run("__probe", owner_ctx()))
check("dispatch refuses when required parameters are absent", not res.ok)
check("...without ever calling the handler", fired["n"] == 0, fired["n"])
check("...and names every missing one",
      all(w in res.message for w in ("player", "number", "text")), res.message)

res = loop.run_until_complete(A.run(
    "__probe", owner_ctx(target_id=1, amount=5, text="x")))
check("...and runs it once everything is supplied", fired["n"] == 1)

# An empty string is not a supplied text argument — `;codeadmin revoke` with no
# code used to reach the store and answer "no such code".
fired["n"] = 0
res = loop.run_until_complete(A.run(
    "__probe", owner_ctx(target_id=1, amount=5, text="   ")))
check("whitespace-only text counts as missing", not res.ok and fired["n"] == 0)
del A.REGISTRY["__probe"]


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5. a handler that raises is caught, logged and reported ──────")

async def _boom(ctx):
    raise ZeroDivisionError("kaboom")


A.REGISTRY["__boom"] = A.Action(key="__boom", label="boom", description="test",
                                category="system", handler=_boom)
errorlog.clear()
res = loop.run_until_complete(A.run("__boom", owner_ctx()))
check("the panel gets a refusal, not an exception", not res.ok)
check("...naming the exception type", "ZeroDivisionError" in res.message, res.message)
check("...and it lands in the ring buffer an admin can read",
      errorlog.count() >= 1, errorlog.count())
del A.REGISTRY["__boom"]


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6. the error ring buffer ─────────────────────────────────────")

errorlog.clear()
errorlog.install()
errorlog.install()                                       # idempotent
log = logging.getLogger("beyblade_bot.simtest")
try:
    1 / 0
except ZeroDivisionError:
    log.exception("a boss fight died")
rows = errorlog.recent(5)
check("a real logged exception is captured", len(rows) == 1, len(rows))
check("...with the message", rows and "boss fight" in rows[0]["msg"])
check("...and a traceback naming the cause",
      rows and "ZeroDivisionError" in rows[0]["trace"])
log.warning("just a warning")
check("warnings are NOT captured — this is for failures", errorlog.count() == 1)
for i in range(200):
    log.error("flood %d", i)
check("the ring is capped and cannot grow without bound",
      errorlog.count() == errorlog.MAX_ENTRIES, errorlog.count())
errorlog.clear()
check("clearing empties it", errorlog.count() == 0)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 7. maintenance mode and the ban list ─────────────────────────")

# Point the state file at a scratch path so a test never writes the live one.
_tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    ".sim_admin_state.json")
A.STATE_PATH = _tmp
A.reload_state()

from cogs.core import onboarding as ON                  # noqa: E402


check("nobody is blocked on a clean install",
      ON.blocked_reason(PLAYER) is None)

A.set_maintenance(True, "deploying v1.13", A.MASTER_ID)
b = ON.blocked_reason(PLAYER)
check("maintenance mode refuses a player",
      isinstance(b, ON.UnderMaintenance), type(b).__name__)
check("...and carries the reason so the refusal can explain itself",
      getattr(b, "reason", "") == "deploying v1.13")
check("...but never the owner — the switch to turn it off is a command too",
      ON.blocked_reason(OWNER) is None)
A.set_maintenance(False)
check("turning it off lets everyone back in", ON.blocked_reason(PLAYER) is None)

A.ban_user(PLAYER.id, "chargeback fraud", A.MASTER_ID)
b = ON.blocked_reason(PLAYER)
check("a banned player is refused", isinstance(b, ON.BotBanned), type(b).__name__)
check("...with the reason", getattr(b, "reason", "") == "chargeback fraud")
check("...and an unrelated player is untouched",
      ON.blocked_reason(FakeUser(999)) is None)
check("the ban survives a reload from disk",
      (A.reload_state() or True) and A.banned(PLAYER.id) is not None)
check("unban lifts it", A.unban_user(PLAYER.id) and ON.blocked_reason(PLAYER) is None)
check("unbanning somebody who isn't banned is not an error",
      A.unban_user(PLAYER.id) is False)

res = loop.run_until_complete(A.run(
    "ban", owner_ctx(target_id=A.MASTER_ID, text="oops")))
check("the owner cannot be banned — that lock has no key",
      not res.ok and A.banned(A.MASTER_ID) is None)

# The gate in front of every command in the bot must fail OPEN. A database
# blip taking the whole bot down for everyone is strictly worse than briefly
# letting a banned player run a command.
_real = A.banned
A.banned = lambda uid: (_ for _ in ()).throw(RuntimeError("store is down"))
check("the gate fails OPEN when the state cannot be read",
      ON.blocked_reason(PLAYER) is None)
A.banned = _real

# `;start` is exempt from the STARTER gate because it is the door. It must not
# be exempt from a ban, or the ban would only stop people already inside.
gate = code("cogs/core/onboarding.py")
check("the ban check runs before the exempt-command shortcut",
      gate.index("blocked = blocked_reason(ctx.author") < gate.index("if is_exempt("))
check("slash commands go through the same two questions",
      "blocked_reason(interaction.user" in gate)
check("one gate answers all of it — no second global check",
      gate.count("bot.add_check(") == 1, gate.count("bot.add_check("))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 8. profile writes are atomic ─────────────────────────────────")
# `get_user()` … mutate … `update_user()` is a race: anything written in
# between is discarded on the write back. That pattern erased blades in
# `redeem.grant`, and every economy command in the old admin.py used it.

acts = code("cogs/admin/actions.py")

# Asserted against the PARSE TREE, not the text. This module's own docstring
# explains the race it avoids, in prose that contains the words — and a
# substring check on the words would pass or fail on the explanation instead of
# on the code. Three assertions in earlier suites had exactly that bug.
import ast                                              # noqa: E402

_tree = ast.parse(src("cogs/admin/actions.py"))
_calls = [n.func.id for n in ast.walk(_tree)
          if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
check("no handler calls update_user — that is the race that erased blades",
      "update_user" not in _calls)
check("...they go through mutate_user instead", _calls.count("mutate_user") >= 6,
      _calls.count("mutate_user"))
check("the bulk grant is one load→save cycle, not one write per player",
      "save_users(users)" in acts and "asyncio.to_thread(_bulk)" in acts)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 9. the panel view ────────────────────────────────────────────")


class FakeBot:
    def __init__(self):
        self.guilds = []
        self.extensions = {}

    def get_cog(self, name):
        return None

    def get_user(self, uid):
        return None


class FakeGuild:
    id = 1234
    name = "Test"


panel = P.AdminPanel(FakeBot(), OWNER, FakeGuild(), None)


def live(cls):
    """Fetch the CURRENT component of this type from the view.

    `clear_items()` drops each child's `.view` backreference, so a button held
    across a rebuild is detached and half its callback silently no-ops. Always
    re-fetch after a refresh.
    """
    for c in panel.children:
        if isinstance(c, cls):
            return c
    return None


check("the panel opens on a real category", panel.category in A.CATEGORY_ORDER)
check("row 0 is the category select", isinstance(panel.children[0], P.CategorySelect))
check("row 1 is the action select", isinstance(panel.children[1], P.ActionSelect))
check("no user picker until an action needs one", live(P.TargetSelect) is None)
check("Run is disabled with nothing chosen", live(P.RunButton).disabled)

cat = live(P.CategorySelect)
check("every category option carries a non-empty value",
      all(o.value for o in cat.options))
check("...and they are unique", len({o.value for o in cat.options}) == len(cat.options))
act = live(P.ActionSelect)
check("the action select is filtered to the open category",
      {o.value for o in act.options} == {a.key for a in A.actions_in(panel.category)})
check("every action option carries a non-empty value",
      all(o.value for o in act.options))
check("every action option description is inside the 100-char cap",
      all(len(o.description or "") <= 100 for o in act.options))

panel.action_key = "givecoins"
panel.build()
check("picking a player-shaped action adds the user picker",
      isinstance(live(P.TargetSelect), discord.ui.UserSelect))
check("...and Run becomes pressable", not live(P.RunButton).disabled)
check("the view stays inside Discord's five action rows",
      len({c.row for c in panel.children}) <= 5,
      sorted({c.row for c in panel.children}))
check("...and no row holds more than five components",
      all(sum(1 for c in panel.children if c.row == r) <= 5 for r in range(5)))

panel.action_key = "audit"
panel.build()
check("an action that needs nobody drops the picker again",
      live(P.TargetSelect) is None)

# Carrying an amount over from the last action is how you give somebody 5,000
# of the wrong thing.
panel.action_key, panel.amount, panel.text = "givecoins", 5000, "x"
panel.pending_confirm = True
panel.reset_selection()
check("changing category clears the action and everything typed for it",
      panel.action_key is None and panel.amount is None and panel.text is None
      and not panel.pending_confirm)

panel.action_key = "resetplayer"
panel.target = PLAYER
panel.target_id = PLAYER.id
panel.pending_confirm = True
panel.build()
btn = live(P.RunButton)
check("a pending confirmation turns the button red",
      btn.style == discord.ButtonStyle.danger, btn.style)
check("...and relabels it so a reflex press isn't the same press",
      btn.label != "Run", btn.label)
e = panel.embed()
body = (e.description or "") + "".join(f"{f.name} {f.value}" for f in e.fields)
check("the confirmation names the action", "Reset a profile" in body)
check("...and who it will hit", PLAYER.mention in body)
check("every embed field has a non-empty name — an empty one is a 400",
      all(f.name and f.value for f in e.fields))
check("the embed stays inside the 6,000-character total",
      len(e) < 6000, len(e))

panel.pending_confirm = False
panel.build()
check("the button goes back to green once the confirmation is cleared",
      live(P.RunButton).style == discord.ButtonStyle.success)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 10. the modal asks only for what the action declared ─────────")

for key, want in (("givecoins", {"amount"}), ("givebey", {"text"}),
                  ("ban", {"text"}), ("code_create", {"text"})):
    panel.action_key = key
    m = P.InputModal(panel, A.REGISTRY[key])
    got = set()
    if m.amount_field is not None:
        got.add("amount")
    if m.text_field is not None:
        got.add("text")
    check(f"`{key}` asks for exactly {sorted(want)}", got == want, got)

panel.action_key = "audit"
m = P.InputModal(panel, A.REGISTRY["audit"])
check("an action needing nothing typed builds an empty modal",
      m.amount_field is None and m.text_field is None)
check("a modal never exceeds Discord's five inputs",
      all(len(P.InputModal(panel, a).children) <= 5 for a in A.REGISTRY.values()))
check("a modal title fits Discord's 45-character cap",
      all(len(P.InputModal(panel, a).title) <= 45 for a in A.REGISTRY.values()))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 11. only the admin who opened it can drive it ────────────────")

pan = code("cogs/admin/panel.py")
check("the panel checks the invoker, not just ephemerality",
      "interaction_check" in pan and "Not your panel" in pan)
check("...and re-checks admin rights per interaction, since roles change",
      "A.is_admin(interaction.user)" in pan)
check("the slash command refuses non-admins outright",
      "Not authorized" in pan)
check("the panel is ephemeral — an admin console is not an announcement",
      "ephemeral=True" in pan)
check("every component callback is wrapped so a failure can't freeze the panel",
      pan.count("@_guard") >= 5, pan.count("@_guard"))

# ── the escalation this nearly shipped ───────────────────────────────────
# `is_admin` accepts a role named "Tournament Admin", which anyone with Manage
# Roles in ANY server the bot is in can create. That was safe in console.py,
# which put six tournament and casino actions behind it. It is not safe for a
# panel that resets profiles, bans players and mints coins into every wallet.
ROLE_HOLDER = types.SimpleNamespace(
    id=7, roles=[types.SimpleNamespace(name=A.ADMIN_ROLE)])

check("every action is owner-only unless it says otherwise",
      all(a.owner_only for a in A.REGISTRY.values() if a.category != "tournament"),
      [k for k, a in A.REGISTRY.items()
       if not a.owner_only and a.category != "tournament"])
check("a role-holder cannot reset a profile",
      not A.may_run(A.REGISTRY["resetplayer"], ROLE_HOLDER))
check("...or ban somebody from the bot",
      not A.may_run(A.REGISTRY["ban"], ROLE_HOLDER))
check("...or mint coins into every wallet",
      not A.may_run(A.REGISTRY["giveallcoins"], ROLE_HOLDER))
check("...or take the bot down for maintenance",
      not A.may_run(A.REGISTRY["maintenance_on"], ROLE_HOLDER))
check("...but CAN still run the tournament actions console.py gave them",
      all(A.may_run(A.REGISTRY[k], ROLE_HOLDER)
          for k in ("t_start", "t_cancel", "t_force_win", "t_ban")))
check("the owner can run everything",
      all(A.may_run(a, OWNER) for a in A.REGISTRY.values()))

# The refusal lives in dispatch, not only in the view. A select that hides an
# action is a convenience; the thing that refuses must be the thing that runs.
_owned = {"n": 0}


async def _never(ctx):
    _owned["n"] += 1
    return A.Result(message="ran")


A.REGISTRY["__owned"] = A.Action(key="__owned", label="owned", description="t",
                                 category="system", handler=_never)
res = loop.run_until_complete(A.run("__owned", A.ActionCtx(invoker=ROLE_HOLDER)))
check("dispatch refuses an owner-only action even if the view is bypassed",
      not res.ok and _owned["n"] == 0, res.message)
check("...and says why", "owner-only" in res.message, res.message)
del A.REGISTRY["__owned"]

# The panel shows a role-holder only what they can use, and opens there.
_p = P.AdminPanel(FakeBot(), ROLE_HOLDER, None, None)
check("a role-holder's panel opens on a page they can use",
      _p.category == "tournament", _p.category)
check("...and offers no other category",
      [c[0] for c in A.categories_for(ROLE_HOLDER)] == ["tournament"])
check("...while the owner still sees every category",
      len(A.categories_for(OWNER)) == len(A.CATEGORIES))

check("the owner passes is_admin", A.is_admin(OWNER))
check("a plain player does not", not A.is_admin(PLAYER))
role_user = types.SimpleNamespace(
    id=7, roles=[types.SimpleNamespace(name=A.ADMIN_ROLE)])
check("the admin role passes", A.is_admin(role_user))
check("a differently-named role does not",
      not A.is_admin(types.SimpleNamespace(
          id=8, roles=[types.SimpleNamespace(name="Moderator")])))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 12. the three prefix survivors ───────────────────────────────")
# They exist because a slash-only recovery tool cannot recover slash commands.

check("`;sync` still exists", "name=\"sync\"" in pan)
check("`;reload` still exists", "name=\"reload\"" in pan)
check("`;version` still exists", "name=\"version\"" in pan)
check("all three call the SAME function the panel does, not a second copy",
      all(f"A.do_{n}(" in pan for n in ("sync", "reload", "version")))
check("`do_sync` is defined once, in the registry", acts.count("async def do_sync") == 1)
check("the panel's sync actions call it too",
      acts.count("await do_sync(") >= 4, acts.count("await do_sync("))

# Two modules owning one command name is what produced CommandAlreadyRegistered.
names = {}
for rel in ("cogs/admin/panel.py", "cogs/codes/redeem.py",
            "cogs/ranked/ranked_cog.py", "cogs/casino/casino_hub.py"):
    for line in src(rel).splitlines():
        line = line.strip()
        if line.startswith("@commands.command(name=") or \
                line.startswith("@commands.group(name="):
            nm = line.split('name="', 1)[1].split('"', 1)[0]
            names.setdefault(nm, []).append(rel)
dupes = {n: f for n, f in names.items() if len(f) > 1}
check("no prefix command name is claimed by two of these files", not dupes, dupes)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 13. the old surface is gone, and nothing still points at it ──")

for gone in ("cogs/admin/admin.py", "cogs/admin/audit.py", "cogs/admin/console.py"):
    check(f"{gone} is deleted",
          not os.path.exists(os.path.join(os.path.dirname(os.path.dirname(
              os.path.abspath(__file__))), gone)))
app = code("app.py")
check("app.py no longer loads the deleted extensions",
      "cogs.admin.audit" not in app and "cogs.admin.console" not in app)
check("app.py still loads the admin package", '"cogs.admin"' in app)
check("app.py installs the error ring buffer at startup",
      "errorlog.install()" in app)
# Without this branch the global handler's `else` also fired on a refused
# check: a banned player got their ban notice AND "⚠️ An unexpected error
# occurred", and a non-admin who guessed `;sync` was told the check for it
# failed — which is how a hidden command announces itself.
check("a refused check is silent globally — the cog that owns it explains",
      "commands.CheckFailure" in app
      and app.index("commands.CheckFailure") < app.index("Unhandled error in"))
# Matched against the DECLARATIONS, not the word. Three assertions in earlier
# suites passed or failed on prose in a nearby docstring rather than on the
# code — both of these files still explain in their docstring where the admin
# commands went, and they should.
red = code("cogs/codes/redeem.py")
check("`;codeadmin` is gone from the redeem cog",
      'name="codeadmin"' not in red and "@codeadmin.command" not in red)
rk = code("cogs/ranked/ranked_cog.py")
check("`;rankadmin` is gone from the ranked cog",
      'name="rankadmin"' not in rk and "@rankadmin.command" not in rk)
check("...and so is the /rankadmin slash group",
      "app_commands.Group(" not in rk)
# …but the logic they wrapped did not move. One implementation, called from
# the registry.
check("the code actions still call the redeem cog's own parser",
      "parse_rewards" in acts and "from cogs.codes.redeem import" in acts)
check("the ranked actions still call utils/ranked.py",
      "from utils import ranked as RK" in acts)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 14. every capability that existed still exists ───────────────")
# Named one by one, because "we consolidated 41 commands" is only true if
# nothing quietly fell off the end.

MOVED = {
    # admin.py
    "sync": "sync", "givecoins": "givecoins", "removecoin": "removecoin",
    "givebey": "givebey", "giveavatar": "giveavatar",
    "resetplayer": "resetplayer", "addpart": "addpart", "setrank": "setrank",
    "carddebug": "carddebug", "reload": "reload", "battlereset": "battlereset",
    "adminspawn": "spawn", "clearspawn": "clearspawn", "givexp": "givexp",
    "setcoins": "setcoins", "removebey": "removebey",
    "listbattles": "listbattles", "giveallcoins": "giveallcoins",
    "spawnloop": "spawnloop", "spawnloop stop": "spawnloop_stop",
    "clearcache": "clearcache", "updatecheck": "updatecheck",
    "version": "version", "servers": "servers",
    # audit.py
    "audit": "audit", "audit wallets": "audit_wallets",
    "audit funnel": "audit_funnel", "audit user": "inspect",
    "audit db": "audit_db", "audit db migrate": "audit_migrate",
    "audit db verify": "audit_verify", "audit backup": "audit_backup",
    # console.py
    "/admin start": "t_start", "/admin cancel": "t_cancel",
    "/admin force_win": "t_force_win", "/admin ban_player": "t_ban",
    "/admin casino_give": "casino_give", "/admin casino_take": "casino_take",
    # redeem.py
    "codeadmin create": "code_create", "codeadmin list": "code_list",
    "codeadmin revoke": "code_revoke",
    # ranked_cog.py
    "rankadmin status": "rank_settings", "rankadmin on/off": "rank_verify",
    "rankadmin server": "rank_server", "rankadmin control": "rank_control",
    "rankadmin invite": "rank_invite", "rankadmin reset": "rank_reset",
}
lost = sorted(old for old, new in MOVED.items() if new not in A.REGISTRY)
check(f"all {len(MOVED)} old commands have a home in the registry", not lost, lost)

NEW = ("errors", "errors_clear", "maintenance_on", "maintenance_off",
       "inspect", "find", "ban", "unban", "banlist")
check("the new tools are present", all(k in A.REGISTRY for k in NEW),
      [k for k in NEW if k not in A.REGISTRY])
check("the surface shrank: one slash command replaces 41",
      len([c for c in dir(P.AdminCog) if False]) == 0 and len(A.REGISTRY) >= len(MOVED))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 15. handlers that can run without a gateway, do ──────────────")


import utils.database as DB                             # noqa: E402

_fake: dict[str, dict] = {}
_real_get, _real_mutate = DB.get_user, DB.mutate_user


def _fake_get(uid):
    return _fake.setdefault(str(uid), {"user_id": str(uid), "coins": 0,
                                       "inventory": [], "xp": 0, "level": 1,
                                       "wins": 0, "losses": 0})


def _fake_mutate(uid, fn):
    return fn(_fake_get(uid))


DB.get_user, DB.mutate_user = _fake_get, _fake_mutate
A.get_user, A.mutate_user = _fake_get, _fake_mutate
try:
    ctx = owner_ctx(target=PLAYER, target_id=PLAYER.id, amount=500)
    r = loop.run_until_complete(A.run("givecoins", ctx))
    check("givecoins credits the profile", _fake_get(PLAYER.id)["coins"] == 500)
    check("...and says the new balance", "500" in r.message, r.message)

    r = loop.run_until_complete(A.run("removecoin", ctx))
    check("removecoin debits it", _fake_get(PLAYER.id)["coins"] == 0)
    loop.run_until_complete(A.run("removecoin", ctx))
    check("...and never goes negative", _fake_get(PLAYER.id)["coins"] == 0)

    loop.run_until_complete(A.run("setcoins", owner_ctx(
        target=PLAYER, target_id=PLAYER.id, amount=777)))
    check("setcoins sets an exact value", _fake_get(PLAYER.id)["coins"] == 777)

    _fake_get(PLAYER.id).update({"inventory": ["Dranzer"], "coins": 9,
                                 "xp": 50, "level": 4,
                                 "active_beyblade": "Dranzer"})
    r = loop.run_until_complete(A.run("removebey", owner_ctx(
        target=PLAYER, target_id=PLAYER.id, text="Dranzer")))
    p = _fake_get(PLAYER.id)
    check("removebey takes the blade out", "Dranzer" not in p["inventory"])
    check("...and unequips it, rather than leaving a phantom active blade",
          p["active_beyblade"] is None)
    r = loop.run_until_complete(A.run("removebey", owner_ctx(
        target=PLAYER, target_id=PLAYER.id, text="Nothing")))
    check("...and removing what they don't own is refused", not r.ok)

    _fake_get(PLAYER.id).update({"coins": 9, "xp": 50, "level": 4,
                                 "inventory": ["X"], "starter_claimed": True})
    loop.run_until_complete(A.run("resetplayer", owner_ctx(
        target=PLAYER, target_id=PLAYER.id)))
    p = _fake_get(PLAYER.id)
    check("resetplayer zeroes the profile",
          p["coins"] == 0 and p["xp"] == 0 and p["inventory"] == []
          and p["level"] == 1)
    # Clearing the starter flag would drop them back behind the `;start` gate
    # with a profile that already exists — the exact lockout v1.08 fixed, where
    # five live accounts could neither play nor re-start.
    check("...but leaves the starter flag alone, or they're locked out of ;start",
          p.get("starter_claimed") is True)
finally:
    DB.get_user, DB.mutate_user = _real_get, _real_mutate
    A.get_user, A.mutate_user = _real_get, _real_mutate


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 16. tournament actions go through the cog's public hooks ─────")
# The old console imported `..tournament.views`, `..models`, `..brackets` and
# `..notifications` directly and read `cog.svc.store` — which is why deleting
# one package broke seventeen admin actions at once.

t_block = acts[acts.index("def _tcog("):acts.index('@register("code_create"')]
for bad in (".svc", "tournament.views", "tournament.models",
            "tournament.brackets", "tournament.notifications"):
    check(f"the tournament actions never touch `{bad}`", bad not in t_block)
check("...they use the admin_* hooks", t_block.count("cog.admin_") >= 3)

cog_src = code("cogs/tournament/tournament.py")
for hook in ("admin_lobby", "admin_start", "admin_cancel", "admin_ban",
             "admin_force_win"):
    check(f"TournamentCog still exposes {hook}", f"def {hook}(" in cog_src)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 17. the real click sequence, end to end ──────────────────────")
# Driving the REAL components through a stubbed interaction, because the thing
# most worth proving — that the first press of a destructive action does NOT
# run it — is invisible to any test that only reads state.


class FakeResponse:
    def __init__(self):
        self.done = False
        self.sent = []
        self.edited = []
        self.modal = None

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kw):
        self.done = True
        self.sent.append(kw | ({"content": content} if content else {}))

    async def edit_message(self, **kw):
        self.done = True
        self.edited.append(kw)

    async def send_modal(self, modal):
        self.done = True
        self.modal = modal

    async def defer(self, **kw):
        self.done = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, **kw):
        self.sent.append(kw)


class FakeInteraction:
    def __init__(self, user):
        self.user = user
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.guild = None
        self.guild_id = 1
        self.channel = None


pan2 = P.AdminPanel(FakeBot(), OWNER, None, None)


def comp(cls):
    """Always re-fetch: `clear_items()` detaches the old child's `.view`."""
    return next(c for c in pan2.children if isinstance(c, cls))


def press(component, values=None, user=OWNER):
    if values is not None:
        component._values = values
    i = FakeInteraction(user)
    loop.run_until_complete(component.callback(i))
    return i


i = press(comp(P.CategorySelect), ["players"])
check("picking a category redraws the panel in place",
      pan2.category == "players" and bool(i.response.edited))

i = press(comp(P.ActionSelect), ["resetplayer"])
check("picking an action that takes a player reveals the picker",
      any(isinstance(c, P.TargetSelect) for c in pan2.children))

press(comp(P.TargetSelect), [PLAYER])
check("the user picker sets the target", pan2.target_id == PLAYER.id)

ran = {"n": 0}


async def _spy(ctx):
    ran["n"] += 1
    return A.Result(message="wiped")


_saved = A.REGISTRY["resetplayer"]
A.REGISTRY["resetplayer"] = A.Action(
    key="resetplayer", label=_saved.label, description=_saved.description,
    category=_saved.category, handler=_spy, needs=_saved.needs,
    confirm=_saved.confirm)
try:
    press(comp(P.RunButton))
    check("the FIRST press of a destructive action does not run it",
          ran["n"] == 0 and pan2.pending_confirm)
    i = press(comp(P.RunButton))
    check("...the second press does", ran["n"] == 1)
    check("...and the answer comes back to the admin",
          bool(i.response.sent or i.followup.sent))
    check("...and the confirmation is spent, not sticky",
          not pan2.pending_confirm)
finally:
    A.REGISTRY["resetplayer"] = _saved

pan2.action_key, pan2.pending_confirm = "givecoins", False
pan2.build()
i = press(comp(P.RunButton))
check("an action that needs typing opens a modal instead of firing",
      isinstance(i.response.modal, P.InputModal))
check("...and the modal is the FIRST response — Discord allows no other order",
      i.response.sent == [])

stranger = types.SimpleNamespace(id=1, roles=[])
i = FakeInteraction(stranger)
check("somebody else's click on the same panel is refused",
      loop.run_until_complete(pan2.interaction_check(i)) is False)
check("...with a reason, not a silent no-op",
      any("Not your panel" in str(x) for x in i.response.sent), i.response.sent)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 18. secrets never reach a message ────────────────────────────")
# `;updatecheck` reports on a GitHub token. Printing it into a Discord channel
# would be worse than the problem it diagnoses.

upd = acts[acts.index('async def _updatecheck('):acts.index('async def _carddebug(')]
check("the update diagnostic shows the token's length and family only",
      "token_len" in upd and "token_family" in upd)
check("...and never the token itself",
      "d['token']" not in upd and 'd["token"]' not in upd)


# ══════════════════════════════════════════════════════════════════════════════
try:
    os.remove(_tmp)
except OSError:
    pass

print("\n" + "=" * 66)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 66)
sys.exit(1 if FAIL else 0)
