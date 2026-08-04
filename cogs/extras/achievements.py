"""
achievements.py  —  🏅 Achievements

Data-driven badges. Every achievement is a row in ACHIEVEMENTS with a `check`
that reads a profile dict and returns True — no per-achievement code paths, so
adding one is a single line.

Two ways they unlock:
  * reactively, off battle / spawn / level events
  * retroactively, whenever the player runs `;achievements`

The retroactive pass matters: there are already players with 59 wins and 76
blades who'd otherwise see an empty badge list on day one.

Storage:
    profile["achievements"] = {"<id>": unix_timestamp}

Commands:
    ;achievements            → your badges, by category (aliases: ;ach, ;badges)
    ;achievements <category> → one category in full
    ;achievements @user      → someone else's
"""

import time
from typing import Callable, Optional

import discord
from discord.ext import commands

from utils.database import get_user, update_user
from utils.mobile_ui import MobileListView, bar as ui_bar

# Existing players get retroactive credit the first time they run ;achievements.
# Measured against the live registry that first pass pays out ~3.5M coins across
# 224 real players (~8.6% of total supply) — it lands on the people who actually
# stuck around, and it slightly dilutes the one wallet holding 48% of everything.
# If that feels like too much, drop this to e.g. 0.25; it only scales the
# catch-up pass, never rewards earned normally afterwards.
FIRST_PASS_SCALE = 1.0

CATEGORIES = {
    "battle":     ("⚔️", "Battle"),
    "collection": ("🌀", "Collection"),
    "progress":   ("📈", "Progress"),
    "wealth":     ("💰", "Wealth"),
    "mastery":    ("🔰", "Mastery"),
    "social":     ("🛡️", "Social"),
}


class Achievement:
    __slots__ = ("id", "name", "desc", "emoji", "cat", "reward", "check", "progress")

    def __init__(self, id: str, name: str, desc: str, emoji: str, cat: str,
                 reward: int, check: Callable[[dict], bool],
                 progress: Optional[Callable[[dict], tuple[int, int]]] = None):
        self.id, self.name, self.desc = id, name, desc
        self.emoji, self.cat, self.reward = emoji, cat, reward
        self.check, self.progress = check, progress


def _wins(p: dict) -> int:
    return p.get("wins", 0)


def _blades(p: dict) -> int:
    return len(p.get("inventory") or [])


def _unique_blades(p: dict) -> int:
    return len({b.lower() for b in (p.get("inventory") or [])})


def _mastery_levels(p: dict) -> int:
    from .mastery import level_from_xp
    return sum(level_from_xp(v.get("xp", 0))
               for v in (p.get("mastery") or {}).values()
               if isinstance(v, dict))


def _max_mastery(p: dict) -> int:
    from .mastery import level_from_xp
    vals = [level_from_xp(v.get("xp", 0))
            for v in (p.get("mastery") or {}).values() if isinstance(v, dict)]
    return max(vals) if vals else 0


def _threshold(getter: Callable[[dict], int], target: int):
    """Build (check, progress) for a simple 'reach N' achievement."""
    return (lambda p: getter(p) >= target,
            lambda p: (min(getter(p), target), target))


def _tiered(prefix: str, name_fmt: str, desc_fmt: str, emoji: str, cat: str,
            getter: Callable[[dict], int],
            tiers: list[tuple[int, int]]) -> list[Achievement]:
    """tiers = [(target, reward), …]"""
    out = []
    for target, reward in tiers:
        chk, prog = _threshold(getter, target)
        out.append(Achievement(
            id=f"{prefix}_{target}",
            name=name_fmt.format(n=target),
            desc=desc_fmt.format(n=target),
            emoji=emoji, cat=cat, reward=reward,
            check=chk, progress=prog,
        ))
    return out


