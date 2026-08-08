"""
cogs/spawn.py
-------------
Poketwo-style wild Beyblade spawn system.

How it works
------------
1.  Every message in any channel increments a per-guild message counter.
2.  When the counter hits a random threshold (between MIN_MESSAGES and
    MAX_MESSAGES), a random Beyblade is selected from beyblades.json using
    weighted rarity probabilities.
3.  If a spawn channel is configured (via ;setspawnchannel), spawns ONLY
    appear there. Otherwise they spawn in whichever channel triggered the threshold.
4.  The first user to type  ;claim <name>  in that channel wins the blade.
5.  The spawn clears once claimed (or when a new one spawns in the same guild).

DB functions required in utils/database.py
-------------------------------------------
  load_spawn_state(guild_id)        → dict | None
      Returns {"counter": int, "target": int} or None if not set.

  save_spawn_state(guild_id, counter, target)
      Upserts counter + target for the guild.

  load_active_spawns(guild_id)      → list[dict]
      Returns list of {"bey": {...}, "channel_id": int, "spawned_at": float} or [].

  save_active_spawn(guild_id, channel_id, bey_data, spawned_at)
      Upserts one active spawn row for (guild_id, channel_id).

  clear_active_spawn(guild_id, channel_id)
      Removes the active spawn row for (guild_id, channel_id).
"""

import asyncio
import logging
import random
import time
import traceback
from typing import Optional

import discord
from discord.ext import commands
from discord import ui

from utils.database import (
    add_beyblade_to_inventory,
    clear_active_spawn,
    get_spawn_channel,
    load_active_spawns,
    load_beyblades,
    load_spawn_state,
    save_active_spawn,
    save_spawn_state,
    set_spawn_channel,
    get_user,
    update_user,
)
from utils.embeds import RARITY_EMOJIS, rarity_colour

log = logging.getLogger("beyblade_bot")

# ── Spawn tuning knobs ────────────────────────────────────────────────────────
MIN_MESSAGES = 15
MAX_MESSAGES = 30
SPAWN_TIMEOUT = 60   # seconds before an unclaimed spawn expires (1 minute)

# ── Button-claim quiz knobs ──────────────────────────────────────────────────
CLAIM_CHOICES   = 4   # how many names to show (1 correct + 3 decoys)
WRONG_COOLDOWN  = 8   # seconds a user must wait after a wrong guess

# ── Quick-sell payouts by rarity (mirrored from shop.py) ──────────────────────
BEY_QUICKSELL_VALUE: dict[str, int] = {
    "Common":    250,
    "Rare":      600,
    "Epic":      1_500,
    "Legendary": 4_000,
    "Mythic":    10_000,
    "Ultimate":  20_000,
}

RARITY_WEIGHTS = {
    "Common":    55,
    "Rare":      25,
    "Epic":      12,
    "Legendary":  5,
    "Mythic":     2,
    "Ultimate":   1,
    # "Exclusive" intentionally absent — hardcoded exclusion, cannot spawn by any means
}

