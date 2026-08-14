#!/usr/bin/env python3
"""
tools/sim_boss_tiers.py — the paid boss difficulty ladder.

Three things here would be expensive to get wrong and cheap to get wrong
silently:

1. **Tier 1 must be a no-op.** A player who never touches this feature has to
   get the fight they got yesterday — same AI rung, same HP, same rewards, same
   1-in-10,000,000. If the free tier drifts even slightly, a paid feature has
   quietly rebalanced the free one.

2. **The ladder must actually climb.** Five tiers where two of them are the
   same fight, or where a higher tier pays less, is a shop that takes money for
   nothing. Every column is asserted strictly monotonic.

3. **A refused purchase must not take the coins.** `charge` runs inside
   `database.mutate_user`, where raising abandons the whole read-modify-write.
   That is the entire safety property, and it is one `try` away from being a
   deduction with no fight attached.

Run:  python3 tools/sim_boss_tiers.py
"""
import os
import random
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


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from cogs.battle.boss import boss_tiers as BT                    # noqa: E402
from cogs.battle.boss import boss_copy as BC                     # noqa: E402
from cogs.battle.boss import boss_ai as AI                       # noqa: E402

ORD = BT.ORDERED

print("\n── 1. the ladder is five tiers and climbs on every column ───────")
check("there are exactly five tiers", len(ORD) == 5, ORD)
check("...and TIERS holds no others", set(BT.TIERS) == set(ORD),
      set(BT.TIERS) ^ set(ORD))
check("the first is the default", ORD[0] == BT.DEFAULT_TIER, BT.DEFAULT_TIER)

for col, direction in (("price", "up"), ("hp_mult", "up"), ("atk_mult", "up"),
                       ("reward_mult", "up"), ("rungs", "flat-or-up"),
                       ("perfect_odds", "down")):
    vals = [BT.TIERS[k][col] for k in ORD]
    if direction == "up":
        ok = all(b > a for a, b in zip(vals, vals[1:]))
    elif direction == "down":
        ok = all(b < a for a, b in zip(vals, vals[1:]))
    else:
        ok = all(b >= a for a, b in zip(vals, vals[1:]))
    check(f"{col} goes {direction}: {vals}", ok, vals)

for k in ORD:
    t = BT.TIERS[k]
    for field in ("key", "label", "emoji", "blurb", "bands"):
        check(f"{k} has a {field}", bool(t.get(field)))
    check(f"{k}: key matches its slot", t["key"] == k)

print("\n── 2. the free tier is EXACTLY today's fight ────────────────────")
std = BT.TIERS["standard"]
check("it is free", std["price"] == 0, std["price"])
check("it moves the AI zero rungs", std["rungs"] == 0, std["rungs"])
check("it does not scale HP", std["hp_mult"] == 1.0, std["hp_mult"])
check("...or attack", std["atk_mult"] == 1.0, std["atk_mult"])
check("...or rewards", std["reward_mult"] == 1.0, std["reward_mult"])
check("Perfect stays at the shipped 1 in 10,000,000",
      std["perfect_odds"] == BC.PERFECT_ODDS,
      (std["perfect_odds"], BC.PERFECT_ODDS))
check("its grade bands are byte-identical to boss_copy.GRADE_BANDS",
      [tuple(b) for b in std["bands"]] == [tuple(b) for b in BC.GRADE_BANDS],
      std["bands"])
for n in (1, 3000, 10_000, 250_000):
    check(f"scale_hp({n}) is unchanged at the free tier",
          BT.scale_hp(n, "standard") == n, BT.scale_hp(n, "standard"))
    check(f"scale_reward({n}) is unchanged at the free tier",
          BT.scale_reward(n, "standard") == n)
check("scale_attack is unchanged too",
      BT.scale_attack(137.5, "standard") == 137.5)

print("\n── 3. an unknown tier fails SAFE, not closed ────────────────────")
for bad in (None, "", "  ", "NIGHTMARE ", "legendary", "1", 7, object()):
    got = BT.get(bad)
    if bad == "NIGHTMARE ":
        check("case and whitespace still resolve a real tier",
              got["key"] == "nightmare", got["key"])
        continue
    check(f"{bad!r} resolves to the free tier", got["key"] == "standard",
          got["key"])
check("an unknown tier is therefore also free", BT.price_of("bogus") == 0)

print("\n── 4. the AI rung walks up, and never down ──────────────────────")
check("the ladder is the one boss_ai actually reads",
      list(BT.ORDER) == ["rookie", "veteran", "elite", "legend", "nightmare"]
      and set(BT.ORDER) == set(AI.DIFFICULTY), (BT.ORDER, set(AI.DIFFICULTY)))

for base in BT.ORDER:
    rungs = [BT.walk_difficulty(base, k) for k in ORD]
    idx = [BT.ORDER.index(r) for r in rungs]
    check(f"from {base}: {rungs}", all(b >= a for a, b in zip(idx, idx[1:])),
          rungs)
    check(f"from {base}: the free tier changes nothing",
          rungs[0] == base, rungs[0])
    check(f"from {base}: nothing ever gets EASIER than the boss shipped",
          all(i >= BT.ORDER.index(base) for i in idx), rungs)
    check(f"from {base}: the top tier is nightmare",
          rungs[-1] == "nightmare", rungs[-1])

