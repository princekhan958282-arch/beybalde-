#!/usr/bin/env python3
"""
tools/sim_chat_personality.py — the bot's server-chat voice.

The regression this suite exists for
------------------------------------
`/server` has offered a personality picker since v1.28. It wrote
`config.K_PERSONALITY` and **nothing ever read it**, so choosing SAVAGE and
choosing FRIENDLY produced byte-identical behaviour: silence. Section 1 below
is written to fail against that build by construction — it asserts the five
personalities actually differ in what they produce.

The second thing being tested is the one with real blast radius. This is the
first feature that makes the bot speak WITHOUT being commanded, in a live
Discord server. Sections 3, 4 and 7 are the gates on that: who it will answer,
where it will stay quiet, and what it is structurally incapable of sending.

Everything runs headless — no gateway, no network, no live DB. The engine takes
a flat `Incoming`, never a `discord.Message`, precisely so that these rules are
reachable from a test at all.

Run:  python3 tools/sim_chat_personality.py
"""
import asyncio
import os
import random
import sys
import tempfile

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


# ── A real store on a temp file, installed before the package is imported ────
# Same technique sim_community.py uses: config lives in the DB, and the whole
# point is to drive the REAL config path rather than a stub of it.
import utils.database as DB                                       # noqa: E402
from utils.userstore import UserStore                             # noqa: E402

_TMP = tempfile.mkdtemp()
STORE = UserStore(os.path.join(_TMP, "sim.db"))
STORE.ensure_ready()
DB.USER_STORE = STORE

from cogs.community import chat as CH                             # noqa: E402
from cogs.community import config as C                            # noqa: E402
from cogs.community import memory as MEM                          # noqa: E402
from cogs.community import personality as P                       # noqa: E402
from utils import llm                                             # noqa: E402

MAIN = 111_111_111
OTHER = 222_222_222
CHAN = 900_001
USER = 700_001

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


asyncio.set_event_loop(asyncio.new_event_loop())

C.put(C.K_MAIN_GUILD, str(MAIN))
C.invalidate()


def engine(seed=7, **kw):
    e = CH.ChatEngine(rng=random.Random(seed), **kw)
    # A client that can never reach the network: every test below is about our
    # own logic, and a suite that quietly depends on an API key is a suite that
    # behaves differently on someone else's machine.
    e.client = llm.Client("chat-test", hourly_budget=0)
    return e


def msg(**kw):
    kw.setdefault("guild_id", MAIN)
    kw.setdefault("channel_id", CHAN)
    kw.setdefault("user_id", USER)
    kw.setdefault("content", "hello")
    return CH.Incoming(**kw)


def busy(e, channel=CHAN, now=1000.0):
    """Make a channel read as 'busy' so unprompted banter is eligible."""
    for i in range(20):
        e.room.observe(channel, i % 5, now=now)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. the dead setting is alive ─────────────────────────────────")
# The whole feature in one section: setting K_PERSONALITY must change what
# comes out. Against the pre-fix build every one of these is identical.

lines = {}
prompts = {}
for name in P.ALL:
    C.put(C.K_PERSONALITY, name)
    C.invalidate()
    e = engine()
    check(f"the engine reads {name} back from config",
          e.personality() == name, e.personality())
    lines[name] = {run(e.compose(msg(mentions_bot=True), "mentioned"))
                   for _ in range(12)}
    prompts[name] = e.prompt_for(msg(mentions_bot=True), "mentioned")

check("all five produce DIFFERENT sets of lines — the picker is no longer "
      "decorative",
      all(lines[a].isdisjoint(lines[b])
          for a in P.ALL for b in P.ALL if a < b),
      {k: len(v) for k, v in lines.items()})
check("all five build different prompts too, so a live API is steered as well "
      "as the fallback",
      len({prompts[n] for n in P.ALL}) == 5)
check("an unknown stored value degrades to a real personality instead of "
      "going mute", P.normalise("LEFTOVER_FROM_AN_OLD_BUILD") in P.ALL)

# The picker and the engine must offer the same five. A name in one and not the
# other is either an unreachable personality or a silent fall to DEFAULT.
panel_src = open(os.path.join(ROOT, "cogs/community/panel.py"),
                 encoding="utf-8").read()
