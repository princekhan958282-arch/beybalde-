"""
mines_multi.py  —  💣 Mines: Last Blade Standing (multiplayer)

Everyone plays on ONE shared grid. Turns rotate. On your turn you reveal a
tile — or fold and take half your entry back. Hit a mine and you're out and
your entry stays in the pot. Last player standing takes everything.

No house cut: the pot is exactly the sum of what everyone put in.

Usage:
    ;minesmp <entry> [mines]     (aliases: ;minesmulti, ;mines_multi, ;mmp)
"""

import asyncio
import logging
import random
import time
from typing import Optional

import discord
from discord.ext import commands

from . import casino_premium, casino_wallet

log = logging.getLogger("beyblade_bot")

# ── Tuning ────────────────────────────────────────────────────────────────────
GRID_ROWS      = 4
GRID_COLS      = 5
GRID_SIZE      = GRID_ROWS * GRID_COLS      # 20 tiles
MIN_ENTRY      = 10
MIN_PLAYERS    = 2
MAX_PLAYERS    = 8
DEFAULT_MINES  = 4
MIN_MINES      = 2
MAX_MINES      = 8
LOBBY_SECONDS  = 90
TURN_SECONDS   = 45
FOLD_REFUND    = 0.5        # you get half your entry back if you bail

SAFE_E = "\U0001f48e"       # 💎
MINE_E = "\U0001f4a5"       # 💥


class MPMinesGame:
    """Shared-grid, turn-based elimination Mines."""

    def __init__(self, entry: int, mine_count: int):
        self.entry      = entry
        self.mine_count = mine_count
        self.pot        = 0
        self.lock       = asyncio.Lock()

        self.players: list[discord.Member] = []
        self.alive:   list[int]            = []       # user ids, in turn order
        self.out:     dict[int, str]       = {}       # uid -> "mine" | "fold"
        self.revealed_by: dict[int, int]   = {}       # uid -> safe tiles revealed

        positions = list(range(GRID_SIZE))
        random.shuffle(positions)
        self.mine_positions = set(positions[:mine_count])
        self.revealed: dict[int, bool] = {}           # idx -> True=safe False=mine

        self.turn_index    = 0
        self.turn_deadline = 0.0
        self.started       = False
        self.finished      = False
        self.winners: list[discord.Member] = []
        self.log: list[str] = []

    # ── Lobby ────────────────────────────────────────────────────────────────
    def add_player(self, member: discord.Member) -> None:
        self.players.append(member)
        self.alive.append(member.id)
        self.revealed_by[member.id] = 0
        self.pot += self.entry

    def has(self, uid: int) -> bool:
        return any(p.id == uid for p in self.players)

    def member(self, uid: int) -> Optional[discord.Member]:
        return next((p for p in self.players if p.id == uid), None)

    # ── Turn state ───────────────────────────────────────────────────────────
    @property
    def current_uid(self) -> Optional[int]:
        if not self.alive:
            return None
        return self.alive[self.turn_index % len(self.alive)]

    @property
    def current_player(self) -> Optional[discord.Member]:
        uid = self.current_uid
        return self.member(uid) if uid else None

    @property
    def safe_total(self) -> int:
        return GRID_SIZE - self.mine_count

    @property
    def safe_revealed(self) -> int:
        return sum(1 for v in self.revealed.values() if v)

    @property
    def board_cleared(self) -> bool:
        return self.safe_revealed >= self.safe_total

    def _advance(self, removed_index: Optional[int] = None) -> None:
        """Move to the next living player."""
        if not self.alive:
            return
        if removed_index is None:
            self.turn_index = (self.turn_index + 1) % len(self.alive)
        else:
            # The current player left the list — index now points at the next
            # player already, unless we fell off the end.
            self.turn_index = removed_index % len(self.alive)
        self.turn_deadline = time.time() + TURN_SECONDS

    # ── Actions (caller must hold self.lock) ─────────────────────────────────
    def reveal(self, uid: int, idx: int) -> str:
        """Returns 'safe' or 'mine'."""
        is_safe = idx not in self.mine_positions
        self.revealed[idx] = is_safe
        name = self.member(uid).display_name if self.member(uid) else "?"

        if is_safe:
            self.revealed_by[uid] = self.revealed_by.get(uid, 0) + 1
            self.log.append(f"{SAFE_E} **{name}** found a safe tile")
            self._advance()
            return "safe"

        pos = self.alive.index(uid)
        self.alive.pop(pos)
        self.out[uid] = "mine"
        self.log.append(f"{MINE_E} **{name}** hit a mine and is OUT")
        self._advance(removed_index=pos)
        return "mine"

    def fold(self, uid: int) -> int:
        """Returns the refund amount. Rest of the entry stays in the pot."""
        refund = int(self.entry * FOLD_REFUND)
        self.pot -= refund
        pos = self.alive.index(uid)
        self.alive.pop(pos)
        self.out[uid] = "fold"
        name = self.member(uid).display_name if self.member(uid) else "?"
        self.log.append(f"🏳️ **{name}** folded (took 🪙 {refund:,} back)")
        self._advance(removed_index=pos)
        return refund

    def random_hidden(self) -> Optional[int]:
        hidden = [i for i in range(GRID_SIZE) if i not in self.revealed]
        return random.choice(hidden) if hidden else None

    # ── Settlement ───────────────────────────────────────────────────────────
    def resolve(self) -> Optional[list[tuple[discord.Member, int]]]:
        """If the game is over, return [(member, payout)], else None."""
        if len(self.alive) <= 1 or self.board_cleared or not self.alive:
            survivors = [self.member(u) for u in self.alive]
            survivors = [s for s in survivors if s]
            self.finished = True
            self.winners  = survivors

            if not survivors:
                return []                      # everyone blew up — pot is dead
            if len(survivors) == 1:
                return [(survivors[0], self.pot)]

            share = self.pot // len(survivors)
            payouts = [(s, share) for s in survivors]
            # give the leftover to whoever cleared the most tiles
            remainder = self.pot - share * len(survivors)
            if remainder:
                best = max(survivors, key=lambda s: self.revealed_by.get(s.id, 0))
                payouts = [(s, p + remainder if s.id == best.id else p)
                           for s, p in payouts]
            return payouts
        return None


