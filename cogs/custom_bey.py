"""Discord UI for /custombey."""
from __future__ import annotations
import asyncio
import io
import ipaddress
import aiohttp
from PIL import Image
import discord
from discord import app_commands
from discord.ext import commands
from utils.database import get_user, mutate_user, get_beyblade, all_user_ids, get_custom_review_channel, set_custom_review_channel
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

class ModernPanel(discord.ui.LayoutView):
    """Native Components V2 card with grouped controls."""
    def __init__(self, timeout=180):
        super().__init__(timeout=timeout)
        self.card = discord.ui.Container(accent_colour=0x5865F2)
        self.rows = {}
        super().add_item(self.card)

    def clear_items(self):
        super().clear_items()
        self.card = discord.ui.Container(accent_colour=0x5865F2)
        self.rows = {}
        super().add_item(self.card)
        return self

    def add_item(self, item):
        row = item.row or 0
        if row not in self.rows:
            self.rows[row] = discord.ui.ActionRow()
            self.card.add_item(self.rows[row])
        self.rows[row].add_item(item)
        return self

    def display(self, embed=None, content=None):
        # Render text and media inside the Components V2 container.
        if embed is None and hasattr(self, "embed"):
            embed = self.embed()
        self.card.clear_items()
        if content:
            self.card.add_item(discord.ui.TextDisplay(content))
        if embed:
            self.card.add_item(discord.ui.TextDisplay(discord.utils.escape_mentions(f"## {embed.title}\n{embed.description or ''}")))
            for field in embed.fields:
                self.card.add_item(discord.ui.TextDisplay(discord.utils.escape_mentions(f"### {field.name}\n{field.value}")))
            media = embed.image.url or embed.thumbnail.url
            if media:
                self.card.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(media=media)))
            if embed.footer.text:
                self.card.add_item(discord.ui.TextDisplay(f"-# {embed.footer.text}"))
        for row in self.rows.values():
            self.card.add_item(row)
        return self

    def control(self, label, callback, row=0, style=discord.ButtonStyle.secondary, custom_id=None):
        button = discord.ui.Button(label=label, style=style, row=row, custom_id=custom_id)
        button.callback = callback
        self.add_item(button)
        return button


class AbilityCatalogView(ModernPanel):
    def __init__(self, owner_id: int, page: int = 0):
        super().__init__()
        self.owner_id, self.page = owner_id, page
        self.pages = (len(ABILITY_EFFECT_CATALOG) + ABILITY_EFFECTS_PER_PAGE - 1) // ABILITY_EFFECTS_PER_PAGE
        self.previous = self.control("Previous", self.prev_page)
        self.next = self.control("Next", self.next_page)
        self.refresh()

    def refresh(self):
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.pages - 1
        start = self.page * ABILITY_EFFECTS_PER_PAGE
        body = "\n\n".join(f"**{ABILITY_PRESETS[key]['label']} • {ABILITY_PRESETS[key]['cost']} points**\n{ABILITY_PRESETS[key]['description']}"
                            for key in ABILITY_EFFECT_CATALOG[start:start + ABILITY_EFFECTS_PER_PAGE])
        embed = discord.Embed(title=f"Ability Library • {len(ABILITY_EFFECT_CATALOG)} effects", description=body)
        embed.set_footer(text=f"Page {self.page + 1}/{self.pages} • 100-point budget • abilities activate at most once every 3 rounds")
        self.display(embed)

    async def interaction_check(self, i):
        if i.user.id != self.owner_id:
            await i.response.send_message("This panel belongs to another player.", ephemeral=True)
            return False
        return True

    async def prev_page(self, i):
        self.page = max(0, self.page - 1); self.refresh()
        await i.response.edit_message(view=self)

    async def next_page(self, i):
        self.page = min(self.pages - 1, self.page + 1); self.refresh()
        await i.response.edit_message(view=self)


class CustomBeyView(ModernPanel):
    def __init__(self, owner_id: int):
        super().__init__()
        self.owner_id = owner_id
        self.control("Ability Library", self.ability, style=discord.ButtonStyle.primary)

    async def interaction_check(self, i):
        if i.user.id != self.owner_id:
            await i.response.send_message("This panel belongs to another player.", ephemeral=True)
            return False
        return True

    async def ability(self, i):
        await i.response.send_message(view=AbilityCatalogView(self.owner_id), ephemeral=True)


