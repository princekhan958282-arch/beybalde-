#!/usr/bin/env python3
"""
tools/sim_gemini.py — verify boss dialogue degrades cleanly when the API dies.

The claim being tested is narrow and important: **an API outage must not change
a single thing about how a boss fight or a story fight plays.** It may only
change what the boss says.

Run:  python3 tools/sim_gemini.py
"""
import asyncio
import os
import sys
import time
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


# ── A fake aiohttp, so nothing here touches the network ──────────────────────
class _FakeResp:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def text(self):
        return self._body

    async def json(self):
        import json
        return json.loads(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    script = []          # list of _FakeResp | Exception, consumed in order
    calls = 0

    def __init__(self, *a, **kw):
        pass

    def post(self, *a, **kw):
        _FakeSession.calls += 1
        item = _FakeSession.script.pop(0) if _FakeSession.script else _FakeResp(200, "{}")
        if isinstance(item, Exception):
            raise item
        return item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


# A key must be present or say() short-circuits before ever reaching the breaker.
os.environ["GEMINI_API_KEY"] = "test-key-aaaaaaaaaaaaaaaaaaaa"

# Import BEFORE the fake is installed: cogs.battle.boss.__init__ pulls in
# discord.py, which subclasses aiohttp classes at import time and would explode
# against a stub. gemini.say() imports aiohttp lazily inside the call, so
# swapping sys.modules afterwards is still enough to intercept every request.
from cogs.battle.boss import gemini      # noqa: E402
import cogs.battle.boss.boss_ai as boss_ai  # noqa: E402

fake_aiohttp = types.ModuleType("aiohttp")
fake_aiohttp.ClientSession = _FakeSession
fake_aiohttp.ClientTimeout = lambda **kw: None
sys.modules["aiohttp"] = fake_aiohttp

OK_BODY = '{"candidates":[{"content":{"parts":[{"text":"A live line."}]}}]}'
STATE = {"boss_hp_pct": 50, "foe_hp_pct": 50, "turn": 4, "foe_habit": "attack"}


def reset():
    gemini._open_until = 0.0
    gemini._trips = 0
    gemini._reason = ""
    gemini._fatal_key = None
    gemini._no_aiohttp = False
    gemini._counts.update(ok=0, failed=0, skipped=0)
    gemini._last_said.clear()
    _FakeSession.script = []
    _FakeSession.calls = 0


def run(n=1, boss="Aetherion Drakos org", moment="winning"):
    out = []
    for _ in range(n):
        out.append(asyncio.get_event_loop().run_until_complete(
            gemini.say(boss, "proud", moment, STATE)))
    return out


asyncio.set_event_loop(asyncio.new_event_loop())

print("\n── 1. happy path ────────────────────────────────────────────────")
reset()
_FakeSession.script = [_FakeResp(200, OK_BODY)]
line = run(1)[0]
check("a 200 returns the API's line", line == "A live line.", line)
check("available() is True while healthy", gemini.available() is True)
check("status() reads 'live'", gemini.status()["state"] == "live",
      gemini.status()["state"])

print("\n── 2. rate limit (429) opens the breaker ────────────────────────")
reset()
_FakeSession.script = [_FakeResp(429, '{"error":{"message":"quota exceeded"}}')]
first = run(1)[0]
_all_lines = [ln for pool in (list(gemini.FALLBACK.values())
                              + [p for b in gemini.PERSONA_FALLBACK.values()
                                 for p in b.values()])
              for ln in pool]
check("the 429 itself yields a canned line", first in _all_lines, first)
check("breaker is now open", gemini._circuit_open() is True)
check("available() flips to False", gemini.available() is False)
calls_before = _FakeSession.calls
run(12)
check("12 further calls make ZERO network requests",
      _FakeSession.calls == calls_before, f"{_FakeSession.calls - calls_before} fired")
check("they are counted as skipped", gemini._counts["skipped"] == 12,
      gemini._counts["skipped"])
check("status() reads 'cooling down'", gemini.status()["state"] == "cooling down")
check("status() reports a retry countdown", gemini.status()["retry_in"] > 0)

print("\n── 3. suppressed calls are instant ──────────────────────────────")
t0 = time.monotonic()
run(200)
elapsed = time.monotonic() - t0
check("200 suppressed calls take under 100ms", elapsed < 0.1, f"{elapsed*1000:.0f}ms")

print("\n── 4. backoff doubles, and Retry-After is honoured ──────────────")
reset()
_FakeSession.script = [_FakeResp(429, "{}")]
run(1)
first_cool = gemini._open_until - time.monotonic()
gemini._open_until = 0.0                       # simulate the cooldown elapsing
_FakeSession.script = [_FakeResp(429, "{}")]
run(1)
second_cool = gemini._open_until - time.monotonic()
check("a second consecutive failure waits longer",
      second_cool > first_cool * 1.8, f"{first_cool:.0f}s then {second_cool:.0f}s")
check("cooldown never exceeds COOLDOWN_MAX",
      second_cool <= gemini.COOLDOWN_MAX + 1)

reset()
_FakeSession.script = [_FakeResp(
    429, '{"error":{"details":[{"retryDelay":"600s"}]}}')]
run(1)
cool = gemini._open_until - time.monotonic()
check("a retryDelay longer than our guess wins", cool > 590, f"{cool:.0f}s")

reset()
_FakeSession.script = [_FakeResp(429, "{}", {"Retry-After": "450"})]
run(1)
cool = gemini._open_until - time.monotonic()
check("a Retry-After header is honoured", cool > 440, f"{cool:.0f}s")

print("\n── 5. a probe that succeeds closes the breaker ──────────────────")
reset()
_FakeSession.script = [_FakeResp(500, "boom")]
run(1)
check("a 5xx opens the breaker", gemini._circuit_open() is True)
check("5xx uses the short server cooldown",
      gemini._open_until - time.monotonic() <= gemini.COOLDOWN_SERVER + 1)
gemini._open_until = 0.0
check("the breaker is half-open once the cooldown lapses",
      gemini._circuit_open() is False)
_FakeSession.script = [_FakeResp(200, OK_BODY)]
line = run(1)[0]
check("the probe reaches the network and returns a live line",
      line == "A live line.", line)
check("success resets the trip counter", gemini._trips == 0)
check("available() is True again", gemini.available() is True)

print("\n── 6. fatal failures, and the key-change escape hatch ───────────")
reset()
_FakeSession.script = [_FakeResp(404, "model not found")]
run(1)
cool = gemini._open_until - time.monotonic()
check("a 404 (bad model) locks out for hours",
      cool > 3600, f"{cool:.0f}s")
check("a 400 classifies as fatal", gemini._classify(400, "") == "fatal")
check("a 403 mentioning quota classifies as rate",
      gemini._classify(403, "Quota exceeded for requests") == "rate")
check("a bare 403 classifies as fatal",
      gemini._classify(403, "permission denied") == "fatal")
os.environ["GEMINI_API_KEY"] = "a-different-key-bbbbbbbbbbbb"
check("changing the key clears the lockout immediately",
      gemini._circuit_open() is False)

print("\n── 7. network faults ────────────────────────────────────────────")
reset()
_FakeSession.script = [OSError("connection reset")]
line = run(1)[0]
check("a socket error never propagates", isinstance(line, str) and line)
check("it opens the breaker", gemini._circuit_open() is True)
check("with the short network cooldown",
      gemini._open_until - time.monotonic() <= gemini.COOLDOWN_NETWORK + 1)

print("\n── 8. an empty 200 does NOT trip the breaker ────────────────────")
reset()
_FakeSession.script = [_FakeResp(
    200, '{"candidates":[{"finishReason":"MAX_TOKENS","content":{"parts":[]}}]}')]
line = run(1)[0]
check("an empty candidate still returns a line", isinstance(line, str) and line)
check("but leaves the breaker CLOSED — the API is healthy",
      gemini._circuit_open() is False)
check("MAX_OUTPUT is large enough for a thinking model",
      gemini.MAX_OUTPUT >= 256, gemini.MAX_OUTPUT)

print("\n── 9. canned lines are usable as the ONLY dialogue ──────────────")
reset()
for moment in ("intro", "winning", "losing", "phase", "victory", "defeat"):
    check(f"generic pool '{moment}' has depth",
          len(gemini.FALLBACK[moment]) >= 6, len(gemini.FALLBACK[moment]))

seen = []
for _ in range(40):
    seen.append(gemini.canned("winning", "Aetherion Drakos org"))
check("no canned line ever repeats back to back",
      all(a != b for a, b in zip(seen, seen[1:])))
check("Drakos speaks from its own pool",
      set(seen) <= set(gemini.PERSONA_FALLBACK["drakos"]["winning"]), set(seen))
nem = {gemini.canned("victory", "NEMESIS ÆTHERION org") for _ in range(20)}
check("Nemesis speaks from its own pool",
      nem <= set(gemini.PERSONA_FALLBACK["nemesis"]["victory"]), nem)
unknown = {gemini.canned("intro", "Some Other Boss") for _ in range(20)}
check("an unknown boss falls back to the generic pool",
      unknown <= set(gemini.FALLBACK["intro"]), unknown)
check("an unknown MOMENT never raises", isinstance(gemini.canned("???"), str))
check("canned() still works with the old one-arg call",
      isinstance(gemini.canned("intro"), str))

print("\n── 10. no key at all ────────────────────────────────────────────")
reset()
os.environ.pop("GEMINI_API_KEY", None)
_FakeSession.calls = 0
lines = run(5)
check("every line is a string", all(isinstance(x, str) and x for x in lines))
check("no network request is made without a key", _FakeSession.calls == 0)
check("available() is False", gemini.available() is False)
check("status() says 'no key'", gemini.status()["state"] == "no key")
os.environ["GEMINI_API_KEY"] = "test-key-aaaaaaaaaaaaaaaaaaaa"

print("\n── 11. the fight itself is untouched ────────────────────────────")
# The real guarantee: boss_ai and the story engine decide everything about a
# fight, and neither imports gemini. Assert that structurally rather than
# trusting the module docstring.
src_ai = open(boss_ai.__file__).read()
check("boss_ai.py does not import gemini",
      "import gemini" not in src_ai and "from . import gemini" not in src_ai)
story_files = [f"cogs/story/{n}" for n in os.listdir("cogs/story") if n.endswith(".py")]
check("no story module imports gemini",
      not any("gemini" in open(p).read() for p in story_files), story_files)

# And a full boss fight resolves identically with the API dead.
reset()
gemini._trip("rate", "simulated outage")
f1 = boss_ai.Fighter("A", 1000, 1000, 120, 100, 100)
f2 = boss_ai.Fighter("B", 1000, 1000, 110, 105, 95)
model = boss_ai.OpponentModel()
seq = []
for _ in range(40):
    if not (f1.alive() and f2.alive()):
        break
    mv, _vals = boss_ai.choose_move(f1, f2, model)
    seq.append(mv)
    boss_ai.resolve(f1, f2, mv, "attack")
check("a fight resolves normally while the breaker is open",
      len(seq) > 0 and gemini._circuit_open() is True, f"{len(seq)} turns")
check("the fight actually reached a conclusion",
      not (f1.alive() and f2.alive()), f"{f1.hp:.0f} vs {f2.hp:.0f}")

print(f"\n{'='*66}\n  {PASS} passed, {FAIL} failed\n{'='*66}")
sys.exit(1 if FAIL else 0)