# Rarities permanently excluded from all spawns — no command can override this
_NEVER_SPAWN = {"Exclusive"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pick_random_beyblade(beyblades: dict) -> Optional[dict]:
    if not beyblades:
        return None

    tier_map: dict[str, list] = {}
    for name, data in beyblades.items():
        r = data.get("rarity", "Common")
        # Never include booster-exclusive or Exclusive rarity beys
        if data.get("booster_exclusive") or r in _NEVER_SPAWN:
            continue
        tier_map.setdefault(r, []).append(data)

    available_tiers = [
        t for t in RARITY_WEIGHTS
        if t in tier_map and tier_map[t]
    ]
    if not available_tiers:
        fallback = [
            v for v in beyblades.values()
            if not v.get("booster_exclusive") and v.get("rarity", "Common") not in _NEVER_SPAWN
        ]
        return random.choice(fallback) if fallback else None

    weights     = [RARITY_WEIGHTS[t] for t in available_tiers]
    chosen_tier = random.choices(available_tiers, weights=weights, k=1)[0]
    return random.choice(tier_map[chosen_tier])


def _is_valid_text_channel(channel) -> bool:
    """Return True only for a real, sendable TextChannel (not a category/thread/None)."""
    return isinstance(channel, discord.TextChannel)


async def _can_send(channel: discord.TextChannel) -> bool:
    """Return True if the bot has send_messages permission in channel."""
    me = channel.guild.me
    perms = channel.permissions_for(me)
    return perms.send_messages and perms.embed_links


# ── Claim quiz helpers ────────────────────────────────────────────────────────

def _make_choices(correct: dict, beyblades: dict, n: int = CLAIM_CHOICES) -> list[str]:
    """Build `n` shuffled name options: the real one plus believable decoys.

    Decoys prefer the same rarity tier so the answer isn't obvious from the
    rarity colour of the spawn embed.
    """
    correct_name = correct.get("name", "???")
    rarity       = correct.get("rarity")

    pool = [
        d["name"] for d in beyblades.values()
        if d.get("name") and d["name"].lower() != correct_name.lower()
    ]
    same_tier = [
        d["name"] for d in beyblades.values()
        if d.get("name") and d["name"].lower() != correct_name.lower()
        and d.get("rarity") == rarity
    ]

    need   = n - 1
    source = same_tier if len(same_tier) >= need else pool

    if len(source) >= need:
        decoys = random.sample(source, need)
    else:
        decoys = list(source)
        extra  = [x for x in pool if x not in decoys]
        random.shuffle(extra)
        decoys += extra[: need - len(decoys)]

    choices = decoys + [correct_name]
    random.shuffle(choices)
    return choices


class GuessButton(ui.Button):
    """One of the four name options in the ephemeral quiz."""

    def __init__(self, name: str, row: int):
        super().__init__(label=name[:80], style=discord.ButtonStyle.secondary, row=row)
        self.bey_name = name

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "SpawnGuessView" = self.view
        if interaction.user.id != view.user.id:
            return await interaction.response.send_message(
                "That's not your guess panel!", ephemeral=True)
        if view.answered:
            return await interaction.response.defer()

        view.answered = True
        for child in view.children:
            child.disabled = True

        correct = self.bey_name.lower() == view.correct_name.lower()

        if not correct:
            self.style = discord.ButtonStyle.danger
            view.cog._wrong_guess[(view.spawn_key, view.user.id)] = time.time()
            e = discord.Embed(
                title="❌ Wrong Blade!",
                description=(
                    f"**{self.bey_name}** isn't it.\n"
                    f"The blade is still spinning — hit **🎯 Claim** again in "
                    f"**{WRONG_COOLDOWN}s** for a fresh set of names."
                ),
                color=discord.Color.red(),
            )
            await interaction.response.edit_message(embed=e, view=view)
            view.stop()
            return

        # ── Correct answer — try to actually take the spawn ────────────────────
        self.style = discord.ButtonStyle.success
        claimed = await view.cog._take_spawn(view.guild_id, view.spawn_entry)

        if not claimed:
            e = discord.Embed(
                title="😤 Too Slow!",
                description=(
                    f"You had it right — **{view.correct_name}** — but someone "
                    f"else claimed it first."
                ),
                color=discord.Color.orange(),
            )
            await interaction.response.edit_message(embed=e, view=view)
            view.stop()
            return

        e = discord.Embed(
            title="✅ Correct!",
            description=f"**{view.correct_name}** is yours — check the channel!",
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=e, view=view)
        view.stop()

        await view.cog._finish_claim(
            view.guild_id, view.channel, interaction.user, view.spawn_entry
        )


class SpawnGuessView(ui.View):
    """Ephemeral, per-user: four names, one is right."""

    def __init__(self, cog, guild_id: int, channel, user,
                 spawn_entry: dict, choices: list[str]):
        super().__init__(timeout=45)
        self.cog          = cog
        self.guild_id     = guild_id
        self.channel      = channel
        self.user         = user
        self.spawn_entry  = spawn_entry
        self.correct_name = spawn_entry["bey"]["name"]
        self.spawn_key    = (guild_id, spawn_entry["channel_id"], spawn_entry["spawned_at"])
        self.answered     = False

        for i, name in enumerate(choices):
            self.add_item(GuessButton(name, row=i // 2))


class SpawnClaimView(ui.View):
    """Public view sitting under the spawn embed — one Claim button for everyone."""

    def __init__(self, cog, guild_id: int, spawn_entry: dict):
        super().__init__(timeout=SPAWN_TIMEOUT)
        self.cog         = cog
        self.guild_id    = guild_id
        self.spawn_entry = spawn_entry
        self.spawn_key   = (guild_id, spawn_entry["channel_id"], spawn_entry["spawned_at"])
        self.message: Optional[discord.Message] = None

    @ui.button(label="Claim", emoji="🎯", style=discord.ButtonStyle.success)
    async def claim_btn(self, interaction: discord.Interaction, _: ui.Button) -> None:
        state = self.cog._get_guild_state(self.guild_id)

        if self.spawn_entry not in state["active"]:
            return await interaction.response.send_message(
                "💨 That Beyblade is already gone!", ephemeral=True)

        # Per-user cooldown after a wrong guess
        last = self.cog._wrong_guess.get((self.spawn_key, interaction.user.id))
        if last is not None:
            waited = time.time() - last
            if waited < WRONG_COOLDOWN:
                return await interaction.response.send_message(
                    f"⏳ Wrong guess — try again in "
                    f"**{WRONG_COOLDOWN - waited:.1f}s**.", ephemeral=True)

        try:
            beyblades = load_beyblades()
        except Exception as exc:
            log.error(f"[spawn] load_beyblades() failed during claim: {exc}")
            return await interaction.response.send_message(
                "⚠️ Couldn't load the blade list — use `;claim <name>` instead.",
                ephemeral=True)

        choices = _make_choices(self.spawn_entry["bey"], beyblades)

        e = discord.Embed(
            title="🎯 Name That Blade!",
            description=(
                "Look at the spawn image and pick the correct name.\n"
                f"You get **{WRONG_COOLDOWN}s** of cooldown if you're wrong — "
                "and other players are guessing too."
            ),
            color=discord.Color.blurple(),
        )
        e.set_thumbnail(url=self.spawn_entry["bey"].get("image_url") or None)

        view = SpawnGuessView(
            self.cog, self.guild_id, interaction.channel,
            interaction.user, self.spawn_entry, choices,
        )
        await interaction.response.send_message(embed=e, view=view, ephemeral=True)

    def lock(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_timeout(self) -> None:
        self.lock()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


# ── Cog ───────────────────────────────────────────────────────────────────────

class SpawnCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # { guild_id: { "counter": int, "target": int, "active": list[dict], "_lock": Lock } }
        self.spawn_states: dict[int, dict] = {}
        # { (spawn_key, user_id): timestamp of last wrong guess }
        self._wrong_guess: dict[tuple, float] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        """Restore persisted spawn state for every guild the bot can see."""
        for guild in self.bot.guilds:
            await self._load_guild_state(guild.id)
        # Bug #1 fix: start the expiry loop that actually uses SPAWN_TIMEOUT
        self._expiry_task = asyncio.create_task(self._expiry_loop())

    async def cog_unload(self) -> None:
        if hasattr(self, "_expiry_task"):
            self._expiry_task.cancel()
            try:
                await self._expiry_task
            except asyncio.CancelledError:
                pass

    async def _expiry_loop(self) -> None:
        """Every 30s, expire any active spawn older than SPAWN_TIMEOUT and post a fled message."""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(30)
            now = time.time()
            for guild_id, state in list(self.spawn_states.items()):
                # FIX Bug 1: remove expired entries under the lock so ;claim
                # can't race and hit ValueError on .remove()
                async with state["_lock"]:
                    expired = [s for s in state["active"] if now - s["spawned_at"] >= SPAWN_TIMEOUT]
                    for entry in expired:
                        state["active"].remove(entry)

                # Async I/O (send + DB) happens outside the lock so we don't
                # block ;claim for the duration of a network call
                for entry in expired:
                    bey_name = entry["bey"]["name"]
                    self._lock_spawn_message(entry)
                    self._wrong_guess = {
                        k: v for k, v in self._wrong_guess.items()
                        if k[0] != (guild_id, entry["channel_id"], entry["spawned_at"])
                    }
                    channel  = self.bot.get_channel(entry["channel_id"])
                    if channel and _is_valid_text_channel(channel):
                        try:
                            await channel.send(
                                f"💨 The wild **{bey_name}** fled before anyone could claim it!"
                            )
                        except discord.DiscordException as e:
                            log.warning(f"[spawn] expiry fled message failed: {e}")
                    try:
                        clear_active_spawn(guild_id, entry["channel_id"])
                    except Exception as e:
                        log.warning(f"[spawn] clear_active_spawn() on expiry failed: {e}")

    async def _load_guild_state(self, guild_id: int) -> dict:
        """Load from DB (if available) or create a fresh state dict."""
        # FIX Issue 1: load persisted counter/target
        try:
            saved = load_spawn_state(guild_id)
        except Exception as e:
            log.warning(f"[spawn] load_spawn_state({guild_id}) failed: {e}")
            saved = None

        counter = saved["counter"] if saved else 0
        target  = saved["target"]  if saved else random.randint(MIN_MESSAGES, MAX_MESSAGES)

        # Bug #2 fix: load persisted active spawns, discard any older than SPAWN_TIMEOUT
        try:
            raw_active = load_active_spawns(guild_id)
            now        = time.time()
            active     = [s for s in raw_active if now - s.get("spawned_at", 0) < SPAWN_TIMEOUT]
            stale      = [s for s in raw_active if s not in active]
            for s in stale:
                log.info(f"[spawn] Discarding stale spawn '{s['bey']['name']}' from guild {guild_id}")
                try:
                    clear_active_spawn(guild_id, s["channel_id"])
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"[spawn] load_active_spawns({guild_id}) failed: {e}")
            active = []

        state = {
            "counter": counter,
            "target":  target,
            "active":  active,
            "_lock":   asyncio.Lock(),  # FIX Issue 5: per-guild lock
        }
        self.spawn_states[guild_id] = state
        return state

    def _get_guild_state(self, guild_id: int) -> dict:
        """Return existing in-memory state or create a fresh default (no DB load)."""
        if guild_id not in self.spawn_states:
            self.spawn_states[guild_id] = {
                "counter": 0,
                "target":  random.randint(MIN_MESSAGES, MAX_MESSAGES),
                "active":  [],
                "_lock":   asyncio.Lock(),
            }
        return self.spawn_states[guild_id]

    # ── Core spawn logic ───────────────────────────────────────────────────────

    async def _do_spawn(self, channel: discord.TextChannel,
                        exclusive_only: bool = False) -> None:
        """Select a random Beyblade and post the spawn alert in channel.

        ``exclusive_only`` — admin-forced spawn of an Exclusive-rarity bey
        (normally these never spawn naturally).
        """

        # FIX Issue 3: validate channel type
        if not _is_valid_text_channel(channel):
            log.warning(f"[spawn] _do_spawn called with non-TextChannel: {channel!r}")
            return

        # FIX Issue 2: check permissions before attempting to send
        if not await _can_send(channel):
            log.warning(
                f"[spawn] Missing send_messages or embed_links in "
                f"#{channel.name} ({channel.id}) guild={channel.guild.id}"
            )
            return

        # FIX Issue 4: wrap load_beyblades
        try:
            beyblades = load_beyblades()
        except Exception as e:
            log.error(f"[spawn] load_beyblades() failed: {e}\n{traceback.format_exc()}")
            return  # No user-facing message — bot can't send reliably here

        # FIX Issue 4: wrap _pick_random_beyblade
        try:
            if exclusive_only:
                # Admin-forced Exclusive spawn: bypass the normal weights and
                # the _NEVER_SPAWN filter, pick uniformly among Exclusives.
                pool = [d for d in beyblades.values()
                        if d.get("rarity") == "Exclusive"]
                chosen = random.choice(pool) if pool else None
            else:
                chosen = _pick_random_beyblade(beyblades)
        except Exception as e:
            log.error(f"[spawn] blade pick raised: {e}\n{traceback.format_exc()}")
            return

        if not chosen:
            log.warning("[spawn] No valid Beyblade found")
            return

        guild_id = channel.guild.id
        state    = self._get_guild_state(guild_id)

        # FIX Bug 2: hold the lock while mutating state["active"] so ;claim
        # and the expiry loop can't see a half-updated list
        async with state["_lock"]:
            replaced    = [s for s in state["active"] if s["channel_id"] == channel.id]
            state["active"] = [s for s in state["active"] if s["channel_id"] != channel.id]
            spawn_entry = {"bey": chosen, "channel_id": channel.id, "spawned_at": time.time()}
            state["active"].append(spawn_entry)

        # Send "fled" messages for any evicted spawns (outside lock — async I/O)
        for evicted in replaced:
            self._lock_spawn_message(evicted)
            try:
                await channel.send(
                    f"💨 The wild **{evicted['bey']['name']}** fled before anyone could claim it!"
                )
            except discord.DiscordException:
                pass

        # Persist new active spawn
        try:
            save_active_spawn(guild_id, channel.id, chosen, spawn_entry["spawned_at"])
        except Exception as e:
            log.warning(f"[spawn] save_active_spawn() failed: {e}")

        rarity = chosen.get("rarity", "Common")
        emoji  = RARITY_EMOJIS.get(rarity, "")

        embed = discord.Embed(
            title       = "🌀 A Wild Beyblade Has Spawned!",
            description = (
                "A powerful entity is spinning out of control in the arena!\n\n"
                "Press **🎯 Claim** and pick the right name from four options — "
                "or type **`;claim <name>`** if you already know it."
            ),
            color = rarity_colour(rarity),
        )
        img_url = chosen.get("image_url")
        embed.set_image(url=img_url if img_url else None)
        embed.set_footer(text="Look closely at the blade... Who could it be?")

        claim_view = SpawnClaimView(self, guild_id, spawn_entry)
        spawn_entry["view"] = claim_view

        # FIX Issue 4: wrap the actual send
        try:
            claim_view.message = await channel.send(embed=embed, view=claim_view)
        except discord.DiscordException as e:
            log.error(f"[spawn] channel.send() failed in #{channel.name}: {e}")
            # Roll back — spawn didn't land, remove it from active
            try:
                claim_view.stop()
            except Exception:
                pass
            async with state["_lock"]:
                state["active"] = [s for s in state["active"] if s is not spawn_entry]
            try:
                clear_active_spawn(guild_id, channel.id)
            except Exception:
                pass

    # ── Shared claim plumbing (used by both the button and ;claim) ─────────────

    async def _take_spawn(self, guild_id: int, entry: dict) -> bool:
        """Atomically remove `entry` from the active list.

        Returns True if this caller won the race, False if it was already gone.
        """
        state = self._get_guild_state(guild_id)
        async with state["_lock"]:
            if entry not in state["active"]:
                return False
            state["active"].remove(entry)
        return True

    def _lock_spawn_message(self, entry: dict) -> None:
        """Disable the Claim button on a spawn that's been resolved."""
        view = entry.get("view")
        if view is None:
            return
        view.lock()
        if getattr(view, "message", None):
            asyncio.create_task(self._safe_edit_view(view))
        try:
            view.stop()
        except Exception:
            pass

    @staticmethod
    async def _safe_edit_view(view) -> None:
        try:
            await view.message.edit(view=view)
        except Exception:
            pass

    async def _finish_claim(self, guild_id: int, channel, user, entry: dict) -> None:
        """Award the blade, announce it, and offer the duplicate sale.

        The caller must already have won `_take_spawn`.
        """
        spawned = entry["bey"]

        self._lock_spawn_message(entry)

        # Drop this spawn's wrong-guess cooldowns so the dict can't grow forever
        key = (guild_id, entry["channel_id"], entry["spawned_at"])
        self._wrong_guess = {k: v for k, v in self._wrong_guess.items() if k[0] != key}

        try:
            clear_active_spawn(guild_id, entry["channel_id"])
        except Exception as exc:
            log.warning(f"[spawn] clear_active_spawn() on claim failed: {exc}")

        # Check ownership BEFORE adding so we never read stale DB data
        pre_profile   = get_user(user.id)
        already_owned = any(
            b.lower() == spawned["name"].lower()
            for b in pre_profile.get("inventory", [])
        )

        add_beyblade_to_inventory(user.id, spawned["name"])

        # Lifetime catch counter for the /leaderboard catches board. Counted
        # here rather than derived from inventory size, because selling a
        # duplicate shrinks the inventory and a catch that already happened
        # must not un-happen. Read-modify-write under the users lock so a
        # simultaneous catch elsewhere cannot clobber it.
        try:
            from utils.database import mutate_user
            from utils import ranked as RK
            mutate_user(user.id, RK.record_catch)
        except Exception as exc:                         # noqa: BLE001
            log.warning(f"[spawn] catch counter failed for {user.id}: {exc}")

        rarity = spawned.get("rarity", "Common")
        emoji  = RARITY_EMOJIS.get(rarity, "")

        embed = discord.Embed(
            title       = f"🎉 {user.display_name} caught a Beyblade!",
            description = (
                f"**{emoji} {spawned['name']}** ({rarity}) has been added "
                f"to your inventory!\n\n"
                f"Use `;profile` to see your collection."
            ),
            color = rarity_colour(rarity),
        )
        embed.set_thumbnail(url=spawned.get("image_url") or None)
        try:
            await channel.send(embed=embed)
        except discord.DiscordException as exc:
            log.warning(f"[spawn] claim announce failed: {exc}")

        # Quest system event
        try:
            self.bot.dispatch("beycord_spawn_catch", int(user.id))
        except Exception:
            pass

        if already_owned:
            await self._prompt_spawn_duplicate_sale(channel, user, spawned, rarity)

    # ── on_message ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        # Don't let commands count toward the spawn threshold
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        guild_id = message.guild.id
        state    = self._get_guild_state(guild_id)

        # FIX Issue 5: lock to prevent concurrent threshold triggers
        async with state["_lock"]:
            state["counter"] += 1
            if state["counter"] < state["target"]:
                # Persist counter only every 5 messages to reduce DB writes
                if state["counter"] % 5 == 0:
                    try:
                        save_spawn_state(guild_id, state["counter"], state["target"])
                    except Exception as e:
                        log.debug(f"[spawn] save_spawn_state() failed: {e}")
                return

            # Threshold hit — reset before the async call to prevent duplicate triggers
            state["counter"] = 0
            state["target"]  = random.randint(MIN_MESSAGES, MAX_MESSAGES)

            # FIX Issue 1: persist reset immediately
            try:
                save_spawn_state(guild_id, state["counter"], state["target"])
            except Exception as e:
                log.warning(f"[spawn] save_spawn_state() after reset failed: {e}")

        # Decide WHERE to spawn (outside lock — pure resolution, no shared state mutation)
        configured_id = get_spawn_channel(guild_id)
        if configured_id:
            spawn_channel = message.guild.get_channel(configured_id)
            # FIX Issue 3: validate resolved channel
            if not _is_valid_text_channel(spawn_channel):
                spawn_channel = message.channel
        else:
            spawn_channel = message.channel

        await self._do_spawn(spawn_channel)

    # ── ;claim ────────────────────────────────────────────────────────────────

    @commands.command(name="claim")
    async def claim_beyblade(self, ctx: commands.Context, *, name: Optional[str] = None) -> None:
        """Claim the active wild Beyblade spawn in this server."""
        state = self._get_guild_state(ctx.guild.id)

        if not state["active"]:
            return await ctx.send("❌ There are no wild Beyblades active right now.")

        if not name:
            return await ctx.send(
                f"❌ You need to specify a name! Example: `{ctx.prefix}claim Dragoon`"
            )

        async with state["_lock"]:
            # Find matching spawn in this channel
            match = next(
                (s for s in state["active"]
                 if s["bey"]["name"].lower() == name.strip().lower()
                 and s["channel_id"] == ctx.channel.id),
                None
            )
            if match is None:
                # Check if it spawned in another channel
                other = next(
                    (s for s in state["active"]
                     if s["bey"]["name"].lower() == name.strip().lower()),
                    None
                )
                if other:
                    spawn_ch = ctx.guild.get_channel(other["channel_id"])
                    mention  = spawn_ch.mention if spawn_ch else "another channel"
                    return await ctx.send(f"❌ That Beyblade spawned in {mention}!")
                return await ctx.send(f"❌ That's not right, {ctx.author.mention}! Keep trying.")

            # ── Success ───────────────────────────────────────────────────────────
            state["active"].remove(match)

        await self._finish_claim(ctx.guild.id, ctx.channel, ctx.author, match)

    # ── ;setspawnchannel ──────────────────────────────────────────────────────

    @commands.command(name="setspawnchannel")
    @commands.has_permissions(administrator=True)
    async def set_spawn_channel_cmd(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """
        Lock Beyblade spawns to a specific channel.

        Usage:
          ;setspawnchannel #channel   → set spawn channel
          ;setspawnchannel            → clear it (spawns anywhere again)

        Requires Administrator permission.
        """
        if channel is None:
            set_spawn_channel(ctx.guild.id, None)
            embed = discord.Embed(
                title       = "✅ Spawn Channel Cleared",
                description = (
                    "Wild Beyblade spawns will now appear in **any channel** "
                    "based on message activity."
                ),
                color = discord.Color.green(),
            )
        else:
            set_spawn_channel(ctx.guild.id, channel.id)
            embed = discord.Embed(
                title       = "✅ Spawn Channel Set",
                description = (
                    f"Wild Beyblade spawns will now **only** appear in {channel.mention}.\n\n"
                    f"Message activity anywhere in the server still counts toward the "
                    f"spawn threshold — spawns just always land in {channel.mention}."
                ),
                color = discord.Color.green(),
            )
            embed.set_footer(text=f"Run '{ctx.prefix}setspawnchannel' with no argument to clear this.")

        await ctx.send(embed=embed)

    @set_spawn_channel_cmd.error
    async def set_spawn_channel_error(self, ctx: commands.Context, error) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You need **Administrator** permission to use this command.")
        elif isinstance(error, commands.ChannelNotFound):
            await ctx.send("❌ Channel not found. Mention the channel with #, e.g. `;setspawnchannel #spawns`")
        else:
            raise error

    async def _prompt_spawn_duplicate_sale(
        self,
        channel,
        user,
        spawned_bey: dict,
        rarity: str
    ) -> None:
        """
        Prompt user if they want to auto-sell a duplicate Beyblade they caught from a spawn.
        Uses quicksell values for payouts.
        """
        bey_name = spawned_bey["name"]
        refund = BEY_QUICKSELL_VALUE.get(rarity, BEY_QUICKSELL_VALUE["Common"])
        r_emoji = RARITY_EMOJIS.get(rarity, "⚪")

        # Confirmation view for auto-sale
        class SpawnDuplicateSaleView(ui.View):
            def __init__(self):
                super().__init__(timeout=30)
                self.sell_duplicate = False

            @ui.button(label="✅ Auto-Sell Duplicate", style=discord.ButtonStyle.success)
            async def sell_btn(self, interaction: discord.Interaction, _: ui.Button):
                if interaction.user.id != user.id:
                    await interaction.response.send_message("Not your decision!", ephemeral=True)
                    return
                await interaction.response.defer()
                self.sell_duplicate = True
                self.stop()

            @ui.button(label="❌ Keep Both", style=discord.ButtonStyle.secondary)
            async def keep_btn(self, interaction: discord.Interaction, _: ui.Button):
                if interaction.user.id != user.id:
                    await interaction.response.send_message("Not your decision!", ephemeral=True)
                    return
                await interaction.response.defer()
                self.stop()

        view = SpawnDuplicateSaleView()
        prompt_msg = await channel.send(
            f"⚠️ **Duplicate Caught!**\n\n"
            f"{user.mention}, you already own {r_emoji} **{bey_name}** in your inventory.\n\n"
            f"Would you like to auto-sell this one for **{refund:,} coins** (quicksell value)?",
            view=view,
        )

        await view.wait()
        await prompt_msg.edit(view=None)

        if view.sell_duplicate:
            # Re-fetch fresh profile after add_beyblade_to_inventory was called
            user_profile = get_user(user.id)
            inventory: list[str] = user_profile.get("inventory", [])

            # Remove the LAST matching copy — that's the one just claimed (the duplicate).
            # Removing the first copy would silently delete the user's original.
            for i in range(len(inventory) - 1, -1, -1):
                if inventory[i].lower() == bey_name.lower():
                    inventory.pop(i)
                    break

            user_profile["inventory"] = inventory
            user_profile["coins"] = user_profile.get("coins", 0) + refund
            update_user(user.id, user_profile)

            embed = discord.Embed(
                title="💸 Duplicate Auto-Sold!",
                description=(
                    f"{r_emoji} **{bey_name}** sold for **{refund:,} coins**!\n"
                    f"Rarity: {rarity}\n"
                    f"💰 New balance: **{user_profile['coins']:,} coins**"
                ),
                color=discord.Color.gold(),
            )
            await channel.send(embed=embed)
        else:
            await channel.send(
                f"👍 Kept both copies of **{bey_name}**! "
                f"You can always `;quicksell` or `;sellbey` one later."
            )

    # ── ;duplicatesell ────────────────────────────────────────────────────────

    @commands.command(name="duplicatesell")
    async def duplicate_sell(self, ctx: commands.Context) -> None:
        """
        Scan your inventory for duplicate Beyblades and sell all extras.

        Keeps ONE copy of each Bey, sells every duplicate for its quicksell
        value, and shows you a summary before you confirm.

        Usage:  ;duplicatesell
        """
        user_profile = get_user(ctx.author.id)
        inventory: list[str] = user_profile.get("inventory", [])

        if not inventory:
            return await ctx.send("❌ Your inventory is empty!")

        # ── Build duplicate map ───────────────────────────────────────────────
        # { normalised_name: [original_string, original_string, ...] }
        seen: dict[str, list[str]] = {}
        for bey in inventory:
            key = bey.lower().strip()
            seen.setdefault(key, []).append(bey)

        # Only entries with more than 1 copy
        duplicates = {k: v for k, v in seen.items() if len(v) > 1}

        if not duplicates:
            return await ctx.send("✅ No duplicates found — your inventory is clean!")

        # ── Load bey data for rarity lookup ──────────────────────────────────
        try:
            all_beys = load_beyblades()
        except Exception as e:
            log.error(f"[duplicatesell] load_beyblades() failed: {e}")
            all_beys = {}

        # Build a name→rarity lookup (case-insensitive)
        rarity_lookup: dict[str, str] = {
            v["name"].lower(): v.get("rarity", "Common")
            for v in all_beys.values()
        }

        # ── Calculate what will be sold ───────────────────────────────────────
        # For each duplicate group: keep 1, sell the rest
        to_sell: list[tuple[str, str, int, int, int]] = []  # (display_name, rarity, extras, sell_value, earned)
        total_coins = 0

        for key, copies in duplicates.items():
            display_name = copies[0]          # use the stored capitalisation
            rarity       = rarity_lookup.get(key, "Common")
            sell_value   = BEY_QUICKSELL_VALUE.get(rarity, BEY_QUICKSELL_VALUE["Common"])
            extras       = len(copies) - 1   # how many will be sold
            earned       = sell_value * extras
            total_coins += earned
            to_sell.append((display_name, rarity, extras, sell_value, earned))

        # ── Build preview embed ───────────────────────────────────────────────
        lines = []
        for display_name, rarity, extras, sell_value, earned in to_sell:
            r_emoji = RARITY_EMOJIS.get(rarity, "⚪")
            lines.append(
                f"{r_emoji} **{display_name}** — {extras}× duplicate(s) "
                f"@ {sell_value:,} = **{earned:,} coins**"
            )

        preview_text = "\n".join(lines) if lines else "Nothing"

        embed = discord.Embed(
            title       = "🗑️ Duplicate Sell — Preview",
            description = (
                f"The following duplicates will be **removed** from your inventory.\n"
                f"You will keep **1 copy** of each.\n\n"
                f"{preview_text}\n\n"
                f"💰 **Total payout: {total_coins:,} coins**"
            ),
            color = discord.Color.orange(),
        )
        embed.set_footer(text="This cannot be undone. Confirm below.")

        # ── Confirmation view ─────────────────────────────────────────────────
        class DupeSellView(ui.View):
            def __init__(self):
                super().__init__(timeout=30)
                self.confirmed = False

            @ui.button(label="✅ Sell All Duplicates", style=discord.ButtonStyle.danger)
            async def confirm_btn(self, interaction: discord.Interaction, _: ui.Button):
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("Not your inventory!", ephemeral=True)
                    return
                await interaction.response.defer()
                self.confirmed = True
                self.stop()

            @ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
            async def cancel_btn(self, interaction: discord.Interaction, _: ui.Button):
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("Not your inventory!", ephemeral=True)
                    return
                await interaction.response.defer()
                self.stop()

        view     = DupeSellView()
        prompt   = await ctx.send(embed=embed, view=view)
        await view.wait()
        await prompt.edit(view=None)

        if not view.confirmed:
            return await ctx.send("❌ Duplicate sell cancelled. Nothing was changed.")

        # ── Execute the sale ──────────────────────────────────────────────────
        # Re-fetch fresh profile right before mutating to avoid race conditions
        user_profile = get_user(ctx.author.id)
        inventory    = user_profile.get("inventory", [])

        for display_name, rarity, extras, sell_value, earned in to_sell:
            key     = display_name.lower().strip()
            removed = 0
            new_inv = []
            for bey in inventory:
                # Keep up to 1 copy; remove the rest
                if bey.lower().strip() == key and removed < extras:
                    removed += 1   # skip (sell) this copy
                else:
                    new_inv.append(bey)
            inventory = new_inv

        user_profile["inventory"] = inventory
        user_profile["coins"]     = user_profile.get("coins", 0) + total_coins
        update_user(ctx.author.id, user_profile)

        # ── Result embed ──────────────────────────────────────────────────────
        sold_count = sum(extras for _, _, extras, _, _ in to_sell)
        result_embed = discord.Embed(
            title       = "💸 Duplicates Sold!",
            description = (
                f"Sold **{sold_count}** duplicate Beyblade(s) across "
                f"**{len(to_sell)}** unique blade(s).\n\n"
                f"💰 Earned: **{total_coins:,} coins**\n"
                f"💳 New balance: **{user_profile['coins']:,} coins**"
            ),
            color = discord.Color.gold(),
        )
        await ctx.send(embed=result_embed)

    # ── ;spawninfo ────────────────────────────────────────────────────────────

    @commands.command(name="spawninfo")
    async def spawn_info(self, ctx: commands.Context) -> None:
        """Show the current spawn channel config for this server."""
        configured_id = get_spawn_channel(ctx.guild.id)
        state         = self._get_guild_state(ctx.guild.id)

        if configured_id:
            ch      = ctx.guild.get_channel(configured_id)
            ch_text = ch.mention if ch else f"<deleted channel {configured_id}>"
        else:
            ch_text = "Any channel (not set)"

        if state["active"]:
            active_text = "\n".join(
                f"**{s['bey']['name']}** in <#{s['channel_id']}>"
                for s in state["active"]
            )
        else:
            active_text = "None"

        embed = discord.Embed(title="🌀 Spawn Info", color=discord.Color.blurple())
        embed.add_field(name="Spawn Channel", value=ch_text,     inline=False)
        embed.add_field(name="Active Spawn",  value=active_text, inline=False)
        embed.add_field(
            name  = "Progress",
            value = f"`{state['counter']}/{state['target']}` messages until next spawn",
            inline=False,
        )
        await ctx.send(embed=embed)



async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SpawnCog(bot))
