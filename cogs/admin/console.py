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
import time
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
    # ── tournament lifecycle ──
    "create":        ("🏆 Create tournament",
                      "text = name, extra = mode, amount = max players",
                      ("text",)),
    "start":         ("▶️ Start tournament", "target = tournament id", ("target",)),
    "end":           ("🏁 End tournament", "target = tournament id", ("target",)),
    "cancel":        ("✖️ Cancel tournament", "target = tournament id", ("target",)),
    "pause":         ("⏸️ Pause tournament", "target = tournament id", ("target",)),
    "resume":        ("⏯️ Resume tournament", "target = tournament id", ("target",)),
    # ── match control ──
    "force_win":     ("⚖️ Force a match winner",
                      "target = match id, user = winner", ("target", "user")),
    "reschedule":    ("🕒 Reschedule a match",
                      "target = match id, amount = hours from now",
                      ("target", "amount")),
    "replace_player": ("🔄 Swap a player",
                       "target = tournament id, user = out, user2 = in",
                       ("target", "user", "user2")),
    # ── players ──
    "ban_player":    ("🚫 Ban from tournaments",
                      "user, text = reason, amount = days (default 30)",
                      ("user", "text")),
    "unban_player":  ("✅ Lift a ban", "user", ("user",)),
    # ── prizes & messaging ──
    "set_reward":    ("🎁 Set the prize",
                      "target = tournament id, extra = role|currency|item, "
                      "text = value", ("target", "extra", "text")),
    "broadcast":     ("📣 Message entrants",
                      "target = tournament id, text = message",
                      ("target", "text")),
    "announce":      ("📢 Announcement composer", "opens the composer", ()),
    "announce_history": ("📜 Announcement history", "last 30 days", ()),
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

