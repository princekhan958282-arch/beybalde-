#!/usr/bin/env python3
"""
tools/sim_panels.py — the shared panel kit, and the five commands built on it.

What this covers
----------------
Discord lists the subcommands of a group FLAT in the picker, so reaching nine
features used to cost twenty-eight lines: `/player` 7, `/casino` 7, `/avatar`
5, `/story` 4, plus `/leaderboard`, `/rank`, `/verify` and the three that were
already single commands. v1.14 put all of them behind one view — the same view
`/admin` uses — and the picker went to six lines. v1.18 added `/leaderboard`
and `/trade` on the same kit, so two more features cost two more lines rather
than nine.

The failures worth testing, all of which have happened here
-----------------------------------------------------------
* **An empty select value is a Discord 400.** `components.…value: Must be
  between 1 and 100 in length` took `;inv` down for 75% of blade holders in
  v96. `option()` now raises rather than shipping one, and every panel's
  options are checked against the documented limits rather than the ones I
  remember.
* **An action that points at nothing.** `console.py` shipped a table of
  actions whose handlers had been deleted; picking one answered "isn't wired
  up". Here the equivalent is `invoke="profile"` naming a prefix command that
  does not exist — the same bug wearing a hat — so every action is resolved
  against a really-loaded bot.
* **Inputs landing in the wrong parameter.** `;avatarupgrade` takes
  (avatar, levels); the panel collects (amount, text). Appending them in a
  fixed order hands it the level count as the card name, silently, with no
  error anywhere. `binds` maps names, and that mapping is checked against each
  command's real signature.
* **A Run button that does nothing.** The worst failure a panel can have,
  because it is indistinguishable from the bot being down.

Run:  python3 tools/sim_panels.py
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


import logging                                          # noqa: E402

logging.disable(logging.CRITICAL)

import discord                                          # noqa: E402
from discord.ext import commands                        # noqa: E402

import app as APP                                       # noqa: E402
from utils import ranked as RK                          # noqa: E402
from cogs.ui import panel_kit as K                      # noqa: E402
from cogs.ui import panels as PN                        # noqa: E402

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)


# A real bot with every extension loaded — the only way to know an action's
# `invoke` names a command that actually exists.
async def _build_bot():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    bot = commands.Bot(command_prefix=";", intents=intents)
    failed = []
    for ext in APP.COGS:
        try:
            await bot.load_extension(ext)
        except Exception as exc:                         # noqa: BLE001
            failed.append((ext, exc))
    return bot, failed


BOT, FAILED = loop.run_until_complete(_build_bot())

USER = types.SimpleNamespace(id=4242, roles=[], mention="<@4242>",
                             display_name="Blader")


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. the picker is nine lines, not twenty-eight ────────────────")

check("every extension loads", not FAILED, FAILED)

lines = 0
for cmd in BOT.tree.get_commands():
    subs = list(getattr(cmd, "commands", []) or [])
    lines += len(subs) if subs else 1
# 9 until v1.27, which added three: /update (one line replacing six admin
# operations) and /bugs + /suggest, both player-facing and reachable no other
# way. v1.28 added four more for the community layer — /server (the whole admin
# surface for nine systems, in one line), /poll, /giveaway and /level. v1.32
# added /commands — the full command list, DM'd, one more line for one more
# feature. Still one line per feature, which is the property v1.14 was
# defending.
check("the slash picker is 17 lines", lines == 17, lines)
check("...one per feature", sorted(c.name for c in BOT.tree.get_commands())
      == ["admin", "avatar", "bugs", "casino", "commands", "giveaway",
          "leaderboard", "level", "player", "poll", "rank", "server",
          "story", "suggest", "tournament", "trade", "update"],
      sorted(c.name for c in BOT.tree.get_commands()))
check("no command is a group any more — groups are what render flat",
      not any(getattr(c, "commands", None) for c in BOT.tree.get_commands()),
      [c.name for c in BOT.tree.get_commands()
       if getattr(c, "commands", None)])
# Measured on origin/main before this change and again after: 132 both times.
# Pinned to the number rather than a floor, because the claim being made is
# "nothing was lost", and a floor would pass while a command quietly vanished.
# v1.18 removed exactly one: `;verify`, with the gate it belonged to.
# v1.27 added two: `;notifications`, the prefix command the new /player row
# invokes, and `;reportchannel`, which sets where /bugs and /suggest land.
# v1.36 added one: `;chat`, the per-user opt-out for server banter. Prefix
# rather than slash on purpose — the slash picker is a curated 17 and a
# personal mute switch does not earn a place on it.
PREFIX_BEFORE = 134
check(f"the prefix surface is {PREFIX_BEFORE} commands",
      len(list(BOT.walk_commands())) == PREFIX_BEFORE,
      len(list(BOT.walk_commands())))
check("`;verify` is gone, not merely unreachable",
      BOT.get_command("verify") is None)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. the option contract, enforced in one place ────────────────")

def raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    except Exception:                                    # noqa: BLE001
        return False
    return False


check("an empty value raises rather than shipping a 400",
      raises(lambda: K.option("Label", "")))
check("...and so does whitespace", raises(lambda: K.option("Label", "   ")))
o = K.option("x" * 300, "v" * 300, "d" * 300)
check("a long label is clamped to 100", len(o.label) == 100, len(o.label))
check("a long value is clamped to 100", len(o.value) == 100, len(o.value))
check("a long description is clamped to 100",
      len(o.description) == 100, len(o.description))
check("an empty description becomes None, not an empty string",
      K.option("L", "v", "").description is None)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. every action resolves to something that exists ────────────")

ALL = []
for key, spec_cls in PN.SPECS.items():
    spec = spec_cls()
    for a in spec.actions("", USER):
        ALL.append((key, a))

check("the six panels declare actions at all", len(ALL) >= 25, len(ALL))
empty = [k for k, c in PN.SPECS.items() if not c().actions("", USER)]
check("...and no panel is empty — an empty select is a Discord 400",
      not empty, empty)

missing = [(p, a.key, a.invoke) for p, a in ALL
           if a.handler is None and BOT.get_command(a.invoke) is None]
check("every `invoke` names a prefix command the bot really has",
      not missing, missing)
neither = [(p, a.key) for p, a in ALL if a.handler is None and not a.invoke]
check("...and no action has neither a handler nor an invoke", not neither, neither)
both = [(p, a.key) for p, a in ALL if a.handler is not None and a.invoke]
check("...nor both, which would silently ignore one", not both, both)


# Inputs must land in the parameter they were meant for.
bad_binds = []
for panel_key, a in ALL:
    if a.handler is not None:
        continue
    cmd = BOT.get_command(a.invoke)
    params = set(cmd.clean_params)
    for param, source in a.binds.items():
        if param not in params:
            bad_binds.append((panel_key, a.key, a.invoke, param, sorted(params)))
    for param in a.kwargs:
        if param not in params:
            bad_binds.append((panel_key, a.key, a.invoke, param, sorted(params)))
check("every bound parameter exists on the command it is bound to",
      not bad_binds, bad_binds[:3])

bad_src = [(p, a.key, s) for p, a in ALL
           for s in a.binds.values()
           if s not in ("user", "amount", "text", "text2")]
check("...and every bind reads a real panel input", not bad_src, bad_src)

# An action that declares an input but binds it nowhere collects it and throws
# it away — the modal asks a question whose answer is discarded.
orphan_inputs = []
for panel_key, a in ALL:
    if a.handler is not None:
        continue
    for need in a.needs:
        if need in ("user", "amount", "text", "text2") \
                and need not in a.binds.values():
            orphan_inputs.append((panel_key, a.key, need))
check("every declared input is bound to a parameter, not collected and dropped",
      not orphan_inputs, orphan_inputs)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 4. Discord's limits, per panel ───────────────────────────────")

for key, spec_cls in PN.SPECS.items():
    spec = spec_cls()
    acts = spec.actions("", USER)
    check(f"/{key}: at most 25 options", len(acts) <= K.MAX_OPTIONS, len(acts))
    check(f"/{key}: every key is a legal, unique select value",
          all(1 <= len(a.key) <= 100 and a.key.isascii() for a in acts)
          and len({a.key for a in acts}) == len(acts))
    check(f"/{key}: every label fits",
          all(1 <= len(a.label) <= 100 for a in acts))
    check(f"/{key}: every description fits",
          all(len(("⚠️ " if a.confirm else "") + a.description) <= 100
              for a in acts),
          [a.key for a in acts
           if len(("⚠️ " if a.confirm else "") + a.description) > 100])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5. the view: rows, pickers and the modal ─────────────────────")


class FakeBot:
    def get_command(self, name):
        return BOT.get_command(name)

    def get_cog(self, name):
        return None


def panel_for(key):
    return K.PanelView(PN.SPECS[key](), FakeBot(), USER, None, None)


for key in PN.SPECS:
    p = panel_for(key)
    check(f"/{key}: opens with no category row — it doesn't need one",
          not p.has_categories)
    check(f"/{key}: row 0 is the action select",
          isinstance(p.children[0], K.ActionSelect))
    check(f"/{key}: within 5 rows",
          len({c.row for c in p.children}) <= K.MAX_ROWS,
          sorted({c.row for c in p.children}))
    check(f"/{key}: no row holds more than 5 components",
          all(sum(1 for c in p.children if c.row == r) <= 5 for r in range(5)))
    check(f"/{key}: Run starts disabled with nothing chosen",
          next(c for c in p.children if isinstance(c, K.RunButton)).disabled)
    e = p.embed()
    check(f"/{key}: every embed field has a non-empty name — a blank one 400s",
          all(f.name and f.value for f in e.fields))
    check(f"/{key}: the embed is inside the 6000-character budget", len(e) < 6000)

p = panel_for("player")
p.action_key = "profile"
p.build()
check("an action taking a player shows the user picker",
      any(isinstance(c, K.TargetSelect) for c in p.children))
p.action_key = "balance"
p.build()
check("...and one that doesn't, doesn't",
      not any(isinstance(c, K.TargetSelect) for c in p.children))

# Every modal, for every action, on every panel.
for key, spec_cls in PN.SPECS.items():
    p = panel_for(key)
    for a in spec_cls().actions("", USER):
        m = K.InputModal(p, a)
        check(f"/{key} {a.key}: modal holds at most 5 inputs",
              len(m.children) <= K.MODAL_INPUTS_MAX, len(m.children))
        check(f"/{key} {a.key}: modal title fits 45 chars",
              len(m.title) <= K.MODAL_TITLE_MAX, m.title)
        want = {n for n in a.needs if n in ("amount", "text", "text2")}
        got = set()
        if m.amount_field is not None:
            got.add("amount")
        if m.text_field is not None:
            got.add("text")
        if m.text2_field is not None:
            got.add("text2")
        check(f"/{key} {a.key}: asks for exactly {sorted(want)}", got == want, got)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6. the click sequence, through the real components ───────────")


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


class FakeInteraction:
    def __init__(self, user=USER):
        self.user = user
        self.response = FakeResponse()
        self.followup = types.SimpleNamespace(
            send=lambda **kw: asyncio.sleep(0))
        self.guild = None
        self.channel = None


def press(panel, cls, values=None, user=USER):
    comp = next(c for c in panel.children if isinstance(c, cls))
    if values is not None:
        comp._values = values
    i = FakeInteraction(user)
    loop.run_until_complete(comp.callback(i))
    return i


p = panel_for("story")
i = press(p, K.ActionSelect, ["map"])
check("picking an action redraws the panel in place", bool(i.response.edited))
check("...and Run becomes pressable",
      not next(c for c in p.children if isinstance(c, K.RunButton)).disabled)

ran = {"n": 0}


async def _spy(panel, interaction):
    ran["n"] += 1


p = panel_for("story")
p.spec.ACTIONS = tuple(
    K.PanelAction("probe", "Probe", "test", handler=_spy)
    for _ in range(1))
p.action_key = "probe"
p.build()
press(p, K.RunButton)
check("Run actually runs the action — a button that does nothing is the "
      "worst failure a panel has", ran["n"] == 1, ran["n"])

# An action needing typing must open a modal, and a modal must be the FIRST
# response to an interaction — it cannot be sent as a followup.
p = panel_for("story")
p.action_key = "jump"
p.build()
i = press(p, K.RunButton)
check("an action needing text opens a modal", isinstance(i.response.modal,
                                                         K.InputModal))
check("...as the first response, which Discord requires", i.response.sent == [])

# A destructive action asks twice.
p = panel_for("avatar")
p.action_key = "reset"
p.build()
i = press(p, K.RunButton)
check("a destructive action asks before doing it", p.pending_confirm)
btn = next(c for c in p.children if isinstance(c, K.RunButton))
check("...and the button turns red", btn.style == discord.ButtonStyle.danger)

# Somebody else's click.
p = panel_for("player")
i = FakeInteraction(types.SimpleNamespace(id=99, roles=[]))
check("another user cannot drive your panel",
      loop.run_until_complete(p.interaction_check(i)) is False)
check("...and is told why", any("Not your panel" in str(x)
                                for x in i.response.sent), i.response.sent)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6b. inputs land in the parameter they were meant for ─────────")
# These checks lived here and were driven by monkeypatching
# `commands.Context.from_interaction` with a stub that recorded kwargs. That
# stub is exactly why this suite passed 244 checks against a feature that had
# never once executed: it replaced the call that raised.
#
# The claim is still worth making — `;avatarupgrade` takes (avatar, levels)
# while the panel collects (amount, text), so a mis-mapped bind hands the level
# count over as the card name, silently. It is now made in
# `tools/sim_panel_invoke.py`, against a real interaction and the command's own
# callback, where "the command received it" can actually be observed.

# No check here: `tools/sim_panel_invoke.py` scans every suite for that stub,
# and a check whose own source contains the pattern it forbids would match
# itself — which is its own small lesson about tests that inspect source.


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 7. nothing was lost in the move ──────────────────────────────")
# Named one by one, because "we replaced four groups with four panels" is only
# true if every subcommand landed somewhere.

MOVED = {
    "/player profile": ("player", "profile"),
    "/player inventory": ("player", "inventory"),
    "/player balance": ("player", "balance"),
    "/player quests": ("player", "quests"),
    "/player claim": ("player", "claim"),
    "/player achievements": ("player", "achievements"),
    "/player mastery": ("player", "mastery"),
    "/rank": ("player", "rank"),
    "/leaderboard": ("leaderboard", "lb_rank"),
    "/casino balance": ("casino", "balance"),
    "/casino daily": ("casino", "daily"),
    "/casino leaderboard": ("casino", "leaderboard"),
    "/casino exchange": ("casino", "buy"),
    "/casino menu": ("casino", "menu"),
    "/casino play": ("casino", "menu"),
    "/casino games": ("casino", "menu"),
    "/avatar upgrade": ("avatar", "upgrade"),
    "/avatar reset": ("avatar", "reset"),
    "/avatar costs": ("avatar", "costs"),
    "/avatar skill": ("avatar", "skill"),
    "/avatar refill": ("avatar", "refill"),
    "/story play": ("story", "play"),
    "/story map": ("story", "map"),
    "/story info": ("story", "info"),
    "/story stats": ("story", "stats"),
}
keys = {p: {a.key for a in PN.SPECS[p]().actions("", USER)} for p in PN.SPECS}
lost = [old for old, (panel, key) in MOVED.items() if key not in keys[panel]]
check(f"all {len(MOVED)} old subcommands have a home", not lost, lost)

# `USER` has no guild, so this is the view from OUTSIDE the main server, where
# the community boards are deliberately not offered: the panel posts publicly,
# and a public "that board is main-server only" is worse than never showing the
# option. `tools/sim_community.py` asserts the inside view.
PUBLIC_BOARDS = {k for k in RK.CATEGORIES
                 if k not in getattr(RK, "MAIN_ONLY", ())}
check("every public leaderboard is its own option, not a typed category",
      len([k for k in keys["leaderboard"] if k.startswith("lb_")])
      == len(PUBLIC_BOARDS),
      sorted(k for k in keys["leaderboard"] if k.startswith("lb_")))
check("...built from RK.CATEGORIES, so a new board appears without editing "
      "this file", {f"lb_{k}" for k in PUBLIC_BOARDS} == keys["leaderboard"],
      keys["leaderboard"])
check("...and the main-server boards are the only ones held back",
      {f"lb_{k}" for k in RK.CATEGORIES} - keys["leaderboard"]
      == {f"lb_{k}" for k in RK.MAIN_ONLY},
      sorted({f"lb_{k}" for k in RK.CATEGORIES} - keys["leaderboard"]))
check("the boards left /player — a board is about everyone",
      not any(k.startswith("lb_") for k in keys["player"]),
      sorted(k for k in keys["player"] if k.startswith("lb_")))
check("level and money are boards now",
      {"lb_level", "lb_money"} <= keys["leaderboard"])
check("story keeps BOTH ways in — the picker and a direct jump",
      {"play", "jump"} <= keys["story"])

# The prefix commands every panel leans on must still be registered.
for name in ("profile", "inventory", "bal", "quests", "questclaim",
             "achievements", "mastery", "leaderboard", "rank", "trade",
             "casinobal", "casinodaily", "casinoleaderboard", "casinoexchange",
             "avatarupgrade", "avatarreset", "avatarcost", "avatarskill",
             "energyrefill", "story", "storymap", "storyinfo", "storystats"):
    check(f"`;{name}` still exists", BOT.get_command(name) is not None)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 8. the retired groups really are gone ────────────────────────")


def code(rel: str) -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), rel)
    return "\n".join(l for l in open(path, encoding="utf-8").read().splitlines()
                     if not l.lstrip().startswith("#"))


for rel, gone in (("cogs/economy/profile.py", "class PlayerCommands"),
                  ("cogs/avatar/avatar_upgrade.py", "class AvatarUpgradeCommands"),
                  ("cogs/story/story_cog.py", "class StoryCommands"),
                  ("cogs/ranked/ranked_cog.py", "class RankedCommands"),
                  ("cogs/casino/casino_hub.py", "app_commands.Group")):
    check(f"{rel.split('/')[-1]}: `{gone}` is gone", gone not in code(rel))

check("app.py loads the panels cog", '"cogs.ui.panels"' in code("app.py"))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 9. /trade has one owner and does not block the panels cog ────")
check("the generic panels layer no longer owns /trade",
      "trade" not in PN.SPECS and not hasattr(PN, "TradeSpec"))
check("PanelCommands does not register a second /trade",
      not hasattr(PN.PanelCommands, "trade"))
_trade_prefix = BOT.get_command("trade")
_trade_slash = BOT.tree.get_command("trade")
check("the reworked trade cog still registers ;trade",
      _trade_prefix is not None)
check("...and its hybrid command owns the single /trade surface",
      _trade_slash is not None)
check("/trade takes exactly one required player argument",
      _trade_slash is not None
      and len(_trade_slash.parameters) == 1
      and _trade_slash.parameters[0].name == "player"
      and _trade_slash.parameters[0].required,
      [] if _trade_slash is None else
      [(p.name, p.required) for p in _trade_slash.parameters])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 10. verification is gone from the surfaces, not just the rules ")

for rel in ("cogs/ranked/ranked_cog.py", "cogs/ui/panels.py",
            "cogs/admin/actions.py", "cogs/battle/battle.py"):
    src = code(rel)
    check(f"{rel.split('/')[-1]}: no call into the removed gate",
          not any(n in src for n in ("verify_required", "is_verified",
                                     "eligibility_error", "control_guild_id",
                                     "control_error", "K_VERIFIED")),
          [n for n in ("verify_required", "is_verified", "eligibility_error",
                       "control_guild_id", "control_error", "K_VERIFIED")
           if n in src])

check("the help index no longer offers `;verify`",
      ";verify" not in code("cogs/ui/help_cog.py"))

loop.run_until_complete(BOT.close())

print("\n" + "=" * 66)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 66)
sys.exit(1 if FAIL else 0)
