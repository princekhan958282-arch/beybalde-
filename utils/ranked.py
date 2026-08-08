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

Verification is OFF until the owner turns it on, and only the owner can turn it
on or point it at a server. While it is on, ranked play and leaderboard
placement require a verified account — a player verifies by being a member of
the configured Discord server.

The reason to gate the ladder rather than the whole bot: an unverified player
loses nothing they already had. They keep catching, battling, story mode and
every other system, and only the competitive surface asks them to verify.
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
K_VERIFIED = "ranked_verified"

# Every key a leaderboard reset is allowed to clear. Kept as an explicit list so
# a reset can never wander into coins, inventory or trainer level — the failure
# mode there is unrecoverable and silent.
RESETTABLE = {
    "rank":     (K_RANK_SCORE, K_RANKED_WINS, K_RANKED_LOSSES),
    "winrate":  (K_RANKED_WINS, K_RANKED_LOSSES),
    "wins":     (K_RANKED_WINS, K_RANKED_LOSSES),
    "streak":   (K_BEST_STREAK, K_WIN_STREAK),
    "catches":  (K_CAUGHT,),
}

# A win rate needs a floor or the board is topped forever by whoever went 1-0
# and stopped playing. Ten games is enough that one lucky run cannot hold first
# place, and low enough to be reachable in an evening.
MIN_RANKED_GAMES = 10


# ── Config (stored in data/config.json under "ranked") ────────────────────────

CONFIG_KEY = "ranked"
DEFAULT_INVITE = "https://discord.gg/bMtyey32Ur"

_DEFAULT_CONFIG = {
    "verify_enabled": False,
    "verify_guild_id": None,
    "verify_invite": DEFAULT_INVITE,
}


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


def verify_required(config: Optional[dict] = None) -> bool:
    c = get_config(config)
    # Enabled but pointed at nothing would lock everyone out of ranked with no
    # way to satisfy the check, so an unset guild means the gate is not armed.
    return bool(c.get("verify_enabled")) and bool(c.get("verify_guild_id"))


def is_verified(profile: dict, config: Optional[dict] = None) -> bool:
    """Can this player touch the ladder right now?"""
    if not verify_required(config):
        return True
    return bool((profile or {}).get(K_VERIFIED))


def eligibility_error(profile: dict, config: Optional[dict] = None) -> str:
    """Player-facing reason they cannot play ranked, or '' when they can."""
    if is_verified(profile, config):
        return ""
    invite = get_config(config).get("verify_invite") or DEFAULT_INVITE
    return ("Ranked play is verified-only. Join the server and run `/verify`:\n"
            f"{invite}")


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
}

DEFAULT_CATEGORY = "rank"


def build_board(users: list[dict], category: str = DEFAULT_CATEGORY,
                limit: int = 100,
                config: Optional[dict] = None) -> list[tuple[dict, float]]:
    """Sorted [(profile, value)] for one category, eligibility already applied.

    Verification is enforced here rather than at display time, so an unverified
    player cannot occupy a slot that a verified one should hold — a board that
    hides rows after ranking them has gaps in its numbering.
    """
    spec = CATEGORIES.get(category) or CATEGORIES[DEFAULT_CATEGORY]
    gate = verify_required(config)
    rows: list[tuple[dict, float]] = []
    for p in users or []:
        if not isinstance(p, dict):
            continue
        if gate and not p.get(K_VERIFIED):
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
