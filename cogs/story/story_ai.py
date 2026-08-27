"""
story_ai.py — the School League opponent, driving a real PvP battle.

What this is, and what it is not
--------------------------------
Story Mode fights run on `cogs/battle/session.py` — the same `BattleSession`
that `;battle` uses, with stability, ring-outs, the full ability DSL, named
Special moves, status effects and the real damage pipeline. None of that
existed in Story before; it ran on the boss resolver, where a blade's ability
did nothing and its Special was `attack × 2.6`.

What Story keeps from the boss engine is its **brain**, not its rules:
`OpponentModel` and the `DIFFICULTY` ladder are pure — they take move strings
and a couple of numbers, and know nothing about any engine. That is the IQ 1–5
dial the League's difficulty modes are built on.

The lossy part, stated plainly
------------------------------
`boss_ai.choose_move` searches by cloning a `Fighter` and calling
`boss_ai.resolve`. A `BattleSession` cannot be cloned — its state lives across
eight managers and resolving mutates them — so the search runs on a
**projection** of the live session into a `Fighter` pair.

The projection carries HP, the three combat stats, stamina and gauge. It does
not carry stability, shields, burns or ability state, so the search cannot see
a ring-out coming. The opponent therefore plays a good move, not a perfect one,
and that is an accepted trade rather than an oversight: the alternative is a
second combat engine, which is the thing this rewrite exists to delete.

The stamina trap
----------------
`boss_ai.STAMINA_COST` is a **separate, stale copy** of the cost table —
1.5 / 1.5 / 0 / 1.0 / 3.0 against the live
`stamina_manager.STAMINA_COST` of 2.2 / 2.2 / 0 / 1.5 / 4.4. Left alone, the
search would happily pick a Special the real session cannot pay for, and
`deduct_cost` would drive the opponent into a stamina KO it never chose. Every
move is re-checked before it is used — by asking
`stamina_manager.cost_for(key, move)`, NOT by reading a table.

That distinction is the whole guard, and it was briefly lost. This used to
re-check against `stamina_manager.STAMINA_COST`, which was correct only while
that table WAS the price. Attack and Defense now scale with the blade's own
stats, so the table became the base of a curve rather than the answer, and a
guard that read it cleared moves the session could not pay for — reintroducing
the exact KO described above. Ask the manager; it is the only thing that knows.
"""

from __future__ import annotations

import logging
from typing import Optional

from cogs.battle import special_gate
from cogs.battle.boss import boss_ai as ai
from cogs.core.constants import (
    MOVE_ATTACK, MOVE_CHARGE, MOVE_DEFENSE, MOVE_SPECIAL, MOVE_STAMINA,
    STABILITY_ATTACK_HIT, STABILITY_DEF_PASSIVE, STABILITY_STAMINA_RECOVERY,
)

log = logging.getLogger("beyblade_bot.story.ai")

# Cheapest first, so the fallback ladder degrades gracefully. Stamina costs
# nothing and is therefore always affordable — the guaranteed last resort.
_FALLBACK_ORDER = (MOVE_STAMINA, MOVE_CHARGE, MOVE_DEFENSE, MOVE_ATTACK,
                   MOVE_SPECIAL)

# ── The ring-out guard ───────────────────────────────────────────────────────
#
# What the projection cannot see is stability, and stability is what ends a
# large share of these battles: attacking costs the ATTACKER 10, defending
# costs the DEFENDER 6, and the Stamina action is the only move that puts any
# back. A search blind to that will happily attack itself off the ring on the
# turn it was winning.
#
# So one thing the search does not know is handed to it directly: do not play a
# move that rings you out when a legal move exists that does not. It is a
# decision improvement, not a stat multiplier — which is the whole basis the
# difficulty modes were asked to be built on.
#
# HOW MUCH IT IS WORTH, measured rather than assumed. Paired runs, identical
# seeds, 160 matches per cell, a level-100 player against the whole League:
#
#     Normal    IQ 3                 91.2% player win rate
#     Nightmare IQ 5, guard off      85.6%
#     Nightmare IQ 5, guard on       83.8%
#
# So the IQ rung is doing the work — 5.6 points — and this guard adds about
# 1.8 more, which at n=160 is three matches and inside the noise. It fires
# roughly once every 24 battles, because reaching 0 stability takes ten
# straight attacks and most battles end first. Keep it because never ringing
# yourself out is strictly correct play and it costs one comparison; do not
# reach for it as the lever if Nightmare ever needs to be genuinely harder.
#
# The numbers are imported from the real table rather than retyped. They are
# the guaranteed component only; counters, clashes and blocks move stability
# too and depend on what the other side plays, which is exactly the part that
# cannot be known when the move is chosen.
_SELF_STABILITY = {
    MOVE_ATTACK:  STABILITY_ATTACK_HIT,          # -10
    MOVE_DEFENSE: STABILITY_DEF_PASSIVE,         # -6
    MOVE_STAMINA: STABILITY_STAMINA_RECOVERY,    # +25
    MOVE_CHARGE:  0,
    MOVE_SPECIAL: 0,
}

