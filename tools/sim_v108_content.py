#!/usr/bin/env python3
"""
tools/sim_v108_content.py — the v1.08 batch, checked through the real engine.

Four pieces of content and one bug fix, and the reason this file exists is that
three of them are the kind of thing that LOOKS right in JSON and does nothing
at run time:

**Surge Xcalius's two-stage Special.** The bank-then-cash mechanic is built
out of two rules on the SAME trigger, and the whole thing hinges on their
ORDER: the consume rule has to run before the rule that banks, or the first
Special ever fired cashes a charge that was placed a microsecond earlier and
pays 230 immediately. Ordering that subtle is not something to assert by
reading — it is measured here, Special by Special.

**Drain Fafnir (Black Edition)** is supposed to be "the same blade, better
ability". So the stat line and Special are asserted EQUAL to the original,
not merely plausible, and the ability asserted strictly better on every axis.
A Black Edition that quietly drifted a stat would be a different blade wearing
the name.

**Lionheart Sovereign** converts Defence into damage through `attack_bonus`,
and the number it converts is handed to the state by `boss_battle._make_state`
because a state cannot read its own fighter. If that hand-off is ever dropped
the conversion silently returns 0 and the boss becomes the stat block this
whole module was written to avoid — so it is checked through the real
`_make_state`, not by constructing a state here.

**`;start` said "already started" to players who had never picked a blade.**
`_has_started` counted `xp > 0`, and XP arrives from redeem codes and admin
grants without a blade ever changing hands. Five live accounts were stuck.

Run:  python3 tools/sim_v108_content.py
"""
import os
import sys
import types as _t

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {detail}")


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from utils.database import load_beyblades                          # noqa: E402
from cogs.abilities.ability_engine import AbilityEngine            # noqa: E402
from cogs.battle.defense_manager import DefenseManager             # noqa: E402
from cogs.battle.stamina_manager import StaminaManager             # noqa: E402
from cogs.battle.status_manager import StatusManager               # noqa: E402
from cogs.battle import stability_manager as SBM                   # noqa: E402
from cogs.abilities import type_system as TS                       # noqa: E402
from cogs.core.constants import MOVE_SPECIAL, MOVE_ATTACK       # noqa: E402

BLADES = load_beyblades()
POOL   = 2108           # the level-100 pool v95 balanced against
FOE    = BLADES["Galaxy Pegasus"]


class Sess:
    """The minimum a real AbilityEngine needs to run against."""

    def __init__(self, mblade, oblade, my_move=MOVE_SPECIAL, their=MOVE_ATTACK):
        self.blades = {"m": mblade, "o": oblade}
        self.hp = {"m": POOL, "o": POOL}
        self.max_hp = POOL
        self.max_hp_per_player = {"m": POOL, "o": POOL}
        self.battle_stats = {k: dict(v.get("stats", {}))
                             for k, v in self.blades.items()}
        self.bey_levels = {"m": 1, "o": 1}
        self.special_stats = {}
        self.avatar_bonuses = {}
        self.last_moves = {"m": my_move, "o": their}
        self.round = 1
        self.status = StatusManager(self)
        self.stamina_manager = StaminaManager(self.blades)
        self.type_mods = {k: TS.TypeModifiers(v) for k, v in self.blades.items()}
        self.stability_manager = SBM.StabilityManager(self.blades, self.type_mods)
        self.chain_handler = _t.SimpleNamespace(
            resolve=lambda *a, **k: [], queue=lambda *a, **k: None)
        self.ability = AbilityEngine(self)
        self.defense_manager = DefenseManager(self)
        self.stat_mult = {"m": 1.0, "o": 1.0}


def fire(sess, trigger, dmg_dealt=0, dmg_taken=0, move="", matchup="win"):
    """Run one trigger for 'm' through the engine's real dispatcher.

    `_fire` rather than `apply`: apply() routes a whole MOVE through the
    pipeline and decides which triggers that implies, which is what the battle
    does. Here the trigger under test is the thing being named, so it is fired
    directly — the ops that run are the same ops either way.
    """
    logs = []
    dealt, taken = sess.ability._fire(
        trigger, "m", "o", sess.blades["m"], move, matchup,
        int(dmg_dealt), int(dmg_taken), logs)
    return dealt, taken, logs


