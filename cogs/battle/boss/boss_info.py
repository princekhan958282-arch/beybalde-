"""
boss_info.py  —  📖 info cards for boss-only blades

These blades are deliberately absent from data/beyblades.json, so `;beypedia`
can't find them the normal way. That absence is what keeps them out of `;list`,
the spawn pool, the shop, boosters, the marketplace and the spawn-quiz decoys —
there is nothing to exclude because they were never in the pool.

But "not obtainable" isn't the same as "not viewable". Players should be able to
look up a boss they just fought, read its abilities and see why it wrecked them.
So this module exposes a small registry and one embed builder, and `;beypedia`
falls back to it when a name misses the main database.

Commands:
    ;bossinfo <name>   → the full card (aliases: ;binfo, ;bossbey)
    ;bey <name>        → falls through to here when the blade isn't obtainable
"""

import io
from typing import Optional

import discord

from . import boss_abilities as ab
from . import drakos as dk

# Every boss-only blade with a full profile. Ordinary bosses (Iron Sentinel and
# friends) have no ability kit, so they aren't listed here.
REGISTRY: dict[str, dict] = {
    ab.NEMESIS["key"]: ab.NEMESIS,
    dk.DRAKOS["key"]:  dk.DRAKOS,
}

SPECIAL_MODULES = {
    ab.NEMESIS["key"]: ab,
    dk.DRAKOS["key"]:  dk,
}

RARITY_COLOURS = {
    "Exclusive": 0xFF6F00,
    "Ultimate":  0xE91E63,
    "Mythic":    0x9C27B0,
}

TYPE_EMOJI = {
    "Attack":  "⚔️",
    "Defense": "🛡️",
    "Stamina": "🔋",
    "Balance": "⚖️",
}


def _norm(text: str) -> str:
    """Fold the characters nobody can type on a phone keyboard.

    Without this, "Nemesis Aetherion" missed entirely (the stored name uses Æ)
    and the bare word "aetherion" resolved to Drakos, because the plain
    substring test hit "Aetherion Drakos" before the Æ name could match.
    """
    return (text or "").lower().replace("æ", "ae").replace("Æ", "ae").strip()


def wants_copy(query: str) -> bool:
    """True when the player asked for the COPY, not the original.

    The blades are named "… org" on purpose: the suffix is what marks the real
    one. So a lookup that omits it — or says "copy" outright — is asking about
    the reproduction:

        ;bossinfo NEMESIS ÆTHERION       → the copy
        ;bossinfo NEMESIS ÆTHERION org   → the real blade

    A bare key ("nemesis", "drakos") is treated as the copy too, since that's
    the version most players will ever hold.
    """
    q = _norm(query)
    if not q:
        return False
    if "copy" in q:
        return True
    return "org" not in q.split()


def find_strict(query: str) -> Optional[dict]:
    """Confident matches only — exact key, exact name, or every query word
    present in the name. No loose substring fallback.

    Needed because the main database's fuzzy matcher is lossy: ";info drakos
    org" scored a hit on "DranSword" and returned that instead, so the boss
    fallback never ran. Checking a STRICT boss match first fixes the ordering
    without letting a vague boss guess shadow a real blade.
    """
    if not query:
        return None
    q = _norm(query)
    # Strip the org/copy marker — it selects which FACE to show, not which
    # blade. If that's all the query was, there's nothing to look up: bare
    # "org" used to survive as a substring and open NEMESIS's real card.
    stripped = " ".join(w for w in q.split() if w not in ("copy", "org")).strip()
    if not stripped:
        return None
    q = stripped
    for key, prof in REGISTRY.items():
        if q == key or q == _norm(prof["name"]):
            return prof
        if q == _norm(prof["name"]).replace(" org", "").strip():
            return prof
    q_words = q.split()
    for prof in REGISTRY.values():
        if q_words and all(w in _norm(prof["name"]) for w in q_words):
            return prof
    return None


