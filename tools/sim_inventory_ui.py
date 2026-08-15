#!/usr/bin/env python3
"""
tools/sim_inventory_ui.py — the inventory panel at 2,000 items.

The panel was built for the six beys most players have. At the new 2,000-slot
ceiling, 6 items a page with prev/next only is **334 pages**, and the last one
is 333 taps from the first. This drives the real `InventoryView` — its actual
paging, its actual component tree — against a full collection.

Discord's limits are not advisory, and every one of them is asserted here
rather than assumed: 5 component rows, 5 buttons a row, 25 options a select,
4,096 characters of embed description, 256 of title. A view that exceeds any
of them raises at send time, which on a 2,000-item inventory means the panel
simply never opens.

The subtle one is `_clamp_page`. Filtering 334 pages down to 2 while sitting
on page 300 renders an empty list with both nav buttons disabled — a panel
with no way out. That is tested at every tab and filter combination.

Run:  python3 tools/sim_inventory_ui.py
"""
import os
import random
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


import discord                                                     # noqa: E402
import utils.database as DB                                        # noqa: E402
import utils.inventory as INV                                      # noqa: E402
import cogs.ui.inventory_ui as UI                                  # noqa: E402

ROSTER = list(DB.load_beyblades())

# Discord's documented ceilings — the numbers this file exists to stay under.
MAX_ROWS, MAX_BTN_PER_ROW = 5, 5
MAX_OPTS, MAX_DESC, MAX_TITLE = 25, 4096, 256
# Per-option label/value/description, and the select's placeholder. A select
# option's value has a MINIMUM of 1 — an empty string is a 400, not a default.
MAX_FIELD, MAX_PLACEHOLDER = 100, 150


class FakeMember:
    def __init__(self, uid, name="Tester"):
        self.id = uid
        self.display_name = name


def install(uid, n_beys, n_parts=0, n_listed=0, bought=0):
    """Put a synthetic profile in front of the view, deterministically."""
    rng = random.Random(uid)
    prof = {
        "inventory": [rng.choice(ROSTER) for _ in range(n_beys)],
        "marketplace_listings": [{"bey_name": ROSTER[0], "price": 1}
                                 for _ in range(n_listed)],
        "parts": [],
        "equipped_parts": [],
        "active_beyblade": None,
        "coins": 0,
        INV.K_EXTRA_SLOTS: bought,
    }
    if n_parts:
        from cogs.economy.shop import PARTS_CATALOG
        prof["parts"] = [PARTS_CATALOG[i % len(PARTS_CATALOG)]["name"]
                         for i in range(n_parts)]
    UI.get_user = lambda _uid, _p=prof: _p
    DB.get_avatar_inventory = lambda _uid: []
    UI.get_avatar_inventory = lambda _uid: []
    UI.get_equipped_avatar = lambda _uid: None
    return prof


def view(uid=1, **kw):
    install(uid, **kw)
    me = FakeMember(uid)
    return UI.InventoryView(me, me)


def rows(v):
    """row index -> the components on it."""
    out = {}
    for c in v.children:
        out.setdefault(c.row, []).append(c)
    return out


print("\n── 1. the shape of the fix ──────────────────────────────────────")
check("beys get a bigger page than the phone default",
      UI.PAGE_SIZE["bey"] > UI.ITEMS_PER_PAGE,
      (UI.PAGE_SIZE["bey"], UI.ITEMS_PER_PAGE))
check("...and so does the mixed tab", UI.PAGE_SIZE["all"] > UI.ITEMS_PER_PAGE)
check("a page still fits a select", UI.PAGE_SIZE["bey"] <= MAX_OPTS)
check("avatars and parts keep the small page",
      UI.PAGE_SIZE.get("avatar", UI.ITEMS_PER_PAGE) == UI.ITEMS_PER_PAGE and
      UI.PAGE_SIZE.get("part", UI.ITEMS_PER_PAGE) == UI.ITEMS_PER_PAGE)

t = time.perf_counter()
v = view(uid=7, n_beys=2000)
build_ms = (time.perf_counter() - t) * 1000
check("a 2,000-bey panel opens in under 250 ms", build_ms < 250, f"{build_ms:.0f} ms")
check("...with every bey loaded", len(v._cache["bey"]) == 2000,
      len(v._cache["bey"]))
check("2,000 beys is 167 pages, not 334",
      v._pages() == 167, v._pages())

print("\n── 2. Discord's limits, at 2,000 items ─────────────────────────")


