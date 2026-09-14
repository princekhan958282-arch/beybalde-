"""UNKNOWN — Horror Story Part 1 battle opponent.

This module deliberately keeps UNKNOWN outside beyblades.json.  Players can
fight it, but it cannot leak into spawns, shops, boosters, inventory or normal
Beypedia lookups.

The opponent reuses BattleSession rather than maintaining a second combat
engine.  Its counter layer is scoped to this horror battle only: it inspects a
COPY of the player's equipped Bey, prepares counters for that kit, and never
rewrites the player's real blade data.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Iterable

import discord
from discord.ext import commands

from cogs.battle.session import BattleSession
from cogs.story.story_ai import LeagueOpponent
from cogs.abilities.legacy_convert import legacy_convert
from utils import horror_state
from utils.database import get_user, mutate_user

log = logging.getLogger("beyblade_bot.horror.unknown")

UNKNOWN_IMAGE_URL = (
    "https://cdn.discordapp.com/attachments/1510856884943454208/"
    "1549076715903516732/38968.png"
    "?ex=6aa9619d&is=6aa8101d&hm=52cb22cc9eb3835acafd3b535fbbbe4dd5a3d6315e42295b03456ad57855ded5&"
)

UNKNOWN_LEVEL = 100
UNKNOWN_STAT = 600
UNKNOWN_NPC_ID = -404404000000

# Real internal values.  The public info surface intentionally never renders
# this dict; see _info_guard below.
UNKNOWN_BEY: dict[str, Any] = {
    "id": "horror_unknown",
    "name": "UNKNOWN",
    "type": "Balance",
    "rarity": "Exclusive",
    "image_url": UNKNOWN_IMAGE_URL,
    "hidden": True,
    "level": UNKNOWN_LEVEL,
    "stats": {
        "hp": UNKNOWN_STAT,
        "attack": UNKNOWN_STAT,
        "defense": UNKNOWN_STAT,
        "stamina": UNKNOWN_STAT,
        "special": UNKNOWN_STAT,
        "stability": UNKNOWN_STAT,
    },
    # The exact move is intentionally unreadable to players.  600 printed
    # Special keeps the move on the same internal statline as every other stat.
    "special_move": {
        "name": "UNKNOWN",
        "hits": 1,
        "damage_per_hit": UNKNOWN_STAT,
        "total_damage": UNKNOWN_STAT,
        "flavour_text": "👁️ UNKNOWN answered.",
    },
    "abilities": [
        {
            "name": "UNKNOWN",
            "description": "UNKNOWN",
            "rules": [],
        }
    ],
}


class UnknownFighter:
    """The tiny player-shaped object BattleSession requires for an NPC."""

    __slots__ = ("id", "display_name", "bot", "mention")

    def __init__(self, player_id: int) -> None:
        # Unique synthetic ID per target so two simultaneous Horror battles
        # cannot overwrite each other's entry in BattleCog.active_battles.
        self.id = UNKNOWN_NPC_ID - int(player_id)
        self.display_name = "UNKNOWN"
        self.bot = False
        self.mention = "**UNKNOWN**"


class UnknownOpponent(LeagueOpponent):
    """Maximum-level NPC using the existing highest-rung battle brain."""

    def __init__(self, member: UnknownFighter, blade: dict) -> None:
        super().__init__(
            member,
            blade,
            difficulty="nightmare",
            level=UNKNOWN_LEVEL,
            hp_gain=0,
            avatar_id=None,
        )


# ── Adaptive-counter classification ──────────────────────────────────────────

_HEAL_OPS = frozenset({
    "heal", "heal_pct", "heal_per_drain", "hp_regen", "hp_regen_per_turn",
    "regen_hp", "lifesteal", "lifesteal_pct", "stack_scaled_lifesteal_pct",
})
_CRIT_OPS = frozenset({
    "crit", "crit_chance", "crit_damage", "guaranteed_crit",
    "guaranteed_crit_turns",
})
_SHIELD_OPS = frozenset({
    "shield", "initial_shield", "invulnerable", "invulnerability",
    "damage_immunity", "resist_damage", "damage_reduction",
})
_DEBUFF_OPS = frozenset({
    "enemy_debuff", "enemy_debuff_pct", "burn", "silence", "curse", "grind",
    "damage_amp_enemy", "stability_debuff",
})


def _iter_rules(blade: dict) -> Iterable[dict]:
    for ability in blade.get("abilities") or []:
        if not isinstance(ability, dict):
            continue
        for rule in ability.get("rules") or []:
            if isinstance(rule, dict):
                yield rule

    for rule in blade.get("special_rules") or []:
        if isinstance(rule, dict):
            yield rule

    sm = blade.get("special_move") or {}
    if isinstance(sm, dict):
        for rule in sm.get("rules") or []:
            if isinstance(rule, dict):
                yield rule

    for sm in (blade.get("special_moves") or {}).values():
        if not isinstance(sm, dict):
            continue
        for rule in sm.get("rules") or []:
            if isinstance(rule, dict):
                yield rule


def _op_name(op: dict) -> str:
    return str(op.get("op") or "").strip().lower()


def _counter_profile(blade: dict) -> dict[str, Any]:
    """Classify the player's authored mechanics into UNKNOWN responses."""
    out: dict[str, Any] = {
        "attack_growth": 0.0,
        "dodge": False,
        "defense": False,
        "heal": False,
        "drain": False,
        "crit": False,
        "debuff": False,
        "special": True,  # UNKNOWN always automatically counters Specials.
    }

    for rule in _iter_rules(blade):
        for op in rule.get("do") or []:
            if not isinstance(op, dict):
                continue
            kind = _op_name(op)
            stat = str(op.get("stat") or "").lower()

            if stat == "attack" and ("buff" in kind or "stack" in kind):
                raw = op.get("pct", op.get("per_stack", op.get("value", 0)))
                try:
                    amount = float(raw or 0)
                except (TypeError, ValueError):
                    amount = 0.0
                # A stacking attack gain is countered at its authored ceiling,
                # not just one stack. Otherwise +8 ATK x10 only produced +8 DEF.
                if "stack" in kind and op.get("max") is not None and op.get("per_stack") is not None:
                    try:
                        amount *= max(1, int(op.get("max") or 1))
                    except (TypeError, ValueError):
                        pass
                # A percentage attack gain is countered with the same share of
                # UNKNOWN's 600 DEF. Flat gains are mirrored flat.
                if "pct" in op or op.get("pct") is not None:
                    amount = UNKNOWN_STAT * amount / (100 if amount > 1 else 1)
                out["attack_growth"] = max(out["attack_growth"], amount)

            if stat == "defense" and ("buff" in kind or "stack" in kind):
                out["defense"] = True
            if "dodge" in kind or "evad" in kind:
                out["dodge"] = True
            if kind in _SHIELD_OPS or "shield" in kind or "invulner" in kind:
                out["defense"] = True
            if kind in _HEAL_OPS or "heal" in kind or "lifesteal" in kind:
                out["heal"] = True
            if "drain" in kind or "steal_stamina" in kind:
                out["drain"] = True
            if kind in _CRIT_OPS or "crit" in kind:
                out["crit"] = True
            if kind in _DEBUFF_OPS or kind.startswith("enemy_debuff"):
                out["debuff"] = True

    # Legacy ability blocks are not rules.  A light key scan makes the counter
    # still recognise old authored cards without teaching this module the old
    # ability engine's entire conversion table.
    blob = repr(blade.get("abilities") or []).lower()
    out["dodge"] = out["dodge"] or "dodge" in blob or "evasion" in blob
    out["heal"] = out["heal"] or "lifesteal" in blob or "heal" in blob
    out["drain"] = out["drain"] or "stamina_drain" in blob or "drain_stamina" in blob
    out["crit"] = out["crit"] or "crit_chance" in blob or "crit_damage" in blob
    out["debuff"] = out["debuff"] or "debuff" in blob or "silence" in blob
    return out


