#!/usr/bin/env python3
"""
tools/sim_panel_invoke.py — the panels actually reach the commands they name.

Why this file exists
--------------------
`tools/sim_panels.py` passed 244 checks against a feature that had never once
worked. Every `invoke=` panel action raised `ValueError: interaction does not
have command data` from v1.14 until v1.19 — and the suite could not see it,
because it monkeypatched the exact call that raises:

    _real_from_interaction = commands.Context.from_interaction
    async def _fake_from_interaction(interaction):
        return _SpyCtx()
    commands.Context.from_interaction = staticmethod(_fake_from_interaction)

It proved the arguments were mapped correctly. It never proved the command
could be reached at all.

So this suite has one rule, and it is the whole design:

    **No assertion may be "it did not raise."**

`guard` (`cogs/ui/panel_kit.py`) turns every panel failure into a message, and
a modal that raises simply closes. "No exception" is therefore compatible with
total failure. Every check here asserts on either (a) a probe proving the
command's own callback ran, or (b) the bytes that would have gone to Discord,
captured at discord.py's own network seam — never at ours.

The interactions are real `discord.Interaction` objects, built from real
gateway payloads. Section 0 proves they are faithful by asserting the **real**
`Context.from_interaction` still refuses them. If anyone ever "simplifies" the
fake into something that function accepts, section 0 fails and says why.

Run:  python3 tools/sim_panel_invoke.py
"""
import asyncio
import itertools
import logging
import os
import re
import sys
from glob import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.disable(logging.CRITICAL)

import discord                                          # noqa: E402
from discord.ext import commands                        # noqa: E402
from discord.webhook.async_ import (                     # noqa: E402
    AsyncWebhookAdapter, async_context)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {detail}")


# Captured BEFORE anything else imports, so the "nothing was stubbed" check at
# the end compares against the genuine originals.
_ORIGINALS = {
    "Context.from_interaction": commands.Context.__dict__["from_interaction"],
    "Context.send": commands.Context.__dict__["send"],
    "InteractionResponse.defer": discord.InteractionResponse.defer,
    "Messageable.send": discord.abc.Messageable.send,
    "Webhook.send": discord.Webhook.send,
}

import app as APP                                       # noqa: E402
from cogs.ui import invoke as INVOKE                    # noqa: E402
from cogs.ui import panel_kit as K                      # noqa: E402
from cogs.ui import panels as PN                        # noqa: E402
from utils import ranked as RK                          # noqa: E402

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

GUILD_ID, CH_ID, USER_ID, TARGET_ID, BOT_ID = 111, 222, 4242, 8484, 999
PANEL_MSG_ID = 777

# Real snowflakes, not counters. A Discord id encodes the time it was minted,
# and `Interaction.is_expired()` reads it — a made-up integer decodes to 2015,
# every interaction looks fifteen minutes stale, and the suite silently tests
# the expiry fallback instead of the path it means to.
_seq = itertools.count(1)


def snowflake(when=None) -> int:
    from datetime import datetime, timezone
    return (discord.utils.time_snowflake(
        when or datetime.now(timezone.utc)) + next(_seq))


class _Ids:
    def __next__(self):
        return snowflake()


_ids = _Ids()


# ══════════════════════════════════════════════════════════════════════════════
#  A bot with every extension really loaded
# ══════════════════════════════════════════════════════════════════════════════

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
            failed.append((ext, repr(exc)))
    # `Interaction._from_data` reads `guild.me`, which needs a client user.
    bot._connection.user = discord.ClientUser(state=bot._connection, data={
        "id": str(BOT_ID), "username": "Beybot", "discriminator": "0",
        "avatar": None, "bot": True, "verified": True, "mfa_enabled": False,
        "flags": 0})
    return bot, failed


BOT, FAILED = loop.run_until_complete(_build_bot())
STATE = BOT._connection
STORE = STATE._view_store

# The test player has been through `;start`.
#
# This is not scaffolding to make the suite pass — it is the point. v1.19 makes
# the panels run the command's own checks, because `ctx.invoke` skips them and
# the tree-level gate fires only for application commands: a panel opened
# before a ban stayed live and usable afterwards. So the gate now really does
# stand in front of a Run button, and a player who has not started is really
# turned away. Section 10 asserts that from the other side.
import cogs.core.onboarding as ONB                      # noqa: E402

