"""
redeem.py  —  🎟️ Redeem codes

Admin-issued reward codes. Useful for events, giveaways, apology payouts after
an outage, or partner servers.

    ;code create coins:5000 uses:100 days:7   (master only)
    ;code list                                (master only)
    ;code revoke <code>                       (master only)
    ;redeem <code>                            (everyone)

Reward spec is a comma-separated string so one code can grant several things:

    coins:5000              → Beycoins
    casino:2000             → casino coins
    blade:Dranzer           → a Beyblade
    premium:pro             → a 7-day premium pass
    coins:5000,casino:1000  → both

Each code tracks who redeemed it, so nobody can claim the same code twice even
if it has uses left.
"""

import time
from typing import Optional

import discord
from discord.ext import commands

from cogs.casino import casino_premium, casino_wallet
from cogs.economy.profile import fuzzy_find_beyblade
from utils.database import add_beyblade_to_inventory, get_user, update_user
from utils.mobile_ui import MobileListView

from .code_store import (
    BACKUP_PATH,
    REDEEM_PATH,
    load,
    make_code,
    normalise,
    redeem_lock,
    save,
)

MASTER_ID = 956773141265391676

# One mistyped zero shouldn't be able to mint a trillion coins into an economy
# whose entire supply is ~41M.
MAX_REWARD_AMOUNT = 10_000_000


def _blank() -> dict:
    return {"codes": {}}


def _load() -> dict:
    data = load(REDEEM_PATH, _blank)
    data.setdefault("codes", {})
    return data


# ── Reward spec parsing ───────────────────────────────────────────────────────

def parse_rewards(spec: str) -> tuple[Optional[list[dict]], Optional[str]]:
    """'coins:5000,blade:Dranzer' -> [{'kind':..,'value':..}] or (None, error)."""
    rewards = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            return None, f"`{part}` should look like `kind:value`."
        kind, _, value = part.partition(":")
        kind, value = kind.strip().lower(), value.strip()

        if kind in ("coins", "beycoins", "coin", "casino", "casinocoins", "cc"):
            if not value.isdigit() or int(value) <= 0:
                return None, f"`{part}` needs a positive amount."
            if int(value) > MAX_REWARD_AMOUNT:
                return None, (f"`{part}` is above the {MAX_REWARD_AMOUNT:,} cap — "
                              f"check for a stray zero.")
            rewards.append({
                "kind": "coins" if kind in ("coins", "beycoins", "coin") else "casino",
                "value": int(value),
            })
        elif kind in ("blade", "bey", "beyblade"):
            blade = fuzzy_find_beyblade(value)
            if blade is None:
                return None, f"No Beyblade matching **{value}**."
            rewards.append({"kind": "blade", "value": blade["name"]})
        elif kind in ("premium", "pass"):
            key = value.lower()
            if key not in casino_premium.PACKS:
                return None, ("Premium must be one of: "
                              + ", ".join(f"`{k}`" for k in casino_premium.PACKS))
            rewards.append({"kind": "premium", "value": key})
        else:
            return None, f"Unknown reward type `{kind}`."

    if not rewards:
        return None, "No rewards given."
    return rewards, None


def describe(rewards: list[dict]) -> str:
    bits = []
    for r in rewards:
        if r["kind"] == "coins":
            bits.append(f"🪙 {r['value']:,} Beycoins")
        elif r["kind"] == "casino":
            bits.append(f"🎰 {r['value']:,} casino coins")
        elif r["kind"] == "blade":
            bits.append(f"🌀 **{r['value']}**")
        elif r["kind"] == "premium":
            bits.append(f"👑 {casino_premium.PACKS[r['value']]['display']} pass")
    return " · ".join(bits)


async def grant(user_id: int, rewards: list[dict]) -> list[str]:
    """Apply rewards. Returns human-readable lines of what landed."""
    got = []
    profile = get_user(user_id)
    dirty = False

    for r in rewards:
        if r["kind"] == "coins":
            profile["coins"] = profile.get("coins", 0) + r["value"]
            dirty = True
            got.append(f"🪙 **+{r['value']:,}** Beycoins")
        elif r["kind"] == "casino":
            await casino_wallet.credit(user_id, r["value"])
            got.append(f"🎰 **+{r['value']:,}** casino coins")
        elif r["kind"] == "blade":
            add_beyblade_to_inventory(user_id, r["value"])
            got.append(f"🌀 **{r['value']}** added to your collection")
        elif r["kind"] == "premium":
            try:
                await casino_premium.grant_premium(user_id, r["value"])
                got.append(f"👑 {casino_premium.PACKS[r['value']]['display']} pass activated")
            except Exception:
                got.append("👑 premium pass could not be applied — tell an admin")

    if dirty:
        update_user(user_id, profile)
    return got


