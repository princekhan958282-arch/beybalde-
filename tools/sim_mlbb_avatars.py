#!/usr/bin/env python3
"""
tools/sim_mlbb_avatars.py — seven new avatars and the MLBB banner.

The check that matters most is containment: MLBB is a CLOSED pool. A 15,000,000
pack that could hand back a Common would be indefensible, and equally the four
existing packs must not start rolling MLBB avatars just because a new rarity
appeared in the data.

Run:  python3 tools/sim_mlbb_avatars.py
"""
import json
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


from cogs.avatar import avatar_shop as SHOP                   # noqa: E402
from cogs.avatar.avatar_engine import avatar_engine, AvatarBonuses  # noqa: E402
from cogs.avatar.avatar_utils import (                        # noqa: E402
    RARITY_ORDER, RARITY_COLORS, RARITY_EMOJI, validate_avatar_data)

avatar_engine.load()
CARDS = {a["name"]: a for a in avatar_engine.get_all_avatars()}
NEW = ["Mare", "Cobra Titan", "Miffy", "Freya", "Vexana", "Helcurt", "Eudora"]

print("\n── 1. all seven exist and validate ──────────────────────────────")
for name in NEW:
    check(f"{name} exists", name in CARDS)
bad = [(n, validate_avatar_data(CARDS[n])[1]) for n in NEW
       if n in CARDS and not validate_avatar_data(CARDS[n])[0]]
check("every new card validates", not bad, bad)
ids = [a["id"] for a in avatar_engine.get_all_avatars()]
check("no duplicate ids", len(ids) == len(set(ids)))
names = [a["name"] for a in avatar_engine.get_all_avatars()]
check("no duplicate names", len(names) == len(set(names)))

print("\n── 2. built like Argus and Dyrroth: 3 skills, 2 stats ───────────")
STAT_KEYS = ("attack_flat", "attack_percent", "defence_flat", "defence_percent",
             "stamina_flat", "stamina_percent")
for name in NEW:
    card = CARDS[name]
    check(f"{name} has exactly 3 skills", len(card.get("skills") or []) == 3,
          len(card.get("skills") or []))
    stats = [k for k in STAT_KEYS if card["bonuses"].get(k)]
    check(f"{name} carries at least 2 stat lines", len(stats) >= 2, stats)
    check(f"{name} has a type", card.get("type") in
          ("attack", "defense", "stamina", "balance"), card.get("type"))
    check(f"{name}'s skills all have a name and text",
          all(s.get("name") and s.get("description") for s in card["skills"]))

print("\n── 3. the requested numbers ─────────────────────────────────────")
mare = CARDS["Mare"]["bonuses"]
check("Mare: +209 defence", mare["defence_flat"] == 209.0, mare["defence_flat"])
check("Mare: +30% defence", mare["defence_percent"] == 0.30)
check("Mare: 2 defence skills + 1 attack/stamina skill",
      mare["attack_flat"] > 0 and mare["stamina_flat"] > 0,
      (mare["attack_flat"], mare["stamina_flat"]))
check("Mare is Ultimate", CARDS["Mare"]["rarity"] == "Ultimate")

ct = CARDS["Cobra Titan"]["bonuses"]
check("Cobra Titan: 169 stamina", ct["stamina_flat"] == 169.0, ct["stamina_flat"])
check("Cobra Titan: 58 attack", ct["attack_flat"] == 58.0)
check("Cobra Titan: 120% stamina", ct["stamina_percent"] == 1.20)
check("Cobra Titan is Exclusive", CARDS["Cobra Titan"]["rarity"] == "Exclusive")
check("...one attack skill, two stamina/defence skills",
      ct["attack_flat"] > 0 and ct["stamina_flat"] > 0
      and ct["resistance_damage_percent"] > 0)

mf = CARDS["Miffy"]["bonuses"]
check("Miffy: 25% attack", mf["attack_percent"] == 0.25)
check("Miffy: 50% defence", mf["defence_percent"] == 0.50)
check("Miffy is Ultimate", CARDS["Miffy"]["rarity"] == "Ultimate")
# "+bey defense": defence_percent is applied to base+parts inside
# loadout.effective_blade, so it already scales off the bey's own Defence.
b = AvatarBonuses(defence_percent=0.50)
check("...and the percent really is of the BEY's defence",
      b.apply_defence_bonus(200) == 300, b.apply_defence_bonus(200))

print("\n── 4. the MLBB rarity is registered everywhere ──────────────────")
check("MLBB is in the shared RARITY_ORDER", "MLBB" in RARITY_ORDER)
check("...at the top", RARITY_ORDER[-1] == "MLBB", RARITY_ORDER[-1])
check("MLBB has a colour", "MLBB" in RARITY_COLORS)
check("MLBB has an emoji", "MLBB" in RARITY_EMOJI)
check("the shop's own RARITY_ORDER knows it too", "MLBB" in SHOP.RARITY_ORDER)
check("...so rarity_rank does not return -1",
      SHOP.rarity_rank("MLBB") == len(SHOP.RARITY_ORDER) - 1,
      SHOP.rarity_rank("MLBB"))
check("MLBB has a duplicate refund rate", SHOP.DUPE_REFUND_RATE.get("MLBB"))

