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


class BuilderTextModal(discord.ui.Modal):
    def __init__(self, builder, kind):
        titles={"name":"Set Bey Name","stats":"Set Bey Stats","special":"Set Special Move","image_url":"Set Image URL","ability_name":"Name Ability"}
        super().__init__(title=titles[kind])
        self.builder=builder; self.kind=kind
        if kind=="name":
            self.value=discord.ui.TextInput(label="Bey name",placeholder="Dark Phoenix",max_length=32)
            self.add_item(self.value)
        elif kind=="stats":
            self.hp=discord.ui.TextInput(label="HP",placeholder="100",max_length=3)
            self.attack=discord.ui.TextInput(label="Attack",placeholder="100",max_length=3)
            self.defense=discord.ui.TextInput(label="Defense",placeholder="100",max_length=3)
            self.stamina=discord.ui.TextInput(label="Stamina",placeholder="95",max_length=3)
            for item in (self.hp,self.attack,self.defense,self.stamina): self.add_item(item)
        elif kind=="special":
            self.special_name=discord.ui.TextInput(label="Special name",placeholder="Phoenix Break",max_length=32)
            self.damage=discord.ui.TextInput(label="Base damage (80-140)",placeholder="120",max_length=3)
            self.effect=discord.ui.TextInput(label="Effect: none | heal | shield",placeholder="none",max_length=10)
            for item in (self.special_name,self.damage,self.effect): self.add_item(item)
        elif kind=="image_url":
            self.value=discord.ui.TextInput(label="Direct image URL",placeholder="https://...",max_length=400)
            self.add_item(self.value)
        else:
            self.value=discord.ui.TextInput(label="Ability name",placeholder="Dragon Rage",max_length=32)
            self.add_item(self.value)

    async def on_submit(self,interaction):
        try:
            if self.kind=="name":
                name=str(self.value).strip()
                if len(name)<3: raise CustomBeyError("Bey name must be at least 3 characters.")
                self.builder.draft["name"]=name
            elif self.kind=="stats":
                vals=[int(str(x).strip()) for x in (self.hp,self.attack,self.defense,self.stamina)]
                if any(v<20 for v in vals): raise CustomBeyError("Every stat must be at least 20.")
                if sum(vals)!=STAT_TOTAL: raise CustomBeyError(f"Stats must total {STAT_TOTAL}; yours total {sum(vals)}.")
                self.builder.draft["stats"]=vals
            elif self.kind=="special":
                damage=int(str(self.damage).strip()); effect=str(self.effect).strip().lower()
                if not 80<=damage<=140: raise CustomBeyError("Special base damage must be 80-140.")
                if effect not in SPECIAL_EFFECTS: raise CustomBeyError("Special effect must be none, heal, or shield.")
                self.builder.draft["special_name"]=str(self.special_name).strip()
                self.builder.draft["special_damage"]=damage; self.builder.draft["special_effect"]=effect
            elif self.kind=="image_url":
                value=str(self.value).strip()
                if not value.startswith(("http://","https://")): raise CustomBeyError("Enter a valid http/https image URL.")
                self.builder.draft["image"]=value
            else:
                idx=self.builder.editing_ability
                self.builder.abilities[idx]["name"]=str(self.value).strip()
        except (CustomBeyError,ValueError) as exc:
            return await interaction.response.send_message(f"❌ {exc}",ephemeral=True)
        self.builder.mode="main" if self.kind!="ability_name" else "ability"
        self.builder.rebuild()
        await interaction.response.edit_message(embed=self.builder.embed(),view=self.builder)

