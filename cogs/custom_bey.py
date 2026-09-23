"""Discord UI for /custombey."""
from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands
from utils.database import get_user, mutate_user, get_beyblade
from utils import bey_levels as BL
from utils.inventory import require_room, InventoryFull
from utils.custom_bey import ABILITY_PRESETS, ABILITY_BUDGET, CUSTOM_TRIGGERS, SPECIAL_EFFECTS, STAT_TOTAL, allowed_triggers, build, CustomBeyError

ABILITY_EFFECT_CATALOG = tuple(ABILITY_PRESETS)
ABILITY_EFFECTS_PER_PAGE = 10

def _summary(blade):
    s=blade["stats"]; meta=blade.get("custom_meta",{})
    abilities=[a["name"] for a in blade.get("abilities",[]) if not a.get("_custom_special_effect")]
    sm=blade["special_move"]
    e=discord.Embed(title=f"🛠️ {blade['name']}",description=f"**CUSTOM • {blade['type']}**\nPlayer-created Bey",colour=0x9B59B6)
    e.add_field(name=f"Stats • {STAT_TOTAL}/{STAT_TOTAL}",value=f"❤️ HP **{s['hp']}**\n⚔️ ATK **{s['attack']}**\n🛡️ DEF **{s['defense']}**\n🌀 STM **{s['stamina']}**",inline=True)
    e.add_field(name=f"Abilities • {meta.get('ability_cost',0)}/{ABILITY_BUDGET}",value="\n".join("• "+x for x in abilities) if abilities else "None",inline=True)
    e.add_field(name=f"✨ {sm['name']}",value=f"Base damage **{sm['damage_per_hit']}**\n{sm.get('description','No secondary effect')}",inline=False)
    e.add_field(name="📈 Level", value=f"Starts at **Level 1** • Max **{BL.MAX_LEVEL}**", inline=False)
    if blade.get("image_url"): e.set_thumbnail(url=blade["image_url"])
    return e

class AbilityCatalogView(discord.ui.View):
    """Read-only browser for the server-approved Custom Bey effects."""
    def __init__(self, owner_id: int, page: int = 0):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.page = page
        self.pages = (len(ABILITY_EFFECT_CATALOG) + ABILITY_EFFECTS_PER_PAGE - 1) // ABILITY_EFFECTS_PER_PAGE
        self._sync_buttons()

    def _sync_buttons(self):
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.pages - 1

    def embed(self):
        start = self.page * ABILITY_EFFECTS_PER_PAGE
        rows = ABILITY_EFFECT_CATALOG[start:start + ABILITY_EFFECTS_PER_PAGE]
        body = "\n".join(
            f"**{start + i + 1}.** `{name}` — {ABILITY_PRESETS[name]['description']}"
            for i, name in enumerate(rows)
        )
        e = discord.Embed(
            title=f"⚙️ Custom Ability Effects • {len(ABILITY_EFFECT_CATALOG)}",
            description=body,
            colour=0x5865F2,
        )
        e.set_footer(text=f"Page {self.page + 1}/{self.pages} • View only")
        return e

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ This Ability browser belongs to another player.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.pages - 1, self.page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)


