"""
utils/ranked.py — the ranked ladder: what counts, what doesn't, and who's on it.

Pure logic. No discord imports, so the whole thing is testable headlessly and a
balance or eligibility change is a one-line edit with a simulator to check it.

── Ranked vs normal ─────────────────────────────────────────────────────────

The ladder only counts RANKED battles. A normal `;battle` still pays coins,
trainer XP and bey XP exactly as before — it simply does not touch rank score,
ranked W/L, or the win streak, and never appears on a leaderboard.

That split is the whole point. A ladder that counts friendly matches is not a
ladder: two players can trade wins to farm rank score, and a win rate that
includes practice games measures nothing. Keeping the casual rewards intact
means nobody is punished for playing casually.

The legacy `wins` / `losses` keys are deliberately left alone and still count
every battle. They feed the profile card, achievements and `;audit`, none of
which are competitive, and repurposing them would have silently rewritten every
one of those surfaces. Ranked play writes to its own keys.

── Verification ─────────────────────────────────────────────────────────────

Ranked play is open to everyone. There was a verification gate — join a
configured server, run `;verify`, or be excluded from the ladder and the
boards — and it was removed in v1.18: it defaulted to off, no install ever
turned it on, and it cost a command, a profile key, a filter inside
`build_board`, three admin actions and half the settings screen to keep
switched off.
"""

from __future__ import annotations

from typing import Optional

# ── Profile keys ─────────────────────────────────────────────────────────────
# Ranked play writes only to these. Nothing else in the bot uses them, so the
# ladder can be reset without touching a single non-competitive number.
K_RANKED_WINS = "ranked_wins"
K_RANKED_LOSSES = "ranked_losses"
K_RANK_SCORE = "rank_score"          # pre-existing; ranked play now owns it
K_BEST_STREAK = "best_streak"
K_WIN_STREAK = "win_streak"
K_CAUGHT = "beys_caught"
# Written by the retired `;verify` and still present on live profiles. Nothing
# reads it as of v1.18; the name is kept so the key is documented rather than
# turning up later as an unexplained field in 3,400 rows.
K_VERIFIED = "ranked_verified"

# Every key a leaderboard reset is allowed to clear. Kept as an explicit list so
# a reset can never wander into coins, inventory or trainer level — the failure
# mode there is unrecoverable and silent.
K_COMMUNITY_XP    = "community_xp"
K_COMMUNITY_LEVEL = "com_level"

RESETTABLE = {
    "rank":     (K_RANK_SCORE, K_RANKED_WINS, K_RANKED_LOSSES),
    "winrate":  (K_RANKED_WINS, K_RANKED_LOSSES),
    "wins":     (K_RANKED_WINS, K_RANKED_LOSSES),
    "streak":   (K_BEST_STREAK, K_WIN_STREAK),
    "catches":  (K_CAUGHT,),
    # Community XP and its level ARE resettable — a community season can start
    # over, and unlike the two below, nothing was bought with them. Both keys
    # go together: leaving the level behind would show a level 20 next to zero
    # XP. They were missing here at first, so `rank_reset all` quietly left
    # both boards standing while the admin screen listed them as boards.
    "chatxp":   (K_COMMUNITY_XP, K_COMMUNITY_LEVEL),
    "commlevel": (K_COMMUNITY_XP, K_COMMUNITY_LEVEL),
    # `level` and `money` are boards but not resettable: their keys are the
    # player's progression and wallet, and "reset a leaderboard" must never be
    # a route to wiping either for every profile in the store.
}

# A win rate needs a floor or the board is topped forever by whoever went 1-0
# and stopped playing. Ten games is enough that one lucky run cannot hold first
# place, and low enough to be reachable in an evening.
MIN_RANKED_GAMES = 10