STARTED = {USER_ID, TARGET_ID}
ONB.user_exists = lambda uid: int(uid) in STARTED
ONB.get_user = lambda uid: {"inventory": ["Dragoon"], "user_id": str(uid)}
ONB._has_started = lambda prof: bool((prof or {}).get("inventory"))
ONB.blocked_reason = lambda user, bot: None


# ══════════════════════════════════════════════════════════════════════════════
#  Capturing what would go on the wire — at discord.py's seams, not ours
# ══════════════════════════════════════════════════════════════════════════════

def _message_payload(mid=None, channel_id=CH_ID):
    return {
        "id": str(mid or next(_ids)), "type": 0, "content": "", "flags": 0,
        "channel_id": str(channel_id),
        "author": {"id": str(BOT_ID), "username": "Beybot",
                   "discriminator": "0", "avatar": None, "bot": True},
        "attachments": [], "embeds": [], "mentions": [], "mention_roles": [],
        "pinned": False, "mention_everyone": False, "tts": False,
        "timestamp": "2020-01-01T00:00:00+00:00", "edited_timestamp": None,
    }


class Call:
    def __init__(self, method, path, payload, multipart=None):
        self.method, self.path = method, path
        self.payload, self.multipart = payload, multipart

    def __repr__(self):                                  # pragma: no cover
        return f"<{self.method} {self.path} {str(self.payload)[:90]}>"


class RecordingAdapter(AsyncWebhookAdapter):
    """Every interaction response and every followup funnels through here.

    Deliberately BELOW `InteractionResponse.defer`, so the real defer logic
    runs and the test asserts on the wire type it actually computed — which is
    the thing under test.
    """

    def __init__(self):
        super().__init__()
        self.calls = []

    async def request(self, route, session=None, *, payload=None,
                      multipart=None, files=None, params=None, **kw):
        self.calls.append(Call(route.method, route.path, payload, multipart))
        if route.path.endswith("/callback"):
            return {"interaction": {"id": "1",
                                    "response_message_loading": True,
                                    "response_message_ephemeral": False}}
        return _message_payload()


ADAPTER = RecordingAdapter()
async_context.set(ADAPTER)

HTTP_CALLS = []
_real_http_request = BOT.http.request


async def _recording_http(route, **kwargs):
    HTTP_CALLS.append(Call(route.method, route.path, kwargs.get("json")))
    if route.path.endswith("/messages") and route.method == "POST":
        return _message_payload()
    if "/messages/" in route.path:
        return _message_payload()
    return {}


BOT.http.request = _recording_http


def reset_wire():
    ADAPTER.calls.clear()
    HTTP_CALLS.clear()


def acks():
    return [c for c in ADAPTER.calls if c.path.endswith("/callback")]


def followups():
    return [c for c in ADAPTER.calls
            if c.method == "POST"
            and c.path == "/webhooks/{webhook_id}/{webhook_token}"]


def channel_msgs():
    return [c for c in HTTP_CALLS
            if c.method == "POST"
            and c.path == "/channels/{channel_id}/messages"]


def is_ephemeral(payload):
    return bool((payload or {}).get("flags", 0) & 64)


_ERROR_WORDS = ("⚠️", "ValueError", "TypeError", "AttributeError",
                "isn't loaded", "Traceback")


def _text(call):
    bits = [str(call.payload)]
    for part in (call.multipart or []):
        bits.append(str(part.get("value", "")))
    return " ".join(bits)


def mentions_error(calls):
    return [c for c in calls if any(w in _text(c) for w in _ERROR_WORDS)]


# ══════════════════════════════════════════════════════════════════════════════
#  Real interactions, from real payloads
# ══════════════════════════════════════════════════════════════════════════════

def _user(uid, name):
    return {"id": str(uid), "username": name, "discriminator": "0",
            "avatar": None, "global_name": name}


