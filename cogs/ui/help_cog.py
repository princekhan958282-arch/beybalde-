# help_cog.py — Drop into your cogs/ folder
import discord
from discord.ext import commands

# ── Category registry ──────────────────────────────────────────────────────────
CATEGORIES = {
    "start":       {"emoji": "🌟",  "color": 0x2ECC71, "label": "Getting Started"},
    "clans":       {"emoji": "🛡️",  "color": 0x9B59B6, "label": "Clans"},
    "mastery":     {"emoji": "🔰",  "color": 0x1ABC9C, "label": "Mastery & Badges"},
    "story":       {"emoji": "📖",  "color": 0x3498DB, "label": "Story Mode"},
    "boss":        {"emoji": "👹",  "color": 0x8E44AD, "label": "Boss Battles"},
    "battle":      {"emoji": "⚔️",  "color": 0x7B68EE, "label": "Battle"},
    "marketplace": {"emoji": "🛒",  "color": 0x9B59B6, "label": "Marketplace"},
    "shop":        {"emoji": "🎁",  "color": 0x2ECC71, "label": "Shop"},
    "leaderboard": {"emoji": "🏆",  "color": 0xF39C12, "label": "Leaderboard"},
    "boosters":    {"emoji": "📦",  "color": 0x1ABC9C, "label": "Boosters"},
    "spawncog":    {"emoji": "⚙️",  "color": 0xE74C3C, "label": "SpawnCog"},
    "casino":      {"emoji": "🎰",  "color": 0xE91E8C, "label": "Casino"},
    "avatar":      {"emoji": "🖼️",  "color": 0x5B6AE8, "label": "Avatars"},
    "moves":       {"emoji": "⚡",  "color": 0xFFD700, "label": "Battle Moves"},
    "matchups":    {"emoji": "🎯",  "color": 0xF39C12, "label": "Type Matchups"},
    "tips":        {"emoji": "💡",  "color": 0x2ECC71, "label": "Pro Tips"},
}

# ── Battle Moves ───────────────────────────────────────────────────────────────
BATTLE_MOVES = {
    "attack": {
        "emoji": "⚔️",
        "cost": "-1.5 Stamina",
        "description": "Deals damage to opponent. Main offensive move.",
        "effects": [
            "Deals damage to opponent.",
            "Main offensive move.",
            "Builds Special Gauge when used.",
            "Gets type advantages.",
        ],
        "counters": "✅ Strong against Stamina\n❌ Weak against Defense (damage reduced)\n⚖️ Normal vs Attack",
        "extra": "Attack vs Stamina gets 1.2× damage bonus.",
    },
    "defense": {
        "emoji": "🛡️",
        "cost": "-1.5 Stamina",
        "description": "Reduces incoming Attack damage.",
        "effects": [
            "Reduces incoming Attack damage.",
            "Does NOT completely block attacks.",
            "Helps survive enemy pressure.",
            "Can trigger defensive abilities/passives.",
        ],
        "important": "Defense is NOT a counter attack.\nIt only mitigates damage.",
    },
    "stamina": {
        "emoji": "⚡",
        "cost": "0 Stamina",
        "description": "Recover & keep spinning.",
        "effects": [
            "Recovers +3 Stamina.",
            "Heals HP based on Stamina stat.",
            "Builds Special Gauge.",
        ],
        "battle_purpose": "Keeps your Bey spinning longer.\nHelps prevent Stamina KO.",
        "counter": "❌ Weak against Attack.",
    },
    "charge": {
        "emoji": "🔋",
        "cost": "-1 Stamina",
        "description": "Build your Special Gauge fast.",
        "effects": [
            "Instantly gives +50 Special Gauge.",
            "Helps unlock Special faster.",
        ],
        "special_requirement": "Need 150 Gauge to activate Special.",
        "example": "0 Gauge → Charge → 50 Gauge\n50 Gauge → Charge → 100 Gauge\n100 Gauge → Charge → 150 Gauge ✨",
    },
    "special": {
        "emoji": "🌟",
        "cost": "-3 Stamina",
        "description": "Uses Beyblade's unique special move.",
        "requirement": "Special Gauge must be 150/150",
        "effects": [
            "Uses Beyblade's unique special move.",
            "Can have custom effects:",
            "  • Huge damage",
            "  • Defense ignore",
            "  • Healing",
            "  • Shields",
            "  • Buffs/debuffs",
            "  • Multi-hit attacks",
        ],
        "note": "Special consumes the full gauge.",
    },
}

