"""Admin panel and recovery/admin utility commands."""
from __future__ import annotations

import json
import logging
import os
import re
import discord
from discord import app_commands
from discord.ext import commands

from ..ui import panel_kit as K
from . import actions as A

log = logging.getLogger("beyblade_bot.admin")
PANEL_TIMEOUT = 300
COLOUR = 0x5865F2
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_ASSET_BYTES = 8 * 1024 * 1024


def _asset_paths():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "data", "beyblades.json"), os.path.join(root, "assets", "beys")


def _asset_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _safe_asset_stem(name: str) -> str:
    # Keep human-readable names while preventing path traversal / invalid filename chars.
    stem = re.sub(r'[\\/:*?"<>|]+', "_", str(name)).strip().strip(".")
    return stem or "bey"


def _load_asset_state():
    roster_path, assets_dir = _asset_paths()
    with open(roster_path, encoding="utf-8") as fh:
        roster = json.load(fh)
    os.makedirs(assets_dir, exist_ok=True)
    local = set()
    for filename in os.listdir(assets_dir):
        stem, ext = os.path.splitext(filename)
        if ext.lower() in IMAGE_EXTS:
            local.add(_asset_key(stem))
    missing = []
    for roster_key, data in roster.items():
        name = (data or {}).get("name") or roster_key
        if _asset_key(name) not in local and _asset_key(roster_key) not in local:
            missing.append(str(name))
    missing.sort(key=str.casefold)
    return roster, assets_dir, missing