MODES = {"single", "double", "round_robin"}
REWARD_TYPES = {"role", "currency", "item"}


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

    # ── tournament lifecycle ─────────────────────────────────────────────────

    async def _do_create(self, interaction, target, user, user2, amount, text,
                         extra):
        cog = self._tournament_cog()
        mode = (extra or "single").lower()
        if mode not in MODES:
            return await self._reply(
                interaction,
                f"`extra` must be one of {', '.join(sorted(MODES))}.", True)
        ok, msg, t = cog.svc.create(
            interaction.guild_id or 0, interaction.channel_id or 0, text, mode,
            amount or 16, time.time() + 3600, interaction.user.id)
        if not ok:
            return await self._reply(interaction, f"❌ {msg}", True)
        from .. tournament.views import TournamentCard
        from .. tournament import ui_v2
        card = TournamentCard(cog, t.id)
        await interaction.response.send_message(
            **ui_v2.card_kwargs(t, len(t.entrants), card))

    async def _state(self, interaction, tid, state, verb):
        cog = self._tournament_cog()
        from ..tournament.models import TournamentState
        ok, msg = cog.svc.set_state(tid, state)
        await self._reply(interaction, ("✅ " if ok else "❌ ") + msg)

    async def _do_start(self, interaction, target, user, user2, amount, text,
                        extra):
        cog = self._tournament_cog()
        await interaction.response.defer()
        ok, msg, _ = cog.svc.start(target)
        await interaction.followup.send(("✅ " if ok else "❌ ") + msg)

    async def _do_end(self, interaction, target, user, user2, amount, text, extra):
        cog = self._tournament_cog()
        from ..tournament.models import TournamentState
        from ..tournament import brackets
        t = cog.svc.store.get_tournament(target)
        if not t:
            return await self._reply(interaction, "❌ No tournament with that id.",
                                     True)
        await interaction.response.defer()
        matches = cog.svc.store.get_matches(t.id)
        champ = brackets.champion(matches, t.mode)
        if champ is None:
            table = brackets.standings(matches)
            champ = table[0][0] if table else None
        ok, msg = cog.svc.set_state(t.id, TournamentState.COMPLETED)
        await interaction.followup.send(("✅ " if ok else "❌ ") + msg)
        if champ is not None:
            await cog._award(t, champ)

    async def _do_cancel(self, interaction, target, *_):
        from ..tournament.models import TournamentState
        await self._state(interaction, target, TournamentState.CANCELLED, "cancelled")

    async def _do_pause(self, interaction, target, *_):
        from ..tournament.models import TournamentState
        await self._state(interaction, target, TournamentState.PAUSED, "paused")

    async def _do_resume(self, interaction, target, *_):
        from ..tournament.models import TournamentState
        await self._state(interaction, target, TournamentState.RUNNING, "resumed")

    # ── match control ────────────────────────────────────────────────────────

    async def _do_force_win(self, interaction, target, user, user2, amount, text,
                            extra):
        cog = self._tournament_cog()
        ok, msg, _m = cog.svc.force_win(target, user.id)
        await self._reply(interaction, ("✅ " if ok else "❌ ") + msg)

    async def _do_reschedule(self, interaction, target, user, user2, amount, text,
                             extra):
        cog = self._tournament_cog()
        from ..tournament import notifications as notify, timeslots as ts
        ok, msg, m = cog.svc.reschedule(target, time.time() + amount * 3600)
        await self._reply(interaction, ("✅ " if ok else "❌ ") + msg)
        if ok and m:
            await notify.dm_all(self.bot, m.players,
                                content=f"Your match `{m.id}` moved to "
                                        f"{ts.discord_ts(m.scheduled_for)}.")

    async def _do_replace_player(self, interaction, target, user, user2, amount,
                                 text, extra):
        cog = self._tournament_cog()
        ok, msg = cog.svc.replace_player(target, user.id, user2.id)
        await self._reply(interaction, ("✅ " if ok else "❌ ") + msg)

    # ── players ──────────────────────────────────────────────────────────────

    async def _do_ban_player(self, interaction, target, user, user2, amount, text,
                             extra):
        cog = self._tournament_cog()
        ok, msg = cog.svc.ban_player(user.id, text, amount or 30)
        await self._reply(interaction, ("✅ " if ok else "❌ ") + msg)

    async def _do_unban_player(self, interaction, target, user, user2, amount,
                               text, extra):
        cog = self._tournament_cog()
        ok, msg = cog.svc.unban_player(user.id)
        await self._reply(interaction, ("✅ " if ok else "❌ ") + msg)

    # ── prizes & messaging ───────────────────────────────────────────────────

    async def _do_set_reward(self, interaction, target, user, user2, amount, text,
                             extra):
        cog = self._tournament_cog()
        kind = (extra or "").lower()
        if kind not in REWARD_TYPES:
            return await self._reply(
                interaction,
                f"`extra` must be one of {', '.join(sorted(REWARD_TYPES))}.", True)
        t = cog.svc.store.get_tournament(target)
        if not t:
            return await self._reply(interaction, "❌ No such tournament.", True)
        t.reward_type, t.reward_value = kind, text
        cog.svc.store.put_tournament(t)
        await self._reply(interaction,
                          f"✅ Prize for **{t.name}**: {kind} `{text}`.")

    async def _do_broadcast(self, interaction, target, user, user2, amount, text,
                            extra):
        cog = self._tournament_cog()
        from ..tournament import notifications as notify
        t = cog.svc.store.get_tournament(target)
        if not t:
            return await self._reply(interaction, "❌ No such tournament.", True)
        await interaction.response.defer(ephemeral=True)
        results = await notify.dm_all(self.bot, t.entrants,
                                      content=f"📣 **{t.name}** — {text}")
        sent = sum(1 for v in results.values() if v)
        await interaction.followup.send(
            f"Sent to {sent}/{len(results)}. "
            f"{len(results) - sent} have DMs closed.", ephemeral=True)

    async def _do_announce(self, interaction, *_):
        cog = self._tournament_cog()
        from ..tournament.views import AnnouncementComposer
        view = AnnouncementComposer(cog, interaction.guild_id or 0,
                                    interaction.channel_id or 0)
        await interaction.response.send_message(
            embed=view.preview(), view=view, ephemeral=True)

    async def _do_announce_history(self, interaction, *_):
        cog = self._tournament_cog()
        recent = cog.svc.store.recent_dm_announcements(time.time() - 30 * 86400)
        if not recent:
            return await self._reply(interaction,
                                     "No DM announcements in the last 30 days.",
                                     True)
        e = discord.Embed(title="📢 Recent announcements", colour=0x5865F2)
        for ann in recent[:10]:
            counts = cog.svc.store.rsvp_counts(ann["id"])
            body = ann["body"].replace("\n", " ")
            if len(body) > 80:
                body = body[:79] + "…"
            e.add_field(
                name=f"<t:{int(ann['created_at'])}:R> · {counts['total']} sent",
                value=(f"{body}\n✅ {counts['yes']} · ❌ {counts['no']} · "
                       f"⏳ {counts['pending']}"),
                inline=False)
        if len(recent) > 10:
            e.set_footer(text=f"showing 10 of {len(recent)}")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ── casino ───────────────────────────────────────────────────────────────

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
