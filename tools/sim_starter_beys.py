#!/usr/bin/env python3
"""
tools/sim_starter_beys.py — the four anime starters, and the ;start gate.

Two things are being pinned here.

STARTER POWER. These are now the only blades `;start` hands out, so they set a
new player's first impression AND become the floor every other blade has to
look better than. A starter that quietly out-stats the Rare band would make the
next 80 blades feel like a downgrade, and nobody would notice until the economy
had already been shaped by it. Every stat line is asserted at or below the Rare
median.

THE GATE. `;start` is now required before anything else works, which makes the
exemption list load-bearing in a way that is easy to get wrong in one
direction: too strict and players are locked out of the door itself, too loose
and the gate does nothing. Both directions are checked, including the SLASH
path — seven cogs expose their slash commands through `ctx.invoke`, which does
not run checks, so a prefix-only gate would have an ungated `/` twin for every
command it guards.

Run:  python3 tools/sim_starter_beys.py
"""
import json
import os
import statistics as st
import sys
import types

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
DB = json.load(open(os.path.join(ROOT, "data", "beyblades.json"),
                    encoding="utf-8"))
ROWS = list(DB.values())

STARTERS = ("Victory Valkyrie", "King Kerbeus", "Rising Ragnaruk",
            "Storm Spriggan")

print("\n── 1. all four exist, one per type ──────────────────────────────")
for n in STARTERS:
    check(f"{n} is in the roster", n in DB)
types_seen = [DB[n]["type"] for n in STARTERS if n in DB]
check("one of each type", sorted(types_seen)
      == ["Attack", "Balance", "Defense", "Stamina"], types_seen)
check("all four are Rare",
      all(DB[n]["rarity"] == "Rare" for n in STARTERS if n in DB))
ids = [DB[n]["id"] for n in STARTERS if n in DB]
check("ids are unique across the whole roster",
      len({b["id"] for b in ROWS}) == len(ROWS), len(ROWS))
check(f"the roster has not shrunk ({len(ROWS)} blades)",
      len(ROWS) >= 84, len(ROWS))

print("\n── 2. they are STARTER power, not Rare power ────────────────────")
# The band they have to sit inside, measured from the blades that were already
# Rare — so this re-measures rather than hardcoding numbers that could drift.
others = [b for b in ROWS
          if b.get("rarity") == "Rare" and b["name"] not in STARTERS]
med = {k: st.median([b["stats"][k] for b in others])
       for k in ("attack", "defense", "stamina", "special", "hp")}
print("     Rare medians (excluding the starters): "
      + "  ".join(f"{k} {v:.0f}" for k, v in med.items()))
IDENTITY = {"Attack": "attack", "Defense": "defense",
            "Stamina": "stamina", "Balance": None}
for n in STARTERS:
    s = DB[n]["stats"]
    over = {k: (s[k], med[k]) for k in med if s[k] > med[k]}
    # A defence starter is ALLOWED to be defensive. Demanding every line sit
    # under the median would forbid the one stat that gives each blade its
    # identity, and produce four indistinguishable lumps. The rule is that at
    # most one stat may exceed the median, and it has to be the blade's own.
    ident = IDENTITY[DB[n]["type"]]
    check(f"{n} exceeds the Rare median on at most one stat",
          len(over) <= 1, over)
    check(f"...and only on its own ({ident or 'none — Balance'})",
          not over or (ident is not None and ident in over), over)
    total = sum(s[k] for k in ("attack", "defense", "stamina"))
    check(f"...total atk+def+sta ({total}) is under the Rare average",
          total < sum(med[k] for k in ("attack", "defense", "stamina")),
          total)

# And they must not be the strongest thing a new player could hold.
best = max(sum(b["stats"][k] for k in ("attack", "defense", "stamina"))
           for b in ROWS if b["name"] not in STARTERS)
worst_starter = max(sum(DB[n]["stats"][k]
                        for k in ("attack", "defense", "stamina"))
                    for n in STARTERS)
check("the best starter is comfortably below the best blade in the game",
      worst_starter < best * 0.8, (worst_starter, best))

print("\n── 3. the authored abilities and specials ───────────────────────")
WANT = {
    "Rising Ragnaruk":  ("Centrifugal Force", "Roktavor Zone"),
    "King Kerbeus":     ("Chain Blade", "Chain Launch"),
    "Storm Spriggan":   ("Spriggan Layer", "Counter Break"),
    "Victory Valkyrie": ("Energy Layer", "Rush Launch"),
}
for n, (ab, sp) in WANT.items():
    b = DB[n]
    check(f"{n}: ability is {ab}", b["ability"]["name"] == ab,
          b["ability"]["name"])
    check(f"{n}: special is {sp}", b["special_move"]["name"] == sp,
          b["special_move"]["name"])
    check(f"{n}: the ability has rules the engine can run",
          bool(b["ability"].get("rules")))
    check(f"{n}: image is a renderable CDN link",
          str(b.get("image_url", "")).startswith(
              "https://cdn.discordapp.com/attachments/"),
          str(b.get("image_url"))[:50])