# ── Match format ─────────────────────────────────────────────────────────────
#
# A ranked MATCH is a series of rounds, not one fight. Each round ends in one of
# three finishes, worth different points, and the first to MATCH_TARGET wins the
# match:
#
#   burst     the opponent's HP reached 0          2 points
#   survival  the opponent ran out of stamina      1 point
#   ringout   the opponent's stability reached 0   1 point
#
# So a match is two bursts, or three of the lesser finishes, or a mix — which is
# what makes the finish type worth playing for rather than incidental. Only the
# MATCH counts for the ladder: rank score, ranked W/L and the streak move once,
# at the end, not once per round.
FINISH_BURST = "burst"
FINISH_SURVIVAL = "survival"
FINISH_RINGOUT = "ringout"

FINISH_POINTS = {
    FINISH_BURST:    2,
    FINISH_SURVIVAL: 1,
    FINISH_RINGOUT:  1,
}

FINISH_LABEL = {
    FINISH_BURST:    ("💥", "Burst Finish"),
    FINISH_SURVIVAL: ("⏳", "Survival Finish"),
    FINISH_RINGOUT:  ("🌀", "Ring-Out Finish"),
}

MATCH_TARGET = 3

# How many ranked matches two specific players may play against each other per
# day. Without a cap, the cheapest way to climb is to find one willing partner
# and farm them, which is the same hole that keeping casual battles off the
# ladder was meant to close.
PAIR_DAILY_LIMIT = 1
K_PAIRS = "ranked_pairs"


def finish_points(kind: str) -> int:
    """Points a finish is worth. Unknown finishes score the minimum rather than
    zero — a round that happened should never be worth nothing."""
    return FINISH_POINTS.get(str(kind or ""), 1)


def finish_label(kind: str) -> str:
    emoji, name = FINISH_LABEL.get(str(kind or ""), ("🏁", "Finish"))
    return f"{emoji} {name}"


# ── Config (stored in data/config.json under "ranked") ────────────────────────
#
# Verification is gone as of v1.18, and with it the control-server lock.
#
# The gate defaulted to off and no install ever turned it on, but it cost a
# command, a profile key, a filter inside `build_board`, three admin actions
# and a slice of the settings screen. The control lock existed to protect
# those settings; with only the leaderboard reset left — already owner-only
# through the admin registry's `owner_only` — a second lock in front of one
# action was more concept than protection.
#
# `_DEFAULT_CONFIG` is kept, empty, rather than deleted: `get_config` and
# `save_config` are the shape the rest of the module and the admin panel talk
# to, and a settings store that exists but holds nothing is a smaller change
# than removing the concept and putting it back the next time ranked needs a
# setting.

CONFIG_KEY = "ranked"

_DEFAULT_CONFIG: dict = {}


def get_config(config: Optional[dict] = None) -> dict:
    """Ranked settings, defaulted. Never raises, never writes."""
    if config is None:
        try:
            from utils.database import load_config
            config = load_config()
        except Exception:                                # noqa: BLE001
            config = {}
    raw = (config or {}).get(CONFIG_KEY)
    out = dict(_DEFAULT_CONFIG)
    if isinstance(raw, dict):
        for k in out:
            if k in raw:
                out[k] = raw[k]
    return out


def save_config(changes: dict) -> dict:
    """Merge `changes` into the ranked config and persist. Returns the result."""
    from utils.database import load_config, save_config as _save
    cfg = load_config() or {}
    current = get_config(cfg)
    current.update({k: v for k, v in changes.items() if k in _DEFAULT_CONFIG})
    cfg[CONFIG_KEY] = current
    _save(cfg)
    return current


# ── Stats ────────────────────────────────────────────────────────────────────