# Only Nightmare gets it. Normal is the IQ 3 rung that was asked for and stays
# exactly that; the gap between the two modes is meant to be how well the
# opponent plays, and this is the sharpest lever available that is not a stat.
STABILITY_GUARD_RUNGS = frozenset({"nightmare"})


def affordable(session, key: str, move: str) -> bool:
    """Can this side actually pay for `move` in the REAL session right now?

    The authority is `stamina_manager`, never `boss_ai`'s copy of the table.
    """
    if move == MOVE_SPECIAL:
        # The gauge AND any second resource the blade carries. Routed through
        # special_gate so the League opponent is held to exactly the rule the
        # SPECIAL button enforces — a fourth private copy of "gauge >= 150"
        # is how an opponent ends up firing a Special the player could not.
        # gauge_max omitted so the blade's own cost is used — passing the
        # global constant here is exactly how the opponent ends up held to a
        # different rule than the player's button.
        if not special_gate.ready(
                session, key, session.blades.get(key),
                session.stamina_manager.gauge.get(key, 0)):
            return False
    # `stamina_manager.cost_for`, never the STAMINA_COST table. The table is
    # only the BASE of the curve now — Attack and Defense scale with the
    # blade's stats — so reading it here cleared moves at the base price that
    # `deduct_cost` then charged at the scaled one, which is precisely the
    # stamina KO this function exists to prevent (see the module docstring).
    have = float(session.stamina_manager.stamina.get(key, 0.0))
    return have >= float(session.stamina_manager.cost_for(key, move))


def legal_moves(session, key: str) -> list[str]:
    """Every move this side can pay for. Never empty — Stamina is free."""
    out = [m for m in _FALLBACK_ORDER if affordable(session, key, m)]
    return out or [MOVE_STAMINA]


def project(session, key: str, okey: str) -> ai.Fighter:
    """One side of the live session as a `boss_ai.Fighter`.

    Deliberately a snapshot: the search clones and mutates it, and nothing it
    does may reach back into the session.
    """
    sm = session.stamina_manager
    stats = (session.battle_stats or {}).get(key) or {}
    blade = (session.blades or {}).get(key) or {}
    name = blade.get("name") or key

    try:
        sp_max = float(sm.cap_for(key))
    except Exception:                                    # noqa: BLE001
        sp_max = ai.STAMINA_MAX

    # A Special's damage scales with the levelled `special` stat, and the
    # search prices Specials off `special_mult`. Without it a level-100 blade's
    # Special looks like its printed one and the opponent under-rates the most
    # expensive move it owns.
    printed_sp = float((blade.get("stats") or {}).get("special", 0) or 0)
    eff_sp = float((getattr(session, "special_stats", None) or {}).get(key, 0))
    special_mult = (max(1.0, eff_sp / printed_sp)
                    if printed_sp > 0 and eff_sp > 0 else 1.0)

    return ai.Fighter(
        name=str(name),
        hp=float(session.hp.get(key, 0)),
        max_hp=float(session.max_hp_per_player.get(key, 1) or 1),
        attack=float(stats.get("attack", 0)),
        defense=float(stats.get("defense", 0)),
        stamina_stat=float(stats.get("stamina", 0)),
        sp=float(sm.stamina.get(key, 0.0)),
        sp_max=sp_max,
        gauge=float(sm.gauge.get(key, 0.0)),
        # `is_boss` stays False. It is the heal-ceiling flag, and a League
        # opponent is a blade at level 100, not a raid boss.
        is_boss=False,
        dmg_mult=ai.type_damage_mult(blade.get("type")),
        special_mult=special_mult,
    )


def stability_after(session, key: str, move: str) -> float:
    """This side's stability after playing `move`, as far as it can be known."""
    try:
        cur = float(session.stability_manager.stability.get(key, 0))
    except Exception:                                    # noqa: BLE001
        return 1.0
    return cur + _SELF_STABILITY.get(move, 0)


