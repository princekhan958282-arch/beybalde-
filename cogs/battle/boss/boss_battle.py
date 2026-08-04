"""
boss_battle.py  —  👹 Boss battles

    ;boss           → pick a boss
    ;boss <name>    → fight one directly
    ;bosses         → the roster and what you've beaten

Self-contained on purpose. The live PvP engine in cogs/battle/session.py is
wired through StaminaManager, AbilityEngine, StabilityManager and friends, and
injecting a synthetic second player into that would mean maintaining a parallel
code path through all of it. This runs the same *rules* (five moves, the
counter table, gauge and stamina economy) over its own resolver in boss_ai.py,
which is also what lets the AI search the position.

Your real blade's stats are used, so gear and mastery still matter.
"""

import asyncio
import logging
import random
import re
import time
from typing import Optional

import discord
from discord.ext import commands

from cogs.casino import casino_wallet
from utils.database import get_beyblade, get_user, update_user
from utils.mobile_ui import MobileListView
from utils.embeds import RARITY_EMOJIS, rarity_colour

from . import blade_abilities as bk
from . import boss_abilities as ab
from . import boss_ai as ai
from . import boss_card as bcard
from . import boss_copy as bcopy
from . import boss_info as binfo
from . import drakos as dk
from . import gemini

BASE_PLAYER_HP = 4000
TURN_SECONDS   = 60
log = logging.getLogger("beyblade_bot.boss")

# ── System lock ───────────────────────────────────────────────────────────────
# The boss system is closed to players. Flip to False to reopen — nothing else
# needs changing, the fights themselves are intact underneath.
BOSS_SYSTEM_LOCKED = True
LOCK_TITLE   = "🔒 Boss Battles are closed"
LOCK_MESSAGE = ("Boss Battles are being reworked and will **open in the next "
                "update**.\n\nYour boss copies are safe — `;copies` still "
                "works, and you can still equip and sell them.")

# Commands left OPEN while the system is locked.
#
# Copies are items players already OWN. Locking those too would strand them:
# anyone with a copy currently equipped could not see it, swap off it, or sell
# it until the system reopened. The lock is meant to close new fights, not to
# freeze inventory. Add these names to the locked set if that changes.
LOCK_EXEMPT = {"copies", "copy", "equipcopy", "sellcopy"}

# Owner bypass, so the rework can be tested on the live bot while it's shut.
MASTER_ID = 956773141265391676


class BossSystemLocked(commands.CheckFailure):
    """Raised by the cog check while BOSS_SYSTEM_LOCKED is set."""

# Battle stamina the boss opens with.
#
# Note this is deliberately ABOVE ai.STAMINA_MAX (15): regeneration is clamped
# to that ceiling, so the extra is a one-off opening RESERVE, not a permanently
# higher cap. The boss can act freely early — no forced Stamina turns while it
# is still setting up — and then settles into the same economy as the player
# once the reserve is spent. Raising ai.STAMINA_MAX instead would let the boss
# top back up to 35 forever, which is a much larger buff than it looks.
BOSS_START_SP = 35.0

COOLDOWN_MIN   = 10          # per boss, per player

# One attempt per boss per DAILY_LIMIT_H, per player.
#
# Persisted in the profile rather than the in-memory `_cooldowns` dict: that
# dict is wiped by every restart, and this bot is redeployed from a phone, so
# an in-memory daily limit would reset several times a day.
#
# Charged when the fight STARTS, not when it is won. Charging on a win would
# let a player retry a loss forever, which is not a daily limit at all.
DAILY_LIMIT_H  = 2           # hours between attempts at the same boss

# NEMESIS is gated behind a Drakos clear. It is the legend-tier boss and the
# harder of the two by a wide margin, so meeting it first reads as the bot
# being broken rather than as a challenge. Drakos is the tutorial for the
# mechanics NEMESIS then punishes you for not knowing.
BOSS_REQUIRES = {"nemesis": "drakos"}


def _daily_key(key: str) -> str:
    return f"boss_daily_{key}"


def daily_remaining(profile: dict, key: str, now: Optional[float] = None
                    ) -> float:
    """Seconds until this boss can be fought again. 0 when it's available."""
    now = now if now is not None else time.time()
    last = float((profile.get("boss_daily") or {}).get(key, 0) or 0)
    return max(0.0, DAILY_LIMIT_H * 3600 - (now - last))


def charge_daily(user_id: int, key: str, now: Optional[float] = None) -> None:
    """Consume today's attempt at this boss."""
    now = now if now is not None else time.time()
    profile = get_user(user_id)
    daily = dict(profile.get("boss_daily") or {})
    daily[key] = now
    profile["boss_daily"] = daily
    update_user(user_id, profile)