mlbb_cards = [a for a in avatar_engine.get_all_avatars() if a["rarity"] == "MLBB"]
check("exactly four MLBB avatars", len(mlbb_cards) == 4,
      [a["name"] for a in mlbb_cards])
check("they are the four named",
      sorted(a["name"] for a in mlbb_cards)
      == ["Eudora", "Freya", "Helcurt", "Vexana"],
      sorted(a["name"] for a in mlbb_cards))

print("\n── 5. the pack costs 15M and is fully wired ─────────────────────")
check("price is 15,000,000", SHOP.PACK_PRICE["mlbb"] == 15_000_000,
      SHOP.PACK_PRICE["mlbb"])
for table, label in ((SHOP.PACK_POOL, "POOL"), (SHOP.PACK_GUARANTEE, "GUARANTEE"),
                     (SHOP.PACK_PRICE, "PRICE"), (SHOP.PACK_DISPLAY, "DISPLAY"),
                     (SHOP.PACK_EMOJI, "EMOJI"),
                     (SHOP.PACK_RARITY_WEIGHT, "RARITY_WEIGHT")):
    check(f"mlbb is in PACK_{label}", "mlbb" in table, list(table))
check("every pack appears in every table — none half-registered",
      all(set(SHOP.PACK_PRICE) == set(t) for t in
          (SHOP.PACK_POOL, SHOP.PACK_GUARANTEE, SHOP.PACK_DISPLAY,
           SHOP.PACK_EMOJI, SHOP.PACK_RARITY_WEIGHT)),
      {k: sorted(set(SHOP.PACK_PRICE) ^ set(v)) for k, v in
       (("pool", SHOP.PACK_POOL), ("guar", SHOP.PACK_GUARANTEE),
        ("disp", SHOP.PACK_DISPLAY), ("emoji", SHOP.PACK_EMOJI),
        ("wt", SHOP.PACK_RARITY_WEIGHT))})
check("slot 1 is a guaranteed MLBB", SHOP.PACK_GUARANTEE["mlbb"] == "MLBB")

print("\n── 6. the banner is CLOSED, both ways ───────────────────────────")
by_rarity: dict = {}
for a in avatar_engine.get_all_avatars():
    by_rarity.setdefault(a["rarity"], []).append(a)

random.seed(3)
pulled = set()
for _ in range(3000):
    got = SHOP._pull_from_pool("mlbb", SHOP.PACK_POOL["mlbb"], by_rarity)
    if got:
        pulled.add(got["rarity"])
check("3000 mlbb pulls produce ONLY MLBB", pulled == {"MLBB"}, pulled)

leaked = []
for pack in ("common", "rare", "epic", "legendary"):
    for _ in range(3000):
        got = SHOP._pull_from_pool(pack, SHOP.PACK_POOL[pack], by_rarity)
        if got and got["rarity"] == "MLBB":
            leaked.append(pack)
            break
check("no other pack can EVER roll an MLBB avatar", not leaked, leaked)
check("...because MLBB is not in their pools",
      all("MLBB" not in SHOP.PACK_POOL[p]
          for p in ("common", "rare", "epic", "legendary")))

check("the guaranteed slot also only gives MLBB",
      all(SHOP._pull_from_pool("mlbb", SHOP.PACK_POOL["mlbb"], by_rarity,
                               exact_rarity="MLBB")["rarity"] == "MLBB"
          for _ in range(200)))

print("\n── 7. images ────────────────────────────────────────────────────")
for name in NEW:
    img = CARDS[name].get("image") or ""
    if name == "Mare":
        # The link supplied for Mare was a discord.com/channels/... message
        # URL, not a CDN image URL. Stored empty rather than broken: an empty
        # image shows no art, a bad one shows a dead embed.
        check("Mare has no image — the supplied link was not an image URL",
              img == "", img[:60])
    else:
        check(f"{name} has a CDN image",
              img.startswith("https://cdn.discordapp.com/attachments/"), img[:60])
    check(f"{name}'s image is not a channel/message link",
          "discord.com/channels" not in img, img[:60])

print("\n── 8. nothing already in the game moved ─────────────────────────")
check("the roster grew by exactly seven", len(CARDS) == 36, len(CARDS))
for old in ("Argus", "Dyrroth", "Omega Prime"):
    check(f"{old} is still present", old in CARDS)
check("Argus still has its 3 skills", len(CARDS["Argus"]["skills"]) == 3)
check("the four original pack prices are unchanged",
      [SHOP.PACK_PRICE[p] for p in ("common", "rare", "epic", "legendary")]
      == [75_000, 150_000, 275_000, 500_000])
check("dodge on every new card respects the 5% cap",
      all(CARDS[n]["bonuses"].get("dodge_chance", 0) <= AvatarBonuses.DODGE_CAP
          for n in NEW),
      {n: CARDS[n]["bonuses"].get("dodge_chance") for n in NEW})

# Every card must still survive the battle path.
broken = []
for a in avatar_engine.get_all_avatars():
    try:
        AvatarBonuses(**{k: v for k, v in a["bonuses"].items()
                         if k in AvatarBonuses.__dataclass_fields__})
    except Exception as exc:                             # noqa: BLE001
        broken.append((a["name"], repr(exc)[:60]))
check("every card builds an AvatarBonuses", not broken, broken[:4])

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
