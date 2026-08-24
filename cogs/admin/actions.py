"""
actions.py — every admin capability, as plain functions with no UI attached.

Why this file exists
--------------------
Admin tooling had sprawled across five files and 41 commands: `admin.py` (22
prefix commands plus a group), `audit.py` (a group of six), `console.py` (a
slash command squeezed through six generic parameters), `;codeadmin` inside
the redeem cog and `;rankadmin` inside the ranked cog. Every one of them was
hidden, so the only way to know a command existed was to have written it.

The split here is the one that worked for the v1.12 tournament rewrite:
`brackets.py` stayed pure and testable while the UI changed around it. This
module owns *what an admin action does*; `panel.py` owns *how it is chosen*.
Nothing here touches `discord.ui`, `Interaction` or a `Context` — a handler
takes an `ActionCtx` dataclass and returns a `Result`, which is why the whole
surface can be driven from `tools/sim_admin.py` without a gateway connection.

The registry is the contract
----------------------------
`console.py` got this wrong in a way worth not repeating: its ACTIONS table
listed actions whose `_do_*` handler had been deleted with the old tournament
package, so picking one from the dropdown answered "isn't wired up". Here the
handler is a field *of* the action, so an action without a handler cannot be
declared, and the suite asserts the reverse direction too.

Every action also declares:

  * `needs` — the parameters it cannot run without. Checked before the handler,
    so a missing id never reaches a service call.
  * `confirm` — whether it destroys something. `;resetplayer` wiped a profile
    on one line with no confirmation; `giveallcoins` touched all 3,400 rows the
    same way. Those now need a second, specific press.
  * `category` — which page of the panel it lives on. Each stays well under
    Discord's 25-option select cap.

Writes go through `mutate_user`
-------------------------------
The old handlers did `get_user()` … mutate … `update_user()`, which is a race:
anything written to that profile in between is silently discarded on the write
back. That exact pattern erased blades in `redeem.grant`. Every profile write
here is a single `mutate_user` call instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from utils.database import (
    BASE_DIR,
    _atomic_write_json,
    _read_json,
    get_beyblade,
    get_user,
    load_users,
    mutate_user,
)

log = logging.getLogger("beyblade_bot.admin")

MASTER_ID = 956773141265391676
ADMIN_ROLE = "Tournament Admin"


def is_admin(user) -> bool:
    """Owner, or anyone holding the admin role.

    Checked per invocation rather than once at registration, because roles
    change while the bot is running.
    """
    if getattr(user, "id", None) == MASTER_ID:
        return True
    roles = getattr(user, "roles", None) or []
    return any(getattr(r, "name", "") == ADMIN_ROLE for r in roles)


# ══════════════════════════════════════════════════════════════════════════════
#  Persistent admin state — maintenance mode and the ban list
# ══════════════════════════════════════════════════════════════════════════════
#
# Read on the hot path: `gate_check` in onboarding runs in front of EVERY
# command in the bot, and it asks both questions on every invocation. So the
# file is read once and then served from memory, with writes going through the
# same lock. One process owns this file, so an in-memory authority is correct —
# and a per-command JSON read is exactly the kind of cost that is invisible in
# testing and painful at 3,400 profiles.

STATE_PATH = os.path.join(BASE_DIR, "data", "admin_state.json")

_state_lock = threading.Lock()
_state_cache: Optional[dict] = None


def _blank_state() -> dict:
    return {"maintenance": {}, "bans": {}}


def _state() -> dict:
    global _state_cache
    if _state_cache is None:
        with _state_lock:
            if _state_cache is None:
                try:
                    data = _read_json(STATE_PATH, _blank_state)
                except Exception as exc:                 # noqa: BLE001
                    log.warning("[admin] admin_state unreadable: %s", exc)
                    data = _blank_state()
                data.setdefault("maintenance", {})
                data.setdefault("bans", {})
                _state_cache = data
    return _state_cache


def _save_state() -> None:
    with _state_lock:
        if _state_cache is not None:
            _atomic_write_json(STATE_PATH, _state_cache)


def reload_state() -> None:
    """Drop the in-memory copy. Only the tests need this."""
    global _state_cache
    with _state_lock:
        _state_cache = None


# ── maintenance mode ──────────────────────────────────────────────────────────

def maintenance() -> dict:
    """`{"on": bool, "reason": str, "since": float}` — always all three keys."""
    m = _state().get("maintenance") or {}
    return {"on": bool(m.get("on")),
            "reason": str(m.get("reason") or ""),
            "since": float(m.get("since") or 0)}


def set_maintenance(on: bool, reason: str = "", by: int = 0) -> dict:
    st = _state()
    st["maintenance"] = {"on": bool(on), "reason": (reason or "")[:300],
                         "since": time.time() if on else 0, "by": by}
    _save_state()
    return maintenance()


# ── the ban list ──────────────────────────────────────────────────────────────

def banned(user_id) -> Optional[dict]:
    """The ban record for this user, or None. Never raises."""
    try:
        return (_state().get("bans") or {}).get(str(user_id))
    except Exception:                                    # noqa: BLE001
        return None


def ban_user(user_id, reason: str = "", by: int = 0) -> dict:
    st = _state()
    rec = {"reason": (reason or "no reason given")[:300],
           "at": time.time(), "by": by}
    st.setdefault("bans", {})[str(user_id)] = rec
    _save_state()
    return rec


def unban_user(user_id) -> bool:
    st = _state()
    gone = st.setdefault("bans", {}).pop(str(user_id), None) is not None
    if gone:
        _save_state()
    return gone


def ban_list() -> list[tuple[str, dict]]:
    """Newest ban first."""
    bans = (_state().get("bans") or {}).items()
    return sorted(bans, key=lambda kv: -float(kv[1].get("at") or 0))


# ══════════════════════════════════════════════════════════════════════════════
#  The handler contract
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ActionCtx:
    """Everything a handler is allowed to know about how it was invoked.

    Deliberately not a `Context` or an `Interaction`: a handler that can only
    see these fields is a handler the suite can call. `target` is whatever
    user-like object the picker produced (it may be None even when `target_id`
    is set, which is why handlers key off `target_id`).
    """
    bot: Any = None
    guild: Any = None
    invoker: Any = None
    invoker_id: int = 0
    target: Any = None
    target_id: Optional[int] = None
    amount: Optional[int] = None
    text: Optional[str] = None
    channel: Any = None
    # Which server a report is filtered to. None means all of them — the
    # default, and the reason `server` never appears in `missing_params`.
    guild_choice: Optional[int] = None

    def target_name(self) -> str:
        return (getattr(self.target, "display_name", None)
                or getattr(self.target, "name", None)
                or (f"ID {self.target_id}" if self.target_id else "nobody"))

    def target_mention(self) -> str:
        m = getattr(self.target, "mention", None)
        return m or (f"<@{self.target_id}>" if self.target_id else "?")


@dataclass
class Result:
    """What a handler produces. `embed` and `file` stay optional so the common
    case — one line of text — needs nothing but a string."""
    ok: bool = True
    message: str = ""
    embed: Any = None
    embeds: list = field(default_factory=list)
    file: Any = None
    # For an action whose answer is interactive rather than a line of text —
    # restoring a backup needs a picker and a confirm, not a typed filename.
    view: Any = None

    @staticmethod
    def fail(message: str) -> "Result":
        return Result(ok=False, message=message)


Handler = Callable[[ActionCtx], Awaitable[Result]]


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    description: str
    category: str
    handler: Handler
    needs: tuple[str, ...] = ()
    confirm: str = ""            # non-empty => a second press, with this warning
    owner_only: bool = True      # see below — the default is the safe answer


# `is_admin` accepts the ADMIN_ROLE, and a role called "Tournament Admin" can
# be created by anyone with Manage Roles in ANY server the bot is in. That was
# safe in `console.py`, which exposed six tournament and casino actions behind
# it. It is NOT safe for a panel that can reset a profile, ban a player or mint
# coins into every wallet in the database — so `owner_only` defaults to True
# and each action that a role-holder may run has to say so out loud.
#
# The check lives in `run()`, not only in the view: a select that hides an
# action is a UI convenience, and the thing that actually refuses has to be
# the thing that dispatches.


def may_run(action: "Action", user) -> bool:
    if getattr(user, "id", None) == MASTER_ID:
        return True
    return (not action.owner_only) and is_admin(user)


# Category keys are ASCII and non-empty because they become select option
# VALUES. An empty value is a Discord 400 (`value: Must be between 1 and 100 in
# length`) — the bug that took `;inv` down for 75% of blade holders in v96.
CATEGORIES: list[tuple[str, str, str]] = [
    ("economy",    "Economy",    "💰"),
    ("content",    "Content",    "🎁"),
    ("players",    "Players",    "👤"),
    ("tournament", "Tournament", "🏆"),
    ("codes",      "Codes",      "🎟️"),
    ("announce",   "Announce",   "📣"),
    ("audit",      "Audit",      "📊"),
    ("ranked",     "Ranked",     "🎖️"),
    ("system",     "System",     "🔧"),
]

CATEGORY_ORDER = [c[0] for c in CATEGORIES]

PARAM_HELP = {
    "user":    "a player (use the picker)",
    "amount":  "a number",
    "text":    "some text",
    "channel": "a channel",
    # A FILTER, not a required input: "all servers" is always a valid answer,
    # so `missing_params` never blocks on it. See `GuildPicker` in the kit.
    "server":  "a server (optional — defaults to all)",
}

REGISTRY: dict[str, Action] = {}


def register(key: str, label: str, description: str, category: str,
             needs: tuple[str, ...] = (), confirm: str = "",
             owner_only: bool = True):
    """Decorator form, so an action and its handler cannot drift apart."""
    def deco(fn: Handler) -> Handler:
        if key in REGISTRY:
            raise ValueError(f"duplicate admin action key: {key}")
        if category not in CATEGORY_ORDER:
            raise ValueError(f"unknown category {category!r} for {key}")
        REGISTRY[key] = Action(key=key, label=label, description=description,
                               category=category, handler=fn, needs=needs,
                               confirm=confirm, owner_only=owner_only)
        return fn
    return deco


def actions_in(category: str, user=None) -> list[Action]:
    """Actions on this page — filtered to what `user` may actually run."""
    out = [a for a in REGISTRY.values() if a.category == category]
    if user is not None:
        out = [a for a in out if may_run(a, user)]
    return out


def categories_for(user=None) -> list[tuple[str, str, str]]:
    """Category rows that still have something on them for this user."""
    return [c for c in CATEGORIES if actions_in(c[0], user)]


def missing_params(action: Action, ctx: ActionCtx) -> list[str]:
    """Which declared requirements this invocation hasn't supplied."""
    got = {"user": ctx.target_id, "amount": ctx.amount,
           "text": (ctx.text or "").strip() or None, "channel": ctx.channel,
           # Always satisfied: no selection means "all servers", which is a
           # real answer rather than a missing one.
           "server": True}
    return [p for p in action.needs if got.get(p) in (None, "")]


