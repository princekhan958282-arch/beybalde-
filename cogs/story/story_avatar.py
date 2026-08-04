"""
story_avatar.py — the "wearing connection" for PvE.

The problem
-----------
An avatar's bonuses come in two families:

  * STAT bonuses (attack/defence/stamina/HP, flat and percent) — already folded
    into the player's fighter by utils.loadout.effective_blade / effective_hp,
    which boss_battle._player_fighter calls. Those have always worked.

  * TIMED bonuses (dodge, counter, crit, nth-hit, defence-break, immortality,
    damage and status resistance, multi-hit) — these live in
    cogs/battle/avatar_combat.py, and every one of its functions takes a
    `session` and reads `session.avatar_bonuses`. That makes them reachable
    only from BattleSession, i.e. only from PvP.

So in a boss fight `_player_fighter` computes the AvatarBonuses object and then
throws it away (boss_battle.py:276-280). Concretely: Argus keeps only its +30%
attack, and Dyrroth — whose entire kit is nth-hit, defence-break,
special_move_percent and ult_adds_attack_stat — contributes nothing at all.

The fix
-------
avatar_combat's contract is small. It needs an object exposing:

    .avatar_bonuses  {key: AvatarBonuses}
    .hp              {key: float}          PRE-damage HP (guard_lethal reads it)
    .round           int
    .stamina_manager .gauge {key: float}   (on_crit refunds into this)

plus two attributes it creates lazily on the session itself,
`_avatar_hit_counts` and `_avatar_immortal`, which must survive between turns.

`_Shim` below is exactly that object, and `AvatarLayer` owns the cross-turn
state and drives the calls. avatar_combat itself is used completely unmodified,
so PvP and Story Mode share one implementation of every effect and cannot drift.

What is inert here, and why
---------------------------
Two bonus families have nothing to attach to in the boss_ai resolver, and are
left visibly inert rather than faked into something they do not mean:

  stability_flat / stability_percent   boss_ai has no stability pool at all.
  resistance_status_chance             plain story opponents inflict no status
                                       effects (silence/burn are a BossState
                                       and an AbilityEngine feature).

Both are also inert everywhere else in the bot today — `apply_stability_bonus`
and `apply_charge_bonus` have zero call sites repo-wide. `charge_*` IS wired up
here, on the gauge a Charge move grants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

from cogs.battle import avatar_combat as AVC

# The two combatant keys. avatar_combat only ever indexes by these, and Story
# Mode is strictly 1v1, so two constants are enough.
PKEY = "player"
NKEY = "npc"

# boss_ai.resolve() is deterministic and has no crit concept, so Story Mode
# supplies one for `crit_percent` to hook into. 1.6x matches the PvP crit in
# damage_rules.calc_damage (`atk_stat * 1.6`).
STORY_CRIT_MULT = 1.6

# boss_ai Specials are a single hit, so avatar_combat.multi_hit_shape — which
# reshapes a hit COUNT — is a no-op here (it returns early on hits <= 1).
# The two multi-hit flags are therefore applied as explicit Special damage
# multipliers instead. This is a deliberate reinterpretation: the flags keep
# their spirit ("your Special hits harder") without pretending the resolver
# models multiple hits.
MULTI_HIT_DOUBLE_MULT = 2.0    # multi_hit_power_double
MULTI_HIT_EXTRA_MULT  = 1.5    # multi_hit_extra_hits — a 3rd hit on a 2-hit kit


class _Shim:
    """A BattleSession-shaped view over one Story Mode exchange."""

    __slots__ = ("avatar_bonuses", "hp", "round", "stamina_manager",
                 "_avatar_hit_counts", "_avatar_immortal")

    def __init__(self, bonuses, round_no: int, player_hp: float,
                 player_gauge: float, hit_counts: dict, immortal: dict):
        self.avatar_bonuses = {PKEY: bonuses}
        # Pre-damage HP on purpose: guard_lethal compares the incoming hit
        # against session.hp[key] to decide whether it is lethal.
        self.hp = {PKEY: float(player_hp)}
        self.round = int(round_no)
        self.stamina_manager = SimpleNamespace(gauge={PKEY: float(player_gauge)})
        # Handed in rather than created, so nth-hit counters and the
        # once-per-battle immortality trigger persist across turns.
        self._avatar_hit_counts = hit_counts
        self._avatar_immortal = immortal


@dataclass
class Exchange:
    """The avatar-adjusted result of one exchange.

    `dmg_out`/`dmg_in` are totals, not deltas — the engine recomputes HP from a
    pre-exchange snapshot using these, rather than patching the HP that
    boss_ai.resolve already wrote. See story_engine.StoryFight.step for why
    that distinction matters.
    """
    dmg_out: float = 0.0          # player -> opponent
    dmg_in:  float = 0.0          # opponent -> player
    counter: float = 0.0          # extra damage to the opponent from a dodge
    gauge:   float = 0.0          # the player's special gauge, post-adjustment
    # True when an incoming hit was reduced to nothing — a dodge, or resistance
    # eating the last point of a tiny hit. Either way the player took no damage,
    # which is what the caller needs to know.
    dodged:  bool = False
    logs:    list[str] = field(default_factory=list)


class AvatarLayer:
    """Applies one player's AvatarBonuses to a boss_ai exchange.

    Owns the state avatar_combat expects to live on the session for the whole
    fight. Construct once per fight; call pre_exchange/post_exchange per turn.
    """

    def __init__(self, bonuses) -> None:
        self.av = bonuses
        self._hit_counts: dict[str, int] = {}
        self._immortal: dict[str, object] = {}

    @property
    def active(self) -> bool:
        """False when nothing is equipped, or the equipped avatar is all zeroes.

        Mirrors avatar_combat._av()'s own gate, so `active` is False in exactly
        the cases where every call below would be a no-op.
        """
        return bool(self.av is not None and getattr(self.av, "has_any_bonus", False))

    def _shim(self, round_no: int, player_hp: float, player_gauge: float) -> _Shim:
        return _Shim(self.av, round_no, player_hp, player_gauge,
                     self._hit_counts, self._immortal)

    # ── Before the exchange ──────────────────────────────────────────────────

    def pre_exchange(self, round_no: int, npc_defense: float,
                     player_attacking: bool) -> tuple[float, list[str]]:
        """Defence break — shave the opponent's DEF for this exchange only.

        Returns the defence to fight with. The caller restores the original in
        a `finally`, the same way BossFight.step handles BladeKit multipliers,
        so the modifier can never compound turn on turn.

        Skipped when the player isn't swinging: the opponent's DEF only shapes
        incoming damage, so breaking it on a Recover turn would change nothing
        and still print a line claiming it had.
        """
        if not self.active or not player_attacking:
            return npc_defense, []
        shim = self._shim(round_no, 0.0, 0.0)
        stats, logs = AVC.apply_defence_break(shim, PKEY, {"defense": npc_defense})
        return float(stats.get("defense", npc_defense)), logs

    # ── After the exchange ───────────────────────────────────────────────────

    def post_exchange(self, *, round_no: int, player_hp_before: float,
                      player_gauge: float, gauge_gained: float,
                      player_attack: float, is_special: bool, is_charge: bool,
                      dmg_out: float, dmg_in: float) -> Exchange:
        """Run the raw exchange numbers through every timed avatar effect.

        `dmg_out`/`dmg_in` come straight out of boss_ai.resolve's report.
        `player_hp_before` must be the HP the player had BEFORE resolve wrote
        to it — guard_lethal decides "was this lethal?" from it.
        `player_gauge` is the gauge AFTER resolve; `gauge_gained` is how much of
        it resolve just granted, which is what charge_* scales.
        """
        out = Exchange(dmg_out=float(dmg_out), dmg_in=float(dmg_in),
                       gauge=float(player_gauge))
        if not self.active:
            return out

        shim = self._shim(round_no, player_hp_before, player_gauge)
        logs: list[str] = []

        # ── Outgoing: crit, nth-hit, Special riders ──────────────────────────
        if out.dmg_out > 0:
            # boss_ai has no crit of its own, so `already_crit` is always False
            # and this is purely the avatar's roll.
            if AVC.roll_extra_crit(shim, PKEY, False):
                before = out.dmg_out
                out.dmg_out *= STORY_CRIT_MULT
                logs.append(f"  💥 **Avatar** — critical hit! "
                            f"{before:.0f} → {out.dmg_out:.0f}")
                # Refunds Special Gauge for avatars with gauge_on_crit (Argus).
                logs.extend(AVC.on_crit(shim, PKEY))

            bonus, nth_logs = AVC.nth_hit_bonus(shim, PKEY, player_attack)
            if bonus:
                out.dmg_out += bonus
                logs.extend(nth_logs)

        if is_special and out.dmg_out > 0:
            # special_move_flat/_percent and ult_adds_attack_stat.
            boosted, ult_logs = AVC.apply_ult_bonus(
                shim, PKEY, int(round(out.dmg_out)), player_attack)
            out.dmg_out = float(boosted)
            logs.extend(ult_logs)
            logs.extend(self._multi_hit(out))

        # ── Incoming: dodge, counter, resistance, then immortality ───────────
        if out.dmg_in > 0:
            before_in = out.dmg_in
            through, counter, abs_logs = AVC.absorb_incoming(
                shim, PKEY, NKEY, int(round(out.dmg_in)))
            out.dmg_in = float(through)
            out.counter = float(counter)
            out.dodged = before_in > 0 and through <= 0
            logs.extend(abs_logs)

        # Must run AFTER absorb_incoming (a dodged hit cannot be lethal) and
        # BEFORE the engine writes HP.
        if out.dmg_in > 0:
            capped, imm_logs = AVC.guard_lethal(shim, PKEY, int(round(out.dmg_in)))
            out.dmg_in = float(capped)
            logs.extend(imm_logs)

        # ── Charge ───────────────────────────────────────────────────────────
        # charge_flat/_percent has no consumer anywhere in the bot. The gauge a
        # Charge move grants is the obvious one, so it is wired here.
        # Seeded from the shim so any gauge_on_crit refund above is included.
        gauge = float(shim.stamina_manager.gauge.get(PKEY, player_gauge))
        if is_charge and gauge_gained > 0:
            boosted = float(self.av.apply_charge_bonus(gauge_gained))
            if boosted > gauge_gained:
                gauge += (boosted - gauge_gained)
                logs.append(f"  🔌 **Avatar** — charge surges "
                            f"+{boosted - gauge_gained:.0f} gauge!")
        out.gauge = gauge

        out.logs = logs
        return out

    # ── Multi-hit ────────────────────────────────────────────────────────────

    def _multi_hit(self, out: Exchange) -> list[str]:
        """multi_hit_power_double / multi_hit_extra_hits as Special multipliers.

        See MULTI_HIT_* above for why this is a multiplier rather than a call
        into avatar_combat.multi_hit_shape.
        """
        logs: list[str] = []
        if getattr(self.av, "multi_hit_extra_hits", False):
            out.dmg_out *= MULTI_HIT_EXTRA_MULT
            logs.append("  ➕ **Avatar** — an extra hit rides the Special!")
        if getattr(self.av, "multi_hit_power_double", False):
            out.dmg_out *= MULTI_HIT_DOUBLE_MULT
            logs.append("  ✳️ **Avatar** — every hit doubled!")
        return logs


# ── Convenience ──────────────────────────────────────────────────────────────

def layer_for(user_id: int) -> AvatarLayer:
    """Build the layer for a player. Never raises — a broken avatar must not
    stop a fight starting, which is the same contract loadout.avatar_bonuses
    already keeps."""
    try:
        from utils.loadout import avatar_bonuses
        return AvatarLayer(avatar_bonuses(user_id))
    except Exception:                                    # noqa: BLE001
        return AvatarLayer(None)