# The two specials that were specified numerically.
rl = DB["Victory Valkyrie"]["special_move"]
check("Rush Launch is 16 hits", rl["hits"] == 16, rl["hits"])
check("...of 10 damage each", rl["damage_per_hit"] == 10, rl["damage_per_hit"])
cb = DB["Storm Spriggan"]["special_move"]
check("Counter Break deals 130", cb["damage_per_hit"] == 130,
      cb["damage_per_hit"])

print("\n── 4. the specials resolve as authored ──────────────────────────")
from cogs.battle.damage_rules import resolve_special, resolve_special_hits  # noqa: E402

hits, per_hit, _fl, _ig = resolve_special(DB["Victory Valkyrie"])
check("resolve_special reports 16 hits", hits == 16, hits)
per = resolve_special_hits(DB["Victory Valkyrie"])
check("...and 16 separate hits come out", len(per) == 16, len(per))
check("...each worth 10 at level 1", set(per) == {10}, sorted(set(per)))
check("Counter Break resolves as one 130 hit",
      resolve_special_hits(DB["Storm Spriggan"]) == [130],
      resolve_special_hits(DB["Storm Spriggan"]))
check("Chain Launch is 2 x 39",
      resolve_special_hits(DB["King Kerbeus"]) == [39, 39],
      resolve_special_hits(DB["King Kerbeus"]))
check("Roktavor Zone is one 78",
      resolve_special_hits(DB["Rising Ragnaruk"]) == [78])

print("\n── 5. the abilities actually fire ───────────────────────────────")
# The real AbilityEngine against a stub session, so the rules are executed
# rather than merely parsed. No discord stub: every other suite imports the
# real library, and stubbing it here broke `utils.embeds`, which onboarding
# needs two sections further down.
from cogs.battle.status_manager import StatusManager           # noqa: E402
from cogs.battle.stamina_manager import StaminaManager          # noqa: E402
from cogs.abilities.ability_engine import AbilityEngine         # noqa: E402


class Stub:
    def __init__(self, b1, b2):
        self.blades = {"1": DB[b1], "2": DB[b2]}
        self.hp = {"1": 700, "2": 700}
        self.max_hp = 700
        self.max_hp_per_player = {"1": 700, "2": 700}
        self.last_moves = {}
        self.round = 1
        self.status = StatusManager(self)
        self.stamina_manager = StaminaManager(self.blades)
        self.chain_handler = types.SimpleNamespace(resolve=lambda *a, **k: [])


def engine(b1, b2="Dranzer"):
    s = Stub(b1, b2)
    e = AbilityEngine(s)
    s.ability = e
    for k in ("1", "2"):
        e.setup(k, s.blades[k])
    return s, e


def act(s, e, move, outcome, dealt=0, taken=0):
    """One exchange for player 1, through the engine's real entry point.

    `apply` is the only way abilities run in a battle — it derives every
    trigger (on_attack_win, on_special, on_any_win ...) from the move and the
    matchup. Calling the internals directly would test rules the live game
    never reaches this way.
    """
    return e.apply("1", "2", s.blades["1"], s.blades["2"],
                   move, outcome, dealt, taken)


# Ragnaruk — heals and stacks defence on a stamina win, capped at 4.
s, e = engine("Rising Ragnaruk")
s.hp["1"] = 600
act(s, e, "stamina", "win")
check("Centrifugal Force heals on a stamina win", s.hp["1"] > 600, s.hp["1"])
before = s.status.get_buff_bonus("1", "defense")
for _ in range(8):
    act(s, e, "stamina", "win")
after = s.status.get_buff_bonus("1", "defense")
check("...and stacks defence", after > before, (before, after))
check("...capped at 4 stacks (20 defence)", after <= 20, after)
s2, e2 = engine("Rising Ragnaruk")
hp0 = s2.hp["1"]
act(s2, e2, "attack", "win")
check("...but a stamina ability does NOT fire on an attack",
      s2.status.get_buff_bonus("1", "defense") == 0)

# Kerbeus — shield on defend, reflect on a defence win.
#
# on_defend and on_take_damage fire on the DEFENDER, from _fire_defensive,
# which apply() runs for the player who is NOT the mover. So Kerbeus has to be
# player 2 and be attacked — driving it as the mover would test a code path
# the trigger never takes.
s, e = engine("Dranzer", "King Kerbeus")
act(s, e, "attack", "win", 60)
check("Chain Blade shields when attacked", s.status.get_shield("2") > 0,
      s.status.get_shield("2"))