def _filter_ops(ops: list, *, flags: dict[str, Any]) -> list:
    """Remove effects UNKNOWN directly hard-counters in this battle copy."""
    kept = []
    for op in ops or []:
        if not isinstance(op, dict):
            kept.append(op)
            continue
        kind = _op_name(op)
        if flags.get("heal") and (kind in _HEAL_OPS or "heal" in kind or "lifesteal" in kind):
            continue
        if flags.get("crit") and (kind in _CRIT_OPS or "crit" in kind):
            continue
        # Shields/immunity are answered by pierce. Removing the grant here also
        # covers shield implementations that sit outside DEF mitigation.
        if flags.get("defense") and (kind in _SHIELD_OPS or "shield" in kind or "invulner" in kind):
            continue
        kept.append(op)
    return kept


def _countered_player_blade(blade: dict, flags: dict[str, Any]) -> dict:
    """Return a private battle copy with countered effects neutralised."""
    out = copy.deepcopy(blade)

    # Automatic Special counter: the player can still press Special, spend its
    # resources and see the move happen, but its damage and on-Special payload
    # are neutralised by UNKNOWN.  `non_damage` is a first-class damage_rules
    # flag and therefore safer than relying on a magic 0 that older floors turn
    # back into 1 damage.
    def neutralise_special(sm: dict) -> dict:
        sm = copy.deepcopy(sm or {})
        sm["non_damage"] = True
        sm["damage_per_hit"] = 0
        sm["total_damage"] = 0
        sm["pierce_defense_pct"] = 0
        sm["ignores_defense"] = False
        sm["per_hit_bonus"] = []
        if "rules" in sm:
            sm["rules"] = []
        return sm

    if isinstance(out.get("special_move"), dict):
        out["special_move"] = neutralise_special(out["special_move"])
    if isinstance(out.get("special_moves"), dict):
        out["special_moves"] = {
            name: neutralise_special(sm)
            for name, sm in out["special_moves"].items()
        }
        # Dual-special blades normally choose before PvP starts. Horror keeps
        # that surface simple: if nothing is already selected, use the first
        # authored Special, then counter it like every other Special.
        if not out.get("special_move") and out["special_moves"]:
            first_name, first = next(iter(out["special_moves"].items()))
            out["special_move"] = copy.deepcopy(first)
            out["chosen_special"] = first_name

    out["special_rules"] = []

    abilities = []
    for ability in out.get("abilities") or []:
        if not isinstance(ability, dict):
            abilities.append(ability)
            continue
        ab = copy.deepcopy(ability)
        # Legacy flat-field abilities must be converted before filtering.
        # Merely detecting "heal"/"crit" in their text did not counter them,
        # because AbilityEngine converted the untouched fields again later.
        source_rules = ab.get("rules") or legacy_convert(ab)
        rules = []
        for rule in source_rules:
            if not isinstance(rule, dict):
                rules.append(rule)
                continue
            r = copy.deepcopy(rule)
            when = str(r.get("when") or "").lower()
            if when in {"on_special", "on_hit"}:
                # Special payload is countered automatically, including per-hit
                # riders that would otherwise turn a 0-damage Special back into
                # real damage.
                r["do"] = []
                r.pop("_chain", None)
            else:
                r["do"] = _filter_ops(r.get("do") or [], flags=flags)
            rules.append(r)
        # Freeze the converted rules onto the private battle copy so the engine
        # cannot re-convert the original legacy flat fields behind our filter.
        ab["rules"] = rules
        abilities.append(ab)
    out["abilities"] = abilities
    return out


