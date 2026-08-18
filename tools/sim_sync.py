#!/usr/bin/env python3
"""
tools/sim_sync.py — command registration, and why everything showed up twice.

The bug this suite exists for
-----------------------------
Every slash command in this bot was listed twice in Discord's picker, and it
was not a display glitch: the bot really had registered each one twice.

`;sync` ran `copy_global_to(guild)` then `sync(guild=...)`, writing a
guild-scoped COPY of every global command. Discord's picker is the union of the
global list and the guild list, so a command present in both is drawn twice.
Then `reconcile()` — the boot cleanup whose whole job is to fix drift — kept
those copies *on purpose*, deleting only names the bot no longer had, on the
theory that "valid mirrors" belonged to someone who wanted instant commands.

A valid mirror IS the duplicate. So the one documented deploy step created the
problem and the one automatic repair preserved it, and between them nothing in
the system could ever return to a single listing.

That is the shape worth testing: not "does sync work" but "after this runs, is
each command registered exactly once?"

Run:  python3 tools/sim_sync.py
"""
import asyncio
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


import discord                                          # noqa: E402

from utils import command_sync as CS                    # noqa: E402

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)


# ── the smallest fake tree that can hold this bug ────────────────────────────

class FakeCommand:
    def __init__(self, name, registry, guild_id=None):
        self.name = name
        self._registry = registry
        self._guild_id = guild_id
        self.deleted = False

    async def delete(self):
        self.deleted = True
        self._registry.remove(self)


class FakeTree:
    """Models the one thing that matters: global and guild lists are separate,
    and Discord serves the union of them."""

    def __init__(self, local_names, guild_copies=None, sync_error=None,
                 fetch_error=None):
        self.local = list(local_names)
        self.globals = list(local_names)
        # guild_id -> [FakeCommand]
        self.guild: dict[int, list] = {}
        for gid, names in (guild_copies or {}).items():
            self.guild[gid] = []
            self.guild[gid].extend(
                FakeCommand(n, self.guild[gid], gid) for n in names)
        self.sync_error = sync_error
        self.fetch_error = fetch_error
        self.copied_to = []

    def get_commands(self, guild=None):
        return [FakeCommand(n, []) for n in self.local]

    def copy_global_to(self, guild=None):
        self.copied_to.append(getattr(guild, "id", None))

    def clear_commands(self, guild=None):
        self.guild[guild.id] = []

    async def sync(self, guild=None):
        if self.sync_error:
            raise self.sync_error
        if guild is None:
            self.globals = list(self.local)
            return list(self.globals)
        if getattr(guild, "id", None) in [g for g in self.copied_to]:
            self.guild.setdefault(guild.id, [])
            lst = self.guild[guild.id]
            lst.clear()
            lst.extend(FakeCommand(n, lst, guild.id) for n in self.local)
        return list(self.guild.get(guild.id, []))

    async def fetch_commands(self, guild=None):
        if self.fetch_error:
            raise self.fetch_error
        return list(self.guild.get(getattr(guild, "id", None), []))

    # what a player actually sees
    def picker_lines(self, guild_id):
        return len(self.globals) + len(self.guild.get(guild_id, []))


class FakeGuild:
    def __init__(self, gid, name="Test"):
        self.id = gid
        self.name = name


class FakeBot:
    def __init__(self, tree, guilds):
        self.tree = tree
        self.guilds = guilds


NAMES = ["admin", "player", "casino", "avatar", "story", "tournament"]


def fresh(guild_copies=None, **kw):
    tree = FakeTree(NAMES, guild_copies, **kw)
    g = FakeGuild(1)
    return tree, g, FakeBot(tree, [g])


def run(coro):
    return loop.run_until_complete(coro)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 1. the reported bug: every command listed twice ──────────────")

# The state a server is left in after the old `;sync`: a guild copy of every
# global, sitting alongside the globals.
tree, guild, bot = fresh({1: NAMES})
check("before: the picker draws every command twice",
      tree.picker_lines(1) == 2 * len(NAMES), tree.picker_lines(1))

report = run(CS.reconcile(bot))
check("reconcile registers the global list", report["synced"] == len(NAMES))
check("...and deletes the guild copies that duplicated it",
      len(report["pruned"].get(1, [])) == len(NAMES), report["pruned"])
check("after: each command is listed exactly once",
      tree.picker_lines(1) == len(NAMES), tree.picker_lines(1))
check("...and nothing was lost — the globals are all still there",
      sorted(tree.globals) == sorted(NAMES))


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 2. stale copies still go, as they always did ─────────────────")

tree, guild, bot = fresh({1: NAMES + ["rankadmin", "codeadmin"]})
report = run(CS.reconcile(bot))
removed = report["pruned"].get(1, [])
check("a command the bot no longer has is deleted",
      "rankadmin" in removed and "codeadmin" in removed, removed)
check("...along with the duplicates, in one pass",
      len(removed) == len(NAMES) + 2, removed)
check("the guild is left holding nothing", tree.guild[1] == [])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 3. the mirror opt-in still works, and says what it costs ─────")