def _int(profile: dict, key: str) -> int:
    try:
        return int((profile or {}).get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def ranked_wins(profile: dict) -> int:
    return _int(profile, K_RANKED_WINS)


def ranked_losses(profile: dict) -> int:
    return _int(profile, K_RANKED_LOSSES)


def ranked_games(profile: dict) -> int:
    return ranked_wins(profile) + ranked_losses(profile)


def win_rate(profile: dict) -> float:
    """Ranked win rate as a percentage. 0.0 with no ranked games."""
    games = ranked_games(profile)
    return (ranked_wins(profile) / games * 100.0) if games else 0.0


def beys_caught(profile: dict) -> int:
    """Lifetime catches.

    Falls back to the current inventory size for a profile that predates the
    counter, so the board is not empty on day one. It under-counts anyone who
    has sold duplicates — inventory shrinks, catches do not — but a low real
    number beats a zero for everybody, and it self-corrects as soon as they
    catch again.
    """
    if K_CAUGHT in (profile or {}):
        return _int(profile, K_CAUGHT)
    inv = (profile or {}).get("inventory") or []
    return len(inv) if isinstance(inv, list) else 0


def best_streak(profile: dict) -> int:
    return _int(profile, K_BEST_STREAK)


def rank_score(profile: dict) -> int:
    return _int(profile, K_RANK_SCORE)


# ── Leaderboard categories ───────────────────────────────────────────────────
#
# One table drives the slash-command choices, the sort, the displayed value and
# the eligibility rule. Adding a category is one entry here rather than four
# edits that can disagree with each other.

def trainer_level(profile: dict) -> int:
    return _int(profile, "level")


def coins(profile: dict) -> int:
    return _int(profile, "coins")


# Community XP is a SEPARATE track from trainer xp — it is earned by talking,
# voting and entering giveaways in the main server, and it never pays coins.
# The key is deliberately not "level": `get_user` recomputes that one from
# trainer xp on every read, so a community level stored there would not survive
# the next profile read.
def community_xp(profile: dict) -> int:
    return _int(profile, K_COMMUNITY_XP)


def community_level(profile: dict) -> int:
    return _int(profile, K_COMMUNITY_LEVEL)


# The boards below are MAIN-SERVER ONLY, and that is enforced where a board is
# run, not here: `@app_commands.choices` is built once at registration, so the
# choice exists in every server whatever this table says. `MAIN_ONLY` is what
# the command layer consults before rendering one.
MAIN_ONLY = frozenset({"chatxp", "commlevel"})


CATEGORIES: dict[str, dict] = {
    "rank": {
        "label": "Rank Score",
        "emoji": "🎖️",
        "describe": "Ladder position by rank score",
        "value": rank_score,
        "format": lambda p: f"{rank_score(p):,} pts",
        "eligible": lambda p: ranked_games(p) > 0,
        "empty": "Nobody has played a ranked battle yet.",
    },
    "winrate": {
        "label": "Win Rate",
        "emoji": "📊",
        "describe": f"Ranked win rate (min {MIN_RANKED_GAMES} games)",
        "value": win_rate,
        "format": lambda p: (f"{win_rate(p):.1f}%  "
                             f"({ranked_wins(p)}W/{ranked_losses(p)}L)"),
        "eligible": lambda p: ranked_games(p) >= MIN_RANKED_GAMES,
        "empty": f"No player has {MIN_RANKED_GAMES} ranked games yet.",
    },
    "wins": {
        "label": "Ranked Wins",
        "emoji": "🏆",
        "describe": "Most ranked battles won",
        "value": ranked_wins,
        "format": lambda p: f"{ranked_wins(p):,} wins",
        "eligible": lambda p: ranked_wins(p) > 0,
        "empty": "No ranked wins recorded yet.",
    },
    "streak": {
        "label": "Best Win Streak",
        "emoji": "🔥",
        "describe": "Longest ranked win streak",
        "value": best_streak,
        "format": lambda p: f"{best_streak(p):,} in a row",
        "eligible": lambda p: best_streak(p) > 0,
        "empty": "No win streaks recorded yet.",
    },
    "catches": {
        "label": "Beys Caught",
        "emoji": "🌀",
        "describe": "Most Beyblades caught from spawns",
        "value": beys_caught,
        "format": lambda p: f"{beys_caught(p):,} caught",
        "eligible": lambda p: beys_caught(p) > 0,
        "empty": "Nobody has caught a Beyblade yet.",
    },
    # Level and money are not ranked stats — they come from playing at all,
    # not from playing ranked. They are boards anyway because "who is furthest
    # along" and "who is richest" are the two questions players actually ask,
    # and neither had an answer anywhere in the bot.
    "level": {
        "label": "Trainer Level",
        "emoji": "📈",
        "describe": "Highest trainer level",
        "value": trainer_level,
        "format": lambda p: f"Level {trainer_level(p):,}",
        "eligible": lambda p: trainer_level(p) > 1,
        "empty": "Nobody has levelled up yet.",
    },
    "money": {
        "label": "Beycoins",
        "emoji": "🪙",
        "describe": "Richest bladers",
        "value": coins,
        "format": lambda p: f"{coins(p):,} coins",
        "eligible": lambda p: coins(p) > 0,
        "empty": "Nobody has any Beycoins yet.",
    },
    "chatxp": {
        "label": "Community XP",
        "emoji": "✨",
        "describe": "Most active in the main server",
        "value": community_xp,
        "format": lambda p: f"{community_xp(p):,} XP",
        "eligible": lambda p: community_xp(p) > 0,
        "empty": "Nobody has earned community XP yet.",
    },
    "commlevel": {
        "label": "Community Level",
        "emoji": "🌟",
        "describe": "Highest community level",
        "value": community_level,
        "format": lambda p: f"Level {community_level(p):,}",
        "eligible": lambda p: community_level(p) > 0,
        "empty": "Nobody has reached community level 1 yet.",
    },
}

DEFAULT_CATEGORY = "rank"


def build_board(users: list[dict], category: str = DEFAULT_CATEGORY,
                limit: int = 100,
                config: Optional[dict] = None) -> list[tuple[dict, float]]:
    """Sorted [(profile, value)] for one category, eligibility already applied.

    The verification filter that used to sit here went with the gate in v1.18.
    Eligibility is now purely about the stat: you are on the wins board once
    you have won something.
    """
    spec = CATEGORIES.get(category) or CATEGORIES[DEFAULT_CATEGORY]
    rows: list[tuple[dict, float]] = []
    for p in users or []:
        if not isinstance(p, dict):
            continue
        if not spec["eligible"](p):
            continue
        rows.append((p, float(spec["value"](p))))
    # Ties break on rank score then ranked wins, so equal values order
    # deterministically instead of by whatever order the store returned.
    rows.sort(key=lambda r: (r[1], rank_score(r[0]), ranked_wins(r[0])),
              reverse=True)
    return rows[:max(1, int(limit))]


def position_of(users: list[dict], user_id, category: str = DEFAULT_CATEGORY,
                config: Optional[dict] = None) -> Optional[int]:
    """1-based position on a board, or None when not placed."""
    board = build_board(users, category, limit=10 ** 9, config=config)
    for i, (p, _v) in enumerate(board, 1):
        if str(p.get("user_id")) == str(user_id):
            return i
    return None


def placings(users: list[dict], user_id,
             config: Optional[dict] = None,
             include_main_only: bool = False) -> dict[str, int]:
    """Every board this player is placed on, as `{category: position}`.

    Boards they are not on are left out rather than mapped to None, so the
    caller renders what it is given. Lives here rather than in the cog because
    it is a rule about the ladder, and because the cog would otherwise sort the
    whole registry once per board inline with no way to test the result.

    `include_main_only` defaults to FALSE, and that default is the fix for a
    leak: this walked every category, so `/rank` printed the main-server-only
    community placings to players in every other server — around the gate that
    exists to withhold exactly that. A caller that has checked the guild passes
    True; everyone else gets the public boards.
    """
    out: dict[str, int] = {}
    for key in CATEGORIES:
        if key in MAIN_ONLY and not include_main_only:
            continue
        pos = position_of(users, user_id, key, config=config)
        if pos:
            out[key] = pos
    return out


# ── Recording a ranked result ────────────────────────────────────────────────

def apply_ranked_win(profile: dict) -> int:
    """Record a ranked win in place. Returns the new streak."""
    from utils.ranks import apply_win
    profile[K_RANKED_WINS] = ranked_wins(profile) + 1
    apply_win(profile)
    streak = _int(profile, K_WIN_STREAK) + 1
    profile[K_WIN_STREAK] = streak
    profile[K_BEST_STREAK] = max(best_streak(profile), streak)
    return streak


def apply_ranked_loss(profile: dict) -> None:
    """Record a ranked loss in place."""
    from utils.ranks import apply_loss
    profile[K_RANKED_LOSSES] = ranked_losses(profile) + 1
    apply_loss(profile)
    profile[K_WIN_STREAK] = 0


# ── Per-opponent daily limit ─────────────────────────────────────────────────

def _today(now: Optional[float] = None) -> str:
    """UTC date stamp. A date string rather than a rolling timer because
    'you have 1 left today' is something a player can reason about, and a
    rolling window means the answer changes depending on when they ask."""
    import datetime
    ts = datetime.datetime.fromtimestamp(
        now if now is not None else __import__("time").time(),
        datetime.timezone.utc)
    return ts.strftime("%Y-%m-%d")


def pair_count(profile: dict, opponent_id, now: Optional[float] = None) -> int:
    """Ranked matches already played against this opponent today."""
    pairs = (profile or {}).get(K_PAIRS)
    if not isinstance(pairs, dict):
        return 0
    entry = pairs.get(str(opponent_id))
    if not isinstance(entry, dict):
        return 0
    if entry.get("day") != _today(now):
        return 0                       # yesterday's tally has already expired
    try:
        return int(entry.get("count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def pair_remaining(profile: dict, opponent_id, now: Optional[float] = None) -> int:
    return max(0, PAIR_DAILY_LIMIT - pair_count(profile, opponent_id, now))


def pair_limit_error(profile: dict, opponent_id, opponent_name: str = "them",
                     now: Optional[float] = None) -> str:
    """Player-facing reason this pairing is used up, or '' when it is not."""
    if pair_remaining(profile, opponent_id, now) > 0:
        return ""
    # Pluralised off the constant rather than hard-coded, so the sentence
    # stays correct if the cap ever moves again — at 1 it must read "1 ranked
    # match", not "1 ranked matches".
    plural = "" if PAIR_DAILY_LIMIT == 1 else "es"
    return (f"You've already played {PAIR_DAILY_LIMIT} ranked match{plural} "
            f"against {opponent_name} today. Find a different opponent — the "
            f"limit resets at midnight UTC.")


def record_pair_match(profile: dict, opponent_id,
                      now: Optional[float] = None) -> int:
    """Count one ranked match against this opponent. Returns the new count.

    Prunes other opponents' expired entries as it goes, so the dict cannot grow
    without bound for a player who fights many different people.
    """
    today = _today(now)
    pairs = profile.get(K_PAIRS)
    if not isinstance(pairs, dict):
        pairs = {}
    pairs = {k: v for k, v in pairs.items()
             if isinstance(v, dict) and v.get("day") == today}
    entry = pairs.get(str(opponent_id)) or {"day": today, "count": 0}
    entry["day"] = today
    try:
        entry["count"] = int(entry.get("count", 0) or 0) + 1
    except (TypeError, ValueError):
        entry["count"] = 1
    pairs[str(opponent_id)] = entry
    profile[K_PAIRS] = pairs
    return entry["count"]


def record_catch(profile: dict) -> int:
    """Count one caught Beyblade. Returns the new total."""
    total = beys_caught(profile) + 1
    profile[K_CAUGHT] = total
    return total


# ── Reset ────────────────────────────────────────────────────────────────────

def reset_keys_for(category: str) -> tuple[str, ...]:
    """Which profile keys a reset of `category` clears. 'all' clears every
    resettable key; an unknown category clears nothing rather than guessing."""
    if category == "all":
        out: set = set()
        for keys in RESETTABLE.values():
            out.update(keys)
        return tuple(sorted(out))
    return RESETTABLE.get(category, ())


def apply_reset(profile: dict, category: str) -> bool:
    """Zero one profile's stats for a category. True when something changed."""
    changed = False
    for key in reset_keys_for(category):
        if _int(profile, key) != 0 or key in profile:
            if profile.get(key) not in (0, None):
                changed = True
            profile[key] = 0
    return changed