# ── Grid UI ───────────────────────────────────────────────────────────────────

class MPTile(discord.ui.Button):
    def __init__(self, idx: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b",
                         row=idx // GRID_COLS)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        view: "MPGridView" = self.view
        await view.handle_reveal(interaction, self.idx)


class MPGridView(discord.ui.View):
    # The turn watchdog resolves games in well under a minute of real inactivity;
    # this timeout is a last-resort net so coins can never be stranded.
    def __init__(self, cog, game: MPMinesGame):
        super().__init__(timeout=1800)
        self.cog  = cog
        self.game = game
        self.message: Optional[discord.Message] = None
        for i in range(GRID_SIZE):
            self.add_item(MPTile(i))

    # ── Rendering ────────────────────────────────────────────────────────────
    def sync_buttons(self):
        g = self.game
        for child in self.children:
            if isinstance(child, MPTile):
                state = g.revealed.get(child.idx)
                if state is True:
                    child.style, child.disabled = discord.ButtonStyle.success, True
                    child.emoji, child.label = discord.PartialEmoji.from_str(SAFE_E), ""
                elif state is False:
                    child.style, child.disabled = discord.ButtonStyle.danger, True
                    child.emoji, child.label = discord.PartialEmoji.from_str(MINE_E), ""
                else:
                    child.disabled = g.finished
            else:
                child.disabled = g.finished

        if g.finished:
            for child in self.children:
                if isinstance(child, MPTile) and child.idx in g.mine_positions:
                    child.style = discord.ButtonStyle.danger
                    child.emoji = discord.PartialEmoji.from_str(MINE_E)
                    child.label = ""

    def build_embed(self, payouts=None) -> discord.Embed:
        g = self.game

        if g.finished:
            if not g.winners:
                title, colour = "💥  Everyone Blew Up", 0xe74c3c
                desc = f"No survivors — the pot of 🪙 {g.pot:,} is gone."
            elif len(g.winners) == 1:
                title, colour = "🏆  Last Blade Standing!", 0xf1c40f
                desc = f"{g.winners[0].mention} takes the whole pot."
            else:
                title, colour = "🤝  Board Cleared — Split Pot", 0x2ecc71
                desc = "Survivors split the pot: " + ", ".join(w.mention for w in g.winners)
        else:
            title, colour = "💣  Mines — Last Blade Standing", 0xf1c40f
            cur = g.current_player
            desc = (f"### {cur.mention}'s turn\n"
                    f"Tap a tile to reveal it, or 🏳️ Fold for "
                    f"🪙 {int(g.entry * FOLD_REFUND):,} back."
                    if cur else "Resolving…")

        e = discord.Embed(title=title, description=desc, color=colour)

        alive_lines = []
        for p in g.players:
            reveals = g.revealed_by.get(p.id, 0)
            if p.id in g.out:
                mark = "💥" if g.out[p.id] == "mine" else "🏳️"
                alive_lines.append(f"{mark} ~~{p.display_name}~~  ({reveals} tiles)")
            elif not g.finished and p.id == g.current_uid:
                alive_lines.append(f"▶️ **{p.display_name}**  ({reveals} tiles)")
            else:
                alive_lines.append(f"🟢 {p.display_name}  ({reveals} tiles)")
        e.add_field(name=f"Players ({len(g.alive)} alive)",
                    value="\n".join(alive_lines) or "—", inline=False)

        e.add_field(name="Pot",   value=f"🪙 {g.pot:,}",  inline=True)
        e.add_field(name="Mines", value=str(g.mine_count), inline=True)
        e.add_field(name="Safe tiles left",
                    value=str(g.safe_total - g.safe_revealed), inline=True)

        if g.log:
            e.add_field(name="Last moves", value="\n".join(g.log[-4:]), inline=False)

        if payouts:
            e.add_field(
                name="Payout",
                value="\n".join(f"🏆 {m.mention} → 🪙 {amt:,}" for m, amt in payouts),
                inline=False,
            )
        elif not g.finished:
            left = max(0, int(g.turn_deadline - time.time()))
            e.set_footer(text=f"Entry 🪙 {g.entry:,} each  •  {left}s to move "
                              f"(auto-reveal if you stall)")
        return e

    # ── Actions ──────────────────────────────────────────────────────────────
    async def handle_reveal(self, interaction: discord.Interaction, idx: int):
        g = self.game
        async with g.lock:
            if g.finished:
                return await interaction.response.defer()
            if not g.has(interaction.user.id):
                return await interaction.response.send_message(
                    "You're not in this game.", ephemeral=True)
            if interaction.user.id in g.out:
                return await interaction.response.send_message(
                    "You're already out of this round.", ephemeral=True)
            if interaction.user.id != g.current_uid:
                cur = g.current_player
                return await interaction.response.send_message(
                    f"Not your turn — waiting on **{cur.display_name}**.", ephemeral=True)
            if idx in g.revealed:
                return await interaction.response.defer()

            result  = g.reveal(interaction.user.id, idx)
            payouts = g.resolve()

        await self._render(interaction, payouts)

        if result == "mine" and not g.finished:
            try:
                await interaction.followup.send(
                    f"💥 {interaction.user.mention} hit a mine — "
                    f"🪙 {g.entry:,} stays in the pot!")
            except Exception:
                pass

    @discord.ui.button(label="Fold", emoji="🏳️", style=discord.ButtonStyle.secondary, row=4)
    async def fold_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        async with g.lock:
            if g.finished:
                return await interaction.response.defer()
            if not g.has(interaction.user.id):
                return await interaction.response.send_message(
                    "You're not in this game.", ephemeral=True)
            if interaction.user.id in g.out:
                return await interaction.response.send_message(
                    "You're already out.", ephemeral=True)
            if interaction.user.id != g.current_uid:
                return await interaction.response.send_message(
                    "You can only fold on your own turn.", ephemeral=True)

            refund = g.fold(interaction.user.id)
            await casino_wallet.credit(interaction.user.id, refund)
            payouts = g.resolve()

        await self._render(interaction, payouts)

    async def _render(self, interaction: discord.Interaction, payouts):
        g = self.game
        if payouts is not None:
            for member, amount in payouts:
                if amount > 0:
                    await casino_wallet.credit(member.id, amount)
            self.cog._release(g)

        self.sync_buttons()
        try:
            await interaction.response.edit_message(
                embed=self.build_embed(payouts), view=self)
        except Exception:
            # Interaction already consumed — fall back to a plain message edit
            # so the board still updates for everyone.
            if self.message:
                try:
                    await self.message.edit(embed=self.build_embed(payouts), view=self)
                except Exception:
                    pass

        if g.finished:
            self.stop()

    async def on_timeout(self):
        """Safety net: if the game somehow stalled, pay the pot back out."""
        if self.game.finished:
            return
        await self.cog._bail_out(self.game, self)

    async def refresh(self, payouts=None):
        """Edit the message without an interaction (used by the turn watchdog)."""
        g = self.game
        if payouts is not None:
            for member, amount in payouts:
                if amount > 0:
                    await casino_wallet.credit(member.id, amount)
            self.cog._release(g)
        self.sync_buttons()
        if self.message:
            try:
                await self.message.edit(embed=self.build_embed(payouts), view=self)
            except Exception:
                pass
        if g.finished:
            self.stop()


# ── Lobby UI ──────────────────────────────────────────────────────────────────

class MPLobbyView(discord.ui.View):
    def __init__(self, cog, host: discord.Member, game: MPMinesGame):
        super().__init__(timeout=LOBBY_SECONDS)
        self.cog     = cog
        self.host    = host
        self.game    = game
        self.launched = False
        self.message: Optional[discord.Message] = None

    def build_embed(self) -> discord.Embed:
        g = self.game
        e = discord.Embed(
            title="💣  Mines — Last Blade Standing",
            description=(
                "One shared grid. Turns rotate. Reveal a tile or fold.\n"
                "Hit a mine and you're **out** — your entry stays in the pot.\n"
                "**Last player standing takes everything.**"
            ),
            color=0x9b59b6,
        )
        e.add_field(name="Entry", value=f"🪙 {g.entry:,}", inline=True)
        e.add_field(name="Mines", value=f"{g.mine_count} of {GRID_SIZE}", inline=True)
        e.add_field(name="Pot",   value=f"🪙 {g.pot:,}", inline=True)
        e.add_field(
            name=f"Players ({len(g.players)}/{MAX_PLAYERS})",
            value="\n".join(f"• {p.display_name}" for p in g.players) or "—",
            inline=False,
        )
        e.set_footer(text=f"Host: {self.host.display_name}  •  "
                          f"needs {MIN_PLAYERS}+ players  •  "
                          f"lobby closes in {LOBBY_SECONDS}s")
        return e

    @discord.ui.button(label="Join", emoji="➕", style=discord.ButtonStyle.success)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.game
        async with g.lock:
            if self.launched:
                return await interaction.response.send_message(
                    "This game already started.", ephemeral=True)
            if g.has(interaction.user.id):
                return await interaction.response.send_message(
                    "You're already in.", ephemeral=True)
            if len(g.players) >= MAX_PLAYERS:
                return await interaction.response.send_message(
                    f"Table is full ({MAX_PLAYERS} players).", ephemeral=True)
            if interaction.user.id in self.cog._busy:
                return await interaction.response.send_message(
                    "You're already in another Mines game.", ephemeral=True)

            max_bet = await casino_premium.get_max_bet(interaction.user.id)
            if g.entry > max_bet:
                return await interaction.response.send_message(
                    f"Entry 🪙 {g.entry:,} is above your max bet of 🪙 {max_bet:,}.",
                    ephemeral=True)
            if not await casino_wallet.deduct(interaction.user.id, g.entry):
                return await interaction.response.send_message(
                    "❌ Not enough casino coins.", ephemeral=True)

            g.add_player(interaction.user)
            self.cog._busy.add(interaction.user.id)
            self.start_btn.disabled = len(g.players) < MIN_PLAYERS

        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Start", emoji="▶", style=discord.ButtonStyle.primary,
                       disabled=True)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            return await interaction.response.send_message(
                "Only the host can start.", ephemeral=True)
        g = self.game
        async with g.lock:
            if self.launched:
                return await interaction.response.defer()
            if len(g.players) < MIN_PLAYERS:
                return await interaction.response.send_message(
                    f"Need at least {MIN_PLAYERS} players.", ephemeral=True)
            self.launched = True
            g.started     = True
            random.shuffle(g.alive)
            g.turn_index    = 0
            g.turn_deadline = time.time() + TURN_SECONDS

        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

        grid = MPGridView(self.cog, g)
        grid.sync_buttons()
        grid.message = await interaction.followup.send(
            embed=grid.build_embed(), view=grid, wait=True)
        self.cog._watch(g, grid)

    @discord.ui.button(label="Cancel", emoji="✖", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            return await interaction.response.send_message(
                "Only the host can cancel.", ephemeral=True)
        async with self.game.lock:
            if self.launched:
                return await interaction.response.defer()
            self.launched = True     # blocks any further joins
        await self._refund_all()
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content="❌ Lobby cancelled — everyone refunded.", view=self)
        self.stop()

    async def _refund_all(self):
        g = self.game
        for p in g.players:
            await casino_wallet.credit(p.id, g.entry)
            self.cog._busy.discard(p.id)
        g.pot = 0
        self.cog._release(g)

    async def on_timeout(self):
        if self.launched:
            return
        self.launched = True
        await self._refund_all()
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    content="⏰ Nobody started in time — everyone refunded.", view=self)
            except Exception:
                pass


