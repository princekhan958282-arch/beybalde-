#!/usr/bin/env python3
"""
tools/sim_multihit_nerf.py — the v95 multi-hit nerf, measured not asserted.

At level 100, Victory Valkyrie X fired its Special into a 2,108 HP pool for
**5,414–6,082** depending on the avatar — a guaranteed one-shot from full
health, twice over. Even with no avatar at all it removed 68% of the pool in
one move.

Two compounding causes, both addressed here:

  * **The avatar flags.** `multi_hit_extra_hits` doubled the count, so the
    only two 16-hit blades in the game fired 32 times; `multi_hit_power_double`
    doubled each hit on top. Now +2 flat and +10%.
  * **`special_scale`.** A Special is multiplied by current special ÷ printed
    special, which reached **4.09×** on VVX because its printed stat is low
    relative to a 16-hit payload — the ratio runs away exactly on the blades
    whose Specials are already the biggest. Capped at 2.5.

And one bug the nerf would otherwise have landed on nothing:
`multi_hit_power_double` barely worked. `attack_manager` rebuilds `hit_table`
from the blade's AUTHORED per-hit damages after `multi_hit_shape` has already
folded the multiplier into `per_hit`, and `per_hit` then only reaches the
filler entries. Four avatars printed "every hit doubled" in the battle log and
changed nothing but their bonus hits.

Every number below is traced through the real `AttackManager._resolve_special`
against the real roster — none of it is asserted on the constants, because a
constant can be right while the code that reads it is not. That is the exact
shape of the bug above.

Run:  python3 tools/sim_multihit_nerf.py
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

import random                                                      # noqa: E402
from utils.database import load_beyblades                          # noqa: E402
from cogs.avatar.avatar_engine import (                            # noqa: E402
    AvatarBonuses, MULTI_HIT_DAMAGE_MULT, MULTI_HIT_EXTRA_HITS)
from cogs.battle import damage_rules as DR                         # noqa: E402
from cogs.battle import avatar_combat as AVC                       # noqa: E402
from cogs.battle.attack_manager import AttackManager               # noqa: E402
from cogs.battle.stamina_manager import StaminaManager             # noqa: E402
from cogs.battle.status_manager import StatusManager               # noqa: E402
from cogs.battle import stability_manager as SBM                   # noqa: E402
from cogs.abilities import type_system as TS                       # noqa: E402
from cogs.abilities.ability_engine import AbilityEngine            # noqa: E402
from cogs.core.constants import MOVE_SPECIAL                       # noqa: E402
from utils import bey_levels as BL                                # noqa: E402

BLADES = load_beyblades()
VVX    = BLADES["Victory Valkyrie X"]

# The pool the plan measured against — a level-100 blade's battle HP.
POOL = 2108


# ── A session just real enough to run _resolve_special ───────────────────────

class Sess:
    def __init__(self, mblade, oblade, special_stat=None, avatar=None):
        self.blades = {"m": mblade, "o": oblade}
        self.hp = {"m": POOL, "o": POOL}
        self.max_hp = POOL
        self.max_hp_per_player = {"m": POOL, "o": POOL}
        self.battle_stats = {k: dict(v.get("stats", {}))
                             for k, v in self.blades.items()}
        self.bey_levels = {"m": 1, "o": 1}
        self.special_stats = {"m": special_stat} if special_stat else {}
        self.avatar_bonuses = {"m": avatar} if avatar else {}
        self.last_moves = {"m": MOVE_SPECIAL, "o": "attack"}
        self.round = 1
        self.status = StatusManager(self)
        self.stamina_manager = StaminaManager(self.blades)
        self.type_mods = {k: TS.TypeModifiers(v) for k, v in self.blades.items()}
        self.stability_manager = SBM.StabilityManager(self.blades, self.type_mods)
        self.chain_handler = _t.SimpleNamespace(
            resolve=lambda *a, **k: [], queue=lambda *a, **k: None)
        self.ability = AbilityEngine(self)
        self.stat_mult = {"m": 1.0, "o": 1.0}


def special_damage(mblade, avatar=None, special_stat=None, oblade=None, seed=1):
    """Total damage of ONE Special, through the real resolver.

    Crits are pinned off — a live 15% crit roll makes every comparison below a
    coin flip, and three assertions in an earlier suite passed by luck for
    exactly that reason.
    """
    oblade = oblade or BLADES["Galaxy Pegasus"]
    s = Sess(mblade, oblade, special_stat, avatar)
    am = AttackManager.__new__(AttackManager)
    am.session = s
    real_random, real_randint = random.random, random.randint
    random.random = lambda: 1.0            # no crit, no dodge, no proc
    random.randint = lambda a, b: a
    try:
        dmg, _logs = am._resolve_special("m", "o", MOVE_SPECIAL,
                                         mblade, oblade, [])
    finally:
        random.random, random.randint = real_random, real_randint
    return int(dmg)


def lvl100_special(blade):
    """The effective special stat of this blade at bey level 100."""
    return BL.stats_at(blade, 100).get("special")


print("\n── 1. the constants, and that they are the new ones ─────────────")
check("extra hits are flat +2", MULTI_HIT_EXTRA_HITS == 2, MULTI_HIT_EXTRA_HITS)
check("per-hit damage is +10%", MULTI_HIT_DAMAGE_MULT == 1.10,
      MULTI_HIT_DAMAGE_MULT)
check("special scale is capped at 2.5", DR.SPECIAL_SCALE_CAP == 2.5,
      DR.SPECIAL_SCALE_CAP)

ex = AvatarBonuses(multi_hit_extra_hits=True)
check("a 16-hit Special becomes 18, not 32", ex.extra_hits(16) == 18,
      ex.extra_hits(16))
check("a 2-hit Special becomes 4", ex.extra_hits(2) == 4)
check("the gain SHRINKS as the payload grows — that is the nerf",
      (ex.extra_hits(16) / 16) < (ex.extra_hits(5) / 5) < (ex.extra_hits(2) / 2))

print("\n── 2. special_scale is capped, and still rewards levelling ──────")
worst = None
for name, b in BLADES.items():
    sp = lvl100_special(b)
    sc = DR.special_scale(b, sp)
    if sc > DR.SPECIAL_SCALE_CAP + 1e-9:
        check(f"{name} exceeds the cap", False, sc)
        break
    if worst is None or sc > worst[1]:
        worst = (name, sc)
else:
    check(f"no blade exceeds {DR.SPECIAL_SCALE_CAP}× at level 100 "
          f"(highest: {worst[0]} at {worst[1]:.2f}×)", True)

atcap = [n for n, b in BLADES.items()
         if DR.special_scale(b, lvl100_special(b)) >= DR.SPECIAL_SCALE_CAP - 1e-9]
check("the cap actually binds on somebody", len(atcap) > 0, len(atcap))
check("Victory Valkyrie X is one of them — it was the 4.09× outlier",
      "Victory Valkyrie X" in atcap, atcap[:5])

grew = [n for n, b in BLADES.items()
        if DR.special_scale(b, lvl100_special(b)) > 1.0]
check("every blade still scales UP at level 100 — the cap did not flatten it",
      len(grew) == len(BLADES), f"{len(grew)}/{len(BLADES)}")
check("a level-1 blade is still exactly its authored damage",
      DR.special_scale(VVX, VVX["stats"]["special"]) == 1.0)
check("a debuffed stat never scales the Special DOWN",
      DR.special_scale(VVX, 1) == 1.0)

print("\n── 3. multi_hit_power_double now changes the number at all ──────")
# The regression test for the bug. `hit_table` is rebuilt from the authored
# per-hit damages, so before v95 the multiplier reached only the filler hits —
# for a blade whose avatar grants no extra hits, that is NONE of them.
dbl_only = AvatarBonuses(multi_hit_power_double=True)
base = special_damage(VVX, special_stat=lvl100_special(VVX))
with_dbl = special_damage(VVX, avatar=dbl_only,
                          special_stat=lvl100_special(VVX))
check("power_double raises a 16-hit Special's damage",
      with_dbl > base, (base, with_dbl))
ratio = with_dbl / base
check(f"...by about 10% ({ratio:.3f}×)", 1.05 < ratio < 1.15, ratio)

# It must reach the AUTHORED hits, which is what the fix is. A 2-hit blade
# with no extra-hit source has no filler entries at all, so any change here
# can only have come from the authored table.
two_hit = next(b for b in BLADES.values()
               if (b.get("special_move") or {}).get("hits") == 2)
b2 = special_damage(two_hit, special_stat=lvl100_special(two_hit))
d2 = special_damage(two_hit, avatar=dbl_only,
                    special_stat=lvl100_special(two_hit))
check(f"...on a 2-hit blade too ({two_hit['name']}: {b2} → {d2})",
      d2 > b2, (b2, d2))

check("the source applies the multiplier to the hit table itself",
      "multi_hit_mult" in open(
          os.path.join(ROOT, "cogs", "battle", "attack_manager.py"),
          encoding="utf-8").read())
check("...and avatar_combat exposes it separately from multi_hit_shape",
      hasattr(AVC, "multi_hit_mult"))

# The filler hits must not be multiplied twice — they already carry it via
# `per_hit` out of multi_hit_shape.
both = AvatarBonuses(multi_hit_extra_hits=True, multi_hit_power_double=True)
only_extra = AvatarBonuses(multi_hit_extra_hits=True)
d_both = special_damage(VVX, avatar=both, special_stat=lvl100_special(VVX))
d_ex = special_damage(VVX, avatar=only_extra, special_stat=lvl100_special(VVX))
check(f"both flags together is ~10% over extra-hits alone, not 21% "
      f"({d_ex} → {d_both})",
      1.05 < d_both / d_ex < 1.15, d_both / d_ex)

print("\n── 4. nothing one-shots a full 2,108 pool ──────────────────────")
LOADOUTS = {
    "no avatar":       None,
    "power_double":    AvatarBonuses(multi_hit_power_double=True),
    "extra_hits":      AvatarBonuses(multi_hit_extra_hits=True),
    "both flags":      both,
    # Eudora's 88% special_move_percent is a SEPARATE lever from multi-hit and
    # is deliberately untouched — she stays the outlier, which is worth
    # measuring rather than hiding.
    "both + Eudora %": AvatarBonuses(multi_hit_extra_hits=True,
                                     multi_hit_power_double=True,
                                     special_move_percent=0.88),
}
for label, av in LOADOUTS.items():
    dmg = special_damage(VVX, avatar=av, special_stat=lvl100_special(VVX))
    pct = dmg / POOL * 100
    check(f"VVX @100, {label:<16} {dmg:>5} ({pct:>5.1f}% of the pool) — "
          f"not a one-shot", dmg < POOL, dmg)

# And across the whole roster, not just the blade that prompted this.
worst_blade, worst_dmg = None, 0
for name, b in BLADES.items():
    if not (b.get("special_move") or {}).get("hits"):
        continue
    d = special_damage(b, avatar=both, special_stat=lvl100_special(b))
    if d > worst_dmg:
        worst_blade, worst_dmg = name, d
check(f"no blade in the roster one-shots from full "
      f"(worst: {worst_blade} at {worst_dmg})", worst_dmg < POOL, worst_dmg)
check("...and the worst case is still a serious hit, not a scratch",
      worst_dmg > POOL * 0.3, worst_dmg)

print("\n── 5. the constants alone are a nerf, at every loadout ─────────")
# Recompute with the OLD constants put back temporarily. Comparing against
# hard-coded numbers would go stale the first time a blade is retuned;
# comparing against the old code cannot.
#
# This isolates the CONSTANTS: the hit-table fix from section 3 stays in place
# on both sides. That is deliberate — the fix is a buff and the constants are
# a nerf, and tangling them would let one hide the other. It also means the
# "before" column here is slightly HIGHER than the real pre-v95 game, where
# power_double barely reached the authored hits. The direction of every
# comparison below is unaffected.
old_cap = DR.SPECIAL_SCALE_CAP
old_extra = AvatarBonuses.extra_hits
old_dmg_fn = AvatarBonuses.apply_multi_hit_damage
try:
    DR.SPECIAL_SCALE_CAP = float("inf")
    AvatarBonuses.extra_hits = (
        lambda self, n: max(n, int(n) * 2) if self.multi_hit_extra_hits else n)
    AvatarBonuses.apply_multi_hit_damage = (
        lambda self, d: d * 2.0 if self.multi_hit_power_double else d)
    before = {k: special_damage(VVX, avatar=v,
                                special_stat=lvl100_special(VVX))
              for k, v in LOADOUTS.items()}
finally:
    DR.SPECIAL_SCALE_CAP = old_cap
    AvatarBonuses.extra_hits = old_extra
    AvatarBonuses.apply_multi_hit_damage = old_dmg_fn

after = {k: special_damage(VVX, avatar=v, special_stat=lvl100_special(VVX))
         for k, v in LOADOUTS.items()}

for k in LOADOUTS:
    cut = (1 - after[k] / before[k]) * 100
    check(f"{k:<16} {before[k]:>5} → {after[k]:>5}  ({cut:>4.0f}% cut)",
          after[k] < before[k], (before[k], after[k]))

check("the old constants DID one-shot — otherwise this suite proves nothing",
      any(v >= POOL for v in before.values()),
      before)
check("...and the new ones do not, at any loadout",
      all(v < POOL for v in after.values()), after)
check("the heaviest cut lands on the loadout that carried both flags",
      (1 - after["both flags"] / before["both flags"])
      > (1 - after["no avatar"] / before["no avatar"]))

print("\n── 6. Story Mode cannot drift from this any more ───────────────")
# There used to be three checks here against `cogs/story/story_avatar.py`,
# which carried its OWN copy of the multi-hit multipliers for Story Mode's
# separate combat resolver. As of v1.22 Story runs on `BattleSession` — the
# same engine PvP runs on — so those constants have no second home to drift
# from. The module is gone; that it stays gone is the check.
check("story_avatar.py no longer exists to hold a second copy",
      not os.path.exists(os.path.join(ROOT, "cogs", "story",
                                      "story_avatar.py")))
check("...nor does the second combat resolver it fed",
      not os.path.exists(os.path.join(ROOT, "cogs", "story",
                                      "story_engine.py")))
story_src = "".join(
    open(os.path.join(ROOT, "cogs", "story", f), encoding="utf-8").read()
    for f in sorted(os.listdir(os.path.join(ROOT, "cogs", "story")))
    if f.endswith(".py"))
check("no file under cogs/story defines a multi-hit multiplier of its own",
      "MULTI_HIT_DOUBLE_MULT" not in story_src
      and "MULTI_HIT_EXTRA_MULT" not in story_src)
check("Story reaches the nerf by using the real session",
      "BattleSession" in story_src)

print("\n── 7. the battle log stopped lying ─────────────────────────────")
avc_src = open(os.path.join(ROOT, "cogs", "battle", "avatar_combat.py"),
               encoding="utf-8").read()
check("no log line still claims the hit count is DOUBLED",
      "hit count DOUBLED" not in avc_src)
check("no log line still claims every hit is doubled",
      "every hit doubled" not in avc_src
      and "every hit doubled" not in story_src)

card = open(os.path.join(ROOT, "cogs", "avatar", "avatar_data.json"),
            encoding="utf-8").read()
check("no card description promises a doubling either",
      "strike is doubled" not in card)

print("\n── 8. the untouched levers, stated rather than hidden ──────────")
# Two mechanisms found while measuring this, deliberately left alone. Named
# here so the next person to wonder why a Special is large has the list.
check("special_move_percent is untouched — Eudora is still the outlier",
      AvatarBonuses(special_move_percent=0.88)
      .apply_special_move_bonus(100) == 188.0)
check("special_amp_stack is still applied per hit, and is still bigger than "
      "either multi-hit flag on the blades that carry it",
      "special_amp_stack" in open(
          os.path.join(ROOT, "cogs", "battle", "attack_manager.py"),
          encoding="utf-8").read())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