# ── Cog ───────────────────────────────────────────────────────────────────────

class RedeemCog(commands.Cog, name="Codes"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── ;redeem ──────────────────────────────────────────────────────────────
    # NOTE: deliberately no ";code" alias. It used to be here, and it meant
    # ";code create coins:5000" hit this player command and answered
    # "that code isn't valid" instead of reaching the admin group below.
    @commands.command(name="redeem", aliases=["claimcode", "usecode"])
    async def redeem(self, ctx: commands.Context, code: str = None):
        """🎟️ Redeem a reward code."""
        if not code:
            return await ctx.send(
                "🎟️ Usage: `;redeem <code>`\n"
                "Lost your account instead? `;backup` and `;restore` handle that.")

        key = normalise(code)
        uid = str(ctx.author.id)

        # redeem_lock is a threading.Lock. Nothing may be awaited while it is
        # held — an await inside would park this coroutine with the lock still
        # taken, and the next redeem would block the whole event loop on it.
        # So: decide everything under the lock, then talk to Discord outside.
        error   = None
        rewards = None
        note    = ""
        with redeem_lock:
            data  = _load()
            entry = data["codes"].get(key)

            if entry is None:
                error = "❌ That code isn't valid."
            elif entry.get("revoked"):
                error = "❌ That code has been revoked."
            elif entry.get("expires") and time.time() > entry["expires"]:
                error = "⏰ That code has expired."
            elif uid in entry.get("claimed_by", {}):
                error = "❌ You've already redeemed this code."
            elif entry.get("max_uses", 0) and \
                    len(entry.get("claimed_by", {})) >= entry["max_uses"]:
                error = "❌ That code has been fully claimed."
            else:
                # Reserve the slot inside the lock so two simultaneous redeems
                # can't both slip past the uses check.
                entry.setdefault("claimed_by", {})[uid] = time.time()
                save(REDEEM_PATH, data)
                rewards = entry["rewards"]
                note    = entry.get("note", "")

        if error:
            return await ctx.send(error)

        lines = await grant(ctx.author.id, rewards)

        e = discord.Embed(
            title="🎟️  Code Redeemed!",
            description="\n".join(lines) or "Nothing to grant.",
            color=0x2ecc71,
        )
        if note:
            e.set_footer(text=note)
        await ctx.send(f"{ctx.author.mention}", embed=e)

    # ── ;code (admin) ────────────────────────────────────────────────────────
    @commands.group(name="codeadmin", aliases=["codes", "code"],
                    invoke_without_command=True, hidden=True)
    async def codeadmin(self, ctx: commands.Context):
        if ctx.author.id != MASTER_ID:
            return
        await ctx.send(
            "🎟️ **Code admin**\n"
            "`;codes create <spec> [uses:N] [days:N] [note:...]`\n"
            "`;codes list` · `;codes revoke <code>` · `;codes info <code>`\n\n"
            "Spec examples: `coins:5000` · `casino:2000` · `blade:Dranzer` · "
            "`premium:pro` · `coins:5000,casino:1000`")

    @codeadmin.command(name="create", aliases=["new", "make"])
    async def code_create(self, ctx: commands.Context, *, args: str = None):
        if ctx.author.id != MASTER_ID:
            return
        if not args:
            return await ctx.send("Usage: `;codes create coins:5000 uses:100 days:7`")

        # Pull the note out FIRST and take the rest of the string with it —
        # scanning left-to-right and breaking on note: meant "note:hi uses:50"
        # silently dropped uses:50.
        note = ""
        body = args
        if "note:" in args.lower():
            idx  = args.lower().index("note:")
            note = args[idx + 5:].strip()
            body = args[:idx]

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
            return await ctx.send(f"❌ {err}")

        key = normalise(make_code("BEY"))
        with redeem_lock:
            data = _load()
            while key in data["codes"]:
                key = normalise(make_code("BEY"))
            data["codes"][key] = {
                "rewards":    rewards,
                "max_uses":   uses,
                "expires":    (time.time() + days * 86400) if days else 0,
                "created_at": time.time(),
                "created_by": ctx.author.id,
                "claimed_by": {},
                "note":       note,
                "revoked":    False,
                "display":    _pretty(key),
            }
            save(REDEEM_PATH, data)

        e = discord.Embed(
            title="🎟️  Code Created",
            description=f"## `{_pretty(key)}`\n\n{describe(rewards)}",
            color=0x2ecc71,
        )
        e.add_field(name="Uses",
                    value=("unlimited" if not uses else f"{uses}"), inline=True)
        e.add_field(name="Expires",
                    value=("never" if not days else f"<t:{int(time.time() + days * 86400)}:R>"),
                    inline=True)
        if note:
            e.add_field(name="Note", value=note, inline=False)
        e.set_footer(text="Players claim it with ;redeem <code>")
        await ctx.send(embed=e)

    @codeadmin.command(name="list")
    async def code_list(self, ctx: commands.Context):
        if ctx.author.id != MASTER_ID:
            return
        data  = _load()
        codes = sorted(data["codes"].items(),
                       key=lambda kv: -kv[1].get("created_at", 0))
        if not codes:
            return await ctx.send("No codes exist yet.")

        def render(item, idx):
            key, c = item
            used = len(c.get("claimed_by", {}))
            cap  = c.get("max_uses", 0)
            state = ("🚫 revoked" if c.get("revoked")
                     else "⏰ expired" if c.get("expires") and time.time() > c["expires"]
                     else "✅ live")
            return (f"**{idx + 1}.** `{c.get('display', key)}` {state}\n"
                    f"　{describe(c['rewards'])[:60]} · "
                    f"{used}/{cap if cap else '∞'} used")

        def option_of(item):
            key, c = item
            return (c.get("display", key),
                    f"{len(c.get('claimed_by', {}))} claims", "🎟️")

        async def detail(interaction, item):
            key, c = item
            e = discord.Embed(title=f"🎟️  {c.get('display', key)}",
                              description=describe(c["rewards"]), color=0x9b59b6)
            e.add_field(name="Claims",
                        value=f"{len(c.get('claimed_by', {}))}/"
                              f"{c.get('max_uses') or '∞'}", inline=True)
            e.add_field(name="Expires",
                        value=("never" if not c.get("expires")
                               else f"<t:{int(c['expires'])}:R>"), inline=True)
            if c.get("note"):
                e.add_field(name="Note", value=c["note"], inline=False)
            await interaction.response.send_message(embed=e, ephemeral=True)

        view = MobileListView(
            owner=ctx.author, title="🎟️  Redeem Codes", items=codes,
            render=render, option_of=option_of, detail=detail,
            detail_placeholder="🔍 Open a code…", colour=0x9b59b6,
        )
        view.message = await ctx.send(embed=view.embed(), view=view)

    @codeadmin.command(name="revoke", aliases=["delete", "kill"])
    async def code_revoke(self, ctx: commands.Context, code: str = None):
        if ctx.author.id != MASTER_ID:
            return
        if not code:
            return await ctx.send("Usage: `;codes revoke <code>`")
        key = normalise(code)
        with redeem_lock:
            data  = _load()
            found = key in data["codes"]
            if found:
                data["codes"][key]["revoked"] = True
                save(REDEEM_PATH, data)
        if not found:
            return await ctx.send("❌ No such code.")
        await ctx.send(f"🚫 Revoked `{_pretty(key)}` — it can't be claimed any more.")


def _pretty(key: str) -> str:
    """BEYM9R5JQH8US9Q -> BEY-M9R5-JQH8-US9Q"""
    if key.startswith("BEY") and len(key) == 15:
        rest = key[3:]
        return "BEY-" + "-".join(rest[i:i + 4] for i in range(0, 12, 4))
    return key


async def setup(bot: commands.Bot):
    await bot.add_cog(RedeemCog(bot))
    from .backup import BackupCog
    await bot.add_cog(BackupCog(bot))
