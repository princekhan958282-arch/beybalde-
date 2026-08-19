#!/usr/bin/env python3
"""
tools/sim_gaster_special.py — does Gaster's skill 3 add 70% or take it away?

Why this exists
---------------
Reported: Dr. W. D. Gaster's third skill, "Gaster Blaster — Special damage
+70%", behaves like a NERF in normal PvP — as though the Special were being
multiplied by 0.70 instead of 1.70.

That is a claim about a number at the end of a five-stage pipeline, and the
only way to answer it is to walk every stage and print what comes out. Reading
the code was not enough: the arithmetic is correct in isolation at every point
I looked, so either the fault is somewhere I did not think to look, or the
number a player sees is being produced by something other than this path.

So this suite is a TRACE first and a regression guard second. It prints the
damage at each stage so the next person to report this has numbers to argue
with rather than an impression.

The stages, in the order a Special actually goes through them
-------------------------------------------------------------
    1. the card             avatar_data.json -> special_move_percent
    2. the skill in play    avatar_skills.bonuses_for(card, slot)
    3. the battle snapshot  avatar_engine.get_battle_bonuses -> AvatarBonuses
    4. the multiplier       AvatarBonuses.apply_special_move_bonus
    5. the battle           attack_manager -> avatar_combat.apply_ult_bonus

Two suspects were named up front and both are cleared here, on the record:

  * `attack_manager` skips the avatar bonus entirely for a blade in
    SELF_MANAGED_HITS. That set is EMPTY, so the skip never fires — but it is
    asserted, because a blade added to it later would silently lose the bonus.
  * Slot 3 costs 75 of 100 energy. A player who cannot afford it gets slot 0
    and NO avatar bonuses at all — not 70% of them. Asserted separately so the
    two failure modes can never be confused for each other again.

Run:  python3 tools/sim_gaster_special.py
"""
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


from cogs.avatar import avatar_skills as AS                    # noqa: E402
from cogs.avatar.avatar_engine import (AvatarBonuses,          # noqa: E402
                                       avatar_engine)

avatar_engine.load()
GID = "avatar_u001"
CARD = avatar_engine.get_avatar(GID)
SLOT = 3
WANT = 0.7          # +70%, i.e. x1.70


def profile(slot=SLOT, energy=AS.MAX_ENERGY, locked=None):
    """A profile shaped the way the live store shapes it.

    `avatar_skill` is a DICT keyed by avatar id, not a bare int. Getting that
    wrong makes `chosen_slot` fall back to 1 and every downstream number look
    like the skill is being ignored — which is exactly the false trail this
    investigation started down.
    """
    p = {AS.K_CHOICE: {GID: slot}, AS.K_ENERGY: energy}
    if locked is not None:
        p[AS.K_LOCKED] = locked
    return p


# ══════════════════════════════════════════════════════════════════════════════
print("\n── stage 1: the card ────────────────────────────────────────────")

check("Gaster is in the roster", CARD is not None)
skills = CARD.get("skills") or []
check("it has three skills", len(skills) == 3, len(skills))
check("skill 3 is Gaster Blaster", skills[2]["name"] == "Gaster Blaster",
      skills[2].get("name"))
check("...declaring special_move_percent 0.7",
      skills[2]["bonuses"].get("special_move_percent") == WANT,
      skills[2]["bonuses"].get("special_move_percent"))
check("...and the card's top-level block agrees",
      CARD["bonuses"].get("special_move_percent") == WANT,
      CARD["bonuses"].get("special_move_percent"))
# A positive number here is the difference between a buff and a nerf. 0.7 means
# +70%; -0.3 would be the nerf that was reported.
check("the value is POSITIVE — a nerf would be a negative number",
      CARD["bonuses"]["special_move_percent"] > 0)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── stage 2: the skill in play ───────────────────────────────────")

for slot in (1, 2, 3):
    b = AS.bonuses_for(CARD, slot)
    print(f"       slot {slot}: special_move_percent="
          f"{b.get('special_move_percent')}  crit={b.get('crit_percent')}")
check("slot 3 supplies the special bonus",
      AS.bonuses_for(CARD, SLOT).get("special_move_percent") == WANT)
check("...and slots 1 and 2 do not — one skill applies at a time",
      not AS.bonuses_for(CARD, 1).get("special_move_percent")
      and not AS.bonuses_for(CARD, 2).get("special_move_percent"))