async def run(key: str, ctx: ActionCtx) -> Result:
    """Dispatch, with the parameter check in front of the handler."""
    action = REGISTRY.get((key or "").strip().lower())
    if action is None:
        return Result.fail(f"Unknown action `{key}`.")
    if not may_run(action, ctx.invoker or types.SimpleNamespace(
            id=ctx.invoker_id, roles=[])):
        return Result.fail(f"**{action.label}** is owner-only.")
    missing = missing_params(action, ctx)
    if missing:
        return Result.fail(
            f"**{action.label}** also needs "
            + ", ".join(PARAM_HELP.get(p, p) for p in missing) + ".")
    try:
        return await action.handler(ctx)
    except Exception as exc:                             # noqa: BLE001
        # Logged through the ring buffer too, so the admin who just broke it
        # can read the traceback from the panel rather than from a log file
        # that does not exist on this host.
        log.exception("[admin] action %s failed", key)
        try:
            from utils import errorlog
            errorlog.record(f"admin:{key}", exc)
        except Exception:                                # noqa: BLE001
            pass
        return Result.fail(f"❌ `{type(exc).__name__}: {exc}`")


def _embed(title: str, colour: int = 0x5865F2, description: str = ""):
    import discord
    return discord.Embed(title=title, colour=colour,
                         description=description or None)


# ══════════════════════════════════════════════════════════════════════════════
#  💰  Economy
# ══════════════════════════════════════════════════════════════════════════════

@register("givecoins", "Give coins", "add Beycoins to one player",
          "economy", needs=("user", "amount"))
async def _givecoins(ctx: ActionCtx) -> Result:
    def apply(p):
        p["coins"] = max(0, int(p.get("coins", 0)) + int(ctx.amount))
        return p["coins"]
    bal = mutate_user(ctx.target_id, apply)
    return Result(message=f"✅ Gave 🪙 **{ctx.amount:,}** to {ctx.target_mention()}. "
                          f"Balance: **{bal:,}**.")


@register("removecoin", "Take coins", "remove Beycoins, never below zero",
          "economy", needs=("user", "amount"))
async def _removecoin(ctx: ActionCtx) -> Result:
    def apply(p):
        p["coins"] = max(0, int(p.get("coins", 0)) - int(ctx.amount))
        return p["coins"]
    bal = mutate_user(ctx.target_id, apply)
    return Result(message=f"✅ Took 🪙 **{ctx.amount:,}** from {ctx.target_mention()}. "
                          f"Balance: **{bal:,}**.")


@register("setcoins", "Set coins", "set a balance to an exact value",
          "economy", needs=("user", "amount"))
async def _setcoins(ctx: ActionCtx) -> Result:
    def apply(p):
        p["coins"] = max(0, int(ctx.amount))
        return p["coins"]
    bal = mutate_user(ctx.target_id, apply)
    return Result(message=f"✅ {ctx.target_mention()}'s balance is now 🪙 **{bal:,}**.")


@register("givexp", "Give XP", "add XP (a negative amount removes it)",
          "economy", needs=("user", "amount"))
async def _givexp(ctx: ActionCtx) -> Result:
    def apply(p):
        p["xp"] = max(0, int(p.get("xp", 0)) + int(ctx.amount))
        return p["xp"]
    xp = mutate_user(ctx.target_id, apply)
    return Result(message=f"✅ {ctx.target_mention()} now has **{xp:,} XP**.")


@register("giveallcoins", "Give coins to EVERYONE", "adds to every profile",
          "economy", needs=("amount",),
          confirm="This adds coins to every profile in the database.")
async def _giveallcoins(ctx: ActionCtx) -> Result:
    if int(ctx.amount) <= 0:
        return Result.fail("Amount must be positive.")
    # ONE load→mutate→save cycle. The original per-user `update_user` loop
    # rewrote the whole users file once per player — 3,400 full-file writes,
    # and the event loop frozen for minutes.
    from utils.database import _users_lock, save_users

    def _bulk() -> int:
        with _users_lock:
            users = load_users()
            for profile in users.values():
                profile["coins"] = int(profile.get("coins", 0)) + int(ctx.amount)
            save_users(users)
            return len(users)

    count = await asyncio.to_thread(_bulk)
    return Result(message=f"✅ Gave 🪙 **{ctx.amount:,}** to all **{count:,}** profiles.")


@register("casino_give", "Give casino coins", "credit the casino wallet",
          "economy", needs=("user", "amount"))
async def _casino_give(ctx: ActionCtx) -> Result:
    if int(ctx.amount) <= 0:
        return Result.fail("Amount must be positive.")
    from cogs.casino import casino_wallet
    await casino_wallet.credit(ctx.target_id, int(ctx.amount))
    # `credit()` returns None — read the balance back rather than assuming it
    # hands one out. Formatting that None was a live TypeError.
    new = await casino_wallet.get_balance(ctx.target_id)
    return Result(message=f"✅ Gave 🎰 **{ctx.amount:,}** to {ctx.target_mention()}. "
                          f"Balance: **{new:,}**.")


@register("casino_take", "Take casino coins", "deduct from the casino wallet",
          "economy", needs=("user", "amount"))
async def _casino_take(ctx: ActionCtx) -> Result:
    if int(ctx.amount) <= 0:
        return Result.fail("Amount must be positive.")
    from cogs.casino import casino_wallet
    if not await casino_wallet.deduct(ctx.target_id, int(ctx.amount)):
        return Result.fail(f"❌ {ctx.target_mention()} doesn't have that many.")
    bal = await casino_wallet.get_balance(ctx.target_id)
    return Result(message=f"✅ Took 🎰 **{ctx.amount:,}** from {ctx.target_mention()}. "
                          f"Balance: **{bal:,}**.")


# ══════════════════════════════════════════════════════════════════════════════
#  🎁  Content
# ══════════════════════════════════════════════════════════════════════════════

@register("givebey", "Give a Beyblade", "grant a blade by name",
          "content", needs=("user", "text"))
async def _givebey(ctx: ActionCtx) -> Result:
    from utils.database import add_beyblade_to_inventory
    name = (ctx.text or "").strip().strip('"').strip("'")
    blade = get_beyblade(name)
    if not blade:
        return Result.fail(f"❌ **{name}** is not in the database.")
    canonical = blade["name"]
    # Through the helper, which writes the profile itself. A snapshot written
    # back on top of it is what erased blades in `redeem.grant`.
    added = add_beyblade_to_inventory(ctx.target_id, canonical)
    if not added:
        return Result(message=f"ℹ️ {ctx.target_mention()} already owns "
                              f"**{canonical}** (or their inventory is full).")
    return Result(message=f"✅ Gave **{canonical}** to {ctx.target_mention()}.")


@register("removebey", "Remove a Beyblade", "take a blade out of an inventory",
          "content", needs=("user", "text"))
async def _removebey(ctx: ActionCtx) -> Result:
    name = (ctx.text or "").strip().strip('"').strip("'")

    def apply(p):
        inv = p.get("inventory", [])
        if name not in inv:
            return False
        inv.remove(name)
        if p.get("active_beyblade") == name:
            p["active_beyblade"] = None
        p["inventory"] = inv
        return True

    if not mutate_user(ctx.target_id, apply):
        return Result.fail(f"❌ **{name}** is not in {ctx.target_mention()}'s inventory.")
    return Result(message=f"✅ Removed **{name}** from {ctx.target_mention()}.")


@register("giveavatar", "Give an avatar card", "the only route for Exclusives",
          "content", needs=("user", "text"))
async def _giveavatar(ctx: ActionCtx) -> Result:
    """Grant an avatar card by name or id.

    This is the only way to hand out an **Exclusive** card: `_build_rarity_map`
    filters Exclusive out of every pack pull, so those cards have no other
    route into a player's hands. An ambiguous name lists the candidates rather
    than guessing — handing the wrong Exclusive to somebody has no undo.
    """
    from cogs.avatar import avatar_engine
    from utils.database import add_avatar_to_inventory, player_owns_avatar

    q = (ctx.text or "").strip().strip('"').strip("'")
    cards = avatar_engine.get_all_avatars()
    ql = q.lower()
    exact = [a for a in cards if a["id"].lower() == ql or a["name"].lower() == ql]
    hits = exact or [a for a in cards if ql in a["name"].lower()]

    if not hits:
        return Result.fail(f"❌ No avatar matches **{q}**. `;avatars` lists them.")
    if len(hits) > 1:
        shown = ", ".join(f"**{a['name']}**" for a in hits[:8])
        more = f" …and {len(hits) - 8} more" if len(hits) > 8 else ""
        return Result.fail(f"❌ **{q}** matches {len(hits)} avatars: {shown}{more}\n"
                           f"Use the full name or the id.")
    av = hits[0]
    if player_owns_avatar(ctx.target_id, av["id"]):
        return Result(message=f"ℹ️ {ctx.target_mention()} already owns **{av['name']}**.")
    add_avatar_to_inventory(ctx.target_id, av["id"])
    log.info("[admin] %s granted avatar %s (%s) to %s",
             ctx.invoker_id, av["id"], av["name"], ctx.target_id)
    return Result(message=f"✅ Gave **{av['name']}** *({av['rarity']})* to "
                          f"{ctx.target_mention()}.\n"
                          f"They equip it with `;equipavatar {av['id']}`.")


@register("addpart", "Give a part", "add a part to a player's collection",
          "content", needs=("user", "text"))
async def _addpart(ctx: ActionCtx) -> Result:
    part = (ctx.text or "").strip().strip('"').strip("'")

    def apply(p):
        parts = p.setdefault("parts", [])
        if part in parts:
            return False
        parts.append(part)
        return True

    added = mutate_user(ctx.target_id, apply)
    return Result(message=(f"✅ Added part **{part}** to {ctx.target_mention()}."
                           if added else
                           f"ℹ️ {ctx.target_mention()} already has **{part}**."))


@register("spawn", "Force a spawn", "spawn a blade in this channel now",
          "content")
async def _spawn(ctx: ActionCtx) -> Result:
    cog = ctx.bot.get_cog("SpawnCog") if ctx.bot else None
    if not cog:
        return Result.fail("❌ SpawnCog is not loaded.")
    target = ctx.channel
    if target is None:
        return Result.fail("❌ No channel to spawn in.")
    exclusive = (ctx.text or "").strip().title() == "Exclusive"
    await cog._do_spawn(target, exclusive_only=exclusive)
    return Result(message=f"✅ Spawned in {getattr(target, 'mention', '#?')}"
                          + (" (Exclusive)." if exclusive else "."))


