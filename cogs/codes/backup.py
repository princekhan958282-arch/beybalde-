"""
backup.py  —  🔐 Account backup & restore codes

The problem: a player loses access to their Discord account, or their data gets
rolled back, and there is no way to prove "that 76-blade collection was mine".

The solution: every player can generate a private recovery code. The bot keeps
a live snapshot behind that code and refreshes it automatically, so the code
always points at their current progress rather than whatever they had on the
day they generated it.

    ;backup            → show your code (DM'd, never posted in channel)
    ;backup new        → regenerate (invalidates the old one)
    ;restore <code>    → move that progress onto the account you're using now

── The exploit this has to prevent ──────────────────────────────────────────
A naive version is a coin printer: generate a code, restore it on an alt, and
now two accounts hold the same 20M coins and the same collection. Every restore
here is therefore a TRANSFER, not a copy:

  * the source account is reset to a fresh profile and its casino wallet zeroed
  * the code is consumed on use and a fresh one is issued to the new account
  * restoring onto an account that already has progress requires confirmation,
    because that progress is about to be overwritten
  * every restore is logged with both user IDs for `;backup audit`

Restoring onto the same account you backed up from is a no-op safety valve —
it just refreshes the snapshot instead of wiping you.
"""

import asyncio
import logging
import time
from typing import Optional

import discord
from discord.ext import commands, tasks

log = logging.getLogger("beyblade_bot")

from cogs.casino import casino_wallet
from utils.database import USER_STORE, _default_profile, get_user, update_user

from .code_store import BACKUP_PATH, backup_lock, load, make_code, normalise, save

MASTER_ID = 956773141265391676

# Snapshots older than this get refreshed by the background task below, so a
# code always hands back roughly-current progress even if the player hasn't
# run ;backup in weeks. Without this the whole feature is a trap: you lose your
# account and recover the version of yourself from three months ago.
SNAPSHOT_STALE_AFTER = 6 * 3600
REFRESH_INTERVAL_MIN = 30


def _blank() -> dict:
    return {"codes": {}, "by_user": {}, "log": []}


def _load() -> dict:
    data = load(BACKUP_PATH, _blank)
    data.setdefault("codes", {})
    data.setdefault("by_user", {})
    data.setdefault("log", [])
    return data


def _pretty(key: str) -> str:
    if key.startswith("BKP") and len(key) == 15:
        rest = key[3:]
        return "BKP-" + "-".join(rest[i:i + 4] for i in range(0, 12, 4))
    return key


async def _snapshot(user_id: int) -> dict:
    """Everything that makes up a player, in one blob."""
    profile = get_user(user_id)
    casino  = await casino_wallet.get_balance(user_id)
    prem = None
    try:
        data = await casino_wallet._load_async()
        prem = data.get(str(user_id), {}).get("premium")
    except Exception:
        pass
    return {
        "profile":     profile,
        "casino":      casino,
        "premium":     prem,
        "taken_at":    time.time(),
        "source_user": user_id,
    }


def _summary(snap: dict) -> str:
    p = snap.get("profile") or {}
    return (f"🪙 {p.get('coins', 0):,} · 🎰 {snap.get('casino', 0):,} · "
            f"L{p.get('level', 0)} · {len(p.get('inventory') or [])} blades · "
            f"{p.get('wins', 0)}W/{p.get('losses', 0)}L")


async def _apply(snap: dict, target_id: int) -> None:
    """Write a snapshot onto an account."""
    profile = dict(snap.get("profile") or {})
    # The schema carries user_id inside the blob; re-point it or the restored
    # profile still claims to belong to the old account.
    profile["user_id"] = str(target_id)
    update_user(target_id, profile)

    async with casino_wallet._lock:
        data = casino_wallet._load()
        uid  = str(target_id)
        data.setdefault(uid, {})
        data[uid]["balance"] = int(snap.get("casino", 0))
        if snap.get("premium"):
            data[uid]["premium"] = snap["premium"]
        casino_wallet._save(data)


async def _wipe(user_id: int) -> None:
    """Reset the source account. This is what stops restore being a dupe."""
    USER_STORE.put_one(str(user_id), _default_profile(str(user_id)))
    async with casino_wallet._lock:
        data = casino_wallet._load()
        uid  = str(user_id)
        if uid in data:
            data[uid]["balance"] = 0
            data[uid].pop("premium", None)
            casino_wallet._save(data)