def guard_ringout(session, key: str, okey: str, move: str, legal: list[str],
                  values: Optional[dict] = None) -> str:
    """Swap out a move that would ring this side out, if anything else will do.

    Returns `move` unchanged when it is survivable, when stability is not in
    play for this matchup at all, or when every legal move loses — a guard that
    invented an illegal move to survive would be worse than the ring-out.
    """
    try:
        if not session.stability_manager.is_effects_active(key, okey):
            # Stability is inert for this type matchup, so the deltas above
            # will not be applied and predicting them would fire the guard for
            # a ring-out that cannot happen.
            return move
    except Exception:                                    # noqa: BLE001
        pass
    if stability_after(session, key, move) > 0:
        return move
    safe = [m for m in legal if stability_after(session, key, m) > 0]
    if not safe:
        return move
    if values:
        return max(safe, key=lambda m: values.get(m, float("-inf")))
    # No scores to rank by: take the move that recovers the most.
    return max(safe, key=lambda m: _SELF_STABILITY.get(m, 0))


class LeagueOpponent:
    """The NPC side of a School League battle.

    `BattleSession` asks this for a move whenever a round opens, and writes the
    answer straight into `session.moves`. It carries the small amount of
    identity the session needs — `key`, a `profile` stub, a `level` and an
    `hp_gain` — so that no profile read ever reaches the store for a player
    who does not exist.
    """

    def __init__(self, member, blade: dict, difficulty: str,
                 level: int = 100, hp_gain: int = 0,
                 rng=None, avatar_id: Optional[str] = None) -> None:
        self.member = member
        self.key = str(member.id)
        self.blade = blade
        self.difficulty = difficulty
        self.level = int(level)
        self.hp_gain = int(hp_gain)
        self.rng = rng
        # The blader card this opponent fights as. Read by
        # `BattleSession._avatar_card_for`, which looks it up straight from the
        # avatar roster — an NPC has no profile to equip anything from.
        self.avatar_id = avatar_id
        # Read by `BattleSession._profile_for`. Empty on purpose: an opponent
        # has no parts, no avatar and no mastery.
        self.profile: dict = {}
        self.model = ai.OpponentModel()
        self._last_seen_round = 0

    # ── the IQ dial ──────────────────────────────────────────────────────────
    @property
    def iq(self) -> int:
        return int((ai.DIFFICULTY.get(self.difficulty) or {}).get("iq", 1))

    @property
    def iq_label(self) -> str:
        return ai.IQ_LABELS.get(self.iq, "?")

    @property
    def guards_stability(self) -> bool:
        return self.difficulty in STABILITY_GUARD_RUNGS

    # ── the one method the session calls ─────────────────────────────────────
    async def choose(self, session) -> str:
        okey = next((k for k in session.moves if k != self.key), None)
        if okey is None:                                 # pragma: no cover
            return MOVE_STAMINA

        # Feed the model what the human actually did last round, so `read` and
        # `predict` have something to work with. Without this the whole
        # opponent-modelling half of the IQ ladder is inert and every rung
        # plays the same.
        try:
            last = (getattr(session, "last_moves", None) or {}).get(okey)
            if last and session.round > self._last_seen_round:
                self.model.observe(last)
                self._last_seen_round = session.round
        except Exception:                                # noqa: BLE001
            pass

        me = project(session, self.key, okey)
        foe = project(session, okey, self.key)

        values: dict = {}
        try:
            move, values = ai.choose_move(
                me, foe, self.model,
                rng=self.rng, difficulty=self.difficulty)
        except Exception:                                # noqa: BLE001
            move = None

        move = self.clamp(session, move)
        if self.guards_stability:
            legal = legal_moves(session, self.key)
            move = self.clamp(session, guard_ringout(
                session, self.key, okey, move, legal, values))
        return move

    def clamp(self, session, move: Optional[str]) -> str:
        """The chosen move, or the best affordable one under the REAL table.

        `boss_ai` carries its own stale cost table, so a move that the search
        believes is affordable may not be. Falling back to the most expensive
        move that IS affordable keeps the opponent playing rather than
        defaulting to Stamina every time the search overreaches.
        """
        legal = legal_moves(session, self.key)
        if move in legal:
            return move
        for candidate in reversed(_FALLBACK_ORDER):
            if candidate in legal:
                return candidate
        return MOVE_STAMINA