@register("clearspawn", "Clear active spawns", "drop this server's live spawns",
          "content")
async def _clearspawn(ctx: ActionCtx) -> Result:
    cog = ctx.bot.get_cog("SpawnCog") if ctx.bot else None
    if not cog:
        return Result.fail("❌ SpawnCog is not loaded.")
    gid = getattr(ctx.guild, "id", None)
    if gid is None:
        return Result.fail("❌ Run this in a server.")
    state = cog.spawn_states.get(gid)
    if not state or not state.get("active"):
        return Result(message="✅ No active spawns here.")
    async with state["_lock"]:
        names = [s["bey"].get("name", "Unknown") for s in state["active"]]
        state["active"] = []
    return Result(message=f"✅ Cleared **{len(names)}** spawn(s): "
                          + ", ".join(f"**{n}**" for n in names))


# One task, held here rather than on a cog instance, so `;reload` can't orphan
# a running loop that nothing has a handle to any more.
_spawn_loop: dict[str, Any] = {"task": None}


@register("spawnloop", "Start the spawn loop", "auto-spawn every 3–5 min for N hours",
          "content", needs=("amount",))
async def _spawnloop(ctx: ActionCtx) -> Result:
    import random
    cog = ctx.bot.get_cog("SpawnCog") if ctx.bot else None
    if not cog:
        return Result.fail("❌ SpawnCog is not loaded.")
    hours = float(ctx.amount)
    if not (1 <= hours <= 24):
        return Result.fail("❌ Hours must be between 1 and 24.")
    channel = ctx.channel
    if channel is None:
        return Result.fail("❌ No channel to spawn in.")

    task = _spawn_loop.get("task")
    note = ""
    if task is not None and not task.done():
        task.cancel()
        note = "\n⚠️ The previous loop was cancelled."

    deadline = hours * 3600

    async def _loop() -> None:
        elapsed = 0.0
        n = 0
        while elapsed < deadline:
            try:
                await cog._do_spawn(channel)
                n += 1
            except Exception as exc:                     # noqa: BLE001
                log.error("[admin] spawn loop error on #%d: %s", n + 1, exc)
            wait = random.randint(3, 5) * 60
            elapsed += wait
            if elapsed < deadline:
                await asyncio.sleep(wait)
        log.info("[admin] spawn loop finished after %d spawns", n)

    _spawn_loop["task"] = asyncio.create_task(_loop())
    return Result(message=f"✅ Spawn loop running in "
                          f"{getattr(channel, 'mention', '#?')} for "
                          f"**{hours:g} hr(s)**, every 3–5 min.{note}")


@register("spawnloop_stop", "Stop the spawn loop", "cancel the running loop",
          "content")
async def _spawnloop_stop(ctx: ActionCtx) -> Result:
    task = _spawn_loop.get("task")
    if task is None or task.done():
        return Result(message="ℹ️ No spawn loop is running.")
    task.cancel()
    _spawn_loop["task"] = None
    return Result(message="✅ Spawn loop stopped.")


# ══════════════════════════════════════════════════════════════════════════════
#  👤  Players
# ══════════════════════════════════════════════════════════════════════════════

@register("inspect", "Inspect a player", "the full profile for one account",
          "players", needs=("user",))
async def _inspect(ctx: ActionCtx) -> Result:
    from cogs.core.onboarding import _has_started
    p = get_user(ctx.target_id)
    e = _embed(f"🔍  {ctx.target_name()}", 0x9B59B6)
    e.add_field(name="Coins", value=f"🪙 {p.get('coins', 0):,}", inline=True)
    e.add_field(name="Level", value=str(p.get("level", 1)), inline=True)
    e.add_field(name="XP", value=f"{p.get('xp', 0):,}", inline=True)
    e.add_field(name="Record",
                value=f"{p.get('wins', 0)}W / {p.get('losses', 0)}L", inline=True)
    e.add_field(name="Rank score", value=f"{p.get('rank_score', 0):,}", inline=True)
    e.add_field(name="Blades", value=str(len(p.get("inventory", []))), inline=True)
    e.add_field(name="Active blade",
                value=str(p.get("active_beyblade") or "none"), inline=False)
    # The question that produced this whole action: five live accounts with
    # 60,000 XP and an empty inventory were locked out of `;start` because the
    # gate inferred "started" from XP. Now it is a flag, and it is visible.
    e.add_field(name="Ran ;start",
                value="✅ yes" if _has_started(p) else "❌ no", inline=True)
    rec = banned(ctx.target_id)
    e.add_field(name="Bot ban",
                value=(f"🔨 {rec.get('reason')}" if rec else "—"), inline=True)

    battles = int(p.get("wins", 0)) + int(p.get("losses", 0))
    if int(p.get("coins", 0)) > max(50_000, battles * 200 * 20):
        e.add_field(
            name="⚠️ Note",
            value=(f"Balance is far above what {battles} battles would normally "
                   f"produce. Worth checking against admin grants and casino "
                   f"history."),
            inline=False)
    e.set_footer(text=f"ID {ctx.target_id}")
    return Result(embed=e)


@register("find", "Find a player", "search profiles by name or id",
          "players", needs=("text",))
async def _find(ctx: ActionCtx) -> Result:
    """Search by display name or id.

    There was no way to look a player up by name before this: `;audit user`
    needed a mention, which means you already had to be able to find them.
    """
    q = (ctx.text or "").strip().lower()
    bot = ctx.bot
    hits: list[tuple[str, str, dict]] = []
    for uid, prof in (load_users() or {}).items():
        if not isinstance(prof, dict):
            continue
        name = ""
        try:
            u = bot.get_user(int(uid)) if bot else None
            name = getattr(u, "display_name", "") or getattr(u, "name", "") or ""
        except Exception:                                # noqa: BLE001
            name = ""
        if q in str(uid).lower() or (name and q in name.lower()):
            hits.append((str(uid), name or f"User {uid}", prof))
        if len(hits) >= 200:
            break
    if not hits:
        return Result.fail(f"No profile matches **{q}**. "
                           f"Names only match players the bot can currently see.")
    hits.sort(key=lambda h: -int(h[2].get("coins", 0)))
    lines = [f"`{uid}` — **{name}** · 🪙 {p.get('coins', 0):,} · "
             f"L{p.get('level', 1)} · {len(p.get('inventory', []))} blades"
             for uid, name, p in hits[:20]]
    more = f"\n…and {len(hits) - 20} more." if len(hits) > 20 else ""
    return Result(embed=_embed(f"🔎  {len(hits)} match(es) for “{q}”", 0x3498DB,
                               "\n".join(lines) + more))


@register("resetplayer", "Reset a profile", "wipes coins, blades, level, record",
          "players", needs=("user",),
          confirm="This erases their coins, blades, level and battle record.")
async def _resetplayer(ctx: ActionCtx) -> Result:
    def apply(p):
        p.update({
            "user_id": str(ctx.target_id), "active_beyblade": None,
            "inventory": [], "coins": 0, "rank_score": 0, "wins": 0,
            "losses": 0, "xp": 0, "level": 1, "parts": [], "last_daily": None,
        })
        # NOT cleared: the starter flag. Wiping it would drop them back behind
        # the `;start` gate with a profile that already exists — which is the
        # exact lockout v1.08 fixed.
        return True
    mutate_user(ctx.target_id, apply)
    return Result(message=f"✅ **{ctx.target_name()}**'s profile has been reset.")


@register("setrank", "Set rank", "override a player's displayed rank",
          "players", needs=("user", "amount"))
async def _setrank(ctx: ActionCtx) -> Result:
    from utils.ranks import rank_name_for
    def apply(p):
        p["rank_override"] = int(ctx.amount)
    mutate_user(ctx.target_id, apply)
    return Result(message=f"✅ {ctx.target_mention()} is now rank **#{ctx.amount}** "
                          f"({rank_name_for(int(ctx.amount))}).")


@register("ban", "Ban from the bot", "they can't use any command",
          "players", needs=("user", "text"),
          confirm="They will be refused by every command in the bot.")
async def _ban(ctx: ActionCtx) -> Result:
    if int(ctx.target_id) == MASTER_ID:
        return Result.fail("❌ You can't ban the owner.")
    rec = ban_user(ctx.target_id, ctx.text or "", ctx.invoker_id)
    log.info("[admin] %s banned %s: %s", ctx.invoker_id, ctx.target_id,
             rec["reason"])
    return Result(message=f"🔨 Banned {ctx.target_mention()} — {rec['reason']}")


@register("unban", "Unban", "lift a bot ban", "players", needs=("user",))
async def _unban(ctx: ActionCtx) -> Result:
    if not unban_user(ctx.target_id):
        return Result(message=f"ℹ️ {ctx.target_mention()} isn't banned.")
    return Result(message=f"✅ Unbanned {ctx.target_mention()}.")


@register("banlist", "Show the ban list", "everyone banned from the bot",
          "players")
async def _banlist(ctx: ActionCtx) -> Result:
    rows = ban_list()
    if not rows:
        return Result(message="✅ Nobody is banned.")
    lines = [f"<@{uid}> (`{uid}`) — {r.get('reason', '?')} "
             f"· <t:{int(r.get('at', 0))}:R>" for uid, r in rows[:25]]
    more = f"\n…and {len(rows) - 25} more." if len(rows) > 25 else ""
    return Result(embed=_embed(f"🔨  Banned ({len(rows)})", 0xE74C3C,
                               "\n".join(lines) + more))


# ══════════════════════════════════════════════════════════════════════════════
#  🏆  Tournament
# ══════════════════════════════════════════════════════════════════════════════
#
# Through the three public `admin_*` methods on TournamentCog, never into its
# internals. The old console imported `..tournament.views`, `..models`,
# `..brackets` and `..notifications` directly and read `cog.svc.store` — which
# is why deleting one package broke seventeen admin actions at once.

def _tcog(ctx: ActionCtx):
    return ctx.bot.get_cog("Tournaments") if ctx.bot else None


@register("t_start", "Force-start the tournament", "starts the lobby short-handed",
          "tournament", owner_only=False)
async def _t_start(ctx: ActionCtx) -> Result:
    cog = _tcog(ctx)
    lobby = cog.admin_lobby(getattr(ctx.guild, "id", 0)) if cog else None
    if not lobby or lobby.started:
        return Result.fail("No open tournament here.")
    from cogs.tournament.tournament import MIN_PLAYERS
    if not cog.admin_start(ctx.guild.id):
        return Result.fail(f"Needs at least {MIN_PLAYERS} entrants.")
    return Result(message="▶️ Starting the tournament.")


