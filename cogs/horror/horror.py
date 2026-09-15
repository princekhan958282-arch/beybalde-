"""Horror Story: owner encounters plus automatic server-wide encounters."""
from __future__ import annotations
import asyncio, logging, random
import discord
from discord.ext import commands, tasks
from cogs.admin.actions import MASTER_ID
from utils import horror_state
log=logging.getLogger("beyblade_bot.horror")
HORROR_IMAGE_URL="https://cdn.discordapp.com/attachments/1520321120123748432/1549073218004975716/8619f5904b67ec3c915a5c8563e1e64e_1.jpg?ex=6aa95e5b&is=6aa80cdb&hm=3666ae337cf73f0f0953a3c3aa1ef532ecb6e9f12dcec8d5705031e9937f4e98&"
PROMPT_TIMEOUT=120.0;AUTO_SPAWN_CHECK_SECONDS=15*60;AUTO_SPAWN_CHANCE_PER_GUILD=0.30
class TargetOnlyView(discord.ui.View):
    def __init__(self,cog,target_id,*,timeout=PROMPT_TIMEOUT):super().__init__(timeout=timeout);self.cog=cog;self.target_id=int(target_id);self.message=None
    async def interaction_check(self,interaction):
        if interaction.user.id==self.target_id:return True
        await interaction.response.send_message("❌ This challenge isn't for you.",ephemeral=True);return False
    async def on_timeout(self):await self.cog.apply_unknown_curse(self.target_id,self.message)
class ForcedBattleView(TargetOnlyView):
    @discord.ui.button(label="BATTLE",emoji="⚔️",style=discord.ButtonStyle.danger)
    async def battle(self,interaction,_button):await self.cog.accept_battle(interaction,self.target_id,self)
class HorrorChallengeView(TargetOnlyView):
    @discord.ui.button(label="BATTLE",emoji="⚔️",style=discord.ButtonStyle.danger)
    async def battle(self,interaction,_button):await self.cog.accept_battle(interaction,self.target_id,self)
    @discord.ui.button(label="NO",emoji="❌",style=discord.ButtonStyle.secondary)
    async def no(self,interaction,_button):
        horror_state.save_encounter(self.target_id,status="declined_once",declined=True);forced=ForcedBattleView(self.cog,self.target_id);forced.message=interaction.message;self.stop();await interaction.response.edit_message(content=f"<@{self.target_id}>\n**UNKNOWN:** battle me.",view=forced)
