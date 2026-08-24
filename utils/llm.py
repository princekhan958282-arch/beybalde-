"""
utils/llm.py — the one generative-text client, shared by every consumer.

Why this exists
---------------
`cogs/battle/boss/gemini.py` grew a genuinely good API client: the secrets
chain, a short timeout, and a circuit breaker that classifies failures and
backs off differently for each kind. All of that is transport — none of it
knows what a boss is. When server-chat banter needed exactly the same
behaviour, the choice was to copy that file or to share it, and copying the
hardest part of a feature is how two implementations drift until only one of
them has the bug fix.

So the transport lives here as a `Client`, and each consumer owns one:

    gemini.py         Client("boss")    boss dialogue
    community/chat.py Client("chat")    server banter

What stays with the CONSUMER, deliberately: the prompt, and the fallback line.
`ask()` returns `None` when it cannot produce text — it never invents a
replacement, because a sensible replacement is a game question ("what would
this boss say?") and this module has no business answering it.

One breaker per consumer, one quota for all
-------------------------------------------
The breaker is per-`Client`, so a chat outage cannot silence a boss fight and
a boss outage cannot silence chat. They are different failure surfaces and
they recover independently.

The *quota*, though, is genuinely shared — one API key, one free tier, and
roughly ten requests a minute in it. A chat bot that talks all day would eat
that quota and leave boss dialogue silently canned, which is the exact failure
this codebase keeps finding: one path degrades and the other never learns why.
So `_LEDGER` holds an hourly ceiling per consumer, and a consumer marked
`priority` is never refused by it. Chat can starve itself. It cannot starve a
boss.

Nothing here logs a key, and nothing here raises: the worst case is `None`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from utils import secrets

log = logging.getLogger("beyblade_bot.llm")

API_KEY_ENV = "GEMINI_API_KEY"

# gemini-2.0-flash was RETIRED on 31 March 2026, so naming it as a default
# means the API answers 404 and every consumer silently drops to its fallback
# with nothing in the log to explain why. 2.5-flash is on the free tier and
# stable; set GEMINI_MODEL to override.
DEFAULT_MODEL = "gemini-2.5-flash"
ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
            "{model}:generateContent")

REQUEST_TIMEOUT = 4.0     # a bot that pauses 10s to talk is worse than a quiet one

# Was 60 in the original. The 2.5 and 3.x flash models spend output tokens on
# internal thinking before writing anything, so a low ceiling was routinely
# consumed entirely by thinking: HTTP 200, finishReason MAX_TOKENS, an empty
# parts list, and a fallback line even though key, model and quota were fine.
# Raising it costs nothing when unused — the reply is still cut to one line.
MAX_OUTPUT = 512

# ── Circuit-breaker tuning ───────────────────────────────────────────────────
COOLDOWN_RATE    = 90.0          # first 429; doubles on each consecutive trip
COOLDOWN_SERVER  = 45.0          # 5xx — their end, usually brief
COOLDOWN_NETWORK = 30.0          # timeout / DNS / reset — may well be ours
COOLDOWN_FATAL   = 6 * 3600.0    # bad key or bad model; retrying cannot help
COOLDOWN_MAX     = 30 * 60.0     # ceiling for the exponential kinds


# ── Pure helpers ─────────────────────────────────────────────────────────────

def classify(status_code: int, body: str) -> str:
    """Which KIND of failure this is. Each kind deserves a different wait."""
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
    return "server"


def retry_after(headers, body: str) -> Optional[float]:
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


def fingerprint(key: Optional[str]) -> Optional[str]:
    """A short, non-reversible tag for a key, so a key CHANGE is detectable
    without ever holding or logging the key itself."""
    if not key:
        return None
    return f"{len(key)}:{hash(key) & 0xffff:04x}"


# ── The shared hourly quota ──────────────────────────────────────────────────

class _Ledger:
    """Requests spent per consumer in the last hour.

    A rolling count rather than a token bucket because the thing being
    protected is a per-hour/per-day free-tier allowance, and the honest
    question is "how many have we spent recently", not "may I have one now".
    """

    WINDOW = 3600.0

    def __init__(self) -> None:
        self._spent: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, consumer: str, now: float) -> list:
        stamps = [t for t in self._spent.get(consumer, ()) if now - t < self.WINDOW]
        self._spent[consumer] = stamps
        return stamps

    def spend(self, consumer: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._prune(consumer, now).append(now)

    def spent(self, consumer: str, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            return len(self._prune(consumer, now))

    def allows(self, consumer: str, ceiling: Optional[int],
               now: Optional[float] = None) -> bool:
        if ceiling is None:          # priority consumer, or no ceiling set
            return True
        return self.spent(consumer, now) < int(ceiling)

    def reset(self) -> None:
        with self._lock:
            self._spent.clear()


_LEDGER = _Ledger()


def ledger() -> _Ledger:
    """The process-wide quota ledger. Exposed for status panels and tests."""
    return _LEDGER


# ── The client ───────────────────────────────────────────────────────────────

class Client:
    """One consumer's view of the API: its own breaker, the shared quota.

    `ask()` is the only method that touches the network, and it can only ever
    return a string or `None` — never raise, never block past `timeout`.
    """

    def __init__(self, consumer: str, *, model: Optional[str] = None,
                 timeout: float = REQUEST_TIMEOUT,
                 max_output: int = MAX_OUTPUT,
                 hourly_budget: Optional[int] = None,
                 priority: bool = False) -> None:
        self.consumer = str(consumer)
        # Resolved per instance rather than at import: a model set in
        # config_local.py after start-up is picked up by `;reload`.
        self._model_override = model
        self.timeout = float(timeout)
        self.max_output = int(max_output)
        # `priority` wins over any ceiling — boss dialogue is never rationed.
        self.hourly_budget = None if priority else hourly_budget
        self.priority = bool(priority)

        # Breaker state, per instance. The original lived in module globals,
        # which is precisely why it could not be shared.
        self.open_until = 0.0    # time.monotonic() deadline; 0 = closed
        self.trips = 0           # consecutive failures, drives the backoff
        self.reason = ""         # human-readable, surfaced by status()
        self.opened_at = 0.0     # wall clock, for status()
        self.fatal_key = None    # key fingerprint at the time of a fatal trip
        self.no_aiohttp = False  # permanent for this process
        self.counts = {"ok": 0, "failed": 0, "skipped": 0, "budget": 0}

    # ── model / key ──────────────────────────────────────────────────────────
    @property
    def model(self) -> str:
        return self._model_override or secrets.get("GEMINI_MODEL") or DEFAULT_MODEL

    def _key(self) -> Optional[str]:
        return secrets.get(API_KEY_ENV)

    # ── breaker ──────────────────────────────────────────────────────────────
    def circuit_open(self) -> bool:
        """True while the breaker is holding calls back.

        A fatal trip is released early if the key has CHANGED since it
        happened — otherwise fixing a typo in config_local.py would mean
        waiting out COOLDOWN_FATAL.
        """
        if self.no_aiohttp:
            return True
        if not self.open_until:
            return False
        if self.fatal_key is not None and fingerprint(self._key()) != self.fatal_key:
            log.info("[llm:%s] API key changed — clearing the failure lockout.",
                     self.consumer)
            self.open_until, self.trips, self.reason, self.fatal_key = 0.0, 0, "", None
            return False
        if time.monotonic() >= self.open_until:
            # Half-open: let exactly one probe through. `trips` is deliberately
            # kept, so a probe that fails again backs off further rather than
            # restarting at the shortest cooldown.
            self.open_until = 0.0
            log.info("[llm:%s] cooldown elapsed — probing with the next call.",
                     self.consumer)
            return False
        return True

    def trip(self, kind: str, detail: str,
             retry_in: Optional[float] = None) -> None:
        """Open the breaker. Never raises; only ever delays future requests."""
        base = {"rate":    COOLDOWN_RATE,
                "server":  COOLDOWN_SERVER,
                "network": COOLDOWN_NETWORK,
                "fatal":   COOLDOWN_FATAL}.get(kind, COOLDOWN_SERVER)

        self.trips += 1
        self.counts["failed"] += 1
        if kind == "fatal":
            cool = base
            self.fatal_key = fingerprint(self._key())
        else:
            cool = min(COOLDOWN_MAX, base * (2 ** (self.trips - 1)))
            self.fatal_key = None

        # Google tells us how long to wait, in a Retry-After header or a
        # RetryInfo block. Honour it when it is longer than our own guess —
        # ignoring it is how you get a second 429 the moment the cooldown lapses.
        if retry_in:
            cool = max(cool, min(float(retry_in), COOLDOWN_MAX))

        self.open_until = time.monotonic() + cool
        self.opened_at = time.time()
        self.reason = detail

        when = f"{cool:.0f}s" if cool < 120 else f"{cool / 60.0:.0f}m"
        log.warning("[llm:%s] %s — falling back to written lines for %s. "
                    "Only the wording changes. (%s)",
                    self.consumer, kind, when, detail)

    def recover(self) -> None:
        """A request succeeded — close the breaker."""
        self.counts["ok"] += 1
        if self.trips or self.open_until:
            log.info("[llm:%s] API responding again after %d failed attempt(s).",
                     self.consumer, self.trips)
        self.open_until, self.trips, self.reason, self.fatal_key = 0.0, 0, "", None

    # ── reporting ────────────────────────────────────────────────────────────
    def available(self) -> bool:
        """Whether a live API line can be expected right now.

        False with no key, with aiohttp missing, while the breaker is open, and
        once this consumer's hourly budget is gone. Callers label UI with this,
        so it must describe reality rather than the presence of configuration.
        """
        return (bool(self._key())
                and not self.circuit_open()
                and _LEDGER.allows(self.consumer, self.hourly_budget))

    def status(self) -> dict:
        """Machine-readable state, for ;version / ;audit / the panels."""
        remaining = max(0.0, self.open_until - time.monotonic()) if self.open_until else 0.0
        if self.no_aiohttp:
            state = "unavailable"
        elif not self._key():
            state = "no key"
        elif remaining > 0:
            state = "cooling down"
        elif not _LEDGER.allows(self.consumer, self.hourly_budget):
            state = "budget spent"
        elif self.trips:
            state = "probing"
        else:
            state = "live"
        return {
            "consumer":      self.consumer,
            "state":         state,
            "model":         self.model,
            "reason":        self.reason,
            "retry_in":      int(remaining),
            "trips":         self.trips,
            "opened_at":     self.opened_at,
            "budget":        self.hourly_budget,
            "spent_hour":    _LEDGER.spent(self.consumer),
            "calls_ok":      self.counts["ok"],
            "calls_failed":  self.counts["failed"],
            "calls_skipped": self.counts["skipped"],
            "calls_budget":  self.counts["budget"],
        }

    # ── the network ──────────────────────────────────────────────────────────
    async def ask(self, prompt: str, *, temperature: float = 1.0,
                  max_chars: int = 180) -> Optional[str]:
        """One line of generated text, or None if we could not get one.

        None is not an error to report to a user — it means "use your own
        fallback". Every failure path returns it.
        """
        key = self._key()
        if not key:
            return None

        if self.circuit_open():
            # No socket, no wait, no quota spent. This is the whole point.
            self.counts["skipped"] += 1
            return None

        if not _LEDGER.allows(self.consumer, self.hourly_budget):
            # Self-imposed, so it is counted separately from a real failure:
            # this is not the API refusing us, it is us refusing ourselves in
            # order to leave headroom for a priority consumer.
            self.counts["budget"] += 1
            return None

        try:
            import aiohttp
        except ImportError:
            if not self.no_aiohttp:
                self.no_aiohttp = True
                log.warning("[llm:%s] aiohttp is not installed — written lines "
                            "only for this run.", self.consumer)
            return None

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": self.max_output,
                "temperature": float(temperature),
            },
            # Standard safety defaults are kept rather than loosened: this is a
            # game aimed at a general Discord audience.
        }

        model = self.model
        _LEDGER.spend(self.consumer)
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    ENDPOINT.format(model=model),
                    headers={"x-goog-api-key": key,
                             "Content-Type": "application/json"},
                    json=payload,
                ) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:400]
                        self.trip(classify(resp.status, body),
                                  f"HTTP {resp.status} on model '{model}': {body[:180]}",
                                  retry_after(resp.headers, body))
                        return None
                    data = await resp.json()

            # Parsed one step at a time rather than as a chain of .get() calls
            # with [0] on the end. A thinking model that spends its whole token
            # budget before writing replies HTTP 200 with `"parts": []`, and
            # indexing that raised IndexError — which the outer handler could
            # only read as a transport failure, tripping the breaker and going
            # quiet for half an hour because the API had worked perfectly.
            candidates = data.get("candidates") or []
            candidate = candidates[0] if candidates else {}
            parts = (candidate.get("content") or {}).get("parts") or []
            text = ""
            for part in parts:
                text = (part or {}).get("text") or ""
                if text:
                    break
            text = text.strip().strip('"').strip()
            text = text.split("\n")[0][:int(max_chars)]

            # A 200 is a healthy API even when the text is unusable, so this
            # closes the breaker rather than tripping it — an empty candidate
            # is a prompt or token-budget problem, and locking out for hours
            # would be the wrong response to it.
            self.recover()
            if not text:
                log.debug("[llm:%s] empty candidate (finishReason=%s)",
                          self.consumer, candidate.get("finishReason", "?"))
                return None
            return text

        except asyncio.TimeoutError:
            self.trip("network", f"no response within {self.timeout:.0f}s")
            return None
        except Exception as exc:                         # noqa: BLE001
            self.trip("network", f"{type(exc).__name__}: {str(exc)[:120]}")
            return None

    async def ask_with_deadline(self, prompt: str, *, deadline: Optional[float] = None,
                                **kw) -> Optional[str]:
        """`ask()` with a hard ceiling, so a hung socket can't stall a caller."""
        try:
            return await asyncio.wait_for(
                self.ask(prompt, **kw), timeout=deadline or self.timeout)
        except Exception:                                # noqa: BLE001
            return None