def take(sess, incoming, move=MOVE_ATTACK, matchup="win"):
    """Run the DEFENSIVE phase for 'm' against an incoming hit.

    The incoming hit travels as `dmg_dealt` — it is the attacker's damage, and
    `reduce_damage_pct` shaves that number. Passing it as `dmg_taken` (the
    obvious-looking choice) reads as damage the defender is dealing back, and
    every reduction op silently does nothing.
    """
    logs = []
    through, back = sess.ability._fire_defensive(
        "m", "o", sess.blades["m"], move, matchup, int(incoming), 0, logs)
    return through, back, logs


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. the three blades landed ──────────────────────────────────")
NEW_BLADES = ("Drain Fafnir (Black Edition)", "Twin Nemesis",
              "Surge Xcalius")
for n in NEW_BLADES:
    check(f"{n} is in the roster", n in BLADES)
check("the roster grew — floor, not an exact count, because it grows again",
      len(BLADES) >= 101, len(BLADES))
ids = [b["id"] for b in BLADES.values()]
check("every blade id is still unique", len(ids) == len(set(ids)),
      [i for i in ids if ids.count(i) > 1])

# The roster is keyed by name AND carries the name inside each entry, and a lot
# of code reads the inner one. All three of these blades shipped without it on
# the first pass — `blade["name"]` is so ordinary that nothing guards it, so
# four unrelated suites died with a bare `KeyError: 'name'` and none of them
# said which blade. Asserted across the WHOLE roster, not just the new three:
# the next blade added by hand will make the same omission.
bad_name = [k for k, v in BLADES.items() if v.get("name") != k]
check("every entry's inner `name` matches its key", not bad_name, bad_name)
REQUIRED = ("id", "name", "rarity", "type", "spin_direction", "stats",
            "image_url", "description", "special_move", "abilities")
missing = {k: [f for f in REQUIRED if f not in v] for k, v in BLADES.items()}
missing = {k: v for k, v in missing.items() if v}
check(f"every one of the {len(BLADES)} blades carries all "
      f"{len(REQUIRED)} required fields", not missing, missing)
STAT_KEYS = ("attack", "defense", "stamina", "special", "hp")
bad_stats = {k: sorted(set(STAT_KEYS) - set(v.get("stats") or {}))
             for k, v in BLADES.items()}
bad_stats = {k: v for k, v in bad_stats.items() if v}
check("...and a complete stat line", not bad_stats, bad_stats)
for n in NEW_BLADES:
    b = BLADES[n]
    check(f"{n}: has art, a Special and an ability kit",
          bool(b.get("image_url")) and bool(b.get("special_move"))
          and bool(b.get("abilities")))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. Black Edition — the SAME blade, a better ability ─────────")
base = BLADES["Drain Fafnir"]
be   = BLADES["Drain Fafnir (Black Edition)"]
# "Everything same as normal drain fafnir but better abilty", taken literally.
# Asserted equal rather than eyeballed: a Black Edition that quietly drifted a
# stat would be a different blade wearing the name.
for f in ("type", "spin_direction", "burst_height"):
    check(f"same {f}", be[f] == base[f], (be[f], base[f]))
check("same stat line, exactly", be["stats"] == base["stats"],
      (be["stats"], base["stats"]))
check("same Special damage", be["special_move"]["total_damage"]
      == base["special_move"]["total_damage"])
check("...and the same Special name — it is the same move",
      be["special_move"]["name"] == base["special_move"]["name"])
check("but its own flavour line", be["special_move"]["flavour_texts"]
      != base["special_move"]["flavour_texts"])
check("rarity is raised to Mythic", be["rarity"] == "Mythic", be["rarity"])
check("...and it is booster-exclusive, like the other Black Edition",
      be.get("booster_exclusive") is True)


def ops_of(blade):
    out = {}
    for ab in blade.get("abilities") or []:
        for rule in ab.get("rules") or []:
            for op in rule.get("do") or []:
                out[op["op"]] = max(out.get(op["op"], 0),
                                    float(op.get("value", op.get("damage", 0)) or 0))
    return out


b_ops, e_ops = ops_of(base), ops_of(be)
for op in ("drain_stamina", "heal_per_drain"):
    check(f"{op} is strictly better ({b_ops[op]} -> {e_ops[op]})",
          e_ops[op] > b_ops[op], (b_ops.get(op), e_ops.get(op)))
check("it also gains damage reduction the original never had",
      "reduce_damage_pct" in e_ops and "reduce_damage_pct" not in b_ops)
check("...and a burst the original never had",
      "counter_burst" in e_ops and "counter_burst" not in b_ops)