def audit(v, label):
    r = rows(v)
    check(f"{label}: at most {MAX_ROWS} rows",
          len(r) <= MAX_ROWS and max(r, default=0) < MAX_ROWS, sorted(r))
    for idx, comps in sorted(r.items()):
        sels = [c for c in comps if isinstance(c, discord.ui.Select)]
        btns = [c for c in comps if isinstance(c, discord.ui.Button)]
        check(f"{label}: row {idx} is one select OR ≤5 buttons",
              (len(sels) == 1 and not btns) or
              (not sels and len(btns) <= MAX_BTN_PER_ROW),
              f"{len(sels)} select(s), {len(btns)} button(s)")
        for s in sels:
            check(f"{label}: row {idx} select has 1–{MAX_OPTS} options",
                  1 <= len(s.options) <= MAX_OPTS, len(s.options))
            vals = [o.value for o in s.options]
            check(f"{label}: row {idx} option values are unique",
                  len(set(vals)) == len(vals), vals)
            check(f"{label}: row {idx} placeholder ≤{MAX_PLACEHOLDER}",
                  len(s.placeholder or "") <= MAX_PLACEHOLDER, s.placeholder)
            # At most one option may be pre-selected in a single-choice select.
            ndef = sum(1 for o in s.options if o.default)
            check(f"{label}: row {idx} has at most one default", ndef <= 1, ndef)
            # Per-OPTION field lengths. This block is the one that was missing,
            # and its absence is why `;inv` shipped broken: the "All rarities"
            # option carried `value=""`, Discord rejects the whole message with
            # a 400 at SEND time, and the panel simply never opened for anyone
            # holding two or more rarities — 75% of the players who own beys.
            #
            # Counting options and checking they are unique is not the same as
            # checking each one is legal. Assert the documented contract, not
            # the parts of it that came to mind.
            for i, o in enumerate(s.options):
                check(f"{label}: row {idx} opt {i} value is 1–{MAX_FIELD}",
                      1 <= len(str(o.value)) <= MAX_FIELD,
                      f"len={len(str(o.value))} {o.value!r}")
                check(f"{label}: row {idx} opt {i} label is 1–{MAX_FIELD}",
                      1 <= len(str(o.label)) <= MAX_FIELD,
                      f"len={len(str(o.label))} {o.label!r}")
                check(f"{label}: row {idx} opt {i} description ≤{MAX_FIELD}",
                      len(str(o.description or "")) <= MAX_FIELD,
                      f"len={len(str(o.description or ''))}")
    e = v.build_embed()
    check(f"{label}: description under {MAX_DESC}",
          len(e.description or "") <= MAX_DESC, len(e.description or ""))
    check(f"{label}: title under {MAX_TITLE}",
          len(e.title or "") <= MAX_TITLE, len(e.title or ""))
    check(f"{label}: footer under 2048",
          len((e.footer.text if e.footer else "") or "") <= 2048)


audit(v, "2,000 beys, page 1")
v.page = v._pages() - 1
v._rebuild()
audit(v, "2,000 beys, last page")
v.page = 83
v._rebuild()
audit(v, "2,000 beys, mid")

print("\n── 3. the page jump ─────────────────────────────────────────────")
v.page = 0
v._rebuild()
jump = next(c for c in v.children if c.row == 4)
check("a jump select appears at 167 pages", isinstance(jump, discord.ui.Select))
check("...offering exactly 25 destinations", len(jump.options) == 25,
      len(jump.options))
targets = [int(o.value) for o in jump.options]
check("...starting at page 1", targets[0] == 0, targets[:3])
check("...ending at the LAST page", targets[-1] == v._pages() - 1, targets[-3:])
check("...in order", targets == sorted(targets))
check("...evenly spread, no clump", max(
    b - a for a, b in zip(targets, targets[1:])) <= 8,
    [b - a for a, b in zip(targets, targets[1:])])

# Wherever you are must be offered, or the select shows a page you are not on
# as "selected" and the panel lies about where you are.
for p in (0, 1, 7, 83, 99, 165, 166):
    v.page = p
    v._rebuild()
    j = next(c for c in v.children if c.row == 4)
    vals = [int(o.value) for o in j.options]
    marked = [int(o.value) for o in j.options if o.default]
    check(f"page {p} is offered and marked current",
          p in vals and marked == [p], (p in vals, marked))
    check(f"page {p}: still at most 25 options", len(j.options) <= MAX_OPTS,
          len(j.options))

small = view(uid=8, n_beys=30)
j = [c for c in small.children if c.row == 4]
check("30 beys is 3 pages, and all 3 are offered",
      len(j) == 1 and len(j[0].options) == 3, j and len(j[0].options))

