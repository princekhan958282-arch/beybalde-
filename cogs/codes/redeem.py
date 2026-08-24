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
from utils.database import add_avatar_to_inventory, add_beyblade_to_inventory, mutate_user

from .code_store import REDEEM_PATH, load, make_code, normalise, redeem_lock, save

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
        elif kind in ("avatar", "avatarcard", "card"):
            from cogs.avatar import avatar_engine
            cards = avatar_engine.get_all_avatars()
            vl = value.lower()
            exact = [a for a in cards if a["id"].lower() == vl or a["name"].lower() == vl]
            hits = exact or [a for a in cards if vl in a["name"].lower()]
            if not hits:
                return None, f"No avatar matching **{value}**. `;avatars` lists them."
            if len(hits) > 1:
                shown = ", ".join(f"**{a['name']}**" for a in hits[:8])
                more = f" …and {len(hits) - 8} more" if len(hits) > 8 else ""
                return None, (f"**{value}** matches {len(hits)} avatars: {shown}{more}. "
                              f"Use the full name or the id.")
            av = hits[0]
            rewards.append({"kind": "avatar", "value": av["id"], "label": av["name"]})
        elif kind in ("bossbey", "bosscopy", "bossblade", "boss"):
            # boss beys are deliberately absent from beyblades.json (see
            # boss_info.py), so they can't go through the `blade:` path above
            # — this is the only route a code has onto one. `value` may carry
            # an optional grade after a second colon: `bossbey:Argus:Perfect`.
            from cogs.battle.boss import boss_copy as bcopy
            from cogs.battle.boss import boss_info as binfo
            name_part, _, grade_part = value.partition(":")
            name_l = name_part.strip().lower()
            matches = [k for k, prof in binfo.REGISTRY.items()
                      if name_l == k or name_l in prof["name"].lower()]
            if not matches:
                # `.name` carries an " org" suffix internally (the marker that
                # distinguishes the real blade from a copy — see boss_info.py) —
                # stripped here same as roll_copy() strips it for a copy's own
                # display name, so the error doesn't leak the internal marker.
                names = ", ".join(p["name"].replace(" org", "").strip()
                                  for p in binfo.REGISTRY.values())
                return None, f"No boss bey matching **{name_part}**. Try: {names}."
            if len(matches) > 1:
                return None, (f"**{name_part}** matches more than one boss bey: "
                              f"{', '.join(matches)}.")
            boss_key = matches[0]
            grade = None
            if grade_part.strip():
                g = grade_part.strip().title()
                if g not in bcopy.GRADE_ORDER:
                    return None, ("Grade must be one of: "
                                  + ", ".join(bcopy.GRADE_ORDER))
                grade = g
            rewards.append({"kind": "bossbey", "value": boss_key, "grade": grade})
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
        elif r["kind"] == "avatar":
            bits.append(f"🖼️ **{r.get('label', r['value'])}**")
        elif r["kind"] == "bossbey":
            grade = f" ({r['grade']})" if r.get("grade") else ""
            bits.append(f"👹 **{r['value'].title()}** boss copy{grade}")
    return " · ".join(bits)


async def grant(user_id: int, rewards: list[dict]) -> list[str]:
    """Apply rewards. Returns human-readable lines of what landed."""
    got = []
    # Coins are accumulated as a delta and applied at the end through
    # `mutate_user`, NOT held as a profile snapshot. A snapshot taken here
    # would be written back below AFTER `add_beyblade_to_inventory` has
    # already written the inventory under the user lock — silently erasing
    # any blade from a code that grants coins and a blade together.
    coin_delta = 0

    for r in rewards:
        if r["kind"] == "coins":
            coin_delta += int(r["value"])
            got.append(f"🪙 **+{r['value']:,}** Beycoins")
        elif r["kind"] == "casino":
            await casino_wallet.credit(user_id, r["value"])
            got.append(f"🎰 **+{r['value']:,}** casino coins")
        elif r["kind"] == "blade":
            if add_beyblade_to_inventory(user_id, r["value"]):
                got.append(f"🌀 **{r['value']}** added to your collection")
            else:
                # Say so. Announcing a blade that was never granted is worse
                # than refusing it — the code is spent either way.
                got.append(f"🎒 **{r['value']}** could not be added — your "
                           f"inventory is full (`;beyslots`)")
        elif r["kind"] == "premium":
            try:
                await casino_premium.grant_premium(user_id, r["value"])
                got.append(f"👑 {casino_premium.PACKS[r['value']]['display']} pass activated")
            except Exception:
                got.append("👑 premium pass could not be applied — tell an admin")
        elif r["kind"] == "avatar":
            label = r.get("label", r["value"])
            if add_avatar_to_inventory(user_id, r["value"]):
                got.append(f"🖼️ **{label}** added to your avatars")
            else:
                got.append(f"🖼️ **{label}** — you already own this one")
        elif r["kind"] == "bossbey":
            from cogs.battle.boss import boss_copy as bcopy
            from cogs.battle.boss import boss_info as binfo
            prof = binfo.REGISTRY.get(r["value"])
            if prof is None:
                got.append("👹 boss copy could not be granted — that boss no longer exists")
            else:
                rolled = bcopy.roll_copy(prof, forced_grade=r.get("grade"))
                bcopy.add_copy(user_id, rolled)
                got.append(f"👹 **{rolled['name']}** ({rolled['grade']}) boss copy added")

    if coin_delta:
        mutate_user(user_id,
                    lambda prof: prof.__setitem__(
                        "coins", int(prof.get("coins", 0) or 0) + coin_delta))
    return got


def create_code(rewards: list[dict], uses: int, days: int, note: str,
                created_by: int) -> tuple[str, dict]:
    """Mint a code from an already-validated reward list. Returns (key, entry).

    The one place that actually writes a new code, so the typed `;code
    create coins:5000 blade:Dranzer` path and the picker in
    `cogs/codes/builder.py` mint identically-shaped entries — neither can
    drift from the other because there is only one implementation.
    """
    key = normalise(make_code("BEY"))
    with redeem_lock:
        data = _load()
        while key in data["codes"]:
            key = normalise(make_code("BEY"))
        entry = {
            "rewards": rewards, "max_uses": uses,
            "expires": (time.time() + days * 86400) if days else 0,
            "created_at": time.time(), "created_by": created_by,
            "claimed_by": {}, "note": note, "revoked": False,
            "display": _pretty(key),
        }
        data["codes"][key] = entry
        save(REDEEM_PATH, data)
    return key, entry


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

    # ── admin code management moved to /admin → 🎟️ Codes (v1.13) ─────────
    #
    # `;codeadmin create/list/revoke` lived here, each subcommand carrying
    # its own hand-rolled `ctx.author.id != MASTER_ID` check. The logic did
    # not move: `cogs/admin/actions.py` calls `parse_rewards`, `describe`,
    # `_load` and `_pretty` from this module, so there is still exactly one
    # implementation of what a redeem code is.


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
