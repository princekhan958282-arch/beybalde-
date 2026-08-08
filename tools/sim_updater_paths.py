#!/usr/bin/env python3
"""
tools/sim_updater_paths.py — what the auto-updater is and isn't allowed to replace.

This exists because of a silent failure that is very hard to spot from the
outside: a blade balance change was committed, merged, and deployed, and the
running bot still showed the old numbers forever. `data/` was blanket-protected
to stop player saves being overwritten, and `data/beyblades.json` — the entire
blade roster — sits in the same directory. Content shipped; content never
arrived.

The two rules this file guards are opposites, and getting either wrong is bad in
a different way:

  * a PLAYER-STATE file that becomes updatable destroys real progress
  * a CONTENT file that stays protected makes every future balance change a
    no-op on live bots, with nothing in any log to say so

Run:  python3 tools/sim_updater_paths.py
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


from utils import updater as U                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

print("\n── 1. player state stays protected ──────────────────────────────")
PLAYER_STATE = [
    "data/users.json",
    "data/users.db",
    "data/avatar_inventory.json",
    "data/casino_wallets.json",
    "data/spawn_state.json",
    "data/config.json",
    "data/tournaments.db",
    "data/pve.db",
    ".env",
    "config_local.py",
]
for rel in PLAYER_STATE:
    check(f"{rel} is protected", U._is_protected(rel))

check("an unknown future file in data/ is protected by default",
      U._is_protected("data/something_new_next_year.json"))
check("a nested path under data/ is protected",
      U._is_protected("data/backups/users.json"))

print("\n── 2. authored content is updatable ─────────────────────────────")
check("data/beyblades.json is NOT protected",
      not U._is_protected("data/beyblades.json"))
check("...and it passes the suffix allowlist too",
      "data/beyblades.json".endswith(U.ALLOWED_SUFFIXES))
for rel in ("app.py", "cogs/battle/damage_rules.py", "utils/updater.py",
            "INSTALL.md", "cogs/avatar/avatar_data.json"):
    check(f"{rel} is updatable", not U._is_protected(rel))

print("\n── 3. the exception cannot widen by accident ────────────────────")
check("DATA_CONTENT is exact-match only — no trailing slash entries",
      not any(p.endswith("/") for p in U.DATA_CONTENT), U.DATA_CONTENT)
check("no wildcard characters", not any(
    any(c in p for c in "*?[") for p in U.DATA_CONTENT), U.DATA_CONTENT)
check("every entry lives under data/",
      all(p.startswith("data/") for p in U.DATA_CONTENT), U.DATA_CONTENT)
check("a near-miss name is still protected",
      U._is_protected("data/beyblades.json.bak")
      and U._is_protected("data/beyblades.json/users.json"))
check("a path that merely CONTAINS the name is still protected",
      U._is_protected("data/old/data/beyblades.json"))
check("case variations do not slip through",
      U._is_protected("data/BeyBlades.json"))

print("\n── 4. every exempted file must be read-only at runtime ──────────")
# The rule for adding to DATA_CONTENT: no runtime write path. Enforced here
# rather than trusted, because the cost of being wrong is lost player data.
WRITE_MARKERS = ("_atomic_write_json", "json.dump", "put_one", ".write(")
for rel in U.DATA_CONTENT:
    const = None
    dbsrc = open(os.path.join(ROOT, "utils", "database.py"), encoding="utf-8").read()
    for line in dbsrc.splitlines():
        if rel.split("/")[-1] in line and "_PATH" in line and "=" in line:
            const = line.split("=")[0].strip()
            break
    check(f"{rel} has a PATH constant in database.py", bool(const), const)
    if not const:
        continue
    writes = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "__pycache__", "tools", ".update_backup")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            for ln, line in enumerate(open(p, encoding="utf-8",
                                           errors="ignore").read().splitlines(), 1):
                if const in line and any(m in line for m in WRITE_MARKERS):
                    writes.append(f"{os.path.relpath(p, ROOT)}:{ln}")
    check(f"{rel} is never written at runtime", not writes, writes)

print("\n── 5. the file the bug was about ────────────────────────────────")
path = os.path.join(ROOT, "data", "beyblades.json")
check("data/beyblades.json exists", os.path.exists(path))
import json                                            # noqa: E402
blades = json.load(open(path, encoding="utf-8"))
check("it holds the full roster", len(blades) >= 78, len(blades))
sdk = blades.get("Shadow Dragon King")
check("Shadow Dragon King is present", sdk is not None)
dw = [a for a in (sdk or {}).get("abilities", []) if a["name"] == "Dark Weather"]
check("Dark Weather is there", bool(dw))
check("...carrying the new 25% special bonus",
      dw and dw[0]["special_damage_bonus_pct"] == 0.25,
      dw[0]["special_damage_bonus_pct"] if dw else None)
check("this file would now ship in an update",
      not U._is_protected("data/beyblades.json"))

print(f"\n{'=' * 66}\n  {PASS} passed, {FAIL} failed\n{'=' * 66}")
sys.exit(1 if FAIL else 0)