tiny = view(uid=9, n_beys=5)
check("a single page shows no jump select at all",
      not [c for c in tiny.children if c.row == 4])
check("...and its nav buttons are all disabled",
      all(c.disabled for c in tiny.children if c.row == 2))

print("\n── 4. the rarity filter ─────────────────────────────────────────")
v = view(uid=7, n_beys=2000)
filt = next(c for c in v.children if c.row == 3)
check("a rarity filter appears on the bey tab",
      isinstance(filt, discord.ui.Select))
check("...offering at most 25 options", len(filt.options) <= MAX_OPTS,
      len(filt.options))
check("...with 'All rarities' first and selected",
      filt.options[0].value == UI.ALL_RARITIES and filt.options[0].default,
      filt.options[0].value)
check("...and every option carrying a count",
      all("(" in o.label for o in filt.options[1:]))

total = len(v._cache["bey"])
sum_counts = sum(n for _r, n in v._rarities())
check("the counts add up to the collection", sum_counts == total,
      (sum_counts, total))

order = list(UI.RARITY_EMOJIS)
present = [r for r, _n in v._rarities()]
check("rarities are listed in roster order, not alphabetical",
      present == sorted(present, key=order.index), present)

for r, n in v._rarities():
    v.rarity, v.page = r, 0
    v._rebuild()
    got = v._items()
    check(f"filtering to {r} shows exactly its {n:,}",
          len(got) == n and all(i["rarity"] == r for i in got), len(got))

v.rarity = None
v._rebuild()
check("clearing the filter restores the whole tab", len(v._items()) == total)

# Parts have no rarity — a filter there would be an empty dropdown.
parts_v = view(uid=10, n_beys=10, n_parts=8)
parts_v.category = "part"
parts_v._rebuild()
check("the parts tab has no rarity filter",
      not [c for c in parts_v.children if c.row == 3])

print("\n── 5. clamping: no dead panel ───────────────────────────────────")
v = view(uid=7, n_beys=2000)
v.page = 166
rare = next((r for r, n in v._rarities() if n <= 40), v._rarities()[0][0])
v.rarity = rare                      # filter WITHOUT resetting the page
v._clamp_page()
check(f"page 167 filtered to {rare} clamps into range",
      v.page < v._pages(), (v.page, v._pages()))
v._rebuild()
check("...and the page is not empty", len(v._page_items()) > 0)
check("...and at least one nav control is live",
      not all(c.disabled for c in v.children if c.row == 2)
      or v._pages() == 1)

# The callback resets to page 1 anyway — belt as well as braces.
import asyncio                                                     # noqa: E402


class FakeResponse:
    def __init__(self): self.edits = []

    async def edit_message(self, **kw): self.edits.append(kw)

    async def send_message(self, *a, **kw): pass


class FakeInteraction:
    def __init__(self, values, uid=7):
        self.data = {"values": list(values)}
        self.user = FakeMember(uid)
        self.response = FakeResponse()


print("\n── 4b. the empty-value 400 (regression) ─────────────────────────")
# `;inv` shipped in v95 with `value=""` on the "All rarities" option. Discord
# requires 1-100 characters and rejects the whole message with a 400 at SEND
# time, so the panel did not degrade — it never opened, for every player
# holding two or more rarities. 50 of the 67 who own beys.
v = view(uid=7, n_beys=2000)
filt = next(c for c in v.children if c.row == 3)
allopt = filt.options[0]
check("the 'all rarities' option has a non-empty value",
      len(str(allopt.value)) >= 1, repr(allopt.value))
check("...and it is the sentinel this codebase already uses elsewhere",
      allopt.value == UI.ALL_RARITIES == "__all__", allopt.value)
check("no option anywhere in the panel has an empty value",
      all(len(str(o.value)) >= 1
          for c in v.children if isinstance(c, discord.ui.Select)
          for o in c.options))
check("every rarity option carries a real emoji or none at all",
      all(o.emoji is None or str(o.emoji) for o in filt.options),
      [str(o.emoji) for o in filt.options])

# The sentinel must round-trip back to "no filter", or picking All would
# filter the list down to zero blades whose rarity is literally "__all__".
v.rarity, v.page = v._rarities()[0][0], 3
asyncio.run(v._rarity_cb(FakeInteraction([UI.ALL_RARITIES])))
check("picking 'all rarities' clears the filter", v.rarity is None)
check("...and shows the whole tab again",
      len(v._items()) == len(v._all_items()))
# A panel opened before the restart still has the old empty-valued option on
# screen. Pressing it must clear, not filter by nothing.
v.rarity = v._rarities()[0][0]
asyncio.run(v._rarity_cb(FakeInteraction([""])))
check("a stale empty value from an open panel still clears the filter",
      v.rarity is None)