async def validate_image_bytes(data):
    if len(data)>8*1024*1024:
        raise CustomBeyError("Image must be at most 8 MB.")
    def check():
        try:
            with Image.open(io.BytesIO(data)) as photo:
                if photo.format not in ("PNG", "WEBP") or photo.width * photo.height > 16000000:
                    raise CustomBeyError("Use a transparent PNG/WebP, at most 16 megapixels.")
                alpha = photo.convert("RGBA").getchannel("A")
                lo, hi = alpha.getextrema()
                if lo != 0 or hi == 0:
                    raise CustomBeyError("Remove the background first. The image needs both visible Bey pixels and fully transparent pixels.")
        except CustomBeyError:
            raise
        except Exception as exc:
            raise CustomBeyError("That file is not a valid PNG/WebP image.") from exc
    await asyncio.to_thread(check)


class PublicImageResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=0):
        rows = await self.resolver.resolve(host, port, family)
        if any(not ipaddress.ip_address(row["host"]).is_global for row in rows):
            raise CustomBeyError("Use a publicly accessible image URL.")
        return rows

    async def close(self):
        await self.resolver.close()


def public_image_url(url):
    from urllib.parse import urlparse
    parsed = urlparse(str(url))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CustomBeyError("Use a public http/https image URL.")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise CustomBeyError("Use a publicly accessible image URL.")


async def validate_image_url(url):
    from urllib.parse import urljoin
    try:
        public_image_url(url)
        connector = aiohttp.TCPConnector(resolver=PublicImageResolver())
        async with aiohttp.ClientSession(connector=connector,timeout=aiohttp.ClientTimeout(total=15)) as client:
            # Check every redirect, including literal IPs which bypass DNS.
            for _ in range(6):
                public_image_url(url)
                async with client.get(url, allow_redirects=False) as response:
                    if response.status in (301,302,303,307,308):
                        url=urljoin(str(response.url),response.headers.get("Location", ""))
                        continue
                    if response.status != 200:
                        raise CustomBeyError("Image URL could not be downloaded. Upload the image instead.")
                    chunks = []; size = 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > 8 * 1024 * 1024:
                            raise CustomBeyError("Image must be at most 8 MB.")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    break
            else:
                raise CustomBeyError("Too many image URL redirects. Upload the image instead.")
        await validate_image_bytes(data)
    except CustomBeyError:
        raise
    except Exception as exc:
        raise CustomBeyError("Image URL could not be checked. Upload a transparent PNG/WebP.") from exc

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
        if interaction.user.id != self.builder.owner_id:
            return await interaction.response.send_message("This builder belongs to another player.",ephemeral=True)
        await interaction.response.defer(ephemeral=True)
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
                await validate_image_url(value)
                self.builder.draft["image"]=value
            else:
                idx=self.builder.editing_ability
                self.builder.abilities[idx]["name"]=str(self.value).strip()
        except (CustomBeyError,ValueError) as exc:
            return await interaction.followup.send(f"❌ {exc}",ephemeral=True)
        self.builder.mode="main" if self.kind!="ability_name" else "ability"
        self.builder.rebuild()
        await self.builder._message.edit(view=self.builder.display())