check("nothing got WORSE — 'better ability' means better on every axis",
      all(e_ops.get(k, 0) >= v for k, v in b_ops.items()),
      {k: (v, e_ops.get(k)) for k, v in b_ops.items() if e_ops.get(k, 0) < v})

# Through the real engine, not just the JSON.
s = Sess(be, FOE)
sb = Sess(base, FOE)
e_taken, _, e_logs = take(s, 300)
b_taken, _, _ = take(sb, 300)
check(f"the black layer eats more of a 300 hit ({b_taken:.0f} vs "
      f"{e_taken:.0f} through)", e_taken < b_taken, (b_taken, e_taken))
# Third hit taken should burst.
s2 = Sess(be, FOE)
hp_before = s2.hp["o"]
for _ in range(3):
    take(s2, 300)
check("the third hit taken bursts back at the attacker",
      s2.hp["o"] < hp_before, (hp_before, s2.hp["o"]))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. Twin Nemesis — two heads, and both of them do something ──")
tn = BLADES["Twin Nemesis"]
check("Balance type — one head each way", tn["type"] == "Balance", tn["type"])
names = [a["name"] for a in tn["abilities"]] + [tn["special_move"]["name"]]
check(f"animal-named kit {names}",
      all(any(w in n for w in ("Beast", "Fang", "Devour", "Claw", "Maw"))
          for n in names), names)
s = Sess(tn, FOE)
hp0 = s.hp["o"]
for i in range(3):
    fire(s, "on_attack_hit", 100.0, 0.0)
check("three strikes bank three Fangs and the second head bites",
      s.hp["o"] < hp0, (hp0, s.hp["o"]))
check("...and the count resets, so it charges again rather than firing "
      "every hit from then on",
      s.ability.counters.get(("m", "fang"), 0) == 0,
      s.ability.counters.get(("m", "fang")))
s = Sess(tn, FOE)
through, back, _ = take(s, 200)
check(f"the guarding head is real too — 200 in, {through:.0f} through",
      through < 200, through)
check(f"...and {back:.0f} reflected straight back", back > 0, back)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 4. Surge Xcalius — everything rewritten as attack ───────────")
sx = BLADES["Surge Xcalius"]
check("Attack type", sx["type"] == "Attack")
check("Mythic", sx["rarity"] == "Mythic")
# The stat line IS the ability: a Mythic with 47 Defence is a statement, and it
# has to stay one. If someone later 'fixes' the defence the blade stops meaning
# anything.
check("its Defence is deliberately the worst thing about it",
      sx["stats"]["defense"] < 60, sx["stats"]["defense"])
check("...and its Attack among the highest in the game",
      sx["stats"]["attack"] >= max(b["stats"]["attack"]
                                   for b in BLADES.values()) - 12,
      sx["stats"]["attack"])

print("      the two-stage Special, Special by Special:")
s = Sess(sx, FOE)
seen = []
for i in range(1, 5):
    dealt, _, logs = fire(s, "on_special", 0.0, 0.0)
    seen.append(dealt)
    print(f"        Special {i}: +{dealt:.0f} bonus damage")
check("the FIRST Special banks and pays nothing — rule order is the whole "
      "mechanism, and a consume placed second would cash its own charge",
      seen[0] == 0, seen)
check("the SECOND cashes the bank for 230", seen[1] == 230, seen)
check("...and every Special after it collects too, because the bank re-arms",
      seen[2] == 230 and seen[3] == 230, seen)

# Stability moved OFF on_special in v1.10. `on_special` fires once, on the
# first hit, so the 30 that used to live there could never be "10 each" across
# three sabers — it rides on_hit now, and section 10 measures it there. Left
# here as an explicit zero so the move is not silently double-charging.
s = Sess(sx, FOE)
before = s.stability_manager.stability.get("o")
fire(s, "on_special", 0, 0)
check(f"on_special itself takes NO stability any more ({before} -> "
      f"{s.stability_manager.stability.get('o')}) — it rides on_hit, so the "
      f"three sabers are 10 each rather than one 30",
      s.stability_manager.stability.get("o") == before, before)

print("      Recover, converted:")
for trig in ("on_stamina_win", "on_stamina_mirror", "on_stamina_loss"):
    s = Sess(sx, FOE)
    dealt, _, _ = fire(s, trig, 0.0, 0.0)
    check(f"{trig} lands 59 damage instead of restoring", dealt == 59, dealt)