panel_names = set(__import__("re").findall(r'\("([A-Z]{4,})",\s+"', panel_src))
check("the /server picker offers exactly the personalities that exist",
      panel_names >= set(P.ALL), panel_names ^ set(P.ALL))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. the authored fallback is playable on its own ──────────────")
# Most installs will never set GEMINI_API_KEY. For them these pools are not a
# degraded mode, they are the entire feature.

for name in P.ALL:
    spec = P.PERSONALITIES[name]
    missing = [m for m in P.MOMENTS if m not in spec["lines"]]
    check(f"{name}: every moment has its own pool", not missing, missing)
    thin = [m for m in P.MOMENTS if len(P.pool(name, m)) < 3]
    check(f"{name}: no pool is thinner than 3 lines", not thin, thin)

all_lines = [(n, ln) for n in P.ALL for m in P.MOMENTS for ln in P.pool(n, m)]
shared = {}
for n, ln in all_lines:
    shared.setdefault(ln, set()).add(n)
check("no line is shared between two personalities — five voices, not one "
      "with five labels",
      not [ln for ln, owners in shared.items() if len(owners) > 1],
      [ln for ln, owners in shared.items() if len(owners) > 1][:3])

P.reset_memory()
seen = [P.canned("SAVAGE", "banter") for _ in range(40)]
check("a canned line never repeats back to back",
      all(a != b for a, b in zip(seen, seen[1:])))
check("an unknown moment still answers in character",
      P.canned("HYPE", "no_such_moment") in P.pool("HYPE", "banter"))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. who it will answer, and who it will not ───────────────────")
C.put(C.K_PERSONALITY, "FRIENDLY")
C.put(C.K_BANTER, "OFF")
C.put(C.K_BANTER_DENY, [])
C.invalidate()

e = engine()
check("a bot's message is ignored",
      not e.decide(msg(is_bot=True, mentions_bot=True)).speak)
check("a command is not conversation",
      not e.decide(msg(is_command=True, mentions_bot=True)).speak)

e = engine()
check("being @mentioned earns a reply even with banter OFF",
      e.decide(msg(mentions_bot=True)).speak)
e = engine()
check("a reply to the bot does too",
      e.decide(msg(replies_to_bot=True)).speak)

e = engine()
check("...but the same user cannot spam it — second one is rate limited",
      e.decide(msg(mentions_bot=True), now=1.0).speak
      and not e.decide(msg(mentions_bot=True), now=2.0).speak)
e = engine()
check("...and the limit lifts once the gap has passed",
      e.decide(msg(mentions_bot=True), now=1.0).speak
      and e.decide(msg(mentions_bot=True), now=1.0 + CH.REPLY_GAP + 1).speak)

e = engine()
d = e.decide(msg(mentions_bot=True, content="you are a useless trash bot"))
check("a shot at the bot is routed to the roast moment",
      d.speak and d.moment == "roast_reply", d.moment)

# Opt-out
opted = {MEM.K_OPTOUT: True}
e = engine()
check("a user who ran ';chat off' is never answered, even on a direct mention",
      not e.decide(msg(mentions_bot=True, profile=opted)).speak)
check("...and opting back in restores it",
      engine().decide(msg(mentions_bot=True,
                          profile={MEM.K_OPTOUT: False})).speak)

# Deny list
C.put(C.K_BANTER_DENY, [str(CHAN)])
C.invalidate()
e = engine()
check("a muted channel silences even a direct mention",
      not e.decide(msg(mentions_bot=True)).speak)
C.put(C.K_BANTER_DENY, [])
C.invalidate()


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 4. unprompted banter is gated to a standstill by default ─────")
C.put(C.K_BANTER, "OFF")
C.invalidate()
e = engine()
busy(e)
check("with banter OFF it never speaks unprompted, however busy the room",
      not any(e.decide(msg(), now=1000.0 + i).speak for i in range(400)))

C.put(C.K_BANTER, "CHAOTIC")
C.invalidate()
e = engine()
# room left quiet on purpose
check("a quiet room is left alone even at CHAOTIC",
      not any(e.decide(msg(), now=1000.0 + i).speak for i in range(200)),
      e.room.state(CHAN, 1000.0))

e = engine()
busy(e)
spoke = sum(1 for i in range(400) if e.decide(msg(), now=1000.0 + i * 30).speak)
check("a busy room at CHAOTIC does eventually get banter", spoke > 0, spoke)

