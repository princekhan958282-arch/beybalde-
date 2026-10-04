"""Pillow ;ainfo card for the supplied metallic frame; no game-state writes."""
from __future__ import annotations
import io
import asyncio
import time
from urllib.parse import urlparse, parse_qs
import discord
import logging
import os
import re
import math
import unicodedata
import urllib.request
from PIL import Image, ImageDraw, ImageOps
from utils.image_generator import _font, _text_w
from cogs.avatar.avatar_utils import is_renderable_image, format_bonuses_summary
from cogs.avatar import avatar_levels as AL, avatar_skills as AS
from cogs.avatar.avatar_progress import slugify

log = logging.getLogger(__name__)
FRAME_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'assets', 'ui', 'ainfo_frame.png')
SIZE = (1536, 864)
ART_BOX = (83, 94, 507, 683)
_FRAME = None
_ART = {}
_RESOLVED_URLS = {}


def clear_cache():
    global _FRAME
    _FRAME = None
    _ART.clear()
    _RESOLVED_URLS.clear()


async def resolve_avatar_image_url(bot, source):
    """Recover unsigned/expired Discord attachments using readable message history.

    Attachment IDs and their containing message share a snowflake creation time;
    a bounded history window locates the original attachment without guessing a
    message ID or modifying the catalog. Valid signed URLs and local art pass through.
    """
    if not isinstance(source, str):
        return source
    parsed = urlparse(source)
    if parsed.hostname not in {'cdn.discordapp.com', 'media.discordapp.net'}:
        return source
    parts = parsed.path.split('/')
    if len(parts) < 5 or parts[1] != 'attachments':
        return source
    query = parse_qs(parsed.query)
    try:
        if query.get('hm') and int(query.get('ex', ['0'])[0], 16) > time.time()+60:
            return source
    except ValueError:
        pass
    hit = _RESOLVED_URLS.get(source)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        channel_id, attachment_id = int(parts[2]), int(parts[3])
        async def find():
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            async for message in channel.history(limit=20, around=discord.Object(id=attachment_id)):
                for attachment in message.attachments:
                    if attachment.id == attachment_id:
                        return attachment.url
            return source
        resolved = await asyncio.wait_for(find(), timeout=5)
        if len(_RESOLVED_URLS) >= 64:
            _RESOLVED_URLS.clear()
        # Also throttle inaccessible/deleted sources rather than repeating reads.
        _RESOLVED_URLS[source] = (time.monotonic()+300, resolved)
        return resolved
    except Exception:
        log.debug('Could not recover Discord avatar attachment', exc_info=True)
        _RESOLVED_URLS[source] = (time.monotonic()+60, source)
        return source


def _art(source):
    if not isinstance(source, str) or not source or not is_renderable_image(source):
        return None
    if source in _ART:
        return _ART[source].copy()
    try:
        if source.startswith(('http://', 'https://')):
            req = urllib.request.Request(source, headers={'User-Agent': 'Beycord/1.0'})
            with urllib.request.urlopen(req, timeout=5) as response:
                data = io.BytesIO(response.read(4*1024*1024))
        else:
            data = source if os.path.isabs(source) else os.path.join(os.path.dirname(os.path.dirname(__file__)), source)
        with Image.open(data) as src:
            image = ImageOps.exif_transpose(src).convert('RGBA')
        image = ImageOps.contain(image, (ART_BOX[2]-ART_BOX[0], ART_BOX[3]-ART_BOX[1]), Image.LANCZOS)
        if len(_ART) >= 32:
            _ART.clear()
        _ART[source] = image
        return image.copy()
    except Exception:
        log.debug('Avatar artwork unavailable', exc_info=True)
        return None


def _number(value):
    try:
        number = float(value or 0)
        return number if math.isfinite(number) else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def permanent_bonuses(avatar, owned=False, equipped=False, level=1):
    """Match compact avatar info: signature effects are conditional, not passive."""
    raw = avatar.get('bonuses')
    out = dict(raw) if isinstance(raw, dict) else {}
    if avatar.get('skills') and not avatar.get('stats_always_on'):
        out = {k:v for k,v in out.items() if k in AS.CARD_LEVEL_BONUS_KEYS}
    if owned or equipped:
        gain = AL.card_stat_bonus(avatar.get('type'), level)
        for stat, key in [('attack','attack_flat'),('defense','defence_flat'),('stamina','stamina_flat')]:
            out[key] = _number(out.get(key)) + gain[stat]
    return out