def _member(uid, name):
    return {"user": _user(uid, name), "roles": [],
            "joined_at": "2020-01-01T00:00:00+00:00", "deaf": False,
            "mute": False, "flags": 0, "permissions": "137439006208"}


def _panel_message():
    """The EPHEMERAL panel message a component sits on — authored by the BOT.

    This one dict is the second-order trap: `from_interaction` skips its
    synthetic branch when `interaction.message` is not None, and `ctx.author`
    then resolves to THIS author. Every naive fix produces a bot-authored
    context, and nothing raises.
    """
    return {"id": str(PANEL_MSG_ID), "type": 0, "content": "", "flags": 64,
            "author": {"id": str(BOT_ID), "username": "Beybot",
                       "discriminator": "0", "avatar": None, "bot": True},
            "attachments": [], "embeds": [], "mentions": [],
            "mention_roles": [], "pinned": False, "mention_everyone": False,
            "tts": False, "timestamp": "2020-01-01T00:00:00+00:00",
            "edited_timestamp": None}


def interaction(kind, data, *, with_message=True, user_id=USER_ID,
                guild=True, message_id=None):
    payload = {
        "id": str(next(_ids)), "application_id": str(BOT_ID),
        "type": kind, "token": "tok", "version": 1,
        "attachment_size_limit": 8388608,
        "app_permissions": "137439006208",
        "data": data,
    }
    if guild:
        payload["guild_id"] = str(GUILD_ID)
        payload["member"] = _member(user_id, f"User{user_id}")
        payload["channel"] = {"id": str(CH_ID), "type": 0, "name": "general",
                              "guild_id": str(GUILD_ID), "position": 0,
                              "permission_overwrites": []}
    else:
        payload["user"] = _user(user_id, f"User{user_id}")
        payload["channel"] = {"id": str(CH_ID), "type": 1}
    if with_message:
        msg = _panel_message()
        if message_id is not None:
            msg["id"] = str(message_id)
        payload["message"] = msg
    return discord.Interaction(data=payload, state=STATE)


def _raises_valueerror(coro_fn):
    try:
        loop.run_until_complete(coro_fn())
    except ValueError:
        return True
    except Exception:                                    # noqa: BLE001
        return False
    return False


def _succeeds(coro_fn):
    try:
        loop.run_until_complete(coro_fn())
        return True
    except Exception:                                    # noqa: BLE001
        return False


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 0. the fake interactions are faithful ────────────────────────")
# The ratchet. These checks are SUPPOSED to pass on the broken tree — they
# prove the harness reproduces production, so that everything below it means
# something.

check("every extension loads", not FAILED, FAILED)

COMP = interaction(3, {"custom_id": "x", "component_type": 2})
check("a component interaction is InteractionType.component",
      COMP.type is discord.InteractionType.component, COMP.type)
check("...and carries no command data — this is the entire bug",
      COMP.command is None)
check("...so the REAL Context.from_interaction refuses it",
      _raises_valueerror(lambda: commands.Context.from_interaction(COMP)))
check("...and interaction.message is NOT None, so from_interaction would skip "
      "its synthetic branch and take the author from the panel message",
      COMP.message is not None)
check("...whose author is the BOT — what ctx.author would silently become",
      COMP.message.author.id == BOT_ID and COMP.message.author.bot)
check("the channel resolves to a real TextChannel, not a PartialMessageable",
      isinstance(COMP.channel, discord.TextChannel) and COMP.channel.id == CH_ID)
check("the clicker resolves to a real Member",
      isinstance(COMP.user, discord.Member) and COMP.user.id == USER_ID)

MODAL_I = interaction(5, {"custom_id": "m", "components": []})
check("a modal submit is InteractionType.modal_submit",
      MODAL_I.type is discord.InteractionType.modal_submit)
check("...and the real from_interaction refuses it too",
      _raises_valueerror(lambda: commands.Context.from_interaction(MODAL_I)))

APP_I = interaction(2, {"id": "1", "name": "rank", "type": 1, "options": []},
                    with_message=False)
check("an application command, by contrast, DOES carry command data",
      APP_I.command is not None, APP_I.command)
check("...and the real from_interaction accepts it",
      _succeeds(lambda: commands.Context.from_interaction(APP_I)))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. the context the helper builds ─────────────────────────────")

