"""
gemini.py  —  optional Gemini layer for boss dialogue

Scope, deliberately narrow: Gemini writes what the boss SAYS. It does not pick
moves. boss_ai.py picks moves, in about 1.7ms, with a two-ply search over the
exact rules — an LLM cannot beat that at arithmetic, and a per-turn API call
would add a second of latency plus a bill to every single exchange.

Consequences of that split, all good:
  * no API key?  the boss still fights at full strength, just with canned lines
  * rate limited? same
  * network down? same
  * the LLM can never make the boss play badly, because it never plays

Story Mode (cogs/story/) does not import this module at all — it has no
dialogue layer, so nothing in a story fight can be affected by the API's state.

The key comes from utils/secrets.py, which checks real environment variables,
then a .env file, then config_local.py. None of those require hosting-panel
support — the last two are ordinary files next to app.py. The key is never
logged and never included in a zip.

── The circuit breaker ───────────────────────────────────────────────────────
Returning a canned line on a failed request was always enough to keep the fight
CORRECT, but not enough to keep it FAST. Once the free tier's quota is gone
every subsequent call still opened a socket, waited, and failed, and two of the
call sites (BossCog.finish) await the result inline — so a rate-limited player
sat looking at a frozen "you lost" for up to REQUEST_TIMEOUT seconds per fight
end, while each doomed request pushed the quota window further out.

So a failure now OPENS the breaker: subsequent calls return a canned line
instantly with no network at all, until a cooldown expires. The first call
after that is a probe — success closes the breaker, another failure re-opens it
with double the cooldown, up to COOLDOWN_MAX. available() reports the breaker's
state, so the UI can honestly say "dialogue offline" instead of claiming a
working API while every line is canned.

Failures are classified, because they deserve very different waits:
  rate     429, or a 403 whose body mentions quota  — backs off exponentially
  fatal    400/401/403/404: bad key, bad model      — hours; retrying cannot help
  server   5xx                                      — short, it's their end
  network  timeout, DNS, connection reset           — short, it may be ours

A fatal trip is cancelled the moment the API key CHANGES, so fixing a typo in
config_local.py and restarting (or ;reload) takes effect immediately instead of
waiting out a six-hour lockout.
"""

import asyncio
import logging
import random
import time
from typing import Optional

from utils import secrets

log = logging.getLogger("beyblade_bot")

API_KEY_ENV = "GEMINI_API_KEY"
# gemini-2.0-flash was RETIRED on 31 March 2026, so the original default here
# was already dead on arrival: the API answers 404 and the boss silently drops
# to canned lines with nothing in the log to explain why. 2.5-flash is on the
# free tier and stable; set GEMINI_MODEL to override (e.g. gemini-3.6-flash, or
# gemini-flash-latest to always track the newest release).
DEFAULT_MODEL = "gemini-2.5-flash"
MODEL       = secrets.get("GEMINI_MODEL") or DEFAULT_MODEL
ENDPOINT    = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "{model}:generateContent")

REQUEST_TIMEOUT = 4.0     # a boss that pauses 10s to talk is worse than a quiet one

# Was 60. The 2.5 and 3.x flash models spend output tokens on internal thinking
# before writing anything, so a 60-token ceiling was routinely consumed entirely
# by thinking: the response came back HTTP 200 with finishReason MAX_TOKENS and
# an empty parts list, and the boss fell to canned lines even though the key,
# the model and the quota were all fine. Raising the ceiling costs nothing when
# it isn't used — the reply is still cut to one line of 180 characters below —
# and it is the one setting that makes a *working* key actually produce lines.
MAX_OUTPUT      = 512

# The free tier allows roughly 10 requests a minute. Speaking every turn meant
# a 20-turn fight fired 20 calls and blew straight through that, after which
# every line came back canned anyway. Talking every few turns keeps the boss
# characterful and the quota intact.
SPEAK_EVERY_N_TURNS = 3