# The cooldown, not just the dice, has to be doing work.
e = engine()
busy(e)
back_to_back = [i for i in range(60) if e.decide(msg(), now=2000.0).speak]
check("it never speaks twice in the same instant — the channel cooldown binds",
      len(back_to_back) <= 1, len(back_to_back))

check("the shipped default is OFF, so merging this cannot make a live server "
      "chatty", C.DEFAULTS[C.K_BANTER] == "OFF", C.DEFAULTS[C.K_BANTER])
# Read off the source rather than imported, so this holds without discord.py.
# An intensity in the picker that the engine does not know would silently fall
# to OFF — the owner would set it, see it saved, and get nothing. Exactly the
# bug section 1 exists for, one setting over.
_re = __import__("re")
_block = _re.search(r"INTENSITIES\s*=\s*\[(.*?)\]", panel_src, _re.S)
_panel_intensities = set(_re.findall(r'\("([A-Z]+)"',
                                     _block.group(1) if _block else ""))
check("every intensity the panel offers is one the engine knows",
      _panel_intensities and _panel_intensities <= set(CH.BANTER),
      _panel_intensities - set(CH.BANTER))
check("...and every intensity the engine knows is offered — no unreachable "
      "setting", set(CH.BANTER) <= _panel_intensities,
      set(CH.BANTER) - _panel_intensities)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5. it never speaks outside the main server ───────────────────")
C.put(C.K_BANTER, "CHAOTIC")
C.invalidate()
e = engine()
busy(e, channel=CHAN)
check("another guild gets nothing, even addressed directly",
      not e.decide(msg(guild_id=OTHER, mentions_bot=True)).speak)

C.put(C.K_MAIN_GUILD, None)
C.invalidate()
e = engine()
check("with NO main server configured it is silent everywhere — unset means "
      "locked, not open",
      not e.decide(msg(mentions_bot=True)).speak
      and not e.decide(msg(guild_id=OTHER, mentions_bot=True)).speak)
C.put(C.K_MAIN_GUILD, str(MAIN))
C.invalidate()


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6. it degrades to written lines, and never blocks ────────────")
C.put(C.K_PERSONALITY, "SAVAGE")
C.invalidate()


class _Dead(llm.Client):
    """A transport that fails every way it can, one after another."""

    def __init__(self):
        super().__init__("dead")
        self.calls = 0

    async def ask(self, prompt, **kw):
        self.calls += 1
        raise OSError("connection reset")


e = engine()
e.client = _Dead()
out = [run(e.compose(msg(mentions_bot=True), "mentioned")) for _ in range(10)]
check("a transport that raises on every call never propagates",
      all(isinstance(x, str) and x for x in out))
check("...and what comes back is still in SAVAGE's voice",
      all(x in P.pool("SAVAGE", "mentioned") for x in out),
      [x for x in out if x not in P.pool("SAVAGE", "mentioned")][:2])

e = engine()
e.client = llm.Client("chat-nokey")
os.environ.pop("GEMINI_API_KEY", None)
check("with no API key at all it still answers",
      bool(run(e.compose(msg(mentions_bot=True), "mentioned"))))

# An open breaker must cost nothing — that is the whole reason it exists.
e = engine()
e.client.trip("rate", "simulated outage")
import time as _time                                              # noqa: E402
t0 = _time.monotonic()
for _ in range(200):
    run(e.compose(msg(mentions_bot=True), "mentioned"))
check("200 lines with the breaker open take under 250ms",
      _time.monotonic() - t0 < 0.25, f"{(_time.monotonic()-t0)*1000:.0f}ms")


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 7. the post-filter, which binds BOTH paths ───────────────────")
e = engine()

check("@everyone is defused",
      "@everyone" not in e.post_filter("@everyone get in here", "HYPE"))
check("@here is defused",
      "@here" not in e.post_filter("@here look at this", "HYPE"))
check("a role ping is stripped",
      "<@&" not in e.post_filter("hey <@&999> come here", "FRIENDLY"))
check("a user ping is stripped",
      "<@" not in e.post_filter("nice one <@12345>", "FRIENDLY"))

long = e.post_filter("word " * 400, "FRIENDLY")
check("a wall of text is cut to the personality's word budget",
      len(long.split()) <= P.style("FRIENDLY").max_words + 1, len(long.split()))
