"""
auction.py  —  Seller-initiated auction system
Only admins or designated sellers can create auctions.
Players bid with casino coins. Highest bidder wins the item.
Supports Beyblade items, custom prizes, or anything else.
"""
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from typing import Optional
from . import casino_wallet

MIN_START_BID = 10
AUCTION_ROLE  = "Auction Host"   # Role name allowed to host auctions
BID_INCREMENT = 1                # minimum bid must exceed current by at least this

ACTIVE_AUCTIONS: dict[int, "AuctionState"] = {}   # channel_id → auction


class AuctionState:
    def __init__(self, seller: discord.Member, item: str,
                 start_bid: int, duration: int, description: str):
        self.seller      = seller
        self.item        = item
        self.description = description
        self.start_bid   = start_bid
        self.duration    = duration       # seconds
        self.current_bid = start_bid - 1  # so first bid can equal start
        self.top_bidder: Optional[discord.Member] = None
        self.top_bid     = 0
        self.bids: list[tuple[discord.Member, int]] = []
        self.ended       = False
        self.message: Optional[discord.Message] = None
        self.reserved: dict[int, int] = {}   # uid → reserved coins (to prevent overbidding)


def build_auction_embed(s: "AuctionState", final=False) -> discord.Embed:
    color = 0xf39c12 if not final else (0x2ecc71 if s.top_bidder else 0x95a5a6)
    e     = discord.Embed(
        title=f"🔨  Auction: {s.item}",
        description=s.description or "*No description provided.*",
        color=color
    )
    e.add_field(name="Seller",     value=s.seller.mention, inline=True)
    e.add_field(name="Start Bid",  value=f"🪙 {s.start_bid:,}", inline=True)
    e.add_field(
        name="Current Bid",
        value=f"🪙 {s.top_bid:,} by {s.top_bidder.mention}" if s.top_bidder
              else "No bids yet",
        inline=False
    )
    if s.bids:
        last5 = s.bids[-5:][::-1]
        e.add_field(
            name="Recent Bids",
            value="\n".join(f"• {b.display_name}  🪙 {amt:,}" for b, amt in last5),
            inline=False
        )
    if final:
        if s.top_bidder:
            e.add_field(
                name="🏆 Winner",
                value=f"{s.top_bidder.mention} won **{s.item}** for 🪙 {s.top_bid:,}!",
                inline=False
            )
        else:
            e.add_field(name="Result", value="No bids — item unsold.", inline=False)
    else:
        e.set_footer(text=f"Bid must exceed 🪙 {s.current_bid:,}")
    return e


class AuctionView(discord.ui.View):
    def __init__(self, state: AuctionState):
        super().__init__(timeout=state.duration)
        self.state = state

    def build_embed(self, final=False) -> discord.Embed:
        return build_auction_embed(self.state, final=final)

    @discord.ui.button(label="💰 Place Bid", style=discord.ButtonStyle.primary, row=0)
    async def bid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.state.ended:
            return await interaction.response.send_message("Auction ended.", ephemeral=True)
        await interaction.response.send_modal(BidModal(self.state, self))

    @discord.ui.button(label="❌ Cancel Auction", style=discord.ButtonStyle.danger, row=0)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.state.seller.id and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Only the seller can cancel.", ephemeral=True)
        await self._end(interaction, cancelled=True)

    async def _end(self, interaction_or_none, cancelled=False):
        s         = self.state
        # Idempotency guard: only the first call settles money. Any later
        # trigger (duplicate timeout, cancel-then-timeout, etc.) just tidies up.
        if s.ended:
            for child in self.children:
                child.disabled = True
            self.stop()
            return
        s.ended   = True
        for child in self.children:
            child.disabled = True

        # Refund all reserved bids except the winner
        for uid, reserved in s.reserved.items():
            if s.top_bidder and uid == s.top_bidder.id:
                continue
            await casino_wallet.credit(uid, reserved)

        # Credit seller if there's a winner
        if s.top_bidder and not cancelled:
            await casino_wallet.credit(s.seller.id, s.top_bid)

        # Refund winner if cancelled
        if cancelled and s.top_bidder:
            await casino_wallet.credit(s.top_bidder.id, s.top_bid)

        embed = self.build_embed(final=True)
        if cancelled:
            embed.set_footer(text="Auction cancelled — all bids refunded")

        if interaction_or_none:
            try:
                await interaction_or_none.response.edit_message(embed=embed, view=self)
            except Exception:
                if s.message:
                    await s.message.edit(embed=embed, view=self)
        elif s.message:
            try:
                await s.message.edit(embed=embed, view=self)
            except Exception:
                pass

        # Remove from active
        for cid, a in list(ACTIVE_AUCTIONS.items()):
            if a is s:
                del ACTIVE_AUCTIONS[cid]
                break

        self.stop()

    async def on_timeout(self):
        await self._end(None, cancelled=False)