# ── Type Matchup Matrix ────────────────────────────────────────────────────────
TYPE_MATCHUPS = {
    "Attack": {
        "Attack": "⚖️ Normal",
        "Defense": "❌ Weak (reduced damage)",
        "Stamina": "✅ Strong (1.2× bonus)",
    },
    "Defense": {
        "Attack": "✅ Strong (reduce damage)",
        "Defense": "⚖️ Normal",
        "Stamina": "❌ Weak (less effective)",
    },
    "Stamina": {
        "Attack": "❌ Weak (takes more damage)",
        "Defense": "⚖️ Normal",
        "Stamina": "✅ Strong (heal more)",
    },
}

# ── Pro Tips & Tricks ──────────────────────────────────────────────────────────
PRO_TIPS = {
    "early_game": [
        "🔋 **Charge Rush:** Start with 2–3 Charges to get your Special fast",
        "💰 **Economy:** Spam Stamina (free!) to build Gauge and heal",
        "🎯 **Scout:** Watch opponent's first move to predict their type",
    ],
    "mid_game": [
        "⚖️ **Type Counter:** If opponent is Stamina, spam Attack for bonus",
        "🛡️ **Survive:** Use Defense before you drop below 30% health",
        "📊 **Gauge Management:** You need 150 for Special — plan ahead",
    ],
    "late_game": [
        "💣 **Special Timing:** Use your Special when opponent is low",
        "🔄 **Rotation:** Mix moves to avoid predictability",
        "🎪 **Passive Synergy:** Some Beyblades get bonuses on certain moves",
    ],
    "special_mechanics": [
        "🌟 **Special Move Types:** Check your Bey's Special in `;beypedia <name>`",
        "⛓️ **Chains:** Some Specials trigger follow-ups — read tooltips",
        "💚 **Healing Specs:** Some Specials heal instead of damage",
        "🛡️ **Defensive Specs:** Some provide shields or evasion buffs",
    ],
}

