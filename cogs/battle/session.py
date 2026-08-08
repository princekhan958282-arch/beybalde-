"""
battle/session.py
-----------------
BattleSession: orchestrates one live battle between two Discord members.

Responsibilities:
  - Own the canonical HP dict
  - Coordinate StaminaManager, AbilityEngine, AttackManager, and TypeModifiers
  - Drive per-round resolution (move collection → damage → gauge → status)
  - Trigger battle-end rewards (XP, coins, rank)

NO ability primitives, stamina cost arithmetic, damage formulas, or
type modifier math belong here — those are fully delegated to the
appropriate sub-module.
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import Optional, Any

import discord
from discord.ext import commands

from .constants import (
    BASE_HP, BATTLE_TIMEOUT, SPECIAL_GAUGE_MAX,
    MOVE_ATTACK, MOVE_DEFENSE, MOVE_STAMINA, MOVE_SPECIAL, MOVE_CHARGE,
    MOVE_LABELS,
    COINS_WIN, COINS_LOSS,
    GAUGE_PER_CHARGE,
)
from cogs.abilities.type_system import TypeModifiers
from .attrition import AttritionSystem
from .stamina_manager import StaminaManager, STAMINA_MAX
from .stability_manager import StabilityManager
from cogs.abilities.ability_engine import AbilityEngine
from .attack_manager import AttackManager
from .defense_manager import DefenseManager
from .status_manager import StatusManager      # FIX #1/#3: Added import
from .chain_handler import ChainHandler        # FIX #1/#3: Added import

from cogs.avatar import avatar_engine
from utils import bey_levels as _BL


def _bey_xp(profile: dict, blade: Optional[dict], won: bool) -> Optional[dict]:
    """Grant battle EXP to the blade that fought. Mutates `profile`.

    Skipped for an equipped boss copy: copies are fixed rolls and don't level.
    Never raises — a bad blade dict must not cost someone their match result.
    """
    try:
        if not blade or profile.get("active_copy"):
            return None
        name = blade.get("name")
        if not name:
            return None
        lo, hi = _BL.XP_BATTLE_WIN if won else _BL.XP_BATTLE_LOSS
        return _BL.award(profile, name, random.randint(lo, hi))
    except Exception:                                # noqa: BLE001
        return None


def _level_hp_gain(user_id, blade: Optional[dict]) -> int:
    """Extra HP this bey has earned from its level.

    max_hp_for_blade() runs the HP stat through hp_system.blade_hp_stat(), which
    CLAMPS it into the blade's type band (80-139). That clamp is right for a
    printed stat — it is what keeps the roster in class — but it also threw away
    every point of levelled HP: a level-100 blade with a levelled HP stat of 280
    was clamped back to 130, so bey HP growth did nothing at all in PvP. Boss
    and Story fights never had this bug because they compose HP through
    loadout.effective_blade instead, which is why the same bey was tougher there.

    Adding the level GAIN on top of the clamped printed stat keeps the type band
    doing its job while letting levels matter, and matches how
    boss_battle._player_fighter already does it.

    Never raises: a missing profile must not stop a battle starting.
    """
    try:
        from utils.loadout import effective_blade
        _eff, breakdown, _av = effective_blade(int(user_id), blade=blade)
        hp_bd = (breakdown or {}).get("hp") or {}
        gain = float(hp_bd.get("total", 0)) - float(hp_bd.get("base", 0))
        return int(gain) if gain > 0 else 0
    except Exception:                                # noqa: BLE001
        return 0


def _effective_special(user_id, blade: Optional[dict]) -> int:
    """This player's SPECIAL stat with level, parts and avatar folded in.

    Falls back to the blade's printed value, which makes resolve_special's
    scale exactly 1.0 — i.e. the Special behaves as it always has. A broken
    profile must never stop a battle starting.
    """
    printed = int(((blade or {}).get("stats") or {}).get("special", 0) or 0)
    try:
        from utils.loadout import effective_blade
        eff, _breakdown, _av = effective_blade(int(user_id), blade=blade)
        return int((eff.get("stats") or {}).get("special", printed) or printed)
    except Exception:                                # noqa: BLE001
        return printed

from utils.database import (
    get_user, update_user, grant_xp,
    level_from_xp, xp_to_next_level, MAX_LEVEL,
    XP_WIN, XP_LOSS, get_stat_multiplier,
)
from utils.embeds import rarity_colour, RARITY_EMOJIS, hp_bar, level_badge
from utils.hp_system import max_hp_for_blade, blade_hp_stat
# apply_win / apply_loss are no longer imported here: rank score is now moved
# only by utils.ranked, which owns the ranked-vs-casual decision. Two callers
# able to change the same score is how a casual battle would leak onto the
# ladder again.
from utils.ranks import tier_for_score, rank_score_for, WIN_SCORE, LOSS_SCORE



# ── Per-round move selection panel ───────────────────────────────────────────

class _InChannelControlPanel(discord.ui.View):
    """Five-button move panel posted each round. Any player in the battle
    can click their move; duplicate clicks are rejected inside submit_move()."""

    def __init__(self, session: "BattleSession") -> None:
        super().__init__(timeout=BATTLE_TIMEOUT)
        self.session = session

    def _special_disabled(self, user_id: str) -> bool:
        """Return True if the special gauge is not full for this player."""
        gauge = self.session.stamina_manager.gauge.get(user_id, 0)
        return gauge < SPECIAL_GAUGE_MAX

    async def _handle(self, interaction: discord.Interaction, move: str) -> None:
        if not self.session.is_player(interaction.user):
            await interaction.response.send_message(
                "❌ You are not in this battle!", ephemeral=True
            )
            return
        await self.session.submit_move(interaction, interaction.user, move)

    @discord.ui.button(label="⚔️ Attack", style=discord.ButtonStyle.danger, row=0)
    async def btn_attack(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle(interaction, MOVE_ATTACK)

    @discord.ui.button(label="🛡️ Defense", style=discord.ButtonStyle.primary, row=0)
    async def btn_defense(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle(interaction, MOVE_DEFENSE)

    @discord.ui.button(label="⚡ Stamina", style=discord.ButtonStyle.success, row=0)
    async def btn_stamina(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle(interaction, MOVE_STAMINA)

    @discord.ui.button(label="🔋 Charge", style=discord.ButtonStyle.secondary, row=1)
    async def btn_charge(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle(interaction, MOVE_CHARGE)

    @discord.ui.button(label="🌟 SPECIAL", style=discord.ButtonStyle.secondary, row=1)
    async def btn_special(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        uid = str(interaction.user.id)
        if self._special_disabled(uid):
            await interaction.response.send_message(
                f"❌ Special gauge not full! "
                f"({self.session.stamina_manager.gauge.get(uid, 0)}/{SPECIAL_GAUGE_MAX})",
                ephemeral=True,
            )
            return
        await self._handle(interaction, MOVE_SPECIAL)

    async def on_timeout(self) -> None:
        """Called by discord.py when the view timer expires (BATTLE_TIMEOUT seconds).

        Find which player(s) haven't submitted a move and force-forfeit them so
        the battle ends cleanly instead of hanging forever.
        """
        session = self.session
        if session.finished:
            return

        # Identify who hasn't chosen yet
        missing = [
            p for p in session.players
            if session.moves.get(str(p.id)) is None
        ]

        if not missing:
            # Both submitted but round resolution hasn't fired yet — shouldn't
            # happen, but trigger it just in case.
            asyncio.get_running_loop().create_task(session._resolve_round())
            return

        # Force HP to 0 for each AFK player — _end_battle picks the winner
        for p in missing:
            session.hp[str(p.id)] = 0

        names = " & ".join(p.display_name for p in missing)
        try:
            await session.channel.send(
                f"⏱️ **Time's up!** {names} didn't choose a move in time — "
                f"they forfeit the round!"
            )
        except Exception:
            pass

        try:
            await session._end_battle()
        except Exception:
            pass


# ── Win streak bonus table ─────────────────────────────────────────────────────
# 3 wins → +100, 5 → +250, 10 → +600, then every 5 wins after 10 → +600 again.
_STREAK_BONUSES = {3: 100, 5: 250, 10: 600}


def _streak_bonus(streak: int) -> int:
    if streak in _STREAK_BONUSES:
        return _STREAK_BONUSES[streak]
    if streak > 10 and streak % 5 == 0:
        return 600
    return 0


class BattleSession:
    def __init__(
        self,
        bot:     commands.Bot,
        channel: discord.TextChannel,
        p1:      discord.Member,
        p2:      discord.Member,
        blade1:  dict,
        blade2:  dict,
        ranked:  bool = False,
    ):
        self.bot     = bot
        self.channel = channel
        self.players = [p1, p2]
        self.blades  = {str(p1.id): blade1, str(p2.id): blade2}
        # Defaults to False so every existing caller — tournament matches
        # included — keeps producing casual results until it opts in. A ladder
        # that counted friendly matches would let two players trade wins to
        # farm rank score, and a win rate including practice games measures
        # nothing.
        self.ranked  = bool(ranked)

        # ── Per-blade HP pools ────────────────────────────────────────────────
        # Max HP builds in four additive layers, in this order:
        #   1. BASE_HP           (global floor)
        #   2. + blade HP stat   (80–139, from beyblades.json)
        #   3. + bey LEVEL gain  (see _level_hp_gain)
        #   4. + avatar HP boost (flat + %, applied just below)
        # Every % threshold / heal / damage number downstream stays valid
        # because only the pool size changes.
        self.hp = {
            str(p1.id): max_hp_for_blade(blade1) + _level_hp_gain(p1.id, blade1),
            str(p2.id): max_hp_for_blade(blade2) + _level_hp_gain(p2.id, blade2),
        }
        # Insurance alias: any code that reads session.max_hp (e.g. win_system)
        # resolves to a real number instead of raising AttributeError. Prefer
        # max_hp_per_player — this is only the fallback.
        self.max_hp  = BASE_HP

        # ── Avatar bonuses (loaded once at battle start) ──────────────────────
        # Stored on session so AbilityEngine and other managers can read them.
        self.avatar_bonuses: dict[str, Any] = {
            str(p1.id): avatar_engine.get_battle_bonuses(p1.id),
            str(p2.id): avatar_engine.get_battle_bonuses(p2.id),
        }
        # Apply avatar HP bonuses on top of the blade's own pool
        for _pid, _bonuses in self.avatar_bonuses.items():
            if _bonuses.has_any_bonus:
                self.hp[_pid] = int(_bonuses.apply_hp_bonus(self.hp[_pid]))
        # max_hp per-player so HP bars scale correctly
        self.max_hp_per_player: dict[str, int] = dict(self.hp)

        # ── Parts stat deltas (loaded once at battle start) ───────────────────
        # Each player's equipped parts contribute flat stat bonuses/penalties that
        # apply to every round alongside ability buffs and the level multiplier.
        try:
            from cogs.economy.shop import get_part_stat_deltas
            _p1_prof = get_user(p1.id)
            _p2_prof = get_user(p2.id)
            self.part_deltas: dict[str, dict[str, int]] = {
                str(p1.id): get_part_stat_deltas(_p1_prof.get("equipped_parts", [])),
                str(p2.id): get_part_stat_deltas(_p2_prof.get("equipped_parts", [])),
            }
        except Exception:
            self.part_deltas = {str(p1.id): {}, str(p2.id): {}}

        # ── Battle-start effective stats (base + parts + avatar + level mult) ─
        # StaminaManager needs MODIFIED stats (not raw base) so starting
        # stamina, stamina heal, and spin-finish scale with the player's real
        # boosted stats. Mirrors _effective_stats but without round buffs
        # (buffs are always 0 at battle start).
        def _start_stats(pid: str, blade: dict) -> dict:
            base = dict(blade.get("stats", {}))
            try:
                mult = get_stat_multiplier(int(pid), blade.get("name"))
            except Exception:
                mult = 1.0
            pd  = self.part_deltas.get(pid, {})
            raw = {
                s: (base.get(s, 0) + pd.get(s, 0)) * mult
                for s in ("attack", "defense", "stamina")
            }
            av = self.avatar_bonuses.get(pid)
            # Floor at 0 — part penalties can push a stat negative, which
            # breaks stamina/counter/crit math downstream.
            if av and av.has_any_bonus:
                return {
                    "attack":  max(0, int(av.apply_attack_bonus(raw["attack"]))),
                    "defense": max(0, int(av.apply_defence_bonus(raw["defense"]))),
                    "stamina": max(0, int(av.apply_stamina_bonus(raw["stamina"]))),
                }
            return {k: max(0, int(v)) for k, v in raw.items()}

        self.battle_stats: dict[str, dict[str, int]] = {
            str(p1.id): _start_stats(str(p1.id), blade1),
            str(p2.id): _start_stats(str(p2.id), blade2),
        }

        # ── Effective SPECIAL stat (levelled + parts + avatar) ────────────────
        # Resolved once at battle start and handed to damage_rules.resolve_special
        # so a Special scales with the bey's level instead of being the flat
        # number printed in beyblades.json. Read through loadout.effective_blade
        # because that is the only place bey levels are folded in — the stats on
        # `self.blades` are the printed ones.
        #
        # Not part of battle_stats/_effective_stats: those are recomputed every
        # round for the attack/defence/stamina exchange, and a Special's scale
        # has no per-round buff to track.
        self.special_stats: dict[str, int] = {
            str(p1.id): _effective_special(p1.id, blade1),
            str(p2.id): _effective_special(p2.id, blade2),
        }

        # ── Sub-module initialisation ─────────────────────────────────────────
        self.stamina_manager = StaminaManager(self.blades, effective_stats=self.battle_stats)
        self.type_mods: dict[str, TypeModifiers] = {
            str(p1.id): TypeModifiers(blade1, stats=self.battle_stats[str(p1.id)]),
            str(p2.id): TypeModifiers(blade2, stats=self.battle_stats[str(p2.id)]),
        }
        self.stability_manager = StabilityManager(self.blades, self.type_mods)
        self.status = StatusManager(self)          # FIX #1/#3: Initialize StatusManager
        self.chain_handler = ChainHandler(self)    # FIX #1/#3: Initialize ChainHandler
        self.ability = AbilityEngine(self)
        self.attack_manager  = AttackManager(self)
        self.defense_manager = DefenseManager(self)

        # ── Battle-start setup (initial_shield, initial_defense_buff, etc.) ──
        # Must run AFTER AbilityEngine is constructed so grant_shield / _add_buf
        # have a valid engine to write into.
        _setup_log: list[str] = []
        for _key, _blade in self.blades.items():
            _setup_log.extend(self.ability.setup(_key, _blade))
        self._setup_log = _setup_log   # stored so run() can announce them

        # ── Convenience aliases (keep existing attribute names working) ───────
        # stamina is read/written by AbilityEngine via session.stamina_manager.stamina
        # but many log paths use self.stamina[key] — expose it as a property alias.

        self.stat_mult: dict[str, float] = {
            str(p1.id): get_stat_multiplier(
                p1.id, self.blades.get(str(p1.id), {}).get("name")),
            str(p2.id): get_stat_multiplier(
                p2.id, self.blades.get(str(p2.id), {}).get("name")),
        }

        self.moves:      dict[str, Optional[str]] = {str(p1.id): None, str(p2.id): None}
        self.round:    int  = 1
        # Forces long matches to resolve — see cogs/battle/attrition.py
        self.attrition = AttritionSystem(self)
        self.log:      list[str] = []
        self.finished: bool = False

        # How this fight ended, and for whom. Set at whichever of the three
        # sites fires first; a fight can only end once, so the first mark wins
        # and later ones are ignored rather than overwriting it.
        #
        # All three sites already collapsed to `hp[key] = 0`, which made every
        # ending look identical downstream — the result embed said "knocked
        # out" whether the blade burst, ran out of spin, or was flung from the
        # ring. Ranked scoring needs to tell them apart, and so does anyone
        # reading a normal battle.
        self.finish_type: dict[str, str] = {}   # loser key -> finish kind
        # Set once the fight resolves, so a caller running a multi-round match
        # can score the round without re-deriving which player lost.
        self.last_finish: str = ""

        self.panel_msg: Optional[discord.Message] = None
        self._current_view: Optional[_InChannelControlPanel] = None
        self._done_event   = asyncio.Event()
        self._resolve_lock = asyncio.Lock()

    # ── Convenience property so legacy code can read self.stamina[key] ────────
    @property
    def stamina(self) -> dict[str, float]:
        return self.stamina_manager.stamina

    # ── Helpers ───────────────────────────────────────────────────────────────

    def is_player(self, user: discord.Member) -> bool:
        return any(p.id == user.id for p in self.players)

    def other_player(self, player: discord.Member) -> discord.Member:
        return self.players[1] if player.id == self.players[0].id else self.players[0]

    def _status_embed(self, title: str = "") -> discord.Embed:
        p1, p2 = self.players
        k1, k2 = str(p1.id), str(p2.id)
        b1, b2 = self.blades[k1], self.blades[k2]
        sm     = self.stamina_manager
        st     = self.status                      # FIX #1/#3: Use self.status (not self.session.status)
        ab     = self.ability

        # ── Visual bar helpers ────────────────────────────────────────────────
        def hp_visual(current: int, maximum: int = BASE_HP, length: int = 8) -> str:
            """Segmented HP bar using block chars: full=█  half=▓  empty=░"""
            pct    = max(0.0, current / maximum)
            filled = round(pct * length)
            if pct > 0.55:
                bar_char = "█"
            elif pct > 0.25:
                bar_char = "▓"
            else:
                bar_char = "▒"
            return bar_char * filled + "░" * (length - filled)

        def sta_bar(val: float, max_val: float = STAMINA_MAX, length: int = 6) -> str:
            filled = min(length, round((val / max_val) * length))
            return "⚡" * filled + "▫️" * (length - filled)

        def gauge_bar(val: float, max_val: int = SPECIAL_GAUGE_MAX, length: int = 6) -> str:
            filled = min(length, round((val / max_val) * length))
            return "🔵" * filled + "⬛" * (length - filled)

        def stability_bar(val: int, max_val: int, length: int = 6) -> str:
            pct    = min(1.0, max(0.0, val / max_val)) if max_val else 0.0
            filled = min(length, max(0, round(pct * length)))
            if pct > 0.55:   char = "🟩"
            elif pct > 0.25: char = "🟨"
            else:             char = "🟥"
            return char * filled + "⬜" * (length - filled)

        # ── Effects — compact single-line tags per effect ─────────────────────
        def active_effects(key: str) -> list[str]:
            """Return a list of compact effect tag strings (one per effect)."""
            dm   = self.defense_manager
            tags = []

            atk_buff  = sum(b["amount"] for b in st.active_buffs.get(key, []) if b["stat"] == "attack")
            def_buff  = sum(b["amount"] for b in st.active_buffs.get(key, []) if b["stat"] == "defense")
            sta_regen = sum(b["amount"] for b in st.active_buffs.get(key, []) if b["stat"] == "stamina_regen")
            if atk_buff:  tags.append(f"⚔️+{atk_buff}")
            if def_buff:  tags.append(f"🛡️+{def_buff}")
            if sta_regen: tags.append(f"♻️+{sta_regen}")

            if st.get_shield(key) > 0:                    tags.append(f"🔵{st.get_shield(key)}")
            if st.burn_stacks.get(key, 0) > 0:            tags.append(f"🔥×{st.burn_stacks[key]}({st.burn_duration.get(key,0)}t)")
            if st.silenced_turns.get(key, 0) > 0: tags.append(f"🔇{st.silenced_turns[key]}t")
            if st.get_duration("ignore_defense_turns", key) > 0: tags.append(f"🩸{st.get_duration('ignore_defense_turns', key)}t")
            if dm.grind_turns.get(key, 0) > 0:            tags.append(f"⚙️{dm.grind_turns[key]}t")
            if st.dmg_amp_stacks.get(key, 0) > 0:          tags.append(f"💢×{st.dmg_amp_stacks[key]:.1f}")
            if ab.shatter_stacks.get(key, 0) > 0:         tags.append(f"💥×{ab.shatter_stacks[key]}")
            if st.get_duration("true_damage_turns", key) > 0: tags.append(f"☠️{st.get_duration('true_damage_turns', key)}t")
            if st.invulnerable_turns.get(key, 0) > 0: tags.append(f"✨{st.invulnerable_turns[key]}t")
            if ab.deflect_active.get(key):                 tags.append("🔁Dfl")
            if ab.post_rebirth_reflect.get(key, 0) > 0:   tags.append(f"🪬{ab.post_rebirth_reflect[key]}")
            if ab.demon_mode_active.get(key):              tags.append(f"😈{ab.demon_mode_atk_stacks.get(key,0)}stk")
            if ab.rage_stacks.get(key, 0) > 0:            tags.append(f"😡×{ab.rage_stacks[key]}")
            if ab.guaranteed_crit_turns.get(key, 0) > 0:           tags.append(f"⚖️{ab.guaranteed_crit_turns[key]}crit")
            if ab.solar_flare_ready.get(key):              tags.append("☀️Flare")
            if ab.soul_hunt_streak.get(key, 0) > 0:       tags.append(f"💀×{ab.soul_hunt_streak[key]}")
            if ab.attack_streak.get(key, 0) > 0:          tags.append(f"🔺×{ab.attack_streak[key]}")
            if ab.limit_break_bonus.get(key, 0) > 0:      tags.append(f"⭐+{ab.limit_break_bonus[key]}")
            if ab.special_boost_flat.get(key, 0) > 0:     tags.append(f"🌟+{ab.special_boost_flat[key]}")
            if ab.stamina_steal_bonus.get(key, 0) > 0:    tags.append(f"🌀+{ab.stamina_steal_bonus[key]}")
            if ab.revival_used.get(key):                   tags.append("🪦Rebirth")
            if ab.dead_armor_triggered.get(key):           tags.append("🦅Armor")
            return tags

        # ── Compose each player panel ─────────────────────────────────────────
        # IMPORTANT: Discord only keeps 3 add_field(inline=True) blocks side-by-side
        # when every field value is narrow (~45 chars/line max). Keep lines SHORT.
        def player_panel(key: str, player: discord.Member, blade: dict,
                         dot: str, gauge_val: float) -> str:
            hp_now   = self.hp[key]
            sta_now  = sm.stamina[key]
            g_now    = gauge_val
            stab_now = self.stability_manager.stability.get(key, 100)
            stab_max = self.type_mods[key].stability_start

            _max_hp   = self.max_hp_per_player.get(key, BASE_HP)
            hp_pct    = int(round((hp_now / _max_hp) * 100))
            # Short bars (length=5) + compact numbers — no /MAX denominator
            hp_line   = f"`{hp_visual(hp_now, _max_hp, length=5)}` **{hp_now}** `{hp_pct}%`"
            # Per-player ceiling: stamina bars differ by bey now, so a flat
            # denominator would misreport every blade that isn't average.
            _sta_max  = sm.cap_for(key)
            sta_line  = f"`{sta_bar(sta_now, _sta_max, length=5)}` {sta_now:g}/{_sta_max:g}"
            gauge_line= f"`{gauge_bar(g_now, length=5)}` {int(g_now)}/{SPECIAL_GAUGE_MAX}"
            stab_line = f"`{stability_bar(stab_now, stab_max, length=5)}` {stab_now}/{stab_max}"

            # Effects: max 2 tags per row to stay narrow
            fx_list = active_effects(key)
            if fx_list:
                rows = []
                for i in range(0, len(fx_list), 2):
                    rows.append(" ".join(fx_list[i:i+2]))
                fx_block = "\n".join(rows)
            else:
                fx_block = "*no effects*"

            return (
                f"**{player.display_name}**\n"
                f"*{blade['name']}*\n"
                f"❤️ {hp_line}\n"
                f"⚡ {sta_line}\n"
                f"🌀 {gauge_line}\n"
                f"🔩 {stab_line}\n"
                f"{fx_block}"
            )

        g1 = sm.gauge[k1]
        g2 = sm.gauge[k2]

        embed = discord.Embed(
            title=f"╔══  ⚔️  ROUND  {self.round}  ⚔️  ══╗",
            color=discord.Color.from_str("#FF4444"),
        )

        # ── Left column: Player 1 ─────────────────────────────────────────────
        embed.add_field(
            name="🔴  Challenger",
            value=player_panel(k1, p1, b1, "🔴", g1),
            inline=True,
        )

        # ── Centre column: VS divider — keeps Discord in 3-col grid ──────────
        embed.add_field(
            name="\u200b",
            value=(
                "\u200b\n"
                "**VS**\n"
                "⚔️\n"
                "\u200b"
            ),
            inline=True,
        )

        # ── Right column: Player 2 ────────────────────────────────────────────
        embed.add_field(
            name="🔵  Challenger",
            value=player_panel(k2, p2, b2, "🔵", g2),
            inline=True,
        )

        embed.set_footer(text="▸ Choose your move below  •  Stamina regens each round")
        return embed

    # ── Move submission ───────────────────────────────────────────────────────

    async def submit_move(
        self,
        interaction: discord.Interaction,
        user:        discord.Member,
        move:        str,
    ) -> None:
        key = str(user.id)
        if self.moves.get(key) is not None:
            await interaction.response.send_message(
                "⏳ You already chose a move this round!", ephemeral=True
            )
            return

        self.moves[key] = move
        label = MOVE_LABELS.get(move, move)
        await interaction.response.send_message(
            f"✅ Move locked: **{label}**", ephemeral=True
        )

        if all(v is not None for v in self.moves.values()):
            # Fire round resolution as a background task so the interaction
            # response is already acknowledged before the heavy async work
            # begins.  Without this, Discord's 3-second interaction deadline
            # expires while _resolve_round is still running, which causes
            # "This interaction failed" for the second player's button click.
            asyncio.get_running_loop().create_task(self._resolve_round())

    # ── Round resolution ──────────────────────────────────────────────────────

    async def _resolve_round(self) -> None:
        if self._resolve_lock.locked() or self.finished:
            return
        async with self._resolve_lock:
            await self._resolve_round_inner()

    async def _resolve_round_inner(self) -> None:
        if self.finished:
            return
        try:
            await self.__resolve_round_body()
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            # Reset moves so players can choose again next round
            p1, p2 = self.players
            self.moves = {str(p1.id): None, str(p2.id): None}
            try:
                await self.channel.send(
                    f"⚠️ **Battle Error** — something went wrong this round.\n"
                    f"```{str(exc)[:400]}```\nMoves have been reset — please choose again."
                )
                # Re-post the panel so they can pick moves
                if not self.finished:
                    new_view = _InChannelControlPanel(self)
                    self._current_view = new_view
                    self.panel_msg = await self.channel.send(
                        embed=self._status_embed(), view=new_view
                    )
            except Exception:
                pass
        return

    async def __resolve_round_body(self) -> None:
        if self.finished:
            return

        p1, p2 = self.players
        k1, k2 = str(p1.id), str(p2.id)
        m1, m2 = self.moves[k1], self.moves[k2]
        b1, b2 = self.blades[k1], self.blades[k2]
        # Compute effective stats (base stats + active ATK/DEF buff bonuses + stat_mult).
        # Previously raw blade stats were passed here, which meant ability buffs
        # (e.g. ATK+20 for 2 rounds) had zero effect on actual damage math — the
        # stat_mult was then applied a second time inside AttackManager to the
        # *damage output*, effectively double-scaling the level bonus while
        # completely ignoring ability-granted buffs.
        # Now we build the effective dict here (matching DecisionEngine._effective_stats)
        # so calc_damage receives correct stat values, and AttackManager no longer
        # needs to re-apply stat_mult to the damage output.
        def _effective_stats(key: str, blade: dict) -> dict:
            base      = dict(blade.get("stats", {}))
            mult      = self.stat_mult.get(key, 1.0)
            st        = self.status
            atk_bonus = st.get_buff_bonus(key, "attack")
            def_bonus = st.get_buff_bonus(key, "defense")
            # Stamina was missing here, so any buff/debuff on the stamina STAT
            # was silently discarded. That is not the same thing as the stamina
            # BAR (StaminaManager) — a mode that costs "-20 stamina" means the
            # stat, exactly like the -20 DEF beside it. Encoding it as a bar
            # drain instead instantly zeroed the bar and Spin-Finished the
            # owner the moment it used its own special.
            sta_buff  = st.get_buff_bonus(key, "stamina")

            # ── Parts flat deltas (bonus and penalty) ─────────────────────────
            pd = self.part_deltas.get(key, {})
            atk_bonus += pd.get("attack",  0)
            def_bonus += pd.get("defense", 0)
            sta_bonus  = pd.get("stamina", 0) + sta_buff

            # ── Avatar percentage bonuses ──────────────────────────────────────
            # NOTE: all stats are floored at 0 — part penalties / debuffs can
            # push a stat negative, and negative stats break the math (negative
            # DEF would ADD damage via Shield Gate flat reduction, negative ATK
            # would heal the opponent, etc). 0 is the hard floor.
            av = self.avatar_bonuses.get(key)
            if av and av.has_any_bonus:
                raw_atk = (base.get("attack",  0) + atk_bonus) * mult
                raw_def = (base.get("defense", 0) + def_bonus) * mult
                raw_sta = (base.get("stamina", 0) + sta_bonus) * mult
                return {
                    "attack":  max(0, int(av.apply_attack_bonus(raw_atk))),
                    "defense": max(0, int(av.apply_defence_bonus(raw_def))),
                    "stamina": max(0, int(av.apply_stamina_bonus(raw_sta))),
                }
            return {
                "attack":  max(0, int((base.get("attack",  0) + atk_bonus) * mult)),
                "defense": max(0, int((base.get("defense", 0) + def_bonus) * mult)),
                "stamina": max(0, int((base.get("stamina", 0) + sta_bonus) * mult)),
            }

        s1 = _effective_stats(k1, b1)
        s2 = _effective_stats(k2, b2)
        sm     = self.stamina_manager

        round_log: list[str] = []

        # ── Round-start stamina regen from active buffs (e.g. Cosmic Mode) ───
        for _key in (k1, k2):
            round_log.extend(self.ability.apply_stamina_regen(_key))

        # ── Round-start DoT tick (Burn, Curse, Corruption) ─────────────────────
        for _key, _blade in ((k1, b1), (k2, b2)):
            round_log.extend(self.ability.apply_dot_tick_extras(_key, _blade))
            round_log.extend(self.status.tick_burn(_key, _blade))  # pass blade for name lookup

        # ── Death check after DoT ─────────────────────────────────────────────
        if self.hp[k1] <= 0 or self.hp[k2] <= 0:
            await self._end_battle()
            return

        # ── Ring-out check after DoT (stability can't drop from DoT but guard anyway) ─
        stab = self.stability_manager
        for key in (k1, k2):
            if stab.check_ring_out(key):
                self.hp[key] = 0
        if self.hp[k1] <= 0 or self.hp[k2] <= 0:
            await self._end_battle()
            return

        # ── Round-start Grind debuff tick ─────────────────────────────────────
        for _key in (k1, k2):
            round_log.extend(self.defense_manager.tick_grind(_key))

        # ── MOVE_STAMINA: recover stamina + heal ──────────────────────────────
        for key, move in ((k1, m1), (k2, m2)):
            if move == MOVE_STAMINA:
                from cogs.abilities.type_system import resolve_active_bonuses
                enemy_key  = k2 if key == k1 else k1
                _my_mod    = self.type_mods.get(key)
                _en_mod    = self.type_mods.get(enemy_key)
                _my_type   = _my_mod.btype if _my_mod else ""
                _en_type   = _en_mod.btype if _en_mod else ""
                _sta_active, _ = resolve_active_bonuses(_my_type, _en_type)
                # Interrupted heal: opponent attacked during our Stamina move
                _enemy_move = m2 if key == k1 else m1
                _attacked   = _enemy_move in (MOVE_ATTACK, MOVE_SPECIAL)
                round_log.extend(
                    sm.apply_stamina_action(key, self.hp, _my_mod, type_active=_sta_active,
                                            attacked=_attacked,
                                            max_hp=self.max_hp_per_player.get(key, self.max_hp))
                )
                sm.add_gauge(key, "stamina")
                try:
                    self.bot.dispatch("beycord_stamina_move", int(key))
                except Exception:
                    pass
                # Stability recovery for using stamina move → +25 (halved if attacked)
                # Gated behind type-advantage check: Stamina recovery only
                # applies when stability effects are active for this matchup.
                if self.stability_manager.is_effects_active(key, enemy_key):
                    round_log.extend(
                        self.stability_manager.apply_stamina_recovery(key, reduced=_attacked)
                    )

        # ── MOVE_CHARGE: fills Special Gauge by +50 ──────────────────────────
        for key, move in ((k1, m1), (k2, m2)):
            if move == MOVE_CHARGE:
                name = self.blades[key]["name"]
                sm.add_gauge(key, "charge")
                round_log.append(
                    f"🔋 **{name}** charges up! "
                    f"Special Gauge: `{sm.gauge[key]}/{SPECIAL_GAUGE_MAX}` (+{GAUGE_PER_CHARGE})"
                )

        # ── Deduct stamina costs ──────────────────────────────────────────────
        for key, move in ((k1, m1), (k2, m2)):
            round_log.extend(sm.deduct_cost(key, move))

        # ── Passive stamina regen (after cost deduction) ────────────────────────
        for key in (k1, k2):
            move_this_round = self.moves[key]
            if move_this_round in (MOVE_STAMINA, MOVE_CHARGE):
                continue
            round_log.extend(sm.apply_passive_regen(key, self.hp))

        # ── Damage resolution (delegated to AttackManager) ──────────────────
        am = self.attack_manager

        dmg_p1, counter_p1, matchup_p1, atk_logs_p1 = am.resolve_pair(
            k1, k2, m1, m2, b1, b2, s1, s2
        )
        dmg_p2, counter_p2, matchup_p2, atk_logs_p2 = am.resolve_pair(
            k2, k1, m2, m1, b2, b1, s2, s1
        )
        round_log.extend(atk_logs_p1)
        round_log.extend(atk_logs_p2)

        # ── Grind debuff: Defense lost to Stamina ─────────────────────────────
        if matchup_p1 == "lose_grind" and m1 == MOVE_DEFENSE:
            round_log.extend(
                self.defense_manager.apply_grind(k1, s1.get("defense", 50), k2)
            )
        if matchup_p2 == "lose_grind" and m2 == MOVE_DEFENSE:
            round_log.extend(
                self.defense_manager.apply_grind(k2, s2.get("defense", 50), k1)
            )

        # ── Apply damage + counter-hit reflections (via AttackManager) ────────
        hp_logs = am.apply_pair_results(
            k1, k2, b1, b2, m1, m2,
            dmg_p1, counter_p1, matchup_p1,
            dmg_p2, counter_p2, matchup_p2,
        )
        round_log.extend(hp_logs)

        # ── Intermediate ring-out check (ability-driven stability drops) ──────
        # apply_pair_results may trigger on_win / on_special abilities that drain
        # stability directly (e.g. Aether Stance, World Rotation).  The existing
        # ring-out check below only runs after apply_stability_costs, so those
        # drops would be missed for a full round.  This catches them immediately.
        _stab_early = self.stability_manager
        for key in (k1, k2):
            if _stab_early.check_ring_out(key) and self.hp[key] > 0:
                blade_name = self.blades[key]["name"]
                round_log.append(
                    f"🌀 **RING OUT!** **{blade_name}** was blasted out of the ring by an ability!"
                )
                self._mark_finish(key, "ringout")
                self.hp[key] = 0

        # ── Attrition ─────────────────────────────────────────────────────────
        # Applied before the Stamina-KO check so a blade drained to 0 this
        # round is resolved immediately rather than a round late.
        round_log.extend(self.attrition.apply_round(self.round))

        # ── Stamina KO ────────────────────────────────────────────────────────
        for key in (k1, k2):
            ko_logs = sm.check_stamina_ko(key, self.hp, self.blades)
            if ko_logs:
                # check_stamina_ko drops HP to 0 itself and only returns logs
                # when it actually fired, so a non-empty result IS the signal.
                self._mark_finish(key, "survival")
            round_log.extend(ko_logs)

        # ── Defense stability costs (after damage resolved so context is known) ─
        # Pass dmg_p2 for k1 and dmg_p1 for k2 — we need the damage the
        # OPPONENT dealt TO this defender, not the counter reflected outward.
        # Pass the OPPONENT'S matchup (their attacker perspective) so the
        # defender context (vs_attack, vs_defense, blocked, etc.) is resolved
        # correctly. matchup_p1 is "k1 attacking k2" — useless when k1 is the
        # defender; matchup_p2 is "k2 attacking k1" — the right context for k1.
        round_log.extend(
            self.defense_manager.apply_stability_costs(k1, m1, m2, matchup_p2, dmg_p2)
        )
        round_log.extend(
            self.defense_manager.apply_stability_costs(k2, m2, m1, matchup_p1, dmg_p1)
        )

        # ── Ring-out check (stability reached zero) ───────────────────────────
        stab = self.stability_manager
        for key in (k1, k2):
            if stab.check_ring_out(key) and self.hp[key] > 0:
                blade_name = self.blades[key]["name"]
                round_log.append(
                    f"🌀 **RING OUT!** **{blade_name}** lost all stability and flew out of the ring!"
                )
                self._mark_finish(key, "ringout")
                self.hp[key] = 0

        # ── Tick all timed status effects (end-of-round) ──────────────────────
        # Must run AFTER damage resolution so effects granted THIS round don't
        # immediately expire, but BEFORE the next panel is posted so the status
        # embed reflects the updated durations.
        st = self.status
        for key in (k1, k2):
            st.tick_buffs(key, round_log)       # ATK/DEF/stamina_regen buffs
            st.tick_silence(key, round_log)     # Silence counter
            st.tick_universal(key)              # ignore_invuln, true_damage
            st.decrement_invulnerable(key)      # Invulnerability turns

            # ignore_defense_turns is normally decremented inside
            # preprocess_defender_stats (MOVE_ATTACK) or _resolve_special
            # (MOVE_SPECIAL). The only case where neither fires is
            # MOVE_STAMINA / MOVE_CHARGE — tick it here so the duration
            # doesn't freeze when the player doesn't attack.
            # MOVE_SPECIAL is intentionally excluded: _resolve_special already
            # decrements once; adding it here causes double-decrement (Bug fix).
            move_this = m1 if key == k1 else m2
            if move_this in (MOVE_STAMINA, MOVE_CHARGE):
                d = st.ignore_defense_turns
                if d.get(key, 0) > 0:
                    d[key] = max(0, d[key] - 1)

        # ── Consume Special Gauge for players who used SPECIAL this round ──────
        # Done here (post-resolution) so the gauge isn't lost if an error
        # occurs mid-round (moved from the button handler).
        for key, move in ((k1, m1), (k2, m2)):
            if move == MOVE_SPECIAL:
                sm.consume_gauge(key)

        # ── Build round summary (via AttackManager) ───────────────────────────
        lines = am.build_round_summary(
            p1, p2, k1, k2, m1, m2,
            dmg_p1, dmg_p2, matchup_p1, matchup_p2,
            round_log,
        )

        result_embed = discord.Embed(
            description="\n".join(lines),
            color=discord.Color.dark_embed(),
        )
        result_embed.set_author(
            name=f"{b1.get('name','?')}  Vs.  {b2.get('name','?')}"
        )
        result_embed.set_footer(
            text=(f"Round {self.round}  •  "
                  f"{b1.get('name','?')} {max(0, self.hp[k1])} HP  •  "
                  f"{b2.get('name','?')} {max(0, self.hp[k2])} HP")
        )
        await self.channel.send(embed=result_embed)

        # ── Reset moves ───────────────────────────────────────────────────────
        self.moves = {k1: None, k2: None}
        self.round += 1

        # ── Win condition check ───────────────────────────────────────────────
        if self.hp[k1] <= 0 or self.hp[k2] <= 0:
            await self._end_battle()
            return

        # ── Stop old panel so stale button clicks can't re-trigger resolution ─
        if self._current_view:
            self._current_view.stop()

        new_view = _InChannelControlPanel(self)
        self._current_view = new_view
        card = await self._battle_card_file()
        if card:
            # Card shows all stats — keep the panel text minimal
            panel_embed = discord.Embed(
                title=f"⚔️ ROUND {self.round} — Choose your move!",
                color=discord.Color.dark_embed(),
            )
            panel_embed.set_image(url="attachment://battle.png")
        else:
            panel_embed = self._status_embed()
        self.panel_msg = await self.channel.send(
            embed=panel_embed,
            file=card,
            view=new_view,
        )

    # ── Battle card (Pillow image) ────────────────────────────────────────────
    def _stability_max(self, k: str) -> int:
        """Player's starting stability (Defense types start higher)."""
        try:
            from cogs.abilities.type_system import TypeModifiers
            return int(TypeModifiers(self.blades[k]).stability_start)
        except Exception:
            return 100

    async def _battle_card_file(self) -> Optional[discord.File]:
        """Render the round status card as a Discord file.

        Returns None on ANY failure (missing Pillow, render error) so battles
        always fall back to the classic text embed and never break.
        """
        try:
            from utils.image_generator import render_battle_card, CARD_ENABLED
            if not CARD_ENABLED:
                return None
            p1, p2 = self.players
            k1, k2 = str(p1.id), str(p2.id)
            sm, st = self.stamina_manager, self.status

            def chips(k: str) -> list[str]:
                out: list[str] = []
                def safe(fn):
                    try: fn()
                    except Exception: pass
                safe(lambda: out.append(f"SHIELD {st.get_shield(k)}") if st.get_shield(k) else None)
                safe(lambda: out.append(f"BURN x{st.burn_stacks.get(k, 0)}")
                     if st.burn_stacks.get(k, 0) > 0 else None)
                safe(lambda: out.append("SILENCE") if st.is_silenced(k) else None)
                safe(lambda: out.append("INVULN") if st.is_invulnerable(k) else None)
                for stat, lbl in (("attack", "ATK"), ("defense", "DEF")):
                    safe(lambda s=stat, L=lbl: out.append(f"{L} {st.get_buff_bonus(k, s):+d}")
                         if st.get_buff_bonus(k, s) else None)
                safe(lambda: out.append(f"AMP {int(st.dmg_amp_stacks.get(k, 0) * 100)}%")
                     if st.dmg_amp_stacks.get(k, 0) else None)
                safe(lambda: out.append("PIERCE") if st.ignore_defense_turns.get(k, 0) > 0 else None)
                safe(lambda: out.append("TRUE DMG") if st.true_damage_turns.get(k, 0) > 0 else None)
                safe(lambda: out.append("CRIT") if st.guaranteed_crit_turns.get(k, 0) > 0 else None)
                safe(lambda: out.append("DEFLECT") if st.deflect_active.get(k) else None)
                safe(lambda: out.append(f"REFLECT {st.post_rebirth_reflect.get(k, 0)}")
                     if st.post_rebirth_reflect.get(k, 0) else None)
                safe(lambda: out.append(f"SPC +{st.special_boost_flat.get(k, 0)}")
                     if st.special_boost_flat.get(k, 0) else None)
                # engine state: mode + biggest stack counter
                eng = self.ability
                safe(lambda: out.append(f"MODE {eng.modes[k]}") if eng.modes.get(k) else None)
                def _stacks():
                    best = max(((n, v) for (kk, n), v in eng.counters.items()
                                if kk == k and v > 0), key=lambda x: x[1], default=None)
                    if best:
                        out.append(f"STACK x{best[1]}")
                safe(_stacks)
                return out

            def side(k: str, member) -> dict:
                blade = self.blades.get(k, {})
                return {
                    "name": getattr(member, "display_name", str(member))[:20],
                    "blade": blade.get("name", "?"),
                    "hp": self.hp.get(k, 0),
                    "max_hp": self.max_hp_per_player.get(k, self.max_hp),
                    "stamina": round(sm.stamina.get(k, 0), 1),
                    "max_stamina": getattr(sm, "max_stamina", {}).get(k, 10) or 10,
                    "gauge": getattr(sm, "gauge", {}).get(k, 0),
                    "gauge_max": SPECIAL_GAUGE_MAX,
                    "stability": getattr(self.stability_manager, "stability", {}).get(k, 0),
                    "stability_max": self._stability_max(k),
                    "statuses": chips(k),
                }

            left, right = side(k1, p1), side(k2, p2)
            buf = await asyncio.to_thread(render_battle_card, self.round, left, right)
            return discord.File(fp=buf, filename="battle.png")
        except Exception:
            return None

    # ── Battle end ────────────────────────────────────────────────────────────

    def _mark_finish(self, key: str, kind: str) -> None:
        """Record HOW this blade went out. First mark wins.

        Stability and stamina both drive HP to 0 to end the fight, so without a
        first-wins rule a blade that rings out on the same round its stamina
        expires could be relabelled by whichever check ran second.
        """
        self.finish_type.setdefault(str(key), kind)

    def finish_for(self, loser_key: str) -> str:
        """The finish that ended it for this player.

        Defaults to a burst: the two special endings mark themselves, so an
        unmarked loss is by definition HP reduced to zero by damage — which is
        exactly what a burst is.
        """
        from utils import ranked as RK
        return self.finish_type.get(str(loser_key), RK.FINISH_BURST)

    async def _end_battle(self) -> None:
        if self.finished:
            return
        self.finished = True
        self._done_event.set()

        p1, p2 = self.players
        k1, k2 = str(p1.id), str(p2.id)

        if self.hp[k1] > self.hp[k2]:
            winner, loser = p1, p2
        elif self.hp[k2] > self.hp[k1]:
            winner, loser = p2, p1
        else:
            winner, loser = None, None

        # Expose the result so external systems (tournament mode) can read it
        self.winner_id: int | None = int(winner.id) if winner else None
        self.loser_id:  int | None = int(loser.id)  if loser  else None

        if self.panel_msg:
            try:
                await self.panel_msg.edit(view=None)
            except discord.NotFound:
                pass
        if self._current_view:
            self._current_view.stop()
            self._current_view = None

        if winner:
            # Guard: member objects can be None/stale if a user left the server
            # mid-battle.  Fallback to blade name so the embed never crashes.
            w_name = getattr(winner, "display_name", None) or self.blades[str(winner.id)]["name"]
            l_name = getattr(loser,  "display_name", None) or self.blades[str(loser.id)]["name"]

            from utils import ranked as RK

            w_profile          = get_user(winner.id)
            # `wins` / `losses` stay lifetime-across-everything: the profile
            # card, achievements and ;audit all read them and none of those is
            # competitive. Only the ranked keys below drive the ladder.
            w_profile["wins"] += 1
            w_profile["coins"] = w_profile.get("coins", 0) + COINS_WIN

            # ── Rank score + win streak — RANKED battles only ─────────────────
            streak = 0
            if self.ranked:
                streak = RK.apply_ranked_win(w_profile)
            streak_bonus = _streak_bonus(streak) if streak else 0
            if streak_bonus:
                w_profile["coins"] += streak_bonus
            # Bey EXP for the blade that actually fought. Awarded on the
            # profile dict already in hand, before update_user, so it lands in
            # the same write rather than racing a second read-modify-write.
            _bey_xp(w_profile, self.blades.get(str(winner.id)), won=True)
            update_user(winner.id, w_profile)
            wlvl, _, w_up = grant_xp(winner.id, XP_WIN)

            l_profile            = get_user(loser.id)
            l_profile["losses"] += 1
            l_profile["coins"]   = l_profile.get("coins", 0) + COINS_LOSS
            if self.ranked:
                RK.apply_ranked_loss(l_profile)   # score down, streak broken
            _bey_xp(l_profile, self.blades.get(str(loser.id)), won=False)
            update_user(loser.id, l_profile)
            llvl, _, l_up = grant_xp(loser.id, XP_LOSS)

            w_blade  = self.blades[str(winner.id)]
            w_rarity = w_blade.get("rarity", "Common")
            w_tier   = tier_for_score(rank_score_for(w_profile))
            l_tier   = tier_for_score(rank_score_for(l_profile))

            # The rank line only appears on a ranked match. Printing "+25 pts"
            # after a casual battle would advertise a score change that did not
            # happen, which is worse than saying nothing.
            w_rank = (f" | {w_tier[2]} {w_tier[1]} (+{WIN_SCORE} pts)"
                      if self.ranked else "")
            l_rank = (f" | {l_tier[2]} {l_tier[1]} (-{LOSS_SCORE} pts)"
                      if self.ranked else "")
            # The finish type is announced on EVERY battle, not just ranked
            # ones. All three endings used to render as "knocked out", so a
            # ring-out and a burst were indistinguishable to the player who
            # just lost one.
            kind = self.finish_for(str(loser.id))
            self.last_finish = kind
            verb = {"burst": "burst the opponent",
                    "survival": "outlasted the opponent",
                    "ringout": "sent the opponent flying"}.get(kind, "won")
            embed = discord.Embed(
                title=(f"{'🏅 RANKED — ' if self.ranked else ''}"
                       f"{RK.finish_label(kind)} — {w_name} WINS!"),
                description=(
                    f"**{w_blade['name']}** {verb}!"
                    + (f"  *(+{RK.finish_points(kind)} match "
                       f"point{'s' if RK.finish_points(kind) != 1 else ''})*"
                       if self.ranked else "") + "\n\n"
                    f"🌀 **{w_name}** +{XP_WIN} XP → Level **{wlvl}** "
                    f"{'⬆️ LEVEL UP! ' if w_up else ''}"
                    f"{w_rank} | +{COINS_WIN} 💰\n"
                    f"💀 **{l_name}** +{XP_LOSS} XP → Level **{llvl}** "
                    f"{'⬆️ LEVEL UP! ' if l_up else ''}"
                    f"{l_rank} | +{COINS_LOSS} 💰"
                    + (f"\n\n🔥 **{streak} WIN STREAK!** Bonus: **+{streak_bonus}** 💰"
                       if streak_bonus else
                       (f"\n\n🔥 Win streak: **{streak}**" if streak >= 2 else ""))
                    + ("" if self.ranked else
                       "\n\n*Casual match — no rank score, win rate or streak.*")
                ),
                color=rarity_colour(w_rarity),
            )
        else:
            for pid in (p1.id, p2.id):
                grant_xp(pid, XP_LOSS)
                draw_profile = get_user(pid)
                draw_profile["coins"] = draw_profile.get("coins", 0) + COINS_LOSS
                update_user(pid, draw_profile)
            embed = discord.Embed(
                title="🤝 DRAW!",
                description="Both blades stopped spinning simultaneously!",
                color=discord.Color.greyple(),
            )

        await self.channel.send(embed=embed)

        # ── Quest / external system events ────────────────────────────────────
        try:
            self.bot.dispatch(
                "beycord_battle_end",
                self.winner_id,
                [int(p1.id), int(p2.id)],
                self.channel.guild.id if self.channel.guild else None,
            )
        except Exception:
            pass

        # Richer, additive event for mastery / achievements / clan wars.
        # Kept separate from beycord_battle_end so existing listeners
        # (quests.py) keep their exact signature.
        try:
            self.bot.dispatch("beycord_battle_blades", {
                "winner_id":   self.winner_id,
                "participants": [int(p1.id), int(p2.id)],
                "blades": {
                    str(p1.id): self.blades.get(str(p1.id), {}).get("name"),
                    str(p2.id): self.blades.get(str(p2.id), {}).get("name"),
                },
                "draw":     self.winner_id is None,
                "guild_id": self.channel.guild.id if self.channel.guild else None,
                "channel":  self.channel,
            })
        except Exception:
            pass

    # ── Public entry-point called by BattleCog ────────────────────────────────

    async def run(self) -> None:
        """Start the battle: announce setup effects, post the first control panel,
        then wait until the battle finishes (driven by button interactions)."""

        # Announce any battle-start setup effects (initial shields, buffs, etc.)
        if self._setup_log:
            setup_embed = discord.Embed(
                title="⚙️ Battle Setup",
                description="\n".join(self._setup_log),
                color=discord.Color.blurple(),
            )
            await self.channel.send(embed=setup_embed)

        # Post the initial status panel with move buttons
        first_view = _InChannelControlPanel(self)
        self._current_view = first_view
        card = await self._battle_card_file()
        if card:
            start_embed = discord.Embed(
                title="⚔️ ROUND 1 — Choose your move!",
                color=discord.Color.dark_embed(),
            )
            start_embed.set_image(url="attachment://battle.png")
        else:
            start_embed = self._status_embed("⚔️ Round 1 — Choose your move!")
        self.panel_msg = await self.channel.send(
            embed=start_embed,
            file=card,
            view=first_view,
        )

        # Block until _end_battle() sets the event
        await self._done_event.wait()