ACHIEVEMENTS: list[Achievement] = [
    # ── Battle ───────────────────────────────────────────────────────────────
    *_tiered("wins", "{n} Wins", "Win {n} battles", "⚔️", "battle", _wins,
             [(1, 500), (10, 2_000), (50, 10_000), (100, 25_000), (500, 100_000)]),
    *_tiered("streak", "{n} Win Streak", "Reach a {n}-battle win streak",
             "🔥", "battle", lambda p: p.get("best_streak", 0),
             [(3, 1_500), (5, 5_000), (10, 20_000), (25, 75_000)]),
    Achievement("first_blood", "First Blood", "Win your very first battle",
                "🩸", "battle", 1_000, *_threshold(_wins, 1)),
    Achievement("veteran", "Veteran", "Fight 100 battles, win or lose",
                "🎖️", "battle", 15_000,
                *_threshold(lambda p: p.get("wins", 0) + p.get("losses", 0), 100)),

    # ── Collection ───────────────────────────────────────────────────────────
    *_tiered("collect", "Collector {n}", "Own {n} Beyblades", "🌀", "collection",
             _blades, [(1, 500), (10, 2_500), (25, 10_000), (50, 30_000),
                       (100, 100_000)]),
    *_tiered("unique", "{n} Unique Blades", "Own {n} different Beyblades",
             "✨", "collection", _unique_blades,
             [(5, 2_000), (20, 15_000), (40, 50_000)]),

    # ── Progress ─────────────────────────────────────────────────────────────
    *_tiered("level", "Level {n}", "Reach trainer level {n}", "📈", "progress",
             lambda p: p.get("level", 0),
             [(5, 1_000), (10, 3_000), (25, 15_000), (50, 60_000), (100, 250_000)]),
    *_tiered("rank", "{n} Rank Points", "Reach {n} rank score", "🏆", "progress",
             lambda p: p.get("rank_score", 0),
             [(100, 2_000), (500, 12_000), (1_000, 40_000)]),

    # ── Wealth ───────────────────────────────────────────────────────────────
    *_tiered("rich", "{n} Beycoins", "Hold {n} Beycoins at once", "💰", "wealth",
             lambda p: p.get("coins", 0),
             [(10_000, 1_000), (100_000, 10_000), (1_000_000, 100_000)]),

    # ── Mastery ──────────────────────────────────────────────────────────────
    Achievement("mastery_first", "Getting the Feel",
                "Reach Mastery 1 on any blade", "🔰", "mastery", 2_000,
                *_threshold(_max_mastery, 1)),
    Achievement("mastery_5", "Signature Blade",
                "Reach Mastery 5 on a single blade", "🔰", "mastery", 15_000,
                *_threshold(_max_mastery, 5)),
    Achievement("mastery_max", "Grandmaster",
                "Max out Mastery 10 on a blade", "👑", "mastery", 75_000,
                *_threshold(_max_mastery, 10)),
    Achievement("mastery_spread", "Well Rounded",
                "Accumulate 20 mastery levels across your blades",
                "📚", "mastery", 25_000, *_threshold(_mastery_levels, 20)),

    # ── Social ───────────────────────────────────────────────────────────────
    Achievement("clan_member", "Not Alone", "Join a clan",
                "🛡️", "social", 3_000,
                check=lambda p: bool(p.get("_in_clan")),
                progress=lambda p: (1 if p.get("_in_clan") else 0, 1)),
    Achievement("clan_founder", "Founder", "Found your own clan",
                "👑", "social", 10_000,
                check=lambda p: bool(p.get("_clan_owner")),
                progress=lambda p: (1 if p.get("_clan_owner") else 0, 1)),
]

BY_ID = {a.id: a for a in ACHIEVEMENTS}


def _augment(user_id: int, profile: dict) -> dict:
    """Add derived flags the checks need but the profile doesn't store."""
    p = dict(profile)
    try:
        from cogs.clans import clan_data as cd
        clan = cd.clan_of(user_id)
        p["_in_clan"]    = clan is not None
        p["_clan_owner"] = bool(clan and clan.get("owner_id") == user_id)
    except Exception:
        p["_in_clan"] = p["_clan_owner"] = False
    return p


def evaluate(user_id: int) -> list[Achievement]:
    """Unlock anything newly earned. Returns the achievements just unlocked."""
    profile = get_user(user_id)
    earned  = profile.get("achievements")
    if not isinstance(earned, dict):
        earned = {}

    view       = _augment(user_id, profile)
    first_pass = not earned          # never evaluated before -> retroactive catch-up
    newly, pay = [], 0
    now = time.time()

    for ach in ACHIEVEMENTS:
        if ach.id in earned:
            continue
        try:
            if ach.check(view):
                earned[ach.id] = now
                newly.append(ach)
                pay += ach.reward
        except Exception:
            continue

    if newly:
        if first_pass and FIRST_PASS_SCALE != 1.0:
            pay = int(pay * FIRST_PASS_SCALE)
        profile["achievements"] = earned
        profile["coins"] = profile.get("coins", 0) + pay
        update_user(user_id, profile)
    return newly