# ── Static command data (mirrors the React preview) ────────────────────────────
COMMAND_DATA = {
    "story": [
        (";story",             "📖 Pick a Story Mode stage and fight it"),
        (";story <stage>",     "Jump straight in, e.g. `;story 1-2` or `;story kenta`"),
        (";storymap",          "Every chapter, every stage, and how far you've got"),
        (";storyinfo <stage>", "Opponent stats, rewards and whether it's unlocked"),
        (";storystats",        "Your Story Mode record and next unlock"),
        ("/story play",        "The same thing as a slash command, with autocomplete"),
    ],
    "battle": [
        (";battle @user",   "Challenge another blader to a 1v1 match"),
        (";battlestats",    "View your full battle history & win/loss record"),
        (";forfeit",        "Surrender your current ongoing battle"),
    ],
    "marketplace": [
        (";buybey <id>",    "Purchase a Beyblade listed by another player"),
        (";sellbey <bey>",  "List your Beyblade for sale on the marketplace"),
        (";marketplace",    "Browse all active player listings"),
    ],
    "shop": [
        (";buy <part>",     "Purchase a Beyblade part from the shop"),
        (";coins",          "Check your current coin balance"),
        (";daily",          "Claim your daily coin reward"),
        (";inventory",      "View your full Beyblade parts inventory"),
    ],
    "leaderboard": [
        ("/leaderboard rank",     "Top bladers by rank score"),
        ("/leaderboard winrate",  "Best ranked win rate (min 10 games)"),
        ("/leaderboard wins",     "Most ranked wins"),
        ("/leaderboard streak",   "Longest ranked win streak"),
        ("/leaderboard catches",  "Most Beyblades caught"),
        (";rank  or  /rank",      "Your rank card and board placings"),
        (";verify  or  /verify",  "Verify your account for ranked play"),
        (";battle @user ranked",  "Play a RANKED match — casual doesn't count"),
        (";card",                 "Display your blader profile card"),
    ],
    "boosters": [
        (";booster",        "Purchase and open a booster pack"),
    ],
    "spawncog": [
        (";claim",              "Claim the currently active wild Beyblade spawn"),
        (";setspawnchannel #ch","Lock wild spawns to a specific channel"),
        (";spawninfo",          "Show current spawn configuration for this server"),
    ],
    "casino": [
        # Lobby
        (";casino",                              "🎰 Open the casino lobby — browse & launch every game"),
        (";premium info",                        "Premium passes — bigger bets, bigger dailies, 0% tax"),
        # Wallet
        (";bal",                                 "💳 Both wallets in one card (Beycoins + casino)"),
        (";casinobal",                           "Casino coins only"),
        (";casinodaily",                         "Claim your daily casino coins"),
        (";casinoexchange buy <beycoins>",       "Buy casino coins — 100 Beycoins = 60 casino coins"),
        (";casinoexchange sell <amount>",        "Sell casino coins back (10% tax, 0% with Legend pass)"),
        (";casinolb",                            "Top 10 casino coin holders"),
        (";casinogive @user <amount>",           "(Admin) Give casino coins to a player"),
        (";casinotake @user <amount>",           "(Admin) Remove casino coins from a player"),
        # New Games
        (";wheel <bet>  |  Solo",                "🆕 Wheel of Fortune — one spin, up to 50x"),
        (";plinko <bet> [risk]  |  Solo",        "🆕 Drop a ball through 12 rows — edges pay up to 220x"),
        (";keno <bet>  |  Solo",                 "🆕 Pick up to 10 of 40 numbers, house draws 10"),
        (";videopoker <bet>  |  Solo",           "🆕 Jacks or Better — hold, draw, royal flush pays 400x"),
        (";beyrace <bet>  |  Solo",              "🆕 Bet on one of 5 Beyblades to win the race"),
        (";minesmp <entry> [mines]  |  MP 2-8",  "🆕 Shared-grid Mines — last blade standing takes the pot"),
        # Solo Games
        (";blackjack <bet>  |  Solo",            "vs dealer AI — hit / stand / double down / split"),
        (";slots <bet>  |  Solo",                "5-reel slot machine — Beyblade jackpot at 500x"),
        (";roulette  |  Solo",                   "Bet on numbers, colors, or groups then spin"),
        (";dice <bet>  |  Solo",                 "Pick risk tier (70/50/30/10/5%) and roll vs house"),
        (";mines <bet>  |  Solo",                "Reveal tiles, avoid bombs — cash out anytime"),

        (";tower <bet>  |  Solo",                "Climb 10 floors vs RNG enemies — cash out between floors"),
        # Multiplayer Games
        (";poker <buy_in>  |  Multiplayer 2-6",  "Texas Hold'em — host starts table, 2-6 players join"),
        (";crash <bet>  |  Multiplayer",         "Shared channel round — everyone rides the same multiplier"),
        (";roulette_multi  |  Multiplayer",      "Shared wheel — everyone bets, host spins"),
        (";diceduel <bet>  |  Multiplayer 1v1",  "Challenge someone — highest d6 roll wins the pot"),
        (";coinflip <bet>  |  Solo or 1v1",      "Solo vs house OR tag a player to challenge them"),
        (";higherlower <bet>  |  Multiplayer 1v1","Card duel vs another player — first to 3 wins takes pot"),
        (";auction <item> <bid> [mins]  |  Multiplayer", "Host an auction — anyone can bid with casino coins"),
    ],
    "start": [
        (";start",                      "🌟 Get your free starter Beyblade — do this first"),
        (";whatnext",                   "The short version of what to do in this bot"),
        (";profile",                    "Your player card — level, record, rank"),
        (";inventory",                  "Everything you own"),
        (";list",                       "📖 Browse every blade — rarity dropdown, ✅ marks yours"),
        (";list <rarity>",              "Jump to a tier, e.g. `;list mythic`"),
        (";bal",                        "💳 Both wallets, both dailies, your tax — one card"),
        (";daily",                      "Claim your daily Beycoins"),
        (";redeem <code>",              "🎟️ Claim an event or giveaway code"),
        (";backup",                     "🔐 Get your account recovery code (by DM)"),
        (";restore <code>",             "🔐 Move a backed-up account onto this one"),
        (";battle @user",               "Challenge someone to a battle"),
        (";casino",                     "Open the casino lobby"),
        (";help <category>",            "Commands for any category"),
    ],
    "clans": [
        (";clan",                       "Your clan — or how to get one"),
        (";clan create <tag> <name>",   "Found a clan (costs 🪙 25,000)"),
        (";clan join <name|tag>",       "Join an open clan"),
        (";clan leave",                 "Leave your clan"),
        (";clan info <name|tag>",       "Look at any clan"),
        (";clan list",                  "Clan leaderboard — tap a clan to inspect it"),
        (";clan invite @user",          "Invite someone (invite-only clans)"),
        (";clan kick @user",            "Owner only — remove a member"),
        (";clan deposit <amount>",      "Put coins into the clan treasury"),
        (";clan withdraw <amount>",     "Owner only — take coins out"),
        (";clan desc <text>",           "Owner only — set the description"),
        (";clan open  /  ;clan closed", "Owner only — toggle who can join"),
        (";clanwar declare <clan> <stake>", "⚔️ Declare war — winner takes both stakes"),
        (";clanwar accept  /  decline",  "Owner only — answer a declaration"),
        (";clanwar status",             "Live war scoreboard"),
        (";clanwar history",            "Past wars"),
    ],
    "boss": [
        (";boss",                       "👹 Pick a boss and fight it"),
        (";boss <name>",                "Fight one directly, e.g. `;boss drakos`"),
        (";bosses",                     "The roster, HP, rewards and your clears"),
        (";bossinfo <name>",            "📖 Info card for a boss-only blade"),
        (";copies",                     "🧬 Boss copies you've won — every one rolls its own kit"),
        (";copy <number|id>",           "Card for one of your copies"),
    ],
    "mastery": [
        (";mastery",                    "🔰 Your blades — paged, tap one for detail"),
        (";mastery <blade>",            "Jump straight to one blade"),
        (";achievements",               "🏅 Your badges — pick a category from the dropdown"),
        (";achievements <category>",    "battle · collection · progress · wealth · mastery · social"),
        (";achievements @user",         "Someone else's badges"),
    ],
    "avatar": [
        # Browse
        (";av",                         "Avatar system overview"),
        (";avatarshop  or  ;ashop",     "Browse all available avatars"),
        (";avatarinfo <id>  or  ;ainfo <id>", "Inspect a specific avatar"),
        # Buying
        (";buyavatar <id>  or  ;buya <id>",   "Buy an avatar directly"),
        (";buypack <type>  or  ;bpack <type>", "Open a pack — types: common rare epic legendary"),
        # Inventory
        (";myavatars  or  ;avatars  or  ;avinv", "View your avatar collection"),
        (";equipavatar <name/id>  or  ;equipa <name/id>", "Equip an avatar"),
        (";unequipavatar  or  ;unequipa",        "Unequip current avatar"),
        # Packs
        (";avatarpacks  or  ;apacks",   "View packs and their prices"),
        # Levelling
        (";avatarupgrade [name] [n]  or  ;aup", "Buy levels for an avatar card"),
        (";avatarcost  or  ;acost",     "The full upgrade curve and what it buys"),
        (";avatarreset <name>  or  ;areset", "Reset to Lv1, refund 70% of the spend"),
    ],
}