# ── Circuit-breaker tuning ───────────────────────────────────────────────────
COOLDOWN_RATE    = 90.0          # first 429; doubles on each consecutive trip
COOLDOWN_SERVER  = 45.0          # 5xx — their end, usually brief
COOLDOWN_NETWORK = 30.0          # timeout / DNS / reset — may well be ours
COOLDOWN_FATAL   = 6 * 3600.0    # bad key or bad model; no amount of retrying helps
COOLDOWN_MAX     = 30 * 60.0     # ceiling for the exponential kinds

_open_until  = 0.0        # time.monotonic() deadline; 0 = closed
_trips       = 0          # consecutive failures, drives the exponential backoff
_reason      = ""         # human-readable, surfaced by status()
_opened_at   = 0.0        # wall clock, for status()
_fatal_key   = None       # key fingerprint at the time of a fatal trip
_no_aiohttp  = False      # permanent for this process — aiohttp isn't installed
_counts      = {"ok": 0, "failed": 0, "skipped": 0}

_last_said: dict = {}     # (boss, moment) -> last canned line, to avoid repeats


def _fingerprint(key: Optional[str]) -> Optional[str]:
    """A short, non-reversible tag for a key, so a key CHANGE is detectable
    without ever holding or logging the key itself."""
    if not key:
        return None
    return f"{len(key)}:{hash(key) & 0xffff:04x}"


def _circuit_open() -> bool:
    """True while the breaker is holding calls back.

    A fatal trip is released early if the key has changed since it happened —
    otherwise fixing a typo would mean waiting out COOLDOWN_FATAL.
    """
    global _open_until, _trips, _reason, _fatal_key
    if _no_aiohttp:
        return True
    if not _open_until:
        return False
    if _fatal_key is not None and _fingerprint(secrets.get(API_KEY_ENV)) != _fatal_key:
        log.info("[gemini] API key changed — clearing the failure lockout, "
                 "dialogue will be retried on the next line.")
        _open_until, _trips, _reason, _fatal_key = 0.0, 0, "", None
        return False
    if time.monotonic() >= _open_until:
        # Half-open: let exactly one probe through. _trips is deliberately kept,
        # so a probe that fails again backs off further rather than restarting
        # at the shortest cooldown.
        _open_until = 0.0
        log.info("[gemini] cooldown elapsed — probing the API with the next line.")
        return False
    return True


def _trip(kind: str, detail: str, retry_after: Optional[float] = None) -> None:
    """Open the breaker. Never raises; only ever delays future requests."""
    global _open_until, _trips, _reason, _opened_at, _fatal_key
    base = {"rate":    COOLDOWN_RATE,
            "server":  COOLDOWN_SERVER,
            "network": COOLDOWN_NETWORK,
            "fatal":   COOLDOWN_FATAL}.get(kind, COOLDOWN_SERVER)

    _trips += 1
    _counts["failed"] += 1
    if kind == "fatal":
        cool = base
        _fatal_key = _fingerprint(secrets.get(API_KEY_ENV))
    else:
        cool = min(COOLDOWN_MAX, base * (2 ** (_trips - 1)))
        _fatal_key = None

    # Google tells us how long to wait, in a Retry-After header or a RetryInfo
    # block in the error body. Honour it when it is longer than our own guess —
    # ignoring it is how you get a second 429 the moment the cooldown lapses.
    if retry_after:
        cool = max(cool, min(float(retry_after), COOLDOWN_MAX))

    _open_until = time.monotonic() + cool
    _opened_at  = time.time()
    _reason     = detail

    mins = cool / 60.0
    when = f"{cool:.0f}s" if cool < 120 else f"{mins:.0f}m"
    log.warning("[gemini] %s — switching boss dialogue to canned lines for %s. "
                "Fights are unaffected; only the trash talk changes. (%s)",
                kind, when, detail)


def _recover() -> None:
    """A request succeeded — close the breaker."""
    global _open_until, _trips, _reason, _fatal_key
    _counts["ok"] += 1
    if _trips or _open_until:
        log.info("[gemini] API responding again after %d failed attempt(s) — "
                 "boss dialogue is back.", _trips)
    _open_until, _trips, _reason, _fatal_key = 0.0, 0, "", None