class CustomBeyBuilder(ModernPanel):
    def __init__(self,owner_id,bey_type):
        super().__init__(timeout=600)
        self.owner_id=owner_id; self.mode="main"; self.editing_ability=0; self.effect_page=0
        self.draft={"name":None,"bey_type":bey_type,"stats":None,"image":None,
                    "special_name":None,"special_damage":None,"special_effect":None}
        self.abilities=[{"name":None,"effect":None,"trigger":None},{"name":None,"effect":None,"trigger":None}]
        self.rebuild()

    async def interaction_check(self,interaction):
        if interaction.user.id!=self.owner_id:
            await interaction.response.send_message("❌ This builder belongs to another player.",ephemeral=True); return False
        return True

    def used_points(self):
        return sum(ABILITY_PRESETS.get(a["effect"], {}).get("cost", 0) for a in self.abilities)

    def embed(self):
        if self.mode=="ability":
            a=self.abilities[self.editing_ability]; cfg=ABILITY_PRESETS.get(a["effect"] or "")
            desc=cfg["description"] if cfg else "Choose an effect to see exactly what it does."
            cost=cfg["cost"] if cfg else 0
            triggers=", ".join(x.replace("_"," ").title() for x in allowed_triggers(a["effect"])) if a["effect"] else "Choose an effect first"
            return discord.Embed(title=f"⚙️ Ability {self.editing_ability+1} Builder",
                description=f"**Name:** {a['name'] or 'Not Set'}\n**Effect:** {(cfg or {}).get('label','Not Set')}\n**Effect details:** {desc}\n**Cost:** {cost} points\n**Total spent:** {self.used_points()}/{ABILITY_BUDGET} • **Remaining:** {ABILITY_BUDGET-self.used_points()}\n**Compatible triggers:** {triggers}",
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
                        f"✨ **Special:** {self.draft['special_name'] or 'Not Set'}\n"
                        f"**Ability points:** {self.used_points()}/{ABILITY_BUDGET} • **Remaining:** {ABILITY_BUDGET-self.used_points()}\n"
                        "**Photo rule:** Background removed • transparent PNG/WebP\n**Ability cooldown:** Once every 3 rounds",
            colour=0x9B59B6)
        if self.draft["image"]: e.set_thumbnail(url=self.draft["image"])
        return e

    def button(self,label,emoji,callback,row=0,style=discord.ButtonStyle.secondary):
        b=discord.ui.Button(label=label,emoji=emoji,style=style,row=row); b.callback=callback; self.add_item(b)

    def rebuild(self):
        self.clear_items()
        if self.mode=="ability":
            page_keys=ABILITY_EFFECT_CATALOG[self.effect_page*20:(self.effect_page+1)*20]
            opts=[discord.SelectOption(label=f"{ABILITY_PRESETS[k]['label']} • {ABILITY_PRESETS[k]['cost']} pts",value=k,description=ABILITY_PRESETS[k]["description"][:100]) for k in page_keys]
            effect=discord.ui.Select(placeholder=f"Ability Effect • Page {self.effect_page+1}/3",options=opts,row=0); effect.callback=self.pick_effect; self.add_item(effect)
            a=self.abilities[self.editing_ability]
            trig_opts=[discord.SelectOption(label=x.replace("_"," ").title(),value=x) for x in allowed_triggers(a["effect"])] if a["effect"] else [discord.SelectOption(label="Choose an effect first",value="pending")]
            trig=discord.ui.Select(placeholder="Activation Trigger",options=trig_opts,disabled=not bool(a["effect"]),row=1); trig.callback=self.pick_trigger; self.add_item(trig)
            self.button("Previous Effects","◀️",self.previous_effects,row=2)
            self.button("Next Effects","▶️",self.next_effects,row=2)
            self.button("Name Ability","✏️",self.name_ability,row=2)
            self.button("Ability Guide","📖",self.guide,row=2)
            self.button("Back","⬅️",self.back,row=3)
            if self.editing_ability==1: self.button("Clear Ability 2","🗑️",self.clear_ability,row=3,style=discord.ButtonStyle.danger)
        else:
            self.button("Name","✏️",self.set_name,row=0); self.button("Stats","📊",self.set_stats,row=0); self.button("Image","🖼️",self.image_menu,row=0)
            self.button("Ability 1","1️⃣",self.ability1,row=1); self.button("Ability 2","2️⃣",self.ability2,row=1); self.button("Special","✨",self.set_special,row=1)
            self.button("Create Bey","✅",self.create,row=2,style=discord.ButtonStyle.success)
        self.display()

    async def previous_effects(self,i):
        self.effect_page=max(0,self.effect_page-1); self.rebuild(); await i.response.edit_message(view=self)
    async def next_effects(self,i):
        self.effect_page=min(2,self.effect_page+1); self.rebuild(); await i.response.edit_message(view=self)

    async def set_name(self,i): await i.response.send_modal(BuilderTextModal(self,"name"))
    async def set_stats(self,i): await i.response.send_modal(BuilderTextModal(self,"stats"))
    async def set_special(self,i): await i.response.send_modal(BuilderTextModal(self,"special"))
    async def name_ability(self,i): await i.response.send_modal(BuilderTextModal(self,"ability_name"))
    async def ability1(self,i): self.effect_page=0; self.editing_ability=0; self.mode="ability"; self.rebuild(); await i.response.edit_message(view=self.display())
    async def ability2(self,i): self.effect_page=0; self.editing_ability=1; self.mode="ability"; self.rebuild(); await i.response.edit_message(view=self.display())
    async def back(self,i): self.mode="main"; self.rebuild(); await i.response.edit_message(view=self.display())
    async def clear_ability(self,i): self.abilities[1]={"name":None,"effect":None,"trigger":None}; self.mode="main"; self.rebuild(); await i.response.edit_message(view=self.display())

    async def pick_effect(self,i):
        a=self.abilities[self.editing_ability]; key=i.data["values"][0]
        other=self.abilities[1-self.editing_ability]
        cost=ABILITY_PRESETS[key]["cost"]+ABILITY_PRESETS.get(other["effect"],{}).get("cost",0)
        if other["effect"]==key:
            return await i.response.send_message("Choose two different abilities.",ephemeral=True)
        if cost>ABILITY_BUDGET:
            return await i.response.send_message(f"This combination costs {cost}/{ABILITY_BUDGET}. Choose a cheaper effect.",ephemeral=True)
        a["effect"]=key; a["trigger"]=None
        self.rebuild(); await i.response.edit_message(view=self.display())
    async def pick_trigger(self,i):
        self.abilities[self.editing_ability]["trigger"]=i.data["values"][0]
        await i.response.edit_message(view=self.display())
    async def guide(self,i):
        browser=AbilityCatalogView(self.owner_id)
        await i.response.send_message(view=browser,ephemeral=True)

    async def image_menu(self,i):
        view=ImageChoiceView(self)
        await i.response.send_message(view=view,ephemeral=True)

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
        if not i.guild or not get_custom_review_channel(i.guild.id):
            return await i.response.send_message("Custom submissions need a review channel. Ask the bot owner to run ;setcustom in the approval server.",ephemeral=True)
        await i.response.defer()
        try:
            await validate_image_url(self.draft["image"])
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
                blade["approval_status"]="pending"
                blade["submitted_by"]=i.user.id
                blade["submitted_guild_id"]=i.guild.id
                profile["custom_bey"]=blade
            await mutate_user(i.user.id,save)
        except (CustomBeyError,InventoryFull) as exc:
            return await i.followup.send(f"❌ {exc}",ephemeral=True)
        posted=False
        try:
            channel_id=get_custom_review_channel(i.guild.id)
            channel=i.client.get_channel(channel_id) or await i.client.fetch_channel(channel_id)
            if channel:
                await channel.send(view=CustomApprovalView(0,i.user.id).display(_approval_embed(i.user.id,blade)))
                posted=True
        except discord.HTTPException: pass
        note="Submitted for approval." if posted else "Saved as pending, but the review channel could not be reached. Ask the owner to run ;setcustom again."
        self.stop(); await i.edit_original_response(view=CustomBeyView(i.user.id).display(_summary(blade),content=note))