async def _send(interaction: discord.Interaction, result: A.Result) -> None:
    embeds = list(result.embeds) if result.embeds else ([result.embed] if result.embed is not None else [])
    kwargs: dict = {}
    if embeds:
        kwargs["embeds"] = embeds[:10]
    if result.file is not None:
        kwargs["file"] = result.file
    if result.view is not None:
        kwargs["view"] = result.view
    content = result.message or ("" if embeds else "Done.")
    if content:
        kwargs["content"] = content
    if interaction.response.is_done():
        await interaction.followup.send(ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(ephemeral=True, **kwargs)
    for chunk in (embeds[i:i + 10] for i in range(10, len(embeds), 10)):
        await interaction.followup.send(embeds=chunk, ephemeral=True)


class AdminSpec(K.PanelSpec):
    title = "🛠️  Admin"
    colour = COLOUR
    timeout = PANEL_TIMEOUT
    placeholder = "Pick an action…"

    def may_open(self, user) -> bool:
        return A.is_admin(user)

    def categories(self, user):
        return A.categories_for(user)

    def actions(self, category: str, user):
        return [_as_panel_action(a) for a in A.actions_in(category, user)]

    def intro(self, panel) -> str:
        n = sum(len(A.actions_in(k, panel.invoker)) for k in A.CATEGORY_ORDER)
        return f"-# {n} action(s) available to you"

    async def execute(self, action, panel, interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        ctx = A.ActionCtx(bot=panel.bot, guild=panel.guild, invoker=panel.invoker,
                          invoker_id=getattr(panel.invoker, "id", 0), target=panel.target,
                          target_id=panel.target_id, amount=panel.amount, text=panel.text,
                          channel=panel.channel, guild_choice=panel.guild_choice)
        result = await A.run(action.key, ctx)
        log.info("[admin] %s ran %s → %s", getattr(panel.invoker, "id", "?"), action.key,
                 "ok" if result.ok else "refused")
        await _send(interaction, result)


def _as_panel_action(a: A.Action) -> K.PanelAction:
    return K.PanelAction(key=a.key, label=a.label, description=a.description,
                         category=a.category, needs=a.needs, confirm=a.confirm,
                         long_text=(a.category == "announce"),
                         draft=(A.update_note if a.key == "announce_update" else None))


class AdminPanel(K.PanelView):
    def __init__(self, bot, invoker, guild, channel):
        super().__init__(AdminSpec(), bot, invoker, guild, channel)


class MissingBeySelect(discord.ui.Select):
    def __init__(self, names: list[str]):
        options = [discord.SelectOption(label=n[:100], value=n) for n in names[:25]]
        super().__init__(placeholder="Choose a missing Bey…", min_values=1, max_values=1,
                         options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: AssetUploadView = self.view
        view.selected_bey = self.values[0]
        await interaction.response.edit_message(content=view.content(), view=view)


class AssetUploadView(discord.ui.View):
    """Interactive missing-art picker. Upload uses Discord's native attachment option."""
    def __init__(self, owner_id: int, total: int, missing: list[str]):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.total = total
        self.missing = missing
        self.selected_bey = missing[0] if len(missing) == 1 else None
        if missing:
            self.add_item(MissingBeySelect(missing))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or not A.is_admin(interaction.user):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return False
        return True

    def content(self) -> str:
        found = self.total - len(self.missing)
        selected = f"\nSelected: **{self.selected_bey}**" if self.selected_bey else "\nChoose a Bey above, then press **Upload Photo**."
        preview = "\n".join(f"• {n}" for n in self.missing[:15])
        more = f"\n…and **{len(self.missing)-15}** more." if len(self.missing) > 15 else ""
        return (f"🖼️ **Bey Asset Check**\nDatabase: **{self.total}** · Found: **{found}** · "
                f"Missing: **{len(self.missing)}**{selected}\n\n{preview}{more}")

    @discord.ui.button(label="Upload Photo", emoji="📤", style=discord.ButtonStyle.primary, row=1)
    async def upload(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_bey:
            return await interaction.response.send_message("Choose a missing Bey first.", ephemeral=True)
        await interaction.response.send_modal(AssetUploadModal(self, self.selected_bey))

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            roster, _, missing = _load_asset_state()
        except (OSError, json.JSONDecodeError) as exc:
            return await interaction.response.send_message(f"⚠️ Asset check failed: `{type(exc).__name__}`", ephemeral=True)
        self.total = len(roster)
        self.missing = missing
        self.selected_bey = None
        self.clear_items()
        if missing:
            self.add_item(MissingBeySelect(missing))
            self.add_item(self.upload)
            self.add_item(self.refresh)
            await interaction.response.edit_message(content=self.content(), view=self)
        else:
            await interaction.response.edit_message(content=f"✅ **Bey Asset Check**\nAll **{self.total}** database Beys now have local artwork.", view=None)


class AssetUploadModal(discord.ui.Modal, title="Upload Bey Photo"):
    # discord.py supports FileUpload in modals on versions exposing this component.
    def __init__(self, parent: AssetUploadView, bey_name: str):
        super().__init__(timeout=180)
        self.parent_view = parent
        self.bey_name = bey_name
        file_upload_cls = getattr(discord.ui, "FileUpload", None)
        if file_upload_cls is None:
            self.photo = None
            self.url = discord.ui.TextInput(label="Image URL", placeholder="Paste a direct PNG/JPG/WEBP URL", required=True)
            self.add_item(self.url)
        else:
            self.photo = file_upload_cls(label="Bey image", required=True, min_values=1, max_values=1)
            self.add_item(self.photo)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.owner_id or not A.is_admin(interaction.user):
            return await interaction.response.send_message("Not authorized.", ephemeral=True)
        if self.photo is None:
            return await interaction.response.send_message(
                "⚠️ This discord.py build does not support attachment uploads in modals yet. Update discord.py to a build with `discord.ui.FileUpload`.", ephemeral=True)
        attachment = self.photo.values[0]
        ext = os.path.splitext(attachment.filename or "")[1].lower()
        if ext not in IMAGE_EXTS:
            return await interaction.response.send_message("❌ Use PNG, JPG, JPEG, or WEBP.", ephemeral=True)
        if attachment.size and attachment.size > MAX_ASSET_BYTES:
            return await interaction.response.send_message("❌ Image is too large. Maximum size is 8 MB.", ephemeral=True)
        if attachment.content_type and not attachment.content_type.startswith("image/"):
            return await interaction.response.send_message("❌ The uploaded file must be an image.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _, assets_dir, _ = _load_asset_state()
            destination = os.path.join(assets_dir, _safe_asset_stem(self.bey_name) + ext)
            temp = destination + ".uploading"
            await attachment.save(temp)
            os.replace(temp, destination)
            roster, _, missing = _load_asset_state()
        except Exception as exc:
            log.exception("Failed to save Bey asset for %s", self.bey_name)
            return await interaction.followup.send(f"❌ Upload failed: `{type(exc).__name__}`", ephemeral=True)
        self.parent_view.total = len(roster)
        self.parent_view.missing = missing
        self.parent_view.selected_bey = None
        await interaction.followup.send(
            f"✅ **{self.bey_name}** artwork uploaded to `assets/beys/`.\n"
            f"Missing assets remaining: **{len(missing)}**.\nRun `/checkbeyassets` again or press **Refresh** on the checker.",
            ephemeral=True)


class AdminCog(commands.Cog, name="Admin"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        return A.is_admin(ctx.author)

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, (commands.CheckFailure, commands.MissingRequiredArgument, commands.BadArgument)):
            return
        log.error("[admin] %s", error, exc_info=error)

    @app_commands.command(name="admin", description="[Admin] Every admin action, in one panel")
    async def admin(self, interaction: discord.Interaction) -> None:
        if not A.is_admin(interaction.user):
            return await interaction.response.send_message("Not authorized.", ephemeral=True)
        panel = AdminPanel(self.bot, interaction.user, interaction.guild, interaction.channel)
        await interaction.response.send_message(embed=panel.embed(), view=panel, ephemeral=True)

    @app_commands.command(name="checkbeyassets", description="[Admin] Find and upload missing Bey artwork")
    async def check_bey_assets(self, interaction: discord.Interaction) -> None:
        if not A.is_admin(interaction.user):
            return await interaction.response.send_message("Not authorized.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            roster, _, missing = _load_asset_state()
        except (OSError, json.JSONDecodeError) as exc:
            return await interaction.followup.send(f"⚠️ Could not read Bey assets: `{type(exc).__name__}`", ephemeral=True)
        total = len(roster)
        if not missing:
            return await interaction.followup.send(
                f"✅ **Bey Asset Check**\nAll **{total}** database Beys have a local asset in `assets/beys/`.", ephemeral=True)
        view = AssetUploadView(interaction.user.id, total, missing)
        await interaction.followup.send(view.content(), view=view, ephemeral=True)

    @commands.command(name="sync", hidden=True)
    async def sync(self, ctx: commands.Context, scope: str = "guild") -> None:
        res = await A.do_sync(self.bot, ctx.guild, scope)
        await ctx.send(res.message or "Done.")

    @commands.command(name="reload", hidden=True)
    async def reload_cogs(self, ctx: commands.Context) -> None:
        res = await A.do_reload(self.bot)
        await ctx.send(content=res.message or None, embed=res.embed)

    @commands.command(name="version", aliases=["build", "ver"], hidden=True)
    async def version(self, ctx: commands.Context) -> None:
        res = await A.do_version(self.bot)
        await ctx.send(content=res.message or None, embed=res.embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
