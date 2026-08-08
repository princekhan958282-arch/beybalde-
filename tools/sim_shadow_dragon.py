#!/usr/bin/env python3
"""
tools/sim_shadow_dragon.py — Shadow Dragon King's Dark Weather buff.

Asserts what the card CLAIMS is what the engine DOES:

  * always active — never switched off, including after Dark Void
  * +25 Attack and +25 Defense, standing, and NOT +50 from stacking
  * +25% Special Move damage
  * ordinary attacks are untouched

The stacking check is the important one. Dark Weather was authored as a plain
passive with a 2-round buff, and `StatusManager.add_buff` APPENDS rather than
replaces — so a passive firing every round always had two entries live and the
real value was +50/+50, double what the card said.

Run:  python3 tools/sim_shadow_dragon.py
"""
import json
import os
import sys

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


from cogs.abilities.ability_engine import AbilityEngine    # noqa: E402
from cogs.battle.status_manager import StatusManager       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLADES = json.load(open(os.path.join(ROOT, "data", "beyblades.json"),
                        encoding="utf-8"))
SDK = BLADES["Shadow Dragon King"]
DW = [a for a in SDK["abilities"] if a["name"] == "Dark Weather"][0]
AD = [a for a in SDK["abilities"] if a["name"] == "Absolute Darkness"][0]

print("\n── 1. the data says what the card says ──────────────────────────")
check("Attack boost is 25", DW["attack_boost"] == 25, DW["attack_boost"])
check("Defense boost is 25", DW["defense_boost"] == 25, DW["defense_boost"])
check("Special damage bonus is 25%",
      DW["special_damage_bonus_pct"] == 0.25, DW["special_damage_bonus_pct"])
check("nothing claims the ability is ever disabled",
      "disabled_after_special" not in DW, list(DW))



def effects_in(node):
    """Every `effect` / `op` value anywhere in an ability, at any depth.

    Structural rather than a substring search on the JSON text: the ability
    carries a `_note` explaining that disable_ability_2 was removed, and a text
    search matches its own documentation.
    """
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("effect", "op") and isinstance(v, str):
                found.append(v)
            found.extend(effects_in(v))
    elif isinstance(node, list):
        for item in node:
            found.extend(effects_in(item))
    return found


check("Absolute Darkness no longer disables it",
      "disable_ability_2" not in effects_in(AD), effects_in(AD))
check("no ability on this blade disables anything",
      "disable_ability_2" not in effects_in(SDK["abilities"]))
check("no description still promises a stamina penalty",
      "Stamina than normal" not in DW["description"])
check("no description still says Dark Weather closes",
      not any(w in json.dumps(SDK) for w in
              ("permanently closes Dark Weather",
               "Dark Weather is also permanently disabled")))
check("Absolute Darkness still grants its +70%",
      '"value": 0.7' in json.dumps(AD))

print("\n── 2. the engine agrees, and does not stack ─────────────────────")


class FakeSession:
    def __init__(self):
        self.status = StatusManager.__new__(StatusManager)
        st = self.status
        st.active_buffs = {}
        for attr in ("ability_2_disabled", "pre_special_amp",
                     "guaranteed_crit_turns"):
            setattr(st, attr, {})
        self.hp = {"p": 2000, "e": 2000}
        self.max_hp_per_player = {"p": 2000, "e": 2000}
        self.max_hp = 2000
        self.blades = {"p": SDK, "e": SDK}
        self.chain_handler = _FakeChains()


class _FakeChains:
    """Records queued chain steps so Absolute Darkness can fire without a
    real session, and so the test can assert what it queued."""

    def __init__(self):
        self.queued = []

    def queue(self, key, steps):
        self.queued.append((key, steps))


def make_engine():
    sess = FakeSession()
    eng = AbilityEngine.__new__(AbilityEngine)
    eng.session = sess
    eng.st = sess.status
    eng._compiled = {}
    eng.once_fired = set()
    eng.modes = {}
    eng.ability_2_disabled = sess.status.ability_2_disabled
    return eng, sess