def _arm_unknown(session: BattleSession, player_key: str, unknown_key: str,
                 flags: dict[str, Any]) -> None:
    """Install runtime counters after BattleSession has built its managers."""
    # All five requested combat stats are 600 internally.  BattleSession's HP
    # helper normally clamps a printed HP stat into a type band, so the Horror
    # opponent explicitly owns its requested 600-point HP pool.
    session.hp[unknown_key] = UNKNOWN_STAT
    session.max_hp_per_player[unknown_key] = UNKNOWN_STAT
    session.special_stats[unknown_key] = UNKNOWN_STAT
    session.bey_levels[unknown_key] = UNKNOWN_LEVEL

    try:
        session.stability_manager.stability[unknown_key] = UNKNOWN_STAT
        session.stability_manager.max[unknown_key] = UNKNOWN_STAT
        session.type_mods[unknown_key].stability_start = UNKNOWN_STAT
    except Exception:  # noqa: BLE001
        pass

    # Example requested by the owner: enemy ATK increase -> UNKNOWN gains DEF.
    if flags.get("attack_growth"):
        amount = max(1, int(round(float(flags["attack_growth"]))))
        session.status.add_buff(
            unknown_key, "defense", amount, 999, source="unknown_adaptive_counter"
        )

    # Enemy defense/shields -> permanent defense pierce for this encounter.
    if flags.get("defense"):
        session.status.set_duration("ignore_defense_turns", unknown_key, 999)

    # Enemy stamina drain -> drain resistance. Existing ability ops cap this
    # mechanic at 90%, so UNKNOWN uses the same engine-safe ceiling.
    if flags.get("drain"):
        session.stamina_manager.drain_reduction[unknown_key] = max(
            session.stamina_manager.drain_reduction.get(unknown_key, 0.0), 0.90
        )

    # Enemy debuffs -> immunity.
    if flags.get("debuff"):
        session.ability.debuff_immune[unknown_key] = True

    # Example requested by the owner: Dodge -> Anti-Dodge. Blade-authored dodge
    # is detected before session creation; avatar dodge is visible only now.
    try:
        av_dodge = float(getattr(session.avatar_bonuses.get(player_key), "dodge_chance", 0) or 0)
    except Exception:  # noqa: BLE001
        av_dodge = 0.0
    if flags.get("dodge") or av_dodge > 0:
        session.ability.undodgeable_turns[unknown_key] = 999