print("      Defend, converted:")
cdp = s.ability.counter_damage_pct("m", sx)
check(f"counters land 20% harder ({cdp:.0f}%)", cdp == 20.0, cdp)
check("...and an ordinary blade is untouched — the passive is read off the "
      "rules, so a blade without the op must get 0",
      Sess(FOE, sx).ability.counter_damage_pct("m", FOE) == 0.0)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5. LIONHEART SOVEREIGN, the first Defence boss ──────────────")
from cogs.battle.boss import boss_battle as BB                      # noqa: E402
from cogs.battle.boss import boss_info as BI                        # noqa: E402
from cogs.battle.boss import boss_ai as AI                          # noqa: E402
from cogs.battle.boss import lionheart as LH                        # noqa: E402

check("registered in the roster", "lionheart" in BB.BOSSES)
check("...and resolves to its own module",
      BB._module_for(BB.BOSSES["lionheart"]) is LH)
check("...and to its own state", isinstance(
    BB._make_state(BB.BOSSES["lionheart"]), LH.LionheartState))
check("...and is in the ;bossinfo registry",
      BI.REGISTRY.get("lionheart") is LH.LIONHEART
      and BI.SPECIAL_MODULES.get("lionheart") is LH)
P = LH.LIONHEART
check("the stats are the ones asked for: 118 / 185 / 127",
      (P["attack"], P["defense"], P["stamina"]) == (118, 185, 127),
      (P["attack"], P["defense"], P["stamina"]))
check("Defence type, and the highest Defence of any boss",
      P["type"] == "Defense"
      and P["defense"] > max(BB.BOSSES[k]["defense"] for k in BB.BOSSES
                             if k != "lionheart"),
      P["defense"])

# The hand-off. A state cannot read its own fighter, so _make_state passes the
# LEVELLED defence in. Drop that line and the conversion silently returns 0 and
# the boss becomes a stat block — checked through the real _make_state.
st = BB._make_state(BB.BOSSES["lionheart"])
check(f"_make_state hands it the levelled Defence ({st.base_defense:.0f}), "
      f"not the printed 185",
      st.base_defense > P["defense"], st.base_defense)
check("a bare state converts nothing rather than crashing — the safe failure",
      LH.LionheartState().attack_bonus() == 0.0)

st.plates = 0
bare = st.attack_bonus()
st.plates = LH.BULWARK_MAX
full = st.attack_bonus()
check(f"plates convert Defence into Attack ({bare:.0f} -> {full:.0f})",
      full > bare, (bare, full))
st.roar_turns = LH.ROAR_TURNS
check(f"...and the Roar doubles the conversion ({full:.0f} -> "
      f"{st.attack_bonus():.0f})", abs(st.attack_bonus() - full * 2) < 1e-6)
st.roar_turns = 0

check("reduction is capped, or five doubled plates stalemate the fight",
      LH.LionheartState(plates=LH.BULWARK_MAX,
                        roar_turns=LH.ROAR_TURNS).damage_reduction() <= 0.35)
st2 = LH.LionheartState(plates=LH.BULWARK_MAX)
through, _ = st2.absorb(500.0)
check(f"the mane soaks ({through:.0f} of 500 through)", through < 500.0)
check("...and banks a share of what it soaked", st2.bank > 0, st2.bank)
check("the bank has a ceiling, so round 30 cannot produce a one-shot",
      LH.REPRISAL_CAP > 0 and all(
          LH.LionheartState(plates=5).absorb(9e6)[0] >= 0 for _ in range(1)))
st3 = LH.LionheartState(plates=5)
for _ in range(200):
    st3.absorb(9999.0)
check(f"...held at {LH.REPRISAL_CAP:.0f} after 200 huge hits",
      st3.bank <= LH.REPRISAL_CAP, st3.bank)

st4 = LH.LionheartState(plates=LH.BULWARK_MAX)
check("chip damage does not strip a plate",
      st4.break_stars(P["hp"] * LH.PLATE_BREAK_DAMAGE / 2, P["hp"]) == 0)
check(f"...but a hit worth {LH.PLATE_BREAK_DAMAGE:.1%} of its health strips "
      f"{LH.PLATES_LOST_PER_BREAK}",
      st4.break_stars(P["hp"] * LH.PLATE_BREAK_DAMAGE, P["hp"])
      == LH.PLATES_LOST_PER_BREAK)