check("a boss with a typo'd difficulty is treated as elite, not rookie",
      BT.walk_difficulty("eliet", "standard") == "elite",
      BT.walk_difficulty("eliet", "standard"))
check("...so a typo cannot hand out a rookie AI at Nightmare prices",
      BT.walk_difficulty("eliet", "nightmare") == "nightmare")
check("a missing difficulty behaves the same",
      BT.walk_difficulty(None, "standard") == "elite")

# The two real bosses.
from cogs.battle.boss import drakos as DK                        # noqa: E402
from cogs.battle.boss import boss_abilities as AB                # noqa: E402
check("Drakos still fights at elite when nobody pays",
      BT.walk_difficulty(DK.DRAKOS["difficulty"], "standard") == "elite",
      DK.DRAKOS["difficulty"])
check("NEMESIS still fights at legend when nobody pays",
      BT.walk_difficulty(AB.NEMESIS["difficulty"], "standard") == "legend",
      AB.NEMESIS["difficulty"])
check("a paid Drakos out-climbs a free NEMESIS",
      BT.ORDER.index(BT.walk_difficulty(DK.DRAKOS["difficulty"], "savage"))
      > BT.ORDER.index(BT.walk_difficulty(AB.NEMESIS["difficulty"], "standard")))

print("\n── 5. the grades really do get better ───────────────────────────")
shares = {}
for k in ORD:
    bands = BT.TIERS[k]["bands"]
    gw = sum(b[0] for b in bands)
    shares[k] = sum(w for w, _lo, _hi, g in bands
                    if g in ("Pristine", "Flawless")) / gw
    check(f"{k}: the band grades match boss_copy's",
          [b[3] for b in bands] == [b[3] for b in BC.GRADE_BANDS],
          [b[3] for b in bands])
    check(f"{k}: the penalty ranges are untouched",
          [(b[1], b[2]) for b in bands] == [(b[1], b[2]) for b in BC.GRADE_BANDS])
    check(f"{k}: every weight is positive — no grade is unreachable",
          all(b[0] > 0 for b in bands), bands)
vals = [shares[k] for k in ORD]
print("     Pristine+ share: "
      + "  ".join(f"{k} {shares[k]*100:.0f}%" for k in ORD))
check("the Pristine-or-better share climbs every tier",
      all(b > a for a, b in zip(vals, vals[1:])),
      [f"{v:.3f}" for v in vals])
check("at Standard it is the shipped ~1.5%", 0.010 < vals[0] < 0.020,
      f"{vals[0]:.4f}")
check("at Nightmare it is the common outcome", vals[-1] > 0.45,
      f"{vals[-1]:.4f}")

print("\n── 6. roll_copy honours the tier, and its defaults are unchanged ─")
prof = {"key": "drakos", "name": "Aetherion Drakos", "hp": 3000,
        "attack": 150, "defense": 130, "stamina": 120,
        "copy_total_range": (300, 450), "abilities": []}

# Defaults: every existing caller must be untouched.
rng = random.Random(7)
a = BC.roll_copy(prof, random.Random(7))
b = BC.roll_copy(prof, random.Random(7),
                 perfect_odds=BC.PERFECT_ODDS, bands=BC.GRADE_BANDS)
check("passing the defaults explicitly changes nothing",
      a["grade"] == b["grade"] and a["card_total"] == b["card_total"],
      (a["grade"], b["grade"]))

# Perfect is reachable at every tier when the roll lands.
for k in ORD:
    t = BT.TIERS[k]

    class AlwaysOne(random.Random):
        def randint(self, lo, hi):
            return 1 if hi == t["perfect_odds"] else super().randint(lo, hi)

    c = BC.roll_copy(prof, AlwaysOne(1), perfect_odds=t["perfect_odds"],
                     bands=t["bands"])
    check(f"{k}: a winning roll is a Perfect", c["grade"] == "Perfect",
          c["grade"])
    check(f"{k}: ...with the complete kit and a guaranteed awakening",
          c["loadout"] == "Complete kit" and c["awakening"] is True,
          (c["loadout"], c["awakening"]))

# And a losing roll never is, at any tier.
for k in ORD:
    t = BT.TIERS[k]
    r = random.Random(99)
    grades = [BC.roll_copy(prof, r, perfect_odds=t["perfect_odds"],
                           bands=t["bands"])["grade"] for _ in range(300)]
    check(f"{k}: 300 ordinary rolls produced no Perfect",
          "Perfect" not in grades, grades.count("Perfect"))
    check(f"{k}: ...and did produce a spread of grades",
          len(set(grades)) >= 3, sorted(set(grades)))

# The tier visibly shifts the distribution, not just the lottery.
r = random.Random(5)
lo_g = [BC.roll_copy(prof, r, bands=BT.TIERS["standard"]["bands"])["grade"]
        for _ in range(800)]