class HorrorCog(commands.Cog,name="Horror Story"):
    def __init__(self,bot):self.bot=bot;self._curse_locks={};self.auto_horror_spawn.start();self.horror_cleanup.start()
    def cog_unload(self):self.auto_horror_spawn.cancel();self.horror_cleanup.cancel()
    def _guild_has_active_encounter(self,guild):
        for member in guild.members:
            row=horror_state.encounter(member.id)
            if int(row.get("guild_id") or 0)==guild.id and row.get("status") in {"spawned","declined_once","battle_requested","battle_running"}:return True
        return False
    @commands.command(name="settings",hidden=True)
    async def settings(self,ctx):
        if ctx.author.id!=MASTER_ID:return
        await ctx.send("👁️ **Horror Story** active. Auto-spawn: **30% per server per check**. Only **one UNKNOWN encounter/battle can run per server**, and after a battle UNKNOWN is locked for that server until **12:00 AM IST**.")
    async def spawn_encounter(self,guild,channel,target):
        if target.guild.id!=guild.id or channel.guild.id!=guild.id:return False,"❌ Target mismatch."
        if horror_state.guild_battle_used_today(guild.id):return False,"❌ UNKNOWN already battled in this server today. Resets at 12:00 AM IST."
        if self._guild_has_active_encounter(guild):return False,"❌ UNKNOWN already has an active encounter in this server."
        me=guild.me
        if me:
            p=channel.permissions_for(me)
            if not(p.view_channel and p.send_messages and p.embed_links):return False,"❌ Beycord cannot send there."
        previous=horror_state.encounter(target.id)
        if previous.get("status") in {"spawned","declined_once","battle_requested","battle_running"}:return False,"❌ Player already has an unresolved Horror encounter."
        embed=discord.Embed(color=0x050505);embed.set_image(url=HORROR_IMAGE_URL);view=HorrorChallengeView(self,target.id)
        msg=await channel.send(content=f"{target.mention}\n**UNKNOWN:** hey u wanna battle me?",embed=embed,view=view,allowed_mentions=discord.AllowedMentions(users=True,roles=False,everyone=False));view.message=msg
        horror_state.save_encounter(target.id,status="spawned",declined=False,cursed=horror_state.is_cursed(target.id),guild_id=guild.id,channel_id=channel.id,message_id=msg.id,target_user_id=target.id);return True,f"👁️ Spawned for {target.mention} in {channel.mention}."
    async def accept_battle(self,interaction,target_id,view):
        guild=getattr(interaction,"guild",None)
        if guild is None:return await interaction.response.send_message("❌ Horror battles are server-only.",ephemeral=True)
        bey_name=None;copy_id=None
        try:
            from cogs.battle.boss import boss_copy as bcopy
            blade,copy=await bcopy.equipped_blade(target_id)
            if blade:bey_name=blade.get("name");copy_id=str((copy or {}).get("id") or "") or None
        except Exception as exc:log.warning("[horror] equipped lookup failed: %s",exc)
        if not bey_name:return await interaction.response.send_message("❌ Equip a Beyblade first.",ephemeral=True)
        if not horror_state.claim_guild_daily_battle(guild.id,target_id):view.stop();return await interaction.response.edit_message(content=f"<@{target_id}>\n**UNKNOWN:** enough for today. I return after 12:00 AM IST.",view=None)
        horror_state.save_encounter(target_id,status="battle_requested",battle_started=True,equipped_bey=bey_name,equipped_copy_id=copy_id,guild_id=guild.id);view.stop();await interaction.response.edit_message(content=f"<@{target_id}>\n**UNKNOWN:** good.",view=None);self.bot.dispatch("horror_battle_requested",interaction.channel,interaction.user,bey_name,copy_id)
    async def apply_unknown_curse(self,target_id,message):
        lock=self._curse_locks.setdefault(int(target_id),asyncio.Lock())
        async with lock:
            row=horror_state.encounter(target_id)
            if row.get("status") in {"battle_requested","battle_running","completed"}:return
            curse=horror_state.apply_curse(target_id);horror_state.save_encounter(target_id,status="cursed",cursed=True,curse_expires_at=curse["expires_at"])
            if message:
                try:await message.edit(view=None)
                except discord.HTTPException:pass
                try:await message.channel.send(f"<@{target_id}>\n👁️ **CURSED**\n**UNKNOWN:** you should have battled me.\n\nAll combat stats are reduced by **20% for 24 hours**.")
                except discord.HTTPException:pass
    @tasks.loop(seconds=AUTO_SPAWN_CHECK_SECONDS)
    async def auto_horror_spawn(self):
        for guild in list(self.bot.guilds):
            if horror_state.guild_battle_used_today(guild.id) or self._guild_has_active_encounter(guild):continue
            if random.random()>=AUTO_SPAWN_CHANCE_PER_GUILD:continue
            members=[m for m in guild.members if not m.bot and not horror_state.is_cursed(m.id) and horror_state.encounter(m.id).get("status") not in {"spawned","declined_once","battle_requested","battle_running"}]
            if not members:continue
            me=guild.me;channels=[c for c in guild.text_channels if me and c.permissions_for(me).view_channel and c.permissions_for(me).send_messages and c.permissions_for(me).embed_links]
            if not channels:continue
            try:await self.spawn_encounter(guild,random.choice(channels),random.choice(members))
            except discord.HTTPException as exc:log.warning("[horror] auto spawn failed guild=%s: %s",guild.id,exc)
    @auto_horror_spawn.before_loop
    async def before_auto_horror_spawn(self):await self.bot.wait_until_ready()
    @tasks.loop(minutes=5)
    async def horror_cleanup(self):
        for guild in list(self.bot.guilds):
            for member in guild.members:horror_state.curse_multiplier(member.id);horror_state.encounter(member.id)
        for token,row in horror_state.due_restorations():self.bot.dispatch("horror_restore_due",token,row)
    @horror_cleanup.before_loop
    async def before_horror_cleanup(self):await self.bot.wait_until_ready()
async def setup(bot):await bot.add_cog(HorrorCog(bot))