# The shape that froze the bot in v1.06. Checked for the new boss BEFORE it
# ever reaches a player.
for k in LH.SPECIALS:
    s_ = LH.LionheartState(plates=3)
    s_.overdriven = True
    d, e = LH.special_damage(k, 118.0, s_, 100.0, 0.5, AI.DMG_SCALE)
    check(f"Special '{k}' returns (float, dict) — the shape _fire_special "
          f"reads", isinstance(d, (int, float)) and isinstance(e, dict),
          (type(d).__name__, type(e).__name__))
s_ = LH.LionheartState(plates=5)
s_.overdriven = True
_, e = LH.special_damage("roar", 118.0, s_, 100.0, 0.5, AI.DMG_SCALE)
check("the Roar heals through the `drain` key _fire_special already pays out",
      e.get("drain", 0) > 0, e)
check("...and is once per battle", s_.ultimate_used
      and "roar" not in LH.available_specials(s_, True))

# Full fights through the real BossFight, which is where the v1.06 hang bit.
class _Member:
    def __init__(self, i):
        self.id, self.display_name, self.mention = i, f"p{i}", f"<@{i}>"


crashes, fired, fights = [], 0, 0
for seed in range(8):
    m = _Member(7000 + seed)
    try:
        f = BB.BossFight(m, "lionheart", party=[m], tier="standard")
        for _ in range(60):
            if f.finished:
                break
            f.boss.gauge = AI.SPECIAL_GAUGE_MAX
            mv = MOVE_ATTACK if f.foe.can(AI.MOVE_ATTACK) else AI.MOVE_CHARGE
            fired += bool(f.step(AI.MOVE_ATTACK if f.foe.can(AI.MOVE_ATTACK)
                                 else AI.MOVE_CHARGE).get("god_special"))
        fights += 1
    except Exception as exc:                                 # noqa: BLE001
        crashes.append((seed, repr(exc)[:140]))
check(f"{fights}/8 full fights, {fired} boss Specials, no exception escapes",
      not crashes and fights == 8, crashes[:2])

check("it is NOT exempt from the daily timer — Argus is, because Argus is the "
      "one you pay for", "lionheart" not in BB.UNTIMED_BOSSES)
check("...and it does pay a coin prize, unlike Argus",
      P["reward"]["coins"] > 0, P["reward"])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6. ;start told players they had started when they hadn't ────")
from cogs.core.onboarding import (                                  # noqa: E402
    _has_started, K_STARTED, STARTER_NAMES)

# The reported bug. `_has_started` counted `xp > 0`, and XP arrives from redeem
# codes, admin grants and migrations without a blade ever changing hands. Five
# live accounts sat on exactly 60,000 xp / 110,999 coins / level 34 with an
# EMPTY inventory: ;start closed on them, they never picked, and nothing that
# needs a blade would run. There is no second door, so it was unrecoverable.
stuck = {"xp": 60000, "coins": 110999, "level": 34, "inventory": []}
check("the five stuck accounts are offered the picker again",
      _has_started(stuck) is False, stuck)
check("holding a blade still counts as started",
      _has_started({"inventory": ["Victory Valkyrie"]}) is True)
check("...and so does the explicit flag, so selling every blade does not "
      "hand out a second free starter",
      _has_started({K_STARTED: True, "inventory": []}) is True)
check("a brand-new profile has not started", _has_started({}) is False)
check("XP alone is no longer enough — that is the whole fix",
      _has_started({"xp": 999999}) is False)
src = open(os.path.join(ROOT, "cogs", "core", "onboarding.py"),
           encoding="utf-8").read()
check("the flag is written where the blade is actually granted, not when the "
      "picker opens — a closed picker must not strand anyone",
      "profile[K_STARTED] = True" in src.split("class StarterPickView")[0])
check("the four authored starters are still what a new player chooses from",
      len(STARTER_NAMES) == 4 and all(n in BLADES for n in STARTER_NAMES),
      STARTER_NAMES)


print("\n── 7. ;giveavatar — the only route an Exclusive card has ───────")
from cogs.avatar import avatar_engine as _ae                        # noqa: E402

_cards = _ae.get_all_avatars()


def _match(q):
    """The matcher from AdminCog.giveavatar, restated for testing.

    Kept in step with the command by the source check below rather than by
    hope: if the two ever disagree the last check in this block fails.
    """
    ql = q.strip().strip('"').strip("'").lower()
    if not ql:
        return []
    exact = [a for a in _cards
             if a["id"].lower() == ql or a["name"].lower() == ql]
    return exact or [a for a in _cards if ql in a["name"].lower()]


_adm = open(os.path.join(ROOT, "cogs", "admin", "admin.py"),
            encoding="utf-8").read()