@register("t_cancel", "Cancel the tournament", "closes the lobby, releases everyone",
          "tournament", owner_only=False)
async def _t_cancel(ctx: ActionCtx) -> Result:
    cog = _tcog(ctx)
    ok = await cog.admin_cancel(getattr(ctx.guild, "id", 0)) if cog else False
    return Result(ok=ok, message=("✖️ Tournament cancelled — everyone released."
                                  if ok else "No open tournament here."))


@register("t_force_win", "Force a match winner", "advance this player",
          "tournament", needs=("user",), owner_only=False)
async def _t_force_win(ctx: ActionCtx) -> Result:
    cog = _tcog(ctx)
    lobby = cog.admin_lobby(getattr(ctx.guild, "id", 0)) if cog else None
    if not lobby or not lobby.matches:
        return Result.fail("No live bracket here.")
    if not cog.admin_force_win(ctx.guild.id, ctx.target_id):
        return Result.fail(f"{ctx.target_mention()} has no unfinished match.")
    return Result(message=f"⚖️ {ctx.target_mention()} advances.")


@register("t_ban", "Ban from tournaments", "they can't join a lobby",
          "tournament", needs=("user", "text"), owner_only=False)
async def _t_ban(ctx: ActionCtx) -> Result:
    cog = _tcog(ctx)
    if cog is None:
        return Result.fail("Tournament cog not loaded.")
    # Through the cog, which owns the "entrants and _active move together"
    # invariant. Hand-rolling half of it out here was the third place that had
    # to stay in sync, and the one that would silently stop being maintained.
    cog.admin_ban(getattr(ctx.guild, "id", 0), ctx.target_id)
    return Result(message=f"🚫 {ctx.target_mention()} banned from tournaments "
                          f"— {ctx.text}")


# ══════════════════════════════════════════════════════════════════════════════
#  🎟️  Codes
# ══════════════════════════════════════════════════════════════════════════════

@register("code_create", "Create a redeem code",
          "type a spec: coins:5000 blade:Name avatar:Name bossbey:Name uses:100 days:7",
          "codes", needs=("text",))
async def _code_create(ctx: ActionCtx) -> Result:
    from cogs.codes.redeem import _pretty, create_code, describe, parse_rewards

    args = (ctx.text or "").strip()
    # The note is pulled out FIRST, taking the rest of the string with it.
    # Scanning left-to-right and breaking on `note:` meant "note:hi uses:50"
    # silently dropped the uses.
    note, body = "", args
    if "note:" in args.lower():
        idx = args.lower().index("note:")
        note, body = args[idx + 5:].strip(), args[:idx]

    spec_parts, uses, days = [], 0, 0
    for token in body.split():
        low = token.lower()
        if low.startswith("uses:"):
            uses = int(token[5:]) if token[5:].isdigit() else 0
        elif low.startswith("days:"):
            days = int(token[5:]) if token[5:].isdigit() else 0
        else:
            spec_parts.append(token)

    rewards, err = parse_rewards(",".join(spec_parts))
    if err:
        return Result.fail(f"❌ {err}")

    key, entry = create_code(rewards, uses, days, note, ctx.invoker_id)

    e = _embed("🎟️  Code created", 0x2ECC71,
               f"## `{_pretty(key)}`\n\n{describe(rewards)}")
    e.add_field(name="Uses", value=("unlimited" if not uses else str(uses)),
                inline=True)
    e.add_field(name="Expires",
                value=("never" if not entry["expires"]
                       else f"<t:{int(entry['expires'])}:R>"),
                inline=True)
    if note:
        e.add_field(name="Note", value=note, inline=False)
    e.set_footer(text="Players claim it with ;redeem <code>")
    return Result(embed=e)


@register("code_builder", "Build a code (picker)",
          "pick rewards from a menu instead of typing a spec",
          "codes")
async def _code_builder(ctx: ActionCtx) -> Result:
    from cogs.codes.builder import CodeBuilderView
    view = CodeBuilderView(ctx.bot, ctx.invoker_id)
    return Result(embed=view.embed(), view=view)


@register("code_list", "List redeem codes", "newest first, with usage",
          "codes")
async def _code_list(ctx: ActionCtx) -> Result:
    from cogs.codes.redeem import _load, describe
    data = _load()
    codes = sorted(data["codes"].items(),
                   key=lambda kv: -float(kv[1].get("created_at", 0)))
    if not codes:
        return Result(message="No codes exist yet.")
    lines = []
    for key, c in codes[:20]:
        used, cap = len(c.get("claimed_by", {})), c.get("max_uses", 0)
        state = ("🚫 revoked" if c.get("revoked")
                 else "⏰ expired" if c.get("expires") and time.time() > c["expires"]
                 else "✅ live")
        lines.append(f"`{c.get('display', key)}` {state} — "
                     f"{describe(c['rewards'])[:50]} · {used}/{cap or '∞'}")
    more = f"\n…and {len(codes) - 20} more." if len(codes) > 20 else ""
    return Result(embed=_embed(f"🎟️  Redeem codes ({len(codes)})", 0x9B59B6,
                               "\n".join(lines) + more))


@register("code_revoke", "Revoke a code", "it can no longer be claimed",
          "codes", needs=("text",),
          confirm="The code stops working for everyone immediately.")
async def _code_revoke(ctx: ActionCtx) -> Result:
    from cogs.codes.code_store import REDEEM_PATH, normalise, redeem_lock, save
    from cogs.codes.redeem import _load, _pretty
    key = normalise(ctx.text or "")
    with redeem_lock:
        data = _load()
        found = key in data["codes"]
        if found:
            data["codes"][key]["revoked"] = True
            save(REDEEM_PATH, data)
    if not found:
        return Result.fail("❌ No such code.")
    return Result(message=f"🚫 Revoked `{_pretty(key)}`.")


# ══════════════════════════════════════════════════════════════════════════════
#  📣  Announce
# ══════════════════════════════════════════════════════════════════════════════
#
# Nothing here fires on its own. The bot deploys by extracting a zip over a
# live install and is in many servers; a feature that posts to all of them
# automatically, on every restart, is one bad boot away from spamming every
# server it is in. So an admin writes the message and presses the button.

ANNOUNCE_COLOUR = 0x5865F2


def _announce_target(ctx: ActionCtx):
    """(channel, error). The configured announcement channel for this guild."""
    from utils.database import get_announce_channel
    gid = getattr(ctx.guild, "id", None)
    if gid is None:
        return None, "Run this in a server."
    cid = get_announce_channel(gid)
    if not cid:
        return None, ("No announcement channel is set here yet — use "
                      "**Set the announcement channel** first.")
    ch = ctx.bot.get_channel(int(cid)) if ctx.bot else None
    if ch is None:
        return None, (f"The announcement channel (`{cid}`) is gone, or I can't "
                      f"see it any more. Set it again.")
    return ch, ""


async def _post(channel, embed) -> Result:
    """Send it, and turn the two failures an admin can actually fix into words."""
    import discord
    try:
        msg = await channel.send(embed=embed)
    except discord.Forbidden:
        return Result.fail(f"❌ I can't post in {channel.mention} — I need "
                           f"**Send Messages** and **Embed Links** there.")
    except Exception as exc:                             # noqa: BLE001
        return Result.fail(f"❌ Couldn't post: `{type(exc).__name__}: {exc}`")
    return Result(message=f"📣 Announced in {channel.mention}. [Jump]({msg.jump_url})")


@register("announce_channel", "Set the announcement channel",
          "where announcements are posted", "announce", needs=("channel",))
async def _announce_channel(ctx: ActionCtx) -> Result:
    from utils.database import set_announce_channel
    gid = getattr(ctx.guild, "id", None)
    if gid is None:
        return Result.fail("Run this in a server.")
    ch = ctx.channel
    if ch is None:
        return Result.fail("Pick a channel.")
    set_announce_channel(gid, ch.id)
    return Result(message=f"✅ Announcements will be posted in {ch.mention}.")


@register("announce_post", "Write an announcement", "posts it to that channel",
          "announce", needs=("text",))
async def _announce_post(ctx: ActionCtx) -> Result:
    channel, err = _announce_target(ctx)
    if err:
        return Result.fail(f"❌ {err}")
    body = (ctx.text or "").strip()
    # First line is the title when one is offered, so a composer with a single
    # text box can still produce a headed announcement.
    title, _, rest = body.partition("\n")
    e = _embed(f"📣 {title.strip()[:250]}", ANNOUNCE_COLOUR, rest.strip() or None)
    if not rest.strip():
        # A one-line announcement reads better as the body than as a bare
        # heading with nothing under it.
        e = _embed("📣 Announcement", ANNOUNCE_COLOUR, body[:4000])
    e.set_footer(text=f"Beycord · {getattr(ctx.guild, 'name', '')}"[:2048])
    return await _post(channel, e)


@register("announce_update", "Announce an update", "prefilled with this build",
          "announce", needs=("text",))
async def _announce_update(ctx: ActionCtx) -> Result:
    """A composer, not an automatic boot announcement.

    `update_note()` supplies the version and the last commit message so the
    admin edits a draft rather than typing a changelog from memory.
    """
    channel, err = _announce_target(ctx)
    if err:
        return Result.fail(f"❌ {err}")
    from utils.buildinfo import VERSION
    e = _embed(f"🧬 Beycord {VERSION}", 0x57F287, (ctx.text or "").strip()[:4000])
    e.set_footer(text="Run ;help to see what's available.")
    return await _post(channel, e)


def update_note() -> str:
    """The draft body for `announce_update` — version plus the last commit.

    Read from the updater's own state file rather than restated here, so the
    announcement cannot claim a version the install is not running.
    """
    from utils.buildinfo import VERSION
    lines = [f"Beycord is now on **{VERSION}**."]
    try:
        from utils import updater
        st = updater.status() or {}
        msg = (st.get("message") or "").strip()
        if msg:
            lines.append("")
            lines.append(msg[:500])
    except Exception:                                    # noqa: BLE001
        pass
    return "\n".join(lines)


@register("report_channel", "Bug & suggestion channel",
          "where /bugs and /suggest land — or ;reportchannel", "announce",
          needs=("channel",))
async def _report_channel(ctx: ActionCtx) -> Result:
    """ONE destination for every server, not one per guild.

    Reports are for whoever maintains the bot, and the useful ones arrive from
    servers that person is not in — a per-guild setting would file them where
    nobody is reading.
    """
    from utils.database import set_report_channel
    ch = ctx.channel
    if ch is None:
        return Result.fail("Pick a channel.")
    set_report_channel(ch.id)
    return Result(message=(f"✅ `/bugs` and `/suggest` from **every** server "
                           f"will now land in {ch.mention}."))