eng, sess = make_engine()
rules = eng._rules_for(SDK, "p")
check("Dark Weather compiles to rules", any(
    r.get("_name") == "Dark Weather" for _, r in rules), [r.get("_name") for _, r in rules])
check("its standing buff fires once per battle", any(
    r.get("_name") == "Dark Weather" and r.get("once") == "battle"
    for _, r in rules))

# Fire the passive over many rounds, ticking buffs the way a real round does.
for rnd in range(1, 13):
    logs = []
    eng._fire("passive", "p", "e", SDK, "attack", "win", 0, 0, logs)
    for b in list(sess.status.active_buffs.get("p", [])):
        b["rounds_left"] -= 1
        if b["rounds_left"] <= 0:
            sess.status.active_buffs["p"].remove(b)

atk = sess.status.get_buff_bonus("p", "attack")
dfn = sess.status.get_buff_bonus("p", "defense")
check("Attack settles at exactly +25 after 12 rounds", atk == 25, atk)
check("Defense settles at exactly +25 after 12 rounds", dfn == 25, dfn)
check("only one buff entry per stat exists, not a growing pile",
      len(sess.status.active_buffs.get("p", [])) == 2,
      sess.status.active_buffs.get("p"))

print("\n── 3. +25% applies to the Special and nothing else ──────────────")
eng, sess = make_engine()
d_special, _ = eng._fire("passive", "p", "e", SDK, "special", "win", 100, 0, [])
eng2, sess2 = make_engine()
d_attack, _ = eng2._fire("passive", "p", "e", SDK, "attack", "win", 100, 0, [])
check("a Special is amplified by 25%", d_special == 125, d_special)
check("an ordinary Attack is NOT amplified", d_attack == 100, d_attack)
check("the amp is 25%, not the old 50%", d_special != 150, d_special)

print("\n── 4. it survives Dark Void ─────────────────────────────────────")
eng, sess = make_engine()
eng._fire("passive", "p", "e", SDK, "attack", "win", 0, 0, [])
before = sess.status.get_buff_bonus("p", "attack")
# Fire the Special — Absolute Darkness triggers here.
eng._fire("on_special", "p", "e", SDK, "special", "win", 100, 0, [])
check("Dark Void does not flag the ability as disabled",
      not eng.ability_2_disabled.get("p"), eng.ability_2_disabled)
queued = [s for _k, steps in sess.chain_handler.queued for s in steps]
check("Absolute Darkness still queued its +70% chain",
      any(s.get("effect") == "all_stats_boost" for s in queued), queued)
check("...and queued no disable step",
      not any(s.get("effect") == "disable_ability_2" for s in queued), queued)
rules_after = eng._rules_for(SDK, "p")
check("Dark Weather's rules are still live after the Special",
      any(r.get("_name") == "Dark Weather" for _, r in rules_after))
dmg_after, _ = eng._fire("passive", "p", "e", SDK, "special", "win", 100, 0, [])
check("...and still amplifies the next Special by 25%", dmg_after == 125,
      dmg_after)
check("the standing buff is still there", before == 25 and
      sess.status.get_buff_bonus("p", "attack") == 25,
      sess.status.get_buff_bonus("p", "attack"))

print("\n── 5. the file is still valid and nothing else moved ────────────")
check("beyblades.json parses", isinstance(BLADES, dict))
check("the roster is intact", len(BLADES) >= 78, len(BLADES))
check("Shadow Dragon King's stats are unchanged",
      SDK["stats"] == {"attack": 130, "defense": 130, "stamina": 130,
                       "special": 130, "hp": 122}, SDK["stats"])
check("Dark Void's damage is unchanged",
      SDK["special_move"]["damage_per_hit"] == 110)
check("Dark Void is still true damage",
      SDK["special_move"]["true_damage"] is True)

# Every other blade must still compile — a malformed edit here would only show
# up as a runtime error mid-battle for some unrelated bey.
broken = []
for name, blade in BLADES.items():
    if not isinstance(blade, dict):
        continue
    try:
        e, _ = make_engine()
        e._rules_for(blade, "p")
    except Exception as exc:                             # noqa: BLE001
        broken.append((name, repr(exc)[:60]))
check("all blades still compile their abilities", not broken, broken[:5])

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