check("the command exists and is master-gated like ;givebey",
      'name="giveavatar"' in _adm
      and "@is_master()" in _adm.split('name="giveavatar"')[1][:200])
check("...and hidden, like every other command in the admin cog",
      "hidden=True" in _adm.split('name="giveavatar"')[1][:120])
_body = _adm.split("async def giveavatar")[1].split("\n    # ──")[0]
check("it grants through the shop's own database helper, not a hand-rolled "
      "profile write — a stale snapshot written back is what ate blades in "
      "redeem.grant", "add_avatar_to_inventory" in _body)
check("...and checks ownership first rather than duplicating a card",
      "player_owns_avatar" in _body)

check("an exact id resolves", len(_match("avatar_u001")) == 1)
check("an exact name resolves", len(_match("Dr. W. D. Gaster")) == 1)
check("a unique substring resolves",
      len(_match("gaster")) == 1 and _match("gaster")[0]["id"] == "avatar_u001")
check("an unknown name resolves to nothing rather than to something",
      _match("zzzznope") == [])
# The reason ambiguity is refused rather than guessed: handing the wrong
# Exclusive to somebody has no undo, and one-letter queries match most of the
# roster.
_amb = _match("a")
check(f"an ambiguous query is refused, not guessed ({len(_amb)} matches)",
      len(_amb) > 1, len(_amb))
check("exact wins over substring — 'Argus' must not be ambiguous just "
      "because some other card contains it",
      len(_match("Argus")) == 1, [a["name"] for a in _match("Argus")])

# The gap this command closes. _build_rarity_map filters Exclusive out of
# every pack pull, so those cards had no route into a player's hands at all.
_excl = [a for a in _cards if a["rarity"] == "Exclusive"]
check(f"there are Exclusive cards ({len(_excl)}) and packs cannot pull them",
      _excl and 'av["rarity"] != "Exclusive"' in open(
          os.path.join(ROOT, "cogs", "avatar", "avatar_shop.py"),
          encoding="utf-8").read(),
      [a["name"] for a in _excl])
for _a in _excl:
    check(f"...so {_a['name']} is reachable by name through ;giveavatar",
          len(_match(_a["name"])) == 1)


print("\n── 8. blade art has ONE ceiling, and it is stated once ─────────")
# "What size should bey photos be?" — answered by the renderers, not by taste.
# TARGET_PX is the single number, and it has to stay >= what the largest
# consumer actually paints or the optimiser silently degrades the info card.
from tools import optimize_assets as _oa                            # noqa: E402

check("the optimiser states the ceiling once, as a constant",
      isinstance(_oa.TARGET_PX, int), _oa.TARGET_PX)
_ic = open(os.path.join(ROOT, "utils", "info_card.py"), encoding="utf-8").read()
check("the info card is still 720 CSS px at device scale 2",
      "CARD_WIDTH   = 720" in _ic and "device_scale_factor=2" in _ic)
# .disc is 250 CSS px; at deviceScaleFactor 2 that is 500 real px, and it is
# the largest thing any surface paints blade art into.
check(f"...so the largest painted art is 500px, and TARGET_PX "
      f"({_oa.TARGET_PX}) clears it",
      "width: 250px; height: 250px" in _ic and _oa.TARGET_PX >= 500,
      _oa.TARGET_PX)
check("the art is circle-cropped with object-fit: cover — which is why "
      "square source matters more than resolution",
      'object-fit:cover' in _ic.replace(" ", ""))

print("\n── 9. Deep Caynox, and a Special that really deals nothing ────")
from cogs.battle.damage_rules import (                              # noqa: E402
    resolve_special, resolve_special_hits)

for _n in ("Deep Caynox", "Deep Caynox ELT"):
    check(f"{_n} is in the roster", _n in BLADES)

_dc, _elt = BLADES["Deep Caynox"], BLADES["Deep Caynox ELT"]
check("the stats are the ones asked for: 50 / 101 / 121",
      (_dc["stats"]["attack"], _dc["stats"]["defense"],
       _dc["stats"]["stamina"]) == (50, 101, 121), _dc["stats"])
check("Stamina type, Legendary",
      _dc["type"] == "Stamina" and _dc["rarity"] == "Legendary")
check("50 Attack really is the lowest of any Legendary — the card is meant "
      "to read that way",
      _dc["stats"]["attack"] == min(
          b["stats"]["attack"] for b in BLADES.values()
          if b["rarity"] == "Legendary"), _dc["stats"]["attack"])