@register("reports_open", "Open bug reports",
          "what players have filed with /bugs and /suggest", "announce")
async def _reports_open(ctx: ActionCtx) -> Result:
    from cogs.updates import reports as R
    from cogs.updates import store as S
    rows = S.open_reports(8)
    if not rows:
        return Result(message="✅ No open reports.")
    e = _embed(f"📋  Open reports ({len(rows)})", ANNOUNCE_COLOUR)
    for r in rows:
        kemoji, klabel, _c = R.KIND_LABEL.get(r.get("kind"), ("❓", "?", 0))
        semoji, slabel, _c2 = R.STATUS_LABEL.get(r.get("status"), ("❓", "?", 0))
        e.add_field(
            name=f"{kemoji} {r.get('summary', '?')}"[:256],
            value=(f"{semoji} {slabel} · <@{r.get('user_id')}>\n"
                   f"`{r.get('report_id')}`")[:1024],
            inline=False)
    return Result(embed=e)


@register("announce_tournament", "Announce a tournament",
          "posts the panel, Join button and all", "announce")
async def _announce_tournament(ctx: ActionCtx) -> Result:
    """Posts the REAL tournament panel into the announcement channel.

    Through `TournamentCog.admin_announce`, never into the cog's internals —
    that coupling is what broke seventeen admin actions when the old
    tournament package was deleted.
    """
    channel, err = _announce_target(ctx)
    if err:
        return Result.fail(f"❌ {err}")
    cog = _tcog(ctx)
    if cog is None:
        return Result.fail("Tournament cog not loaded.")
    note = (ctx.text or "").strip()
    ok = await cog.admin_announce(channel, ctx.invoker, note)
    if not ok:
        return Result.fail("A tournament is already open in this server — "
                           "cancel it first, or use **Force-start**.")
    return Result(message=f"🏆 Tournament announced in {channel.mention} — "
                          f"players join with the button.")


# ══════════════════════════════════════════════════════════════════════════════
#  📊  Audit
# ══════════════════════════════════════════════════════════════════════════════
#
# Built after a registry snapshot showed 3,131 of 3,356 rows completely empty,
# 46 accounts that had ever fought, and one wallet holding 48.5% of the supply.

CONCENTRATION_WARN = 0.20


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "—"


def _ts(value) -> str:
    try:
        return f"<t:{int(value)}:R>" if value else "unknown"
    except Exception:                                    # noqa: BLE001
        return "unknown"


@register("audit", "Economy overview", "population, engagement, coin supply",
          "audit")
async def _audit(ctx: ActionCtx) -> Result:
    from utils.database import USER_STORE
    s = USER_STORE.stats()
    pct = USER_STORE.coin_percentiles()
    total = s["total"] or 1
    supply = s["coin_supply"] or 0
    wallets = USER_STORE.top_wallets(1)
    top_share = (wallets[0]["coins"] / supply) if wallets and supply else 0

    e = _embed("🔍  Economy audit",
               0xE74C3C if top_share >= CONCENTRATION_WARN else 0x2ECC71)
    e.add_field(name="Population",
                value=(f"Rows: **{s['total']:,}**\n"
                       f"Empty: **{s['ghosts']:,}** ({_pct(s['ghosts'], total)})\n"
                       f"Real: **{s['total'] - s['ghosts']:,}**"), inline=True)
    e.add_field(name="Engagement",
                value=(f"Ever battled: **{s['battlers']:,}** "
                       f"({_pct(s['battlers'], total)})\n"
                       f"Own a blade: **{s['collectors']:,}** "
                       f"({_pct(s['collectors'], total)})\n"
                       f"Active 7d: "
                       f"**{USER_STORE.active_since(time.time() - 7 * 86400):,}**"),
                inline=True)
    e.add_field(name="Coin supply",
                value=(f"Total: 🪙 **{supply:,}**\n"
                       f"Median 🪙 {pct.get('p50', 0):,} · "
                       f"p90 🪙 {pct.get('p90', 0):,} · "
                       f"p99 🪙 {pct.get('p99', 0):,}\n"
                       f"Richest holds **{top_share * 100:.1f}%**"), inline=False)
    if top_share >= CONCENTRATION_WARN:
        e.add_field(
            name="⚠️ Concentration warning",
            value=(f"One wallet holds {top_share * 100:.1f}% of the supply. If "
                   f"that isn't you, the leaderboard and the casino balance are "
                   f"both meaningless until you find the source."), inline=False)
    return Result(embed=e)


@register("audit_wallets", "Top wallets", "biggest balances, flagged",
          "audit")
async def _audit_wallets(ctx: ActionCtx) -> Result:
    from utils.database import USER_STORE
    rows = USER_STORE.top_wallets(20)
    supply = USER_STORE.stats()["coin_supply"] or 1
    lines = []
    for i, r in enumerate(rows, 1):
        share = r["coins"] / supply * 100
        battles = r["wins"] + r["losses"]
        # A big balance with almost no gameplay behind it is the tell.
        flag = " 🚩" if (share >= 5 and battles < 5 and r["inv_count"] < 5) else ""
        lines.append(f"**{i}.** <@{r['user_id']}> — 🪙 {r['coins']:,} "
                     f"({share:.1f}%){flag} · L{r['level']} · "
                     f"{r['wins']}W/{r['losses']}L · {r['inv_count']} blades · "
                     f"{_ts(r['last_seen'])}")
    e = _embed("💰  Top wallets", 0xF1C40F, "\n".join(lines) or "No wallets.")
    e.set_footer(text="🚩 big balance, little gameplay")
    return Result(embed=e)


@register("audit_activity", "Who played today", "who was active, and what they ran",
          "audit", needs=("server",))
async def _audit_activity(ctx: ActionCtx) -> Result:
    """Who used the bot, filtered to one server or across all of them.

    What counts as activity: running a command, claiming a wild blade,
    battling. NOT chatting. Until v1.20 it did count chatting, and not
    intentionally — chat XP pays out on every message, every payout wrote the
    profile, and every profile write stamped `last_seen`. So a busy chat
    channel read as a busy game. `chat_xp.py` now writes with `touch=False`
    and both numbers below mean what they say.

    Two sources, because they answer different questions and neither is a
    substitute for the other. `utils/activity.py` counts actions as they
    happen, so it is exact about WHAT was done; the store's `last_seen` only
    knows that somebody used the bot, but it survives restarts. Showing both
    means a restart mid-day reports a small action tally beside an honest
    headcount, instead of quietly reporting a dead day.

    Per server, two numbers per player: **today**, and **lifetime**. The
    lifetime tally survives midnight and restarts, so the top ten is about who
    actually uses the bot here rather than who happened to be online in the
    last few hours.
    """
    from utils import activity
    from utils.database import USER_STORE

    snap = activity.snapshot()
    midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    seen_today = USER_STORE.active_since(midnight)

    gid = ctx.guild_choice
    guild = ctx.bot.get_guild(gid) if (gid and ctx.bot) else None
    where = guild.name if guild is not None else (str(gid) if gid else None)

    e = _embed(f"📅  Today — {where}" if where else "📅  Today", 0x3498DB)

    if gid:
        day_n, life_n = activity.guild_totals(gid)
        e.add_field(name="This server",
                    value=(f"Commands today: **{day_n:,}**\n"
                           f"Commands ever: **{life_n:,}**"), inline=True)
        e.add_field(name="Bot-wide",
                    value=(f"Ran a command: **{snap['active_users']:,}**\n"
                           f"Seen today: **{seen_today:,}**"), inline=True)

        top = activity.top_in_guild(gid, 10)
        if top:
            lines = [f"**{i}.** <@{uid}> — **{life:,}** total"
                     + (f" · {day:,} today" if day else "")
                     for i, (uid, day, life) in enumerate(top, 1)]
            e.add_field(name="Top 10 here", value="\n".join(lines)[:1024],
                        inline=False)
        else:
            e.add_field(name="Top 10 here",
                        value="Nobody has run a command here yet.",
                        inline=False)
    else:
        e.add_field(name="Active",
                    value=(f"Ran a command: **{snap['active_users']:,}**\n"
                           f"Seen today: **{seen_today:,}**"), inline=True)
        e.add_field(name="Commands",
                    value=(f"Run: **{snap['total_commands']:,}**\n"
                           f"Distinct: **{snap['distinct_commands']:,}**"),
                    inline=True)

        if snap["top_users"]:
            lines = [f"**{i}.** <@{uid}> — {n:,} command{'' if n == 1 else 's'}"
                     for i, (uid, n) in enumerate(snap["top_users"], 1)]
            e.add_field(name="Busiest players", value="\n".join(lines)[:1024],
                        inline=False)

        # "for each server", at a glance. Ten is enough to see the shape
        # without turning the report into a directory.
        rows = []
        for g in activity.guilds_seen()[:10]:
            day_n, life_n = activity.guild_totals(g)
            got = ctx.bot.get_guild(g) if ctx.bot else None
            name = got.name if got is not None else f"`{g}`"
            people = len(activity.top_in_guild(g, 10 ** 6))
            rows.append(f"**{name}** — {people:,} player"
                        f"{'' if people == 1 else 's'} · {life_n:,} commands"
                        + (f" · {day_n:,} today" if day_n else ""))
        if rows:
            e.add_field(name="By server", value="\n".join(rows)[:1024],
                        inline=False)

    if snap["commands"]:
        lines = [f"`{name}` — **{n:,}**" for name, n in snap["commands"]]
        e.add_field(name="What they did (bot-wide)",
                    value="\n".join(lines)[:1024], inline=False)
    else:
        e.add_field(name="What they did (bot-wide)",
                    value="Nothing yet since the last restart.", inline=False)

    e.set_footer(text=f"{snap['day']} UTC · commands, claims and battles count "
                      f"— chatting does not · pick a server above to filter")
    return Result(embed=e)


@register("audit_who", "Active players", "the last players the store saw",
          "audit", needs=("amount",))
async def _audit_who(ctx: ActionCtx) -> Result:
    """The tail of `last_seen`, with what each of them ran today beside it."""
    from utils import activity
    from utils.database import USER_STORE
    limit = max(1, min(int(ctx.amount or 20), 50))
    midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = USER_STORE.active_users_since(midnight, limit)
    if not rows:
        return Result(message="Nobody has touched the bot today.")
    lines = []
    for r in rows:
        ran = activity.user_commands(r["user_id"])
        top = ", ".join(f"`{k}`×{v}" for k, v in
                        sorted(ran.items(), key=lambda kv: -kv[1])[:3])
        lines.append(f"<@{r['user_id']}> — L{r['level']} · 🪙 {r['coins']:,} · "
                     f"{_ts(r['last_seen'])}" + (f"\n   ↳ {top}" if top else ""))
    e = _embed(f"👥  Active today — last {len(rows)}", 0x3498DB,
               "\n".join(lines)[:4000])
    e.set_footer(text="Ordered by last seen. Commands shown are since the last "
                      "restart.")
    return Result(embed=e)