def render_avatar_info_card(avatar, *, owned=False, equipped=False, level=1,
                            skill_levels=None, active_skill_slot=0):
    """JPEG buffer or None. All network/image work belongs on a worker thread."""
    global _FRAME
    try:
        if _FRAME is None:
            with Image.open(FRAME_PATH) as source:
                _FRAME = source.convert('RGBA').resize(SIZE, Image.LANCZOS)
        img = _FRAME.copy()
        draw = ImageDraw.Draw(img)
        cyan, white, dim, gold = '#22beee', '#eef3fa', '#9babc0', '#ffce52'
        def text(value, box, size=24, color=white):
            value = unicodedata.normalize('NFKC', str(value))
            value = ''.join(c for c in value if c in '+%' or (unicodedata.category(c)[0] in 'LNPZ' and ord(c) < 0x2500))
            x,y,w = box
            f = _font(size)
            while f.size > 13 and _text_w(draw,value,f) > w:
                f = _font(f.size-1)
            while value and _text_w(draw,value,f) > w:
                value = value[:-2]+'…' if len(value)>2 else ''
            draw.text((x,y), value, font=f, fill=color)
        def paragraph(value, box, max_lines=2, size=21):
            x,y,w = box
            clean = re.sub(r'\*\*|`', '', str(value or ''))
            words = clean.split()
            lines, line = [], ''
            font = _font(size)
            for word in words:
                candidate = (line+' '+word).strip()
                if line and _text_w(draw,candidate,font)>w:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            if line: lines.append(line)
            for i,line in enumerate(lines[:max_lines]):
                if i==max_lines-1 and len(lines)>max_lines: line += '…'
                text(line,(x,y+i*(size+5),w),size)
        art = _art(avatar.get('image'))
        if art is not None:
            img.alpha_composite(art, ((ART_BOX[0]+ART_BOX[2]-art.width)//2,
                                      (ART_BOX[1]+ART_BOX[3]-art.height)//2))
        else:
            text((str(avatar.get('name') or '?'))[0].upper(), (239,300,130),120,cyan)
            text('ART UNAVAILABLE',(145,625,310),20,dim)
        text(avatar.get('name') or 'Avatar',(624,88,810),52)
        state = 'EQUIPPED' if equipped else 'OWNED' if owned else 'NOT OWNED'
        text(state,(1230,137,210),18,gold)
        lvl = AL.clamp_level(level)
        text(str(avatar.get('rarity') or 'Common').upper(),(680,217,160),23,cyan)
        text(str(avatar.get('type') or 'Balance').upper(),(986,217,174),23,cyan)
        text(str(lvl) if owned or equipped else '—',(1310,217,140),23,cyan)
        bonuses = permanent_bonuses(avatar,owned,equipped,lvl)
        for x, key in [(680,'attack'),(912,'defence'),(1140,'stamina'),(1370,'hp')]:
            flat, pct = _number(bonuses.get(key+'_flat')), _number(bonuses.get(key+'_percent'))
            parts = ([f'{flat:+g}'] if flat else []) + ([f'{pct*100:+g}%'] if pct else [])
            if len(parts) == 2:
                text(parts[0],(x,316,105),25,cyan)
                text(parts[1],(x,341,105),20,cyan)
            else:
                text(parts[0] if parts else '—',(x,325,105),28,cyan)
        skills = avatar.get('skills')
        skills = [s for s in skills if isinstance(s,dict)][:3] if isinstance(skills,list) else []
        # Main stat bonuses are already visible in the four tiles. Only show
        # additional permanent effects here, never the union of skill effects.
        main_keys = {k+'_'+suffix for k in ('attack','defence','stamina','hp') for suffix in ('flat','percent')}
        extra = {k:v for k,v in bonuses.items() if k not in main_keys}
        summary = format_bonuses_summary(extra)
        if not any(extra.values()):
            summary = 'Permanent bonuses shown above'
        paragraph(summary,(621,460,818),2,23)
        if skills:
            text('Select one signature skill',(621,515,815),19,dim)
        levels = skill_levels if isinstance(skill_levels,dict) else {}
        for i,y in enumerate([644,699,754]):
            if i<len(skills):
                skill = skills[i]
                selected = active_skill_slot==i+1 and (owned or equipped)
                if selected:
                    draw.rectangle((632,y-8,1252,y+36),outline=cyan,width=2)
                text(skill.get('name') or 'Skill',(651,y,586),27,cyan if selected else white)
                try:
                    sk_level = max(1,min(AL.MAX_SKILL_LEVEL,int(levels.get(slugify(skill.get('name') or ''),1) or 1)))
                except (TypeError,ValueError,OverflowError):
                    sk_level = 1
                text(str(sk_level) if owned or equipped else '—',(1292,y,68),26,cyan)
                text(str(AS.skill_cost(i+1)),(1400,y,67),26,cyan)
            elif i==0 and not skills:
                text('No signature skills',(653,y,565),23,dim)
        buf=io.BytesIO()
        img.convert('RGB').save(buf,'JPEG',quality=90,optimize=True)
        buf.seek(0)
        return buf
    except Exception:
        log.exception('Avatar info card render failed; using existing embed')
        return None