class CustomBeyView(discord.ui.View):
    """Actions shown with a Custom Bey card."""
    def __init__(self, owner_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id

    @discord.ui.button(label="Ability", emoji="⚙️", style=discord.ButtonStyle.primary)
    async def ability(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("❌ This Custom Bey panel belongs to another player.", ephemeral=True)
        browser = AbilityCatalogView(self.owner_id)
        await interaction.response.send_message(embed=browser.embed(), view=browser, ephemeral=True)


class AbilitySetupView(discord.ui.View):
    """Single ephemeral panel for choosing both Custom Bey abilities and triggers."""
    def __init__(self, owner_id: int, draft: dict):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.draft = draft
        self.effect1 = None
        self.trigger1 = None
        self.effect2 = None
        self.trigger2 = None

        effect_options = [
            discord.SelectOption(label=v["label"], value=k, description=v["description"][:100])
            for k, v in ABILITY_PRESETS.items()
        ]
        self.effect_one = discord.ui.Select(placeholder="Ability 1 effect", options=effect_options, row=0)
        self.trigger_one = discord.ui.Select(
            placeholder="Choose Ability 1 effect first", options=[
                discord.SelectOption(label="Choose an effect first", value="pending")
            ], disabled=True, row=1)
        self.effect_two = discord.ui.Select(
            placeholder="Ability 2 effect (optional)", options=[
                discord.SelectOption(label="None", value="none", description="Use only one ability"),
                *effect_options,
            ], row=2)
        self.trigger_two = discord.ui.Select(
            placeholder="Choose Ability 2 effect first", options=[
                discord.SelectOption(label="None", value="none", description="No second ability")
            ], disabled=True, row=3)
        self.effect_one.callback = self._effect_one
        self.trigger_one.callback = self._trigger_one
        self.effect_two.callback = self._effect_two
        self.trigger_two.callback = self._trigger_two
        self.add_item(self.effect_one)
        self.add_item(self.trigger_one)
        self.add_item(self.effect_two)
        self.add_item(self.trigger_two)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ This Custom Bey setup belongs to another player.", ephemeral=True)
            return False
        return True

    def _trigger_options(self, effect_key, optional=False):
        options = [
            discord.SelectOption(label=k.replace("_", " ").title(), value=k)
            for k in allowed_triggers(effect_key)
        ]
        if optional:
            options.insert(0, discord.SelectOption(label="None", value="none", description="No second ability"))
        return options

    async def _effect_one(self, interaction):
        self.effect1 = self.effect_one.values[0]
        self.trigger1 = None
        self.trigger_one.options = self._trigger_options(self.effect1)
        self.trigger_one.disabled = False
        self.trigger_one.placeholder = "Ability 1 trigger"
        await interaction.response.edit_message(view=self)

    async def _trigger_one(self, interaction):
        self.trigger1 = self.trigger_one.values[0]
        await interaction.response.defer()

    async def _effect_two(self, interaction):
        self.effect2 = None if self.effect_two.values[0] == "none" else self.effect_two.values[0]
        self.trigger2 = None
        if self.effect2:
            self.trigger_two.options = self._trigger_options(self.effect2, optional=True)
            self.trigger_two.disabled = False
            self.trigger_two.placeholder = "Ability 2 trigger (optional)"
        else:
            self.trigger_two.options = [discord.SelectOption(label="None", value="none", description="No second ability")]
            self.trigger_two.disabled = True
            self.trigger_two.placeholder = "No second ability"
        await interaction.response.edit_message(view=self)

    async def _trigger_two(self, interaction):
        self.trigger2 = None if self.trigger_two.values[0] == "none" else self.trigger_two.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="Create Custom Bey", emoji="✅", style=discord.ButtonStyle.success, row=4)
    async def create(self, interaction, button):
        if not self.effect1 or not self.trigger1:
            return await interaction.response.send_message("❌ Select Ability 1 effect and trigger.", ephemeral=True)
        if bool(self.effect2) != bool(self.trigger2):
            return await interaction.response.send_message("❌ Ability 2 needs both an effect and a trigger, or choose None for both.", ephemeral=True)
        keys = [self.effect1] + ([self.effect2] if self.effect2 else [])
        triggers = [self.trigger1] + ([self.trigger2] if self.trigger2 else [])
        try:
            blade = build(
                self.draft["name"], self.draft["bey_type"], *self.draft["stats"],
                keys, self.draft["special_name"], self.draft["special_damage"],
                self.draft["special_effect"], self.draft["image"], triggers)
            if get_beyblade(blade["name"]):
                raise CustomBeyError("That name already belongs to an official Bey.")
            def save(profile):
                if profile.get("custom_bey"):
                    raise CustomBeyError("You already own a Custom Bey. Delete it before creating another.")
                require_room(profile, 1, blade["name"])
                profile["custom_bey"] = blade
                # Custom Beys live in the same inventory and level progression as normal Beys.
                profile.setdefault("inventory", []).append(blade["name"])
                BL.entry_for(profile, blade["name"])
                return blade
            await mutate_user(interaction.user.id, save)
        except (CustomBeyError, InventoryFull) as exc:
            return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        self.stop()
        await interaction.response.edit_message(
            content="✅ **Custom Bey created!** Use /custombey action:equip to equip it.",
            embed=_summary(blade), view=CustomBeyView(interaction.user.id))

class CustomBeyModal(discord.ui.Modal,title="Create Your Custom Bey"):
    name=discord.ui.TextInput(label="Bey name",placeholder="Dark Phoenix",max_length=32)
    hp=discord.ui.TextInput(label="HP",placeholder="100",max_length=3)
    attack=discord.ui.TextInput(label="Attack",placeholder="100",max_length=3)
    defense=discord.ui.TextInput(label="Defense",placeholder="100",max_length=3)
    stamina=discord.ui.TextInput(label="Stamina",placeholder="95",max_length=3)

    def __init__(self,bey_type):
        super().__init__(); self.bey_type=bey_type

    async def on_submit(self,interaction):
        try:
            vals=[int(str(x).strip()) for x in (self.hp,self.attack,self.defense,self.stamina)]
        except (TypeError,ValueError):
            return await interaction.response.send_message("❌ HP, Attack, Defense and Stamina must each be a number.",ephemeral=True)
        try:
            # Validate stats now; Special and image are collected in the next step.
            if any(v < 20 for v in vals):
                raise CustomBeyError("Every stat must be at least 20.")
            if sum(vals) != STAT_TOTAL:
                raise CustomBeyError(f"HP + ATK + DEF + STM must equal exactly {STAT_TOTAL}; yours totals {sum(vals)}.")
        except CustomBeyError as exc:
            return await interaction.response.send_message(f"❌ {exc}",ephemeral=True)
        await interaction.response.send_modal(CustomBeyDetailsModal(self.bey_type,str(self.name),vals))

class CustomBeyDetailsModal(discord.ui.Modal,title="Custom Bey Details"):
    special=discord.ui.TextInput(label="Special: name | damage | effect",placeholder="Phoenix Break | 120 | heal",max_length=80)
    image=discord.ui.TextInput(label="Image URL",required=True,placeholder="https://...",max_length=400)

    def __init__(self,bey_type,name,stats):
        super().__init__(); self.bey_type=bey_type; self.name=name; self.stats=stats

    async def on_submit(self,interaction):
        parts=[x.strip() for x in str(self.special).split("|")]
        if len(parts)!=3:
            return await interaction.response.send_message("❌ Special must be Name | damage | effect, for example Phoenix Break | 120 | heal.",ephemeral=True)
        try:
            damage=int(parts[1])
            build(self.name,self.bey_type,*self.stats,[],parts[0],damage,parts[2].lower(),str(self.image),[])
        except (CustomBeyError,ValueError) as exc:
            return await interaction.response.send_message(f"❌ {exc}",ephemeral=True)
        draft={
            "name":self.name, "bey_type":self.bey_type, "stats":self.stats,
            "special_name":parts[0], "special_damage":damage,
            "special_effect":parts[2].lower(), "image":str(self.image),
        }
        view=AbilitySetupView(interaction.user.id,draft)
        embed=discord.Embed(
            title="⚙️ Choose Custom Bey Abilities",
            description=(
                "Select **Ability 1 + its trigger** and optionally **Ability 2 + its trigger** below.\n"
                f"Ability budget: **{ABILITY_BUDGET} points**. The bot validates the final combination."
            ),
            colour=0x5865F2,
        )
        await interaction.response.send_message(embed=embed,view=view,ephemeral=True)

class CustomBeyCog(commands.Cog):
    def __init__(self,bot): self.bot=bot
    @app_commands.command(name="custombey",description="Create, view, equip, or delete your Custom Bey")
    @app_commands.describe(action="What you want to do",bey_type="Type used when creating a Bey")
    @app_commands.choices(action=[
        app_commands.Choice(name="Create",value="create"),app_commands.Choice(name="View",value="view"),
        app_commands.Choice(name="Equip",value="equip"),app_commands.Choice(name="Delete",value="delete"),
        app_commands.Choice(name="Rules",value="rules")],
        bey_type=[app_commands.Choice(name=x,value=x) for x in ("Attack","Defense","Stamina","Balance")])
    async def custombey(self,interaction,action: app_commands.Choice[str],bey_type: app_commands.Choice[str]|None=None):
        act=action.value
        if act=="create":
            if bey_type is None: return await interaction.response.send_message("❌ Choose a bey_type when creating your Bey.",ephemeral=True)
            if (await get_user(interaction.user.id)).get("custom_bey"):
                return await interaction.response.send_message("❌ You already own a Custom Bey. View or delete it first.",ephemeral=True)
            return await interaction.response.send_modal(CustomBeyModal(bey_type.value))
        if act=="rules":
            abilities=", ".join(f"{k} ({v['cost']})" for k,v in ABILITY_PRESETS.items()); effects=", ".join(SPECIAL_EFFECTS)
            return await interaction.response.send_message(f"### 🛠️ Custom Bey Rules\n• HP + ATK + DEF + STM = **{STAT_TOTAL}** exactly.\n• No individual maximum; minimum **20** each.\n• Up to **2 abilities**, **{ABILITY_BUDGET}** ability points.\n• Ability keys: {abilities}\n• Ability effects and triggers are selected from the creation panel.\n• Triggers: {", ".join(CUSTOM_TRIGGERS)}\n• Special damage **80-140**; effects: {effects}\n• Image URL is required.\n• Custom Beys start at **Level 1**, use the normal Bey XP/level system, and go directly into your regular inventory.\n• One Custom Bey per player; server-side validation.",ephemeral=True)
        profile=await get_user(interaction.user.id); blade=profile.get("custom_bey")
        if not isinstance(blade,dict): return await interaction.response.send_message("❌ You don't have a Custom Bey yet. Use /custombey action:create.",ephemeral=True)
        if act=="view": return await interaction.response.send_message(embed=_summary(blade),view=CustomBeyView(interaction.user.id),ephemeral=True)
        if act=="equip":
            def equip(prof):
                current=prof.get("custom_bey")
                if not isinstance(current,dict): raise CustomBeyError("Your Custom Bey no longer exists.")
                prof["active_copy"]=None; prof["active_custom_bey"]=True; prof["active_beyblade"]=current["name"]
            try: await mutate_user(interaction.user.id,equip)
            except CustomBeyError as exc: return await interaction.response.send_message(f"❌ {exc}",ephemeral=True)
            return await interaction.response.send_message(f"✅ **{blade['name']}** is now equipped.",ephemeral=True)
        if act=="delete":
            def delete(prof):
                old=prof.pop("custom_bey",None)
                if not isinstance(old,dict): raise CustomBeyError("Your Custom Bey no longer exists.")
                name=old.get("name"); inv=prof.get("inventory") or []
                try: inv.remove(name)
                except ValueError: pass
                prof["inventory"]=inv
                if prof.get("active_custom_bey"): prof["active_custom_bey"]=False; prof["active_beyblade"]=None
                return name
            try: name=await mutate_user(interaction.user.id,delete)
            except CustomBeyError as exc: return await interaction.response.send_message(f"❌ {exc}",ephemeral=True)
            return await interaction.response.send_message(f"🗑️ Custom Bey **{name}** deleted.",ephemeral=True)

async def setup(bot): await bot.add_cog(CustomBeyCog(bot))
