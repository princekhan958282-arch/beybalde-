"""
cogs/extras/trade.py
--------------------
Simple shared-panel player trading.

Flow:
  /trade player:@user
  -> both players choose one eligible Bey from their own inventory
  -> searchable, paginated private inventory picker
  -> both players accept the shared offer
  -> ownership is revalidated and both profiles are exchanged atomically

The legacy ;trade command remains available through hybrid_command, but no
blade names are typed into the command anymore.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time

import discord
from discord import app_commands
from discord.ext import commands

from utils.availability import is_owner_bound
from utils.database import get_user, load_beyblades, mutate_users


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOG_PATH = os.path.join(_PROJECT_ROOT, "data", "trade_log.json")
_log_lock = threading.Lock()
PAGE_SIZE = 20
TRADE_TIMEOUT = 180


def _blade_def(name: str) -> dict:
    try:
        return (load_beyblades() or {}).get(str(name)) or {}
    except Exception:  # noqa: BLE001
        return {}


def _item_name(item) -> str:
    return str(item.get("name") if isinstance(item, dict) else item)


def _find_blade(inventory: list, name: str):
    low = str(name).casefold().strip()
    for item in inventory:
        if _item_name(item).casefold() == low:
            return item
    return None


def _eligible_inventory(profile: dict) -> list:
    out = []
    for item in profile.get("inventory", []):
        name = _item_name(item)
        if name and not is_owner_bound(_blade_def(name)):
            out.append(item)
    return out


def _log_trade(entry: dict) -> None:
    try:
        with _log_lock:
            data = []
            if os.path.exists(_LOG_PATH):
                try:
                    with open(_LOG_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = []
            data.append(entry)
            tmp = _LOG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, _LOG_PATH)
    except Exception:
        pass


class TradeSearchModal(discord.ui.Modal, title="Search your Beys"):
    query = discord.ui.TextInput(
        label="Bey name",
        placeholder="Example: Dragon",
        required=False,
        max_length=80,
    )

    def __init__(self, picker: "InventoryPickerView"):
        super().__init__()
        self.picker = picker

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.picker.query = str(self.query.value or "").strip()
        self.picker.page = 0
        await self.picker.reload()
        await interaction.response.edit_message(
            embed=self.picker.make_embed(),
            view=self.picker,
        )


class InventorySelect(discord.ui.Select):
    def __init__(self, picker: "InventoryPickerView"):
        self.picker = picker
        options = picker.current_options()
        if options:
            select_options = [
                discord.SelectOption(
                    label=_item_name(item)[:100],
                    value=str(i),
                    description="Select this Bey for your trade"[:100],
                )
                for i, item in options
            ]
            super().__init__(
                placeholder="Choose a Bey...",
                min_values=1,
                max_values=1,
                options=select_options,
                row=0,
            )
        else:
            super().__init__(
                placeholder="No Beys found",
                min_values=1,
                max_values=1,
                options=[discord.SelectOption(label="No results", value="none")],
                disabled=True,
                row=0,
            )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "none":
            return await interaction.response.defer()

        index = int(self.values[0])
        options = self.picker.current_options()
        selected = next((item for i, item in options if i == index), None)
        if selected is None:
            return await interaction.response.send_message(
                "❌ That Bey is no longer available. Reopen your inventory.",
                ephemeral=True,
            )

        name = _item_name(selected)
        profile = await get_user(interaction.user.id)
        live_item = _find_blade(profile.get("inventory", []), name)
        if live_item is None:
            return await interaction.response.send_message(
                "❌ You no longer own that Bey.",
                ephemeral=True,
            )
        if is_owner_bound(_blade_def(name)):
            return await interaction.response.send_message(
                f"❌ **{name}** is a personal Bey and cannot be traded.",
                ephemeral=True,
            )

        self.picker.trade_view.set_offer(interaction.user.id, name)
        await interaction.response.edit_message(
            content=f"✅ **{name}** added to your offer.",
            embed=None,
            view=None,
        )
        await self.picker.trade_view.refresh()


class InventoryPickerView(discord.ui.View):
    def __init__(self, trade_view: "TradeView", owner_id: int, inventory: list):
        super().__init__(timeout=120)
        self.trade_view = trade_view
        self.owner_id = owner_id
        self.inventory = inventory
        self.query = ""
        self.page = 0
        self.rebuild()

    def filtered(self) -> list:
        if not self.query:
            return list(self.inventory)
        q = self.query.casefold()
        return [item for item in self.inventory if q in _item_name(item).casefold()]

    def pages(self) -> int:
        return max(1, math.ceil(len(self.filtered()) / PAGE_SIZE))

    def current_options(self) -> list[tuple[int, object]]:
        items = self.filtered()
        self.page = max(0, min(self.page, self.pages() - 1))
        start = self.page * PAGE_SIZE
        return list(enumerate(items[start:start + PAGE_SIZE], start=start))

    def rebuild(self) -> None:
        self.clear_items()
        self.add_item(InventorySelect(self))

        search = discord.ui.Button(label="🔎 Search", style=discord.ButtonStyle.primary, row=1)
        prev = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.secondary, row=1)
        nxt = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, row=1)
        clear = discord.ui.Button(label="Clear Search", style=discord.ButtonStyle.secondary, row=2)

        prev.disabled = self.page <= 0
        nxt.disabled = self.page >= self.pages() - 1
        clear.disabled = not bool(self.query)

        async def search_cb(interaction: discord.Interaction):
            await interaction.response.send_modal(TradeSearchModal(self))

        async def prev_cb(interaction: discord.Interaction):
            self.page = max(0, self.page - 1)
            self.rebuild()
            await interaction.response.edit_message(embed=self.make_embed(), view=self)

        async def next_cb(interaction: discord.Interaction):
            self.page = min(self.pages() - 1, self.page + 1)
            self.rebuild()
            await interaction.response.edit_message(embed=self.make_embed(), view=self)

        async def clear_cb(interaction: discord.Interaction):
            self.query = ""
            self.page = 0
            self.rebuild()
            await interaction.response.edit_message(embed=self.make_embed(), view=self)

        search.callback = search_cb
        prev.callback = prev_cb
        nxt.callback = next_cb
        clear.callback = clear_cb
        self.add_item(search)
        self.add_item(prev)
        self.add_item(nxt)
        self.add_item(clear)

    async def reload(self) -> None:
        profile = await get_user(self.owner_id)
        self.inventory = _eligible_inventory(profile)
        self.rebuild()

    def make_embed(self) -> discord.Embed:
        items = self.filtered()
        title = "🔎 Search Results" if self.query else "🎒 Select From Inventory"
        desc = (
            f"Search: **{self.query}**\n" if self.query else ""
        ) + f"Page **{self.page + 1}/{self.pages()}** • **{len(items)}** tradable Bey(s)"
        return discord.Embed(
            title=title,
            description=desc + "\n\nChoose the Bey you want to offer.",
            color=discord.Color.blurple(),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This inventory picker belongs to the other trader.",
                ephemeral=True,
            )
            return False
        if self.trade_view.finished:
            await interaction.response.send_message(
                "This trade is already closed.",
                ephemeral=True,
            )
            return False
        return True


class TradeView(discord.ui.View):
    def __init__(self, cog: "TradeCog", initiator: discord.Member, target: discord.Member):
        super().__init__(timeout=TRADE_TIMEOUT)
        self.cog = cog
        self.initiator = initiator
        self.target = target
        self.offers: dict[int, str | None] = {
            initiator.id: None,
            target.id: None,
        }
        self.accepted: set[int] = set()
        self.message: discord.Message | None = None
        self.finished = False
        self.processing = False

    def is_trader(self, user_id: int) -> bool:
        return user_id in self.offers

    def set_offer(self, user_id: int, name: str) -> None:
        if self.offers.get(user_id) != name:
            self.offers[user_id] = name
            # Any offer change invalidates every previous approval.
            self.accepted.clear()

    def status(self, user_id: int) -> str:
        return "✅ Accepted" if user_id in self.accepted else "⏳ Waiting"

    def make_embed(self) -> discord.Embed:
        a_offer = self.offers[self.initiator.id] or "*Nothing selected*"
        b_offer = self.offers[self.target.id] or "*Nothing selected*"
        embed = discord.Embed(
            title="🤝 Bey Trade",
            description=(
                "Each player selects a Bey from their own inventory. "
                "When both offers are ready, **both players must accept**.\n\n"
                "Changing either offer resets both approvals."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name=self.initiator.display_name,
            value=f"**Offer**\n{a_offer}\n\n{self.status(self.initiator.id)}",
            inline=True,
        )
        embed.add_field(
            name=self.target.display_name,
            value=f"**Offer**\n{b_offer}\n\n{self.status(self.target.id)}",
            inline=True,
        )
        embed.set_footer(text="Trade closes after 3 minutes of inactivity.")
        return embed

    async def refresh(self) -> None:
        if self.message and not self.finished:
            try:
                await self.message.edit(embed=self.make_embed(), view=self)
            except discord.HTTPException:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.is_trader(interaction.user.id):
            await interaction.response.send_message(
                "Only the two players in this trade can use these controls.",
                ephemeral=True,
            )
            return False
        if self.finished or self.processing:
            await interaction.response.send_message(
                "This trade is already closing.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="🎒 Select Bey", style=discord.ButtonStyle.primary)
    async def select_bey(self, interaction: discord.Interaction, _: discord.ui.Button):
        profile = await get_user(interaction.user.id)
        inventory = _eligible_inventory(profile)
        if not inventory:
            return await interaction.response.send_message(
                "❌ You don't have any tradable Beys.",
                ephemeral=True,
            )
        picker = InventoryPickerView(self, interaction.user.id, inventory)
        await interaction.response.send_message(
            embed=picker.make_embed(),
            view=picker,
            ephemeral=True,
        )

    @discord.ui.button(label="✅ Accept Trade", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not all(self.offers.values()):
            return await interaction.response.send_message(
                "⚠️ Both players need to select a Bey first.",
                ephemeral=True,
            )

        uid = interaction.user.id
        if uid in self.accepted:
            return await interaction.response.send_message(
                "You already accepted this version of the trade.",
                ephemeral=True,
            )

        self.accepted.add(uid)
        if len(self.accepted) < 2:
            await interaction.response.edit_message(embed=self.make_embed(), view=self)
            return

        self.processing = True
        await interaction.response.defer()
        await self.complete_trade()

    @discord.ui.button(label="❌ Cancel Trade", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.finished = True
        self.stop()
        self.cog.release(self.initiator.id, self.target.id)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Trade Cancelled",
                description=f"{interaction.user.mention} cancelled the trade.",
                color=discord.Color.red(),
            ),
            view=self,
        )

    async def complete_trade(self) -> None:
        a_id, b_id = self.initiator.id, self.target.id
        a_name = self.offers[a_id]
        b_name = self.offers[b_id]

        def exchange(profiles: dict):
            a_prof = profiles[str(a_id)]
            b_prof = profiles[str(b_id)]
            a_item = _find_blade(a_prof.get("inventory", []), a_name)
            b_item = _find_blade(b_prof.get("inventory", []), b_name)

            if a_item is None or b_item is None:
                raise ValueError("One of the offered Beys is no longer owned.")
            if is_owner_bound(_blade_def(a_name)) or is_owner_bound(_blade_def(b_name)):
                raise ValueError("A personal Bey cannot be traded.")

            a_prof.setdefault("inventory", []).remove(a_item)
            b_prof.setdefault("inventory", []).remove(b_item)
            a_prof["inventory"].append(b_item)
            b_prof["inventory"].append(a_item)

            if str(a_prof.get("active_beyblade", "")).casefold() == str(a_name).casefold():
                a_prof["active_beyblade"] = None
            if str(b_prof.get("active_beyblade", "")).casefold() == str(b_name).casefold():
                b_prof["active_beyblade"] = None

        try:
            await mutate_users([a_id, b_id], exchange)
        except ValueError as exc:
            self.accepted.clear()
            self.processing = False
            if self.message:
                await self.message.edit(
                    embed=discord.Embed(
                        title="⚠️ Trade Needs Attention",
                        description=f"{exc}\n\nThe trade was **not** completed. Please reselect your offers.",
                        color=discord.Color.orange(),
                    ),
                    view=self,
                )
            return
        except Exception:
            self.finished = True
            self.stop()
            self.cog.release(a_id, b_id)
            if self.message:
                await self.message.edit(
                    embed=discord.Embed(
                        title="❌ Trade Failed",
                        description="The exchange could not be saved, so **nothing was traded**.",
                        color=discord.Color.red(),
                    ),
                    view=None,
                )
            raise

        self.finished = True
        self.stop()
        self.cog.release(a_id, b_id)
        _log_trade({
            "ts": int(time.time()),
            "guild": self.message.guild.id if self.message and self.message.guild else None,
            "a_id": a_id,
            "a_gave": str(a_name),
            "b_id": b_id,
            "b_gave": str(b_name),
        })

        for child in self.children:
            child.disabled = True
        if self.message:
            await self.message.edit(
                embed=discord.Embed(
                    title="✅ Trade Complete!",
                    description=(
                        f"{self.initiator.mention} received **{b_name}**\n"
                        f"{self.target.mention} received **{a_name}**\n\n"
                        "If an offered Bey was equipped, it was automatically unequipped."
                    ),
                    color=discord.Color.green(),
                ),
                view=self,
            )

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self.finished = True
        self.cog.release(self.initiator.id, self.target.id)
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    embed=discord.Embed(
                        title="⌛ Trade Expired",
                        description="No Beys were exchanged. Start a new trade whenever you're ready.",
                        color=discord.Color.greyple(),
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass


class TradeCog(commands.Cog, name="Trading"):
    """Simple shared-panel Bey trading."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._busy: set[int] = set()

    def release(self, *user_ids: int) -> None:
        for user_id in user_ids:
            self._busy.discard(user_id)

    @commands.guild_only()
    @commands.hybrid_command(
        name="trade",
        brief="Open a trade with another player 🤝",
        description="Open a shared Bey trade panel with another player.",
    )
    @app_commands.describe(player="Player you want to trade with")
    async def trade(self, ctx: commands.Context, player: discord.Member) -> None:
        target = player
        if target.bot:
            return await ctx.send("❌ You can't trade with a bot!")
        if target.id == ctx.author.id:
            return await ctx.send("❌ You can't trade with yourself!")
        if ctx.author.id in self._busy or target.id in self._busy:
            return await ctx.send("⚠️ One of you already has an active trade.")

        self._busy.update({ctx.author.id, target.id})
        view = TradeView(self, ctx.author, target)
        try:
            msg = await ctx.send(
                content=f"{ctx.author.mention} ↔ {target.mention}",
                embed=view.make_embed(),
                view=view,
            )
            view.message = msg
        except Exception:
            self.release(ctx.author.id, target.id)
            raise

    @trade.error
    async def trade_error(self, ctx: commands.Context, error) -> None:
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send("❌ Usage: `/trade player:@user` or `;trade @user`")
        else:
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TradeCog(bot))
