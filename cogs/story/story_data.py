"""
story_data.py — the Story Mode campaign.

Pure data plus a few lookup helpers. Deliberately imports nothing from discord
so the engine and the headless simulator (tools/sim_story.py) can load it
without a bot.

A stage is the Story Mode equivalent of an entry in boss_battle.BOSSES, minus
the gimmick machinery: story opponents carry no BossState, which every code
path in boss_ai already handles (`state=None` is the default).

Balance notes
-------------
The player's HP pool is BASE_PLAYER_HP (4000) scaled by trainer level and
mastery, so opponent HP is quoted against that, not against the 900-1300 a
boss uses — a boss also has an ability kit and scripted specials, and a story
opponent has neither. `difficulty` keys straight into boss_ai.DIFFICULTY and is
the real difficulty dial: it sets the AI's search depth, whether it models your
habits, how often it blunders and how much it mixes.

The numbers below were tuned with tools/sim_story.py; re-run it after editing.
"""

from __future__ import annotations

import re
from typing import Optional

STAGE_ID_RE = re.compile(r"^\d+-\d+$")

# Difficulty names must exist in boss_ai.DIFFICULTY.
#   rookie -> veteran -> elite -> legend -> nightmare
CHAPTERS: dict[int, dict] = {
    1: {
        "name":  "Rookie Alley",
        "emoji": "🌀",
        "blurb": "Backstreet bladers and borrowed launchers. Everyone starts here.",
        "stages": [
            {
                "id": "1-1", "name": "Kenta", "emoji": "🟢",
                "difficulty": "rookie", "type": "balance",
                "hp": 700, "attack": 58, "defense": 52, "stamina": 58,
                "colour": 0x2ECC71,
                "persona": "twitchy, over-eager, telegraphs everything",
                "blurb": "Attacks on instinct. Punish the heals.",
                "reward": {"coins": 2_000, "xp": 60, "bey_xp": (120, 200)},
                "boss": False,
            },
            {
                "id": "1-2", "name": "Mira", "emoji": "🔵",
                "difficulty": "rookie", "type": "defense",
                "hp": 820, "attack": 68, "defense": 72, "stamina": 64,
                "colour": 0x3498DB,
                "persona": "patient, blocks first and asks questions later",
                "blurb": "Blocks a lot — and a block ripostes. Bait it out.",
                "reward": {"coins": 3_000, "xp": 80, "bey_xp": (140, 230)},
                "boss": False,
            },
            {
                "id": "1-3", "name": "Rook", "emoji": "🟠",
                "difficulty": "rookie", "type": "attack",
                "hp": 800, "attack": 80, "defense": 58, "stamina": 60,
                "colour": 0xE67E22,
                "persona": "all offence, no patience",
                "blurb": "Hits hard and often. Attack blades hurt more — mind the clash.",
                "reward": {"coins": 4_500, "xp": 100, "bey_xp": (160, 260)},
                "boss": False,
            },
            {
                "id": "1-4", "name": "Alley King Doji", "emoji": "👑",
                "difficulty": "veteran", "type": "balance",
                "hp": 980, "attack": 118, "defense": 86, "stamina": 80,
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
                "id": "2-1", "name": "Sena", "emoji": "🌊",
                "difficulty": "veteran", "type": "stamina",
                "hp": 1020, "attack": 124, "defense": 88, "stamina": 110,
                "colour": 0x1ABC9C,
                "persona": "unhurried, wins by outlasting",
                "blurb": "Heals to stay alive. Its heal budget is finite — spend it for them.",
                "reward": {"coins": 12_000, "xp": 240, "bey_xp": (320, 470)},
                "boss": False,
            },
            {
                "id": "2-2", "name": "Garrick", "emoji": "🛡️",
                "difficulty": "veteran", "type": "defense",
                "hp": 1080, "attack": 113, "defense": 104, "stamina": 90,
                "colour": 0x34495E,
                "persona": "a wall with a grudge",
                "blurb": "High guard. Specials pierce half of it — save your gauge.",
                "reward": {"coins": 15_000, "xp": 270, "bey_xp": (340, 500)},
                "boss": False,
            },
            {
                "id": "2-3", "name": "Vex", "emoji": "⚡",
                "difficulty": "elite", "type": "attack",
                "hp": 870, "attack": 102, "defense": 85, "stamina": 84,
                "colour": 0xF1C40F,
                "persona": "fast, ruthless, searches two moves ahead",
                "blurb": "Rarely blunders. Trades will not go your way.",
                "reward": {"coins": 18_000, "xp": 300, "bey_xp": (360, 520)},
                "boss": False,
            },
            {
                "id": "2-4", "name": "Circuit Champion Ryn", "emoji": "🏆",
                "difficulty": "elite", "type": "balance",
                "hp": 940, "attack": 130, "defense": 100, "stamina": 100,
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
                "id": "3-1", "name": "Ashen Ko", "emoji": "🔥",
                "difficulty": "elite", "type": "attack",
                "hp": 900, "attack": 98, "defense": 92, "stamina": 90,
                "colour": 0xD35400,
                "persona": "burns the fight down before it can be planned",
                "blurb": "Opens fast. Survive the first ten turns and it evens out.",
                "reward": {"coins": 34_000, "xp": 480, "bey_xp": (520, 720)},
                "boss": False,
            },
            {
                "id": "3-2", "name": "Sister Ilva", "emoji": "🕯️",
                "difficulty": "elite", "type": "stamina",
                "hp": 1000, "attack": 122, "defense": 110, "stamina": 120,
                "colour": 0x8E44AD,
                "persona": "calm, exact, refuses to be rushed",
                "blurb": "Blocks and heals in the right order. Force the tempo.",
                "reward": {"coins": 38_000, "xp": 520, "bey_xp": (540, 750)},
                "boss": False,
            },
            {
                "id": "3-3", "name": "Warden Kael", "emoji": "⛓️",
                "difficulty": "legend", "type": "defense",
                "hp": 900, "attack": 124, "defense": 118, "stamina": 106,
                "colour": 0x2C3E50,
                "persona": "almost never wrong",
                "blurb": "Blunders 4% of the time. That is your whole opening.",
                "reward": {"coins": 45_000, "xp": 600, "bey_xp": (600, 820)},
                "boss": False,
            },
            {
                "id": "3-4", "name": "Crown Sovereign Astra", "emoji": "🌟",
                "difficulty": "legend", "type": "balance",
                "hp": 900, "attack": 132, "defense": 122, "stamina": 120,
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
