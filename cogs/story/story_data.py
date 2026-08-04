"""
story_data.py — the Story Mode campaign.

Pure data plus a few lookup helpers. Deliberately imports nothing from discord
so the engine and the headless simulator (tools/sim_story.py) can load it
without a bot.

A stage is the Story Mode equivalent of an entry in boss_battle.BOSSES, minus
the gimmick machinery: story opponents carry no BossState, which every code
path in boss_ai already handles (`state=None` is the default).

Levels
------
Both sides of a story fight are levelled.

The PLAYER's level system already existed and already applies — the equipped
bey's level goes through bey_levels.stats_at inside loadout.effective_blade,
and trainer level plus blade mastery go through database.get_stat_multiplier.
Story Mode just never showed it.

The OPPONENT's is here. A stage declares a `level` and an archetype (`type`),
and its stats are derived: ARCHETYPE_BASE scaled by the level curve below.
That means difficulty is one number per stage rather than four, and the four
numbers can never drift out of line with the archetype they are supposed to
represent.

Two growth rates, on purpose:

  STAT_GROWTH  attack / defence / stamina. This is where a stage's difficulty
               comes from, and it is gentle — see the note on the constant.
  HP_GROWTH    flatter still. HP sets how LONG a fight is, not how hard, and a
               campaign whose last fight takes four times as many turns as its
               first is just slower, not harder. Keeping HP nearly level means
               every stage lands in the same 20-36 turn window.

Balance notes
-------------
The player's pool is BASE_PLAYER_HP (2000 — see story_engine) scaled by trainer
level and mastery, and opponent HP is quoted against that, not against the
900-1300 a boss uses; a boss also has an ability kit and scripted specials, and
a story opponent has neither.

`difficulty` keys into boss_ai.DIFFICULTY and sets the AI's search depth,
whether it models your habits, and how often it blunders. It is a far coarser
dial than it looks: measured on one fixed statline, the same opponent went from
a 92% player win rate at `rookie` to 37% at `veteran` to near-zero at `elite`.

That is why it changes ONCE, in chapter 1, and never again. A tier step is
worth more than the entire level curve, so a step anywhere the fight is already
close inverts the campaign — the stage after the step needs WEAKER stats than
the stage before it to stay winnable, and the stat block stops being an honest
description of the opponent. Landing the one step at 1-4, where the player wins
comfortably either way, is the only place it costs nothing. `elite`, `legend`
and `nightmare` are deliberately unused: they are the headroom a harder mode
would run on, not spare difficulty to sprinkle through this one.

The numbers below were tuned with tools/sim_story.py; re-run it after editing.
"""

from __future__ import annotations

import re
from typing import Optional

STAGE_ID_RE = re.compile(r"^\d+-\d+$")

# ── Level curve ──────────────────────────────────────────────────────────────

# Derived, not guessed. A fight ends when the OPPONENT dies, so the opponent
# only gets HP_opponent / damage_player turns in which to land its own damage.
# The player is therefore threatened only when
#     HP_opponent x damage_opponent  >=  HP_player x damage_player
# and against the baseline player (2000 HP, ~45 damage a turn) that right-hand
# side is ~90,000. This is why raising an opponent's attack alone barely helps:
# a hard-hitting opponent with a shallow bar simply dies before its damage can
# accumulate. Opponent HP is the term that buys it the turns.
#
# STAT_GROWTH is deliberately gentle. At +3%/level the campaign's usable range
# was about twelve levels wide — measured win rate fell 92% -> 33% -> 2% between
# levels 32, 40 and 48 — so most of the level span was either free or
# unwinnable. A softer curve spreads the same difficulty over the whole 5-48
# range, which is what makes the level number mean something at every stage
# rather than only in the narrow band where it happens to bite.
STAT_GROWTH = 0.0175    # +1.75% per level over 1, on attack / defence / stamina
HP_GROWTH   = 0.006     # +0.6% per level over 1, on HP
MAX_LEVEL   = 100       # matches utils.bey_levels.MAX_LEVEL