def _bar(done: int, total: int, width: int = 8) -> str:
    return ui_bar(done, total, width)


class AchievementsCog(commands.Cog, name="Achievements"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Announce helper ──────────────────────────────────────────────────────
    async def _announce(self, channel, user_id: int, newly: list[Achievement]):
        if not newly or channel is None:
            return
        total = sum(a.reward for a in newly)
        lines = [f"{a.emoji} **{a.name}** — {a.desc}  `+🪙 {a.reward:,}`"
                 for a in newly[:6]]
        if len(newly) > 6:
            lines.append(f"…and {len(newly) - 6} more")
        e = discord.Embed(
            title="🏅  Achievement Unlocked!",
            description=f"<@{user_id}>\n\n" + "\n".join(lines),
            color=0xf1c40f,
        )
        e.set_footer(text=f"Reward: 🪙 {total:,}")
        try:
            await channel.send(embed=e)
        except Exception:
            pass

    # ── Event hooks ──────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_beycord_battle_blades(self, result: dict):
        channel = result.get("channel")
        for uid in result.get("participants", []):
            try:
                newly = evaluate(int(uid))
                await self._announce(channel, int(uid), newly)
            except Exception:
                continue

    @commands.Cog.listener()
    async def on_beycord_spawn_catch(self, user_id: int):
        try:
            evaluate(int(user_id))
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_level_up(self, user_id: int, old_level: int,
                          new_level: int, channel=None):
        try:
            newly = evaluate(int(user_id))
            await self._announce(channel, int(user_id), newly)
        except Exception:
            pass

    # ── ;achievements ────────────────────────────────────────────────────────
    @commands.command(name="achievements",
                      aliases=["ach", "badges", "achievement"])
    async def achievements(self, ctx: commands.Context, *, arg: str = None):
        """🏅 Your badges. Runs a catch-up pass so old progress counts."""
        target = ctx.message.mentions[0] if ctx.message.mentions else ctx.author

        category = None
        if arg and not ctx.message.mentions:
            key = arg.strip().lower()
            if key in CATEGORIES:
                category = key
            else:
                return await ctx.send(
                    "❌ Unknown category. Options: "
                    + " · ".join(f"`{c}`" for c in CATEGORIES))

        newly = evaluate(target.id)          # retroactive catch-up

        view = AchievementHub(ctx.author, target, category)
        view.message = await ctx.send(embed=view.embed(), view=view)

        if newly:
            await self._announce(ctx.channel, target.id, newly)


class CategorySelect(discord.ui.Select):
    """Phone-friendly replacement for ';ach <category>' — no typing needed."""

    def __init__(self, current: Optional[str]):
        opts = [discord.SelectOption(
            label="Overview", value="__all__", emoji="🏅",
            description="progress across every category",
            default=(current is None))]
        for key, (emoji, label) in CATEGORIES.items():
            opts.append(discord.SelectOption(
                label=label, value=key, emoji=emoji,
                default=(current == key)))
        super().__init__(placeholder="📂 Pick a category…", options=opts, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: "AchievementHub" = self.view
        if interaction.user.id != view.owner.id:
            return await interaction.response.send_message(
                "That's not your list.", ephemeral=True)
        view.category = None if self.values[0] == "__all__" else self.values[0]
        view.page = 0
        view.rebuild()
        await interaction.response.edit_message(embed=view.embed(), view=view)


class AchievementHub(discord.ui.View):
    """Overview + per-category pages, all inside one message.

    Sized for a phone: 4 badges a page, one category dropdown, two arrows.
    """

    PAGE = 3          # locked rows are 2-3 lines each — 3 fits a phone

    def __init__(self, owner, target, category: Optional[str] = None):
        super().__init__(timeout=180)
        self.owner    = owner
        self.target   = target
        self.category = category
        self.page     = 0
        self.message  = None
        self.rebuild()

    # ── Data ─────────────────────────────────────────────────────────────────
    def _state(self):
        profile = get_user(self.target.id)
        earned  = profile.get("achievements") or {}
        return earned, _augment(self.target.id, profile)

    def _rows(self):
        return [a for a in ACHIEVEMENTS
                if self.category is None or a.cat == self.category]

    def _pages(self) -> int:
        if self.category is None:
            return 1
        return max(1, (len(self._rows()) + self.PAGE - 1) // self.PAGE)

    # ── Layout ───────────────────────────────────────────────────────────────
    def rebuild(self):
        self.clear_items()
        self.add_item(CategorySelect(self.category))
        if self._pages() > 1:
            self.add_item(_AchPrev(self.page == 0))
            self.add_item(_AchLabel(self.page + 1, self._pages()))
            self.add_item(_AchNext(self.page >= self._pages() - 1))

    def embed(self) -> discord.Embed:
        earned, view = self._state()

        if self.category is None:
            # One field with six short lines beats six inline fields — Discord
            # mobile stacks inline fields two-wide and it ends up 12 lines tall.
            grid = []
            for key, (emoji, label) in CATEGORIES.items():
                cat_all  = [a for a in ACHIEVEMENTS if a.cat == key]
                cat_done = [a for a in cat_all if a.id in earned]
                grid.append(f"{emoji} `{_bar(len(cat_done), len(cat_all), 6)}` "
                            f"{len(cat_done)}/{len(cat_all)} {label}")

            nxt = []
            for a in ACHIEVEMENTS:
                if a.id in earned or a.progress is None:
                    continue
                try:
                    cur, tot = a.progress(view)
                except Exception:
                    continue
                if tot:
                    nxt.append((cur / tot, a, cur, tot))
            nxt.sort(key=lambda x: -x[0])

            body = "\n".join(grid)
            if nxt:
                # One "next up" line, not a header plus a list — the grid above
                # is already six lines and a phone shows about seven.
                _r, a, c, t = nxt[0]
                body += (f"\n\n🎯 Next: {a.emoji} **{a.name}** "
                         f"`{_bar(c, t, 6)}` {c:,}/{t:,}")

            e = discord.Embed(
                title=f"🏅  {self.target.display_name}",
                description=body,
                color=0xf1c40f,
            )
            e.set_footer(text=f"{len(earned)}/{len(ACHIEVEMENTS)} unlocked  •  "
                              f"pick a category below")
            return e

        rows  = self._rows()
        start = self.page * self.PAGE
        emoji, label = CATEGORIES[self.category]
        done  = [a for a in rows if a.id in earned]

        lines = []
        for a in rows[start:start + self.PAGE]:
            if a.id in earned:
                # Unlocked badges collapse to one line — you already know them.
                lines.append(f"✅ {a.emoji} **{a.name}**")
            else:
                tail = ""
                if a.progress:
                    try:
                        cur, tot = a.progress(view)
                        tail = f" `{_bar(cur, tot, 6)}` {cur:,}/{tot:,}"
                    except Exception:
                        pass
                lines.append(f"🔒 {a.emoji} **{a.name}** `+🪙{a.reward:,}`\n"
                             f"　{a.desc}{tail}")

        e = discord.Embed(
            title=f"{emoji}  {label}",
            description=(f"**{len(done)}/{len(rows)}** unlocked\n\n"
                         + "\n".join(lines)),
            color=0xf1c40f,
        )
        e.set_footer(text=f"Page {self.page + 1}/{self._pages()}  •  "
                          f"{self.target.display_name}")
        return e

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class _AchPrev(discord.ui.Button):
    def __init__(self, disabled):
        super().__init__(emoji="◀", style=discord.ButtonStyle.secondary,
                         row=1, disabled=disabled)

    async def callback(self, interaction):
        v = self.view
        if interaction.user.id != v.owner.id:
            return await interaction.response.send_message("Not your list.",
                                                           ephemeral=True)
        v.page = max(0, v.page - 1); v.rebuild()
        await interaction.response.edit_message(embed=v.embed(), view=v)


class _AchNext(discord.ui.Button):
    def __init__(self, disabled):
        super().__init__(emoji="▶", style=discord.ButtonStyle.secondary,
                         row=1, disabled=disabled)

    async def callback(self, interaction):
        v = self.view
        if interaction.user.id != v.owner.id:
            return await interaction.response.send_message("Not your list.",
                                                           ephemeral=True)
        v.page = min(v._pages() - 1, v.page + 1); v.rebuild()
        await interaction.response.edit_message(embed=v.embed(), view=v)


class _AchLabel(discord.ui.Button):
    def __init__(self, page, total):
        super().__init__(label=f"{page}/{total}", row=1,
                         style=discord.ButtonStyle.secondary, disabled=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AchievementsCog(bot))