def _unknown_info_embed() -> discord.Embed:
    embed = discord.Embed(
        title="👁️ UNKNOWN",
        description=(
            "**Name:** UNKNOWN\n"
            "**Type:** UNKNOWN\n"
            "**Rarity:** UNKNOWN\n"
            "**Level:** UNKNOWN\n\n"
            "**HP:** UNKNOWN\n"
            "**ATK:** UNKNOWN\n"
            "**DEF:** UNKNOWN\n"
            "**STM:** UNKNOWN\n"
            "**Stability:** UNKNOWN\n\n"
            "**Ability:** UNKNOWN\n"
            "**Special Move:** UNKNOWN\n\n"
            "*No information about this Beyblade exists.*"
        ),
        color=0x050505,
    )
    embed.set_image(url=UNKNOWN_IMAGE_URL)
    return embed


def _take_equipped(profile: dict, bey_name: str, copy_id: str | None) -> str:
    """mutate_user callback: remove exactly the Bey that entered the encounter."""
    if copy_id:
        copies = list(profile.get("boss_copies") or [])
        kept = [c for c in copies if str(c.get("id", "")).lower() != copy_id.lower()]
        if len(kept) != len(copies):
            profile["boss_copies"] = kept
            if str(profile.get("active_copy") or "").lower() == copy_id.lower():
                profile["active_copy"] = None
                profile["active_beyblade"] = None
            return bey_name
        return ""

    inv = list(profile.get("inventory") or [])
    match_i = next((i for i, n in enumerate(inv)
                    if str(n).casefold() == str(bey_name).casefold()), None)
    if match_i is None:
        return ""
    gone = str(inv.pop(match_i))
    profile["inventory"] = inv
    if str(profile.get("active_beyblade") or "").casefold() == gone.casefold():
        profile["active_beyblade"] = None
        profile["active_copy"] = None

    # Progress/mastery belong to the species. Preserve them if another copy of
    # the same normal Bey remains; otherwise remove the orphaned rows.
    if not any(str(n).casefold() == gone.casefold() for n in inv):
        (profile.get("bey_progress") or {}).pop(gone, None)
        (profile.get("mastery") or {}).pop(gone, None)
    return gone


