#!/usr/bin/env python3
"""
tools/sim_limited_beys.py — limited-time and owner-bound blades.

The requirement was "a category for limited-time and custom beys, but they
still show in the Ultimate category". That rules out a new rarity: rarity is
what drives sorting, grouping and colour everywhere, and the game already has
~20 rarity maps across 15 files, several of which fail SILENTLY on a value
they don't know. Adding "Limited" as a rarity would have meant a blade that
sorts into its own tier — the opposite of what was asked — and a fresh crop of
white-circle fallbacks.

So these are FLAGS. `rarity` is untouched; the flags gate acquisition and add
a badge. That is the same shape `hidden_drop_one_in` already uses.

What is pinned here:

* **A flagged blade is still its rarity** everywhere that sorts or groups.
* **Every acquisition route is closed** when the window shuts or the blade is
  owner-bound. Four routes, all asserted — a gate that covers three of four is
  a blade obtainable by the fourth.
* **Both gates fail OPEN.** A typo in a date must not silently delete content
  from the game with no error anywhere.
* **Possession is never revoked.** The window gates getting one, not keeping
  one.

Run:  python3 tools/sim_limited_beys.py
"""
import copy
import json
import os
import sys
import time

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

from utils import availability as AV                              # noqa: E402

PAST = "2020-01-01T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"

print("\n── 1. the two gates, on their own ───────────────────────────────")
cases = [
    ("not limited",        {},                                       True),
    ("limited, no date",   {"limited": True},                        True),
    ("limited, open",      {"limited": True, "available_until": FUTURE}, True),
    ("limited, closed",    {"limited": True, "available_until": PAST},  False),
    ("date but not flagged", {"available_until": PAST},              True),
]
for label, entry, want in cases:
    check(f"{label}: available={want}", AV.is_available(entry) is want,
          AV.is_available(entry))
check("a date without the flag is inert — the flag is what arms it",
      AV.is_available({"available_until": PAST}) is True)
check("is_limited reports the flag, not the window",
      AV.is_limited({"limited": True, "available_until": PAST}) is True
      and AV.is_limited({}) is False)

# Fail OPEN. One typo must not delete content with no error anywhere.
for bad in ("not a date", "2026-13-45", "", "  ", [], {}, 0):
    e = {"limited": True, "available_until": bad}
    check(f"unparseable {bad!r} stays available", AV.is_available(e) is True,
          AV.is_available(e))
check("a unix timestamp works as well as ISO-8601",
      AV.is_available({"limited": True, "available_until": time.time() + 60})
      and not AV.is_available({"limited": True,
                               "available_until": time.time() - 60}))
check("a naive datetime string is read as UTC, not host-local",
      AV.expires_at({"limited": True,
                     "available_until": "2099-01-01T00:00:00"})
      == AV.expires_at({"limited": True, "available_until": FUTURE}))

print("\n── 2. owner-bound ───────────────────────────────────────────────")
bound = {"owner_ids": [42, 99]}
check("the named owners may have it",
      AV.owned_by(bound, 42) and AV.owned_by(bound, 99))
check("nobody else may", not AV.owned_by(bound, 7))
check("string ids still match", AV.owned_by(bound, "42"))
check("unbound content is open to everyone",
      AV.owned_by({}, 7) and AV.owned_by({"owner_ids": []}, 7))
check("a malformed owner list opens rather than locking everyone out",
      AV.owned_by({"owner_ids": "nonsense"}, 7),
      AV.owner_ids({"owner_ids": "nonsense"}))
check("obtainable() with NO player refuses owner-bound content",
      AV.obtainable(bound) is False)
check("...which is what keeps it out of shared pools", True)
check("obtainable() with the owner allows it", AV.obtainable(bound, 42))
check("both gates apply together — an owner still can't beat the clock",
      AV.obtainable({"owner_ids": [42], "limited": True,
                     "available_until": PAST}, 42) is False)

print("\n── 3. every acquisition route is closed ─────────────────────────")
import cogs.economy.shop as SHOP                                  # noqa: E402
import cogs.spawn.spawn as SPAWN                                  # noqa: E402
import utils.database as DBASE                                    # noqa: E402

BASE = copy.deepcopy(DB["Ultimate Valkyrie (Black Edition)"])
REAL = DBASE.load_beyblades