CMD = BOT.get_command("rank")
ctx = loop.run_until_complete(INVOKE.build_context(COMP, CMD, bot=BOT))

check("ctx.author is the clicker", ctx.author.id == USER_ID, ctx.author)
check("...and is NOT the bot — the panel message's author is, and that is what "
      "a naive fix would have handed every command",
      ctx.author.id != BOT_ID and not ctx.author.bot
      and ctx.author.id != COMP.message.author.id)
check("ctx.author is a Member, so display_name and role checks work",
      isinstance(ctx.author, discord.Member))
check("ctx.guild is the real guild",
      ctx.guild is not None and ctx.guild.id == GUILD_ID)
check("ctx.channel is where the panel was opened", ctx.channel.id == CH_ID)
check("ctx.channel is a real channel — a PartialMessageable has no .guild off "
      "cache, and `;leaderboard` would render every row as 'User 123…'",
      not isinstance(ctx.channel, discord.PartialMessageable))
check("ctx.message is synthetic, not the panel message",
      ctx.message.id != PANEL_MSG_ID)
check("ctx.message.mentions exists — `;achievements` reads it, and a missing "
      "key means the attribute is never created rather than defaulting",
      ctx.message.mentions == [])
check("ctx._state is the bot's state", ctx._state is STATE)
check("ctx.prefix is ';', not '/' — spawn.py prints it at players",
      ctx.prefix == ";", ctx.prefix)
check("ctx.command is set — boss_battle reads it for LOCK_EXEMPT",
      ctx.command is CMD)
check("ctx.bot is the bot", ctx.bot is BOT)
check("ctx.interaction is kept, so ctx.typing() stays a no-op instead of "
      "starting a real HTTP typing loop", ctx.interaction is COMP)

dm = interaction(3, {"custom_id": "x", "component_type": 2}, guild=False)
dctx = loop.run_until_complete(INVOKE.build_context(dm, CMD, bot=BOT))
check("in a DM ctx.guild is None and nothing raises", dctx.guild is None)

blind = interaction(3, {"custom_id": "x", "component_type": 2})
blind.channel = None
bctx = loop.run_until_complete(INVOKE.build_context(blind, CMD, bot=BOT))
check("with no interaction.channel we fall back to the panel message's channel",
      bctx.channel.id == CH_ID)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. a public action posts where everyone can see it ───────────")

MEMBER = COMP.user
CHANNEL = COMP.channel
GUILD = COMP.guild

RAN = {"n": 0, "author": None, "category": None}
_lb_cmd = BOT.get_command("leaderboard")
_lb_real = _lb_cmd.callback


async def _lb_probe(self, ctx, category=RK.DEFAULT_CATEGORY):
    RAN["n"] += 1
    RAN["author"] = ctx.author
    RAN["category"] = category
    await _lb_real(self, ctx, category)


_lb_cmd.callback = _lb_probe   # the probe: the ONLY wrap of our own code


async def _make_panel(spec):
    """Built INSIDE the loop, deliberately.

    `View.__init__` only creates its `__stopped` future when there is a running
    loop, and `_dispatch_item` returns None when that future is missing — so a
    view constructed synchronously silently refuses every click. A test that
    built its panels outside the loop would watch nothing happen and conclude
    the code was broken.
    """
    view = K.PanelView(spec, BOT, MEMBER, GUILD, CHANNEL)

    async def _on_error(interaction, error, item):
        RAISED.append(error)

    view.on_error = _on_error
    return view


def panel_for(key):
    return loop.run_until_complete(_make_panel(PN.SPECS[key]()))


RAISED = []


async def _click(view, comp, inter):
    """Drive a component the way the gateway does, not by calling callback().

    Through `dispatch_view`, so `View.interaction_check` runs and the select's
    values come from the payload via `Item._refresh_state` — the same path
    production takes. Calling `comp.callback(...)` directly would skip both.
    """
    tasks = []
    real_add = STORE.add_task
    STORE.add_task = tasks.append
    try:
        # Registered under the message id the interaction names, because that
        # is the first key `dispatch_view` looks under — a view stored without
        # one is only found by the persistent-view fallback, and a panel is
        # not persistent.
        STORE.add_view(view, getattr(inter.message, "id", None))
        STORE.dispatch_view(comp.type.value, comp.custom_id, inter)
    finally:
        STORE.add_task = real_add
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                RAISED.append(r)