def _fmt_wait(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else f"{m}m"

# ── Co-op ─────────────────────────────────────────────────────────────────
# Render the fight as a PNG card. Costs ~0.75s and ~150 KB a turn; set to
# False to fall back to the plain embed, which is instant and free.
USE_CARD           = True

LOBBY_SECONDS      = 45      # window for friends to join
MAX_PARTY          = 4
# Party damage scales roughly linearly with headcount now that the boss acts
# once per ROUND, so HP has to scale with it or two players trivialise a boss
# that's impossible solo. 1.00 keeps the fight honest at every party size.
BOSS_HP_PER_JOIN   = 1.00    # +100% boss HP per extra player


def scaled_boss_hp(cfg: dict, extra: int) -> int:
    """Boss HP for a party, honouring the per-boss ceiling.

    HP doubles per extra player, so a 4-player Drakos was 5,000 and climbing
    with no upper bound — long past the point where the fight is a war of
    attrition rather than a fight. `hp_cap` in the boss config is that ceiling.
    Both the fight and the lobby preview call this, so the number the party is
    shown is always the number it gets.
    """
    hp  = int(cfg["hp"] * (1 + BOSS_HP_PER_JOIN * extra))
    cap = cfg.get("hp_cap")
    return min(hp, int(cap)) if cap else hp
BOSS_ATK_PER_JOIN  = 0.05    # +5% boss attack per extra player


# ── Roster ────────────────────────────────────────────────────────────────────
# Roster trimmed to the two blades with full ability kits. The five generic
# bosses (Iron Sentinel, Emberfang, Tidecaller, Voidmaw, APEX-0) had no
# abilities, no specials and no drop profile — they were stat blocks, and next
# to NEMESIS and Drakos they read as filler.
BOSSES = {
    "drakos": {
        "name":       dk.DRAKOS["name"],
        "emoji":      dk.DRAKOS["emoji"],
        "difficulty": dk.DRAKOS["difficulty"],
        "persona":    dk.DRAKOS["persona"],
        "hp":         dk.DRAKOS["hp"],
        # Ceiling on party scaling. At +100% per extra player a 4-player Drakos
        # was 5,000 HP and unbounded beyond that, which turns the fight into an
        # attrition slog rather than a harder fight.
        "hp_cap":     4200,
        "attack":     dk.DRAKOS["attack"],
        "defense":    dk.DRAKOS["defense"],
        "stamina":    dk.DRAKOS["stamina"],
        "colour":     dk.DRAKOS["colour"],
        "reward":     dk.DRAKOS["reward"],
        "blurb":      dk.DRAKOS["blurb"],
        "boss_only":  True,
        "module":     "drakos",
    },
    "nemesis": {
        "name":       ab.NEMESIS["name"],
        "emoji":      ab.NEMESIS["emoji"],
        "difficulty": ab.NEMESIS["difficulty"],
        "persona":    ab.NEMESIS["persona"],
        "hp":         ab.NEMESIS["hp"],
        # Party scaling doubles HP per extra player, so 4p was 6,400 — a wall
        # that made the fight longer rather than harder, on top of NEMESIS
        # already being the boss nobody could beat. Capped like Drakos.
        "hp_cap":     4800,
        "attack":     ab.NEMESIS["attack"],
        "defense":    ab.NEMESIS["defense"],
        "stamina":    ab.NEMESIS["stamina"],
        "colour":     ab.NEMESIS["colour"],
        "reward":     ab.NEMESIS["reward"],
        "blurb":      ab.NEMESIS["blurb"],
        "god":        True,
        "boss_only":  True,
        "module":     "nemesis",
    },
}

MOVE_META = {
    ai.MOVE_ATTACK:  ("⚔️", "Attack",  discord.ButtonStyle.danger),
    ai.MOVE_DEFENSE: ("🛡️", "Defend",  discord.ButtonStyle.primary),
    ai.MOVE_STAMINA: ("🔋", "Recover", discord.ButtonStyle.success),
    ai.MOVE_CHARGE:  ("🔌", "Charge",  discord.ButtonStyle.secondary),
    ai.MOVE_SPECIAL: ("🌟", "SPECIAL", discord.ButtonStyle.success),
}


def bcopy_embed(c: dict, prof: dict) -> discord.Embed:
    """Fallback card for a copy when the PNG renderer isn't available."""
    d = bcopy.describe(c, prof)
    e = discord.Embed(
        title=f"{d['emoji']}  {c['name']}",
        description=(f"**{c['grade']}** copy of {prof['name']}\n"
                     f"`#{c['id']}` · {d['pct']:.0f}% of the original"),
        color=d["colour"],
    )
    st = c["stats"]
    e.add_field(name="Stats",
                value=(f"⚔️ {st['attack']}\n🛡️ {st['defense']}\n🔋 {st['stamina']}"),
                inline=True)
    e.add_field(name="Power",
                value=(f"{d['total']}/{d['base_total']}\n"
                       f"−{d['deficit']} vs original"),
                inline=True)
    kit = []
    if d["ultimates"]: kit.append("💀 " + ", ".join(d["ultimates"]))
    if d["specials"]:  kit.append("⚡ " + ", ".join(d["specials"]))
    if d["abilities"]: kit.append("◆ " + ", ".join(d["abilities"]))
    e.add_field(name=f"Kit — {c['loadout']}", value="\n".join(kit) or "—", inline=False)
    if c["awakening"]:
        e.add_field(
            name="✨ Awakening",
            value=(f"**{c['awaken_chance'] * 100:.0f}%** chance each round to "
                   f"return to the original's full power for one exchange."),
            inline=False,
        )
    e.set_footer(text="Boss copy · rolled once, never repeats · not in the Beypedia")
    return e


def _module_for(cfg: dict):
    """Which ability module drives this boss, if any."""
    return {"nemesis": ab, "drakos": dk}.get(cfg.get("module"))


def _make_state(cfg: dict):
    mod = _module_for(cfg)
    if mod is ab:
        return ab.BossState()
    if mod is dk:
        return dk.DrakosState()
    return None


def _bar(cur: float, mx: float, width: int = 10) -> str:
    filled = max(0, min(width, round(cur / max(1e-6, mx) * width)))
    return "█" * filled + "░" * (width - filled)


def _player_fighter(user_id: int) -> tuple[ai.Fighter, dict]:
    """Build a fighter from the player's equipped blade — copy or database."""
    profile = get_user(user_id)
    # equipped_blade() resolves an equipped boss copy to its rolled stats and
    # falls back to the database blade. Reading active_beyblade directly here
    # would look the copy's display name up in beyblades.json, miss, and drop
    # the player to the 90/90/90 default.
    blade, copy = bcopy.equipped_blade(user_id)
    # Fold in equipped parts and avatar bonuses. Boss fights previously used
    # the raw blade, so equipping either changed nothing here — a player could
    # kit out completely and see no difference against a boss.
    av = None
    if blade:
        from utils.loadout import effective_blade
        blade, _breakdown, av = effective_blade(
            user_id, profile, blade,
            # A copy is a fixed roll; parts on top would make a farmed copy
            # stronger than the boss it came from.
            include_parts=(copy is None))
    name  = (blade or {}).get("name") or profile.get("active_beyblade")
    stats = (blade or {}).get("stats") or {}

    atk = float(stats.get("attack", 90) or 90)
    dfn = float(stats.get("defense", 90) or 90)
    sta = float(stats.get("stamina", 90) or 90)

    # Trainer level and blade mastery both feed in, so progression matters.
    # A copy has no mastery row of its own — it rides the trainer level only.
    try:
        from utils.database import get_stat_multiplier
        mult = get_stat_multiplier(user_id, None if copy else name)
    except Exception:
        mult = 1.0

    hp = BASE_PLAYER_HP * mult
    if copy and stats.get("hp"):
        hp = max(hp, float(stats["hp"]) * mult)
    else:
        # Boss fights use a flat HP pool, so a blade's own hp stat has never
        # affected them. That would leave the level system's hp GROWTH purely
        # decorative, so the gain over base is added to the pool — the blade's
        # base hp still doesn't matter, but levelling it does.
        hp_bd = (_breakdown or {}).get("hp") or {}
        hp_gain = float(hp_bd.get("total", 0)) - float(hp_bd.get("base", 0))
        if hp_gain > 0:
            hp += hp_gain * mult
    if av is not None:
        from utils.loadout import effective_hp
        hp = effective_hp(user_id, hp, av)

    f  = ai.Fighter(name or "Unequipped", hp, hp,
                    atk * mult, dfn * mult, sta * mult, sp=10.0,
                    dmg_mult=ai.type_damage_mult((blade or {}).get("type")))
    return f, (blade or {})


class BossFight:
    def __init__(self, player: discord.Member, key: str, party: list = None):
        cfg = BOSSES[key]
        self.key    = key
        self.cfg    = cfg
        self.player = player                       # host / current turn holder
        self.party  = list(party or [player])

        # Every member gets their own fighter built from their own blade, and
        # their own ability kit.
        self.fighters: dict[int, ai.Fighter] = {}
        self.kits: dict[int, bk.BladeKit] = {}
        for m in self.party:
            f, blade = _player_fighter(m.id)
            self.fighters[m.id] = f
            self.kits[m.id] = bk.kit_for(blade)
        self.foe, self.blade = _player_fighter(player.id)
        self.foe = self.fighters[player.id]
        self.kit = self.kits[player.id]
        self.turn_index = 0
        self.target = player

        # Scale the boss to the party. One extra body would otherwise halve the
        # fight, so HP climbs steeply and damage only nudges up — more players
        # means a longer fight, not a trivially safer one.
        extra   = max(0, len(self.party) - 1)
        hp      = scaled_boss_hp(cfg, extra)
        attack  = cfg["attack"] * (1 + BOSS_ATK_PER_JOIN * extra)

        self.boss = ai.Fighter(cfg["name"], hp, hp,
                               attack, cfg["defense"], cfg["stamina"],
                               sp=BOSS_START_SP, is_boss=True,
                               state=_make_state(cfg))
        self.model   = ai.OpponentModel()
        self.turn    = 0
        self.log: list[str] = []
        self.finished = False
        self.result: Optional[str] = None
        self.phase_two = False
        self.line = ""
        self.last_special = None

    def foe_habit(self) -> str:
        h = self.model.history[-6:]
        if not h:
            return "nothing obvious"
        top = max(set(h), key=h.count)
        return f"{top} ({h.count(top)}/{len(h)} recent turns)"

    def state(self) -> dict:
        return {
            "boss_hp_pct": self.boss.hp / self.boss.max_hp * 100,
            "foe_hp_pct":  self.foe.hp / self.foe.max_hp * 100,
            "turn":        self.turn,
            "foe_habit":   self.foe_habit(),
        }

    @property
    def alive_party(self) -> list:
        return [m for m in self.party if self.fighters[m.id].alive()]

    @property
    def active(self):
        alive = self.alive_party
        if not alive:
            return self.party[0]
        return alive[self.turn_index % len(alive)]

    def _advance_turn(self) -> None:
        alive = self.alive_party
        if alive:
            self.turn_index = (self.turn_index + 1) % len(alive)
        self.player = self.active
        self.foe = self.fighters[self.player.id]
        self.kit = self.kits[self.player.id]

    def step(self, player_move: str) -> dict:
        self.turn += 1
        self.model.observe(player_move)

        # The boss acts ONCE per party round, not once per player.
        #
        # With a strict rotation the party's combined damage stayed identical to
        # a solo run (only one member moves per turn) while boss HP climbed 50%
        # per member — measured at 97% -> 5% win rate going from 1 to 4 players
        # against Emberfang. Grouping up was a straight punishment. Now every
        # member gets a swing and the boss replies once at the end of the round,
        # so the +50% HP is a counterweight instead of a wall.
        alive = self.alive_party
        is_round_end = (len(alive) <= 1) or (self.turn_index % len(alive) == len(alive) - 1)

        boss_move, _vals = ai.choose_move(
            self.boss, self.foe, self.model,
            difficulty=self.cfg["difficulty"])
        if not is_round_end:
            boss_move = ai.MOVE_CHARGE if self.boss.can(ai.MOVE_CHARGE) else ai.MOVE_STAMINA

        # NEMESIS fires named Specials/Ultimates rather than the generic one.
        god_special = None
        mod = _module_for(self.cfg)
        if is_round_end and self.boss.state and mod and boss_move == ai.MOVE_SPECIAL:
            god_special = mod.pick_special(
                self.boss.state,
                self.boss.gauge >= ai.SPECIAL_GAUGE_MAX,
                self.foe.hp / self.foe.max_hp,
                self.boss.hp / self.boss.max_hp,
            )

        # The boss swings at a random living member, not whoever just moved.
        # Focusing the actor meant one player soaked every hit while the rest
        # watched — and in a party the same person kept getting hit because
        # they were the one taking turns.
        alive_now = self.alive_party
        if is_round_end and len(alive_now) > 1:
            if god_special == "zerohour":
                # The Protocol is an execution order, not a swing — it goes to
                # the biggest threat rather than a random member. "Strongest"
                # is remaining HP x attack: raw attack alone would send it at
                # a glass cannon already at 5% HP.
                self.target = max(
                    alive_now,
                    key=lambda m: (self.fighters[m.id].hp
                                   * self.fighters[m.id].eff_attack))
            else:
                self.target = random.choice(alive_now)
        else:
            self.target = self.player
        target_fighter = self.fighters[self.target.id]

        # Snapshot the fighter outcome() will actually judge (self.foe, the
        # actor), not target_fighter. In a party the boss swings at a random
        # member, so those are different objects, and feeding outcome() another
        # player's HP fraction decides the double-KO tiebreak off the wrong
        # health bar.
        before = ai.hp_fractions(self.boss, self.foe)

        # Blade abilities: stat multipliers are applied to the fighter for the
        # exchange, then restored, so the modifiers never compound turn on turn.
        kit  = self.kit
        mult = kit.stats_for(self.foe.hp / self.foe.max_hp)
        base_atk, base_def, base_sta = self.foe.attack, self.foe.defense, self.foe.stamina_stat
        self.foe.attack       = base_atk * mult["attack"]
        self.foe.defense      = base_def * mult["defense"]
        self.foe.stamina_stat = base_sta * mult["stamina"]
        try:
            if god_special:
                report = self._fire_special(god_special, player_move, mod)
            else:
                report = ai.resolve(self.boss, self.foe, boss_move, player_move)

            # Move the boss's damage onto whoever it actually aimed at.
            if target_fighter is not self.foe and report.get("dmg_to_b", 0) > 0:
                dmg = report["dmg_to_b"]
                self.foe.hp = min(self.foe.max_hp, self.foe.hp + dmg)
                target_fighter.hp = max(0.0, target_fighter.hp - dmg)
                report["redirected_to"] = self.target
        finally:
            self.foe.attack, self.foe.defense, self.foe.stamina_stat = (
                base_atk, base_def, base_sta)

        report.update(self._apply_kit(kit, report))
        res = ai.outcome(self.boss, self.foe, before)

        if not self.boss.alive():
            self.finished, self.result = True, "win"
        elif not self.alive_party:
            self.finished, self.result = True, "loss"
        elif res == "draw" and len(self.party) == 1:
            self.finished, self.result = True, "draw"

        pm = MOVE_META[player_move][1]
        bm = (f"{mod.SPECIALS[god_special]['emoji']} {mod.SPECIALS[god_special]['name']}"
              if god_special and mod else MOVE_META[boss_move][1])
        bits = [f"**T{self.turn}** you **{pm}** vs **{bm}**"]
        if report["dmg_to_a"] > 0:
            bits.append(f"→ 💥 {report['dmg_to_a']:.0f} to boss")
        if report["dmg_to_b"] > 0:
            who = report.get("redirected_to")
            bits.append(f"→ 💢 {report['dmg_to_b']:.0f} to "
                        + (who.display_name if who else "you"))
        if report["heal_a"] > 0:
            bits.append(f"→ boss +{report['heal_a']:.0f}")
        if report["heal_b"] > 0:
            bits.append(f"→ you +{report['heal_b']:.0f}")
        self.log.append(" ".join(bits))
        self.log = self.log[-3:]

        report["boss_move"]   = boss_move
        report["god_special"]  = god_special
        report["actor"]        = self.player
        if not self.finished:
            self._advance_turn()
        return report

    def _apply_kit(self, kit, report: dict) -> dict:
        """Post-exchange ability effects: amp, pierce, reflect, lifesteal."""
        extra = {}
        dealt = report.get("dmg_to_a", 0.0)
        taken = report.get("dmg_to_b", 0.0)

        if dealt > 0 and kit.dmg_amp:
            bonus = dealt * kit.dmg_amp
            self.boss.hp = max(0.0, self.boss.hp - bonus)
            extra["ability_bonus"] = bonus

        # Damage reduction and lifesteal are paid out as HP AFTER resolve() has
        # already clamped the loser to 0, so a fighter that died this exchange
        # was refunded back above zero and simply carried on. Anything with
        # reduction or heal_pct was therefore unkillable: Eternal Blaze Garuda
        # (reduction 0.20, heal_pct 0.25) beat both bosses 100% of the time,
        # while Azeroth Veyrath — 0.00 on both — died normally and won 0%.
        # That, not healing, is why Stamina blades could not lose.
        #
        # These stay refunds rather than becoming pre-damage modifiers because
        # `report` is already resolved by the time we get here; gating them on
        # still being alive is the minimal correct fix.
        alive = self.foe.hp > 0
        if taken > 0 and kit.reduction and alive:
            back = taken * kit.reduction
            self.foe.hp = min(self.foe.max_hp, self.foe.hp + back)
            extra["ability_soak"] = back
        if taken > 0 and kit.reflect:
            self.boss.hp = max(0.0, self.boss.hp - kit.reflect)
            extra["ability_reflect"] = kit.reflect
        if dealt > 0 and kit.heal_pct and alive:
            heal = dealt * kit.heal_pct
            self.foe.hp = min(self.foe.max_hp, self.foe.hp + heal)
            extra["ability_heal"] = heal
        return extra

    def _fire_special(self, key: str, player_move: str, mod) -> dict:
        """Resolve one of NEMESIS's named Specials. Mirrors ai.resolve()'s
        bookkeeping so the two paths can't drift apart."""
        st   = self.boss.state
        spec = mod.SPECIALS[key]

        self.boss.sp = max(0.0, self.boss.sp - ai.STAMINA_COST[ai.MOVE_SPECIAL])
        self.foe.sp  = max(0.0, self.foe.sp - ai.STAMINA_COST[player_move])

        dmg, effects = mod.special_damage(
            key, self.boss.eff_attack, st,
            self.foe.eff_defense, self.foe.hp / self.foe.max_hp,
            ai.DMG_SCALE,
        )
        # A block still helps, unless the move is flagged true damage.
        if player_move == ai.MOVE_DEFENSE and not spec.get("true_damage"):
            dmg *= 0.55

        # The player's own move still lands.
        back = 0.0
        if player_move in (ai.MOVE_ATTACK, ai.MOVE_SPECIAL):
            mult = ai.SPECIAL_MULT if player_move == ai.MOVE_SPECIAL else 1.0
            # Same reason as the riposte in ai.resolve(): this hand-rolled
            # damage line bypasses _raw_damage, so it needs dmg_mult explicitly
            # or the player's counter during a boss Special ignores their type.
            raw  = self.foe.eff_attack * ai.DMG_SCALE * mult * self.foe.dmg_mult
            back = max(1.0, raw * max(0.4, 1 - self.boss.eff_defense / 400.0))
            if hasattr(st, "absorb"):
                back, _shattered = st.absorb(back)
            if st.reflect():
                dmg += st.reflect()

        heal = effects.get("drain", 0.0)
        self.foe.hp  = max(0.0, self.foe.hp - dmg)
        self.boss.hp = max(0.0, min(self.boss.max_hp, self.boss.hp - back + heal))

        if effects.get("strip"):
            self.foe.gauge = 0.0
        if effects.get("freeze"):
            st.freeze_turns = effects["freeze"]

        self.boss.gauge = 0.0
        if back > 0:
            st.bank_debt(back)
            self.foe.gauge = min(ai.SPECIAL_GAUGE_MAX,
                                 self.foe.gauge + ai.GAUGE_PER_DMG_TAKEN)
        if spec["ultimate"]:
            if hasattr(st, "ultimates_used"):
                st.ultimates_used.add(key)
            else:
                st.ultimate_used = True
        if key == "zerohour" and hasattr(st, "protocol_fired"):
            # Once only, for the whole fight.
            st.protocol_fired = True

        st.tick()
        st.flip_stance()
        if self.boss.alive() and st.should_ascend(self.boss.hp / self.boss.max_hp):
            st.ascend()

        self.last_special = spec
        return {"dmg_to_a": back, "dmg_to_b": dmg,
                "heal_a": heal, "heal_b": 0.0,
                "note_a": spec["name"], "note_b": "",
                "debt_spent": effects.get("debt_spent", 0.0)}

    def card_state(self) -> dict:
        """Everything boss_card.render() needs, and nothing it doesn't."""
        cfg = self.cfg
        prof = binfo.REGISTRY.get(self.key) or {}
        theme = prof.get("card_theme") or {}
        art = ""
        try:
            from utils import info_card as _ic
            art = _ic._art_src({"name": cfg["name"],
                                "image_url": prof.get("image_url") or ""})
        except Exception:
            art = ""
        verdict = ""
        if self.finished:
            verdict = {"win": "🏆 VICTORY", "loss": "💀 DEFEATED",
                       "draw": "🤝 DOUBLE KO"}.get(self.result, "")
        return {
            "boss_name":  cfg["name"],
            "tier":       prof.get("rarity", cfg.get("difficulty", "boss")),
            "difficulty": cfg.get("difficulty", ""),
            "boss_hp":    self.boss.hp, "boss_max": self.boss.max_hp,
            "boss_gauge": self.boss.gauge, "boss_sp": self.boss.sp,
            "gauge_max":  ai.SPECIAL_GAUGE_MAX,
            "art_src":    art,
            "accent":     theme.get("accent", "#c77dff"),
            "glow":       theme.get("glow", "#6a0dad"),
            "tint":       theme.get("tint", "#140a1e"),
            "line":       self.line,
            "turn":       self.turn,
            "alive":      len(self.alive_party), "total": len(self.party),
            "verdict":    verdict,
            "log":        [re.sub(r"\*\*|`", "", l) for l in self.log[-3:]],
            "party": [
                {
                    "name": m.display_name,
                    "hp": self.fighters[m.id].hp,
                    "max_hp": self.fighters[m.id].max_hp,
                    "gauge": self.fighters[m.id].gauge,
                    "gauge_max": ai.SPECIAL_GAUGE_MAX,
                    "sp": self.fighters[m.id].sp,
                    "alive": self.fighters[m.id].alive(),
                    "active": (not self.finished and m.id == self.active.id),
                    "ready": self.fighters[m.id].gauge >= ai.SPECIAL_GAUGE_MAX,
                }
                for m in self.party
            ],
        }

    def moment(self) -> str:
        if self.finished:
            return "victory" if self.result == "loss" else "defeat"
        bp = self.boss.hp / self.boss.max_hp
        fp = self.foe.hp / self.foe.max_hp
        if bp < 0.5 and not self.phase_two:
            self.phase_two = True
            return "phase"
        return "winning" if bp > fp else "losing"


class BossView(discord.ui.View):
    def __init__(self, cog, fight: BossFight):
        super().__init__(timeout=TURN_SECONDS * 6)
        self.cog     = cog
        self.fight   = fight
        self.message: Optional[discord.Message] = None
        self.busy    = False
        self._build()

    def _build(self):
        self.clear_items()
        f = self.fight
        for i, move in enumerate(ai.ALL_MOVES):
            emoji, label, style = MOVE_META[move]
            btn = discord.ui.Button(
                label=label, emoji=emoji, style=style,
                row=0 if i < 3 else 1,
                disabled=f.finished or not f.foe.can(move),
            )
            btn.callback = self._make_cb(move)
            self.add_item(btn)

    def _make_cb(self, move: str):
        async def cb(interaction: discord.Interaction):
            f = self.fight
            if not any(m.id == interaction.user.id for m in f.party):
                return await interaction.response.send_message(
                    "You're not in this fight — run `;boss` to start your own.",
                    ephemeral=True)
            if interaction.user.id != f.active.id:
                return await interaction.response.send_message(
                    f"Not your turn — it's **{f.active.display_name}**'s move.",
                    ephemeral=True)
            if f.finished:
                return await interaction.response.defer()
            if self.busy:
                return await interaction.response.defer()
            if not f.foe.can(move):
                return await interaction.response.send_message(
                    "Not enough stamina for that.", ephemeral=True)

            self.busy = True
            try:
                f.step(move)
                self._build()
                # Defer first: rendering the card takes ~0.75s and Discord
                # discards an interaction that isn't answered inside 3s.
                await interaction.response.defer()
                await self.push(interaction)
                # Dialogue is fetched AFTER the turn is already shown, so a slow
                # or dead API can never delay the fight itself — and only every
                # few turns, because the free tier allows ~10 requests a minute
                # and a 20-turn fight speaking every turn exhausts it.
                if (f.turn % gemini.SPEAK_EVERY_N_TURNS == 0
                        or f.finished
                        or f.moment() == "phase"):
                    self.cog._spawn(self._speak())
                if f.finished:
                    await self.cog.finish(f, interaction.channel)
                    self.stop()
            finally:
                self.busy = False
        return cb

    async def push(self, interaction=None):
        """Redraw the fight — PNG card when it renders, embed when it doesn't."""
        buf = None
        if USE_CARD:
            buf = await bcard.render(self.fight.card_state())

        if buf is not None:
            fname = getattr(buf, "name", "boss.png")
            file = discord.File(buf, filename=fname)
            e = discord.Embed(color=self.fight.cfg["colour"])
            e.set_image(url=f"attachment://{fname}")
            try:
                if interaction is not None:
                    await interaction.edit_original_response(
                        embed=e, attachments=[file], view=self)
                elif self.message:
                    await self.message.edit(embed=e, attachments=[file], view=self)
                return
            except Exception:
                pass  # fall through to the embed

        try:
            if interaction is not None:
                await interaction.edit_original_response(
                    embed=self.embed(), attachments=[], view=self)
            elif self.message:
                await self.message.edit(embed=self.embed(), view=self)
        except Exception:
            pass

    async def _speak(self):
        await self.refresh_line(self.fight.moment())

    async def refresh_line(self, moment: str):
        """Fetch a line and swap it into the already-posted embed."""
        f = self.fight
        try:
            line = await gemini.say_with_deadline(
                f.cfg["name"], f.cfg["persona"], moment, f.state())
            if not line or line == f.line:
                return
            f.line = line
            if self.message and not f.finished:
                await self.push()
        except Exception:
            pass

    def embed(self) -> discord.Embed:
        f   = self.fight
        cfg = f.cfg
        e = discord.Embed(
            title=f"{cfg['emoji']}  {cfg['name']}",
            color=cfg["colour"],
        )
        if f.line:
            e.description = f"*“{f.line}”*"

        e.add_field(
            name=f"{cfg['emoji']} Boss",
            value=(f"`{_bar(f.boss.hp, f.boss.max_hp)}` {f.boss.hp:.0f}\n"
                   f"🌀 {int(f.boss.gauge)}/{ai.SPECIAL_GAUGE_MAX} · "
                   f"💨 {f.boss.sp:.1f}"),
            inline=True,
        )
        if len(f.party) == 1:
            e.add_field(
                name=f"🌀 {f.foe.name}",
                value=(f"`{_bar(f.foe.hp, f.foe.max_hp)}` {f.foe.hp:.0f}\n"
                       f"🌀 {int(f.foe.gauge)}/{ai.SPECIAL_GAUGE_MAX} · "
                       f"💨 {f.foe.sp:.1f}"),
                inline=True,
            )
        else:
            lines = []
            for m in f.party:
                ftr = f.fighters[m.id]
                mark = ("▶️" if (not f.finished and m.id == f.active.id)
                        else ("💀" if not ftr.alive() else "　"))
                ready = "✨" if ftr.gauge >= ai.SPECIAL_GAUGE_MAX else ""
                lines.append(
                    f"{mark} **{m.display_name}**{ready}\n"
                    f"　`{_bar(ftr.hp, ftr.max_hp, 6)}` {ftr.hp:.0f}\n"
                    f"　🌀 {int(ftr.gauge)}/{ai.SPECIAL_GAUGE_MAX} · 💨 {ftr.sp:.1f}")
            e.add_field(name=f"🌀 Party ({len(f.alive_party)}/{len(f.party)})",
                        value="\n".join(lines), inline=False)
        if f.log:
            e.add_field(name="Last exchanges", value="\n".join(f.log), inline=False)

        if f.finished:
            verdict = {"win": "🏆 You won!", "loss": "💀 Defeated.",
                       "draw": "🤝 Double knockout."}[f.result]
            e.add_field(name="Result", value=verdict, inline=False)
        if not f.finished and len(f.party) > 1:
            e.description = ((e.description + "\n") if e.description else "") + \
                f"### ▶️ {f.active.display_name}'s turn"
        e.set_footer(text=f"Turn {f.turn} · difficulty: {cfg['difficulty']}"
                          + ("" if gemini.available() else " · dialogue offline"))
        return e

    async def on_timeout(self):
        # Release the player. Without this an idle fight left them in _active
        # forever and every later ;boss answered "you're already in a fight".
        self.fight.finished = True
        for m in self.fight.party:
            self.cog._active.discard(m.id)
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                e = self.embed()
                e.set_footer(text="⏰ Timed out — the boss lost interest.")
                await self.message.edit(embed=e, view=self)
            except Exception:
                pass


class BossLobbyView(discord.ui.View):
    """Friends can join before the fight starts.

    Each extra player adds +50% boss HP and +5% boss attack, so bringing a
    party makes the fight longer rather than free.
    """

    def __init__(self, cog, host: discord.Member, key: str):
        super().__init__(timeout=LOBBY_SECONDS)
        self.cog     = cog
        self.host    = host
        self.key     = key
        self.party   = [host]
        self.started = False
        self.message: Optional[discord.Message] = None

    def build_embed(self) -> discord.Embed:
        cfg = BOSSES[self.key]
        extra = len(self.party) - 1
        hp  = scaled_boss_hp(cfg, extra)
        atk = cfg["attack"] * (1 + BOSS_ATK_PER_JOIN * extra)
        e = discord.Embed(
            title=f"{cfg['emoji']}  {cfg['name']}",
            description=(f"**{self.host.display_name}** is challenging this boss.\n"
                         f"Press **⚔️ Join** to fight alongside them."),
            color=cfg["colour"],
        )
        e.add_field(
            name=f"Party ({len(self.party)}/{MAX_PARTY})",
            value="\n".join(f"• {m.display_name}" for m in self.party),
            inline=True,
        )
        e.add_field(
            name="Boss scaling",
            value=(f"❤️ {hp:,} HP\n⚔️ {atk:.0f} ATK"
                   + (f"\n*+{extra * 50}% HP, +{extra * 5}% ATK*" if extra else "")),
            inline=True,
        )
        e.set_footer(text=f"Each player adds +50% boss HP and +5% boss attack  •  "
                          f"starts in {LOBBY_SECONDS}s")
        return e

    @discord.ui.button(label="Join", emoji="⚔️", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.started:
            return await interaction.response.defer()
        if any(m.id == interaction.user.id for m in self.party):
            return await interaction.response.send_message("You're already in.",
                                                           ephemeral=True)
        if len(self.party) >= MAX_PARTY:
            return await interaction.response.send_message(
                f"Party is full ({MAX_PARTY}).", ephemeral=True)
        if interaction.user.id in self.cog._active:
            return await interaction.response.send_message(
                "You're already in a boss fight.", ephemeral=True)
        # Joiners go through the SAME gate as the host. Without this a player
        # who had already used today's attempt (or hadn't unlocked the boss)
        # could still join someone else's lobby and take the rewards.
        ok, msg = self.cog._can_fight(interaction.user.id, self.key)
        if not ok:
            return await interaction.response.send_message(msg, ephemeral=True)

        self.party.append(interaction.user)
        self.cog._active.add(interaction.user.id)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Start", emoji="▶", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.host.id:
            return await interaction.response.send_message(
                "Only the host can start.", ephemeral=True)
        await self._launch(interaction)

    async def _launch(self, interaction: Optional[discord.Interaction]):
        if self.started:
            return
        self.started = True
        for c in self.children:
            c.disabled = True
        fight = BossFight(self.host, self.key, party=self.party)
        # Charge the daily attempt for EVERY member, at launch. Charging only
        # the host would let three friends farm a boss by taking turns hosting,
        # and charging on the result would make a loss free.
        for member in self.party:
            try:
                charge_daily(member.id, self.key)
            except Exception as e:                   # noqa: BLE001
                log.warning("couldn't charge daily for %s: %s", member.id, e)
        view  = BossView(self.cog, fight)
        fight.line = gemini.canned("intro")

        buf = await bcard.render(fight.card_state()) if USE_CARD else None
        if buf is not None:
            fname = getattr(buf, "name", "boss.png")
            e = discord.Embed(color=fight.cfg["colour"])
            e.set_image(url=f"attachment://{fname}")
            payload = {"embed": e, "file": discord.File(buf, filename=fname),
                       "view": view}
        else:
            payload = {"embed": view.embed(), "view": view}

        if interaction is not None:
            await interaction.response.edit_message(view=self)
            view.message = await interaction.followup.send(**payload, wait=True)
        elif self.message:
            await self.message.edit(view=self)
            view.message = await self.message.channel.send(**payload)
        self.cog._spawn(view.refresh_line("intro"))

    async def on_timeout(self):
        if not self.started:
            await self._launch(None)


class BossSelect(discord.ui.Select):
    def __init__(self, cleared: set):
        opts = []
        for key, cfg in BOSSES.items():
            done = " ✅" if key in cleared else ""
            opts.append(discord.SelectOption(
                label=f"{cfg['name']}{done}",
                value=key,
                emoji=cfg["emoji"],
                description=f"{cfg['difficulty']} · 🪙 {cfg['reward']['coins']:,}"[:100],
            ))
        super().__init__(placeholder="👹 Choose your opponent…", options=opts)

    async def callback(self, interaction: discord.Interaction):
        view: "BossPickView" = self.view
        if interaction.user.id != view.player.id:
            return await interaction.response.send_message("Not your menu.", ephemeral=True)
        await view.cog.launch(interaction, self.values[0])
        view.stop()


class BossPickView(discord.ui.View):
    def __init__(self, cog, player, cleared: set):
        super().__init__(timeout=120)
        self.cog = cog
        self.player = player
        self.add_item(BossSelect(cleared))
        self.message = None

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class _SellCopyConfirm(discord.ui.View):
    """Selling destroys a roll that can never be recovered — a 1-in-10,000,000
    Perfect is one mis-tap away from gone — so the sale is confirmed first."""

    def __init__(self, owner, copy: dict, value: int):
        super().__init__(timeout=30)
        self.owner, self.copy, self.value = owner, copy, value
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message(
                "That isn't your copy.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Sell", emoji="\U0001f4b0",
                       style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _btn):
        gone = bcopy.remove_copy(self.owner.id, self.copy["id"])
        if gone is None:
            return await interaction.response.edit_message(
                content="\u274c That copy is already gone.", view=None)
        prof = get_user(self.owner.id)
        prof["coins"] = prof.get("coins", 0) + self.value
        update_user(self.owner.id, prof)
        self.stop()
        await interaction.response.edit_message(
            content=(f"\U0001f4b0 Sold **{gone['name']}** "
                     f"({gone['grade']} \u00b7 `#{gone['id']}`) for "
                     f"\U0001fa99 **{self.value:,}**."),
            view=None)

    @discord.ui.button(label="Keep", emoji="\U0001f6e1\ufe0f",
                       style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _btn):
        self.stop()
        await interaction.response.edit_message(
            content="\U0001f6e1\ufe0f Kept.", view=None)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="\u23f1\ufe0f Sale expired.", view=None)
            except Exception:
                pass


class BossCog(commands.Cog, name="Boss"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active: set[int] = set()
        self._cooldowns: dict[tuple[int, str], float] = {}
        # asyncio only holds a weak reference to a running task, so a bare
        # create_task() can be garbage-collected mid-flight and the boss just
        # goes quiet. Keep a strong ref until it finishes.
        self._tasks: set = set()

    async def cog_check(self, ctx: commands.Context) -> bool:
        """One gate for the whole cog while the system is locked.

        Checked here rather than decorating each command so a command added
        later is locked by default — the failure mode of forgetting a
        decorator is a command that quietly stays open.

        The bot owner is exempt so the rework can be tested in place.
        """
        if not BOSS_SYSTEM_LOCKED:
            return True
        if ctx.command and ctx.command.name in LOCK_EXEMPT:
            return True
        if ctx.author.id == MASTER_ID:
            return True
        raise BossSystemLocked()

    async def cog_command_error(self, ctx: commands.Context,
                                error: Exception) -> None:
        if isinstance(getattr(error, "original", error), BossSystemLocked) \
                or isinstance(error, (BossSystemLocked, commands.CheckFailure)):
            e = discord.Embed(title=LOCK_TITLE, description=LOCK_MESSAGE,
                              colour=0x4E5058)
            try:
                await ctx.send(embed=e)
            except Exception:                        # noqa: BLE001
                pass
            return
        log.warning("boss command error in %s: %s",
                    getattr(ctx.command, "name", "?"), error)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def cog_unload(self):
        for t in list(self._tasks):
            t.cancel()

    # ── Rewards ──────────────────────────────────────────────────────────────
    async def finish(self, fight: BossFight, channel):
        for m in fight.party:
            self._active.discard(m.id)
        cfg = fight.cfg

        if fight.result != "win":
            line = await gemini.say_with_deadline(
                cfg["name"], cfg["persona"], "victory", fight.state())
            e = discord.Embed(
                title=f"💀  {cfg['name']} stands unbeaten",
                description=f"*“{line}”*\n\nTry again — `;boss {fight.key}`",
                color=0xe74c3c,
            )
            if fight.result == "draw":
                e.title = f"🤝  Double knockout against {cfg['name']}"
            return await channel.send(f"{fight.player.mention}", embed=e)

        reward = cfg["reward"]

        # Every member of the party is paid, not just fight.player.
        #
        # fight.player is the ROTATING active member (_advance_turn sets it from
        # the turn index), so on a co-op clear it is simply whoever happened to
        # be up when the boss died. Paying only that one meant the other three
        # got no coins, no clear credit and — the visible symptom — no boss copy
        # at all: the drop embed announced a blade that had been written to
        # somebody else's profile, so it never appeared in their ;inventory.
        #
        # Each member also rolls their OWN copy. Sharing one roll would mean
        # three players hold the same id, and find_copy/;copy resolve by id.
        prof  = binfo.REGISTRY.get(fight.key)
        drops: list[tuple] = []          # (member, rolled copy | None)
        first_any = False

        for member in fight.party:
            profile = get_user(member.id)
            first   = fight.key not in set(profile.get("bosses_cleared") or [])
            first_any = first_any or first

            profile["coins"] = profile.get("coins", 0) + reward["coins"] * (2 if first else 1)
            cleared = list(profile.get("bosses_cleared") or [])
            if first:
                cleared.append(fight.key)
            profile["bosses_cleared"] = cleared
            update_user(member.id, profile)

            await casino_wallet.credit(
                member.id, reward["casino"] * (2 if first else 1))
            self._cooldowns[(member.id, fight.key)] = time.time()

            rolled = None
            if prof is not None:
                rolled = bcopy.roll_copy(prof)
                # add_copy re-reads the profile, so it must run AFTER the
                # update_user above or the coin write would clobber the copy.
                bcopy.add_copy(member.id, rolled)
            drops.append((member, rolled))

        # Headline numbers reflect a first clear if it was one for anybody.
        coins  = reward["coins"]  * (2 if first_any else 1)
        casino = reward["casino"] * (2 if first_any else 1)

        line = await gemini.say_with_deadline(
            cfg["name"], cfg["persona"], "defeat", fight.state())
        e = discord.Embed(
            title=f"🏆  {cfg['name']} defeated!",
            description=f"*“{line}”*",
            color=0x2ecc71,
        )
        e.add_field(name="🪙 Beycoins", value=f"+{coins:,}", inline=True)
        e.add_field(name="🎰 Casino", value=f"+{casino:,}", inline=True)
        for member, drop in drops:
            if not drop:
                continue
            d = bcopy.describe(drop, prof)
            kit = []
            if d["ultimates"]: kit.append("💀 " + ", ".join(d["ultimates"]))
            if d["specials"]:  kit.append("⚡ " + ", ".join(d["specials"]))
            if d["abilities"]: kit.append("◆ " + ", ".join(d["abilities"]))
            if drop["awakening"]:
                kit.append(f"✨ Awakening ({drop['awaken_chance'] * 100:.0f}%)")
            who = "" if len(drops) == 1 else f" — {member.display_name}"
            e.add_field(
                name=f"{d['emoji']} {drop['grade']} copy dropped!{who}",
                value=(f"**{drop['name']}** · `#{drop['id']}`\n"
                       f"{d['total']}/{d['base_total']} stats "
                       f"({d['pct']:.0f}% of the original)\n"
                       + "\n".join(kit)),
                inline=False,
            )
        if first_any:
            e.add_field(name="✨ First clear", value="Rewards doubled!", inline=False)
        e.set_footer(text=f"Cleared in {fight.turn} turns"
                          + ("  •  ;copies to view your drops" if drops else ""))
        mentions = " ".join(m.mention for m in fight.party)
        await channel.send(mentions, embed=e)

    # ── Launch ───────────────────────────────────────────────────────────────
    async def launch(self, interaction: discord.Interaction, key: str):
        # A picker posted BEFORE the lock still has live buttons until its view
        # times out, so the command gate alone doesn't close every door.
        if BOSS_SYSTEM_LOCKED and interaction.user.id != MASTER_ID:
            return await interaction.response.send_message(
                embed=discord.Embed(title=LOCK_TITLE, description=LOCK_MESSAGE,
                                    colour=0x4E5058),
                ephemeral=True)
        player = interaction.user
        ok, msg = self._can_fight(player.id, key)
        if not ok:
            return await interaction.response.send_message(msg, ephemeral=True)

        self._active.add(player.id)
        lobby = BossLobbyView(self, player, key)
        await interaction.response.edit_message(embed=lobby.build_embed(), view=lobby)
        try:
            lobby.message = await interaction.original_response()
        except Exception:
            pass

    def _can_fight(self, user_id: int, key: str) -> tuple[bool, str]:
        if user_id in self._active:
            return False, "❌ You're already in a boss fight!"
        profile = get_user(user_id)
        if not profile.get("active_beyblade"):
            return False, "❌ Equip a blade first — `;equip <name>` (or `;start`)."

        # Progression lock.
        need = BOSS_REQUIRES.get(key)
        if need and need not in set(profile.get("bosses_cleared") or []):
            need_name = BOSSES.get(need, {}).get("name", need)
            this_name = BOSSES.get(key, {}).get("name", key)
            return False, (f"🔒 **{this_name}** is locked.\n"
                           f"Beat **{need_name}** first — `;boss {need}`.")

        # Daily limit, persisted so a restart doesn't hand out a free attempt.
        left = daily_remaining(profile, key)
        if left > 0:
            return False, (f"🌙 You've faced that boss recently.\n"
                           f"Next attempt in **{_fmt_wait(left)}**.")

        last = self._cooldowns.get((user_id, key), 0)
        left = COOLDOWN_MIN * 60 - (time.time() - last)
        if left > 0:
            return False, f"⏳ You've just beaten that one. Try again in {int(left // 60) + 1}m."
        return True, ""

    # ── Commands ─────────────────────────────────────────────────────────────
    @commands.command(name="boss", aliases=["bossfight", "raidboss"])
    async def boss(self, ctx: commands.Context, *, name: str = None):
        """👹 Fight a boss. `;boss` to pick one."""
        profile = get_user(ctx.author.id)
        cleared = set(profile.get("bosses_cleared") or [])

        if name:
            q = name.strip().lower()
            key = next((k for k, c in BOSSES.items()
                        if k == q or c["name"].lower() == q or q in c["name"].lower()), None)
            if key is None:
                return await ctx.send(
                    "❌ No such boss. `;bosses` to see the roster.")
            ok, msg = self._can_fight(ctx.author.id, key)
            if not ok:
                return await ctx.send(msg)

            self._active.add(ctx.author.id)
            lobby = BossLobbyView(self, ctx.author, key)
            try:
                lobby.message = await ctx.send(embed=lobby.build_embed(), view=lobby)
            except discord.DiscordException:
                self._active.discard(ctx.author.id)
            return

        view = BossPickView(self, ctx.author, cleared)

        def _status(k: str) -> str:
            """Why a boss can't be fought right now, if it can't.

            Shown up front so a player isn't told 'locked' only after picking
            it — the roster should explain itself.
            """
            need = BOSS_REQUIRES.get(k)
            if need and need not in cleared:
                return f"🔒 Beat {BOSSES[need]['name']} first"
            left = daily_remaining(profile, k)
            if left > 0:
                return f"🌙 Again in {_fmt_wait(left)}"
            return "✅ Ready now"

        e = discord.Embed(
            title="👹  Boss Battles",
            description="\n".join(
                f"{c['emoji']} **{c['name']}**{' ✅' if k in cleared else ''}\n"
                f"　*{c['difficulty']}* · 🪙 {c['reward']['coins']:,}\n"
                f"　{_status(k)}"
                for k, c in BOSSES.items()
            ),
            color=0x9b59b6,
        )
        e.set_footer(text=f"One attempt per boss every {DAILY_LIMIT_H}h  "
                          f"•  first clear pays double")
        e.add_field(
            name="👁️ God tier",
            value=("**NEMESIS ÆTHERION** is boss-only — it can't be caught, "
                   "bought, traded or spawned."),
            inline=False,
        )
        view.message = await ctx.send(embed=e, view=view)

    @commands.command(name="bossinfo", aliases=["bossbey", "bossblade"])
    async def bossinfo(self, ctx: commands.Context, *, name: str = None):
        """📖 Info card for a boss-only blade."""
        if not name:
            return await ctx.send(
                "📖 Usage: `;bossinfo <name>`\nAvailable: "
                + " · ".join(f"**{n}**" for n in binfo.names()))
        prof = binfo.find(name)
        if prof is None:
            return await ctx.send(
                f"❌ No boss blade called **{name}**.\nTry: "
                + " · ".join(f"`{n}`" for n in binfo.names()))
        await binfo.send_info(ctx, prof, as_copy=binfo.wants_copy(name))

    @commands.command(name="copies", aliases=["bosscopies", "mycopies"])
    async def copies(self, ctx: commands.Context):
        """🧬 The boss copies you've collected."""
        rows = bcopy.all_copies(ctx.author.id)
        if not rows:
            return await ctx.send(
                "🧬 No boss copies yet. Beat a boss with `;boss` — every clear "
                "drops a copy, and every copy rolls its own kit.")

        # all_copies() already returns best-first, and find_copy() indexes the
        # same order — re-sorting here is what broke ";copy <number>".
        def render(c, idx):
            prof = binfo.REGISTRY.get(c["source"])
            d = bcopy.describe(c, prof) if prof else {"emoji": "🧬", "pct": 0}
            awk = " ✨" if c["awakening"] else ""
            return (f"{d['emoji']} **{idx + 1}. {c['name']}**{awk}\n"
                    f"　{c['grade']} · {d.get('pct', 0):.0f}% power · "
                    f"`#{c['id']}`")

        def option_of(c):
            return (f"{c['grade']} — {c['name']}"[:96],
                    f"#{c['id']}" + (" · awakening" if c["awakening"] else ""),
                    bcopy.grade_meta(c.get("grade"))[0])

        async def detail(interaction, c):
            prof = binfo.REGISTRY.get(c["source"])
            if prof is None:
                return await interaction.response.defer()
            await interaction.response.send_message(
                embed=bcopy_embed(c, prof), ephemeral=True)

        view = MobileListView(
            owner=ctx.author, title="🧬  Your Boss Copies", items=rows,
            render=render, option_of=option_of, detail=detail,
            detail_placeholder="🔍 Open a copy…", colour=0x9b59b6,
            footer="best grade first  •  ;copy <number|id> for the card",
        )
        view.message = await ctx.send(embed=view.embed(), view=view)

    @commands.command(name="equipcopy", aliases=["ecopy", "copyequip"])
    async def equipcopy_cmd(self, ctx: commands.Context, ref: str = None):
        """🧬 Equip one of your boss copies. Usage: ;equipcopy <number|id>"""
        if not ref:
            return await ctx.send(
                "🧬 Usage: `;equipcopy <number|id>` — `;copies` to list them.")
        c = bcopy.find_copy(ctx.author.id, ref)
        if c is None:
            return await ctx.send(f"❌ No copy matching **{ref}**. Try `;copies`.")
        bcopy.equip(ctx.author.id, c["id"])
        src = binfo.REGISTRY.get(c["source"])
        d   = bcopy.describe(c, src) if src else {"emoji": "🧬", "pct": 0}
        await ctx.send(
            f"{d['emoji']} Equipped **{c['name']}** — {c['grade']} copy "
            f"`#{c['id']}` ({d.get('pct', 0):.0f}% power).\n"
            f"Use `;equip <blade>` to switch back to a normal Beyblade.")

    @commands.command(name="sellcopy", aliases=["scopy", "copysell"])
    async def sellcopy_cmd(self, ctx: commands.Context, ref: str = None):
        """💰 Sell a boss copy for coins. Usage: ;sellcopy <number|id>"""
        if not ref:
            return await ctx.send(
                "💰 Usage: `;sellcopy <number|id>` — `;copies` to list them.")
        c = bcopy.find_copy(ctx.author.id, ref)
        if c is None:
            return await ctx.send(f"❌ No copy matching **{ref}**. Try `;copies`.")
        src   = binfo.REGISTRY.get(c["source"])
        value = bcopy.sell_value(c, src)

        view = _SellCopyConfirm(ctx.author, c, value)
        view.message = await ctx.send(
            f"🧬 Sell **{c['name']}** ({c['grade']} · `#{c['id']}`) "
            f"for 🪙 **{value:,}**?\nThis cannot be undone.", view=view)

    @commands.command(name="copy", aliases=["copybey", "bosscopy"])
    async def copy_cmd(self, ctx: commands.Context, ref: str = None):
        """🧬 Card for one of your copies. Usage: ;copy <number|id>"""
        if not ref:
            return await ctx.send("🧬 Usage: `;copy <number|id>` — `;copies` to list them.")
        c = bcopy.find_copy(ctx.author.id, ref)
        if c is None:
            return await ctx.send(f"❌ No copy matching **{ref}**. Try `;copies`.")
        prof = binfo.REGISTRY.get(c["source"])
        if prof is None:
            return await ctx.send("❌ That copy's source boss no longer exists.")

        async with ctx.typing():
            buf, ext = None, "png"
            try:
                from utils import info_card
                b = await info_card.render_info_card(bcopy.to_blade(c, prof))
                if b is not None:
                    import io as _io
                    data, ext = info_card.compress(b.getvalue())
                    buf = _io.BytesIO(data)
            except Exception:
                buf = None
        if buf is not None:
            return await ctx.send(
                file=discord.File(buf, filename=f"copy_{c['id']}.{ext}"))
        await ctx.send(embed=bcopy_embed(c, prof))

    @commands.command(name="bosses", aliases=["bosslist"])
    async def bosses(self, ctx: commands.Context):
        """👹 The boss roster and your clears."""
        profile = get_user(ctx.author.id)
        cleared = set(profile.get("bosses_cleared") or [])
        e = discord.Embed(
            title="👹  Boss Roster",
            description=f"Cleared **{len(cleared)}/{len(BOSSES)}**",
            color=0x9b59b6,
        )
        for key, cfg in BOSSES.items():
            e.add_field(
                name=f"{cfg['emoji']} {cfg['name']}{' ✅' if key in cleared else ''}",
                value=(f"*{cfg['blurb']}*\n"
                       f"{cfg['difficulty']} · {cfg['hp']:,} HP · "
                       f"🪙 {cfg['reward']['coins']:,} + 🎰 {cfg['reward']['casino']:,}"),
                inline=False,
            )
        e.set_footer(text="`;boss <name>` to fight  •  first clear pays double")
        await ctx.send(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(BossCog(bot))
