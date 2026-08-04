"""
story_engine.py — the Story Mode turn machine.

A solo, trimmed sibling of boss_battle.BossFight: no party, no lobby, no
scripted boss specials, no BossState. What it keeps is the part that matters —
the exchange resolver and the search AI in boss_ai, used completely unmodified,
so Story Mode fights by exactly the same rules as a boss fight.

Deliberately imports no discord. boss_battle does (and its package __init__
pulls it in transitively), so `_player_fighter` is rebuilt here out of the same
underlying helpers — utils.loadout, boss_copy.equipped_blade,
database.get_stat_multiplier — rather than imported from it. That keeps this
module loadable by tools/sim_story.py without a bot, and lets Story Mode's HP
baseline move independently of the boss system's.

The avatar connection lives in story_avatar.AvatarLayer; see that module for
why it exists and what it can and cannot reach.
"""

from __future__ import annotations

import logging
import random
from typing import Optional

from cogs.battle import stamina_manager as _SM
from cogs.battle.boss import boss_ai as ai
from cogs.battle.boss import boss_copy as bcopy

from . import story_data
from .story_avatar import AvatarLayer, Exchange

log = logging.getLogger("beyblade_bot.story")

# Story Mode runs a smaller HP pool than boss_battle's BASE_PLAYER_HP (4000).
# At 4000 the player's bar is ~50 turns deep against any statline a story
# opponent can plausibly carry, so every fight hit the turn cap and no opponent
# could ever actually threaten the player. A story stage is meant to be a
# 15-30 turn fight you can lose, and that means both bars have to be roughly
# the same depth. Avatar and level HP bonuses scale on top of this, so they are
# proportionally MORE valuable here than in a boss fight, not less.
BASE_PLAYER_HP = 2000

# Anti-stall. boss_ai's heal budget makes an infinite fight very unlikely, but
# "very unlikely" is not "impossible", and a Discord view cannot run forever.
# A fight that reaches this is a draw and pays nothing. Set well clear of the
# ~30 turns a late-campaign stage actually takes: a cap that a legitimate fight
# can reach turns a win the player earned into a draw that pays nothing.
TURN_CAP = 55