def _classify(status_code: int, body: str) -> str:
    if status_code == 429:
        return "rate"
    if status_code == 403:
        # 403 covers both "this key may not do that" (fatal) and some quota
        # exhaustion responses (temporary). The body is the only way to tell.
        low = body.lower()
        if "quota" in low or "rate" in low or "exhaust" in low or "limit" in low:
            return "rate"
        return "fatal"
    if status_code in (400, 401, 404):
        return "fatal"
    if status_code >= 500:
        return "server"
    return "server"


def _retry_after(headers, body: str) -> Optional[float]:
    """Seconds Google asked us to wait, from the header or the RetryInfo body."""
    try:
        raw = headers.get("Retry-After") if headers else None
        if raw:
            return float(str(raw).strip())
    except (TypeError, ValueError):
        pass
    # "retryDelay": "21s" inside error.details[]. Parsed off the raw text so a
    # body that isn't valid JSON can't throw.
    try:
        marker = '"retryDelay"'
        i = body.find(marker)
        if i != -1:
            chunk = body[i + len(marker):i + len(marker) + 32]
            digits = ""
            for ch in chunk:
                if ch.isdigit() or (ch == "." and digits):
                    digits += ch
                elif digits:
                    break
            if digits:
                return float(digits)
    except (ValueError, TypeError):
        pass
    return None


def available() -> bool:
    """Whether a live API line can be expected right now.

    False when there is no key, when aiohttp is missing, and — the addition —
    while the breaker is open. Callers use this to label the UI, so it must
    describe reality rather than merely the presence of configuration.
    """
    return bool(secrets.get(API_KEY_ENV)) and not _circuit_open()


def status() -> dict:
    """Machine-readable state, for ;version / ;audit."""
    remaining = max(0.0, _open_until - time.monotonic()) if _open_until else 0.0
    if _no_aiohttp:
        state = "unavailable"
    elif not secrets.get(API_KEY_ENV):
        state = "no key"
    elif remaining > 0:
        state = "cooling down"
    elif _trips:
        state = "probing"
    else:
        state = "live"
    return {
        "state":       state,
        "model":       MODEL,
        "reason":      _reason,
        "retry_in":    int(remaining),
        "trips":       _trips,
        "opened_at":   _opened_at,
        "calls_ok":    _counts["ok"],
        "calls_failed": _counts["failed"],
        "calls_skipped": _counts["skipped"],
    }


# ── Canned dialogue ──────────────────────────────────────────────────────────
# This is not filler. Every fight opens on a canned intro, and with no key at
# all it is the ONLY dialogue the game has, so the pools are deep enough that a
# forty-turn fight speaking every third turn does not visibly loop.

FALLBACK = {
    "intro": [
        "So you've come. Let's see if you can keep up.",
        "Another challenger. Try to make this interesting.",
        "You picked the wrong stadium to walk into.",
        "Step in, then. The floor has held better than you.",
        "I hope you brought more than that blade.",
        "Begin. I have all the time in the world.",
    ],
    "winning": [
        "Is that everything you've got?",
        "You're already slowing down.",
        "I've seen this pattern before. It doesn't end well for you.",
        "Your spin is fading. Mine hasn't started.",
        "Keep going. I want to see the exact moment you stop.",
        "You are throwing your blade at a wall and calling it strategy.",
    ],
    "losing": [
        "Not bad. Now I'm paying attention.",
        "You've earned that hit. You won't get another.",
        "Interesting. Let's raise the tempo.",
        "That one landed. Do it again, if you can find it twice.",
        "Good. I was beginning to think you'd wasted my time.",
        "So there is something behind the bravado after all.",
    ],
    "phase": [
        "Enough warm-up.",
        "You've forced my hand. Good.",
        "Now we start properly.",
        "I'll stop holding back. You won't enjoy the difference.",
        "Consider the courtesy withdrawn.",
        "You wanted my full attention. It's yours.",
    ],
    "victory": [
        "Predictable to the end.",
        "Come back when your blade can keep pace.",
        "A good effort. Not a good enough one.",
        "You lasted longer than most. That is not the same as winning.",
        "The stadium is quiet again. As it should be.",
        "Train. Then try again. I'll still be here.",
    ],
    "defeat": [
        "…Well fought. Genuinely.",
        "You read me. I won't forget that.",
        "The stadium is yours. This time.",
        "I have no complaint. You were simply better.",
        "Remember this feeling. You'll need it again.",
        "Take the win. You earned every point of it.",
    ],
}