def stat_multiplier(level: int) -> float:
    """Attack / defence / stamina scaling for a levelled fighter."""
    return 1.0 + STAT_GROWTH * (max(1, min(MAX_LEVEL, int(level))) - 1)


def hp_multiplier(level: int) -> float:
    """HP scaling — deliberately much flatter than stat_multiplier."""
    return 1.0 + HP_GROWTH * (max(1, min(MAX_LEVEL, int(level))) - 1)


# Opponent stats at level 1, by archetype. `type` also feeds
# boss_ai.type_damage_mult, which gives an Attack-type opponent a 1.40x damage
# multiplier — that is why the attack archetype's ATTACK base is the LOWEST of
# the four. Its damage advantage is already paid for elsewhere, and stacking a
# high attack stat on top made attack stages spike far above their neighbours.
ARCHETYPE_BASE: dict[str, dict[str, int]] = {
    "attack":  {"hp": 980, "attack": 70, "defense": 45, "stamina": 51},
    "defense": {"hp": 1080, "attack": 79, "defense": 60, "stamina": 56},
    "stamina": {"hp": 1020, "attack": 82, "defense": 54, "stamina": 69},
    "balance": {"hp": 1000, "attack": 89, "defense": 51, "stamina": 58},
}

_STAT_KEYS = ("attack", "defense", "stamina")


def opponent_stats(stage: dict) -> dict[str, int]:
    """The stage opponent's actual stats at its level.

    `stat_tweak` is an optional per-stage multiplier dict for the rare case
    where an archetype needs a nudge to fit its slot in the curve — a chapter
    boss that should hit above its level, say. Absent on most stages.
    """
    base = ARCHETYPE_BASE.get(stage.get("type", "balance"),
                              ARCHETYPE_BASE["balance"])
    level = int(stage.get("level", 1))
    tweak = stage.get("stat_tweak") or {}
    out = {"hp": int(round(base["hp"] * hp_multiplier(level)
                           * float(tweak.get("hp", 1.0))))}
    m = stat_multiplier(level)
    for key in _STAT_KEYS:
        out[key] = int(round(base[key] * m * float(tweak.get(key, 1.0))))
    return out


# ── The campaign ─────────────────────────────────────────────────────────────
# Difficulty names must exist in boss_ai.DIFFICULTY:
#   rookie -> veteran -> elite -> legend -> nightmare

