"""
code_store.py  —  shared code generation and storage helpers

Two separate systems live on top of this:
  * redeem.py  — admin-issued reward codes players can claim
  * backup.py  — per-player account recovery codes

Both need the same things: unambiguous human-typeable codes, atomic JSON
storage, and a lock so two claims can't race.
"""

import os
import secrets
import threading
from typing import Optional

from utils.database import BASE_DIR, _atomic_write_json, _read_json

# No 0/O/1/I/L — these get read off a phone screen and typed by hand.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

REDEEM_PATH = os.path.join(BASE_DIR, "data", "redeem_codes.json")
BACKUP_PATH = os.path.join(BASE_DIR, "data", "backup_codes.json")

redeem_lock = threading.Lock()
backup_lock = threading.Lock()


def make_code(prefix: str, groups: int = 3, size: int = 4) -> str:
    """e.g. BEY-7K2M-QX4P-N9WD.

    31^12 ≈ 7.9e17 combinations — brute-forcing one over Discord's rate limits
    is not a realistic attack, and every lookup is a dict hit so there is no
    timing side channel worth worrying about.
    """
    body = "-".join(
        "".join(secrets.choice(ALPHABET) for _ in range(size))
        for _ in range(groups)
    )
    return f"{prefix}-{body}"


def normalise(code: str) -> str:
    """Accept sloppy input: lowercase, missing dashes, stray spaces.

    No confusable-folding here on purpose. ALPHABET already excludes every
    ambiguous pair (0/O, 1/I/L), so there is nothing legitimate to fold — and
    a naive chain like .replace("0","O").replace("O","0") collapses BOTH
    characters into one and silently corrupts valid codes.
    """
    if not code:
        return ""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def load(path: str, default_factory) -> dict:
    data = _read_json(path, default_factory)
    return data if isinstance(data, dict) else default_factory()


def save(path: str, data: dict) -> None:
    _atomic_write_json(path, data)
