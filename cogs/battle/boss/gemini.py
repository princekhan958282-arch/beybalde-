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

The key comes from utils/secrets.py, which checks real environment variables,
then a .env file, then config_local.py. None of those require hosting-panel
support — the last two are ordinary files next to app.py. The key is never
logged and never included in a zip.
"""

import asyncio
import logging
import os
import random
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
MAX_OUTPUT      = 60      # tokens — one or two lines of trash talk

# The free tier allows roughly 10 requests a minute. Speaking every turn meant
# a 20-turn fight fired 20 calls and blew straight through that, after which
# every line came back canned anyway. Talking every few turns keeps the boss
# characterful and the quota intact.
SPEAK_EVERY_N_TURNS = 3

_warned: set = set()      # so a broken key/model is logged loudly ONCE

# Fires only when the LLM is unavailable, which must never look broken.
FALLBACK = {
    "intro": [
        "So you've come. Let's see if you can keep up.",
        "Another challenger. Try to make this interesting.",
        "You picked the wrong stadium to walk into.",
    ],
    "winning": [
        "Is that everything you've got?",
        "You're already slowing down.",
        "I've seen this pattern before. It doesn't end well for you.",
    ],
    "losing": [
        "Not bad. Now I'm paying attention.",
        "You've earned that hit. You won't get another.",
        "Interesting. Let's raise the tempo.",
    ],
    "phase": [
        "Enough warm-up.",
        "You've forced my hand. Good.",
        "Now we start properly.",
    ],
    "victory": [
        "Predictable to the end.",
        "Come back when your blade can keep pace.",
        "A good effort. Not a good enough one.",
    ],
    "defeat": [
        "…Well fought. Genuinely.",
        "You read me. I won't forget that.",
        "The stadium is yours. This time.",
    ],
}


def available() -> bool:
    return bool(secrets.get(API_KEY_ENV))


def canned(moment: str, rng=random) -> str:
    return rng.choice(FALLBACK.get(moment, FALLBACK["winning"]))


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
    key = secrets.get(API_KEY_ENV)
    if not key:
        return canned(moment)

    try:
        import aiohttp
    except ImportError:
        return canned(moment)

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
                    body = (await resp.text())[:180]
                    # Loud the first time, quiet after — a wrong model name or
                    # a revoked key should be obvious in the console, not
                    # something you discover weeks later.
                    if resp.status not in _warned:
                        _warned.add(resp.status)
                        log.warning(
                            f"[gemini] HTTP {resp.status} using model "
                            f"'{MODEL}' — boss dialogue will use canned lines. "
                            f"{body}")
                    else:
                        log.debug(f"[gemini] HTTP {resp.status}: {body}")
                    return canned(moment)
                data = await resp.json()

        text = (data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")) or ""
        text = text.strip().strip('"').strip()
        # One line only, and never long enough to wreck the embed layout.
        text = text.split("\n")[0][:180]
        return text or canned(moment)

    except asyncio.TimeoutError:
        log.debug("[gemini] timed out — using a canned line")
        return canned(moment)
    except Exception as exc:
        log.debug(f"[gemini] failed ({type(exc).__name__}) — using a canned line")
        return canned(moment)


async def say_with_deadline(boss_name: str, persona: str, moment: str,
                            state: dict, deadline: float = REQUEST_TIMEOUT) -> str:
    """say() with a hard ceiling, so a hung socket can't stall a turn."""
    try:
        return await asyncio.wait_for(
            say(boss_name, persona, moment, state), timeout=deadline)
    except Exception:
        return canned(moment)
