"""
casino_wallet.py
Separate coin economy for the casino — fully isolated from Beycord's main currency.
Uses a simple JSON file for persistence (swap for DB calls when ready).
"""

import json
import asyncio
import os
from pathlib import Path

# Resolved from __file__, NOT the working directory. A relative path here means
# that starting the bot from any other directory silently creates a fresh, empty
# wallet file — every player's casino balance reads as zero and new writes land
# somewhere the real file never sees. Same failure that hit _BEY_DIR and
# config_local; every other module in the project already resolves this way.
_ROOT       = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WALLET_FILE = Path(_ROOT) / "data" / "casino_wallets.json"
DAILY_BONUS = 500
_lock = asyncio.Lock()


def _load() -> dict:
    if not WALLET_FILE.exists():
        WALLET_FILE.parent.mkdir(parents=True, exist_ok=True)
        return {}
    with open(WALLET_FILE) as f:
        return json.load(f)


def _save(data: dict):
    import os
    tmp = WALLET_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, WALLET_FILE)


async def get_balance(user_id: int) -> int:
    async with _lock:
        data = _load()
        return data.get(str(user_id), {}).get("balance", 0)


async def set_balance(user_id: int, amount: int):
    async with _lock:
        data = _load()
        uid = str(user_id)
        if uid not in data:
            data[uid] = {"balance": 0}
        data[uid]["balance"] = max(0, amount)
        _save(data)


async def deduct(user_id: int, amount: int) -> bool:
    """Returns False if insufficient funds."""
    async with _lock:
        data = _load()
        uid = str(user_id)
        bal = data.get(uid, {}).get("balance", 0)
        if bal < amount:
            return False
        data.setdefault(uid, {})["balance"] = bal - amount
        _save(data)
        return True


async def credit(user_id: int, amount: int):
    async with _lock:
        data = _load()
        uid = str(user_id)
        data.setdefault(uid, {})["balance"] = data.get(uid, {}).get("balance", 0) + amount
        _save(data)


async def can_afford(user_id: int, amount: int) -> bool:
    return await get_balance(user_id) >= amount


async def _load_async() -> dict:
    """Thread-safe async read of wallet data (no write)."""
    async with _lock:
        return _load()


async def claim_daily(user_id: int) -> tuple[bool, int]:
    """Returns (claimed, amount). False if already claimed today.
    Amount reflects active premium pass bonus if present.
    """
    import time
    from . import casino_premium
    bonus = await casino_premium.get_daily_bonus(user_id)
    async with _lock:
        data = _load()
        uid = str(user_id)
        today = int(time.time() // 86400)
        last  = data.get(uid, {}).get("last_daily", 0)
        if last == today:
            return False, 0
        data.setdefault(uid, {})
        data[uid]["balance"]    = data[uid].get("balance", 0) + bonus
        data[uid]["last_daily"] = today
        _save(data)
        return True, bonus
