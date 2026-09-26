"""Persistent tournament career statistics stored on the existing player profile.

The live lobby remains intentionally in-memory; only completed competitive
results are persisted. This keeps tournament recovery simple while giving
players a durable career profile.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

DEFAULT_RATING = 1000
K_FACTOR = 24


def career(profile: dict[str, Any]) -> dict[str, Any]:
    raw = profile.get("tournament_profile") or {}
    return {
        "tournaments": int(raw.get("tournaments", 0)),
        "match_wins": int(raw.get("match_wins", 0)),
        "match_losses": int(raw.get("match_losses", 0)),
        "championships": int(raw.get("championships", 0)),
        "finals": int(raw.get("finals", 0)),
        "semifinals": int(raw.get("semifinals", 0)),
        "current_streak": int(raw.get("current_streak", 0)),
        "best_streak": int(raw.get("best_streak", 0)),
        "rating": int(raw.get("rating", DEFAULT_RATING)),
        "bey_wins": dict(raw.get("bey_wins") or {}),
        "recent": list(raw.get("recent") or [])[-8:],
    }


def _expected(a: int, b: int) -> float:
    return 1.0 / (1.0 + 10.0 ** ((b - a) / 400.0))


def record_match(winner_profile: dict, loser_profile: dict,
                 winner_bey: str = "", loser_bey: str = "") -> tuple[int, int]:
    """Mutate two loaded profiles for one completed tournament match.

    Returns the new winner/loser ratings. Callers should persist both profiles
    through the bot's normal atomic mutation path.
    """
    w = career(winner_profile)
    l = career(loser_profile)
    delta = max(1, round(K_FACTOR * (1.0 - _expected(w["rating"], l["rating"]))))
    w["rating"] += delta
    l["rating"] = max(0, l["rating"] - delta)
    w["match_wins"] += 1
    l["match_losses"] += 1
    w["current_streak"] += 1
    w["best_streak"] = max(w["best_streak"], w["current_streak"])
    l["current_streak"] = 0
    if winner_bey:
        wins = Counter(w["bey_wins"])
        wins[winner_bey] += 1
        w["bey_wins"] = dict(wins)
    winner_profile["tournament_profile"] = w
    loser_profile["tournament_profile"] = l
    return w["rating"], l["rating"]


def record_finish(profile: dict, placement: int) -> None:
    """Record one completed tournament appearance and final placement."""
    c = career(profile)
    c["tournaments"] += 1
    if placement == 1:
        c["championships"] += 1
    if placement <= 2:
        c["finals"] += 1
    if placement <= 4:
        c["semifinals"] += 1
    c["recent"] = (c["recent"] + [int(placement)])[-8:]
    profile["tournament_profile"] = c


def best_bey(profile: dict) -> tuple[str, int]:
    wins = career(profile)["bey_wins"]
    if not wins:
        return "—", 0
    name = max(wins, key=lambda key: (wins[key], key))
    return name, int(wins[name])


def win_rate(profile: dict) -> float:
    c = career(profile)
    total = c["match_wins"] + c["match_losses"]
    return (100.0 * c["match_wins"] / total) if total else 0.0
