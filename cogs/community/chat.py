"""
cogs/community/chat.py — whether the bot speaks, and what it says.

Imports no discord. The listener in `cog.py` flattens a `discord.Message` into
an `Incoming` and hands it here, so every decision in this file is reachable
from a test with no gateway, no database and no clock. That split is the point:
"does the bot talk to this message" is the half with all the risk in it, and a
rule you can only exercise by running a live bot is a rule nobody exercises.

The safety posture, stated once
-------------------------------
This is the first thing in the codebase that makes the bot speak WITHOUT being
commanded. The failure everyone has seen is a chatbot that will not shut up, so
the defaults are stacked the other way:

  * `banter_intensity` ships **OFF**. Merging this changes nothing in a live
    server until the owner opts in.
  * Mention and reply are the only paths on by default, and they are still
    rate-limited per user.
  * Unprompted banter needs the intensity setting AND a per-channel cooldown
    AND a global cooldown AND a room that is not quiet AND a dice roll.
  * Any single user can turn it off for themselves with `;chat off`.
  * `guard.listener_ok` is checked first and unset-means-locked, so a
    misconfigured install is silent rather than loose.

The post-filter binds both the LLM and the authored lines. Trusting a model to
respect "under 20 words, no pings" is how you ship a bot that @everyone's a
server at 3am.
"""

from __future__ import annotations

import logging
import random
import re
from typing import Optional

from . import config as C
from . import cooldowns as CD
from . import memory as MEM
from . import personality as P
from .guard import listener_ok
from utils import llm

log = logging.getLogger("beyblade_bot.community.chat")

# ── Rate limits ──────────────────────────────────────────────────────────────
# Unprompted banter: probability per eligible message, and a floor between
# lines in the same channel. Both gates apply — the probability keeps it from
# feeling scripted, the cooldown keeps a burst of traffic from turning into a
# burst of bot.
BANTER = {
    "OFF":     {"chance": 0.00, "channel_gap": 0.0},
    "LIGHT":   {"chance": 0.02, "channel_gap": 900.0},   # ~once per 15 min at most
    "NORMAL":  {"chance": 0.06, "channel_gap": 300.0},
    "CHAOTIC": {"chance": 0.15, "channel_gap": 90.0},
}
DEFAULT_INTENSITY = "OFF"

GLOBAL_GAP = 45.0     # across the whole server, any channel
REPLY_GAP  = 8.0      # per user, applies to mention/reply so one person
                      # cannot turn the bot into their personal megaphone

HARD_CHAR_CAP = 300   # nothing this bot says is ever longer than this

# ── Patterns ─────────────────────────────────────────────────────────────────
_MASS_PING = re.compile(r"@(everyone|here)", re.IGNORECASE)
_ROLE_PING = re.compile(r"<@&\d+>")
_ANY_PING  = re.compile(r"<@!?\d+>")
_BANG_RUN  = re.compile(r"!{2,}")
_WS        = re.compile(r"\s+")

# Rough but dependency-free: the emoji planes plus the common symbol blocks.
# Deliberately generous — over-counting costs a personality one emoji, while
# under-counting lets a wall of them through.
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF️❤]")
_CUSTOM_EMOJI = re.compile(r"<a?:\w+:\d+>")


class Incoming:
    """One message, flattened. The only thing the listener passes in."""

    __slots__ = ("guild_id", "channel_id", "user_id", "content", "display_name",
                 "is_bot", "is_command", "mentions_bot", "replies_to_bot",
                 "profile")

    def __init__(self, *, guild_id=None, channel_id=None, user_id=None,
                 content: str = "", display_name: str = "",
                 is_bot: bool = False, is_command: bool = False,
                 mentions_bot: bool = False, replies_to_bot: bool = False,
                 profile: Optional[dict] = None) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.content = content or ""
        self.display_name = display_name or ""
        self.is_bot = bool(is_bot)
        self.is_command = bool(is_command)
        self.mentions_bot = bool(mentions_bot)
        self.replies_to_bot = bool(replies_to_bot)
        self.profile = profile if isinstance(profile, dict) else {}

    @property
    def addressed(self) -> bool:
        return self.mentions_bot or self.replies_to_bot


