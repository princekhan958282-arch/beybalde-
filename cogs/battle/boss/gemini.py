"""
gemini.py  —  boss dialogue: the prompt, the fallback pools, and nothing else.

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

Where the transport went
------------------------
The HTTP client, the secrets chain and the circuit breaker used to live here
as module globals. They now live in `utils/llm.py` as a `Client`, because
server-chat banter needed exactly the same machinery and a second copy of a
circuit breaker is a second thing to fix. This module keeps the half that is
actually about bosses: the prompt, the canned pools, and the no-repeat rule.

`_client` below is this consumer's own breaker — a chat outage cannot silence
a boss fight, and vice versa. It is marked `priority`, so the shared hourly
quota in `utils/llm.py` will ration chat banter before it ever rations a boss.

Every public name this module used to export still works: `say`,
`say_with_deadline`, `canned`, `available`, `status`, `SPEAK_EVERY_N_TURNS`
and the cooldown constants.
"""

import asyncio
import logging
import random
from typing import Optional

from utils import llm

log = logging.getLogger("beyblade_bot")

# ── Re-exported so existing callers and tools keep working unchanged ─────────
API_KEY_ENV      = llm.API_KEY_ENV
DEFAULT_MODEL    = llm.DEFAULT_MODEL
ENDPOINT         = llm.ENDPOINT
REQUEST_TIMEOUT  = llm.REQUEST_TIMEOUT
MAX_OUTPUT       = llm.MAX_OUTPUT
COOLDOWN_RATE    = llm.COOLDOWN_RATE
COOLDOWN_SERVER  = llm.COOLDOWN_SERVER
COOLDOWN_NETWORK = llm.COOLDOWN_NETWORK
COOLDOWN_FATAL   = llm.COOLDOWN_FATAL
COOLDOWN_MAX     = llm.COOLDOWN_MAX

# The free tier allows roughly 10 requests a minute. Speaking every turn meant
# a 20-turn fight fired 20 calls and blew straight through that, after which
# every line came back canned anyway. Talking every few turns keeps the boss
# characterful and the quota intact.
SPEAK_EVERY_N_TURNS = 3

# `priority=True`: boss dialogue is never rationed by the shared hourly budget.
# Chat banter is the consumer that yields.
_client = llm.Client("boss", priority=True)

_last_said: dict = {}     # (boss, moment) -> last canned line, to avoid repeats


def _fingerprint(key: Optional[str]) -> Optional[str]:
    return llm.fingerprint(key)


def _classify(status_code: int, body: str) -> str:
    return llm.classify(status_code, body)


def _retry_after(headers, body: str) -> Optional[float]:
    return llm.retry_after(headers, body)


def _circuit_open() -> bool:
    return _client.circuit_open()


def _trip(kind: str, detail: str, retry_after_s: Optional[float] = None) -> None:
    _client.trip(kind, detail, retry_after_s)


def _recover() -> None:
    _client.recover()


def available() -> bool:
    """Whether a live API line can be expected right now."""
    return _client.available()


def status() -> dict:
    """Machine-readable state, for ;version / ;audit."""
    return _client.status()


# `MODEL` was a module-level constant resolved at import. Callers only ever
# read it for display, and `status()["model"]` is the live value, so this stays
# a plain string for compatibility.
MODEL = _client.model


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
    """Return a line for the boss. Always returns something, never raises.

    The fallback decision lives HERE rather than in `utils/llm.py`: what a
    boss says when the API is unreachable is a game question, and the
    transport has no business answering it. `ask()` returning None is the
    only signal it gives.
    """
    text = await _client.ask(_prompt(boss_name, persona, moment, state))
    return text or canned(moment, boss_name)


async def say_with_deadline(boss_name: str, persona: str, moment: str,
                            state: dict, deadline: float = REQUEST_TIMEOUT) -> str:
    """say() with a hard ceiling, so a hung socket can't stall a turn."""
    try:
        return await asyncio.wait_for(
            say(boss_name, persona, moment, state), timeout=deadline)
    except Exception:                                # noqa: BLE001
        return canned(moment, boss_name)
