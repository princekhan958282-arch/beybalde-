"""Persistent state for Beycord Horror Story encounters."""
from __future__ import annotations
import json, os, threading, time
from datetime import datetime, timezone, timedelta
from utils.database import BASE_DIR
STATE_PATH=os.path.join(BASE_DIR,"data","horror_state.json");_LOCK=threading.Lock();HORROR_EFFECT_SECONDS=24*60*60;ENCOUNTER_STALE_SECONDS=24*60*60;IST=timezone(timedelta(hours=5,minutes=30))
def _blank():return {"curses":{},"encounters":{},"taken":{},"guild_daily_battles":{},"guild_active_encounters":{}}
def _read():
    try:
        with open(STATE_PATH,"r",encoding="utf-8") as f:
            data=json.load(f)
            if not isinstance(data,dict):return _blank()
    except(FileNotFoundError,json.JSONDecodeError,OSError):return _blank()
    for k in ("curses","encounters","taken","guild_daily_battles","guild_active_encounters"):data.setdefault(k,{})
    return data
def _write(data):
    os.makedirs(os.path.dirname(STATE_PATH),exist_ok=True);tmp=STATE_PATH+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:json.dump(data,f,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(tmp,STATE_PATH)
def ist_day_key(now=None):return datetime.fromtimestamp(time.time() if now is None else float(now),tz=IST).date().isoformat()
def guild_battle_used_today(guild_id,now=None):
    day=ist_day_key(now)
    with _LOCK:
        row=_read()["guild_daily_battles"].get(str(int(guild_id)),{})
        return row.get("day")==day and bool(row.get("used"))
def claim_guild_daily_battle(guild_id,user_id,now=None):
    stamp=time.time() if now is None else float(now);day=ist_day_key(stamp)
    with _LOCK:
        data=_read();key=str(int(guild_id));row=data["guild_daily_battles"].get(key,{})
        if row.get("day")==day and row.get("used"):return False
        data["guild_daily_battles"][key]={"day":day,"used":True,"user_id":int(user_id),"claimed_at":int(stamp)};_write(data);return True
def claim_guild_encounter(guild_id,user_id):
    """Reserve the one active UNKNOWN encounter slot for a guild."""
    now=int(time.time());gkey=str(int(guild_id))
    with _LOCK:
        data=_read();row=data["guild_active_encounters"].get(gkey,{})
        if row.get("active"):
            owner=str(row.get("user_id") or "")
            erow=data["encounters"].get(owner,{})
            if erow.get("status") in {"spawned","declined_once","battle_requested","battle_running"}:return False
        data["guild_active_encounters"][gkey]={"active":True,"user_id":int(user_id),"claimed_at":now};_write(data);return True
def release_guild_encounter(guild_id,user_id=None):
    with _LOCK:
        data=_read();key=str(int(guild_id));row=data["guild_active_encounters"].get(key,{})
        if user_id is not None and row.get("user_id") not in {None,int(user_id)}:return False
        row["active"]=False;row["released_at"]=int(time.time());data["guild_active_encounters"][key]=row;_write(data);return True
def curse_multiplier(user_id):
    key=str(int(user_id));now=int(time.time())
    with _LOCK:
        data=_read();row=data["curses"].get(key,{})
        if row.get("active") and int(row.get("expires_at") or 0)<=now:row["active"]=False;row["cleared_at"]=now;data["curses"][key]=row;_write(data)
        active=bool(row.get("active"))
    if not active:return 1.0
    try:return float(row.get("multiplier",0.8))
    except(TypeError,ValueError):return 0.8
def is_cursed(user_id):return curse_multiplier(user_id)<1.0
def apply_curse(user_id,*,source="unknown_challenger",duration=HORROR_EFFECT_SECONDS):
    now=int(time.time());row={"active":True,"multiplier":0.8,"source":source,"applied_at":now,"expires_at":now+int(duration)}
    with _LOCK:data=_read();data["curses"][str(int(user_id))]=row;_write(data)
    return dict(row)
def clear_curse(user_id):
    with _LOCK:data=_read();row=data["curses"].setdefault(str(int(user_id)),{});row["active"]=False;row["cleared_at"]=int(time.time());_write(data)
def record_taken(user_id,*,kind,amount=1,value=None,source="horror"):
    now=int(time.time());token=f"{int(user_id)}:{now}:{time.time_ns()}"
    with _LOCK:data=_read();data["taken"][token]={"user_id":int(user_id),"kind":str(kind),"amount":amount,"value":value,"source":source,"taken_at":now,"restore_at":now+HORROR_EFFECT_SECONDS,"returned":False};_write(data)
    return token
def due_restorations(now=None):
    now=int(now or time.time())
    with _LOCK:
        rows=_read()["taken"];return[(token,dict(row)) for token,row in rows.items() if not row.get("returned") and int(row.get("restore_at") or 0)<=now]
def mark_returned(token):
    with _LOCK:
        data=_read();row=data["taken"].get(str(token))
        if not row or row.get("returned"):return False
        row["returned"]=True;row["returned_at"]=int(time.time());_write(data);return True
def save_encounter(user_id,**fields):
    key=str(int(user_id));now=int(time.time())
    with _LOCK:
        data=_read();row=dict(data["encounters"].get(key,{}));row.update(fields);row["updated_at"]=now
        if fields.get("status")=="completed" and fields.get("bey_claimed") and not row.get("restoration_token"):
            token=f"{int(user_id)}:{now}:{time.time_ns()}";value={"name":fields.get("claimed_bey") or row.get("equipped_bey"),"copy_id":row.get("equipped_copy_id")};data["taken"][token]={"user_id":int(user_id),"kind":"bey","amount":1,"value":value,"source":"unknown_battle","taken_at":now,"restore_at":now+HORROR_EFFECT_SECONDS,"returned":False};row["restoration_token"]=token
        data["encounters"][key]=row;_write(data)
    return row
def encounter(user_id):
    with _LOCK:
        data=_read();key=str(int(user_id));row=dict(data["encounters"].get(key,{}))
        if row.get("status") in {"spawned","declined_once","battle_requested","battle_failed"} and int(row.get("updated_at") or 0)+ENCOUNTER_STALE_SECONDS<=int(time.time()):row["status"]="expired";row["expired_at"]=int(time.time());data["encounters"][key]=row;_write(data)
        return row