CHAPTERS: dict[int, dict] = {
    1: {
        "name":  "Rookie Alley",
        "emoji": "🌀",
        "blurb": "Backstreet bladers and borrowed launchers. Everyone starts here.",
        "stages": [
            {
                "id": "1-1", "name": "Kenta", "emoji": "🟢", "level": 5,
                "difficulty": "rookie", "type": "balance",
                "colour": 0x2ECC71,
                "persona": "twitchy, over-eager, telegraphs everything",
                "blurb": "Attacks on instinct. Punish the heals.",
                "reward": {"coins": 2_000, "xp": 60, "bey_xp": (120, 200)},
                "boss": False,
            },
            {
                "id": "1-2", "name": "Mira", "emoji": "🔵", "level": 10,
                "difficulty": "rookie", "type": "defense",
                "colour": 0x3498DB,
                "persona": "patient, blocks first and asks questions later",
                "blurb": "Blocks a lot — and a block ripostes. Bait it out.",
                "reward": {"coins": 3_000, "xp": 80, "bey_xp": (140, 230)},
                "boss": False,
            },
            {
                "id": "1-3", "name": "Rook", "emoji": "🟠", "level": 15,
                "difficulty": "rookie", "type": "attack",
                "colour": 0xE67E22,
                "persona": "all offence, no patience",
                "blurb": "Hits hard and often. Attack blades hurt more — mind the clash.",
                "reward": {"coins": 4_500, "xp": 100, "bey_xp": (160, 260)},
                "boss": False,
            },
            {
                "id": "1-4", "name": "Alley King Doji", "emoji": "👑", "level": 19,
                "difficulty": "veteran", "type": "balance",
                "colour": 0x9B59B6,
                "persona": "smug, reads your habits, never wastes a turn",
                "blurb": "He watches what you repeat. Stop repeating it.",
                "reward": {"coins": 10_000, "xp": 220, "bey_xp": (300, 450)},
                "boss": True,
            },
        ],
    },
    2: {
        "name":  "The Circuit",
        "emoji": "🏟️",
        "blurb": "Ranked play. The bladers here have actually trained.",
        "stages": [
            {
                "id": "2-1", "name": "Sena", "emoji": "🌊", "level": 22,
                "difficulty": "veteran", "type": "stamina",
                "colour": 0x1ABC9C,
                "persona": "unhurried, wins by outlasting",
                "blurb": "Heals to stay alive. Its heal budget is finite — spend it for them.",
                "reward": {"coins": 12_000, "xp": 240, "bey_xp": (320, 470)},
                "boss": False,
            },
            {
                "id": "2-2", "name": "Garrick", "emoji": "🛡️", "level": 26,
                "difficulty": "veteran", "type": "defense",
                "colour": 0x34495E,
                "persona": "a wall with a grudge",
                "blurb": "High guard. Specials pierce half of it — save your gauge.",
                "reward": {"coins": 15_000, "xp": 270, "bey_xp": (340, 500)},
                "boss": False,
            },
            {
                "id": "2-3", "name": "Vex", "emoji": "⚡", "level": 30,
                "difficulty": "veteran", "type": "attack",
                "colour": 0xF1C40F,
                "persona": "fast, ruthless, searches two moves ahead",
                "blurb": "Rarely blunders. Trades will not go your way.",
                "reward": {"coins": 18_000, "xp": 300, "bey_xp": (360, 520)},
                "boss": False,
            },
            {
                "id": "2-4", "name": "Circuit Champion Ryn", "emoji": "🏆", "level": 34,
                "difficulty": "veteran", "type": "balance",
                "colour": 0xE74C3C,
                "persona": "the complete blader — no weakness to aim at",
                "blurb": "No gap in the kit. Win the gauge race or lose the fight.",
                "reward": {"coins": 30_000, "xp": 450, "bey_xp": (500, 700)},
                "boss": True,
            },
        ],
    },
    3: {
        "name":  "Crown Tournament",
        "emoji": "👑",
        "blurb": "The last four. Bring everything you own.",
        "stages": [
            {
                "id": "3-1", "name": "Ashen Ko", "emoji": "🔥", "level": 38,
                "difficulty": "veteran", "type": "attack",
                "colour": 0xD35400,
                "persona": "burns the fight down before it can be planned",
                "blurb": "Opens fast. Survive the first ten turns and it evens out.",
                "reward": {"coins": 34_000, "xp": 480, "bey_xp": (520, 720)},
                "boss": False,
            },
            {
                "id": "3-2", "name": "Sister Ilva", "emoji": "🕯️", "level": 42,
                "difficulty": "veteran", "type": "stamina",
                "colour": 0x8E44AD,
                "persona": "calm, exact, refuses to be rushed",
                "blurb": "Blocks and heals in the right order. Force the tempo.",
                "reward": {"coins": 38_000, "xp": 520, "bey_xp": (540, 750)},
                "boss": False,
            },
            {
                "id": "3-3", "name": "Warden Kael", "emoji": "⛓️", "level": 45,
                "difficulty": "veteran", "type": "defense",
                "colour": 0x2C3E50,
                "persona": "almost never wrong",
                "blurb": "Blunders 4% of the time. That is your whole opening.",
                "reward": {"coins": 45_000, "xp": 600, "bey_xp": (600, 820)},
                "boss": False,
            },
            {
                "id": "3-4", "name": "Crown Sovereign Astra", "emoji": "🌟", "level": 48,
                "difficulty": "veteran", "type": "balance",
                "colour": 0xFFD700,
                "persona": "the reason the crown exists",
                "blurb": "The end of the road. An avatar is not optional here.",
                "reward": {"coins": 75_000, "xp": 900, "bey_xp": (800, 1100)},
                "boss": True,
            },
        ],
    },
}