class ImageChoiceView(ModernPanel):
    def __init__(self,builder):
        super().__init__(timeout=120); self.builder=builder
        self.control("Image URL",self.url)
        self.control("Upload Image",self.upload,style=discord.ButtonStyle.primary)
        self.display(content="## Custom Bey Photo\nUpload or link a background-removed transparent PNG/WebP. Maximum 8 MB / 16 megapixels.")
    async def interaction_check(self,i):
        if i.user.id!=self.builder.owner_id:
            await i.response.send_message("❌ This image setup belongs to another player.",ephemeral=True); return False
        return True
    async def url(self,i): await i.response.send_modal(BuilderTextModal(self.builder,"image_url"))
    async def upload(self,i):
        await i.response.send_message("📤 Upload **one image** in this channel within 60 seconds. I will use its Discord attachment URL.",ephemeral=True)
        def check(m): return m.author.id==i.user.id and m.channel.id==i.channel.id and bool(m.attachments)
        try: msg=await self.builder._client.wait_for("message",timeout=60,check=check)
        except Exception: return await i.followup.send("❌ Image upload timed out. Press Image and try again.",ephemeral=True)
        att=msg.attachments[0]
        if not (att.content_type or "").startswith("image/"):
            return await i.followup.send("❌ That attachment is not an image.",ephemeral=True)
        if att.size > 8*1024*1024:
            return await i.followup.send("Image must be at most 8 MB.",ephemeral=True)
        try: await validate_image_bytes(await att.read())
        except CustomBeyError as exc: return await i.followup.send(str(exc),ephemeral=True)
        self.builder.draft["image"]=att.url
        self.builder.rebuild()
        if getattr(self.builder,"_message",None):
            try: await self.builder._message.edit(view=self.builder.display())
            except Exception: pass
        await i.followup.send("✅ Image uploaded. Return to the builder and continue.",ephemeral=True)