# ── Build main overview embed ──────────────────────────────────────────────────
def build_main_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🌀 Beybot Help",
        description=(
            "Select a category below to see its commands.\n"
            "You can also type `;help <category>` directly.\n\n"
            "**New here? Run `;start`.**\n\n"
            "**Categories:** start · story · clans · boss · mastery · battle · marketplace · shop · "
            "leaderboard · boosters · spawncog · casino · avatar\n"
            "**Advanced:** ;help moves · ;help matchups · ;help tips"
        ),
        color=0x7B68EE,
    )
    for key, meta in CATEGORIES.items():
        embed.add_field(
            name=f"{meta['emoji']} {meta['label']}",
            value=f"`{key}`",
            inline=True,
        )
    embed.set_footer(text="⚔️ Beybot Help System · Let it rip!")
    return embed

# ── Build per-category embed ───────────────────────────────────────────────────
def build_category_embed(key: str, meta: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"{meta['emoji']} {meta['label']} Commands",
        color=meta["color"],
    )
    for cmd_name, cmd_desc in COMMAND_DATA.get(key, []):
        embed.add_field(name=f"`{cmd_name}`", value=cmd_desc, inline=False)
    embed.set_footer(text="Use ;help <category> for any category · ⚔️ Beybot")
    return embed

# ── Build battle moves embed ───────────────────────────────────────────────────
def build_moves_embed(move_name: str = None) -> discord.Embed:
    if move_name and move_name.lower() in BATTLE_MOVES:
        # Single move detail
        move_key = move_name.lower()
        move = BATTLE_MOVES[move_key]
        embed = discord.Embed(
            title=f"{move['emoji']} {move_key.upper()} Button",
            description=move["description"],
            color=0x7B68EE,
        )
        embed.add_field(name="💰 Cost", value=move["cost"], inline=False)
        
        embed.add_field(
            name="Effect:",
            value="\n".join(move["effects"]),
            inline=False,
        )
        
        # Add optional fields based on move type
        if "counters" in move:
            embed.add_field(name="Counters:", value=move["counters"], inline=False)
        
        if "important" in move:
            embed.add_field(name="Important:", value=move["important"], inline=False)
        
        if "battle_purpose" in move:
            embed.add_field(name="Battle purpose:", value=move["battle_purpose"], inline=False)
        
        if "counter" in move:
            embed.add_field(name="Counter:", value=move["counter"], inline=False)
        
        if "special_requirement" in move:
            embed.add_field(name="Special requirement:", value=move["special_requirement"], inline=False)
        
        if "example" in move:
            embed.add_field(name="Example:", value=move["example"], inline=False)
        
        if "requirement" in move:
            embed.add_field(name="Requirement:", value=move["requirement"], inline=False)
        
        if "note" in move:
            embed.add_field(name="Note:", value=move["note"], inline=False)
        
        if "extra" in move:
            embed.add_field(name="Extra:", value=move["extra"], inline=False)
        
        return embed
    else:
        # Overview of all moves
        embed = discord.Embed(
            title="⚔️ Battle Moves Breakdown",
            description="Type `;help moves <move>` to learn more about each move.",
            color=0x7B68EE,
        )
        for move_key, move_data in BATTLE_MOVES.items():
            embed.add_field(
                name=f"{move_data['emoji']} {move_key.upper()}",
                value=f"{move_data['description']}\n*Cost: {move_data['cost']}*",
                inline=False,
            )
        embed.set_footer(text="Available moves: attack, defense, stamina, charge, special")
        return embed