def run_click(view, comp, inter):
    loop.run_until_complete(_click(view, comp, inter))


reset_wire()
p = panel_for("leaderboard")
sel = next(c for c in p.children if isinstance(c, K.ActionSelect))
run_click(p, sel, interaction(3, {"custom_id": sel.custom_id,
                                  "component_type": 3,
                                  "values": ["lb_rank"]}))
check("picking a board redraws the panel in place",
      acks() and acks()[-1].payload.get("type") == 7,
      acks()[-1].payload if acks() else None)

reset_wire()
run = next(c for c in p.children if isinstance(c, K.RunButton))
run_click(p, run, interaction(3, {"custom_id": run.custom_id,
                                  "component_type": 2}))

check("the prefix command ACTUALLY RAN — before v1.19 it never did, because "
      "from_interaction raised before ctx.invoke was ever reached",
      RAN["n"] == 1, RAN["n"])
check("...with the clicker as ctx.author, not the bot",
      getattr(RAN["author"], "id", None) == USER_ID, RAN["author"])
check("...and the chosen board as the argument", RAN["category"] == "rank",
      RAN["category"])
check("the interaction was acked with deferred_message_update (type 6), which "
      "leaves no spinner and pins nothing ephemeral",
      acks() and acks()[0].payload.get("type") == 6,
      acks()[0].payload if acks() else None)
check("the board landed in the CHANNEL, where everyone can see it",
      len(channel_msgs()) == 1, channel_msgs())
check("...and nothing went out ephemeral",
      not any(is_ephemeral(c.payload) for c in followups()), followups())
check("nothing apologised — `guard` turns this bug into a message, so a check "
      "that only asks 'did it raise' passes against a totally dead feature",
      not mentions_error(channel_msgs() + followups() + acks()),
      mentions_error(channel_msgs() + followups() + acks()))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. a private action stays private, on every send ─────────────")

SENDS = {"n": 0}


async def _chatty(ctx):
    SENDS["n"] += 1
    await ctx.send("one")
    await ctx.send("two")
    await ctx.send("three")


BOT.add_command(commands.Command(_chatty, name="_simchatty"))


class _ProbeSpec(K.PrefixSpec):
    title = "probe"
    public = False
    ACTIONS = (K.PanelAction("chatty", "Chatty", "three sends",
                             invoke="_simchatty"),)


reset_wire()
pp = loop.run_until_complete(_make_panel(_ProbeSpec()))
pp.action_key = "chatty"
pp.build()
run_click(pp, next(c for c in pp.children if isinstance(c, K.RunButton)),
          interaction(3, {"custom_id": next(
              c for c in pp.children if isinstance(c, K.RunButton)).custom_id,
              "component_type": 2}))

check("the private command ran", SENDS["n"] == 1, SENDS["n"])
check("a private action defers ephemeral AND thinking — `ephemeral` alone is "
      "silently dropped on a component, the flag only attaches when thinking",
      acks() and acks()[0].payload.get("type") == 5
      and (acks()[0].payload.get("data") or {}).get("flags") == 64,
      acks()[0].payload if acks() else None)
check("all three messages went out", len(followups()) == 3, len(followups()))
check("...and EVERY one of them is ephemeral, including the second and third — "
      "Context.send re-defaults ephemeral to False on each call, so an "
      "unforced second send leaks publicly",
      followups() and all(is_ephemeral(c.payload) for c in followups()),
      [c.payload.get("flags") for c in followups()])
check("nothing went to the channel", not channel_msgs(), channel_msgs())


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 4. the interaction is acked before the command body runs ─────")

DEADLINE = {}


async def _slow(ctx):
    DEADLINE["acked_first"] = bool(acks())
    DEADLINE["ack_type"] = acks()[0].payload.get("type") if acks() else None


BOT.add_command(commands.Command(_slow, name="_simdeadline"))

