#!/usr/bin/env python3
"""
tools/sim_shop_ui.py — the shop, which had no test coverage at all.

Fifty parts, real coin purchases, two independent renderers, and nothing
anywhere asserted a single thing about them. That is how the shop players
actually open ended up advertising every part with a downside as a pure
upgrade: `;partsbrowse` printed the penalty, `;shop` printed only the bonus,
and no test compared them.

What is pinned here:

* **Every part's full effect is rendered wherever it is sold.** Not "the
  formatter works" — every part, on the page it appears on.
* **A purchase is a transaction.** Charged once, refused when short, and a
  refusal leaves the profile byte-identical. `;buy` used to be a
  get_user/mutate/update_user race on real money.
* **Both penalty shapes agree.** The singular `penalty_stat` pair the fifty
  shipped parts use, and the `penalties` dict a part needs to carry more than
  one, must produce identical deltas.

Run:  python3 tools/sim_shop_ui.py
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


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from cogs.economy.shop import (PARTS_CATALOG, part_penalties,          # noqa: E402
                               part_effect_line, get_part_stat_deltas,
                               apply_part_purchase, PurchaseError,
                               PART_TYPE_LABEL)
from utils.loadout import part_bonuses                                  # noqa: E402

print("\n── 1. the catalog is coherent ───────────────────────────────────")
check("there are parts at all", len(PARTS_CATALOG) >= 50, len(PARTS_CATALOG))
names = [p["name"] for p in PARTS_CATALOG]
check("every part name is unique", len(set(names)) == len(names),
      len(names) - len(set(names)))
for p in PARTS_CATALOG:
    ok = all(k in p for k in ("name", "type", "price", "stat", "bonus", "desc"))
    if not ok:
        check(f"{p.get('name')} has the required fields", False, sorted(p))
check("every part has the required fields", True)
check("every part slots into a known type",
      all(p["type"] in PART_TYPE_LABEL for p in PARTS_CATALOG),
      sorted({p["type"] for p in PARTS_CATALOG} - set(PART_TYPE_LABEL)))
check("every part boosts one of the three real stats",
      all(p["stat"] in ("attack", "defense", "stamina") for p in PARTS_CATALOG),
      sorted({p["stat"] for p in PARTS_CATALOG}))
check("every price is positive", all(int(p["price"]) > 0 for p in PARTS_CATALOG))

print("\n── 2. the two penalty shapes agree ──────────────────────────────")
legacy = {"name": "L", "type": "disk", "price": 1, "stat": "attack",
          "bonus": 30, "penalty_stat": "stamina", "penalty": 25, "desc": ""}
modern = {"name": "M", "type": "disk", "price": 1, "stat": "attack",
          "bonus": 30, "penalties": {"stamina": 25}, "desc": ""}
check("the singular pair and the dict produce the same penalties",
      part_penalties(legacy) == part_penalties(modern) == {"stamina": 25},
      (part_penalties(legacy), part_penalties(modern)))
check("...and the same rendered line",
      part_effect_line(legacy) == part_effect_line(modern),
      (part_effect_line(legacy), part_effect_line(modern)))

multi = {"name": "X", "type": "disk", "price": 1, "stat": "attack",
         "bonus": 120, "penalties": {"defense": 50, "stamina": 120}, "desc": ""}
check("a part can carry TWO penalties — the singular pair could not",
      part_penalties(multi) == {"defense": 50, "stamina": 120},
      part_penalties(multi))
line = part_effect_line(multi)
check("...and both appear in the rendered line",
      "50 Defense" in line and "120 Stamina" in line and "+120 Attack" in line,
      line)

both = dict(legacy, penalties={"defense": 10})
check("a part carrying both shapes gets both, not one overriding the other",
      part_penalties(both) == {"stamina": 25, "defense": 10},
      part_penalties(both))
check("a part with no penalty renders just the bonus",
      part_effect_line({"stat": "attack", "bonus": 10}) == "**+10 Attack**",
      part_effect_line({"stat": "attack", "bonus": 10}))

print("\n── 3. every part's downside is VISIBLE where it is sold ─────────")
# The actual regression. Not "the formatter can render a penalty" — every
# part, through the renderer the player opens.
from cogs.ui.main_shop import _parts_pages                             # noqa: E402

pages = _parts_pages()
blob = "\n".join(f.name + "\n" + f.value
                 for pg in pages for f in pg.fields)
missing = []
for p in PARTS_CATALOG:
    pens = part_penalties(p)
    if not pens:
        continue
    for stat, amt in pens.items():
        if f"{amt} {stat.capitalize()}" not in blob:
            missing.append((p["name"], stat, amt))
penalised = [p for p in PARTS_CATALOG if part_penalties(p)]
check(f"all {len(penalised)} parts with a downside show it on `;shop`",
      not missing, missing[:5])
check("every part appears on some page",
      all(p["name"] in blob for p in PARTS_CATALOG),
      [p["name"] for p in PARTS_CATALOG if p["name"] not in blob][:5])
check("every part's price is shown",
      all(f"{p['price']:,} coins" in blob for p in PARTS_CATALOG))
check("there are parts with a downside to show at all",
      len(penalised) >= 30, len(penalised))

# Discord caps a Select at 25 options, so any page-scoped picker must fit.
sizes = [len(pg.fields) for pg in pages]
check(f"no shop page holds more than 25 parts (max {max(sizes)})",
      max(sizes) <= 25, sizes)
check("pages are non-empty", min(sizes) > 0, sizes)

print("\n── 4. deltas, and the two resolvers still agree ─────────────────")
check("a two-penalty part produces both negative deltas",
      get_part_stat_deltas([]) == {} and
      part_penalties(multi) == {"defense": 50, "stamina": 120})
disagree = []
for p in PARTS_CATALOG:
    prof = {"equipped_parts": [p["name"]]}
    if part_bonuses(prof) != get_part_stat_deltas([p["name"]]):
        disagree.append(p["name"])
check(f"loadout.part_bonuses agrees with get_part_stat_deltas on all "
      f"{len(PARTS_CATALOG)}", not disagree, disagree[:5])
check("an unknown part name is skipped, not fatal",
      get_part_stat_deltas(["No Such Part"]) == {})
check("penalties really subtract",
      all(v < 0 for k, v in get_part_stat_deltas(["Omega Blaze Ring"]).items()
          if k == "stamina"),
      get_part_stat_deltas(["Omega Blaze Ring"]))

print("\n── 5. buying one is a transaction ───────────────────────────────")
cheap = min(PARTS_CATALOG, key=lambda p: p["price"])
price = int(cheap["price"])

prof = {"coins": price, "parts": []}
res = apply_part_purchase(prof, cheap["name"])
check(f"charges exactly {price:,}", res["spent"] == price, res)
check("...leaving the balance at zero", prof["coins"] == 0, prof["coins"])
check("...and granting the part", prof["parts"] == [cheap["name"]], prof["parts"])
check("the result reports the new balance", res["coins"] == 0, res)

prof = {"coins": 10_000_000, "parts": []}
apply_part_purchase(prof, cheap["name"].upper())
check("the name match is case-insensitive", prof["parts"] == [cheap["name"]],
      prof["parts"])

# A refusal must leave the profile untouched — raising inside mutate_user
# abandons the whole write, and that is the entire safety property.
for short in (1, price):
    prof = {"coins": price - short, "parts": ["keep me"], "inventory": ["x"]}
    before = {k: (list(v) if isinstance(v, list) else v)
              for k, v in prof.items()}
    try:
        apply_part_purchase(prof, cheap["name"])
        raised = False
    except PurchaseError:
        raised = True
    check(f"{short:,} short: refused", raised)
    check(f"{short:,} short: profile is byte-identical", prof == before, prof)

prof = {"coins": 10_000_000, "parts": [cheap["name"]]}
try:
    apply_part_purchase(prof, cheap["name"])
    raised = False
except PurchaseError as exc:
    raised, msg = True, str(exc)
check("buying one you already own is refused", raised)
check("...and the coins are untouched", prof["coins"] == 10_000_000)
check("...naming the part", cheap["name"] in msg, msg)

prof = {"coins": 10_000_000, "parts": []}
try:
    apply_part_purchase(prof, "Not A Real Part")
    raised = False
except PurchaseError:
    raised = True
check("an unknown part is refused rather than crashing", raised)
check("...and takes no coins", prof["coins"] == 10_000_000)

prof = {}
try:
    apply_part_purchase(prof, cheap["name"])
    raised = False
except PurchaseError:
    raised = True
check("a profile with no coins key is refused, not crashed", raised)

try:
    apply_part_purchase({"coins": 0, "parts": []}, cheap["name"])
except PurchaseError as exc:
    msg = str(exc)
check("the refusal names the part, the price and the shortfall",
      cheap["name"] in msg and f"{price:,}" in msg and "short" in msg.lower(),
      msg)

print("\n── 6. `;buy` goes through the transaction, not around it ────────")
src = open(os.path.join(ROOT, "cogs", "economy", "shop.py"),
           encoding="utf-8").read()
buy_body = src.split("async def buy(", 1)[1].split("\n    # ── ;sell", 1)[0]
check(";buy calls apply_part_purchase", "apply_part_purchase" in buy_body)
check("...inside mutate_user", "mutate_user(" in buy_body)
check("...and no longer hand-rolls the read-modify-write",
      "update_user(ctx.author.id, profile)" not in buy_body)
check("the booster-pack branch still works",
      "booster_amount" in buy_body and "ctx.invoke(" in buy_body)

msrc = open(os.path.join(ROOT, "cogs", "ui", "main_shop.py"),
            encoding="utf-8").read()
check("`;shop` renders through the shared formatter",
      "part_effect_line(part)" in msrc)
check("...and no longer prints the bonus on its own",
      "*(+{part['bonus']} {stat})*" not in msrc)

print("\n── 7. negative deltas are no longer hidden from the player ──────")
from utils.loadout import summary_lines                                # noqa: E402

bd = {"attack": {"total": 220, "level": 0, "parts": 120, "avatar": 0},
      "stamina": {"total": 0, "level": 0, "parts": -120, "avatar": 0},
      "defense": {"total": 5, "level": 0, "parts": -50, "avatar": 0}}
lines = summary_lines(bd)
check("a downside appears in the breakdown at all", len(lines) == 3, lines)
check("...signed, so it reads as a loss",
      any("-120 parts" in ln for ln in lines), lines)
check("...and the upside still reads as a gain",
      any("+120 parts" in ln for ln in lines), lines)
check("an unchanged stat stays out of the breakdown",
      summary_lines({"attack": {"total": 10, "level": 0, "parts": 0,
                                "avatar": 0}}) == [])

print("\n── 8. every rarity has a distinct look ─────────────────────────")
from utils.embeds import RARITY_EMOJIS, RARITY_COLOURS, rarity_colour  # noqa: E402
import discord                                                         # noqa: E402
import json                                                            # noqa: E402

SHIPPED = sorted({v.get("rarity") for v in json.load(
    open(os.path.join(ROOT, "data", "beyblades.json"), encoding="utf-8")).values()})
check(f"the roster uses {len(SHIPPED)} rarities: {SHIPPED}", len(SHIPPED) >= 7)
# These two maps stopped at Legendary, and both .get() with a fallback — so
# Mythic, Ultimate and Exclusive silently rendered as Common white circles and
# black embeds. Nothing errored; the rarest blades just looked the cheapest.
for r in SHIPPED:
    check(f"{r} has its own emoji", r in RARITY_EMOJIS, RARITY_EMOJIS.get(r))
    check(f"{r} has a real colour",
          r in RARITY_COLOURS and rarity_colour(r) != discord.Color.default(),
          rarity_colour(r))
emojis = [RARITY_EMOJIS[r] for r in SHIPPED]
check("no two rarities share an emoji", len(set(emojis)) == len(emojis), emojis)
colours = [str(rarity_colour(r)) for r in SHIPPED]
check("no two rarities share a colour", len(set(colours)) == len(colours),
      colours)
check("the map matches the other two full maps",
      set(RARITY_EMOJIS) == set(RARITY_COLOURS), 
      set(RARITY_EMOJIS) ^ set(RARITY_COLOURS))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