# ── Build type matchups embed ──────────────────────────────────────────────────
def build_matchups_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎯 Type Matchup Chart",
        description=(
            "Understand how your Beyblade's type interacts with opponents.\n"
            "**Your Type → Opponent Type**"
        ),
        color=0xF39C12,
    )
    
    for your_type, matchups in TYPE_MATCHUPS.items():
        matchup_text = "\n".join(
            f"{symbol} vs {opp_type}: {result}"
            for opp_type, result in matchups.items()
            for symbol in [""] if opp_type == "Attack" or opp_type == "Defense" or opp_type == "Stamina"
        )
        
        lines = []
        for opp_type in ["Attack", "Defense", "Stamina"]:
            result = matchups[opp_type]
            lines.append(f"• **vs {opp_type}:** {result}")
        
        embed.add_field(
            name=f"**{your_type} Type**",
            value="\n".join(lines),
            inline=False,
        )
    
    embed.add_field(
        name="📊 Quick Reference",
        value=(
            "✅ **Strong** = You get bonus damage or survivability\n"
            "❌ **Weak** = You take increased damage or struggle\n"
            "⚖️ **Normal** = No bonus or penalty"
        ),
        inline=False,
    )
    embed.set_footer(text="Always check your opponent's type before battling!")
    return embed

# ── Build pro tips embed ───────────────────────────────────────────────────────
def build_tips_embed(category: str = None) -> discord.Embed:
    if category and category.lower() in PRO_TIPS:
        # Specific tip category
        cat_key = category.lower()
        tips = PRO_TIPS[cat_key]
        embed = discord.Embed(
            title=f"💡 {cat_key.replace('_', ' ').title()} Tips",
            color=0x2ECC71,
        )
        for tip in tips:
            embed.add_field(name="🎯", value=tip, inline=False)
        return embed
    else:
        # Overview of all tips
        embed = discord.Embed(
            title="💡 Pro Tips & Tricks",
            description="Type `;help tips <category>` to dive deeper.",
            color=0x2ECC71,
        )
        for cat_key, tips in PRO_TIPS.items():
            cat_name = cat_key.replace('_', ' ').title()
            preview = tips[0][:50] + "..." if len(tips[0]) > 50 else tips[0]
            embed.add_field(
                name=f"📌 {cat_name}",
                value=preview,
                inline=False,
            )
        embed.set_footer(text="Categories: early_game, mid_game, late_game, special_mechanics")
        return embed