def find(query: str) -> Optional[dict]:
    """Match on key, full name, or the distinctive words in a name.

    Matching ignores the org/copy distinction — it resolves WHICH blade you
    mean. Use wants_copy() to decide which face of it to show.
    """
    if not query:
        return None
    q = _norm(query)
    if not q:
        return None
    # "… copy" and "… org" both point at the same profile. A query that is
    # ONLY those markers names no blade at all.
    stripped = " ".join(w for w in q.split() if w not in ("copy", "org")).strip()
    if not stripped:
        return None
    q = stripped

    # 1. exact key or exact full name
    for key, prof in REGISTRY.items():
        if q == key or q == _norm(prof["name"]):
            return prof

    # 2. every query word present in the name — handles "nemesis aetherion"
    q_words = q.split()
    for prof in REGISTRY.values():
        name = _norm(prof["name"])
        if q_words and all(w in name for w in q_words):
            return prof
    for key, prof in REGISTRY.items():
        if q == _norm(prof["name"]).replace(" org", "").strip() or q == key:
            return prof

    # 3. a distinctive single word. "aetherion" appears in BOTH names, so it
    #    stays ambiguous and is only used as a last resort.
    for prof in REGISTRY.values():
        words = _norm(prof["name"]).split()
        if any(q == w for w in words):
            return prof

    # 4. loose substring, longest name match wins so it's deterministic
    hits = [p for p in REGISTRY.values() if len(q) >= 4 and q in _norm(p["name"])]
    if len(hits) == 1:
        return hits[0]
    return hits[0] if hits else None


def names() -> list[str]:
    return [p["name"] for p in REGISTRY.values()]


def to_blade(prof: dict) -> dict:
    """Adapt a boss profile into the schema utils/info_card.py renders.

    The PNG card template reads a fixed set of keys — name, stats,
    spin_direction, type, rarity, image_url, abilities, special_move — so
    rather than write a second renderer for bosses, the profile is reshaped
    into that exact shape and goes through the same pipeline every other blade
    uses. One card style across the whole bot, and any future change to the
    template picks these up for free.
    """
    mod = SPECIAL_MODULES.get(prof["key"])

    # The card shows one signature move, so lead with the Ultimate — that's the
    # move players will remember losing to.
    special = None
    if mod:
        ults = [s for s in mod.SPECIALS.values() if s["ultimate"]]
        pick = (ults or list(mod.SPECIALS.values()))[0]
        special = {
            "name":           pick["name"],
            "description":    pick["text"],
            "hits":           pick["hits"],
            "damage_per_hit": int(prof["attack"] * pick["mult"] / max(1, pick["hits"])),
            "total_damage":   int(prof["attack"] * pick["mult"]),
            "true_damage":    bool(pick.get("true_damage")),
        }

    return {
        "name":           prof["name"],
        "type":           prof.get("type", "Balance"),
        "rarity":         prof.get("rarity", "Exclusive"),
        "spin_direction": prof.get("spin", "Right"),
        "image_url":      prof.get("image_url") or "",
        "description":    prof.get("description", prof.get("blurb", "")),
        "stats": {
            "attack":  prof["attack"],
            "defense": prof["defense"],
            "stamina": prof["stamina"],
            # The template expects these two; bosses don't carry them natively.
            "special": max(prof["attack"], prof["defense"], prof["stamina"]),
            "hp":      min(200, round(prof["hp"] / 12)),
        },
        "abilities": [
            {
                "name":        a["name"],
                "trigger":     "passive",
                "description": _strip_markdown(a["desc"]),
                "chain":       [],
            }
            for a in prof.get("abilities", [])
        ],
        "special_move": special,
        "booster_exclusive": False,
        # The renderer reads this off the blade dict, so it has to travel with
        # the adapter output — not just sit on the profile.
        "card_theme":   prof.get("card_theme"),
    }


def _strip_markdown(text: str) -> str:
    """The PNG template renders plain text — ** and ` show up literally."""
    return (text or "").replace("**", "").replace("`", "")


async def render_card(prof: dict) -> tuple[Optional[io.BytesIO], str]:
    """Rendered card via the shared renderer. Returns (buffer, extension)."""
    try:
        from utils import info_card
        buf = await info_card.render_info_card(to_blade(prof))
        if buf is None:
            return None, "png"
        data, ext = info_card.compress(buf.getvalue())
        return io.BytesIO(data), ext
    except Exception:
        return None, "png"