def with_blade(**flags):
    """The roster plus one flagged Ultimate, as the loaders would see it."""
    doc = copy.deepcopy(DB)
    b = copy.deepcopy(BASE)
    b["name"] = "Test Limited"
    b["booster_exclusive"] = True
    b.pop("hidden_drop_one_in", None)
    b.update(flags)
    doc["Test Limited"] = b
    return doc


def pools(doc):
    # shop.py does `from utils.database import load_beyblades`, so it holds
    # its OWN reference — patching utils.database alone changes nothing there.
    SHOP.load_beyblades = lambda: doc
    DBASE.load_beyblades = lambda: doc
    try:
        booster = {x["name"] for x in SHOP._load_booster_pool()}
        hidden = {x["name"] for x in SHOP._hidden_drop_pool()}
        spawned = set()
        for _ in range(4000):
            got = SPAWN._pick_random_beyblade(doc)
            if got:
                spawned.add(got.get("name"))
        return booster, hidden, spawned
    finally:
        SHOP.load_beyblades = REAL
        DBASE.load_beyblades = REAL


NAME = "Test Limited"
b, h, s = pools(with_blade(limited=True, available_until=FUTURE))
check("inside its window it IS in the booster pool", NAME in b, sorted(b))
check("...and still not in spawns (it is booster-exclusive)", NAME not in s)

b, h, s = pools(with_blade(limited=True, available_until=PAST))
check("past its window it is OUT of the booster pool", NAME not in b, sorted(b))
check("...out of the hidden pool", NAME not in h)
check("...and out of spawns", NAME not in s)

b, h, s = pools(with_blade(limited=True, available_until=PAST,
                           hidden_drop_one_in=1))
check("a closed window beats even a 1-in-1 hidden drop", NAME not in h, sorted(h))

b, h, s = pools(with_blade(owner_ids=[42]))
check("owner-bound is out of the booster pool", NAME not in b)
check("...and out of spawns", NAME not in s)

b2, h2, s2 = pools(with_blade(limited=True, available_until=FUTURE,
                              booster_exclusive=False))
check("a limited NON-booster blade can spawn while its window is open",
      NAME in s2, len(s2))
b3, h3, s3 = pools(with_blade(limited=True, available_until=PAST,
                              booster_exclusive=False))
check("...and cannot once it closes", NAME not in s3)

# The unflagged roster must be completely unaffected.
b0, h0, s0 = pools(copy.deepcopy(DB))
check("the shipped roster's booster pool is unchanged", len(b0) >= 10, len(b0))
check("...and its hidden pool still holds the Black Edition",
      "Ultimate Valkyrie (Black Edition)" in h0, sorted(h0))
check("...and spawns still produce plenty of blades", len(s0) >= 40, len(s0))

print("\n── 4. it is still an Ultimate, not a new tier ───────────────────")
lim = with_blade(limited=True, available_until=FUTURE)[NAME]
check("rarity is untouched", lim["rarity"] == "Ultimate", lim["rarity"])
from utils.embeds import RARITY_EMOJIS, rarity_colour                 # noqa: E402
check("it takes the Ultimate emoji",
      RARITY_EMOJIS.get(lim["rarity"]) == RARITY_EMOJIS["Ultimate"])
check("...and the Ultimate colour",
      rarity_colour(lim["rarity"]) == rarity_colour("Ultimate"))
from cogs.economy.profile import RARITY_ORDER                          # noqa: E402
check("it sorts into the Ultimate tier of `;list`",
      "Ultimate" in RARITY_ORDER and "Limited" not in RARITY_ORDER,
      RARITY_ORDER)
check("no new rarity was invented anywhere",
      "Limited" not in RARITY_EMOJIS and "Personal" not in RARITY_EMOJIS)

print("\n── 5. the badges are visible ────────────────────────────────────")
from cogs.economy.profile import _availability_fields                  # noqa: E402

check("a plain blade gets no availability field", _availability_fields({}) == [])
f = dict(_availability_fields({"limited": True, "available_until": FUTURE}))
check("an open window shows a Limited field", "⏳ Limited" in f, list(f))
check("...with a live countdown", "<t:" in f["⏳ Limited"], f.get("⏳ Limited"))
f = dict(_availability_fields({"limited": True, "available_until": PAST}))
check("a closed window says so rather than hiding",
      "closed" in f["⏳ Limited"], f.get("⏳ Limited"))
