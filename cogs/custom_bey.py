"""Discord UI for /custombey."""
from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands
from utils.database import get_user, mutate_user, get_beyblade
from utils.inventory import require_room, InventoryFull
from utils.custom_bey import ABILITY_PRESETS, ABILITY_BUDGET, CUSTOM_TRIGGERS, SPECIAL_EFFECTS, STAT_TOTAL, build, CustomBeyError

ABILITY_EFFECT_CATALOG = tuple(ABILITY_PRESETS)\nABILITY_EFFECTS_PER_PAGE = 10

def _summary(blade):
    s=blade["stats"]; meta=blade.get("custom_meta",{})
    abilities=[a["name"] for a in blade.get("abilities",[]) if not a.get("_custom_special_effect")]
    sm=blade["special_move"]
    e=discord.Embed(title=f"🛠️ {blade['name']}",description=f"**CUSTOM • {blade['type']}**\nPlayer-created Bey",colour=0x9B59B6)
    e.add_field(name=f"Stats • {STAT_TOTAL}/{STAT_TOTAL}",value=f"❤️ HP **{s['hp']}**\n⚔️ ATK **{s['attack']}**\n🛡️ DEF **{s['defense']}**\n🌀 STM **{s['stamina']}**",inline=True)
    e.add_field(name=f"Abilities • {meta.get('ability_cost',0)}/{ABILITY_BUDGET}",value="\n".join("• "+x for x in abilities) if abilities else "None",inline=True)
    e.add_field(name=f"✨ {sm['name']}",value=f"Base damage **{sm['damage_per_hit']}**\n{sm.get('description','No secondary effect')}",inline=False)
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
            f"**{start + i + 1}.** `{name}` — {name.replace('_', ' ').title()}"
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


class CustomBeyModal(discord.ui.Modal,title="Create Your Custom Bey"):
    name=discord.ui.TextInput(label="Bey name",placeholder="Dark Phoenix",max_length=32)
    stats=discord.ui.TextInput(label="Stats: HP, ATK, DEF, STM",placeholder="100,100,100,95",max_length=32)
    abilities=discord.ui.TextInput(label="Abilities: effect@trigger (0-2)",placeholder="pattern_reader@attack_win,second_wind@low_hp",required=False,max_length=100)
    special=discord.ui.TextInput(label="Special: name | damage | effect",placeholder="Phoenix Break | 120 | heal",max_length=80)
    image=discord.ui.TextInput(label="Image URL (optional)",required=False,placeholder="https://...",max_length=400)
    def __init__(self,bey_type):
        super().__init__(); self.bey_type=bey_type
    async def on_submit(self,interaction):
        try:
            vals=[int(x.strip()) for x in str(self.stats).split(",")]
            if len(vals)!=4: raise ValueError
        except (TypeError,ValueError):
            return await interaction.response.send_message("❌ Stats must be HP,ATK,DEF,STM, for example 100,100,100,95.",ephemeral=True)
        parts=[x.strip() for x in str(self.special).split("|")]
        if len(parts)!=3:
            return await interaction.response.send_message("❌ Special must be Name | damage | effect, for example Phoenix Break | 120 | heal.",ephemeral=True)
        try:
            damage=int(parts[1])
            entries=[x.strip().lower() for x in str(self.abilities).split(",") if x.strip()]
            keys=[]; triggers=[]
            for entry in entries:
                if "@" not in entry:
                    raise CustomBeyError("Each ability needs a trigger: effect@trigger, for example pattern_reader@attack_win.")
                key, trigger = (x.strip() for x in entry.split("@", 1))
                keys.append(key); triggers.append(trigger)
            blade=build(str(self.name),self.bey_type,*vals,keys,parts[0],damage,parts[2].lower(),str(self.image),triggers)
            if get_beyblade(blade["name"]): raise CustomBeyError("That name already belongs to an official Bey.")
        except (CustomBeyError,ValueError) as exc:
            return await interaction.response.send_message(f"❌ {exc}",ephemeral=True)
        def save(profile):
            if profile.get("custom_bey"): raise CustomBeyError("You already own a Custom Bey. Delete it before creating another.")
            require_room(profile,1,blade["name"]); profile["custom_bey"]=blade; profile.setdefault("inventory",[]).append(blade["name"]); return blade
        try: await mutate_user(interaction.user.id,save)
        except (CustomBeyError,InventoryFull) as exc:
            return await interaction.response.send_message(f"❌ {exc}",ephemeral=True)
        await interaction.response.send_message(
            "✅ **Custom Bey created!** Use /custombey action:equip to equip it.",
            embed=_summary(blade), view=CustomBeyView(interaction.user.id), ephemeral=True)

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
            return await interaction.response.send_message(f"### 🛠️ Custom Bey Rules\n• HP + ATK + DEF + STM = **{STAT_TOTAL}** exactly.\n• No individual maximum; minimum **20** each.\n• Up to **2 abilities**, **{ABILITY_BUDGET}** ability points.\n• Ability keys: {abilities}\n• Choose a trigger for every ability: **effect@trigger**.\n• Triggers: {", ".join(CUSTOM_TRIGGERS)}\n• Special damage **80-140**; effects: {effects}\n• One Custom Bey per player; server-side validation.",ephemeral=True)
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
