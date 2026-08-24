"""
cogs/snapshots/receipt.py — the DM sent after a scheduled backup.

Two buttons, restart-proof the same way `cogs/updates/reports.py`'s
`ReportButton` is: `discord.ui.DynamicItem` matches a `custom_id` by regex
(`snapback:<action>:<day>`) and rebuilds the handler from it, so nothing has
to stay resident in memory between a backup landing and someone pressing a
button on it — which could be days.

**Download** resends the LOCAL `.gz` — the bot already has the bytes on
disk, so no second network round trip to GitHub is needed to hand them back.
**Acknowledge** is non-destructive (a record that you saw it) so it needs no
second confirm; it edits the message and disables both buttons.
"""

from __future__ import annotations

import logging
import os
import time

import discord

from utils import snapshot as SN

log = logging.getLogger("beyblade_bot.snapshot.receipt")


def _owner_id() -> int:
    from cogs.admin import actions as A
    return A.MASTER_ID


def _local_path(day: str) -> str:
    daily = os.path.join(SN.folder(), SN.DAILY_DIR, f"{day}.json.gz")
    if os.path.exists(daily):
        return daily
    return os.path.join(SN.folder(), SN.LATEST)


def build_embed(day: str, describe: dict, local_kb: int, gh: dict,
                acknowledged: bool = False, ack_at: float = 0.0) -> discord.Embed:
    e = discord.Embed(title="💾 Daily backup", colour=0x2ECC71,
                      timestamp=discord.utils.utcnow())
    e.add_field(
        name=f"backups/daily/{day}.json.gz",
        value=(f"**{describe.get('profiles', 0):,}** profiles · "
               f"**{describe.get('beys', 0):,}** beys · "
               f"**{describe.get('avatars', 0):,}** avatars\n"
               f"{local_kb} KB, local"),
        inline=False)
    if gh.get("ok"):
        e.add_field(name="GitHub", value=f"✅ pushed to `{gh.get('repo', '?')}`",
                    inline=False)
    elif gh.get("error") == "not configured":
        e.add_field(name="GitHub", value="⚪ not configured — local copy only",
                    inline=False)
    else:
        e.add_field(name="GitHub", value=f"⚠️ {gh.get('error', 'push failed')}",
                    inline=False)
    if acknowledged:
        e.set_footer(text=f"Acknowledged {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(ack_at))}")
    return e


class ReceiptButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"snapback:(?P<action>dl|ack):(?P<day>[\d-]+)"):

    LABELS = {
        "dl":  ("Download", discord.ButtonStyle.primary, "📥"),
        "ack": ("Acknowledge", discord.ButtonStyle.secondary, "✅"),
    }

    def __init__(self, day: str, action: str) -> None:
        self.day = day
        self.action = action
        label, style, emoji = self.LABELS[action]
        super().__init__(discord.ui.Button(
            label=label, style=style, emoji=emoji,
            custom_id=f"snapback:{action}:{day}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["day"], match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != _owner_id():
            return await interaction.response.send_message(
                "This backup receipt isn't yours.", ephemeral=True)

        if self.action == "dl":
            return await self._download(interaction)
        await self._acknowledge(interaction)

    async def _download(self, interaction: discord.Interaction) -> None:
        path = _local_path(self.day)
        if not os.path.exists(path):
            return await interaction.response.send_message(
                "That snapshot is no longer on disk.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.followup.send(
                content="Keep this somewhere off the machine.",
                file=discord.File(path, filename=os.path.basename(path)),
                ephemeral=True)
        except Exception as exc:                        # noqa: BLE001
            log.exception("[snapshot] could not send the receipt download")
            await interaction.followup.send(
                f"Couldn't send it: `{type(exc).__name__}`.", ephemeral=True)

    async def _acknowledge(self, interaction: discord.Interaction) -> None:
        # No state survives between a DynamicItem being dispatched and the
        # next — `day`/`action` from the custom_id is all there is. Rather
        # than reconstruct the GitHub outcome (and risk reporting a push
        # that actually failed as fine), this keeps the ORIGINAL embed
        # exactly as posted and only adds the acknowledgement footer.
        now = time.time()
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed is not None:
            embed.set_footer(text="Acknowledged "
                             f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))}")
        view = view_for(self.day, acknowledged=True)
        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except Exception:                               # noqa: BLE001
            log.exception("[snapshot] could not edit the receipt on ack")


def view_for(day: str, acknowledged: bool = False) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    dl = ReceiptButton(day, "dl")
    ack = ReceiptButton(day, "ack")
    if acknowledged:
        dl.item.disabled = True
        ack.item.disabled = True
        ack.item.label = "Acknowledged"
    v.add_item(dl)
    v.add_item(ack)
    return v