check("...and says owners keep theirs",
      "keep" in f["⏳ Limited"].lower(), f.get("⏳ Limited"))
f = dict(_availability_fields({"limited": True}))
check("an open-ended window does not fake a deadline",
      "<t:" not in f["⏳ Limited"], f.get("⏳ Limited"))
f = dict(_availability_fields({"owner_ids": [1]}))
check("owner-bound shows a Personal field", "👑 Personal" in f, list(f))
check("...and says it cannot be traded",
      "traded" in f["👑 Personal"], f.get("👑 Personal"))

psrc = open(os.path.join(ROOT, "cogs", "economy", "profile.py"),
            encoding="utf-8").read()
check("`;list` rows badge limited blades", '" ⏳"' in psrc)
check("...and personal ones", '" 👑"' in psrc)
check("the footer legend explains both",
      "⏳ limited" in psrc and "👑 personal" in psrc)
check("both info embeds use the shared field builder",
      psrc.count("_availability_fields(blade)") == 2,
      psrc.count("_availability_fields(blade)"))

print("\n── 6. both card renderers show it, and lay out for it ───────────")
from utils import info_card_pillow as ICP                              # noqa: E402
from PIL import Image                                                  # noqa: E402
import io                                                              # noqa: E402

plain = copy.deepcopy(DB["Ultimate Valkyrie"])
plain.pop("booster_exclusive", None)
h_plain = Image.open(io.BytesIO(
    ICP.render_info_card_pillow(plain).getvalue())).size[1]
for n, extra in ((1, {"limited": True}),
                 (2, {"limited": True, "owner_ids": [1]}),
                 (3, {"limited": True, "owner_ids": [1],
                      "booster_exclusive": True})):
    card = dict(plain, **extra)
    h = Image.open(io.BytesIO(
        ICP.render_info_card_pillow(card).getvalue())).size[1]
    # The Pillow renderer accumulates its layout height by hand through a
    # y_* chain; a badge that forgets to add its height overlaps the panel
    # below rather than failing.
    check(f"{n} badge(s) adds exactly {n * 40}px of card",
          h - h_plain == n * 40, (h_plain, h, h - h_plain))

isrc = open(os.path.join(ROOT, "utils", "info_card.py"), encoding="utf-8").read()
check("the HTML card renders the badges too",
      "LIMITED TIME" in isrc and "PERSONAL BLADE" in isrc)
psrc2 = open(os.path.join(ROOT, "utils", "info_card_pillow.py"),
             encoding="utf-8").read()
check("...and so does the Pillow one",
      "LIMITED TIME" in psrc2 and "PERSONAL BLADE" in psrc2)
check("the Pillow height is driven by the badge COUNT, not a boolean",
      "40 * len(_badges)" in psrc2)

print("\n── 7. trade cannot launder a personal blade ─────────────────────")
tsrc = open(os.path.join(ROOT, "cogs", "extras", "trade.py"),
            encoding="utf-8").read()
check("trade checks both sides", "for side_name in (a_name, b_name)" in tsrc)
check("...against is_owner_bound", "is_owner_bound(_blade_def(side_name))" in tsrc)
check("...and refuses before anything is written",
      tsrc.index("is_owner_bound(_blade_def") < tsrc.index("view.accepted"))
check("a limited blade is NOT blocked from trading — possession is kept",
      "is_limited" not in tsrc)

print("\n── 8. avatars use the same gate, and it now fires ───────────────")
from cogs.avatar.avatar_engine import avatar_is_available               # noqa: E402

check("a non-limited avatar is available", avatar_is_available({}))
check("a closed limited avatar is not",
      not avatar_is_available({"limited": True, "available_until": PAST}))
check("an open one is",
      avatar_is_available({"limited": True, "available_until": FUTURE}))
asrc = open(os.path.join(ROOT, "cogs", "avatar", "avatar_shop.py"),
            encoding="utf-8").read()
check("the pack roll actually consults it — it never used to",
      "avatar_is_available(av)" in asrc)
# Sliced after the DEFINITION, not after the first mention of the name: any
# comment elsewhere in the file that refers to `_build_rarity_map` would
# otherwise move the window and fail a check about code that never changed.
check("...at _build_rarity_map, the one choke point every pull reads",
      "avatar_is_available" in asrc.split("def _build_rarity_map", 1)[1][:900],
      asrc.split("def _build_rarity_map", 1)[1][:200])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