def build_embed(prof: dict, compact: bool = False) -> discord.Embed:
    """The info card. Sized for a phone: short header, two inline stat blocks,
    then abilities and moves as their own fields."""
    rarity = prof.get("rarity", "Exclusive")
    tname  = prof.get("type", "Balance")

    header = []
    if prof.get("event_limited"):
        header.append("🎟️ **EVENT LIMITED**")
    header.append(f"✨ **{rarity}**")
    header.append(f"{TYPE_EMOJI.get(tname, '')} {tname}")

    e = discord.Embed(
        title=f"{prof['emoji']}  {prof['name']}",
        description=(
            " · ".join(header)
            + f"\n*{prof.get('title', '')}*\n\n"
            + prof.get("description", prof.get("blurb", ""))
        ),
        color=RARITY_COLOURS.get(rarity, prof.get("colour", 0x9b59b6)),
    )
    if prof.get("image_url"):
        e.set_thumbnail(url=prof["image_url"])

    # Two inline blocks read side by side on mobile; three get squashed.
    e.add_field(
        name="Stats",
        value=(f"⚔️ ATK **{prof['attack']}**\n"
               f"🛡️ DEF **{prof['defense']}**\n"
               f"🔋 STA **{prof['stamina']}**"),
        inline=True,
    )
    e.add_field(
        name="Boss data",
        value=(f"❤️ HP **{prof['hp']:,}**\n"
               f"🎯 {prof.get('difficulty', '—')}\n"
               f"🌀 {prof.get('spin', '—')} spin"),
        inline=True,
    )

    for a in prof.get("abilities", []):
        e.add_field(name=f"{a['emoji']} {a['name']}", value=a["desc"], inline=False)

    if not compact:
        mod = SPECIAL_MODULES.get(prof["key"])
        if mod:
            specials = [s for s in mod.SPECIALS.values() if not s["ultimate"]]
            ults     = [s for s in mod.SPECIALS.values() if s["ultimate"]]
            if specials:
                e.add_field(
                    name=f"⚡ Special Moves ({len(specials)})",
                    value="\n".join(
                        f"{s['emoji']} **{s['name']}** — {s['hits']} hit(s)"
                        for s in specials),
                    inline=False,
                )
            if ults:
                e.add_field(
                    name=f"💀 Ultimate{'s' if len(ults) > 1 else ''} ({len(ults)})",
                    value="\n".join(
                        f"{s['emoji']} **{s['name']}** — {s['hits']} hit(s)"
                        for s in ults),
                    inline=False,
                )

    e.set_footer(text="Boss-only · can't be caught, bought, traded or spawned  "
                      f"•  ;boss {prof['key']} to fight it")
    return e


def copy_blade(prof: dict) -> dict:
    """A generic 'what a copy looks like' card — not a specific rolled instance.

    Stats shown are the AVERAGE roll, so the card reads as a fair preview of
    what beating the boss gets you rather than promising the best case.
    """
    from . import boss_copy as bc

    base = prof["attack"] + prof["defense"] + prof["stamina"]
    mean_pen = 122                      # measured across 300k rolls
    scale = max(0.35, (base - mean_pen) / base)

    lo = [f"{w}" for w, *_ in bc.LOADOUTS]
    total_w = sum(w for w, *_ in bc.LOADOUTS)
    kit_lines = [
        f"{label} — {w / total_w * 100:.0f}%"
        for w, _ns, _nu, _na, label in bc.LOADOUTS
    ]
    grade_lines = []
    gw = sum(b[0] for b in bc.GRADE_BANDS)
    for w, _lo, _hi, g in bc.GRADE_BANDS:
        emoji = bc.grade_meta(g)[0]
        grade_lines.append(f"{emoji} {g} — {w / gw * 100:.1f}%")
    grade_lines.append(f"{bc.grade_meta('Perfect')[0]} Perfect — 1 in "
                       f"{bc.PERFECT_ODDS:,}")

    return {
        "name":           prof["name"].replace(" org", "").strip(),
        "type":           prof.get("type", "Balance"),
        "rarity":         "Mythic",
        "spin_direction": prof.get("spin", "Right"),
        "image_url":      prof.get("image_url") or "",
        "description": (
            f"A reproduction of {prof['name']}. Dropped every time you beat it, "
            f"and no two are the same — each copy rolls its own moves, its own "
            f"stats and its own grade. You don't own one yet, so the stats "
            f"below are an AVERAGE roll, not a promise."
        ),
        "stats": {
            "attack":  int(prof["attack"] * scale),
            "defense": int(prof["defense"] * scale),
            "stamina": int(prof["stamina"] * scale),
            "special": int(max(prof["attack"], prof["defense"],
                               prof["stamina"]) * scale),
            "hp":      min(200, round(prof["hp"] / 12 * scale)),
        },
        "abilities": [
            {"name": "Rolled Kit", "trigger": "passive", "chain": [],
             "description": "Every copy gets a random slice of the original's "
                            "moveset:  " + "  ·  ".join(kit_lines)},
            {"name": "Rolled Grade", "trigger": "passive", "chain": [],
             "description": "Stat quality is rolled too, averaging about "
                            f"{mean_pen} points below the original:  "
                            + "  ·  ".join(grade_lines)},
            {"name": "Awakening", "trigger": "passive", "chain": [],
             "description": "Some copies keep an echo of the original. When it "
                            "wakes, the copy fights at the source blade's full "
                            "power. Roughly 1 copy in 8 has it, and the better "
                            "the grade the more often it triggers."},
        ],
        "special_move": None,
        "booster_exclusive": False,
        "card_theme": {"accent": "#9aa4b5", "glow": "#3f4756", "tint": "#0f1218"},
    }


