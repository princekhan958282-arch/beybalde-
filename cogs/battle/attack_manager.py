"""
cogs/attack_manager.py
----------------------
Dedicated Attack Manager.

Centralises every piece of attack-resolution logic that was previously
scattered between session.py (resolve_pair), damage_rules.py, and
ability_engine.py so BattleSession stays thin.

Responsibilities
----------------
  • Raw damage calculation (delegating maths to damage_rules helpers)
  • Stat-multiplier scaling
  • Crit announcement
  • Passive flat damage reduction (Dead Phoenix / Undying Blaze)
  • Type modifier application — outgoing ATK bonus + incoming DEF mitigation
  • Defense-pierce / shatter-stack pre-processing
  • Solar Flare bonus consumption
  • Attack-type counter announcement (Attack vs Stamina 1.2×)
  • Special move resolution (multi-hit loop + per-hit procs)
  • Gauge updates post-attack
  • HP application and counter-hit HP application
  • Counter log messages

What stays elsewhere
--------------------
  • Ability primitives (rage, buffs, chains, shields) — AbilityEngine.apply()
  • Stamina costs and gauge state — StaminaManager
  • Stamina KO check — StaminaManager.check_stamina_ko()
  • Grind debuff tracking — session._resolve_round_inner (small, session-scoped)
  • Round-log assembly for non-attack events (DoT, regen) — session

Public API
----------
  AttackManager(session)          — construct once per BattleSession

  .resolve_pair(mkey, okey, mmove, omove, mblade, oblade, mstats, ostats)
      → (dmg_dealt: int, dmg_taken: int, matchup: str, logs: list[str])

  .apply_pair_results(k1, k2, b1, b2, m1, m2,
                      dmg_p1, counter_p1, matchup_p1,
                      dmg_p2, counter_p2, matchup_p2)
      → logs: list[str]
      Applies damage to hp dict and emits counter/counter-announce log lines.

  .build_round_summary(p1, p2, k1, k2, m1, m2, dmg_p1, dmg_p2,
                       matchup_p1, matchup_p2, extra_logs)
      → list[str]
      Builds the human-readable round results block.
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING, Any

from .constants import (
    BASE_HP,
    MOVE_ATTACK, MOVE_DEFENSE, MOVE_STAMINA, MOVE_SPECIAL, MOVE_CHARGE,
    MOVE_LABELS,
    SPECIAL_GAUGE_MAX,
)
from .damage_rules import calc_damage, resolve_special
from . import avatar_combat as AVC
from cogs.abilities.special_moves import SELF_MANAGED_HITS
from cogs.ui.log_formatter import format_battle_logs  # NEW: Import log formatter

if TYPE_CHECKING:
    import discord
    from .session import BattleSession


class AttackManager:
    """Owns the full attack-resolution pipeline for one BattleSession.

    Constructed once and stored as ``session.attack_manager``.
    All methods are synchronous — no async needed since no Discord calls happen here.
    """

    def __init__(self, session: "BattleSession") -> None:
        self.session = session

    # =========================================================================
    #  Main entry point — resolve one player's move against the opponent
    # =========================================================================

    def resolve_pair(
        self,
        mkey:   str,
        okey:   str,
        mmove:  str,
        omove:  str,
        mblade: dict,
        oblade: dict,
        mstats: dict,
        ostats: dict,
    ) -> tuple[int, int, str, list[str]]:
        """Resolve one attacker → defender pairing.

        Returns ``(dmg_dealt, dmg_taken, matchup, log_lines)``.

        ``dmg_taken`` is non-zero only when a Defense counter returns damage
        to the attacker (handled by the Attack-vs-Defense branch in
        damage_rules.calc_damage).
        """
        logs: list[str] = []

        # ── Non-combat moves: no damage, but abilities STILL run ─────────────
        # Gauge for STAMINA/CHARGE is already added by session._resolve_round_inner.
        # Returning early here skipped AbilityEngine.apply() entirely, which
        # meant: every `passive` rule went silent on any round its owner banked
        # or charged (~40% of rounds), the whole on_stamina_win / _loss /
        # _mirror trigger family was never dispatched even once, and
        # DamageFilter step 1 — the only thing that ticks buff and silence
        # durations — never ran, so timed buffs froze instead of expiring.
        # calc_damage already classifies these moves (Stamina beats Defense,
        # loses to Attack, mirrors everything else), so ask it for the matchup
        # and run the pipeline with zero damage.
        if mmove in (MOVE_STAMINA, MOVE_CHARGE):
            _, _, matchup, _ = calc_damage(mmove, mstats, ostats, mblade, omove)
            _, _, ab_logs = self.session.ability.apply(
                mkey, okey, mblade, oblade, mmove, matchup, 0, 0
            )
            logs.extend(ab_logs)
            # Damage stays 0: apply_pair_results deliberately skips HP changes
            # for non-combat moves. Abilities that deal damage on a Stamina win
            # use true_damage / steal_hp, which write session.hp directly.
            return 0, 0, matchup, logs

        if mmove == MOVE_SPECIAL:
            # Specials never cost stability (spec: "Special move → self 0").
            # The old -10 here meant blades could ring THEMSELVES out by
            # unleashing their own special.
            dmg, logs = self._resolve_special(mkey, okey, mmove, mblade, oblade, logs)
            return dmg, 0, "win", logs

        # ── Check active defense-pierce before preprocessing ──────────────────
        pierce_turns = self.session.status.get_duration("ignore_defense_turns", mkey)
        piercing_defense = pierce_turns > 0

        # ── Pre-process defender stats (pierce / shatter / def buff) ──────────
        ostats, pre_logs = self.session.defense_manager.preprocess_defender_stats(
            mkey, okey, mblade, oblade, ostats
        )
        logs.extend(pre_logs)

        # ── Avatar: Guard Breaker shreds DEF for the opening rounds ──────────
        ostats, _avc_logs = AVC.apply_defence_break(self.session, mkey, ostats)
        logs.extend(_avc_logs)

        # ── Raw damage from damage_rules ──────────────────────────────────────
        dmg_dealt, dmg_taken, matchup, is_crit = calc_damage(
            mmove, mstats, ostats, mblade, omove
        )

        # ── Avatar: extra crit roll on top of the blade's own chance ──────────
        if mmove == MOVE_ATTACK:
            is_crit = AVC.roll_extra_crit(self.session, mkey, is_crit)

        # ── Guaranteed crit override (Guilty Longinus — Condemned Mode) ───────
        # guaranteed_crit_turns is set by on_win / ChainHandler but was never
        # read here — the feature was completely dead.  Wire it up: if the
        # attacker has turns remaining and this is an attack-type move that
        # didn't already crit naturally, force a crit and consume one turn.
        if mmove == MOVE_ATTACK and not is_crit:
            gc_turns = self.session.ability.guaranteed_crit_turns.get(mkey, 0)
            if gc_turns > 0:
                atk_stat = mstats.get("attack", 50)
                def_stat = ostats.get("defense", 50)
                import math as _math
                from .constants import NORMAL_ATTACK_DAMAGE_SCALE
                raw_crit = max(5, _math.ceil(atk_stat * 1.6))
                if omove == MOVE_DEFENSE:
                    from .damage_rules import SHIELD_FLAT_RATIO, _deficit_bleed
                    flat_red = _math.ceil(def_stat * SHIELD_FLAT_RATIO)
                    dmg_dealt = max(5, raw_crit - flat_red)
                    dmg_dealt = _deficit_bleed(dmg_dealt, atk_stat, def_stat)
                    dmg_dealt = max(5, _math.ceil(dmg_dealt * NORMAL_ATTACK_DAMAGE_SCALE))
                    dmg_taken = 0   # no counter on a crit
                    matchup   = "win"
                else:
                    dmg_dealt = max(5, _math.ceil(raw_crit * NORMAL_ATTACK_DAMAGE_SCALE))
                is_crit = True
                self.session.ability.guaranteed_crit_turns[mkey] = gc_turns - 1
                logs.append(
                    f"  ⚖️ **Condemned Mode** — {mblade['name']} forces a GUARANTEED CRIT! "
                    f"({gc_turns - 1} turn(s) remaining)"
                )

        # ── Ignore Defense: suppress the counter-hit when piercing defense ────
        # When defense is pierced the counter is nullified and we announce once.
        # Previously this only fired when dmg_taken > 0, but when preprocess
        # zeroes DEF the gate threshold drops to 0 meaning calc_damage never
        # generates a counter — dmg_taken is always 0, so the log never printed.
        # Now we announce unconditionally whenever piercing is active and the
        # opponent used Defense, then clear any residual counter just in case.
        if piercing_defense:
            if omove == MOVE_DEFENSE:
                dmg_taken = 0
                logs.append(
                    f"  🎯 **Defense Pierce** — {mblade['name']} cuts through "
                    f"{oblade['name']}'s Defense! Counter NULLIFIED!"
                )
            elif dmg_taken > 0:
                # Shouldn't happen (pierce zeroes DEF so no counter is generated)
                # but guard it anyway.
                dmg_taken = 0

        # NOTE: stat_mult (level bonus) is now baked into the effective stats
        # dict built by session.py before resolve_pair is called.  Do NOT apply
        # it again here — doing so would double-scale the level bonus.

        # ── Passive flat damage reduction — skipped when piercing defense ──────
        if not piercing_defense:
            dmg_dealt, logs = self._apply_passive_reduction(okey, oblade, dmg_dealt, logs)

        # ── Type modifier — outgoing ATK bonus ────────────────────────────────
        dmg_dealt, logs = self._apply_atk_type_mod(mkey, mblade, mmove, dmg_dealt, logs, okey=okey)

        # ── Type modifier — incoming DEF mitigation — skipped when piercing ───
        if not piercing_defense:
            dmg_dealt, logs = self._apply_def_type_mod(okey, oblade, dmg_dealt, logs, mkey=mkey)

        # ── Avatar: every Nth strike carries a slice of total Attack ─────────
        if mmove == MOVE_ATTACK and dmg_dealt > 0:
            _nth, _nth_logs = AVC.nth_hit_bonus(
                self.session, mkey, mstats.get("attack", 0))
            if _nth:
                dmg_dealt += _nth
                logs.extend(_nth_logs)

        # ── Crit announcement ─────────────────────────────────────────────────
        if is_crit:
            logs.append(
                f"  💥 **CRITICAL HIT!** {mblade['name']} scores a crit! "
                f"(bypasses Shield Gate!)"
            )
            logs.extend(AVC.on_crit(self.session, mkey))

        # ── Defense Pierce announce (non-Defense move) ────────────────────────
        # For Attack vs Defense, the announce already fired above.
        # For Attack vs non-Defense moves, pierce stripped passive/type
        # reductions — announce it so the player sees the effect.
        if piercing_defense and omove != MOVE_DEFENSE:
            logs.append(
                f"  🎯 **Defense Pierce** — {mblade['name']} ignores "
                f"{oblade['name']}'s passive defenses!"
            )

        # ── Solar Flare bonus ─────────────────────────────────────────────────
        if mmove == MOVE_ATTACK and matchup in ("win", "lose"):
            sf_bonus, sf_logs = self.session.ability.consume_solar_flare(mkey, mblade)
            dmg_dealt += sf_bonus
            logs.extend(sf_logs)

        # ── Ability engine (rage, buffs, shields, chains, etc.) ───────────────
        dmg_dealt, dmg_taken, ab_logs = self.session.ability.apply(
            mkey, okey, mblade, oblade, mmove, matchup, dmg_dealt, dmg_taken
        )
        logs.extend(ab_logs)

        # ── Knockout chance bonus (generic proc for bonus true damage on hit) ─
        if dmg_dealt > 0 and mmove in (MOVE_ATTACK, MOVE_SPECIAL):
            for ab in self.session.ability._get_abilities(mblade, mkey):
                ko_chance = ab.get("knockout_chance_bonus", 0)
                if ko_chance > 0 and random.random() < ko_chance:
                    bonus_dmg = max(10, int(dmg_dealt * 0.25))  # 25% of hit as bonus true dmg
                    dmg_dealt += bonus_dmg
                    logs.append(
                        f"  💥 **{ab.get('name', 'Knockout')}** — "
                        f"{mblade['name']} lands a devastating blow! "
                        f"+**{bonus_dmg} bonus true damage**!"
                    )
                    break  # Only one KO bonus per hit

        # ── Enhanced stamina steal vs Stamina-type (Vanish Fafnir) ───────────
        logs = self._apply_extra_stamina_steal(mkey, okey, mmove, mblade, oblade, logs)

        # ── Gauge generation ──────────────────────────────────────────────────
        self._update_gauge(mkey, okey, mmove, dmg_dealt)

        # ── Stability costs ───────────────────────────────────────────────────
        stab = self.session.stability_manager
        if mmove == MOVE_ATTACK and stab.is_effects_active(mkey, okey):
            if matchup == "mirror":
                # Attack vs Attack: the clash penalty IS the whole cost
                # (-10 each, spec table). Applying the base attack cost on
                # top made mirrors drain -20/round → ring-out by round 5.
                # Guard with mkey < okey so the clash fires only once across
                # the two resolve_pair calls (p1→p2 and p2→p1).
                if mkey < okey:
                    logs.extend(stab.apply_clash_penalty(mkey, okey))
            else:
                # Attacker loses stability whether they hit or not
                logs.extend(stab.apply_attack_cost(mkey, hit=dmg_dealt > 0))
                # Counter: attacker got countered → attacker loses stability
                if dmg_taken > 0:
                    logs.extend(stab.apply_counter_penalty(mkey))

        return dmg_dealt, dmg_taken, matchup, logs

    # =========================================================================
    #  HP application + counter-announce (called after both pairs resolved)
    # =========================================================================

    def apply_pair_results(
        self,
        k1: str, k2: str,
        b1: dict, b2: dict,
        m1: str, m2: str,
        dmg_p1: int, counter_p1: int, matchup_p1: str,
        dmg_p2: int, counter_p2: int, matchup_p2: str,
    ) -> list[str]:
        """Apply all damage to hp dict and return log lines for this step.

        Handles:
          • Normal move damage (p1 → p2 and p2 → p1)
          • Counter-hit reflections
          • Attack-beats-Stamina counter announcements
        """
        logs: list[str] = []
        hp   = self.session.hp
        p1, p2 = self.session.players

        # ── Counter-beats-Stamina announcements ───────────────────────────────
        if m1 == MOVE_ATTACK and m2 == MOVE_STAMINA:
            logs.append(
                f"💥 **Counter!** {p1.display_name} attacks while "
                f"{p2.display_name} is recovering — "
                f"1.2× bonus damage applied! (total: **{dmg_p1}**)"
            )
        if m2 == MOVE_ATTACK and m1 == MOVE_STAMINA:
            logs.append(
                f"💥 **Counter!** {p2.display_name} attacks while "
                f"{p1.display_name} is recovering — "
                f"1.2× bonus damage applied! (total: **{dmg_p2}**)"
            )

        # ── Apply normal move damage ──────────────────────────────────────────
        # Avatar immortality is checked here, at the one place HP actually
        # drops, so it covers every damage source that funnels through the
        # round rather than just direct attacks.
        # Defender's avatar gets first refusal on the hit — dodge zeroes it,
        # a dodge can roll a counter, otherwise flat resistance shaves it down.
        # Immortality is checked last, on whatever actually got through.
        if m1 not in (MOVE_STAMINA, MOVE_CHARGE):
            dmg_p1, _ctr, _avl = AVC.absorb_incoming(self.session, k2, k1, dmg_p1)
            logs.extend(_avl)
            if _ctr:
                hp[k1] = max(0, hp[k1] - _ctr)
            dmg_p1, _imm = AVC.guard_lethal(self.session, k2, dmg_p1)
            logs.extend(_imm)
            hp[k2] = max(0, hp[k2] - dmg_p1)
        if m2 not in (MOVE_STAMINA, MOVE_CHARGE):
            dmg_p2, _ctr, _avl = AVC.absorb_incoming(self.session, k1, k2, dmg_p2)
            logs.extend(_avl)
            if _ctr:
                hp[k2] = max(0, hp[k2] - _ctr)
            dmg_p2, _imm = AVC.guard_lethal(self.session, k1, dmg_p2)
            logs.extend(_imm)
            hp[k1] = max(0, hp[k1] - dmg_p2)

        # ── Apply counter-hit reflections ─────────────────────────────────────
        # counter_pN is non-zero only for Attack-vs-Defense hits.
        # Guard against "mirror" matchup: in a mirror, calc_damage returns
        # dmg_taken = MIRROR_CHIP_DAMAGE as a symmetric value — that chip is
        # already applied in the normal damage block above, so applying it
        # again here would double the chip damage (Bug fix).
        #
        # IMPORTANT: counter_p1 and counter_p2 are mutually exclusive in a
        # correctly resolved round.  If P1 attacked into P2's Defense then
        # counter_p1 > 0; P2 cannot simultaneously have attacked into P1's
        # Defense (P2 was on Defense, not Attack).  The explicit mutual-
        # exclusion guard below makes this guarantee enforced rather than
        # implicit, preventing any future code path from double-counting.
        if counter_p1 > 0 and counter_p2 > 0:
            # Defensive sanity check — should never happen in normal play.
            # If both counters are somehow non-zero, prefer the larger one
            # and discard the other to avoid double damage.
            if counter_p1 >= counter_p2:
                counter_p2 = 0
            else:
                counter_p1 = 0

        if counter_p1 > 0 and matchup_p1 != "mirror":
            hp[k1] = max(0, hp[k1] - counter_p1)
            logs.append(
                f"  🔰 **Counter!** {b1['name']} hits {b2['name']}'s Defense — "
                f"**{counter_p1} dmg** reflected back at {b1['name']}!"
            )
        if counter_p2 > 0 and matchup_p2 != "mirror":
            hp[k2] = max(0, hp[k2] - counter_p2)
            logs.append(
                f"  🔰 **Counter!** {b2['name']} hits {b1['name']}'s Defense — "
                f"**{counter_p2} dmg** reflected back at {b2['name']}!"
            )

        return logs

    # =========================================================================
    #  Round summary builder
    # =========================================================================

    def build_round_summary(
        self,
        p1: "discord.Member",
        p2: "discord.Member",
        k1: str,
        k2: str,
        m1: str,
        m2: str,
        dmg_p1: int,
        dmg_p2: int,
        matchup_p1: str,
        matchup_p2: str,
        extra_logs: list[str],
    ) -> list[str]:
        """Build the compact two-column round results block for Discord embeds.

        extra_logs are automatically cleaned by log_formatter.format_battle_logs()
        to deduplicate identical messages and group repeated ability effects.
        This keeps the battle log concise and readable.

        Returns a list of lines that session.py joins into an embed description.
        Layout:
          ┌──────────────────────────────────┐
          │  Round N  ·  🔴 Name  vs  🔵 Name │
          │  🔴 [move icon] Move   🔵 [move]  │
          │  ❤️ dmg dealt         ❤️ dmg dealt│
          │  ── effects / extra logs ──────── │
          │  ❤️ HP left           ❤️ HP left  │
          └──────────────────────────────────┘
        """
        # ── Move label helpers ────────────────────────────────────────────────
        MOVE_SHORT = {
            MOVE_ATTACK:  "Attack",
            MOVE_DEFENSE: "Defense",
            MOVE_STAMINA: "Stamina",
            MOVE_SPECIAL: "Special",
            MOVE_CHARGE:  "Charge",
        }

        import re as _re
        _EMOJI = _re.compile(
            "["
            "\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF"
            "\uFE0F\u200D\u2049\u203C\u20E3"
            "]+"
        )

        def _clean(line: str) -> str:
            """Strip emoji clutter so log lines read like plain sentences."""
            return _EMOJI.sub("", line).strip(" —-·")

        def _move_block(member, key: str, move: str, dmg: int, matchup: str) -> list[str]:
            blade = self.session.blades[key].get("name", member.display_name)
            out = [f"**{blade} used {MOVE_SHORT.get(move, move)}!**"]
            if move in (MOVE_CHARGE,):
                out.append("Special gauge charged up!")
            elif move == MOVE_STAMINA:
                out.append("It braced and recovered stamina!")
            elif dmg > 0:
                if matchup == "win":
                    out.append(f"It's super effective! Dealt **{dmg}** damage!")
                elif matchup == "mirror":
                    out.append(f"They clashed head-on! **{dmg}** damage!")
                else:
                    out.append(f"It was resisted... **{dmg}** damage.")
            else:
                out.append("But it was blocked!")
            return out

        lines: list[str] = []
        b1n = self.session.blades[k1].get("name", p1.display_name)
        b2n = self.session.blades[k2].get("name", p2.display_name)

        # ── Classify extra logs per blade, extract cost lines ─────────────────
        _STA_RE  = _re.compile(r"stamina cost: (-[\d.]+)\s*→\s*`?([\d.]+)`?")
        _STAB_RE = _re.compile(r"stability ([+-]\d+).*?→\s*`?(\d+)`?")

        buckets: dict[str, list[str]] = {b1n: [], b2n: [], "": []}
        costs: dict[str, dict] = {b1n: {}, b2n: {}}
        cleaned = [_clean(l) for l in format_battle_logs(extra_logs or [])]
        w1 = b1n.split()[0] if b1n else ""
        w2 = b2n.split()[0] if b2n else ""
        def _owner(line: str) -> str:
            if b1n in line: return b1n
            if b2n in line: return b2n
            # first-word fallback (e.g. "Lord's Decree" → Lord Spriggan),
            # only when the two blades' first words differ
            if w1 != w2 and len(w1) > 3 and w1 in line: return b1n
            if w1 != w2 and len(w2) > 3 and w2 in line: return b2n
            return ""

        for line in cleaned:
            if not line:
                continue
            owner = _owner(line)
            if owner:
                ms = _STA_RE.search(line)
                mb = _STAB_RE.search(line)
                if ms:
                    c = costs[owner]
                    c["sta_d"] = c.get("sta_d", 0) + float(ms.group(1))
                    c["sta_v"] = ms.group(2)
                    continue
                if mb:
                    c = costs[owner]
                    c["stab_d"] = c.get("stab_d", 0) + int(mb.group(1))
                    c["stab_v"] = mb.group(2)
                    continue
            buckets[owner].append(line)

        def cost_line(name: str) -> str:
            c = costs[name]
            parts = []
            if "stab_d" in c:
                parts.append(f"Stability {c['stab_d']:+d} → {c['stab_v']}")
            if "sta_d" in c:
                parts.append(f"Stamina {c['sta_d']:+g} → {c['sta_v']}")
            return "  •  ".join(parts)

        # ── Player 1 block ─────────────────────────────────────────────────────
        lines += _move_block(p1, k1, m1, dmg_p1, matchup_p1)
        lines += buckets[b1n]
        cl = cost_line(b1n)
        if cl:
            lines.append(f"-# {cl}")
        lines.append("")

        # ── Player 2 block ─────────────────────────────────────────────────────
        lines += _move_block(p2, k2, m2, dmg_p2, matchup_p2)
        lines += buckets[b2n]
        cl = cost_line(b2n)
        if cl:
            lines.append(f"-# {cl}")

        # ── Shared / unattributed lines ────────────────────────────────────────
        if buckets[""]:
            lines.append("")
            lines += buckets[""]

        return lines

    # =========================================================================
    #  Private helpers
    # =========================================================================

    def _resolve_special(
        self,
        mkey:   str,
        okey:   str,
        mmove:  str,
        mblade: dict,
        oblade: dict,
        logs:   list[str],
    ) -> tuple[int, list[str]]:
        """Resolve a MOVE_SPECIAL: multi-hit loop with per-hit procs."""
        from cogs.abilities.type_system import resolve_active_bonuses
        sm         = self.session.stamina_manager
        ab_eng     = self.session.ability
        # The effective special stat scales the authored damage — this is what
        # makes a Special grow with the bey's level. Absent (older sessions,
        # tests) it resolves to the printed damage exactly.
        _spc = getattr(self.session, "special_stats", {}).get(mkey)
        hits, per_hit, flavour, ignores_def = resolve_special(mblade, _spc)
        # Abilities can grant extra hits (e.g. a max-stack payoff). Consumed
        # here so it applies to exactly one Special, then clears.
        _extra = 0
        try:
            _extra = int(getattr(ab_eng, "extra_special_hits", {}).pop(mkey, 0) or 0)
        except Exception:
            _extra = 0
        if _extra > 0:
            hits += _extra
            logs.append(
                f"  ⚔️ **Max Stacks** — +{_extra} bonus hit"
                f"{'s' if _extra > 1 else ''} ({_extra * per_hit} extra damage)!"
            )
        # ── Avatar multi-hit modifiers ───────────────────────────────────────
        hits, per_hit, _mh_logs = AVC.multi_hit_shape(
            self.session, mkey, hits, per_hit)
        logs.extend(_mh_logs)

        mult       = self.session.stat_mult.get(mkey, 1.0)
        atk_sp_mod = self.session.type_mods.get(mkey)

        # Resolve type-advantage gate once for the whole special sequence
        _atk_mod_obj  = atk_sp_mod
        _def_mod_obj  = self.session.type_mods.get(okey)
        _atk_type     = _atk_mod_obj.btype if _atk_mod_obj else ""
        _def_type     = _def_mod_obj.btype if _def_mod_obj else ""
        _sp_atk_active, _sp_def_active = resolve_active_bonuses(_atk_type, _def_type)
        # Suppress mods that aren't active this matchup
        if not _sp_atk_active:
            atk_sp_mod = None

        # ── Honor active defense-pierce turns for Special moves ──────────────
        # preprocess_defender_stats is bypassed for SPECIAL, so we check and
        # consume ignore_defense_turns here manually.  If the attacker has an
        # active pierce buff, the Special also ignores defense (and the turns
        # are decremented so the buff doesn't last forever).
        pierce_logged = False
        pierce_turns = self.session.status.get_duration("ignore_defense_turns", mkey)
        if pierce_turns > 0:
            ignores_def = True
            # Decrement directly — set_duration() is a max-merge and can't reduce
            d = self.session.status.ignore_defense_turns
            d[mkey] = max(0, pierce_turns - 1)
            logs.append(
                f"  🎯 **Defense Pierce** — {mblade['name']} Special IGNORES all defense!"
            )
            pierce_logged = True

        if flavour:
            logs.append(f"  🌟 {flavour[0]}")
        if hits > 1:
            logs.append(f"  ⚡ **{hits}-hit barrage begins!**")
        # Only log native ignores_def here; the buff branch above already logged
        # when pierce_logged=True, so skip to avoid printing the same line twice.
        if ignores_def and not pierce_logged:
            logs.append(
                f"  🎯 **{mblade['name']}** — Special IGNORES all defense!"
            )

        # Blades in SELF_MANAGED_HITS apply their own hits directly to
        # session.hp inside _on_special / apply_new_special.  The outer loop
        # still runs so per-hit procs, gauges, and ability.apply() side-effects
        # (buffs, silences) fire normally — but we skip adding hit_dmg to
        # total_dmg and skip the stamina gauge for "dmg_taken" to avoid
        # double-counting damage that was already deducted from hp.
        self_managed = mblade.get("name", "") in SELF_MANAGED_HITS

        # Avatar Special bonuses are applied once to the finished total rather
        # than per hit, so a 5-hit Special isn't multiplied five times over.
        _ult_atk = 0
        try:
            _ult_atk = self.session.battle_stats.get(mkey, {}).get("attack", 0)
        except Exception:
            _ult_atk = mblade.get("stats", {}).get("attack", 0)

        total_dmg = 0
        for hit_n in range(hits):
            hit_base = math.ceil(per_hit * mult)
            if atk_sp_mod:
                hit_base = atk_sp_mod.apply_attack(hit_base)

            # Per-hit proc (on_hit abilities — Reckless Fury etc.)
            hit_base, proc_logs = self.session.ability.process_hit_proc(
                mkey, mblade, okey, hit_base
            )
            logs.extend(proc_logs)

            # is_first_hit=True only on the first hit (controls buff ticking and
            # on_special firing).  is_last_hit=True only on the final hit so
            # revival and KO-resist project the full cumulative damage correctly.
            is_first = hit_n == 0
            is_last  = hit_n == hits - 1

            hit_dmg, _, ab_logs = self.session.ability.apply(
                mkey, okey, mblade, oblade, MOVE_SPECIAL, "win", hit_base, 0,
                is_first_hit=is_first,
                cumulative_dmg=total_dmg,
                is_last_hit=is_last,
            )

            # Passive flat damage reduction (Dead Phoenix / Undying Blaze)
            # Skipped if the Special explicitly ignores defense
            if not ignores_def:
                hit_dmg, logs = self._apply_passive_reduction(okey, oblade, hit_dmg, logs)

            # Type defense mitigation (skipped if special pierces defense or
            # defender's type bonus is not active for this matchup)
            def_sp_mod = self.session.type_mods.get(okey) if _sp_def_active else None
            if def_sp_mod and not ignores_def:
                hit_dmg = def_sp_mod.apply_defense(hit_dmg)

            # Gauge — defender gains from taking damage; attacker does NOT gain
            # gauge from their own Special move (they just consumed the full
            # gauge to fire it — awarding per-hit "attack" gauge immediately
            # refunds a chunk of what was spent, effectively making multi-hit
            # Specials cheaper than single-hit ones).

            if self_managed:
                # Handler already wrote to session.hp — only track gauges/logs,
                # don't add to total_dmg (session HP was already reduced).
                logs.extend(ab_logs)
                continue

            total_dmg += hit_dmg

            if hit_dmg > 0:
                sm.add_gauge(okey, "dmg_taken")

            logs.extend(ab_logs)
            if hits > 1:
                logs.append(f"  💥 **Hit {hit_n + 1}/{hits}** — **{hit_dmg} dmg**")

        # ── Avatar: Special multiplier + full Attack stat rider ──────────────
        if not self_managed and total_dmg > 0:
            total_dmg, _ult_logs = AVC.apply_ult_bonus(
                self.session, mkey, total_dmg, _ult_atk)
            logs.extend(_ult_logs)

        if hits > 1:
            logs.append(f"  🔥 **Total Special Damage: {total_dmg}**")

        return total_dmg, logs

    def _apply_passive_reduction(
        self,
        okey:     str,
        oblade:   dict,
        dmg_dealt: int,
        logs:     list[str],
    ) -> tuple[int, list[str]]:
        """Apply flat passive damage reduction from defender's abilities."""
        for _oab in self.session.ability._get_abilities(oblade):
            _red = _oab.get("passive_damage_reduction", 0)
            if _red and dmg_dealt > 0:
                dmg_dealt = max(0, dmg_dealt - _red)
                logs.append(
                    f"  🦅 **{_oab.get('name', 'Passive')}** — "
                    f"{oblade['name']} reduces incoming damage by **{_red}**!"
                )
                break  # only one reduction per hit
        return dmg_dealt, logs

    def _apply_atk_type_mod(
        self,
        mkey:     str,
        mblade:   dict,
        mmove:    str,
        dmg_dealt: int,
        logs:     list[str],
        okey:     str = "",
    ) -> tuple[int, list[str]]:
        """Apply outgoing Attack-type stat modifier.

        Gated behind type-advantage: the attacker's bonus only activates when
        resolve_active_bonuses() says their side is active for this matchup.
        Mirror matchups (attack vs attack, defense vs defense, stamina vs
        stamina) return (False, False) so neither side gets a bonus.
        """
        from cogs.abilities.type_system import resolve_active_bonuses
        atk_type_mod = self.session.type_mods.get(mkey)
        if atk_type_mod and dmg_dealt > 0 and mmove == MOVE_ATTACK:
            # Resolve advantage gate
            enemy_mod  = self.session.type_mods.get(okey)
            enemy_type = enemy_mod.btype if enemy_mod else ""
            a_active, _ = resolve_active_bonuses(atk_type_mod.btype, enemy_type)
            if not a_active:
                return dmg_dealt, logs
            pre = dmg_dealt
            dmg_dealt = atk_type_mod.apply_attack(dmg_dealt)
            if dmg_dealt != pre:
                logs.append(
                    f"  ⚔️ **Type Bonus ({atk_type_mod.btype.title()})** — "
                        f"{mblade['name']} deals +**{dmg_dealt - pre}** bonus dmg!"
                    )
        return dmg_dealt, logs

    def _apply_def_type_mod(
        self,
        okey:     str,
        oblade:   dict,
        dmg_dealt: int,
        logs:     list[str],
        mkey:     str = "",
    ) -> tuple[int, list[str]]:
        """Apply incoming Defense-type stat modifier.

        Gated behind type-advantage: the defender's bonus only activates when
        resolve_active_bonuses() says their side (b_active) is active for
        this matchup.  Mirror matchups suppress both sides.
        """
        from cogs.abilities.type_system import resolve_active_bonuses
        def_type_mod = self.session.type_mods.get(okey)
        if def_type_mod is not None and dmg_dealt > 0:
            atk_mod    = self.session.type_mods.get(mkey)
            atk_type   = atk_mod.btype if atk_mod else ""
            _, b_active = resolve_active_bonuses(atk_type, def_type_mod.btype)
            if not b_active:
                return dmg_dealt, logs
            pre = dmg_dealt
            dmg_dealt = def_type_mod.apply_defense(dmg_dealt)
            if dmg_dealt != pre:
                logs.append(
                    f"  🛡️ **Type Mitigation ({def_type_mod.btype.title()})** — "
                    f"{oblade['name']} reduces damage by **{pre - dmg_dealt}**!"
                )
        return dmg_dealt, logs

    def _apply_extra_stamina_steal(
        self,
        mkey:   str,
        okey:   str,
        mmove:  str,
        mblade: dict,
        oblade: dict,
        logs:   list[str],
    ) -> list[str]:
        """Apply enhanced stamina steal vs Stamina-type opponents (Vanish Fafnir)."""
        if mmove not in (MOVE_ATTACK, MOVE_DEFENSE):
            return logs

        # Check if opponent is stamina type and attacker has enhanced steal
        otype = str(oblade.get("type", "")).lower()
        if "stamina" not in otype:
            return logs

        for ab in self.session.ability._get_abilities(mblade):
            extra_steal = ab.get("enhanced_stamina_steal_vs_stamina_type", 0)
            if extra_steal:
                sta = self.session.stamina_manager.stamina
                sta[mkey] = min(15, sta.get(mkey, 0) + extra_steal)
                sta[okey] = max(0, sta.get(okey, 0) - extra_steal)
                logs.append(
                    f"  🌀 **{ab.get('name', 'Enhanced Steal')}** — "
                    f"{mblade['name']} drains **{extra_steal} extra Stamina** from "
                    f"Stamina-type {oblade['name']}!"
                )
                break
        return logs

    def _update_gauge(
        self,
        mkey:   str,
        okey:   str,
        mmove:  str,
        dmg_dealt: int,
    ) -> None:
        """Update special gauge based on the move and damage dealt.

        NOTE: MOVE_STAMINA and MOVE_CHARGE gauge additions are handled by
        session._resolve_round_inner before resolve_pair is called — do NOT
        add them here or the player gets double gauge credit.
        MOVE_SPECIAL gauge is added per-hit inside _resolve_special — skip here.
        """
        sm = self.session.stamina_manager

        if mmove == MOVE_ATTACK:
            sm.add_gauge(mkey, "attack")
        elif mmove == MOVE_DEFENSE:
            sm.add_gauge(mkey, "defense")
        # MOVE_STAMINA: gauge handled by session.py (sm.add_gauge "stamina")
        # MOVE_CHARGE:  gauge handled by session.py (sm.add_gauge "charge")
        # MOVE_SPECIAL: gauge handled per-hit in _resolve_special

        # Defender gains gauge from taking damage (attack only)
        if dmg_dealt > 0 and mmove == MOVE_ATTACK:
            sm.add_gauge(okey, "dmg_taken")