class CustomBeyBuilder(discord.ui.View):
    def __init__(self,owner_id,bey_type):
        super().__init__(timeout=600)
        self.owner_id=owner_id; self.mode="main"; self.editing_ability=0
        self.draft={"name":None,"bey_type":bey_type,"stats":None,"image":None,
                    "special_name":None,"special_damage":None,"special_effect":None}
        self.abilities=[{"name":None,"effect":None,"trigger":None},{"name":None,"effect":None,"trigger":None}]
        self.rebuild()

    async def interaction_check(self,interaction):
        if interaction.user.id!=self.owner_id:
            await interaction.response.send_message("❌ This builder belongs to another player.",ephemeral=True); return False
        return True

    def embed(self):
        if self.mode=="ability":
            a=self.abilities[self.editing_ability]; cfg=ABILITY_PRESETS.get(a["effect"] or "")
            desc=cfg["description"] if cfg else "Choose an effect to see exactly what it does."
            cost=cfg["cost"] if cfg else 0
            triggers=", ".join(x.replace("_"," ").title() for x in allowed_triggers(a["effect"])) if a["effect"] else "Choose an effect first"
            return discord.Embed(title=f"⚙️ Ability {self.editing_ability+1} Builder",
                description=f"**Name:** {a['name'] or 'Not Set'}\n**Effect:** {(cfg or {}).get('label','Not Set')}\n**Effect details:** {desc}\n**Cost:** {cost}/{ABILITY_BUDGET}\n**Compatible triggers:** {triggers}",
                colour=0x5865F2)
        stats=self.draft["stats"]; total=sum(stats) if stats else 0
        a1=self.abilities[0]; a2=self.abilities[1]
        def aline(a):
            cfg=ABILITY_PRESETS.get(a["effect"] or "")
            return f"{a['name'] or 'Not Set'}" + (f" • {cfg['label']} • {(a['trigger'] or 'No trigger').replace('_',' ').title()}" if cfg else "")
        e=discord.Embed(title="🛠️ CUSTOM BEY BUILDER",
            description=f"**Name:** {self.draft['name'] or 'Not Set'}\n**Type:** {self.draft['bey_type']}\n**Level:** 1\n\n"
                        f"❤️ **HP:** {stats[0] if stats else 'Not Set'}\n⚔️ **Attack:** {stats[1] if stats else 'Not Set'}\n"
                        f"🛡️ **Defense:** {stats[2] if stats else 'Not Set'}\n🌀 **Stamina:** {stats[3] if stats else 'Not Set'}\n📊 **Total:** {total}/{STAT_TOTAL}\n\n"
                        f"🖼️ **Image:** {'Set ✅' if self.draft['image'] else 'Not Set'}\n"
                        f"⚙️ **Ability 1:** {aline(a1)}\n⚙️ **Ability 2:** {aline(a2)}\n"
                        f"✨ **Special:** {self.draft['special_name'] or 'Not Set'}",
            colour=0x9B59B6)
        if self.draft["image"]: e.set_thumbnail(url=self.draft["image"])
        return e

    def button(self,label,emoji,callback,row=0,style=discord.ButtonStyle.secondary):
        b=discord.ui.Button(label=label,emoji=emoji,style=style,row=row); b.callback=callback; self.add_item(b)

    def rebuild(self):
        self.clear_items()
        if self.mode=="ability":
            opts=[discord.SelectOption(label=v["label"],value=k,description=f"{v['description']} • {v['cost']} pts"[:100]) for k,v in ABILITY_PRESETS.items()]
            effect=discord.ui.Select(placeholder="Ability Effect — view all 16 effects",options=opts,row=0); effect.callback=self.pick_effect; self.add_item(effect)
            a=self.abilities[self.editing_ability]
            trig_opts=[discord.SelectOption(label=x.replace("_"," ").title(),value=x) for x in allowed_triggers(a["effect"])] if a["effect"] else [discord.SelectOption(label="Choose an effect first",value="pending")]
            trig=discord.ui.Select(placeholder="Activation Trigger",options=trig_opts,disabled=not bool(a["effect"]),row=1); trig.callback=self.pick_trigger; self.add_item(trig)
            self.button("Name Ability","✏️",self.name_ability,row=2)
            self.button("Ability Guide","📖",self.guide,row=2)
            self.button("Back","⬅️",self.back,row=3)
            if self.editing_ability==1: self.button("Clear Ability 2","🗑️",self.clear_ability,row=3,style=discord.ButtonStyle.danger)
        else:
            self.button("Name","✏️",self.set_name,row=0); self.button("Stats","📊",self.set_stats,row=0); self.button("Image","🖼️",self.image_menu,row=0)
            self.button("Ability 1","1️⃣",self.ability1,row=1); self.button("Ability 2","2️⃣",self.ability2,row=1); self.button("Special","✨",self.set_special,row=1)
            self.button("Create Bey","✅",self.create,row=2,style=discord.ButtonStyle.success)

    async def set_name(self,i): await i.response.send_modal(BuilderTextModal(self,"name"))
    async def set_stats(self,i): await i.response.send_modal(BuilderTextModal(self,"stats"))
    async def set_special(self,i): await i.response.send_modal(BuilderTextModal(self,"special"))
    async def name_ability(self,i): await i.response.send_modal(BuilderTextModal(self,"ability_name"))
    async def ability1(self,i): self.editing_ability=0; self.mode="ability"; self.rebuild(); await i.response.edit_message(embed=self.embed(),view=self)
    async def ability2(self,i): self.editing_ability=1; self.mode="ability"; self.rebuild(); await i.response.edit_message(embed=self.embed(),view=self)
    async def back(self,i): self.mode="main"; self.rebuild(); await i.response.edit_message(embed=self.embed(),view=self)
    async def clear_ability(self,i): self.abilities[1]={"name":None,"effect":None,"trigger":None}; self.mode="main"; self.rebuild(); await i.response.edit_message(embed=self.embed(),view=self)

    async def pick_effect(self,i):
        a=self.abilities[self.editing_ability]; a["effect"]=i.data["values"][0]; a["trigger"]=None
        self.rebuild(); await i.response.edit_message(embed=self.embed(),view=self)
    async def pick_trigger(self,i):
        self.abilities[self.editing_ability]["trigger"]=i.data["values"][0]
        await i.response.edit_message(embed=self.embed(),view=self)
    async def guide(self,i):
        browser=AbilityCatalogView(self.owner_id)
        await i.response.send_message(embed=browser.embed(),view=browser,ephemeral=True)

    async def image_menu(self,i):
        view=ImageChoiceView(self)
        await i.response.send_message("🖼️ **Set Custom Bey Image**\nChoose URL or upload an image from your device.",view=view,ephemeral=True)

    async def create(self,i):
        if not self.draft["name"] or not self.draft["stats"] or not self.draft["image"] or not self.draft["special_name"]:
            return await i.response.send_message("❌ Complete Name, Stats, Image and Special first.",ephemeral=True)
        a1,a2=self.abilities
        if not all((a1["name"],a1["effect"],a1["trigger"])):
            return await i.response.send_message("❌ Complete Ability 1: name, effect and trigger.",ephemeral=True)
        if any(a2.values()) and not all(a2.values()):
            return await i.response.send_message("❌ Complete all Ability 2 fields or clear Ability 2.",ephemeral=True)
        chosen=[a1]+([a2] if all(a2.values()) else [])
        keys=[a["effect"] for a in chosen]; triggers=[a["trigger"] for a in chosen]
        try:
            blade=build(self.draft["name"],self.draft["bey_type"],*self.draft["stats"],keys,
                        self.draft["special_name"],self.draft["special_damage"],self.draft["special_effect"],self.draft["image"],triggers)
            if get_beyblade(blade["name"]): raise CustomBeyError("That name already belongs to an official Bey.")
            # Player-written ability names are cosmetic; the server-owned effect operation stays unchanged.
            normal=[a for a in blade["abilities"] if not a.get("_custom_special_effect")]
            for built,chosen_ability in zip(normal,chosen):
                built["name"]=chosen_ability["name"]
                for rule in built.get("rules",[]): rule["_name"]=chosen_ability["name"]
            def save(profile):
                if profile.get("custom_bey"): raise CustomBeyError("You already own a Custom Bey.")
                require_room(profile,1,blade["name"]); profile["custom_bey"]=blade
                profile.setdefault("inventory",[]).append(blade["name"]); BL.entry_for(profile,blade["name"])
            await mutate_user(i.user.id,save)
        except (CustomBeyError,InventoryFull) as exc:
            return await i.response.send_message(f"❌ {exc}",ephemeral=True)
        self.stop(); await i.response.edit_message(content="✅ **Custom Bey created and added to your inventory!**",embed=_summary(blade),view=CustomBeyView(i.user.id))