class RejectReasonModal(discord.ui.Modal,title="Reject Custom Bey"):
    reason=discord.ui.TextInput(label="Rejection reason",style=discord.TextStyle.paragraph,max_length=500)
    def __init__(self,owner_id,target_id):
        super().__init__(); self.owner_id=owner_id; self.target_id=target_id
    async def on_submit(self,interaction):
        if interaction.user.id!=self.owner_id: return await interaction.response.send_message("❌ Not your review panel.",ephemeral=True)
        await interaction.response.defer()
        reason=str(self.reason).strip()
        def reject(profile):
            blade=profile.get("custom_bey")
            if not isinstance(blade,dict) or blade.get("approval_status")!="pending": raise CustomBeyError("Submission is no longer pending.")
            blade["approval_status"]="rejected"; blade["rejection_reason"]=reason
            return blade.get("name","Custom Bey")
        try: name=await mutate_user(self.target_id,reject)
        except CustomBeyError as exc: return await interaction.followup.send(f"❌ {exc}",ephemeral=True)
        try:
            user=interaction.client.get_user(self.target_id) or await interaction.client.fetch_user(self.target_id)
            await user.send(f"❌ Your Custom Bey **{name}** was rejected.\n**Reason:** {reason}\nDelete it, fix it, and submit again.")
        except Exception: pass
        await interaction.edit_original_response(view=ModernPanel().display(content=f"❌ **{name}** rejected.\nReason: {reason}"))

class CustomApprovalView(ModernPanel):
    def __init__(self,owner_id,target_id):
        super().__init__(timeout=None); self.owner_id=owner_id; self.target_id=target_id
        self.control("Approve",self.approve,style=discord.ButtonStyle.success,custom_id=f"custom:approve:{target_id}")
        self.control("Reject",self.reject,style=discord.ButtonStyle.danger,custom_id=f"custom:reject:{target_id}")
    async def interaction_check(self,i):
        if not await i.client.is_owner(i.user):
            await i.response.send_message("Only the bot owner can review Custom Beys.",ephemeral=True); return False
        return True
    async def approve(self,i):
        await i.response.defer()
        def approve_profile(profile):
            blade=profile.get("custom_bey")
            if not isinstance(blade,dict) or blade.get("approval_status")!="pending": raise CustomBeyError("Submission is no longer pending.")
            require_room(profile,1,blade["name"])
            inv=profile.setdefault("inventory",[])
            if blade["name"] not in inv: inv.append(blade["name"])
            BL.entry_for(profile,blade["name"])
            blade["approval_status"]="approved"; blade.pop("rejection_reason",None)
            return blade["name"]
        try: name=await mutate_user(self.target_id,approve_profile)
        except (CustomBeyError,InventoryFull) as exc: return await i.followup.send(f"❌ {exc}",ephemeral=True)
        try:
            user=i.client.get_user(self.target_id) or await i.client.fetch_user(self.target_id)
            await user.send(f"✅ Your Custom Bey **{name}** was approved! It is now in your inventory and starts at Level 1.")
        except Exception: pass
        self.stop(); panel=ModernPanel().display(content=f"✅ **{name}** approved and added to <@{self.target_id}>'s inventory."); await i.edit_original_response(view=panel)
    async def reject(self,i): await i.response.send_modal(RejectReasonModal(i.user.id,self.target_id))

async def _pending_custom_submissions():
    rows=[]
    for uid in all_user_ids():
        profile=await get_user(uid)
        blade=profile.get("custom_bey")
        if isinstance(blade,dict) and blade.get("approval_status")=="pending":
            rows.append((uid,blade))
    return rows

def _approval_embed(uid,blade):
    meta=blade.get("custom_meta",{}); stats=blade["stats"]
    abilities=[f"• **{a.get('name','Ability')}** — {a.get('description','')} — trigger `{a.get('trigger','?')}`" for a in blade.get("abilities",[]) if not a.get("_custom_special_effect")]
    e=discord.Embed(title=f"📝 Custom Bey Approval • {blade['name']}",
        description=f"Player: <@{uid}> • ID: `{uid}`\nType: **{blade['type']}**\nStatus: **Pending**\nAbility cost: **{meta.get('ability_cost',0)}/{ABILITY_BUDGET}**",
        colour=0xF1C40F)
    e.add_field(name="Stats",value=f"HP {stats['hp']} • ATK {stats['attack']} • DEF {stats['defense']} • STM {stats['stamina']}",inline=False)
    if blade.get("submitted_guild_id"):
        e.add_field(name="Submitted in server",value=str(blade["submitted_guild_id"]),inline=False)
    e.add_field(name="Abilities",value="\n".join(abilities) or "None",inline=False)
    sm=blade["special_move"]; e.add_field(name="Special",value=f"**{sm['name']}** • {sm['damage_per_hit']} damage\n{sm.get('description','')}",inline=False)
    if blade.get("image_url"): e.set_image(url=blade["image_url"])
    return e