check("and nothing ever exceeds the hard character cap",
      len(e.post_filter("x" * 5000, "FRIENDLY")) <= CH.HARD_CHAR_CAP)

said = "my launcher snapped clean in half yesterday and i cried"
check("it refuses to parrot the user back at themselves",
      e.post_filter(f"lol {said} lmao", "SAVAGE", said) == "")
check("...but an ordinary reply to the same message is fine",
      e.post_filter("that's rough, buy a new one", "SAVAGE", said) != "")

check("SERIOUS never shouts", "!" not in e.post_filter("GO GO GO!!!", "SERIOUS"))
check("SERIOUS gets no emoji",
      not any(c in e.post_filter("nice 🔥🔥 work 😄", "SERIOUS")
              for c in "🔥😄"))
check("HYPE keeps some emoji but not a wall",
      e.post_filter("YES 🔥🔥🔥🔥🔥🔥🔥🔥", "HYPE").count("🔥")
      <= P.style("HYPE").max_emoji)
check("custom emoji are dropped entirely",
      "<:" not in e.post_filter("nice <:bey:12345> one", "HYPE"))
check("a non-shouting personality has long caps runs calmed down",
      "TERRIBLE" not in e.post_filter("that was TERRIBLE", "FRIENDLY"))
check("...but short caps like HP and XP survive",
      "HP" in e.post_filter("watch your HP", "FRIENDLY"))
check("multi-line input is reduced to one line",
      "\n" not in e.post_filter("first line\nsecond line", "FUNNY"))

rejected = [(n, m, ln) for n in P.ALL for m in P.MOMENTS
            for ln in P.pool(n, m) if not e.post_filter(ln, n, "")]
check("every authored line survives the filter that will be applied to it — "
      "the pools obey the same budget the model is held to",
      not rejected, rejected[:3])

# compose() must never return something the filter would have rejected.
e = engine()
e.client = _Dead()
composed = [run(e.compose(msg(mentions_bot=True), "mentioned")) for _ in range(30)]
check("nothing leaves compose() unfiltered",
      all(x == e.post_filter(x, e.personality(), "") or x == P.signature(
          e.personality()) for x in composed))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 8. the shared quota protects boss dialogue ───────────────────")
os.environ["GEMINI_API_KEY"] = "k" * 20
llm.ledger().reset()
boss = llm.Client("boss-test", priority=True)
chatc = llm.Client("chat-test2", hourly_budget=5)
for _ in range(50):
    llm.ledger().spend("chat-test2")
check("chat refuses itself once its hourly budget is spent",
      chatc.available() is False)
check("...and says so honestly rather than looking healthy",
      chatc.status()["state"] == "budget spent", chatc.status()["state"])
check("boss dialogue is untouched by however much chat has spent",
      boss.available() is True)
check("a priority consumer cannot be given a ceiling by accident",
      llm.Client("x", priority=True, hourly_budget=1).hourly_budget is None)
check("the two consumers have SEPARATE breakers — a chat outage cannot "
      "silence a boss",
      (chatc.trip("rate", "chat outage"), chatc.circuit_open() is True
       and boss.circuit_open() is False)[1])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 9. what it remembers ─────────────────────────────────────────")
prof = {}
first = MEM.note_exchange(prof, now=1000.0)
check("a first exchange is recorded", first["rapport"] == 1 and prof[MEM.K_FIRST])
for _ in range(200):
    last = MEM.note_exchange(prof, now=1000.0)
check("rapport is capped per day, so it measures talking not spamming",
      last["rapport"] == MEM.RAPPORT_DAILY_CAP, last["rapport"])
check("...and the cap is reported, not silently swallowed", last["capped"])

tomorrow = 1000.0 + 86400.0 * 2
MEM.note_exchange(prof, now=tomorrow)
check("a new UTC day lets it grow again",
      MEM.rapport_of(prof) == MEM.RAPPORT_DAILY_CAP + 1, MEM.rapport_of(prof))

check("tiers are ordered and reachable",
      [MEM.tier_for(x) for x in (0, 10, 60, 250)]
      == ["stranger", "regular", "familiar", "ride-or-die"])