class BossMoveView(discord.ui.View):
    """Move detail behind a button, so the card itself stays one screen."""

    def __init__(self, prof: dict, owner_id: int):
        super().__init__(timeout=180)
        self.prof = prof
        self.owner_id = owner_id
        self.message = None

    @discord.ui.button(label="Moves", emoji="⚡", style=discord.ButtonStyle.primary)
    async def moves(self, interaction: discord.Interaction, _: discord.ui.Button):
        mod = SPECIAL_MODULES.get(self.prof["key"])
        if mod is None:
            return await interaction.response.send_message("No move data.",
                                                           ephemeral=True)
        e = discord.Embed(
            title=f"{self.prof['emoji']}  {self.prof['name']} — Moves",
            color=RARITY_COLOURS.get(self.prof.get("rarity"), 0x9b59b6),
        )
        for spec in mod.SPECIALS.values():
            tag = "💀 ULTIMATE" if spec["ultimate"] else "⚡ Special"
            bits = [f"{spec['hits']} hit(s)", f"{spec['mult']:.2f}x"]
            if spec.get("pierce"):
                bits.append(f"pierces {int(spec['pierce'] * 100)}%")
            if spec.get("true_damage"):
                bits.append("true damage")
            if spec.get("drain"):
                bits.append(f"drains {int(spec['drain'] * 100)}%")
            if spec.get("freeze"):
                bits.append(f"freezes {spec['freeze']} turns")
            e.add_field(
                name=f"{spec['emoji']} {spec['name']}  ·  {tag}",
                value=f"*{spec['text']}*\n`{' · '.join(bits)}`",
                inline=False,
            )
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


async def send_info(ctx, prof: dict, as_copy: bool = False):
    """PNG card first, exactly like ;beypedia. Falls back to the embed if
    Chromium is missing, the art URL is dead or the render times out."""
    if as_copy:
        return await send_copy_info(ctx, prof)

    view = BossMoveView(prof, ctx.author.id)
    async with ctx.typing():
        buf, ext = await render_card(prof)

    if buf is not None:
        fname = f"{prof['key']}_card.{ext}"
        view.message = await ctx.send(file=discord.File(buf, filename=fname),
                                      view=view)
        return

    view.message = await ctx.send(embed=build_embed(prof), view=view)


def owned_copies(user_id: int, source_key: str) -> list[dict]:
    """This player's rolled copies of one boss, best grade first."""
    try:
        from . import boss_copy as bc
        return [c for c in bc.all_copies(user_id) if c.get("source") == source_key]
    except Exception:
        return []


async def send_copy_info(ctx, prof: dict):
    """Your OWN copy if you have one, otherwise the generic preview.

    Asking about a blade you're holding should show what YOU rolled, not an
    average of what a copy tends to be. If you own several, the best is shown
    and the note points at ;copies for the rest.
    """
    import io as _io

    import discord as _d

    mine = owned_copies(ctx.author.id, prof["key"])
    if mine:
        from . import boss_copy as bc
        best = mine[0]                      # all_copies() is already best-first
        async with ctx.typing():
            buf, ext = None, "png"
            try:
                from utils import info_card
                b = await info_card.render_info_card(bc.to_blade(best, prof))
                if b is not None:
                    data, ext = info_card.compress(b.getvalue())
                    buf = _io.BytesIO(data)
            except Exception:
                buf = None

        extra = (f"  ·  you own {len(mine)} — `;copies` for the rest"
                 if len(mine) > 1 else "")
        note = f"🧬 **your {best['grade']}** copy · `#{best['id']}`{extra}"
        if buf is not None:
            return await ctx.send(
                note, file=_d.File(buf, filename=f"copy_{best['id']}.{ext}"))
        from cogs.battle.boss.boss_battle import bcopy_embed
        return await ctx.send(note, embed=bcopy_embed(best, prof))

    blade = copy_blade(prof)
    async with ctx.typing():
        buf, ext = None, "png"
        try:
            from utils import info_card
            b = await info_card.render_info_card(blade)
            if b is not None:
                data, ext = info_card.compress(b.getvalue())
                buf = _io.BytesIO(data)
        except Exception:
            buf = None
    if buf is not None:
        return await ctx.send(
            file=_d.File(buf, filename=f"{prof['key']}_copy_card.{ext}"))

    e = _d.Embed(
        title=f"🧬  {blade['name']}",
        description=blade["description"],
        color=0x9aa4b5,
    )
    for a in blade["abilities"]:
        e.add_field(name=a["name"], value=a["description"][:1024], inline=False)
    e.set_footer(text=f"Beat {prof['name']} to roll one  •  ;copies to see yours")
    await ctx.send(embed=e)