v = view(uid=7, n_beys=2000)
v.page = 120
inter = FakeInteraction([rare])
asyncio.run(v._rarity_cb(inter))
check("the rarity callback returns you to page 1", v.page == 0)
# Every interaction edits the SAME message — a followup would leave a trail of
# panels down the channel.
check("...by editing the same message, not sending a new one",
      len(inter.response.edits) == 1
      and set(inter.response.edits[0]) == {"embed", "view"},
      inter.response.edits)

v = view(uid=7, n_beys=2000)
asyncio.run(v._jump_cb(FakeInteraction(["99"])))
check("the jump callback moves to the chosen page", v.page == 99)
asyncio.run(v._last_cb(FakeInteraction([])))
check("⏭ goes to the last page", v.page == v._pages() - 1)
asyncio.run(v._first_cb(FakeInteraction([])))
check("⏮ goes back to the first", v.page == 0)
asyncio.run(v._jump_cb(FakeInteraction(["not a number"])))
check("a junk jump value is ignored, not raised", v.page == 0)

# Switching tabs must drop the filter — "Ultimate" carried onto parts is an
# empty list nobody asked for.
v = view(uid=10, n_beys=200, n_parts=8)
v.rarity = "Ultimate"
asyncio.run(v._make_cat_cb("part")(FakeInteraction([])))
check("changing tab clears the rarity filter", v.rarity is None)
check("...and returns to page 1", v.page == 0)
check("...and the parts tab is not empty", len(v._items()) == 8, len(v._items()))

print("\n── 6. every tab, every filter, at size ─────────────────────────")
v = view(uid=11, n_beys=1500, n_parts=12)
for cat, _lbl in UI.CATEGORIES:
    v.category, v.rarity, v.page = cat, None, 0
    v._rebuild()
    audit(v, f"tab {cat}")
    opts = [(r, n) for r, n in v._rarities()] if cat in UI.RARITY_TABS else []
    for r, _n in opts[:3]:
        v.rarity, v.page = r, 0
        v._rebuild()
        audit(v, f"tab {cat} / {r}")
    v.rarity = None

print("\n── 7. the item select still points at the right item ────────────")
v = view(uid=7, n_beys=2000)
per = v._per_page()
for page in (0, 1, 55, 166):
    v.page = page
    v._rebuild()
    sel = next(c for c in v.children if c.row == 1)
    items = v._page_items()
    check(f"page {page}: the select lists exactly this page",
          len(sel.options) == len(items), (len(sel.options), len(items)))
    check(f"page {page}: option 1 is numbered {page * per + 1}",
          sel.options[0].label.startswith(f"{page * per + 1}."),
          sel.options[0].label)
    for i, o in enumerate(sel.options):
        check_name = items[i]["name"][:80]
        if not o.label.endswith(check_name):
            check(f"page {page}: option {i} names its item", False, o.label)
            break
    else:
        check(f"page {page}: every option names its item", True)

# Selecting resolves against the FILTERED page, not the raw inventory.
v.rarity, v.page = v._rarities()[-1][0], 0
v._rebuild()
want = v._page_items()[0]
asyncio.run(v._select_cb(FakeInteraction(["0"])))
check("selecting under a filter opens the item that was shown",
      v.detail is not None and v.detail["name"] == want["name"],
      v.detail and v.detail["name"])

print("\n── 8. the footer tells you where you stand ─────────────────────")
v = view(uid=12, n_beys=1500, n_listed=20, bought=1400)
foot = (v.build_embed().footer.text or "")
check("the slot count is on the bey tab", "slots" in foot, foot)
check("...counting listings as used", "1,520" in foot, foot)
check("...against the bought capacity", "1,600" in foot, foot)
v.rarity = v._rarities()[0][0]
v.page = 0
foot = (v.build_embed().footer.text or "")
check("a filtered footer says how many of how many", " of " in foot, foot)
check("...and names the filter", v.rarity in foot, foot)

v.category = "part"
v.rarity = None
v._rebuild()
check("the parts tab does not claim bey slots",
      "slots" not in (v.build_embed().footer.text or ""))

print("\n── 9. an empty collection still renders ────────────────────────")
v = view(uid=13, n_beys=0)
e = v.build_embed()
check("zero beys renders one page", v._pages() == 1)
check("...with a message rather than a blank", "Nothing here" in e.description,
      e.description)
check("...and no select to pick from",
      not [c for c in v.children if c.row == 1])
audit(v, "empty")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