class BidModal(discord.ui.Modal, title="Place a Bid"):
    amount = discord.ui.TextInput(
        label="Your bid amount", placeholder="Must exceed current bid")

    def __init__(self, state: AuctionState, view: AuctionView):
        super().__init__()
        self._state = state
        self._view  = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = int(self.amount.value.replace(",","").strip())
        except ValueError:
            return await interaction.response.send_message("Invalid amount.", ephemeral=True)

        s   = self._state
        uid = interaction.user.id

        if amt <= s.current_bid:
            return await interaction.response.send_message(
                f"Bid must exceed 🪙 {s.current_bid:,}.", ephemeral=True)

        # Check balance (accounting for any previous reserved bid)
        prev_reserved = s.reserved.get(uid, 0)
        needed        = amt - prev_reserved

        if needed > 0 and not await casino_wallet.deduct(uid, needed):
            return await interaction.response.send_message(
                "Not enough casino coins.", ephemeral=True)

        # Reserve tracks the total deducted from this user's wallet for this auction.
        # Set to amt (not accumulate) so winner's reserve always equals their winning bid.
        s.reserved[uid] = amt

        s.current_bid = amt
        s.top_bid     = amt
        s.top_bidder  = interaction.user
        s.bids.append((interaction.user, amt))

        await interaction.response.send_message(
            f"✅ Bid placed: 🪙 {amt:,}", ephemeral=True)

        if s.message:
            try:
                await s.message.edit(embed=self._view.build_embed())
            except Exception:
                pass


class AuctionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _can_host(self, member: discord.Member) -> bool:
        return (member.guild_permissions.administrator
                or any(r.name == AUCTION_ROLE for r in member.roles))

    @commands.command(name="auction")
    async def auction(self, ctx: commands.Context, item: str = "", start_bid: int = 0,
                      duration_minutes: int = 10, *, description: str = ""):
        """Start an auction. Usage: ;auction <item> <start_bid> [duration_mins] [description]"""
        if not self._can_host(ctx.author):
            return await ctx.send(f"❌ You need the **{AUCTION_ROLE}** role or Admin to run auctions.")

        if not item:
            return await ctx.send("❌ Usage: `;auction <item> <start_bid> [minutes] [description]`")

        cid = ctx.channel.id
        if cid in ACTIVE_AUCTIONS:
            return await ctx.send("❌ There's already an active auction in this channel.")

        if start_bid < MIN_START_BID:
            return await ctx.send(f"❌ Starting bid must be at least 🪙 {MIN_START_BID:,}.")

        duration_minutes = max(1, min(60, duration_minutes))
        duration_secs    = duration_minutes * 60

        state = AuctionState(
            seller=ctx.author,
            item=item,
            start_bid=start_bid,
            duration=duration_secs,
            description=description
        )
        ACTIVE_AUCTIONS[cid] = state

        view  = AuctionView(state)
        embed = view.build_embed()
        embed.set_footer(text=f"Auction runs for {duration_minutes} minute(s)")

        state.message = await ctx.send(embed=embed, view=view)
        await view.wait()

    @commands.command(name="auction_status", aliases=["auctionstatus", "astatus"])
    async def auction_status(self, ctx: commands.Context):
        """Check the current auction in this channel. Usage: ;auction_status"""
        cid   = ctx.channel.id
        state = ACTIVE_AUCTIONS.get(cid)
        if not state:
            return await ctx.send("❌ No active auction in this channel.")
        await ctx.send(embed=build_auction_embed(state))


async def setup(bot):
    await bot.add_cog(AuctionCog(bot))