class UnknownBattleCog(commands.Cog, name="Unknown Horror Battle"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._running: set[int] = set()
        bot.add_check(self._info_guard)

    def cog_unload(self) -> None:
        try:
            self.bot.remove_check(self._info_guard)
        except Exception:  # noqa: BLE001
            pass

    async def _info_guard(self, ctx: commands.Context) -> bool:
        """Intercept every normal Beypedia alias for the hidden opponent."""
        cmd = getattr(ctx, "command", None)
        if cmd is None or getattr(cmd, "name", "") != "beypedia":
            return True
        content = str(getattr(getattr(ctx, "message", None), "content", "") or "")
        prefix = str(getattr(ctx, "prefix", ";") or ";")
        raw = content[len(prefix):].strip() if content.startswith(prefix) else content.strip()
        parts = raw.split(maxsplit=1)
        if len(parts) != 2 or parts[1].strip().casefold() != "unknown":
            return True
        await ctx.send(embed=_unknown_info_embed())
        return False

    @commands.Cog.listener()
    async def on_horror_battle_requested(
        self, channel, user, equipped_bey_name, equipped_copy_id=None
    ) -> None:
        uid = int(user.id)
        if uid in self._running:
            return
        self._running.add(uid)
        try:
            await self._run(
                channel, user, str(equipped_bey_name),
                str(equipped_copy_id) if equipped_copy_id else None,
            )
        finally:
            self._running.discard(uid)

    async def _run(
        self, channel, player, accepted_name: str, accepted_copy_id: str | None = None
    ) -> None:
        from cogs.battle.battle import _apply_parts
        from cogs.battle.boss import boss_copy as bcopy

        profile = await get_user(player.id)
        raw_blade, copy_instance = await bcopy.equipped_blade(player.id)
        if not raw_blade:
            horror_state.save_encounter(player.id, status="battle_failed")
            return await channel.send(
                f"{player.mention}\n**UNKNOWN:** you came without a bey."
            )

        # The button snapshotted the name. Refuse a last-millisecond loadout swap
        # instead of silently making UNKNOWN claim a different Bey afterward.
        if str(raw_blade.get("name") or "").casefold() != accepted_name.casefold():
            horror_state.save_encounter(player.id, status="battle_failed")
            return await channel.send(
                f"{player.mention}\n**UNKNOWN:** that's not the bey you brought me."
            )
        current_copy_id = str((copy_instance or {}).get("id") or "") or None
        if current_copy_id != accepted_copy_id:
            horror_state.save_encounter(player.id, status="battle_failed")
            return await channel.send(
                f"{player.mention}\n**UNKNOWN:** don't switch beys after accepting."
            )

        player_blade = raw_blade if copy_instance else _apply_parts(raw_blade, profile)
        flags = _counter_profile(player_blade)
        player_blade = _countered_player_blade(player_blade, flags)

        npc = UnknownFighter(player.id)
        unknown_blade = copy.deepcopy(UNKNOWN_BEY)
        controller = UnknownOpponent(npc, unknown_blade)

        intro = discord.Embed(
            title="👁️ UNKNOWN CHALLENGER",
            description=(
                f"**Your Bey:** {discord.utils.escape_markdown(accepted_name)}\n"
                "**Opponent:** UNKNOWN\n"
                "**Type:** UNKNOWN\n"
                "**Level:** UNKNOWN\n\n"
                "*Something is wrong with this Bey...*"
            ),
            color=0x050505,
        )
        intro.set_image(url=UNKNOWN_IMAGE_URL)
        await channel.send(embed=intro)

        session = await BattleSession.create(
            bot=self.bot,
            channel=channel,
            p1=player,
            p2=npc,
            blade1=player_blade,
            blade2=unknown_blade,
            ranked=False,
            npc_controller=controller,
            payout=False,
            spend_energy=False,
        )
        pkey, ukey = str(player.id), str(npc.id)
        _arm_unknown(session, pkey, ukey, flags)
        # Hard stop for the player's Special. This lives on the session so
        # flat passives, per-hit procs and avatar Special riders cannot leak
        # damage back into a Special that UNKNOWN already countered.
        session.special_nullified_keys = {pkey}

        # Do not reveal which counter was selected. The entire point of Part 1
        # is that players notice the adaptation before they understand it.
        if any(bool(v) for k, v in flags.items() if k != "special"):
            await channel.send("👁️ **UNKNOWN adapted.**")

        horror_state.save_encounter(
            player.id,
            status="battle_running",
            battle_started=True,
            equipped_bey=accepted_name,
            equipped_copy_id=(copy_instance or {}).get("id"),
        )

        battle_cog = self.bot.get_cog("Battle")
        if battle_cog is not None:
            active = getattr(battle_cog, "active_battles", None)
            if isinstance(active, dict):
                if player.id in active:
                    horror_state.save_encounter(player.id, status="battle_failed")
                    return await channel.send(
                        f"{player.mention}\n**UNKNOWN:** finish your other battle first."
                    )
                active[player.id] = session
                active[npc.id] = session

        try:
            await session.run()
        finally:
            if battle_cog is not None:
                active = getattr(battle_cog, "active_battles", None)
                if isinstance(active, dict):
                    active.pop(player.id, None)
                    active.pop(npc.id, None)

        winner_id = getattr(session, "winner_id", None)
        if winner_id is None:
            # Never destroy inventory because the battle errored, timed out in
            # an undecidable state, or was interrupted before a result existed.
            horror_state.save_encounter(
                player.id,
                status="battle_failed",
                battle_started=False,
                bey_claimed=False,
            )
            return await channel.send(
                f"{player.mention}\n**UNKNOWN:** this battle isn't finished."
            )
        player_won = str(winner_id) == str(player.id)
        copy_id = accepted_copy_id
        claimed = await mutate_user(
            player.id,
            lambda p: _take_equipped(p, accepted_name, copy_id),
        )

        horror_state.clear_curse(player.id)
        horror_state.save_encounter(
            player.id,
            status="completed",
            battle_result=("win" if player_won else "loss"),
            bey_claimed=bool(claimed),
            claimed_bey=claimed or accepted_name,
            cursed=False,
        )

        if player_won:
            text = (
                f"{player.mention}\n"
                "**UNKNOWN:** you should not have won.\n\n"
                "👁️ **Your blader has been CLAIMED by the Horror Story.**"
            )
        else:
            text = (
                f"{player.mention}\n"
                "**UNKNOWN:** mine.\n\n"
                f"👁️ **{discord.utils.escape_markdown(claimed or accepted_name)}** "
                "was taken by UNKNOWN."
            )
        await channel.send(text)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UnknownBattleCog(bot))