# ── Dropdown select menu ───────────────────────────────────────────────────────
class CategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=v["label"], value=k, emoji=v["emoji"])
            for k, v in CATEGORIES.items()
        ]
        super().__init__(placeholder="📂 Choose a category…", options=options)

    async def callback(self, interaction: discord.Interaction):
        key  = self.values[0]
        meta = CATEGORIES[key]
        
        # Route to appropriate builder based on category
        if key == "moves":
            embed = build_moves_embed()
            view = MovesView()  # Show battle move buttons
        elif key == "matchups":
            embed = build_matchups_embed()
            view = None
        elif key == "tips":
            embed = build_tips_embed()
            view = None
        else:
            embed = build_category_embed(key, meta)
            view = self.view
        
        await interaction.response.edit_message(embed=embed, view=view)

# ── Battle Move Buttons ────────────────────────────────────────────────────────
class MoveButton(discord.ui.Button):
    def __init__(self, move_key: str, move_data: dict):
        self.move_key = move_key
        super().__init__(
            label=move_key.upper(),
            emoji=move_data["emoji"],
            style=discord.ButtonStyle.primary,
        )

    async def callback(self, interaction: discord.Interaction):
        embed = build_moves_embed(self.move_key)
        await interaction.response.edit_message(embed=embed)

class MovesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        for move_key, move_data in BATTLE_MOVES.items():
            self.add_item(MoveButton(move_key, move_data))

# ── View wrapper ───────────────────────────────────────────────────────────────
class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(CategorySelect())

# ── Cog ───────────────────────────────────────────────────────────────────────
class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="help", aliases=["h"])
    async def help_cmd(self, ctx: commands.Context, *args):
        """Show the Beybot help menu.
        
        Usage:
            ;help                    → Main menu
            ;help battle             → Battle commands
            ;help moves              → Battle move breakdown
            ;help moves attack       → Details on Attack move
            ;help matchups           → Type matchup chart
            ;help tips               → All pro tips
            ;help tips early_game    → Early game tips
        """
        if not args:
            # Main menu
            embed = build_main_embed()
            view  = HelpView()
            await ctx.send(embed=embed, view=view)
        
        elif args[0].lower() == "moves":
            # Battle moves
            if len(args) > 1:
                # Specific move
                embed = build_moves_embed(args[1])
            else:
                # All moves overview
                embed = build_moves_embed()
            await ctx.send(embed=embed)
        
        elif args[0].lower() == "matchups":
            # Type matchups
            embed = build_matchups_embed()
            await ctx.send(embed=embed)
        
        elif args[0].lower() == "tips":
            # Pro tips
            if len(args) > 1:
                # Specific category
                embed = build_tips_embed(args[1])
            else:
                # All tips overview
                embed = build_tips_embed()
            await ctx.send(embed=embed)
        
        elif args[0].lower() in CATEGORIES:
            # Category command
            key  = args[0].lower()
            meta = CATEGORIES[key]
            embed = build_category_embed(key, meta)
            await ctx.send(embed=embed)
        
        else:
            await ctx.send(
                f"❌ Unknown help topic: `{args[0]}`\n"
                f"Available: **story** · **battle** · **marketplace** · **shop** · **leaderboard** · **boosters** · **spawncog** · **casino** · **avatar** · **moves** · **matchups** · **tips**"
            )

async def setup(bot: commands.Bot):
    bot.remove_command("help")   # Remove discord.py default before adding ours
    await bot.add_cog(HelpCog(bot))
