"""
brackets.py — generation and progression for single elim, double elim and
round robin.

Pure functions over Match records. Nothing here schedules, notifies or writes to
a database, so a whole tournament can be played out in a loop to prove the
bracket terminates and produces exactly one champion.
"""

from __future__ import annotations

import math
from typing import Optional

from .models import Match, MatchState, Mode


def _pow2_ceil(n: int) -> int:
    return 1 if n <= 1 else 1 << (n - 1).bit_length()


def seed_order(size: int) -> list[int]:
    """Standard bracket seeding, so the top two seeds can only meet in the final.

    Naive pairing (1v2, 3v4, ...) knocks the best players out first and makes
    the bracket feel arbitrary. This produces 1v8, 4v5, 2v7, 3v6 for size 8.
    """
    order = [0]
    while len(order) < size:
        n = len(order) * 2
        nxt = []
        for s in order:
            nxt.append(s)
            nxt.append(n - 1 - s)
        order = nxt
    return order


# ── Generation ────────────────────────────────────────────────────────────────

def generate(tournament_id: str, entrants: list[int], mode: str) -> list[Match]:
    if len(entrants) < 2:
        raise ValueError("A tournament needs at least 2 players.")
    if mode == Mode.ROUND_ROBIN.value:
        return _round_robin(tournament_id, entrants)
    return _elimination(tournament_id, entrants, double=(mode == Mode.DOUBLE.value))