os.environ[CS._MIRROR] = "1"
try:
    check("mirroring() reads the flag", CS.mirroring() is True)
    tree, guild, bot = fresh({1: NAMES + ["rankadmin"]})
    report = run(CS.reconcile(bot))
    removed = report["pruned"].get(1, [])
    check("opted in, the mirrors are spared", removed == ["rankadmin"], removed)
    check("...but the stale copy still goes", "rankadmin" in removed)
    check("...and the report says mirroring was on", report["mirroring"] is True)
    check("the picker is doubled — that is the accepted cost",
          tree.picker_lines(1) == 2 * len(NAMES), tree.picker_lines(1))
finally:
    os.environ.pop(CS._MIRROR, None)

check("off by default", CS.mirroring() is False)
for value in ("0", "false", "no", "", "off"):
    os.environ[CS._MIRROR] = value
    check(f"...and {value!r} does not turn it on", CS.mirroring() is False)
os.environ.pop(CS._MIRROR, None)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 4. prune_guild's two modes ───────────────────────────────────")

tree, guild, bot = fresh({1: NAMES})
removed = run(CS.prune_guild(bot, guild))
check("no `keep` set spares nothing — every copy goes",
      len(removed) == len(NAMES), removed)

tree, guild, bot = fresh({1: NAMES + ["gone"]})
removed = run(CS.prune_guild(bot, guild, {"admin", "player", "casino",
                                          "avatar", "story", "tournament"}))
check("a `keep` set spares exactly those names", removed == ["gone"], removed)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 5. it never raises — this runs in front of the bot booting ───")

tree, guild, bot = fresh({1: NAMES}, sync_error=RuntimeError("429 rate limited"))
report = run(CS.reconcile(bot))
check("a failed global sync returns a report rather than raising",
      isinstance(report, dict) and report["synced"] == 0)
# Without a successful sync we don't know the real command set, and pruning
# against a guess would delete working commands.
check("...and prunes nothing, because the command set is unknown",
      report["pruned"] == {} and len(tree.guild[1]) == len(NAMES))

tree, guild, bot = fresh({1: NAMES}, fetch_error=discord.Forbidden.__new__(
    discord.Forbidden))
removed = run(CS.prune_guild(bot, guild))
check("a guild the bot cannot read is skipped, not fatal", removed == [])

tree, guild, bot = fresh({1: NAMES}, fetch_error=RuntimeError("boom"))
check("...and neither is any other fetch failure",
      run(CS.prune_guild(bot, guild)) == [])


class BadDelete(FakeCommand):
    async def delete(self):
        raise RuntimeError("500")


tree, guild, bot = fresh({1: []})
tree.guild[1] = []
tree.guild[1].extend([BadDelete("a", tree.guild[1]),
                      FakeCommand("b", tree.guild[1])])
removed = run(CS.prune_guild(bot, guild))
check("one undeletable command does not stop the rest", removed == ["b"], removed)


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 6. the per-boot guild cap still holds ────────────────────────")

many = {i: NAMES for i in range(40)}
tree = FakeTree(NAMES, many)
bot = FakeBot(tree, [FakeGuild(i) for i in range(40)])
CS.GUILD_DELAY = 0                                       # don't sleep in a test
report = run(CS.reconcile(bot))
check("no more than MAX_GUILDS_PER_BOOT are touched",
      len(report["pruned"]) <= CS.MAX_GUILDS_PER_BOOT, len(report["pruned"]))
check("...and the remainder is reported, not silently dropped",
      report["skipped"] == 40 - CS.MAX_GUILDS_PER_BOOT, report["skipped"])


# ══════════════════════════════════════════════════════════════════════════════
print("\n── 7. the admin action no longer creates the duplicates ─────────")

from cogs.admin import actions as A                      # noqa: E402


# Asserted by OBSERVING the call, not by reading for it. `do_sync`'s docstring
# explains the duplication bug in prose containing both "mirror" and
# "copy_global_to", and a substring check reads that explanation as the
# offence — the same trap that produced three bad assertions in earlier suites.
# `copied_to` records every copy_global_to the tree actually received.
tree, guild, bot = fresh({1: NAMES})
res = run(A.do_sync(bot, guild, "guild"))
check("the DEFAULT path never copies the globals into the guild",
      tree.copied_to == [], tree.copied_to)
for mode in ("clean", "purge", "global"):
    t2, g2, b2 = fresh({1: NAMES})
    run(A.do_sync(b2, g2, mode))
    check(f"...and neither does `{mode}`", t2.copied_to == [], t2.copied_to)
check("the default clears this server's copies", tree.guild[1] == [], tree.guild[1])
check("...and registers globally", sorted(tree.globals) == sorted(NAMES))
check("...leaving each command listed once",
      tree.picker_lines(1) == len(NAMES), tree.picker_lines(1))
check("...and says how many duplicates it removed",
      "6" in res.message, res.message)

tree, guild, bot = fresh()
res = run(A.do_sync(bot, guild, "mirror"))
check("`mirror` is the ONLY mode that copies them", tree.copied_to == [guild.id],
      tree.copied_to)
check("...and it still mirrors", tree.picker_lines(1) == 2 * len(NAMES))
check("...and warns that everything now shows twice",
      "twice" in res.message.lower(), res.message)

check("`sync_purge` is gone — it and the new default did the same thing",
      "sync_purge" not in A.REGISTRY)
check("`sync_mirror` is flagged destructive enough to confirm",
      bool(A.REGISTRY["sync_mirror"].confirm))


print("\n" + "=" * 66)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 66)
sys.exit(1 if FAIL else 0)