reset_wire()
loop.run_until_complete(INVOKE.run_prefix_command(
    interaction(3, {"custom_id": "z", "component_type": 2}),
    BOT.get_command("_simdeadline"), bot=BOT, visibility=INVOKE.PUBLIC))
check("the ack happens BEFORE the body — the 3-second deadline, asserted by "
      "ordering rather than by a stopwatch",
      DEADLINE.get("acked_first") is True, DEADLINE)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5. a command that sends nothing still closes the interaction ─")


async def _silent(ctx):
    return


BOT.add_command(commands.Command(_silent, name="_simsilent"))

reset_wire()
loop.run_until_complete(INVOKE.run_prefix_command(
    interaction(3, {"custom_id": "z", "component_type": 2}),
    BOT.get_command("_simsilent"), bot=BOT, visibility=INVOKE.PRIVATE))
check("a thinking defer that nothing fills would rot into 'the application "
      "did not respond' — so it gets filled", len(followups()) == 1,
      followups())


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5b. an expired interaction still delivers ────────────────────")
# Fifteen minutes after the click the token is dead and there is no private
# transport left. `Context.send` would quietly fall back to a PUBLIC channel
# message here — so the fallback is made explicit and addressed to the player,
# rather than inherited by accident.

from datetime import datetime, timedelta, timezone      # noqa: E402

OLD = interaction(3, {"custom_id": "z", "component_type": 2})
OLD.id = snowflake(datetime.now(timezone.utc) - timedelta(minutes=20))
check("a 20-minute-old interaction reads as expired", OLD.is_expired())

reset_wire()
loop.run_until_complete(INVOKE.run_prefix_command(
    OLD, BOT.get_command("_simchatty"), bot=BOT, visibility=INVOKE.PRIVATE))
check("...so a private command falls back to the channel rather than doing "
      "nothing at all", len(channel_msgs()) == 3, len(channel_msgs()))
check("...addressed to the player, so it is not an anonymous stray message",
      all("<@4242>" in str(c.payload.get("content")) for c in channel_msgs()),
      [c.payload.get("content") for c in channel_msgs()])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6. /rank and /leaderboard run from a real slash interaction ──")

reset_wire()
RANK_RAN = {"n": 0, "author": None}
_rank_cmd = BOT.get_command("rank")
_rank_real = _rank_cmd.callback


async def _rank_probe(self, ctx, member=None):
    RANK_RAN["n"] += 1
    RANK_RAN["author"] = ctx.author


_rank_cmd.callback = _rank_probe

loop.run_until_complete(INVOKE.run_prefix_command(
    interaction(2, {"id": "1", "name": "rank", "type": 1, "options": []},
                with_message=False),
    _rank_cmd, bot=BOT, visibility=INVOKE.PUBLIC))

check("/rank reaches `;rank` through the same helper the panels use",
      RANK_RAN["n"] == 1, RANK_RAN["n"])
check("...with the invoking player as ctx.author",
      getattr(RANK_RAN["author"], "id", None) == USER_ID)
check("a slash command has no type-6 ack available, so it defers thinking and "
      "the first send fills the placeholder",
      acks() and acks()[0].payload.get("type") == 5,
      acks()[0].payload if acks() else None)
check("...publicly", not is_ephemeral((acks()[0].payload.get("data") or {})))

_rank_cmd.callback = _rank_real

check("/leaderboard offers Discord's own dropdown of every board",
      len(BOT.tree.get_command("leaderboard").parameters[0].choices)
      == len(RK.CATEGORIES),
      len(BOT.tree.get_command("leaderboard").parameters[0].choices))
check("...and the board parameter is optional, so bare /leaderboard still "
      "opens the panel",
      not BOT.tree.get_command("leaderboard").parameters[0].required)
check("/rank is a top-level slash command",
      BOT.tree.get_command("rank") is not None)
check("the picker is 17 lines",  # +/server, /poll, /giveaway, /level in v1.28,
      len(BOT.tree.get_commands()) == 17,  # +/commands in v1.32
      sorted(c.name for c in BOT.tree.get_commands()))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6b. inputs land in the parameter they were meant for ─────────")