@register("audit_funnel", "New player funnel", "where players drop off",
          "audit")
async def _audit_funnel(ctx: ActionCtx) -> Result:
    from utils.database import USER_STORE
    s = USER_STORE.stats()
    total = s["total"] or 1
    stages = [("Touched the bot", s["total"]),
              ("Owns a blade", s["collectors"]),
              ("Fought a battle", s["battlers"]),
              ("Active last 7d",
               USER_STORE.active_since(time.time() - 7 * 86400))]
    lines, prev = [], None
    for label, n in stages:
        bar = "█" * max(0, min(20, round(n / total * 20)))
        drop = "" if prev in (None, 0) else f"  ↓ {(1 - n / prev) * 100:.0f}% drop"
        lines.append(f"`{bar:<20}` **{n:,}** — {label}{drop}")
        prev = n
    e = _embed("📉  New player funnel", 0x3498DB, "\n".join(lines))
    e.add_field(name="Read this as",
                value=("The biggest gap is where to spend the next update. If "
                       "most rows never get a blade, the fix is onboarding, not "
                       "more content."), inline=False)
    return Result(embed=e)


@register("audit_db", "Storage backend", "which store is live, and is it healthy",
          "audit")
async def _audit_db(ctx: ActionCtx) -> Result:
    from utils import database as _db
    e = _embed("🗄️  Storage backend",
               0x2ECC71 if _db.BACKEND == "mysql" else 0x3498DB)
    e.add_field(name="Active", value=f"**{_db.BACKEND.upper()}**", inline=True)
    e.add_field(name="Profiles", value=f"{_db.USER_STORE.count():,}", inline=True)
    try:
        from utils import mysql_store as ms
        from utils import secrets as _sec
        url = _sec.get("MYSQL_URL")
        if not url:
            d = _sec.diagnose()
            # Say exactly where it looked and what was there, so a missing key
            # is a two-second fix instead of a guess.
            detail = ("**MYSQL_URL not found.** Checked:\n"
                      f"• env vars → {', '.join(d['env_keys']) or 'none set'}\n"
                      f"• `config_local.py` → "
                      + ("exists" if d["local_exists"] else "**missing**")
                      + (", loaded" if d["local_loaded"] else ", **not loadable**")
                      + "\n• keys it defines → "
                      + (", ".join(f"`{k}`" for k in d["local_keys"]) or "*none*")
                      + f"\n\nPath: `{d['local_path']}`\nAdd it there and "
                        f"**restart** — the file is read once at boot.")
        elif not ms.driver_available():
            detail = "PyMySQL not installed — `pip install PyMySQL`"
        else:
            cfg = ms.parse_url(url) or {}
            ok, msg = (_db.MYSQL_STORE.probe() if _db.MYSQL_STORE
                       else ms.MySQLStore(url).probe())
            detail = (f"`{cfg.get('host')}/{cfg.get('database')}`\n"
                      f"{'✅' if ok else '❌'} {msg}")
    except Exception as exc:                             # noqa: BLE001
        detail = f"check failed: {exc}"
    e.add_field(name="MySQL", value=detail, inline=False)
    e.add_field(name="SQLite (always present)",
                value=f"{_db.SQLITE_STORE.count():,} profiles", inline=False)
    return Result(embed=e)


@register("audit_migrate", "Migrate to MySQL", "copy local rows into MySQL",
          "audit", confirm="This writes every local profile into MySQL.")
async def _audit_migrate(ctx: ActionCtx) -> Result:
    from utils import database as _db
    from utils import mysql_store as ms
    if _db.MYSQL_STORE is None:
        return Result.fail("❌ MySQL isn't connected — check **Storage backend**.")
    rep = await asyncio.to_thread(
        ms.migrate, _db.SQLITE_STORE, os.path.join(_db.BASE_DIR, "data"),
        _db.MYSQL_STORE, False)
    e = _embed("🗄️  Migration", 0xE67E22 if rep["errors"] else 0x2ECC71)
    e.add_field(name="Profiles",
                value=(f"{rep['users']:,}"
                       + (" (already present, skipped)" if rep["skipped"] else "")),
                inline=False)
    if rep["kv"]:
        e.add_field(name="Other stores",
                    value="\n".join(f"`{k}` — {v}" for k, v in rep["kv"].items()),
                    inline=False)
    if rep["errors"]:
        e.add_field(name="⚠️ Errors", value="\n".join(rep["errors"])[:1000],
                    inline=False)
    e.set_footer(text="Local files are untouched — safe to run again")
    return Result(embed=e)


@register("audit_verify", "Verify MySQL", "sample local rows against MySQL",
          "audit")
async def _audit_verify(ctx: ActionCtx) -> Result:
    from utils import database as _db
    from utils import mysql_store as ms
    if _db.MYSQL_STORE is None:
        return Result.fail("❌ MySQL isn't connected.")
    v = await asyncio.to_thread(ms.verify, _db.SQLITE_STORE, _db.MYSQL_STORE)
    ok = v.get("mismatched", 1) == 0 and not v.get("error")
    e = _embed("🗄️  Verification", 0x2ECC71 if ok else 0xE74C3C)
    e.add_field(name="Local rows", value=f"{v.get('source_rows', 0):,}", inline=True)
    e.add_field(name="MySQL rows", value=f"{v.get('mysql_rows', 0):,}", inline=True)
    e.add_field(name="Sampled",
                value=f"{v.get('checked', 0)} checked · "
                      f"{v.get('mismatched', 0)} mismatched", inline=False)
    if v.get("error"):
        e.add_field(name="Error", value=str(v["error"])[:1000], inline=False)
    return Result(embed=e)


@register("audit_backup", "Back up profiles", "dump the store back out to JSON",
          "audit")
async def _audit_backup(ctx: ActionCtx) -> Result:
    from utils.database import USER_STORE, USERS_PATH
    path = USERS_PATH.replace(".json", f".backup.{int(time.time())}.json")
    n = await asyncio.to_thread(USER_STORE.export_json, path)
    USER_STORE.checkpoint()
    return Result(message=f"✅ Exported **{n:,}** profiles to "
                          f"`{path.split('/')[-1]}`.\n"
                          f"WAL checkpointed — `users.db` is safe to copy alone.")


# ══════════════════════════════════════════════════════════════════════════════
#  🔧  System
# ══════════════════════════════════════════════════════════════════════════════

async def do_sync(bot, guild, mode: str = "guild") -> Result:
    """The one implementation behind both `;sync` and the panel's sync actions.

    Two entry points on one function is safe; two modules each *owning* a
    command name is what produced `CommandAlreadyRegistered`.

    What the default does, and why it changed in v1.14
    --------------------------------------------------
    It used to run `copy_global_to(guild)` then `sync(guild=...)`, writing a
    guild-scoped COPY of every command. Discord's picker is the union of the
    global list and the guild list, so from then on every command in this bot
    was drawn twice — and the boot reconcile deliberately preserved those
    copies, so nothing ever undid it. The one documented deploy step, "run
    `;sync` afterwards", was creating the problem.

    So the default now deletes this server's copies and registers globally.
    Global registration can take up to an hour to appear the first time; that
    is the honest cost, and it is paid once per new command rather than by
    every player reading a doubled list every day.

    `mirror` is the old behaviour, kept because instant registration is
    genuinely useful the day you add a command. It says out loud what it does.
    """
    from utils.command_sync import prune_guild, purge_guild, reconcile
    mode = (mode or "guild").lower()

    if mode.startswith("mirror"):
        bot.tree.copy_global_to(guild=guild)
        cmds = await bot.tree.sync(guild=guild)
        return Result(message=f"🪞 Mirrored **{len(cmds)}** command(s) into this "
                              f"server — they work instantly here.\n"
                              f"⚠️ They now exist **globally and here**, so each "
                              f"one shows **twice** in the picker until you run "
                              f"`;sync` again.")
    if mode.startswith("purge"):
        n = await purge_guild(bot, guild)
        return Result(message=f"🧹 Cleared this server's command copies. "
                              f"The {n} global command(s) still apply.")
    if mode.startswith("clean"):
        # Selective: spare the mirrors, take only what the bot no longer has.
        # For a server deliberately running mirrors that has drifted.
        keep = {c.name for c in bot.tree.get_commands()}
        removed = await prune_guild(bot, guild, keep)
        return Result(message=(f"🧹 Removed **{len(removed)}** stale command(s): "
                               + ", ".join(f"`/{r}`" for r in removed))
                              if removed else "✅ Nothing stale in this server.")
    if mode.startswith("global"):
        report = await reconcile(bot, guilds=[guild] if guild else None)
        pruned = sum(len(v) for v in report["pruned"].values())
        return Result(message=f"🔁 Synced **{report['synced']}** command(s) globally"
                              + (f", removed **{pruned}** duplicate/stale copy "
                                 f"(copies) here." if pruned else "."))

    # The default. Remove this server's copies first so nothing is left
    # shadowing the globals, then register the real list.
    dropped = 0
    if guild is not None:
        try:
            dropped = len(await prune_guild(bot, guild))
        except Exception as exc:                         # noqa: BLE001
            log.warning("[admin] could not clear guild copies: %s", exc)
    cmds = await bot.tree.sync()
    note = (f"\n🧹 Removed **{dropped}** duplicate copy/copies this server was "
            f"holding — commands were showing twice." if dropped else "")
    return Result(message=f"🔁 Registered **{len(cmds)}** command(s) globally."
                          f"{note}\n"
                          f"-# A brand-new command can take up to an hour to "
                          f"appear. `;sync mirror` makes it instant here, at the "
                          f"cost of listing everything twice.")


@register("sync", "Sync slash commands", "register globally, clear this server's copies",
          "system")
async def _sync(ctx: ActionCtx) -> Result:
    return await do_sync(ctx.bot, ctx.guild, "guild")


@register("sync_clean", "Clean stale commands", "drop copies the bot no longer has",
          "system")
async def _sync_clean(ctx: ActionCtx) -> Result:
    return await do_sync(ctx.bot, ctx.guild, "clean")


@register("sync_global", "Sync globally and prune", "sync, then clear every server's copies",
          "system")
async def _sync_global(ctx: ActionCtx) -> Result:
    return await do_sync(ctx.bot, ctx.guild, "global")


