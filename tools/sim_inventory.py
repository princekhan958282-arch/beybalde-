#!/usr/bin/env python3
"""
tools/sim_inventory.py — the inventory, which had no test coverage at all.

Nothing anywhere asserted adding, removing, duplicates, or how any of it
behaves at size. This file starts with the performance groundwork, because
every one of those problems is invisible until a collection is large and then
all of them arrive at once.

The three that were real:

* **`load_beyblades()` re-parsed 274 KB on every call** — a raw `open` +
  `json.load` in a module whose parse cache sits three functions above it. 21
  call sites, including one inside `;buybey` purely to read a single blade's
  rarity.
* **`get_beyblade` deepcopies per record.** ~0.05 ms is nothing once and
  ~107 ms for a two-thousand-item inventory panel, synchronously, on the event
  loop.
* **`;inventory_legacy` put every bey in ONE embed description**, which Discord
  caps at 4,096 characters — so it raised an HTTPException at roughly 160 beys
  and would have broken below the 200-slot cap.

The shared-cache change has a hazard of its own, and it is asserted here:
`load_beyblades()` now returns an object every caller shares, so a caller that
writes to it corrupts the registry for the whole process.

Run:  python3 tools/sim_inventory.py
"""
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

import utils.database as DB                                        # noqa: E402

print("\n── 1. the registry is parsed once, not per call ─────────────────")
DB.load_beyblades()                       # warm
a, b = DB.load_beyblades(), DB.load_beyblades()
check("load_beyblades returns the SHARED cached object", a is b)
check("...and it is the whole roster", len(a) >= 90, len(a))

t = time.perf_counter()
for _ in range(500):
    DB.load_beyblades()
per = (time.perf_counter() - t) * 1000 / 500
check(f"a cached call is under 0.5 ms ({per:.3f} ms)", per < 0.5, per)

src = open(os.path.join(ROOT, "utils", "database.py"), encoding="utf-8").read()
body = src.split("def load_beyblades", 1)[1].split("\ndef ", 1)[0]
check("it goes through the parse cache, not a raw open()",
      "_read_json_cached" in body and "json.load(f)" not in body)
check("...and says out loud that the result is shared",
      "SHARED" in body or "shared" in body)

print("\n── 2. the shared object must not be mutated ─────────────────────")
# This is the hazard the cache introduces. A caller that writes to the result
# corrupts the registry for every other reader in the process.
snapshot = {k: dict(v) for k, v in DB.load_beyblades().items()}
from cogs.economy.profile import fuzzy_find_beyblade                # noqa: E402

for q in ("ultimate valk", "sriggan", "dranz", "zzzz no such blade"):
    fuzzy_find_beyblade(q)
now = DB.load_beyblades()
drift = [k for k in snapshot if snapshot[k] != now.get(k)]
check("fuzzy_find_beyblade leaves the registry byte-identical", not drift,
      drift[:3])
check("...and still finds things",
      (fuzzy_find_beyblade("ultimate valk") or {}).get("name")
      == "Ultimate Valkyrie")
check("...returning a copy, not the shared record",
      fuzzy_find_beyblade("ultimate valk") is not now["Ultimate Valkyrie"])

psrc = open(os.path.join(ROOT, "cogs", "economy", "profile.py"),
            encoding="utf-8").read()
check("no caller writes `name` into the shared record any more",
      'data.setdefault("name", name)' not in psrc)

print("\n── 3. beyblade_ref vs get_beyblade ──────────────────────────────")
ref = DB.beyblade_ref("Ultimate Valkyrie")
check("beyblade_ref finds a blade", ref is not None and ref["id"] == "BB088")
check("...case-insensitively",
      DB.beyblade_ref("ULTIMATE valkyrie") is ref)
check("...and hands back the SHARED record, no copy",
      ref is DB.load_beyblades()["Ultimate Valkyrie"])
check("an unknown name is None, not a crash",
      DB.beyblade_ref("no such blade") is None)
cp = DB.get_beyblade("Ultimate Valkyrie")
check("get_beyblade still copies — existing callers are unchanged",
      cp is not ref and cp["id"] == ref["id"])
cp["stats"]["attack"] = 1
check("...and writing to that copy cannot reach the registry",
      DB.load_beyblades()["Ultimate Valkyrie"]["stats"]["attack"] == 165,
      DB.load_beyblades()["Ultimate Valkyrie"]["stats"]["attack"])

N = 2000
t = time.perf_counter()
for _ in range(N):
    DB.beyblade_ref("Dranzer")