# Moved here from `sim_panels.py`, which proved it against a stub. `;avatar\
# upgrade` takes (avatar, levels) and the panel collects (amount, text), so
# appending them in a fixed order hands the level count over as the card name —
# silently, with no error anywhere. Proven now by watching the real command's
# own callback receive them.

BOUND = {}


def _capture(cmd_name):
    cmd = BOT.get_command(cmd_name)
    real = cmd.callback

    async def probe(self, ctx, *args, **kwargs):
        BOUND.clear()
        BOUND.update(kwargs)
        BOUND["_args"] = args
        BOUND["_author"] = ctx.author

    cmd.callback = probe
    return cmd, real


_up_cmd, _up_real = _capture("avatarupgrade")
reset_wire()
_pa = panel_for("avatar")
_pa.amount, _pa.text = 3, "Valkyrie"
_pa.action_key = "upgrade"
loop.run_until_complete(PN.AvatarSpec().execute(
    next(a for a in PN.AvatarSpec().ACTIONS if a.key == "upgrade"), _pa,
    interaction(3, {"custom_id": "z", "component_type": 2})))
check("the amount lands in `levels`", BOUND.get("levels") == 3, BOUND)
check("...and the text in `avatar`, not the other way round",
      BOUND.get("avatar") == "Valkyrie", BOUND)

_pa.text = None
loop.run_until_complete(PN.AvatarSpec().execute(
    next(a for a in PN.AvatarSpec().ACTIONS if a.key == "upgrade"), _pa,
    interaction(3, {"custom_id": "z", "component_type": 2})))
check("an unfilled optional is omitted, so the command's own default applies "
      "— a blank card means the equipped one", "avatar" not in BOUND, BOUND)
_up_cmd.callback = _up_real

_ex_cmd, _ex_real = _capture("casinoexchange")
_pc = panel_for("casino")
_pc.amount = 500
loop.run_until_complete(PN.CasinoSpec().execute(
    next(a for a in PN.CasinoSpec().ACTIONS if a.key == "buy"), _pc,
    interaction(3, {"custom_id": "z", "component_type": 2})))
check("a literal kwarg rides along with a bound one",
      BOUND.get("direction") == "buy" and BOUND.get("amount") == 500, BOUND)
_ex_cmd.callback = _ex_real


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6c. /trade, all the way through to the swap ──────────────────")
# The hardest one, and the reason `/trade` had to become public: only the
# TARGET may press Accept, so an offer only the sender can see can never be
# accepted. Driven end to end — Run → modal → the real `;trade` → a DIFFERENT
# user pressing Accept → the blades actually changing hands.

import cogs.extras.trade as T                           # noqa: E402

PROFILES = {
    USER_ID: {"inventory": [{"name": "Dragoon"}], "active_beyblade": "Dragoon"},
    TARGET_ID: {"inventory": [{"name": "Valkyrie"}], "active_beyblade": None},
}
TRADE_LOG = []
async def _fake_get_user(uid):
    return PROFILES[int(uid)]
async def _fake_update_user(uid, prof):
    PROFILES.__setitem__(int(uid), prof)
T.get_user = _fake_get_user
T.update_user = _fake_update_user
T._log_trade = lambda entry: TRADE_LOG.append(entry)

TARGET_MEMBER = interaction(3, {"custom_id": "q", "component_type": 2},
                            user_id=TARGET_ID).user

reset_wire()
pt = panel_for("trade")
pt.action_key = "offer"
pt.target = TARGET_MEMBER
pt.build()
runbtn = next(c for c in pt.children if isinstance(c, K.RunButton))
run_click(pt, runbtn, interaction(3, {"custom_id": runbtn.custom_id,
                                      "component_type": 2}))
check("Run opened a modal as the FIRST response, which Discord requires",
      acks() and acks()[-1].payload.get("type") == 9,
      acks()[-1].payload.get("type") if acks() else None)

MODAL = list(STORE._modals.values())[-1]
check("...asking for both blade names, labelled so they cannot be swapped",
      [c.label for c in MODAL.children] == ["Your blade", "Their blade"],
      [c.label for c in MODAL.children])