# ── Lookups ──────────────────────────────────────────────────────────────────
# Built once at import. Order matters: `_ORDER` defines the unlock chain, so a
# stage's prerequisite is simply the entry before it.

def _build() -> tuple[dict[str, dict], list[str]]:
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for cnum in sorted(CHAPTERS):
        chapter = CHAPTERS[cnum]
        for i, st in enumerate(chapter["stages"], start=1):
            st = dict(st)
            st["chapter"] = cnum
            st["index"] = i
            st["chapter_name"] = chapter["name"]
            # Resolved once so every consumer — the engine, the info card, the
            # simulator — reads the same numbers.
            st["stats"] = opponent_stats(st)
            by_id[st["id"]] = st
            order.append(st["id"])
    return by_id, order


_BY_ID, _ORDER = _build()


def all_stages() -> list[dict]:
    """Every stage, in campaign order."""
    return [_BY_ID[sid] for sid in _ORDER]


def stage(stage_id: str) -> Optional[dict]:
    """Look a stage up by id (`1-2`) — never raises."""
    return _BY_ID.get(str(stage_id or "").strip().lower())


def resolve(query: str) -> Optional[dict]:
    """Find a stage by id, exact name, or unambiguous partial name.

    Mirrors avatar_shop._resolve_avatar_query so `;story 2-1`, `;story sena`
    and `;story circuit champion` all work. Ambiguous partials return None so
    the caller can offer the matches.
    """
    q = str(query or "").strip().lower()
    if not q:
        return None
    if q in _BY_ID:
        return _BY_ID[q]
    for st in all_stages():
        if st["name"].lower() == q:
            return st
    hits = [st for st in all_stages() if q in st["name"].lower()]
    return hits[0] if len(hits) == 1 else None


def matches(query: str) -> list[dict]:
    """Every partial-name match — for disambiguating a failed resolve()."""
    q = str(query or "").strip().lower()
    if not q:
        return []
    return [st for st in all_stages() if q in st["name"].lower()]


def prereq_of(stage_id: str) -> Optional[str]:
    """The stage that must be cleared first, or None for the opener."""
    try:
        i = _ORDER.index(str(stage_id).strip().lower())
    except ValueError:
        return None
    return _ORDER[i - 1] if i > 0 else None


def next_stage(stage_id: str) -> Optional[str]:
    """The stage unlocked by clearing this one, or None at the end."""
    try:
        i = _ORDER.index(str(stage_id).strip().lower())
    except ValueError:
        return None
    return _ORDER[i + 1] if i + 1 < len(_ORDER) else None


def chapter_of(stage_id: str) -> Optional[dict]:
    st = stage(stage_id)
    return CHAPTERS.get(st["chapter"]) if st else None


def chapter_stages(chapter_num: int) -> list[dict]:
    ch = CHAPTERS.get(int(chapter_num))
    if not ch:
        return []
    return [_BY_ID[s["id"]] for s in ch["stages"]]


def is_unlocked(stage_id: str, cleared: set[str] | list[str] | None) -> bool:
    """A stage is open when its predecessor has been cleared."""
    need = prereq_of(stage_id)
    if need is None:
        return True
    return need in set(cleared or ())


def first_uncleared(cleared: set[str] | list[str] | None) -> Optional[str]:
    """The stage the player should play next — the campaign's 'continue'."""
    done = set(cleared or ())
    for sid in _ORDER:
        if sid not in done:
            return sid
    return None


def total_stages() -> int:
    return len(_ORDER)