@register("sync_mirror", "Mirror commands here", "instant here — but lists everything twice",
          "system",
          confirm="Every command will then exist globally AND in this server, "
                  "so each one shows up twice in the picker until you re-sync.")
async def _sync_mirror(ctx: ActionCtx) -> Result:
    return await do_sync(ctx.bot, ctx.guild, "mirror")


async def do_reload(bot) -> Result:
    """Reload every loaded extension. Shared by `;reload` and the panel."""
    lines = []
    for cog in list(bot.extensions.keys()):
        try:
            await bot.reload_extension(cog)
            lines.append(f"✅ `{cog}`")
        except Exception as exc:                         # noqa: BLE001
            lines.append(f"❌ `{cog}`: {exc}")
    bad = sum(1 for line in lines if line.startswith("❌"))
    return Result(ok=not bad,
                  embed=_embed(f"♻️  Reload — {len(lines) - bad}/{len(lines)} ok",
                               0xE74C3C if bad else 0x2ECC71,
                               "\n".join(lines)[:4000]))


@register("reload", "Reload every cog", "re-import all extensions in place",
          "system")
async def _reload(ctx: ActionCtx) -> Result:
    return await do_reload(ctx.bot)


async def do_version(bot) -> Result:
    """Which build is running, and do all the modules agree?

    The question this answers is "did my upload actually land?" — the panel can
    extract a zip partially, leaving new cogs calling old utils.
    """
    from utils import database as db
    from utils.buildinfo import selfcheck
    rep = selfcheck(verbose=False)

    e = _embed(f"🧬 Beycord {rep['version']}",
               0x57F287 if rep["ok"] else 0xED4245)
    e.add_field(name="Python", value=rep["python"], inline=True)
    e.add_field(name="discord.py", value=rep["discord_version"] or "?", inline=True)
    e.add_field(name="DB backend", value=getattr(db, "BACKEND", "?"), inline=True)

    # Missing art. A feature whose asset is absent does not crash, it falls
    # back — so the only symptom is "the new thing doesn't work" with nothing
    # in the log. The profile card's frame went missing on a live host for
    # exactly this reason.
    if rep.get("missing_assets"):
        e.add_field(name="🖼️ MISSING ART",
                    value=("\n".join(f"`{p}`" for p in rep["missing_assets"])
                           + "\n\nRe-upload the whole zip, `assets/` included."),
                    inline=False)
    try:
        from utils import updater
        st = updater.status()
        if not st:
            value = ("**Never run on this host.**\nEither `utils/updater.py` "
                     "isn't installed, or the bot hasn't been restarted since "
                     "it was.")
        else:
            lines = []
            if st.get("sha"):
                lines.append(f"Installed: `{st['sha'][:7]}` on "
                             f"`{st.get('branch', '?')}`")
                lines.append((st.get("message") or "")[:70])
            if st.get("last_outcome"):
                lines.append(f"Last check: **{st['last_outcome']}** "
                             f"({st.get('last_check', '?')})")
            if st.get("last_detail"):
                lines.append(f"*{st['last_detail'][:150]}*")
            value = "\n".join(lines) or "No detail recorded."
        e.add_field(name="📥 Auto-update", value=value, inline=False)
    except Exception:                                    # noqa: BLE001
        pass

    # Boss dialogue. Worth surfacing because when the Gemini quota runs out the
    # only in-game symptom is "the boss sounds repetitive" — the fights are
    # identical either way, so without this the state is invisible.
    try:
        from cogs.battle.boss import gemini as _gm
        gs = _gm.status()
        label = {"live": "✅ live", "no key": "➖ no key (canned lines)",
                 "probing": "🟡 retrying", "cooling down": "🟠 canned lines",
                 "unavailable": "❌ aiohttp missing"}.get(gs["state"], gs["state"])
        lines = [f"{label} · model `{gs['model']}`"]
        if gs["retry_in"]:
            lines.append(f"Retrying in **{gs['retry_in']}s** — fights unaffected.")
        if gs["reason"]:
            lines.append(f"*{gs['reason'][:150]}*")
        e.add_field(name="🗣️ Boss dialogue", value="\n".join(lines), inline=False)
    except Exception:                                    # noqa: BLE001
        pass

    if rep["parity_gaps"]:
        e.add_field(name="❌ Store mismatch",
                    value=("MySQLStore is missing: "
                           + ", ".join(f"`{m}`" for m in rep["parity_gaps"][:8])
                           + "\n**utils/ didn't update — re-upload the zip.**"),
                    inline=False)
    if rep["stale_pyc"]:
        e.add_field(name="❌ Stale __pycache__",
                    value=("\n".join(f"`{p}`" for p in rep["stale_pyc"][:6])
                           + "\nDelete every `__pycache__` folder and restart."),
                    inline=False)
    m = maintenance()
    if m["on"]:
        e.add_field(name="🚧 Maintenance mode",
                    value=f"**ON** — {m['reason'] or 'no reason given'}",
                    inline=False)
    if rep["ok"]:
        e.add_field(name="Self-check", value="✅ All modules agree.", inline=False)
    return Result(embed=e)


@register("version", "Build info", "version, self-check, updater, dialogue",
          "system")
async def _version(ctx: ActionCtx) -> Result:
    return await do_version(ctx.bot)


@register("servers", "Server list", "verified against the API, not the cache",
          "system")
async def _servers(ctx: ActionCtx) -> Result:
    """`bot.guilds` is the gateway CACHE, not the truth.

    It is filled from the READY payload plus a GUILD_CREATE per guild, and if
    the gateway drops mid-stream — which a small container does regularly — the
    bot carries on with a short list and nothing says so. That is the failure
    that reads as "the portal says 21 and `;servers` says 10". So this asks the
    REST API too (`GET /users/@me/guilds`, the same source the Developer Portal
    counts from) and reconciles the two.
    """
    bot = ctx.bot
    cached = {g.id: g for g in bot.guilds}

    # The authoritative list. Bounded because a bot in thousands of guilds
    # would otherwise page forever inside one invocation.
    rest: dict[int, str] = {}
    rest_error = ""
    try:
        async for g in bot.fetch_guilds(limit=200):
            rest[g.id] = g.name
    except Exception as exc:                             # noqa: BLE001
        rest_error = f"{type(exc).__name__}: {exc}"
        log.warning("[admin] fetch_guilds failed: %s", exc)

    missing_from_cache = [gid for gid in rest if gid not in cached]
    missing_from_rest = [gid for gid in cached if gid not in rest]

    # Prefer the cached Guild object (it carries member_count); fall back to a
    # REST-only stub for anything the gateway never delivered.
    order = list(rest) if rest else list(cached)
    guilds = [cached[gid] for gid in order if gid in cached]
    guilds.sort(key=lambda g: g.member_count or 0, reverse=True)
    if not guilds and not rest:
        return Result.fail("Not in any servers (or still connecting)."
                           + (f"\nREST check also failed — `{rest_error}`"
                              if rest_error else ""))

    total = sum(g.member_count or 0 for g in guilds)
    lines = [f"`{i:>3}.` **{g.name}** — `{g.id}` · {g.member_count or 0:,} members"
             for i, g in enumerate(guilds, 1)]
    # Guilds the API knows about but the gateway never sent. Listed rather than
    # counted, because "which ones" is what you need in order to go and look.
    for gid in missing_from_cache:
        lines.append(f"`  ?` **{rest[gid]}** — `{gid}` · ⚠️ not in gateway cache")

    # Embed descriptions cap at 4096 characters, so page rather than let a long
    # list fail the send outright once the bot is in many servers.
    LIMIT = 3900
    pages, buf = [], ""
    for line in lines:
        if len(line) > LIMIT:
            line = line[:LIMIT - 1] + "\u2026"
        if buf and len(buf) + len(line) + 1 > LIMIT:
            pages.append(buf)
            buf = ""
        buf += line + "\n"
    if buf:
        pages.append(buf)

    headline = len(rest) if rest else len(cached)
    drifted = bool(missing_from_cache or missing_from_rest)

    embeds = []
    for n, page in enumerate(pages, 1):
        e = _embed(f"🌐 Servers ({headline})"
                   + (f" — page {n}/{len(pages)}" if len(pages) > 1 else ""),
                   0xE67E22 if drifted else 0x5865F2, page)
        if n == len(pages):
            # The reconciliation, spelled out. Without this the only symptom of
            # a half-filled cache is a number that looks wrong, and the obvious
            # conclusion ("the command is broken") is the wrong one.
            diag = [f"API: **{len(rest) if not rest_error else '?'}** · "
                    f"gateway cache: **{len(cached)}**"]
            if rest_error:
                diag.append(f"⚠️ API check failed — `{rest_error[:120]}`")
            elif missing_from_cache:
                diag.append(f"⚠️ **{len(missing_from_cache)}** server(s) the API "
                            f"knows about never arrived over the gateway. That is "
                            f"a dropped connection during startup, not a missing "
                            f"intent — restarting the bot usually refills the "
                            f"cache.")
            elif missing_from_rest:
                diag.append(f"⚠️ **{len(missing_from_rest)}** server(s) are cached "
                            f"but the API no longer lists them — the bot was "
                            f"removed while it was offline or mid-session.")
            else:
                diag.append("✅ API and cache agree.")
            shards = getattr(bot, "shard_count", None)
            if shards:
                diag.append(f"Shards: {shards}")
            intents = bot.intents
            if not intents.guilds:
                diag.append("❌ The `guilds` intent is OFF — the cache cannot fill.")
            if not intents.members:
                diag.append("ℹ️ `members` intent off — member counts are approximate.")
            e.add_field(name="Diagnosis", value="\n".join(diag), inline=False)
            e.set_footer(text=f"{total:,} members across {headline} servers")
        embeds.append(e)
    return Result(embeds=embeds)


@register("listbattles", "List active battles", "every live PvP session",
          "system")
async def _listbattles(ctx: ActionCtx) -> Result:
    cog = ctx.bot.get_cog("Battle") if ctx.bot else None
    if not cog:
        return Result.fail("❌ BattleCog is not loaded.")
    if not cog.active_battles:
        return Result(message="✅ No active battles.")
    seen, lines = set(), []
    for uid, session in cog.active_battles.items():
        if id(session) in seen:
            continue
        seen.add(id(session))
        p1, p2 = getattr(session, "p1", None), getattr(session, "p2", None)
        ch = getattr(session, "channel", None)
        lines.append(f"• {getattr(p1, 'display_name', uid)} vs "
                     f"{getattr(p2, 'display_name', '?')} in "
                     f"#{getattr(ch, 'name', '?')}")
    return Result(embed=_embed(f"⚔️  Active battles ({len(lines)})", 0x5865F2,
                               "\n".join(lines[:30])))