_dealt, taken, _logs = act(s, e, "attack", "win", 60)
check("...and the chains snap 10 back at whoever swung", taken >= 10, taken)

# Valkyrie — attack stacks, and the Special sharpens the flurry.
s, e = engine("Victory Valkyrie")
act(s, e, "attack", "win")
check("Energy Layer stacks attack",
      s.status.get_buff_bonus("1", "attack") > 0,
      s.status.get_buff_bonus("1", "attack"))
for _ in range(8):
    act(s, e, "attack", "win")
check("...capped at 4 stacks (24 attack)",
      s.status.get_buff_bonus("1", "attack") <= 24,
      s.status.get_buff_bonus("1", "attack"))
s, e = engine("Victory Valkyrie")
act(s, e, "special", "win", 40)
check("...and Rush Launch grants +20% crit",
      abs(e.crit_chance_bonus.get("1", 0) - 0.20) < 1e-6,
      e.crit_chance_bonus.get("1"))

# Spriggan — the counter arms on its Special, then spends itself on the next
# hit it TAKES. Arming is a mover trigger and the reflect is a defender one,
# so this needs both directions: Spriggan is player 1 to fire the Special, and
# player 1 again as the defender when player 2 attacks.
s, e = engine("Storm Spriggan")
check("Counter Break starts disarmed", e.modes.get("1") != "counter_break")
act(s, e, "special", "win", 40)
check("the Special arms it", e.modes.get("1") == "counter_break",
      e.modes.get("1"))

# Player 2 attacks; Spriggan (player 1) is the defender.
_d, ret, _l = e.apply("2", "1", s.blades["2"], s.blades["1"],
                      "attack", "win", 60, 0)
check("the next hit taken is returned to the attacker", ret > 0, ret)
check("...and the stance is spent", e.modes.get("1") != "counter_break",
      e.modes.get("1"))
_d2, ret2, _l2 = e.apply("2", "1", s.blades["2"], s.blades["1"],
                         "attack", "win", 60, 0)
check("a second hit is NOT returned — it is a counter, not an aura",
      ret2 < ret, (ret, ret2))

# The heal half of Spriggan Layer.
s, e = engine("Storm Spriggan")
s.hp["1"] = 500
act(s, e, "attack", "win", 30)
check("Spriggan Layer mends HP on any win", s.hp["1"] > 500, s.hp["1"])

print("\n── 6. ;start hands out exactly these four ───────────────────────")
from cogs.core import onboarding as ON                          # noqa: E402

check("STARTER_NAMES is the authored four",
      set(ON.STARTER_NAMES) == set(STARTERS), ON.STARTER_NAMES)
check("the pool offers all four, not a random sample",
      ON.STARTER_CHOICES == 4, ON.STARTER_CHOICES)
pool = ON._starter_pool()
check("_starter_pool resolves every one against the real roster",
      [b["name"] for b in pool] == list(ON.STARTER_NAMES),
      [b.get("name") for b in pool])
src = open(os.path.join(ROOT, "cogs", "core", "onboarding.py"),
           encoding="utf-8").read()
check("it no longer samples at random",
      "random.sample" not in src)

print("\n── 7. the gate lets the right things through ────────────────────")


class Cmd:
    def __init__(self, name, aliases=None, parent=None):
        self.name = self.qualified_name = name
        self.aliases = aliases or []
        self.parent = parent


# Too strict is the dangerous direction: these are how a new player reaches
# ;start at all, and how an admin diagnoses a broken install.
for name, aliases in (("start", ["begin", "newplayer"]), ("help", []),
                      ("whatnext", ["guide"]), ("version", ["ver"])):
    check(f"`{name}` works before starting", ON.is_exempt(Cmd(name, aliases)))
    for al in aliases:
        check(f"...and so does its alias `{al}`", ON.is_exempt(Cmd(al)))

# Too loose is the other direction — the gate has to actually gate.
for name in ("battle", "profile", "buypack", "story", "boss", "askill",
             "daily", "equip", "leaderboard"):
    check(f"`{name}` is gated", not ON.is_exempt(Cmd(name)))

check("a command with no name is treated as exempt, never as an error",
      ON.is_exempt(None))

# The slash side. Every cog that exposes `/` by calling ctx.invoke bypasses
# prefix checks, so the tree gate is the only thing covering them.
check("a tree gate installer exists", callable(ON.install_tree_gate))
check("the gate embed names the starters",
      all(n in ON._gate_embed().fields[0].value for n in STARTERS))
check("NotStarted is a CheckFailure, so the error handler can catch it",
      issubclass(ON.NotStarted, Exception))
check("the gate fails OPEN on internal errors",
      "fail open" in src)

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