class ImageChoiceView(discord.ui.View):
    def __init__(self,builder):
        super().__init__(timeout=120); self.builder=builder
    async def interaction_check(self,i):
        if i.user.id!=self.builder.owner_id:
            await i.response.send_message("❌ This image setup belongs to another player.",ephemeral=True); return False
        return True
    @discord.ui.button(label="Image URL",emoji="🔗")
    async def url(self,i,b): await i.response.send_modal(BuilderTextModal(self.builder,"image_url"))
    @discord.ui.button(label="Upload Image",emoji="📤",style=discord.ButtonStyle.primary)
    async def upload(self,i,b):
        await i.response.send_message("📤 Upload **one image** in this channel within 60 seconds. I will use its Discord attachment URL.",ephemeral=True)
        def check(m): return m.author.id==i.user.id and m.channel.id==i.channel.id and bool(m.attachments)
        try: msg=await self.builder._client.wait_for("message",timeout=60,check=check)
        except Exception: return await i.followup.send("❌ Image upload timed out. Press Image and try again.",ephemeral=True)
        att=msg.attachments[0]
        if not (att.content_type or "").startswith("image/"):
            return await i.followup.send("❌ That attachment is not an image.",ephemeral=True)
        self.builder.draft["image"]=att.url
        await i.followup.send("✅ Image uploaded. Return to the builder and continue.",ephemeral=True)


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
            builder=CustomBeyBuilder(interaction.user.id,bey_type.value); builder._client=self.bot\n            return await interaction.response.send_message(embed=builder.embed(),view=builder,ephemeral=True)
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