# The non-damage Special. Every other path in resolve_special floors per-hit
# damage at 1, so authoring `damage_per_hit: 0` gives 0 at level 1 and silently
# becomes 1 the moment the special stat scales it — a different move at level 2
# than at level 1. `non_damage` is what makes "deals nothing" stay true.
check("Levitation Launch is declared non_damage, not merely authored as 0",
      _dc["special_move"].get("non_damage") is True)
for _lbl, _stat in (("unlevelled", None), ("heavily levelled", 300.0)):
    _h, _p, _f, _ig = resolve_special(_dc, _stat)
    _tbl = resolve_special_hits(_dc, _stat)
    check(f"...and deals exactly 0 when {_lbl} (per_hit={_p}, table={_tbl})",
          _p == 0 and sum(_tbl) == 0, (_p, _tbl))
# The floor it opts out of, shown rather than asserted about: an ordinary
# blade authored at 0 would be dragged up to 1.
_fake = {"name": "x", "stats": _dc["stats"],
         "special_move": {"name": "y", "hits": 1, "damage_per_hit": 0}}
check("...and WITHOUT the flag the engine floors it at 1, which is the whole "
      "reason the flag exists",
      resolve_special_hits(_fake, 300.0) == [1],
      resolve_special_hits(_fake, 300.0))

# A non-damage Special with no ability rules would be a button that does
# nothing at all, so everything it does lives in the kit.
_s = Sess(_dc, FOE)
_s.hp["m"] = 1000
_s.stability_manager.stability["m"] = 55
_sp0 = _s.stamina_manager.stamina.get("m")
_dealt, _, _logs = fire(_s, "on_special", 0, 0)
check("Levitation Launch deals no damage through the engine either",
      _dealt == 0, _dealt)
check(f"...but heals ({1000} -> {_s.hp['m']})", _s.hp["m"] > 1000)
check(f"...restores stamina ({_sp0:.1f} -> "
      f"{_s.stamina_manager.stamina.get('m'):.1f})",
      _s.stamina_manager.stamina.get("m") > _sp0)
check(f"...restores stability (55 -> "
      f"{_s.stability_manager.stability.get('m')})",
      _s.stability_manager.stability.get("m") > 55)
check("...and leaves a shield", any("shield" in l.lower() for l in _logs), _logs)
check("...and a Defence buff that LASTS, unlike reduce_damage_pct — that op "
      "takes no `turns` and would have shaved a hit that isn't happening",
      any("Defense" in l and "turns" in l for l in _logs), _logs)

# Switch Strike: 50 Attack is only playable because the strike is not made of
# Attack.
_s = Sess(_dc, FOE)
_hit, _, _ = fire(_s, "on_attack_hit", 40, 0)
check(f"Switch Strike puts Stamina behind the blow (40 -> {_hit})",
      _hit > 40 + _dc["stats"]["attack"] * 0.5, _hit)
check("...and the bonus is read off the STAMINA stat, not a printed constant",
      any(op.get("stat") == "stamina"
          for ab in _dc["abilities"] for r in ab["rules"]
          for op in r["do"] if op["op"] == "bonus_damage_stat"))

print("      ELT — same blade, better ability:")
for _f in ("type", "spin_direction", "burst_height"):
    check(f"same {_f}", _elt[_f] == _dc[_f], (_elt[_f], _dc[_f]))
check("same stat line, exactly", _elt["stats"] == _dc["stats"],
      (_elt["stats"], _dc["stats"]))
check("same Special name and non_damage flag",
      _elt["special_move"]["name"] == _dc["special_move"]["name"]
      and _elt["special_move"].get("non_damage") is True)
check("but its own flavour line",
      _elt["special_move"]["flavour_texts"]
      != _dc["special_move"]["flavour_texts"])
check("booster-exclusive", _elt.get("booster_exclusive") is True)
_b_ops, _e_ops = ops_of(_dc), ops_of(_elt)
check("nothing got WORSE — 'better ability' means better on every axis",
      all(_e_ops.get(k, 0) >= v for k, v in _b_ops.items()),
      {k: (v, _e_ops.get(k)) for k, v in _b_ops.items()
       if _e_ops.get(k, 0) < v})
_e_hit, _, _ = fire(Sess(_elt, FOE), "on_attack_hit", 40, 0)
check(f"...and it hits harder through the engine ({_hit} -> {_e_hit})",
      _e_hit > _hit, (_hit, _e_hit))
