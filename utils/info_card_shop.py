"""BEYCBOT two-ability ;info cosmetic renderer."""
from __future__ import annotations
import io
from PIL import Image, ImageDraw, ImageFont
from utils import info_card_legacy as legacy
from utils.hp_system import blade_hp_stat

W, H = 1400, 1050
BG=(3,13,25); PANEL=(7,22,37); CYAN=(83,213,247); WHITE=(238,245,250); GOLD=(236,190,62); MUTED=(130,162,183)

def _font(size, bold=True):
    paths=["assets/font.ttf","/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for p in paths:
        try: return ImageFont.truetype(p,size)
        except Exception: pass
    return ImageFont.load_default()

def _txt(d, xy, text, size, fill=WHITE, bold=True):
    d.text(xy, str(text), font=_font(size,bold), fill=fill)

def _box(d, box, outline=CYAN, width=2, fill=PANEL):
    d.rounded_rectangle(box, radius=10, fill=fill, outline=outline, width=width)

def _abilities(blade):
    return legacy._collect_abilities(blade)

def _art(blade, size):
    try:
        from utils.info_card_v2 import _art
        return _art(blade,size)
    except Exception:
        return None

def render(blade: dict):
    abilities=_abilities(blade)
    if len(abilities) != 2:
        return None
    img=Image.new("RGB",(W,H),BG); d=ImageDraw.Draw(img)
    d.rectangle((18,18,W-18,H-18),outline=CYAN,width=2)
    _txt(d,(52,28),"BEYCBOT",30); _txt(d,(W-125,28),";info",26)
    d.line((260,31,W-150,31),fill=CYAN,width=2)

    rarity=str(blade.get("rarity","Common")); btype=str(blade.get("type","Balance")); level=blade.get("level",1); name=str(blade.get("name","Unknown"))
    chips=[("★  "+rarity,GOLD),("○  "+btype,CYAN),(name,CYAN),("▥  LEVEL "+str(level),CYAN)]
    x=60
    for label,col in chips:
        w=max(170,min(310,40+len(label)*12)); _box(d,(x,160,x+w,207),outline=col); _txt(d,(x+18,170),label,19,fill=col); x+=w+18

    _box(d,(60,235,580,600))
    art=_art(blade,470)
    if art:
        art.thumbnail((450,450),Image.Resampling.LANCZOS)
        px=60+(520-art.width)//2; py=235+(365-art.height)//2
        img.paste(art,(px,py),art)
    else:
        d.ellipse((145,270,495,620),outline=(35,75,100),width=2)

    st=blade.get("stats") or {}
    vals=[("HP",blade_hp_stat(blade),"♡"),("ATK",st.get("attack",0),"⚔"),("DEF",st.get("defense",st.get("defence",0)),"⬡"),("STM",st.get("stamina",0),"ϟ")]
    positions=[(610,235,970,365),(980,235,1340,365),(610,375,970,505),(980,375,1340,505)]
    for (lab,val,icon),box in zip(vals,positions):
        _box(d,box,outline=(75,102,120)); _txt(d,(box[0]+25,box[1]+24),icon,34); _txt(d,(box[0]+95,box[1]+25),lab,31); _txt(d,(box[0]+95,box[1]+72),val,25,fill=CYAN)
        d.line((box[0]+25,box[3]-20,box[2]-25,box[3]-20),fill=CYAN,width=3)

    _box(d,(610,515,1340,600),outline=(75,102,120)); _txt(d,(635,532),"EXP",27)
    xp=blade.get("xp",blade.get("experience",0)); _txt(d,(735,535),xp,23,fill=CYAN)
    d.line((635,580,1310,580),fill=CYAN,width=5)

    rows=[("ABILITY 1",abilities[0],CYAN),("ABILITY 2",abilities[1],CYAN)]
    y=630
    for label,ab,col in rows:
        _box(d,(60,y,1340,y+105),outline=col)
        _txt(d,(82,y+15),label,18,fill=col); _txt(d,(220,y+14),ab.get("name","Unknown"),24)
        desc=str(ab.get("description",""))
        if len(desc)>115: desc=desc[:112]+"..."
        _txt(d,(220,y+55),desc,16,fill=MUTED,bold=False); y+=120

    special=blade.get("special") or blade.get("special_move") or {}
    _box(d,(60,870,1340,990),outline=GOLD,width=3)
    _txt(d,(82,892),"SPECIAL\nMOVE",21,fill=GOLD)
    if isinstance(special,dict):
        sname=special.get("name","Special Move"); sdesc=str(special.get("description",""))
    else:
        sname=str(special or "Special Move"); sdesc=""
    _txt(d,(280,888),sname,27,fill=GOLD)
    if len(sdesc)>120: sdesc=sdesc[:117]+"..."
    _txt(d,(280,935),sdesc,16,fill=MUTED,bold=False)
    out=io.BytesIO(); img.save(out,"PNG",optimize=True); out.name="beycbot_info.png"; out.seek(0); return out