hi_g = [BC.roll_copy(prof, r, bands=BT.TIERS["nightmare"]["bands"])["grade"]
        for _ in range(800)]
good = ("Pristine", "Flawless")
check(f"Nightmare rolls far more Pristine+ than Standard "
      f"({sum(g in good for g in lo_g)} -> {sum(g in good for g in hi_g)} of 800)",
      sum(g in good for g in hi_g) > sum(g in good for g in lo_g) * 5,
      (sum(g in good for g in lo_g), sum(g in good for g in hi_g)))

print("\n── 7. paying for it ─────────────────────────────────────────────")
for k in ORD:
    t = BT.TIERS[k]
    p = {"coins": 1_000_000}
    spent = BT.charge(p, k)
    check(f"{k}: charges exactly {t['price']:,}", spent == t["price"], spent)
    check(f"{k}: ...and the balance moved by exactly that",
          p["coins"] == 1_000_000 - t["price"], p["coins"])

p = {"coins": 10_000_000}
check("the free tier deducts nothing", BT.charge(p, "standard") == 0
      and p["coins"] == 10_000_000, p["coins"])

# The safety property: a refusal leaves the profile untouched, because the
# raise happens BEFORE the write and abandons the mutate_user transaction.
price = BT.price_of("nightmare")
for short in (1, 1000, price):
    p = {"coins": price - short, "inventory": ["x"]}
    before = dict(p)
    try:
        BT.charge(p, "nightmare")
        raised = False
    except BT.TierError:
        raised = True
    check(f"{short:,} coins short: refused", raised)
    check(f"{short:,} coins short: the coins are still there", p == before, p)

p = {"coins": price}
check("exactly enough is enough", BT.charge(p, "nightmare") == price
      and p["coins"] == 0, p)
p = {}
try:
    BT.charge(p, "hardened")
    raised = False
except BT.TierError:
    raised = True
check("a profile with no coins key is refused, not crashed", raised)

check("can_afford agrees with charge",
      BT.can_afford({"coins": price}, "nightmare")
      and not BT.can_afford({"coins": price - 1}, "nightmare"))
check("everyone can afford the free tier",
      BT.can_afford({}, "standard") and BT.can_afford({"coins": 0}, "standard"))

try:
    BT.charge({"coins": 0}, "savage")
except BT.TierError as exc:
    msg = str(exc)
check("the refusal names the tier, the price and the shortfall",
      "Savage" in msg and "75,000" in msg and "short" in msg.lower(), msg)

print("\n── 8. the wiring ───────────────────────────────────────────────")
src = open(os.path.join(ROOT, "cogs", "battle", "boss", "boss_battle.py"),
           encoding="utf-8").read()
check("the AI rung is walked, not read straight off the boss profile",
      "difficulty=btiers.walk_difficulty(" in src)
check("no call site still passes the raw cfg difficulty to the AI",
      'difficulty=self.cfg["difficulty"]' not in src)
check("boss HP is tier-scaled on top of party scaling",
      "btiers.scale_hp(scaled_boss_hp(cfg, extra)" in src)
check("...and so is attack", "btiers.scale_attack(" in src)
check("rewards are scaled once, before the party loop",
      src.index("t_coins  = btiers.scale_reward") < src.index("for member in fight.party:"))
check("the drop roll receives the tier's odds AND bands",
      "perfect_odds=tcfg[\"perfect_odds\"]" in src and 'bands=tcfg["bands"]' in src)
check("every member is charged, not just the host",
      "for member in self.party[1:]:" in src)
check("the charge goes through mutate_user, not get_user/update_user",
      "btiers.charge_for(" in src)
check("Join quotes the price before anyone commits",
      "btiers.can_afford(get_user(interaction.user.id)" in src)
check("a member who cannot pay is dropped, not left blocking the launch",
      "self.party.remove(m)" in src and "broke.append(member)" in src)
check("...and is told, rather than silently vanishing",
      "Left behind" in src)
check("a host who cannot pay falls back to free rather than failing",
      "tier = btiers.DEFAULT_TIER" in src)
check("...and the party is told that too", "couldn't cover" in src)
check("the lobby card quotes the chosen tier",
      "lobby_card_state(\n                self.key, self.party, tier=self.tier"
      in src or "tier=self.tier," in src)
check("only the host may change the difficulty",
      "Only the host picks the difficulty." in src)
check("there is a command to read the ladder", 'name="bosstiers"' in src)

bsrc = open(os.path.join(ROOT, "cogs", "battle", "boss", "boss_copy.py"),
            encoding="utf-8").read()
check("roll_copy's new arguments default to the shipped constants",
      "perfect_odds: Optional[int] = None" in bsrc
      and "bands: Optional[list] = None" in bsrc)
check("a zero or negative odds value cannot crash the roll",
      BC.roll_copy(prof, random.Random(1), perfect_odds=0) is not None)

hsrc = open(os.path.join(ROOT, "cogs", "ui", "help_cog.py"), encoding="utf-8").read()
check("the command is in help", ";bosstiers" in hsrc)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