class ConfirmRestore(discord.ui.View):
    """Restoring over existing progress destroys it — make them say so."""

    def __init__(self, owner_id: int):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.value: Optional[bool] = None

    @discord.ui.button(label="Overwrite my account", emoji="⚠️",
                       style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("Not your restore.",
                                                           ephemeral=True)
        self.value = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("Not your restore.",
                                                           ephemeral=True)
        self.value = False
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


class BackupCog(commands.Cog, name="Backup"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.refresh_snapshots.start()

    def cog_unload(self):
        self.refresh_snapshots.cancel()

    # ── Keep every live snapshot current ─────────────────────────────────────
    @tasks.loop(minutes=REFRESH_INTERVAL_MIN)
    async def refresh_snapshots(self):
        """Re-snapshot any unused code whose data has gone stale."""
        try:
            with backup_lock:
                data  = _load()
                stale = [
                    (key, entry["owner"])
                    for key, entry in data["codes"].items()
                    if not entry.get("used_by")
                    and entry.get("owner")
                    and time.time() - entry.get("updated_at", 0) > SNAPSHOT_STALE_AFTER
                ]
            if not stale:
                return

            # Snapshotting touches the DB and the wallet file, so build them
            # all outside the lock and write once.
            fresh = {}
            for key, owner in stale:
                try:
                    fresh[key] = await _snapshot(int(owner))
                except Exception:
                    continue
                await asyncio.sleep(0)      # yield between users

            with backup_lock:
                data = _load()
                for key, snap in fresh.items():
                    entry = data["codes"].get(key)
                    if entry and not entry.get("used_by"):
                        entry["snapshot"]   = snap
                        entry["updated_at"] = time.time()
                # Drop consumed codes once they're a month old; the restore log
                # keeps the audit trail, so nothing is lost by pruning them.
                cutoff = time.time() - 30 * 86400
                for key in [k for k, e in data["codes"].items()
                            if e.get("used_at") and e["used_at"] < cutoff]:
                    data["codes"].pop(key, None)
                save(BACKUP_PATH, data)
            log.info(f"[backup] refreshed {len(fresh)} stale recovery snapshots")
        except Exception as exc:
            log.warning(f"[backup] snapshot refresh failed: {exc}")

    @refresh_snapshots.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ── ;backup ──────────────────────────────────────────────────────────────
    @commands.group(name="backup", aliases=["recovery", "backupcode"],
                    invoke_without_command=True)
    async def backup(self, ctx: commands.Context):
        """🔐 Get your account recovery code (sent by DM)."""
        uid  = str(ctx.author.id)
        snap = await _snapshot(ctx.author.id)

        with backup_lock:
            data = _load()
            key  = data["by_user"].get(uid)

            if key and key in data["codes"] and not data["codes"][key].get("used_by"):
                data["codes"][key]["snapshot"] = snap
                data["codes"][key]["updated_at"] = time.time()
                fresh = False
            else:
                key = normalise(make_code("BKP"))
                while key in data["codes"]:
                    key = normalise(make_code("BKP"))
                data["codes"][key] = {
                    "owner":      ctx.author.id,
                    "snapshot":   snap,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                    "used_by":    None,
                    "used_at":    None,
                }
                data["by_user"][uid] = key
                fresh = True
            save(BACKUP_PATH, data)

        e = discord.Embed(
            title="🔐  Your Recovery Code",
            description=(
                f"## `{_pretty(key)}`\n\n"
                f"**{_summary(snap)}**\n\n"
                "If you ever lose this account, run `;restore <code>` from the "
                "new one and this progress moves across."
            ),
            color=0xe74c3c,
        )
        e.add_field(
            name="⚠️ Keep it private",
            value=("Anyone with this code can take your account's progress. "
                   "Never post it in a channel or send it to a 'staff member' "
                   "who asks — real staff never will."),
            inline=False,
        )
        e.add_field(
            name="Good to know",
            value=("• The snapshot refreshes every time you run `;backup`\n"
                   "• Restoring **moves** progress — the old account is reset\n"
                   "• `;backup new` invalidates this code and issues another"),
            inline=False,
        )
        e.set_footer(text="Snapshot taken just now")

        try:
            await ctx.author.send(embed=e)
            note = ("📬 Sent your recovery code by DM."
                    if fresh else "📬 DM'd you your code and refreshed the snapshot.")
            await ctx.send(f"{ctx.author.mention} {note}")
        except discord.Forbidden:
            await ctx.send(
                f"{ctx.author.mention} ❌ I couldn't DM you — your DMs are closed. "
                f"Open DMs for this server and run `;backup` again.\n"
                f"*(Recovery codes are never posted in a channel.)*")

    @backup.command(name="new", aliases=["regen", "reset"])
    async def backup_new(self, ctx: commands.Context):
        """Invalidate the old code and issue a fresh one."""
        uid = str(ctx.author.id)
        with backup_lock:
            data = _load()
            old  = data["by_user"].pop(uid, None)
            if old and old in data["codes"] and not data["codes"][old].get("used_by"):
                data["codes"].pop(old, None)
            save(BACKUP_PATH, data)
        await ctx.invoke(self.backup)

    @backup.command(name="audit")
    async def backup_audit(self, ctx: commands.Context):
        """Master only — every restore that has happened."""
        if ctx.author.id != MASTER_ID:
            return
        log = _load()["log"][:15]
        if not log:
            return await ctx.send("No restores have happened yet.")
        lines = [
            f"<t:{int(r['at'])}:d> <@{r['from']}> → <@{r['to']}>  ·  {r['summary']}"
            for r in log
        ]
        await ctx.send(embed=discord.Embed(
            title="🔐  Restore Log", description="\n".join(lines), color=0x9b59b6))

    # ── ;restore ─────────────────────────────────────────────────────────────
    @commands.command(name="restore", aliases=["recover"])
    async def restore(self, ctx: commands.Context, code: str = None):
        """🔐 Move a backed-up account onto the one you're using now."""
        if not code:
            return await ctx.send(
                "🔐 Usage: `;restore <code>`\nGet a code with `;backup`.")

        # Never leave a recovery code sitting in channel history.
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

        key = normalise(code)
        # backup_lock is a threading.Lock — resolve under it, reply outside.
        error = None
        snap = None
        with backup_lock:
            data  = _load()
            entry = data["codes"].get(key)
            if entry is None:
                error = f"{ctx.author.mention} ❌ That recovery code isn't valid."
            elif entry.get("used_by"):
                error = (f"{ctx.author.mention} ❌ That code was already used "
                         f"<t:{int(entry['used_at'])}:R>. Codes work once.")
            else:
                snap = entry["snapshot"]
                source = int(entry.get("owner") or snap.get("source_user") or 0)

        if error:
            return await ctx.send(error)

        if source <= 0:
            # Without a real source we can't wipe anything, and restoring
            # anyway would be a straight duplication.
            return await ctx.send(
                f"{ctx.author.mention} ❌ That code is malformed — it has no "
                f"source account. Ask an admin to check `;backup audit`.")

        # ── Same account: refresh instead of wiping them ─────────────────────
        if source == ctx.author.id:
            return await ctx.send(
                f"{ctx.author.mention} ✅ That's this account's own code — "
                f"nothing to restore. Run `;backup` to refresh the snapshot.")

        # ── Overwriting real progress needs explicit consent ─────────────────
        mine = get_user(ctx.author.id)
        has_progress = bool(mine.get("inventory")) or mine.get("xp", 0) > 0 \
            or mine.get("coins", 0) > 0
        if has_progress:
            warn = discord.Embed(
                title="⚠️  This account already has progress",
                description=(
                    f"**Restoring will replace it.**\n\n"
                    f"**Current:** {_summary({'profile': mine, 'casino': await casino_wallet.get_balance(ctx.author.id)})}\n"
                    f"**Incoming:** {_summary(snap)}\n\n"
                    f"The account the code came from will be reset."
                ),
                color=0xe74c3c,
            )
            view = ConfirmRestore(ctx.author.id)
            msg  = await ctx.send(f"{ctx.author.mention}", embed=warn, view=view)
            await view.wait()
            if not view.value:
                return await msg.edit(content=f"{ctx.author.mention} Restore cancelled.",
                                      embed=None, view=None)

        # ── Consume the code inside the lock, before touching any data ───────
        with backup_lock:
            data  = _load()
            entry = data["codes"].get(key)
            if entry is None or entry.get("used_by"):
                taken = True
            else:
                taken = False
                entry["used_by"] = ctx.author.id
                entry["used_at"] = time.time()
                data["by_user"].pop(str(source), None)
                data["log"].insert(0, {
                    "from": source, "to": ctx.author.id,
                    "at": time.time(), "summary": _summary(snap),
                })
                data["log"] = data["log"][:200]
                save(BACKUP_PATH, data)

        if taken:
            return await ctx.send(
                f"{ctx.author.mention} ❌ That code was just used by someone else.")

        # Order matters: wipe the source FIRST. If anything fails after this,
        # the worst case is a player who lost the old account and has to ask an
        # admin — not two accounts holding the same coins.
        await _wipe(source)
        await _apply(snap, ctx.author.id)

        e = discord.Embed(
            title="🔐  Account Restored",
            description=(
                f"Progress has been moved onto this account.\n\n"
                f"**{_summary(snap)}**\n\n"
                f"The old account (<@{source}>) has been reset — this was a "
                f"transfer, not a copy."
            ),
            color=0x2ecc71,
        )
        e.set_footer(text="Run ;backup to generate a fresh recovery code for this account")
        await ctx.send(f"{ctx.author.mention}", embed=e)