# Per-boss pools, matched on a substring of the boss's name. Falls back to the
# generic pool above for any moment a boss doesn't define, so adding a boss can
# never leave a moment with nothing to say.
PERSONA_FALLBACK = {
    # a proud celestial dragon, grand and unhurried, who treats the duel as a
    # rite rather than a fight
    "drakos": {
        "intro": [
            "The rite begins. Stand where the light can see you.",
            "Few are permitted this. Fewer still deserve it.",
            "I have circled stars older than your name. Begin.",
        ],
        "winning": [
            "You strike as though haste were a virtue.",
            "The heavens do not hurry. Neither will I.",
            "Your blade writes nothing on me worth reading.",
        ],
        "losing": [
            "Ah. The rite has teeth this time.",
            "You have drawn light from me. That is rare.",
            "Good. Let this be worth remembering.",
        ],
        "phase": [
            "The crown turns. Witness it.",
            "I set aside the ceremony. What follows is older.",
            "You have earned the second half of this rite.",
        ],
        "victory": [
            "The rite closes. You were a worthy verse in it.",
            "Return when your spin can carry starlight.",
            "Rest. The sky keeps its record without you.",
        ],
        "defeat": [
            "Then the rite belongs to you. Carry it well.",
            "You have taken something from the heavens. Do not waste it.",
            "I yield the circle. That has not happened often.",
        ],
    },
    # an ancient divine arbiter who speaks in verdicts, not threats; utterly
    # certain, never raises its voice
    "nemesis": {
        "intro": [
            "The case is opened. You will be measured.",
            "You may speak with your blade. Nothing else is admitted.",
            "I have judged worlds. This will take moments.",
        ],
        "winning": [
            "The finding is unchanged.",
            "You argue loudly and prove nothing.",
            "Every exchange narrows your defence.",
        ],
        "losing": [
            "Noted. The record is amended.",
            "An unexpected point. It will be weighed.",
            "You have earned a hearing. Continue.",
        ],
        "phase": [
            "Sentence is passed.",
            "The proceedings advance. Your position does not.",
            "Leniency is withdrawn from the record.",
        ],
        "victory": [
            "The verdict stands. It always did.",
            "Appeal when you have evidence.",
            "You were heard in full. It changed nothing.",
        ],
        "defeat": [
            "The judgement falls against me. It is recorded.",
            "You have overturned a verdict. Few ever do.",
            "I accept the finding. Take what it grants you.",
        ],
    },
}


def _pool(moment: str, boss_name: Optional[str]) -> list:
    generic = FALLBACK.get(moment) or FALLBACK["winning"]
    if not boss_name:
        return generic
    low = boss_name.lower()
    for tag, pools in PERSONA_FALLBACK.items():
        if tag in low:
            return pools.get(moment) or generic
    return generic


def canned(moment: str, boss_name: Optional[str] = None, rng=random) -> str:
    """A fallback line. Never repeats the previous line for the same moment.

    The no-repeat rule matters more than it looks: BossView.refresh_line drops
    the update entirely when the new line equals the one already on screen, so
    without it a repeated draw read as the boss falling silent.
    """
    pool = _pool(moment, boss_name)
    if not pool:
        return "…"
    slot = ((boss_name or ""), moment)
    previous = _last_said.get(slot)
    choices = [ln for ln in pool if ln != previous] or list(pool)
    line = rng.choice(choices)
    _last_said[slot] = line
    return line