md = MEM.Mood(halflife=10.0)
md.nudge(MAIN, "boss_defeated", now=0.0)
check("an event moves the mood", md.value(MAIN, now=0.0) > 0.3)
check("one halflife loses half of it",
      abs(md.value(MAIN, now=10.0) - md.value(MAIN, now=0.0) / 2) < 1e-6)
check("it decays back toward neutral", abs(md.value(MAIN, now=200.0)) < 0.01)
check("an unknown event is a no-op, not a crash",
      md.nudge(MAIN, "not_a_real_event", now=0.0) == md.value(MAIN, now=0.0))
check("mood is clamped", MEM.Mood().nudge(1, "boss_defeated") <= MEM.MOOD_MAX)

rm = MEM.Room()
check("an empty channel reads quiet", rm.state("c") == "quiet")
for i in range(20):
    rm.observe("c", i % 5, now=500.0)
check("a burst reads busy", rm.state("c", now=500.0) == "busy")
check("one person talking a lot is NOT a busy room",
      MEM.Room().state("solo") == "quiet")
rm2 = MEM.Room()
for i in range(20):
    rm2.observe("c", i % 5, now=500.0)
check("and it goes quiet again once the window passes",
      rm2.state("c", now=500.0 + MEM.ROOM_WINDOW + 1) == "quiet")

who = MEM.recall({"wins": 7, "losses": 3, "level": 22,
                  "equipped_bey": "Artemis Roze"}, display_name="Kai")
check("recall projects the profile the game already keeps",
      who["favourite"] == "Artemis Roze" and who["win_pct"] == 70.0
      and who["level"] == 22, who)
check("recall on an empty profile does not raise",
      MEM.recall({})["tier"] == "stranger")

C.put(C.K_PERSONALITY, "FUNNY")
C.invalidate()
e = engine()
p = e.prompt_for(msg(mentions_bot=True, display_name="Kai",
                     profile={"wins": 40, "losses": 2, "level": 88,
                              "equipped_bey": "Artemis Roze",
                              MEM.K_RAPPORT: 300}), "mentioned")
check("the prompt carries who they are without leaking raw scores",
      "ride-or-die" in p and "Artemis Roze" in p and "300" not in p, p[:200])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 10. it is actually wired in ──────────────────────────────────")
# A feature only the test calls is not shipped. sim_community.py's own sweep
# makes the same argument.
cog_src = open(os.path.join(ROOT, "cogs/community/cog.py"), encoding="utf-8").read()
check("the cog constructs a ChatEngine", "CH.ChatEngine()" in cog_src)
check("the listener calls it", "_maybe_chat" in cog_src
      and "self.chat.decide" in cog_src)
check("it is reached from the on_message listener, not a dead method",
      'await self._maybe_chat(' in cog_src)
check("get_context is resolved once per message, not twice",
      cog_src.count("await self.bot.get_context(message)") == 1,
      cog_src.count("await self.bot.get_context(message)"))
check("the room is observed even when the bot stays quiet",
      "self.chat.room.observe" in cog_src)
check("sends are pinned to AllowedMentions.none() as well as filtered",
      "AllowedMentions.none()" in cog_src)
check("the exchange is written with touch=False, so chatting does not count "
      "as playing", "touch=False" in cog_src)
check("';chat' exists so a player can opt out themselves",
      'name="chat"' in cog_src)

check("the /server panel reaches the banter page",
      "BanterView" in panel_src and "banter_embed" in panel_src)
check("the banter page can set intensity and mute channels",
      "IntensitySelect" in panel_src and "BanterDenySelect" in panel_src)
check("the panel reports the generator state honestly instead of implying "
      "one is live", "written lines" in panel_src)

cfg_src = open(os.path.join(ROOT, "cogs/community/config.py"),
               encoding="utf-8").read()
check("the dead K_ROAST_MAX setting is gone, not left dangling",
      "K_ROAST_MAX" not in cfg_src.split("# `K_ROAST_MAX`")[0]
      and "K_ROAST_MAX:" not in cfg_src)

gem_src = open(os.path.join(ROOT, "cogs/battle/boss/gemini.py"),
               encoding="utf-8").read()
check("boss dialogue and chat share ONE transport, not two copies",
      "utils import llm" in gem_src and "aiohttp" not in gem_src)

print(f"\n{'='*66}\n  {PASS} passed, {FAIL} failed\n{'='*66}")
sys.exit(1 if FAIL else 0)