check("the standing pick resolves to the slot chosen",
      AS.active_slot(profile(3), CARD) == 3, AS.active_slot(profile(3), CARD))
check("...for every slot, not just 3",
      [AS.active_slot(profile(s), CARD) for s in (1, 2, 3)] == [1, 2, 3])
check("in battle the LOCKED slot wins over the standing pick",
      AS.active_slot(profile(3, locked=2), CARD) == 2)

# The other failure mode, kept distinct on purpose: unaffordable is 0% of the
# card, not 70% of the Special.
check(f"slot 3 costs {AS.skill_cost(3)} of {AS.MAX_ENERGY} energy",
      AS.skill_cost(3) == 75)
check("a player who could not afford it is locked to 0",
      AS.active_slot(profile(3, energy=0, locked=0), CARD) == 0)
check("...and then gets NO avatar bonus at all, which is a different bug "
      "from a reduced one",
      not AS.bonuses_for(CARD, 0).get("special_move_percent"))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── stage 3 & 4: the multiplier ──────────────────────────────────")

av = AvatarBonuses(special_move_percent=WANT)
check("the bonus survives into an AvatarBonuses", av.special_move_percent == WANT)
# If this were False the whole avatar layer is skipped by `_av()` and the
# Special does 100% — the other way this could read as "the bonus does nothing".
check("has_any_bonus sees it, so avatar_combat will not skip the avatar",
      av.has_any_bonus)

print(f"       {'special':>9}{'x1.70 expected':>17}{'actual':>9}")
for dmg in (100, 135, 154, 170):
    got = av.apply_special_move_bonus(dmg)
    print(f"       {dmg:>9}{dmg * 1.7:>17.0f}{got:>9.0f}")
    check(f"a {dmg} Special becomes {round(dmg * 1.7)}",
          abs(got - dmg * 1.7) < 0.01, got)

# The reported symptom, stated as a test so it cannot come back quietly.
worst = min(av.apply_special_move_bonus(d) / d for d in (100, 135, 154, 170))
check("the multiplier is ABOVE 1 at every size — it is a buff, not a nerf",
      worst > 1.0, worst)
check("...and is nowhere near the reported 0.70", abs(worst - 0.70) > 0.5,
      worst)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── stage 5: the battle ──────────────────────────────────────────")

import cogs.battle.avatar_combat as AVC                        # noqa: E402
from cogs.abilities.special_moves import SELF_MANAGED_HITS     # noqa: E402


class FakeSession:
    def __init__(self, bonuses):
        self.avatar_bonuses = {"p": bonuses}
        self.hp = {"p": 900, "e": 1000}
        self.round = 1


out, logs = AVC.apply_ult_bonus(FakeSession(av), "p", 154, 0)
print(f"       apply_ult_bonus(154) -> {out}")
for line in logs:
    print(f"       {line.strip()}")
check("the battle path applies the same x1.70", out == 262, out)
check("...and says so in the log the player reads",
      any("154" in l and "262" in l for l in logs), logs)

# No avatar equipped must be a clean pass-through, or every player without a
# card is quietly affected by this code path.
null_out, null_logs = AVC.apply_ult_bonus(
    FakeSession(AvatarBonuses()), "p", 154, 0)
check("a player with no avatar bonus is untouched", null_out == 154, null_out)
check("...and gets no log line about it", not null_logs, null_logs)

# The skip that would silently drop the bonus for a whole blade.
check("SELF_MANAGED_HITS is empty, so no blade skips the avatar bonus",
      not SELF_MANAGED_HITS, sorted(SELF_MANAGED_HITS))
src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "cogs", "battle", "attack_manager.py"),
    encoding="utf-8").read()
check("...and attack_manager still gates the bonus on exactly that set",
      "if not self_managed and total_dmg > 0" in src)


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 66)
print("  Every stage measures x1.70. If the Special is reading low in a real")
print("  fight, the cause is NOT in the avatar layer — the next places to look")
print("  are the boss multiplier (PLAYER_SPECIAL_VS_BOSS = 0.20, and avatar")
print("  bonuses are dropped entirely in boss fights) and the blade's own")
print("  Special abilities.")
print("=" * 66)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 66)
sys.exit(1 if FAIL else 0)