def _prompt(boss_name: str, persona: str, moment: str, state: dict) -> str:
    return (
        f"You are {boss_name}, a Beyblade boss in a Discord game. "
        f"Personality: {persona}.\n"
        f"Situation: {moment}.\n"
        f"Your HP {state.get('boss_hp_pct', 0):.0f}%, "
        f"challenger HP {state.get('foe_hp_pct', 0):.0f}%, "
        f"turn {state.get('turn', 1)}.\n"
        f"The challenger has been favouring: {state.get('foe_habit', 'nothing obvious')}.\n\n"
        "Write ONE line of in-character trash talk, under 18 words. "
        "No quotation marks, no emoji, no narration, no stage directions. "
        "Just the line."
    )


async def say(boss_name: str, persona: str, moment: str, state: dict) -> str:
    """Return a line for the boss. Always returns something, never raises."""
    global _no_aiohttp

    key = secrets.get(API_KEY_ENV)
    if not key:
        return canned(moment, boss_name)

    if _circuit_open():
        # No socket, no wait, no quota spent. This is the whole point.
        _counts["skipped"] += 1
        return canned(moment, boss_name)

    try:
        import aiohttp
    except ImportError:
        if not _no_aiohttp:
            _no_aiohttp = True
            log.warning("[gemini] aiohttp is not installed — boss dialogue will "
                        "use canned lines for this run. Fights are unaffected.")
        return canned(moment, boss_name)

    payload = {
        "contents": [{"parts": [{"text": _prompt(boss_name, persona, moment, state)}]}],
        "generationConfig": {
            "maxOutputTokens": MAX_OUTPUT,
            "temperature": 1.0,
        },
        # The boss is meant to be menacing, not filtered into blandness, but
        # this is a game aimed at a general Discord audience — keep the
        # standard safety defaults rather than loosening them.
    }

    url = ENDPOINT.format(model=MODEL)
    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json=payload,
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:400]
                    _trip(_classify(resp.status, body),
                          f"HTTP {resp.status} on model '{MODEL}': {body[:180]}",
                          _retry_after(resp.headers, body))
                    return canned(moment, boss_name)
                data = await resp.json()

        # Parsed one step at a time rather than as a chain of .get() calls with
        # [0] on the end. A thinking model that spends its whole token budget
        # before writing replies HTTP 200 with `"parts": []`, and indexing that
        # raised IndexError — which the outer handler could only read as a
        # transport failure, tripping the breaker and silencing dialogue for
        # half an hour because the API had worked perfectly.
        candidates = data.get("candidates") or []
        candidate = candidates[0] if candidates else {}
        parts = (candidate.get("content") or {}).get("parts") or []
        text = ""
        for part in parts:
            text = (part or {}).get("text") or ""
            if text:
                break
        text = text.strip().strip('"').strip()
        # One line only, and never long enough to wreck the embed layout.
        text = text.split("\n")[0][:180]

        # A 200 is a healthy API even when the text is unusable, so this closes
        # the breaker rather than tripping it — an empty candidate is a prompt
        # or token-budget problem, and locking dialogue out for half an hour
        # would be the wrong response to it.
        _recover()
        if not text:
            log.debug("[gemini] empty candidate (finishReason=%s) — canned line",
                      candidate.get("finishReason", "?"))
            return canned(moment, boss_name)
        return text

    except asyncio.TimeoutError:
        _trip("network", f"no response within {REQUEST_TIMEOUT:.0f}s")
        return canned(moment, boss_name)
    except Exception as exc:
        _trip("network", f"{type(exc).__name__}: {str(exc)[:120]}")
        return canned(moment, boss_name)


async def say_with_deadline(boss_name: str, persona: str, moment: str,
                            state: dict, deadline: float = REQUEST_TIMEOUT) -> str:
    """say() with a hard ceiling, so a hung socket can't stall a turn."""
    try:
        return await asyncio.wait_for(
            say(boss_name, persona, moment, state), timeout=deadline)
    except Exception:
        return canned(moment, boss_name)