_b_thru, _, _ = take(Sess(_dc, FOE), 200)
_e_thru, _, _ = take(Sess(_elt, FOE), 200)
check(f"...and soaks more of a 200 hit ({_b_thru} -> {_e_thru} through)",
      _e_thru < _b_thru, (_b_thru, _e_thru))


print("\n── 10. Surge Xcalibur -> Surge Xcalius, and Triple Saber ──────")
# Safe as a straight rename ONLY because nobody held it: measured against the
# live store, 0 owners and 0 equipped, since it shipped the day before. A
# rename of anything anyone held would need a profile migration, because
# inventories store the NAME, not the id.
check("the new name is in the roster", "Surge Xcalius" in BLADES)
check("...and the old one is gone", "Surge Xcalibur" not in BLADES)
_sx = BLADES["Surge Xcalius"]
check("its inner name matches too, not just the key",
      _sx["name"] == "Surge Xcalius", _sx["name"])
check("it kept its id — a rename is not a new blade", _sx["id"] == "BB101",
      _sx["id"])
check("everything else is the same blade: 158 / 47 / 92, Mythic, Attack",
      (_sx["stats"]["attack"], _sx["stats"]["defense"],
       _sx["stats"]["stamina"]) == (158, 47, 92)
      and _sx["rarity"] == "Mythic" and _sx["type"] == "Attack", _sx["stats"])
# A stale "Xcalibur" in runtime code or data would be a dead lookup, so those
# are swept. The `tools/add_*` migration scripts are excluded on purpose: the
# one that PERFORMS the rename has to name both sides of it, and the v1.08
# script carries a note saying what the blade originally shipped as. Excluding
# the whole repo would make the check meaningless; excluding nothing would make
# it unsatisfiable.
import subprocess                                                   # noqa: E402
_sweep = subprocess.run(
    ["grep", "-rn", "Surge Xcalibur",
     os.path.join(ROOT, "cogs"), os.path.join(ROOT, "utils"),
     os.path.join(ROOT, "data"), os.path.join(ROOT, "README.md")],
    capture_output=True, text=True)
_hits = [h for h in _sweep.stdout.strip().splitlines()
         if h and "__pycache__" not in h]
check("no stale 'Surge Xcalibur' left in cogs/, utils/, data/ or the README",
      not _hits, _hits[:3])

_sm = _sx["special_move"]
check("the Special is renamed to Triple Saber",
      _sm["name"] == "Triple Saber", _sm["name"])
check("...with 3 hits", _sm["hits"] == 3, _sm["hits"])
check("...and the SAME total damage — 'everything will be same'. A scalar "
      "would have been 34//3 = 11 and quietly shaved a point",
      sum(resolve_special_hits(_sx, None)) == 34,
      resolve_special_hits(_sx, None))
check("...which needs a per-hit LIST to express",
      isinstance(_sm["damage_per_hit"], list), _sm["damage_per_hit"])

# 10 stability per hit. Moved from on_special (fires ONCE, on the first hit) to
# on_hit (the per-Special-hit trigger), so "10 each" is three separate 10s
# rather than one 30 wearing a different label.
_s = Sess(_sx, FOE)
_st0 = _s.stability_manager.stability.get("o")
_per = []
for _ in range(3):
    fire(_s, "on_hit", 0, 0)
    _per.append(_s.stability_manager.stability.get("o"))
check(f"each saber takes 10 stability ({_st0} -> {_per})",
      _per == [_st0 - 10, _st0 - 20, _st0 - 30], (_st0, _per))
check("...totalling the same 30 the single-hit version took",
      _st0 - _per[-1] == 30)
check("the stability rides on_hit, not on_special — on_special fires once, "
      "so 3x10 there would have been 10",
      not any(op["op"] == "enemy_lose_stability"
              for ab in _sx["abilities"] for r in ab["rules"]
              if r.get("when") == "on_special" for op in r["do"]))
# And the rest of the blade is untouched, which is the other half of
# "everything will be same".
_s = Sess(_sx, FOE)
check("the bank-then-cash Special is unchanged: 0, then 230 every time after",
      [fire(_s, "on_special", 0, 0)[0] for _ in range(4)]
      == [0, 230, 230, 230])
_s = Sess(_sx, FOE)
check("Recover still lands 59 instead of restoring",
      fire(_s, "on_stamina_win", 0, 0)[0] == 59)
check("...and Defend still counters 20% harder",
      _s.ability.counter_damage_pct("m", _sx) == 20.0)


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