def _elimination(tid: str, entrants: list[int], double: bool) -> list[Match]:
    size = _pow2_ceil(len(entrants))
    # Pad to a power of two with byes rather than rejecting odd counts — a bye
    # is a match with one player that auto-advances.
    padded: list[Optional[int]] = list(entrants) + [None] * (size - len(entrants))
    ordered = [padded[i] for i in seed_order(size)]

    matches: list[Match] = []
    wb: dict[int, list[Match]] = {}

    rounds = max(1, int(math.log2(size)))
    for rnd in range(1, rounds + 1):
        wb[rnd] = []
        for slot in range(size // (2 ** rnd)):
            m = Match(tournament_id=tid, round_no=rnd, bracket="winners",
                      slot=slot)
            if rnd == 1:
                m.player_a = ordered[slot * 2]
                m.player_b = ordered[slot * 2 + 1]
            wb[rnd].append(m)
            matches.append(m)

    for rnd in range(1, rounds):
        for slot, m in enumerate(wb[rnd]):
            m.winner_to = [wb[rnd + 1][slot // 2].id, "a" if slot % 2 == 0 else "b"]

    if not double:
        return matches

    # ── Losers bracket ───────────────────────────────────────────────────────
    # Widths run S/4, S/4, S/8, S/8, ... 1, 1. Odd rounds absorb that round's
    # winners-bracket droppers; even rounds pair the survivors off.
    lb: dict[int, list[Match]] = {}
    widths: list[int] = []
    count = size // 4
    while count >= 1:
        widths.append(count)
        widths.append(count)
        count //= 2
    for i, width in enumerate(widths, start=1):
        lb[i] = [Match(tournament_id=tid, round_no=i, bracket="losers", slot=s)
                 for s in range(width)]
        matches.extend(lb[i])

    for rnd in range(1, len(widths)):
        cur, nxt = lb[rnd], lb[rnd + 1]
        for slot, m in enumerate(cur):
            if len(nxt) == len(cur):          # merge round: seat A, dropper is B
                m.winner_to = [nxt[slot].id, "a"]
            else:                             # pairing round: halve
                m.winner_to = [nxt[slot // 2].id,
                               "a" if slot % 2 == 0 else "b"]

    # Winners-bracket losers drop in. Round 1 losers fill both seats of LB
    # round 1; later rounds take seat B of the matching merge round.
    if lb:
        for slot, m in enumerate(wb[1]):
            m.loser_to = [lb[1][slot // 2].id, "a" if slot % 2 == 0 else "b"]
    for rnd in range(2, rounds + 1):
        target = lb.get((rnd - 1) * 2)
        if not target:
            continue
        for slot, m in enumerate(wb[rnd]):
            m.loser_to = [target[slot % len(target)].id, "b"]

    gf = Match(tournament_id=tid, round_no=1, bracket="grand_final", slot=0)
    matches.append(gf)
    wb[rounds][0].winner_to = [gf.id, "a"]
    if widths:
        lb[len(widths)][0].winner_to = [gf.id, "b"]
    else:
        # size 2: there is no losers bracket at all, so the winners-final loser
        # IS the grand final opponent. Without this the GF waits forever.
        wb[rounds][0].loser_to = [gf.id, "b"]
    return matches


def _round_robin(tid: str, entrants: list[int]) -> list[Match]:
    """Circle method. With an odd count one player sits out each round."""
    players: list[Optional[int]] = list(entrants)
    if len(players) % 2:
        players.append(None)
    n = len(players)
    matches: list[Match] = []
    for rnd in range(n - 1):
        for slot in range(n // 2):
            a, b = players[slot], players[n - 1 - slot]
            if a is None or b is None:
                continue                     # the sit-out, not a real match
            matches.append(Match(tournament_id=tid, round_no=rnd + 1,
                                 bracket="rr", slot=slot,
                                 player_a=a, player_b=b))
        players = [players[0]] + [players[-1]] + players[1:-1]
    return matches


# ── Progression ───────────────────────────────────────────────────────────────

def _by_id(matches: list[Match]) -> dict:
    return {m.id: m for m in matches}


def _seat(match: Match, seat: str, user_id: int) -> None:
    if seat == "a":
        match.player_a = user_id
    else:
        match.player_b = user_id


def advance(matches: list[Match], finished: Match, mode: str) -> list[Match]:
    """Send the winner (and in double elim the loser) along their stored edges.

    Returns the matches that changed, so the caller knows what to persist and
    announce.
    """
    if finished.winner is None or mode == Mode.ROUND_ROBIN.value:
        return []
    index = _by_id(matches)
    touched: list[Match] = []
    for link, who in ((finished.winner_to, finished.winner),
                      (finished.loser_to, finished.loser)):
        if not link or who is None:
            continue
        target = index.get(link[0])
        if target is None:
            continue
        _seat(target, link[1], who)
        touched.append(target)
    return touched


def ready_matches(matches: list[Match]) -> list[Match]:
    """Matches with both seats filled that have not been scheduled yet."""
    return [m for m in matches
            if m.state == MatchState.PENDING.value and len(m.players) == 2]


def _pending_feeders(matches: list[Match], target_id: str) -> bool:
    """True while some unfinished match could still deliver a player here."""
    done = (MatchState.COMPLETED.value, MatchState.FORFEIT.value)
    for m in matches:
        if m.state in done:
            continue
        for link in (m.winner_to, m.loser_to):
            if link and link[0] == target_id:
                return True
    return False


def auto_byes(matches: list[Match], mode: str, now: float) -> list[Match]:
    """Resolve one-player matches and cascade, to a fixed point.

    A bye can fill a later match that is itself a bye, so this repeats until
    nothing changes. A half-filled match only counts as a bye once no
    unfinished match still points at it — otherwise the second player simply
    has not arrived.
    """
    if mode == Mode.ROUND_ROBIN.value:
        return []
    changed: list[Match] = []
    progress = True
    while progress:
        progress = False
        for m in matches:
            if m.state != MatchState.PENDING.value:
                continue
            if _pending_feeders(matches, m.id):
                continue                      # a player may still arrive
            if not m.players:
                # Both feeders were byes, so nobody will ever arrive. Without
                # this the match sits PENDING forever with zero players — not a
                # bye by is_bye()'s definition — and every match downstream of
                # it waits on a feeder that can never finish. That was the
                # double-elimination deadlock at 5, 9 and 10 entrants.
                m.score = "empty"
                m.transition(MatchState.FORFEIT, now)
                changed.append(m)
                progress = True
                continue
            if not m.is_bye():
                continue
            m.winner = m.players[0]
            m.score = "bye"
            for step in (MatchState.SCHEDULED, MatchState.CHECKIN,
                         MatchState.ACTIVE, MatchState.COMPLETED):
                m.transition(step, now)
            changed.append(m)
            changed.extend(advance(matches, m, mode))
            progress = True
    return changed


def champion(matches: list[Match], mode: str) -> Optional[int]:
    if mode == Mode.ROUND_ROBIN.value:
        table = standings(matches)
        return table[0][0] if table else None
    done = (MatchState.COMPLETED.value, MatchState.FORFEIT.value)
    if mode == Mode.DOUBLE.value:
        gf = next((m for m in matches if m.bracket == "grand_final"), None)
        return gf.winner if gf and gf.state in done else None
    finals = [m for m in matches if m.bracket == "winners"]
    if not finals:
        return None
    last = max(m.round_no for m in finals)
    final = next((m for m in finals if m.round_no == last and m.slot == 0), None)
    return final.winner if final and final.state in done else None


def is_complete(matches: list[Match], mode: str) -> bool:
    if mode == Mode.ROUND_ROBIN.value:
        return all(m.state in (MatchState.COMPLETED.value,
                               MatchState.FORFEIT.value) for m in matches)
    return champion(matches, mode) is not None


def standings(matches: list[Match]) -> list[tuple[int, int, int]]:
    """(user_id, wins, losses) sorted by wins desc — used for round robin."""
    tally: dict[int, list[int]] = {}
    for m in matches:
        if m.state not in (MatchState.COMPLETED.value, MatchState.FORFEIT.value):
            continue
        for p in m.players:
            tally.setdefault(p, [0, 0])
        if m.winner is not None:
            tally.setdefault(m.winner, [0, 0])[0] += 1
        if m.loser is not None:
            tally.setdefault(m.loser, [0, 0])[1] += 1
    return sorted(((u, w, l) for u, (w, l) in tally.items()),
                  key=lambda r: (-r[1], r[2]))