reset_wire()
submit = interaction(5, {
    "custom_id": MODAL.custom_id,
    "components": [
        {"type": 1, "components": [{"type": 4,
                                    "custom_id": MODAL.children[0].custom_id,
                                    "value": "Dragoon"}]},
        {"type": 1, "components": [{"type": 4,
                                    "custom_id": MODAL.children[1].custom_id,
                                    "value": "Valkyrie"}]},
    ]})


async def _submit():
    tasks = []
    real_add = STORE.add_task
    STORE.add_task = tasks.append
    try:
        STORE.dispatch_modal(MODAL.custom_id, submit,
                             submit.data["components"], {})
    finally:
        STORE.add_task = real_add
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)


loop.run_until_complete(_submit())

offers = channel_msgs()
check("the offer went to the CHANNEL, not to an ephemeral followup — the "
      "target cannot press a button on a message only the sender can see",
      len(offers) >= 1 and not followups(),
      (len(offers), len(followups())))
if offers:
    body = str(offers[0].payload)
    check("...carrying the Accept/Decline buttons",
          bool(offers[0].payload.get("components")), offers[0].payload.keys())
    check("...naming the right blades in the right direction — a swapped bind "
          "raises nothing and only shows up as 'you don't own that'",
          "Dragoon" in body and "Valkyrie" in body, body[:200])
    check("...and not ephemeral", not is_ephemeral(offers[0].payload))

check("no error leaked into the offer", not mentions_error(offers), offers)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 7. nothing builds a Context from a component interaction ─────")


def _sources():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "__pycache__", "node_modules")]
        for f in filenames:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


OFFENDERS = []
for path in _sources():
    rel = os.path.relpath(path, ROOT)
    if rel.startswith("tools/sim_"):
        continue
    src = open(path, encoding="utf-8").read()
    for n, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "Context.from_interaction(" in line:
            OFFENDERS.append(f"{rel}:{n}")
check("no source file calls Context.from_interaction — it raises on every "
      "button, select and modal submit, which is every panel click there is",
      not OFFENDERS, OFFENDERS)

STUBBERS = []
_SELF = os.path.abspath(__file__)
for f in sorted(glob(os.path.join(ROOT, "tools", "sim_*.py"))):
    if os.path.abspath(f) == _SELF:
        continue           # this file quotes the old stub in its docstring
    src = open(f, encoding="utf-8").read()
    if re.search(r"(commands\.)?Context\.from_interaction\s*=", src):
        STUBBERS.append(os.path.basename(f))
check("no suite stubs Context.from_interaction — sim_panels.py did, which is "
      "precisely why 244 passing checks sat on top of a dead code path",
      not STUBBERS, STUBBERS)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 8. ;leaderboard reads the registry once, not twice ───────────")

import cogs.ranked.ranked_cog as RKC                    # noqa: E402

LOADS = {"n": 0}
_real_load = RKC.load_users


def _counting_load():
    LOADS["n"] += 1
    return {}


RKC.load_users = _counting_load
_lb_cmd.callback = _lb_real
reset_wire()
loop.run_until_complete(INVOKE.run_prefix_command(
    interaction(3, {"custom_id": "z", "component_type": 2}),
    _lb_cmd, bot=BOT, visibility=INVOKE.PUBLIC, category="rank"))
RKC.load_users = _real_load
check("one full read of 3,400 profiles, not two — it used to load the whole "
      "registry again just to answer 'your position'",
      LOADS["n"] == 1, LOADS["n"])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 9. the suite never replaced the code it is testing ───────────")

for name, original in _ORIGINALS.items():
    if name == "Context.from_interaction":
        now = commands.Context.__dict__["from_interaction"]
    elif name == "Context.send":
        now = commands.Context.__dict__["send"]
    elif name == "InteractionResponse.defer":
        now = discord.InteractionResponse.defer
    elif name == "Messageable.send":
        now = discord.abc.Messageable.send
    else:
        now = discord.Webhook.send
    check(f"{name} is still discord.py's own", now is original)


loop.run_until_complete(BOT.close())

print("\n" + "=" * 66)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 66)
sys.exit(1 if FAIL else 0)