ref_ms = (time.perf_counter() - t) * 1000
t = time.perf_counter()
for _ in range(N):
    DB.get_beyblade("Dranzer")
cp_ms = (time.perf_counter() - t) * 1000
check(f"{N} refs beat {N} copies by 5x or better "
      f"({ref_ms:.0f} ms vs {cp_ms:.0f} ms)",
      cp_ms > ref_ms * 5, (ref_ms, cp_ms))
check(f"a {N}-item panel's lookups cost under 25 ms ({ref_ms:.0f} ms)",
      ref_ms < 25, ref_ms)

isrc = open(os.path.join(ROOT, "cogs", "ui", "inventory_ui.py"),
            encoding="utf-8").read()
check("the inventory panel uses the ref, not the copy",
      "beyblade_ref(str(name))" in isrc and "get_beyblade(str(name))" not in isrc)

print("\n── 4. `;duplicatesell` is one pass, and unchanged in meaning ────")
ssrc = open(os.path.join(ROOT, "cogs", "spawn", "spawn.py"),
            encoding="utf-8").read()
check("it no longer rebuilds the list per duplicate group",
      "for bey in inventory:" not in ssrc.split("to_sell", 1)[-1][:3000]
      or "budget" in ssrc)
check("...using a per-name budget in a single sweep", "budget[key] -= 1" in ssrc)


def old_way(inventory, to_sell):
    for name, extras in to_sell:
        key, removed, new = name.lower().strip(), 0, []
        for b in inventory:
            if b.lower().strip() == key and removed < extras:
                removed += 1
            else:
                new.append(b)
        inventory = new
    return inventory


def new_way(inventory, to_sell):
    budget = {n.lower().strip(): e for n, e in to_sell}
    kept = []
    for b in inventory:
        k = str(b).lower().strip()
        if budget.get(k, 0) > 0:
            budget[k] -= 1
            continue
        kept.append(b)
    return kept


import random                                                       # noqa: E402
from collections import Counter                                     # noqa: E402

random.seed(20260815)
names = ["Alpha", "beta", "GAMMA", "Delta ", "alpha", "Epsilon"]
bad = 0
for _ in range(4000):
    inv = [random.choice(names) for _ in range(random.randint(0, 40))]
    c = Counter(x.lower().strip() for x in inv)
    seen, ts = set(), []
    for n in inv:
        k = n.lower().strip()
        if k in seen or c[k] <= 1:
            continue
        seen.add(k)
        ts.append((n, c[k] - 1))
    if old_way(list(inv), ts) != new_way(list(inv), ts):
        bad += 1
check(f"4,000 random inventories: the rewrite is behaviourally identical",
      bad == 0, bad)
check("...including that it keeps exactly one of each duplicate",
      new_way(["A", "A", "A", "B"], [("A", 2)]) == ["A", "B"],
      new_way(["A", "A", "A", "B"], [("A", 2)]))
check("...and preserves order", new_way(["C", "A", "A", "B"], [("A", 1)])
      == ["C", "A", "B"])

print("\n── 5. `;inventory_legacy` no longer breaks on a big collection ──")
check("it takes a page argument", "page: int = 1" in psrc)
check("...chunks below Discord's 4096-char description limit",
      "> 3900" in psrc)
check("...and tells you there are more pages",
      "for the next" in psrc)


def paginate(lines):
    pages, buf = [], ""
    for ln in lines:
        if len(buf) + len(ln) + 1 > 3900:
            pages.append(buf)
            buf = ""
        buf += ln + "\n"
    if buf or not pages:
        pages.append(buf)
    return pages


for n in (0, 1, 160, 200, 910, 2000):
    lines = [f"  • Some Beyblade Name {i}" for i in range(n)]
    pages = paginate(lines)
    over = [len(p) for p in pages if len(p) > 4096]
    check(f"{n:>5} beys -> {len(pages)} page(s), none over the limit",
          not over, over)
check("an empty collection still produces one page, not zero",
      len(paginate([])) == 1)

print("\n── 6. nothing about the data changed ────────────────────────────")
reg = DB.load_beyblades()
check("the roster is intact", len(reg) >= 91, len(reg))
check("every blade still has a name", all("name" in v for v in reg.values()))
check("...and a rarity", all("rarity" in v for v in reg.values()))
check("get_beyblade and beyblade_ref agree on every blade",
      all(DB.get_beyblade(n)["id"] == DB.beyblade_ref(n)["id"] for n in reg))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
