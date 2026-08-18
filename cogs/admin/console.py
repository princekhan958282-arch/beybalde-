"""
console.py — every admin action behind one `/admin` command.

Why one command instead of subcommands
--------------------------------------
Discord lists subcommands FLAT in the command picker: `/tournament create`,
`/tournament cancel`, `/tournament ban_player` … each gets its own line, so a
group with thirteen admin subcommands puts thirteen lines in front of every
player, most of which they can't use. Declaring the group differently doesn't
change that — it's how the client renders subcommands.

A single command with a choice parameter renders as ONE line. Picking the
action happens in a dropdown after `/admin`, which is the behaviour that was
actually wanted.

The cost, and how it's handled
------------------------------
One command can't give each action its own typed parameters, so the parameters
here are generic and optional. That would normally mean silent misuse — call
`ban` with no user and something confusing happens. Instead every action
declares what it needs in ACTIONS, and dispatch refuses with a precise message
naming the missing parameter before any handler runs.

No game or tournament logic lives here. Every handler delegates to the same
service methods the old subcommands used, so behaviour can't drift.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

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


# action -> (label, description, required parameter names)
# `required` is what makes generic parameters safe: it is checked before the
# handler runs, so a missing id can never reach the service layer.
ACTIONS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # ── tournament ──
    #
    # Four, down from fifteen. The other eleven — create, end, pause, resume,
    # reschedule, replace_player, unban_player, set_reward, broadcast,
    # announce, announce_history — only meant anything for the scheduled
    # bracket system deleted in v1.12. A self-serve lobby creates itself from
    # `/tournament`, runs itself, and has no schedule to reschedule.
    "start":         ("▶️ Force-start the tournament",
                      "starts the open lobby now, short-handed", ()),
    "cancel":        ("✖️ Cancel the tournament", "closes the open lobby", ()),
    "force_win":     ("⚖️ Force a match winner",
                      "user = winner", ("user",)),
    "ban_player":    ("🚫 Ban from tournaments",
                      "user, text = reason", ("user", "text")),
    # ── casino ──
    "casino_give":   ("🪙 Give casino coins", "user, amount", ("user", "amount")),
    "casino_take":   ("💸 Take casino coins", "user, amount", ("user", "amount")),
}

PARAM_HELP = {
    "target": "`target` (a tournament or match id)",
    "user":   "`user`",
    "user2":  "`user2`",
    "amount": "`amount`",
    "text":   "`text`",
    "extra":  "`extra`",
}

class AdminConsole(commands.Cog, name="Admin console"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── helpers ──────────────────────────────────────────────────────────────

    def _tournament_cog(self):
        return self.bot.get_cog("Tournaments")

    async def _reply(self, interaction: discord.Interaction, msg: str,
                     ephemeral: bool = False) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(msg, ephemeral=ephemeral)

    # ── the one command ──────────────────────────────────────────────────────

    @app_commands.command(
        name="admin",
        description="[Admin] Every admin action — pick one from the list")
    @app_commands.describe(
        action="What to do",
        target="Tournament or match id, where the action needs one",
        user="The player the action applies to",
        user2="Second player (only for swapping one player for another)",
        amount="A number — coins, days, hours or max players",
        text="Free text — a name, reason, message or reward value",
        extra="Mode (single/double/round_robin) or reward type (role/currency/item)")
    async def admin(self, interaction: discord.Interaction, action: str,
                    target: Optional[str] = None,
                    user: Optional[discord.Member] = None,
                    user2: Optional[discord.Member] = None,
                    amount: Optional[int] = None,
                    text: Optional[str] = None,
                    extra: Optional[str] = None) -> None:
        if not is_admin(interaction.user):
            return await interaction.response.send_message("Not authorized.",
                                                           ephemeral=True)

        key = (action or "").strip().lower()
        spec = ACTIONS.get(key)
        if spec is None:
            return await interaction.response.send_message(
                f"Unknown action `{action}` — pick one from the dropdown.",
                ephemeral=True)
        label, _hint, required = spec

        supplied = {"target": target, "user": user, "user2": user2,
                    "amount": amount, "text": text, "extra": extra}
        missing = [p for p in required if supplied.get(p) in (None, "")]
        if missing:
            # Named up front rather than letting a None reach the service and
            # produce something vague.
            return await interaction.response.send_message(
                f"**{label}** also needs "
                + ", ".join(PARAM_HELP[p] for p in missing) + ".",
                ephemeral=True)

        handler = getattr(self, f"_do_{key}", None)
        if handler is None:
            return await interaction.response.send_message(
                f"`{key}` isn't wired up.", ephemeral=True)
        try:
            await handler(interaction, target, user, user2, amount, text, extra)
        except Exception as e:                       # noqa: BLE001
            log.exception("/admin %s failed: %s", key, e)
            await self._reply(interaction,
                              f"❌ `{type(e).__name__}: {e}`", ephemeral=True)

    @admin.autocomplete("action")
    async def admin_autocomplete(self, interaction: discord.Interaction,
                                 current: str):
        cur = (current or "").lower()
        out = []
        for key, (label, hint, _req) in ACTIONS.items():
            if cur and cur not in key and cur not in label.lower():
                continue
            # The dropdown is where an admin learns which parameters an action
            # wants, so the hint rides along in the visible name.
            name = f"{label} — {hint}"
            out.append(app_commands.Choice(name=name[:100], value=key))
        return out[:25]

    # ── tournament ───────────────────────────────────────────────────────────
    #
    # These reach the cog through the three public `admin_*` methods on
    # TournamentCog rather than into its internals. The old console imported
    # `..tournament.views`, `..tournament.models`, `..tournament.brackets` and
    # `..tournament.notifications` directly and read `cog.svc.store` — which is
    # why deleting one package broke seventeen admin actions at once.

    async def _do_start(self, interaction, target, user, user2, amount, text,
                        extra) -> None:
        cog = self._tournament_cog()
        lobby = cog.admin_lobby(interaction.guild_id) if cog else None
        if not lobby or lobby.started:
            return await self._reply(interaction,
                                     "No open tournament here.", ephemeral=True)
        from cogs.tournament.tournament import MIN_PLAYERS
        if not cog.admin_start(interaction.guild_id):
            return await self._reply(
                interaction, f"Needs at least {MIN_PLAYERS} entrants.",
                ephemeral=True)
        await self._reply(interaction, "▶️ Starting the tournament.")

    async def _do_cancel(self, interaction, target, user, user2, amount, text,
                         extra) -> None:
        cog = self._tournament_cog()
        ok = await cog.admin_cancel(interaction.guild_id) if cog else False
        await self._reply(interaction,
                          "✖️ Tournament cancelled — everyone released."
                          if ok else "No open tournament here.",
                          ephemeral=not ok)

    async def _do_force_win(self, interaction, target, user, user2, amount,
                            text, extra) -> None:
        cog = self._tournament_cog()
        lobby = cog.admin_lobby(interaction.guild_id) if cog else None
        if not lobby or not lobby.matches:
            return await self._reply(interaction,
                                     "No live bracket here.", ephemeral=True)
        if not cog.admin_force_win(interaction.guild_id, user.id):
            return await self._reply(
                interaction, f"{user.mention} has no unfinished match.",
                ephemeral=True)
        await self._reply(interaction, f"⚖️ {user.mention} advances.")

    async def _do_ban_player(self, interaction, target, user, user2, amount,
                             text, extra) -> None:
        cog = self._tournament_cog()
        if cog is None:
            return await self._reply(interaction, "Tournament cog not loaded.",
                                     ephemeral=True)
        # Through the cog, which owns the "entrants and _active move together"
        # invariant. Hand-rolling half of it out here was the third place that
        # had to stay in sync, and the one that would silently stop being
        # maintained the moment a fourth thing joined the invariant.
        cog.admin_ban(interaction.guild_id, user.id)
        await self._reply(interaction,
                          f"🚫 {user.mention} banned from tournaments — {text}")

    async def _do_casino_give(self, interaction, target, user, user2, amount,
                              text, extra):
        if amount <= 0:
            return await self._reply(interaction, "Amount must be positive.", True)
        from ..casino import casino_wallet
        await casino_wallet.credit(user.id, amount)
        # credit() returns None — read the balance back rather than assuming
        # it hands one out. Formatting a None here was a live TypeError.
        new = await casino_wallet.get_balance(user.id)
        await self._reply(interaction,
                          f"✅ Gave 🎰 **{amount:,}** to {user.mention}. "
                          f"New balance: **{new:,}**.")

    async def _do_casino_take(self, interaction, target, user, user2, amount,
                              text, extra):
        if amount <= 0:
            return await self._reply(interaction, "Amount must be positive.", True)
        from ..casino import casino_wallet
        ok = await casino_wallet.deduct(user.id, amount)
        if not ok:
            return await self._reply(
                interaction, f"❌ {user.mention} doesn't have that many.", True)
        bal = await casino_wallet.get_balance(user.id)
        await self._reply(interaction,
                          f"✅ Took 🎰 **{amount:,}** from {user.mention}. "
                          f"New balance: **{bal:,}**.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminConsole(bot))