class Decision:
    """Why the bot is (or is not) about to speak. `reason` exists so a silent
    bot can be diagnosed from a log line instead of by guesswork."""

    __slots__ = ("speak", "moment", "reason")

    def __init__(self, speak: bool, moment: str = "", reason: str = "") -> None:
        self.speak = bool(speak)
        self.moment = moment
        self.reason = reason

    def __repr__(self) -> str:                           # pragma: no cover
        return f"<Decision speak={self.speak} moment={self.moment!r} {self.reason}>"


# Words that read as a shot at the bot. Used only to pick a MOMENT — the reply
# is in character either way, so a false positive costs nothing worse than a
# slightly spikier answer.
_ROAST_HINTS = ("dumb", "stupid", "trash", "useless", "mid", "bad bot",
                "shut up", "cringe", "worst", "broken", "sucks", "L bot")


class ChatEngine:
    """Decides, composes, and filters. Owns no Discord objects."""

    def __init__(self, *, client: Optional[llm.Client] = None,
                 mood: Optional[MEM.Mood] = None,
                 room: Optional[MEM.Room] = None,
                 conversation: Optional[MEM.Conversation] = None,
                 rng=None) -> None:
        # A modest ceiling. Chat is the consumer that yields when the shared
        # free tier runs low — `gemini.Client("boss", priority=True)` is not
        # rationed at all, so a busy chat day can never mute a boss fight.
        self.client = client or llm.Client("chat", hourly_budget=120)
        self.mood = mood or MEM.Mood()
        self.room = room or MEM.Room()
        self.conversation = conversation or MEM.Conversation()
        self.rng = rng or random
        self._channel_gate = CD.Bucket(0.0)     # gap set per call from config
        self._global_gate = CD.Bucket(GLOBAL_GAP)
        self._user_gate = CD.Bucket(REPLY_GAP)

    # ── settings ─────────────────────────────────────────────────────────────
    def personality(self) -> str:
        return P.normalise(C.get(C.K_PERSONALITY))

    def intensity(self) -> str:
        raw = str(C.get(C.K_BANTER) or DEFAULT_INTENSITY).strip().upper()
        return raw if raw in BANTER else DEFAULT_INTENSITY

    def intensity_on(self) -> bool:
        """Is unprompted banter enabled at all?

        The listener's cheap exit: with banter OFF — the shipped default — an
        unaddressed message must cost one cached settings read and nothing
        else, because this runs on every message in the server.
        """
        return BANTER[self.intensity()]["chance"] > 0.0

    def denied_channels(self) -> set:
        raw = C.get(C.K_BANTER_DENY) or []
        out = set()
        for item in raw if isinstance(raw, (list, tuple, set)) else ():
            try:
                out.add(int(item))
            except (TypeError, ValueError):
                continue
        return out

    # ── the decision ─────────────────────────────────────────────────────────
    def decide(self, msg: Incoming, now: Optional[float] = None) -> Decision:
        """Whether to speak, and into which moment. Pure apart from cooldowns."""
        if msg.is_bot:
            return Decision(False, reason="author is a bot")

        # The main-server lock comes before anything else, and unset means
        # locked — see guard.py. A misconfigured install must be silent.
        if not listener_ok(msg.guild_id):
            return Decision(False, reason="not the main server")

        if msg.is_command:
            return Decision(False, reason="message is a command")

        if msg.channel_id in self.denied_channels():
            return Decision(False, reason="channel is on the banter deny list")

        if MEM.opted_out(msg.profile):
            return Decision(False, reason="user opted out")

        # Being spoken to always earns an answer. It is the one path that is on
        # by default, so it must not depend on banter_intensity at all.
        if msg.addressed:
            if not self._user_gate.hit(msg.user_id, now):
                return Decision(False, reason="user reply cooldown")
            low = msg.content.lower()
            moment = ("roast_reply"
                      if any(h in low for h in _ROAST_HINTS) else "mentioned")
            return Decision(True, moment, "addressed directly")

        # Everything below is UNPROMPTED, and every gate is a veto.
        cfg = BANTER[self.intensity()]
        if cfg["chance"] <= 0.0:
            return Decision(False, reason="banter is off")

        state = self.room.state(msg.channel_id, now)
        if state == "quiet":
            # Talking into a dead channel is the failure people notice.
            return Decision(False, reason="room is quiet")

        if self.rng.random() >= cfg["chance"]:
            return Decision(False, reason="dice")

        # Cooldowns last, and only stamped once the roll has already passed —
        # otherwise a losing roll burns the window and the real rate is far
        # below the configured one.
        self._channel_gate.seconds = float(cfg["channel_gap"])
        if not self._channel_gate.hit(msg.channel_id, now):
            return Decision(False, reason="channel cooldown")
        if not self._global_gate.hit("server", now):
            return Decision(False, reason="global cooldown")

        return Decision(True, "busy_room" if state == "busy" else "banter",
                        f"unprompted ({self.intensity().lower()})")

    # ── composing ────────────────────────────────────────────────────────────
    def prompt_for(self, msg: Incoming, moment: str,
                   now: Optional[float] = None) -> str:
        """The instruction handed to the model.

        Everything adaptive lands here: who they are to us, how the room feels,
        what mood we are in. The model never sees raw numbers it could recite —
        `tier` is "a familiar regular", not "rapport 61".
        """
        name = self.personality()
        spec = P.spec(name)
        st = spec["style"]
        who = MEM.recall(msg.profile, display_name=msg.display_name, now=now)

        facts = [f"They are {who['tier']} to you."]
        if who["returning"]:
            facts.append("They have not been around for a few days.")
        if who["favourite"]:
            facts.append(f"They usually battle with {who['favourite']}.")
        if who["matches"] >= 10 and who["win_pct"] is not None:
            facts.append(f"Their record is {who['wins']}-{who['losses']}.")
        if who["level"]:
            facts.append(f"They are trainer level {who['level']}.")

        # Recent back-and-forth with THIS player in THIS channel, so a reply
        # to the bot's own line lands as a continuation rather than as a
        # second cold open. Only addressed moments carry history — unprompted
        # banter has no "conversation" to be part of, and folding it in would
        # let an unrelated aside from ten minutes ago colour a fresh reply.
        history = ""
        if msg.addressed:
            turns = self.conversation.recall(msg.channel_id, msg.user_id, now)
            if turns:
                lines = "\n".join(
                    f"{'Them' if who_said == 'them' else 'You'}: {text[:150]}"
                    for who_said, text in turns)
                history = f"Earlier in this conversation:\n{lines}\n\n"

        return (
            f"You are Beycord, a Beyblade game bot in a Discord server.\n"
            f"Personality: {spec['voice']}.\n"
            f"Right now your mood is {self.mood.label(msg.guild_id, now)} and "
            f"the channel is {self.room.state(msg.channel_id, now)}.\n"
            f"About {msg.display_name or 'this player'}: {' '.join(facts)}\n"
            f"{history}"
            f"They said: {msg.content[:300]}\n\n"
            f"Reply with ONE line, under {st.max_words} words, in character. "
            f"Stay consistent with anything you already said above. "
            f"No quotation marks, no @mentions, no narration, no stage "
            f"directions. Just the line."
        )

    async def compose(self, msg: Incoming, moment: str,
                      now: Optional[float] = None) -> str:
        """The line to send. Always a usable string; never raises.

        Same contract boss dialogue has: the API can be dead, rate-limited,
        unconfigured or absent and the bot still speaks in character. `ask()`
        returning None is the only signal the transport gives.
        """
        name = self.personality()
        text = None
        try:
            text = await self.client.ask_with_deadline(
                self.prompt_for(msg, moment, now),
                max_chars=HARD_CHAR_CAP)
        except Exception:                                # noqa: BLE001
            # ask_with_deadline already swallows everything; this is the belt
            # to its braces, because a raise here would kill the listener.
            log.debug("[chat] generation failed, using an authored line",
                      exc_info=True)

        reply = None
        if text:
            cleaned = self.post_filter(text, name, msg.content)
            if cleaned:
                reply = cleaned

        if reply is None:
            # Authored fallback. Drawn a few times because the filter can
            # legitimately reject a candidate — if the player happened to
            # quote that exact line, `_echoes` fires — and a different draw
            # usually passes.
            for _ in range(4):
                cleaned = self.post_filter(P.canned(name, moment, self.rng),
                                           name, msg.content)
                if cleaned:
                    reply = cleaned
                    break

        if reply is None:
            # Last resort. Deliberately NOT an unfiltered authored line:
            # nothing leaves this method without having been through
            # post_filter, or the filter is only load-bearing on the paths
            # that happen to be tested.
            reply = P.signature(name)

        # Recorded regardless of which path produced the line — a canned
        # fallback is still a real turn in the conversation, and the NEXT
        # message deserves to know what the player was just told even if the
        # model was unreachable when it was said.
        if msg.addressed:
            self.conversation.remember(msg.channel_id, msg.user_id,
                                       said=msg.content, replied=reply, now=now)
        return reply

    # ── the filter ───────────────────────────────────────────────────────────
    def post_filter(self, text: str, name: Optional[str] = None,
                    said: str = "") -> str:
        """Make a candidate line safe and in-budget, or return "" to reject it.

        Applied to authored lines as well as generated ones. Two reasons: the
        pools are only as disciplined as whoever wrote them, and a filter that
        runs on one path is a filter nobody notices has broken.
        """
        if not text:
            return ""
        st = P.style(name)

        # Pings first — this is the one that matters at 3am.
        text = _MASS_PING.sub("everyone", text)
        text = _ROLE_PING.sub("", text)
        text = _ANY_PING.sub("", text)

        text = text.replace("\r", "\n").split("\n")[0]
        text = _WS.sub(" ", text).strip().strip('"').strip()
        if not text:
            return ""

        # Never parrot the user back at themselves. A model asked to reply in
        # character will sometimes quote the message it was given, which reads
        # as mockery from SAVAGE and as a malfunction from everyone else.
        if _echoes(text, said):
            return ""

        words = text.split(" ")
        if len(words) > st.max_words:
            text = " ".join(words[:st.max_words]).rstrip(",;:-") + "…"

        # Emoji budget, custom emoji included.
        text = _trim_matches(text, _CUSTOM_EMOJI, 0)
        text = _trim_matches(text, _EMOJI, st.max_emoji)

        if not st.allow_caps:
            text = _deshout(text)
        if st.max_bangs <= 0:
            text = text.replace("!", ".")
        else:
            text = _BANG_RUN.sub("!" * st.max_bangs, text)

        return text.strip()[:HARD_CHAR_CAP]


# ── filter helpers ───────────────────────────────────────────────────────────

def _trim_matches(text: str, pattern: re.Pattern, keep: int) -> str:
    """Leave at most `keep` matches of `pattern`, dropping the rest."""
    seen = 0

    def _sub(m):
        nonlocal seen
        seen += 1
        return m.group(0) if seen <= keep else ""

    return pattern.sub(_sub, text)


def _deshout(text: str) -> str:
    """Lower any all-caps run of 4+ letters, for personalities that don't shout.

    Short runs are left alone on purpose: `HP`, `XP`, `IQ` and bey names in
    caps are not shouting, and lowering them makes the bot look broken.
    """
    return re.sub(r"\b[A-Z]{4,}\b", lambda m: m.group(0).capitalize(), text)


def _echoes(reply: str, said: str, run: int = 24) -> bool:
    """True when `reply` contains a long verbatim run from `said`."""
    a = _WS.sub(" ", (reply or "").lower()).strip()
    b = _WS.sub(" ", (said or "").lower()).strip()
    if len(b) < run or len(a) < run:
        return False
    return any(b[i:i + run] in a for i in range(len(b) - run + 1))