@register("battlereset", "Clear all battles", "frees every stuck battle slot",
          "system", confirm="Every in-progress battle is dropped.")
async def _battlereset(ctx: ActionCtx) -> Result:
    cog = ctx.bot.get_cog("Battle") if ctx.bot else None
    if not cog:
        return Result.fail("❌ BattleCog is not loaded.")
    n = len(cog.active_battles)
    cog.active_battles.clear()
    return Result(message=f"✅ Cleared **{n}** battle slot(s).")


@register("clearcache", "Clear __pycache__", "delete every compiled cache",
          "system")
async def _clearcache(ctx: ActionCtx) -> Result:
    from utils.updater import purge_pycache
    removed, freed = await asyncio.to_thread(purge_pycache)
    if not removed:
        return Result(message="✅ No `__pycache__` folders to clear.")
    return Result(message=f"🧹 Cleared **{removed}** folder(s), freeing "
                          f"**{freed / 1024:.1f} KB**.\nRestart to load new code.")


@register("updatecheck", "Update diagnostics", "ask GitHub why the updater fails",
          "system")
async def _updatecheck(ctx: ActionCtx) -> Result:
    """`version` reports THAT the last check failed. This reports WHY, by
    making the calls live from the host that holds the token — a wrong token
    and a wrong repo name produce the same 404."""
    from utils import updater as up
    d = await asyncio.to_thread(up.diagnose)
    ok = all(c["code"] == 200 for c in d["checks"][:2]) if d["checks"] else False
    e = _embed("📥 Update diagnostics", 0x57F287 if ok else 0xED4245)
    e.add_field(name="Repo", value=f"`{d['repo']}`\n-# from `{d['repo_raw'][:60]}`",
                inline=False)
    e.add_field(name="Branch", value=f"`{d['branch']}`", inline=True)
    # Length and family only — never the token itself.
    e.add_field(name="Token",
                value=(f"{d['token_family']} · {d['token_len']} chars"
                       if d["token_set"] else "❌ not set"), inline=True)
    if d["checks"]:
        e.add_field(name="GitHub says",
                    value="\n".join(f"{'✅' if c['code'] == 200 else '❌'} "
                                    f"`{c['label']:<6}` HTTP "
                                    f"**{c['code'] or 'no reply'}**"
                                    for c in d["checks"]), inline=False)
    e.add_field(name="Verdict", value=d["verdict"][:1000], inline=False)
    return Result(embed=e)


@register("carddebug", "Card render debug", "which engine served it, and why",
          "system")
async def _carddebug(ctx: ActionCtx) -> Result:
    import platform
    import shutil
    from utils import info_card
    from utils.database import load_beyblades

    blade = get_beyblade("Dynamite Belial") or next(iter(load_beyblades().values()), None)
    info_card.clear_cache()                              # force a real render
    t0 = time.time()
    buf = await info_card.render_info_card(blade)
    dt = (time.time() - t0) * 1000

    lines = [f"**engine:** `{info_card.last_engine}` · {dt:.0f} ms",
             f"**python:** `{platform.python_version()}` · **free /tmp:** "
             f"`{shutil.disk_usage('/tmp').free // 2 ** 20} MB`"]
    # find_spec rather than `import playwright`: this only needs to know
    # whether the package is installed, and importing it for the answer is
    # both slower and an unused-import warning forever.
    import importlib.util
    lines.append("**playwright pkg:** installed"
                 if importlib.util.find_spec("playwright")
                 else "**playwright pkg:** ❌ NOT INSTALLED")
    if info_card.last_playwright_error:
        err = info_card.last_playwright_error[:600]
        lines.append(f"**playwright error:**\n```\n{err}\n```")
    if buf is None:
        return Result.fail("❌ Both engines failed.\n" + "\n".join(lines))
    import discord
    return Result(message="\n".join(lines),
                  file=discord.File(buf, filename="carddebug.png"))


@register("errors", "Recent errors", "the last exceptions the bot swallowed",
          "system")
async def _errors(ctx: ActionCtx) -> Result:
    """The single highest-value thing in this panel.

    There are 21 `log.exception` sites and no `logs/` directory: until now
    every incident in this project was found by a player reporting it. The boss
    battle that froze mid-fight for weeks was one look at this away from
    obvious.
    """
    from utils import errorlog
    rows = errorlog.recent(8)
    if not rows:
        return Result(message="✅ No errors recorded since the last restart.")
    e = _embed(f"📋  Recent errors ({errorlog.count()} held)", 0xED4245)
    for r in rows:
        first = ""
        if r.get("trace"):
            # The LAST line of a traceback is the exception itself — the line
            # that says what actually went wrong.
            tail = [ln for ln in r["trace"].strip().splitlines() if ln.strip()]
            first = tail[-1] if tail else ""
        # Embed field names must be 1–256 characters. An empty name is a 400
        # that takes the whole message down, so the logger name is a fallback
        # that can never itself be empty.
        name = f"<t:{int(r['when'])}:t> · {r.get('logger') or 'bot'}"
        body = (r.get("msg") or "(no message)")[:300]
        if first:
            body += f"\n```{first[:300]}```"
        e.add_field(name=name[:256], value=body[:1024], inline=False)
    e.set_footer(text="In memory only — a restart clears this")
    return Result(embed=e)


@register("errors_clear", "Clear the error log", "empty the ring buffer",
          "system")
async def _errors_clear(ctx: ActionCtx) -> Result:
    from utils import errorlog
    n = errorlog.count()
    errorlog.clear()
    return Result(message=f"🧹 Cleared **{n}** recorded error(s).")


@register("backups", "Backups", "daily player-data snapshots, and restore",
          "system")
async def _backups(ctx: ActionCtx) -> Result:
    """The one door to the backup system.

    Returns a VIEW rather than text because the destructive half of this needs
    a picker and a confirm — typing a filename to restore 3,400 profiles is how
    the wrong file gets restored.
    """
    from cogs.snapshots.panel import SnapshotPanel
    panel = SnapshotPanel(ctx.bot, ctx.invoker_id)
    return Result(embed=panel.embed(), view=panel)


@register("backup_now", "Take a backup now", "snapshot every profile right now",
          "system")
async def _backup_now(ctx: ActionCtx) -> Result:
    import os
    cog = ctx.bot.get_cog("Snapshots") if ctx.bot else None
    if cog is None:
        return Result.fail("The snapshot cog isn't loaded.")
    try:
        path = await cog.take("admin")
    except Exception as exc:                             # noqa: BLE001
        return Result.fail(f"Couldn't take one: `{type(exc).__name__}: {exc}`")
    return Result(message=(f"💾 Saved `{os.path.basename(path)}` "
                           f"({os.path.getsize(path) // 1024 or 1} KB). "
                           f"`/admin → System → Backups` to download or "
                           f"restore it."))


@register("maintenance_on", "Maintenance mode ON", "refuse everyone but the owner",
          "system", needs=("text",),
          confirm="Every player is locked out until you turn this off.")
async def _maintenance_on(ctx: ActionCtx) -> Result:
    """The bot deploys by extracting a zip over a live install. Without this, a
    player mid-battle during a deploy simply breaks."""
    m = set_maintenance(True, ctx.text or "", ctx.invoker_id)
    return Result(message=f"🚧 Maintenance mode **ON** — players will be told: "
                          f"*{m['reason']}*")


@register("maintenance_off", "Maintenance mode OFF", "let everyone back in",
          "system")
async def _maintenance_off(ctx: ActionCtx) -> Result:
    if not maintenance()["on"]:
        return Result(message="ℹ️ Maintenance mode is already off.")
    set_maintenance(False, "", ctx.invoker_id)
    return Result(message="✅ Maintenance mode **OFF** — the bot is open again.")


# `_rank_locked` and the control-server lock went with verification in v1.18.
# The lock answered "where" — settings could only be changed from one guild —
# and it guarded four verify actions plus the leaderboard reset. With the four
# gone it guarded a single owner-only action, which `owner_only=True` was
# already doing: that is the gate that was actually doing the work.


@register("rank_settings", "Ranked settings", "ladder rules and resettable boards",
          "ranked")
async def _rank_settings(ctx: ActionCtx) -> Result:
    from utils import ranked as RK
    e = _embed("🎖️ Ranked settings", 0x5865F2)
    e.add_field(name="Entry", value="Open to everyone", inline=True)
    plural = "" if RK.PAIR_DAILY_LIMIT == 1 else "es"
    e.add_field(name="Daily pair limit",
                value=f"**{RK.PAIR_DAILY_LIMIT}** match{plural} per opponent "
                      f"per day", inline=True)
    ranked_players = sum(1 for u in (load_users() or {}).values()
                         if isinstance(u, dict) and RK.ranked_games(u) > 0)
    e.add_field(name="Players with ranked games", value=f"{ranked_players:,}",
                inline=True)
    e.add_field(name="Boards",
                value=", ".join(f"{c['emoji']} {c['label']}"
                                for c in RK.CATEGORIES.values()), inline=False)
    e.add_field(name="Resettable",
                value=", ".join(f"`{k}`" for k in RK.RESETTABLE) + ", `all`",
                inline=False)
    e.set_footer(text="Verification was removed in v1.18 — ranked is open to all.")
    return Result(embed=e)


@register("rank_reset", "Reset a leaderboard", "zeroes one board for every player",
          "ranked", needs=("text",),
          confirm="This zeroes that board for EVERY player and cannot be undone. "
                  "Coins, inventory and levels are not touched.")
async def _rank_reset(ctx: ActionCtx) -> Result:
    from utils import ranked as RK
    board = (ctx.text or "").strip().lower()
    if board not in RK.RESETTABLE and board != "all":
        opts = ", ".join(f"`{k}`" for k in RK.RESETTABLE)
        return Result.fail(f"Pick a board — {opts} or `all`.")
    keys = RK.reset_keys_for(board)

    # One load→mutate→save pass on a thread. The old confirm view called
    # `update_user` per profile, which rewrites the whole users file once per
    # player — the same shape that froze the event loop in `;giveallcoins`.
    from utils.database import _users_lock, save_users

    def _bulk() -> int:
        with _users_lock:
            users = load_users()
            changed = 0
            for prof in users.values():
                if isinstance(prof, dict) and RK.apply_reset(prof, board):
                    changed += 1
            if changed:
                save_users(users)
            return changed

    changed = await asyncio.to_thread(_bulk)
    return Result(message=f"✅ **{board}** leaderboard reset — cleared "
                          f"{', '.join(f'`{k}`' for k in keys)} for "
                          f"**{changed:,}** profiles.")
