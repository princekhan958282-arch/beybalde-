#!/usr/bin/env python3
"""
tools/sim_servers.py — verify ;servers reconciles the gateway cache against the API.

The bug this covers: the Developer Portal said 21 servers and `;servers` said 10.
The command was fine — `bot.guilds` really did hold 10, because the gateway drops
part-way through the GUILD_CREATE stream and nothing reports it. So the command
now asks the REST API as well and says which of the two is wrong.

Driven through a fake bot rather than a live gateway, so the reconciliation is
tested without a token.

Run:  python3 tools/sim_servers.py
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


import discord                                        # noqa: E402
from discord.ext import commands                      # noqa: E402
import cogs.admin.admin as ADMIN                      # noqa: E402


class FakeGuild:
    def __init__(self, gid, name, members=100):
        self.id = gid
        self.name = name
        self.member_count = members


class FakeIntents:
    def __init__(self, guilds=True, members=True):
        self.guilds = guilds
        self.members = members


class FakeBot:
    """Only the surface ;servers touches."""

    def __init__(self, cached, rest, rest_error=None, shard_count=None,
                 intents=None):
        self.guilds = [FakeGuild(g, n) for g, n in cached.items()]
        self._rest = rest
        self._rest_error = rest_error
        self.shard_count = shard_count
        self.intents = intents or FakeIntents()

    def fetch_guilds(self, limit=200):
        rest = self._rest
        err = self._rest_error

        class _Iter:
            def __aiter__(self):
                return self

            def __init__(self):
                self._items = iter(list(rest.items()))

            async def __anext__(self):
                if err:
                    raise err
                try:
                    gid, name = next(self._items)
                except StopIteration:
                    raise StopAsyncIteration
                return FakeGuild(gid, name)
        return _Iter()


class FakeCtx:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None):
        self.sent.append(embed if embed is not None else content)
        return None


def run(cached, rest, **kw):
    bot = FakeBot(cached, rest, **kw)
    cog = ADMIN.AdminCog(bot)
    ctx = FakeCtx()
    # .callback bypasses the is_master check and the cog_check, which is the
    # point — this tests the reconciliation, not the permission gate.
    asyncio.get_event_loop().run_until_complete(
        ADMIN.AdminCog.servers.callback(cog, ctx))
    return ctx.sent


def text_of(sent):
    out = []
    for item in sent:
        if isinstance(item, discord.Embed):
            out.append(item.title or "")
            out.append(item.description or "")
            for f in item.fields:
                out.append(f"{f.name} {f.value}")
            if item.footer and item.footer.text:
                out.append(item.footer.text)
        else:
            out.append(str(item))
    return "\n".join(out)


asyncio.set_event_loop(asyncio.new_event_loop())

TEN = {1000 + i: f"Server {i}" for i in range(10)}
TWENTY_ONE = {1000 + i: f"Server {i}" for i in range(21)}

print("\n── 1. the reported bug: portal 21, cache 10 ─────────────────────")
sent = run(TEN, TWENTY_ONE)
body = text_of(sent)
check("the headline count uses the API, not the cache", "(21)" in body, body[:200])
check("it names the cache count too", "cache: **10**" in body, body[-600:])
check("it flags the 11 missing servers", "**11** server(s)" in body)
check("it says which direction the fault is",
      "never arrived over the gateway" in body)
check("it rules out the wrong diagnosis explicitly",
      "not a missing intent" in body)
check("it tells you what to do", "restart" in body.lower())
check("every missing server is listed by name",
      all(f"Server {i}" in body for i in range(10, 21)),
      [i for i in range(10, 21) if f"Server {i}" not in body])
check("missing servers are marked, not shown as normal rows",
      body.count("not in gateway cache") == 11,
      body.count("not in gateway cache"))
check("the embed turns orange to signal drift",
      any(e.colour and e.colour.value == 0xE67E22
          for e in sent if isinstance(e, discord.Embed)))

print("\n── 2. the healthy case ──────────────────────────────────────────")
sent = run(TWENTY_ONE, TWENTY_ONE)
body = text_of(sent)
check("counts agree", "(21)" in body)
check("it says so plainly", "API and cache agree" in body)
check("no false warning", "never arrived" not in body)
check("the embed stays blurple",
      any(e.colour and e.colour.value == 0x5865F2
          for e in sent if isinstance(e, discord.Embed)))

print("\n── 3. removed while offline (cache ahead of the API) ────────────")
sent = run(TWENTY_ONE, TEN)
body = text_of(sent)
check("it detects the opposite drift", "no longer lists them" in body)
check("...and counts it", "**11** server(s)" in body)
check("it does NOT claim a gateway fault", "never arrived" not in body)

print("\n── 4. the API call failing must not break the command ───────────")
sent = run(TEN, {}, rest_error=RuntimeError("503 Service Unavailable"))
body = text_of(sent)
check("the command still answers", bool(sent))
check("it falls back to the cache count", "(10)" in body, body[:200])
check("it says the API check failed", "API check failed" in body)
check("...naming the error", "503" in body)
check("it does not invent a drift warning", "never arrived" not in body)

print("\n── 5. intents and shards are surfaced ───────────────────────────")
sent = run(TEN, TEN, intents=FakeIntents(guilds=False, members=True))
check("a disabled guilds intent is called out",
      "`guilds` intent is OFF" in text_of(sent))
sent = run(TEN, TEN, intents=FakeIntents(guilds=True, members=False))
check("a disabled members intent explains approximate counts",
      "member counts are approximate" in text_of(sent))
sent = run(TEN, TEN, shard_count=4)
check("shard count is shown when sharded", "Shards: 4" in text_of(sent))
check("...and omitted when not",
      "Shards" not in text_of(run(TEN, TEN)))

print("\n── 6. edge cases ────────────────────────────────────────────────")
sent = run({}, {})
check("no servers at all is handled", bool(sent))
check("...with a readable message",
      "Not in any servers" in text_of(sent), text_of(sent)[:120])

sent = run({}, TEN)
body = text_of(sent)
check("a totally empty cache still lists the API's servers", "(10)" in body)
check("...and flags all ten as missing", "**10** server(s)" in body)

big = {2000 + i: f"Really Quite A Long Server Name Number {i}" for i in range(120)}
sent = run(big, big)
check("120 servers paginate rather than fail the 4096 limit", len(sent) > 1,
      len(sent))
check("every page is under Discord's limit",
      all(len(e.description or "") <= 4096
          for e in sent if isinstance(e, discord.Embed)),
      [len(e.description or "") for e in sent if isinstance(e, discord.Embed)])
check("the diagnosis lands on the LAST page only",
      sum(1 for e in sent if isinstance(e, discord.Embed) and e.fields) == 1)
check("page numbering is present", "page 1/" in text_of(sent))

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