class CustomBeyCog(commands.Cog):
    def __init__(self,bot): self.bot=bot

    @commands.command(name="setcustom",hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def setcustom(self,ctx,channel: discord.TextChannel | None = None):
        channel = channel or ctx.channel
        if channel.guild.id != ctx.guild.id:
            return await ctx.send("Choose a channel in this server.")
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await ctx.send("I need View Channel, Send Messages and Embed Links in the review channel.")
        set_custom_review_channel(ctx.guild.id,channel.id)
        pending=await _pending_custom_submissions()
        await ctx.send(view=ModernPanel().display(content=f"## Custom Bey Review Channel\nSaved {channel.mention}. Submissions from every server will appear here.\n**{len(pending)} pending submissions**"))
        for uid,blade in pending:
            await channel.send(view=CustomApprovalView(0,uid).display(_approval_embed(uid,blade)))

    @setcustom.error
    async def setcustom_error(self,ctx,error):
        if isinstance(error,commands.NotOwner):
            await ctx.send("Only the bot owner can set the Custom Bey review channel.")
        elif isinstance(error,commands.NoPrivateMessage):
            await ctx.send("Run ;setcustom in your server's review channel.")
        elif isinstance(error,commands.BadArgument):
            await ctx.send("Use ;setcustom #channel, or ;setcustom in the channel you want to save.")
        else:
            raise error

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
            if not interaction.guild or not get_custom_review_channel(interaction.guild.id):
                return await interaction.response.send_message("Ask the bot owner to run ;setcustom in the approval server first.",ephemeral=True)
            if bey_type is None: return await interaction.response.send_message("❌ Choose a bey_type when creating your Bey.",ephemeral=True)
            if (await get_user(interaction.user.id)).get("custom_bey"):
                return await interaction.response.send_message("❌ You already own a Custom Bey. View or delete it first.",ephemeral=True)
            builder=CustomBeyBuilder(interaction.user.id,bey_type.value); builder._client=self.bot
            await interaction.response.send_message(view=builder.display(),ephemeral=True)
            try:
                builder._message=await interaction.original_response()
            except Exception:
                builder._message=None
            return
        if act=="rules":
            return await interaction.response.send_message(view=ModernPanel().display(content=
                f"## Custom Bey Rules\n"
                f"• HP + ATK + DEF + STM = **{STAT_TOTAL}**; minimum **20** each.\n"
                f"• Up to **2 different abilities** using **{ABILITY_BUDGET} ability points**.\n"
                f"• **{len(ABILITY_PRESETS)} effects**; costs appear in the Ability Library.\n"
                "• Select one compatible trigger per ability. Abilities have a 3-round cooldown. Revival arms once per battle.\n"
                "• Special base damage **80–140**; secondary effect: none, heal, shield.\n"
                "• Photo must be a **background-removed transparent PNG/WebP**, at most **8 MB / 16 megapixels**.\n"
                "• One Custom Bey per player. Owner approval required; approved Beys enter inventory at Level 1.\n"
                "• Create in any server; approval requests go to the central review channel.\n"
                "• Owner: use **;setcustom #channel** to save that channel for all servers."),ephemeral=True)
        profile=await get_user(interaction.user.id); blade=profile.get("custom_bey")
        if not isinstance(blade,dict): return await interaction.response.send_message("❌ You don't have a Custom Bey yet. Use /custombey action:create.",ephemeral=True)
        if act=="view": return await interaction.response.send_message(view=CustomBeyView(interaction.user.id).display(_summary(blade)),ephemeral=True)
        if act=="equip":
            if blade.get("approval_status") != "approved":
                return await interaction.response.send_message("❌ Your Custom Bey is not approved yet.",ephemeral=True)
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
                progress=prof.get("bey_progress")
                if isinstance(progress,dict): progress.pop(name,None)
                return name
            try: name=await mutate_user(interaction.user.id,delete)
            except CustomBeyError as exc: return await interaction.response.send_message(f"❌ {exc}",ephemeral=True)
            return await interaction.response.send_message(f"🗑️ Custom Bey **{name}** deleted.",ephemeral=True)

async def setup(bot):
    await bot.add_cog(CustomBeyCog(bot))
    for uid, blade in await _pending_custom_submissions():
        bot.add_view(CustomApprovalView(0,uid).display(_approval_embed(uid,blade)))