# ── Cog ───────────────────────────────────────────────────────────────────────

class MinesMultiCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot   = bot
        self._busy: set[int] = set()               # players currently in an MP game
        self._games: list[MPMinesGame] = []

    def _release(self, game: MPMinesGame) -> None:
        for p in game.players:
            self._busy.discard(p.id)
        if game in self._games:
            self._games.remove(game)

    def _watch(self, game: MPMinesGame, grid: MPGridView) -> None:
        asyncio.create_task(self._turn_watchdog(game, grid))

    async def _turn_watchdog(self, game: MPMinesGame, grid: MPGridView) -> None:
        """Auto-reveals for anyone who stalls, so one AFK player can't freeze the table."""
        try:
            while not game.finished:
                await asyncio.sleep(3)
                if game.finished:
                    return
                if time.time() < game.turn_deadline:
                    continue

                async with game.lock:
                    if game.finished:
                        return
                    uid = game.current_uid
                    if uid is None:
                        return
                    idx = game.random_hidden()
                    if idx is None:
                        game.finished = True
                        payouts = game.resolve()
                    else:
                        name = game.member(uid).display_name if game.member(uid) else "?"
                        game.log.append(f"⏱️ **{name}** stalled — auto-revealed")
                        game.reveal(uid, idx)
                        payouts = game.resolve()

                await grid.refresh(payouts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("[minesmp] watchdog crashed — bailing out the table",
                      exc_info=True)
            await self._bail_out(game, grid)

    async def _bail_out(self, game: MPMinesGame, grid: "MPGridView") -> None:
        """Split the whole pot back among whoever is still alive.

        Refunding only each survivor's own entry would orphan the coins that
        eliminated players already put in, so the entire pot goes back out.
        """
        async with game.lock:
            if game.finished:
                return
            game.finished = True
            survivors = [game.member(u) for u in game.alive]
            survivors = [s for s in survivors if s]
            game.winners = survivors
            if survivors:
                share     = game.pot // len(survivors)
                remainder = game.pot - share * len(survivors)
                payouts   = [(s, share) for s in survivors]
                if remainder:
                    payouts[0] = (payouts[0][0], payouts[0][1] + remainder)
            else:
                payouts = []
        try:
            await grid.refresh(payouts)
        except Exception:
            for m, amt in payouts:
                if amt > 0:
                    await casino_wallet.credit(m.id, amt)
            self._release(game)

    @commands.command(name="minesmp",
                      aliases=["minesmulti", "mines_multi", "mmp", "minesbattle"])
    async def minesmp(self, ctx: commands.Context, entry: int = 0, mines: int = DEFAULT_MINES):
        """💣 Multiplayer Mines. Usage: ;minesmp <entry> [mines]"""
        host = ctx.author

        if host.id in self._busy:
            return await ctx.send("❌ You're already in a multiplayer Mines game!")

        max_bet = await casino_premium.get_max_bet(host.id)
        if not (MIN_ENTRY <= entry <= max_bet):
            return await ctx.send(f"❌ Entry must be 🪙 {MIN_ENTRY:,}–{max_bet:,}.")
        if not (MIN_MINES <= mines <= MAX_MINES):
            return await ctx.send(f"❌ Mine count must be {MIN_MINES}–{MAX_MINES}.")

        if not await casino_wallet.deduct(host.id, entry):
            return await ctx.send("❌ Not enough casino coins.")

        game = MPMinesGame(entry, mines)
        game.add_player(host)
        self._busy.add(host.id)
        self._games.append(game)

        view = MPLobbyView(self, host, game)
        try:
            view.message = await ctx.send(
                f"💣 **{host.display_name}** opened a Mines table — "
                f"entry 🪙 {entry:,}, {mines} mines. Press **Join**!",
                embed=view.build_embed(), view=view)
        except discord.DiscordException:
            await casino_wallet.credit(host.id, entry)
            self._release(game)


async def setup(bot: commands.Bot):
    await bot.add_cog(MinesMultiCog(bot))