MOVE_LABELS = {
    ai.MOVE_ATTACK:  ("⚔️", "Attack"),
    ai.MOVE_DEFENSE: ("🛡️", "Defend"),
    ai.MOVE_STAMINA: ("🔋", "Recover"),
    ai.MOVE_CHARGE:  ("🔌", "Charge"),
    ai.MOVE_SPECIAL: ("🌟", "Special"),
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def build_player_fighter(user_id: int) -> tuple[ai.Fighter, dict]:
    """The player's fighter, with parts, bey level, mastery and avatar folded in.

    Same composition as boss_battle._player_fighter, and for the same reason:
    utils.loadout.effective_blade is the single place a player's real stats are
    resolved, so equipping a part or an avatar changes this without any further
    wiring. Returns (fighter, blade).
    """
    from utils.database import get_user, get_stat_multiplier

    profile = get_user(user_id)
    # equipped_blade resolves an equipped boss copy to its rolled stats and
    # falls back to the database blade; reading active_beyblade directly would
    # miss a copy's display name and drop the player to the 90/90/90 default.
    try:
        blade, copy = bcopy.equipped_blade(user_id)
    except Exception as exc:                             # noqa: BLE001
        log.debug("equipped blade lookup failed for %s: %s", user_id, exc)
        blade, copy = None, None

    breakdown: dict = {}
    av = None
    if blade:
        from utils.loadout import effective_blade
        # A copy is a fixed roll; letting parts stack on top would make a
        # farmed copy stronger than the boss it came from.
        blade, breakdown, av = effective_blade(
            user_id, profile, blade, include_parts=(copy is None))

    name  = (blade or {}).get("name") or profile.get("active_beyblade")
    stats = (blade or {}).get("stats") or {}

    atk = float(stats.get("attack", 90) or 90)
    dfn = float(stats.get("defense", 90) or 90)
    sta = float(stats.get("stamina", 90) or 90)

    try:
        mult = get_stat_multiplier(user_id, None if copy else name)
    except Exception:                                    # noqa: BLE001
        mult = 1.0

    hp = BASE_PLAYER_HP * mult
    if copy and stats.get("hp"):
        hp = max(hp, float(stats["hp"]) * mult)
    else:
        # The pool is flat, so a blade's printed hp stat never mattered here —
        # but that would make the level system's hp GROWTH purely decorative,
        # so the gain over base is added on.
        hp_bd = (breakdown or {}).get("hp") or {}
        gain = float(hp_bd.get("total", 0)) - float(hp_bd.get("base", 0))
        if gain > 0:
            hp += gain * mult

    if av is not None:
        from utils.loadout import effective_hp
        hp = effective_hp(user_id, hp, av)

    # Stamina bar from the stamina stat, same derivation PvP uses, instead of a
    # flat 10 for everyone. A stamina blade now carries a visibly deeper bar
    # here too, and the stat keeps paying past the old flat ceiling.
    eff_sta = sta * mult
    sp_max = _SM.max_stamina_for(eff_sta)
    sp_now = _SM.StaminaManager._initial_stamina(int(eff_sta), sp_max)

    fighter = ai.Fighter(
        name or "Unequipped", hp, hp,
        atk * mult, dfn * mult, eff_sta, sp=sp_now, sp_max=sp_max,
        dmg_mult=ai.type_damage_mult((blade or {}).get("type")),
        special_mult=_special_mult(breakdown),
    )
    return fighter, (blade or {})


def _special_mult(breakdown: dict) -> float:
    """How far the bey's `special` stat has grown past its printed value.

    effective_blade's breakdown already carries both numbers, so this needs no
    second lookup. 1.0 for an unlevelled bey, which leaves Specials exactly as
    they were.
    """
    spc = (breakdown or {}).get("special") or {}
    base = float(spc.get("base", 0) or 0)
    total = float(spc.get("total", 0) or 0)
    if base <= 0 or total <= 0:
        return 1.0
    return max(1.0, total / base)


def build_opponent(stage: dict) -> ai.Fighter:
    """The stage's opponent, at its level.

    Stats come from story_data.opponent_stats — archetype base scaled by the
    stage's level — so a stage's difficulty is one number, not four. No
    BossState: story opponents have no gimmick.
    """
    stats = stage.get("stats") or story_data.opponent_stats(stage)
    hp = float(stats["hp"])
    return ai.Fighter(
        stage["name"], hp, hp,
        float(stats["attack"]), float(stats["defense"]), float(stats["stamina"]),
        sp=10.0, is_boss=True,
        dmg_mult=ai.type_damage_mult(stage.get("type")),
    )


class StoryFight:
    """One Story Mode stage attempt.

    Takes a player id and display name rather than a discord.Member so the
    engine stays importable without discord — the cog passes
    `member.id, member.display_name`.
    """

    def __init__(self, user_id: int, display_name: str, stage_id: str,
                 rng: Optional[random.Random] = None) -> None:
        stage = story_data.stage(stage_id)
        if stage is None:
            raise KeyError(f"unknown story stage: {stage_id!r}")

        self.user_id = int(user_id)
        self.display_name = display_name
        self.stage = stage
        self.stage_id = stage["id"]
        self.difficulty = stage["difficulty"]

        self.foe, self.blade = build_player_fighter(user_id)
        self.npc = build_opponent(stage)
        self.avatar = AvatarLayer(_avatar_bonuses(user_id))

        # ── Levels, both sides ───────────────────────────────────────────────
        # The player's is not new — the bey's level already reaches the fighter
        # through bey_levels.stats_at inside effective_blade, and the trainer
        # level through get_stat_multiplier. Story Mode simply never showed it,
        # so a player levelling up saw no acknowledgement anywhere. These read
        # what is already applied; they do not apply anything themselves, which
        # is what keeps the scaling from being counted twice.
        self.npc_level = int(stage.get("level", 1))
        self.bey_level = int((self.blade or {}).get("level", 1) or 1)
        self.trainer_level = _trainer_level(user_id)

        self.model = ai.OpponentModel()
        self.rng = rng or random.Random()
        self.turn = 0
        self.finished = False
        self.result: Optional[str] = None        # "win" | "loss" | "draw"
        self.log: list[str] = []
        self.avatar_logs: list[str] = []
        self.last_report: dict = {}

    # ── Read-only views ──────────────────────────────────────────────────────

    @property
    def avatar_active(self) -> bool:
        return self.avatar.active

    def foe_habit(self) -> str:
        h = self.model.history[-6:]
        if not h:
            return "nothing obvious"
        top = max(set(h), key=h.count)
        return f"{top} ({h.count(top)}/{len(h)} recent turns)"

    def legal_moves(self) -> list[str]:
        return self.foe.legal_moves()

    @property
    def level_gap(self) -> int:
        """Opponent level minus the player's bey level.

        Positive means you are under-levelled for this stage — the one number
        that tells a player 'go and level up' rather than 'try again harder'.
        """
        return self.npc_level - self.bey_level

    def state(self) -> dict:
        return {
            "npc_hp_pct":  self.npc.hp / self.npc.max_hp * 100,
            "foe_hp_pct":  self.foe.hp / self.foe.max_hp * 100,
            "turn":        self.turn,
            "foe_habit":   self.foe_habit(),
            "npc_level":   self.npc_level,
            "bey_level":   self.bey_level,
        }

    # ── The turn ─────────────────────────────────────────────────────────────

    def step(self, player_move: str) -> dict:
        """Resolve one exchange. Mutates both fighters. Returns a report dict."""
        if self.finished:
            return dict(self.last_report)

        self.turn += 1
        self.model.observe(player_move)

        npc_move, _meta = ai.choose_move(
            self.npc, self.foe, self.model,
            rng=self.rng, difficulty=self.difficulty)

        # Snapshot everything the avatar layer may need to rewrite. HP is
        # recomputed from these below rather than patched afterwards — see the
        # note on _apply_kit in boss_battle for why post-hoc HP refunds are a
        # trap (they resurrect fighters that resolve() already clamped to 0).
        hp_npc_before   = self.npc.hp
        hp_foe_before   = self.foe.hp
        gauge_before    = self.foe.gauge
        before_fracs    = ai.hp_fractions(self.npc, self.foe)

        # Defence break applies for this exchange only, then is restored —
        # the same temporary-stat pattern BossFight.step uses for BladeKit.
        npc_def_before = self.npc.defense
        new_def, pre_logs = self.avatar.pre_exchange(
            self.turn, self.npc.defense,
            player_move in (ai.MOVE_ATTACK, ai.MOVE_SPECIAL))
        self.npc.defense = new_def
        try:
            report = ai.resolve(self.npc, self.foe, npc_move, player_move)
        finally:
            self.npc.defense = npc_def_before

        # resolve(a, b, ...) is called with a=opponent, b=player, so
        # dmg_to_a is the damage the PLAYER dealt and dmg_to_b what they took.
        raw_out = float(report.get("dmg_to_a", 0.0))
        raw_in  = float(report.get("dmg_to_b", 0.0))
        heal_npc = float(report.get("heal_a", 0.0))
        heal_foe = float(report.get("heal_b", 0.0))

        ex: Exchange = self.avatar.post_exchange(
            round_no=self.turn,
            player_hp_before=hp_foe_before,
            player_gauge=self.foe.gauge,
            gauge_gained=max(0.0, self.foe.gauge - gauge_before),
            player_attack=self.foe.eff_attack,
            is_special=(player_move == ai.MOVE_SPECIAL),
            is_charge=(player_move == ai.MOVE_CHARGE),
            dmg_out=raw_out,
            dmg_in=raw_in,
        )

        # Recompute both bars from the pre-exchange snapshot. Every source of
        # HP movement inside resolve() is accounted for by its four reported
        # numbers (offence, shard absorb, riposte and reflect are all folded
        # into dmg_* before the single write at boss_ai.py:319-320), so this is
        # a faithful replay with the avatar's adjustments applied.
        self.npc.hp = _clamp(hp_npc_before - ex.dmg_out - ex.counter + heal_npc,
                             0.0, self.npc.max_hp)
        self.foe.hp = _clamp(hp_foe_before - ex.dmg_in + heal_foe,
                             0.0, self.foe.max_hp)

        # Gauge bookkeeping. resolve() grants GAUGE_PER_DMG_TAKEN for any hit
        # that landed; a dodged hit did not land, so that grant is taken back.
        gauge = ex.gauge
        if ex.dodged and raw_in > 0:
            gauge -= ai.GAUGE_PER_DMG_TAKEN
        self.foe.gauge = _clamp(gauge, 0.0, ai.SPECIAL_GAUGE_MAX)

        # outcome() is the authority, not `alive()`. On a double KO it settles
        # the tiebreak by pre-exchange health and revives the winner to 1 HP —
        # so reading `not npc.alive()` first would score a genuine draw, where
        # both fighters are down, as a win.
        #   a = opponent, b = player.
        res = ai.outcome(self.npc, self.foe, before_fracs)
        if res == "b":
            self.finished, self.result = True, "win"
        elif res == "a":
            self.finished, self.result = True, "loss"
        elif res == "draw":
            self.finished, self.result = True, "draw"
        elif self.turn >= TURN_CAP:
            self.finished, self.result = True, "draw"

        report.update(
            npc_move=npc_move,
            player_move=player_move,
            dmg_out=ex.dmg_out + ex.counter,
            dmg_in=ex.dmg_in,
            counter=ex.counter,
            dodged=ex.dodged,
            heal_npc=heal_npc,
            heal_foe=heal_foe,
            avatar_logs=pre_logs + ex.logs,
        )
        self._log_turn(report)
        self.last_report = report
        return report

    # ── Logging ──────────────────────────────────────────────────────────────

    def _log_turn(self, report: dict) -> None:
        pm = MOVE_LABELS[report["player_move"]][1]
        nm = MOVE_LABELS[report["npc_move"]][1]
        bits = [f"**T{self.turn}** you **{pm}** vs **{nm}**"]
        if report["dmg_out"] > 0:
            bits.append(f"→ 💥 {report['dmg_out']:.0f} to {self.npc.name}")
        if report["dodged"]:
            bits.append("→ 💨 dodged")
        elif report["dmg_in"] > 0:
            bits.append(f"→ 💢 {report['dmg_in']:.0f} to you")
        if report["heal_npc"] > 0:
            bits.append(f"→ {self.npc.name} +{report['heal_npc']:.0f}")
        if report["heal_foe"] > 0:
            bits.append(f"→ you +{report['heal_foe']:.0f}")
        self.log.append(" ".join(bits))
        self.log = self.log[-3:]

        if report["avatar_logs"]:
            self.avatar_logs.extend(report["avatar_logs"])
            self.avatar_logs = self.avatar_logs[-4:]


def _avatar_bonuses(user_id: int):
    """Never raises — a broken avatar must not stop a fight starting."""
    try:
        from utils.loadout import avatar_bonuses
        return avatar_bonuses(user_id)
    except Exception as exc:                             # noqa: BLE001
        log.debug("avatar bonuses unavailable for %s: %s", user_id, exc)
        return None


def _trainer_level(user_id: int) -> int:
    """The player's trainer level. Never raises — display only."""
    try:
        from utils.database import get_user
        return int(get_user(user_id).get("level", 0) or 0)
    except Exception as exc:                             # noqa: BLE001
        log.debug("trainer level unavailable for %s: %s", user_id, exc)
        return 0
