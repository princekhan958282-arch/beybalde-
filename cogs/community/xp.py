"""
cogs/community/xp.py — `XPManager` and `LevelManager`. Main server only.

A SEPARATE track from trainer XP, and that separation is the point. Trainer XP
(`profile["xp"]`) drives trainer levels, and every trainer level-up pays
`level x 100` coins inside `grant_xp` — so if chatting fed that track, talking
would print money. Community XP feeds `profile["community_xp"]` and pays roles
and status, never coins.

The key is NOT called `level`: `get_user` force-recomputes `profile["level"]`
from `profile["xp"]` on every single read (`utils/database.py:360`), so a
community level stored there would be silently overwritten within milliseconds.

Anti-farming lives here rather than in the listener, so every source is guarded
whether it arrives from a message, a button, or a future caller that does not
exist yet.
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
from typing import Any, Optional

from . import config as C
from . import cooldowns as CD
from .guard import require_main

log = logging.getLogger("beyblade_bot.community.xp")

# ── Profile keys ─────────────────────────────────────────────────────────────
K_XP        = "community_xp"
K_LEVEL     = "com_level"
K_LAST_MSG  = "com_last_msg"
K_DAY       = "com_day"
K_LAST_HASH = "com_last_hash"
K_HASH_AT   = "com_last_hash_at"

# ── The curve ────────────────────────────────────────────────────────────────
# Same family as the trainer curve (utils/trainer_levels.py) so the two feel
# related, with a smaller constant and a far lower cap: this is a community
# standing, not a second progression grind.
XP_PER_LEVEL_SQ = 25
MAX_LEVEL = 200

# ── Awards ───────────────────────────────────────────────────────────────────
XP_MESSAGE_MIN, XP_MESSAGE_MAX = 8, 15
XP_REACTION      = 2
XP_POLL_VOTE     = 5
XP_GIVEAWAY      = 3
XP_MEME_APPROVED = 25
XP_MEME_UPVOTE   = 1

# ── The guards ───────────────────────────────────────────────────────────────
MESSAGE_COOLDOWN = 60.0      # per player, between paying messages
MIN_LENGTH       = 8         # characters, after stripping
DUPLICATE_WINDOW = 300.0     # repeating yourself earns nothing for this long
DAILY_XP_CAP     = 1_500
DAILY_REACT_CAP  = 20

SOURCES = ("message", "reaction", "poll", "giveaway", "meme", "meme_vote")


def xp_for_level(level: int) -> int:
    return max(0, int(level)) ** 2 * XP_PER_LEVEL_SQ


def level_from_xp(xp: int) -> int:
    try:
        total = max(0, int(xp))
    except (TypeError, ValueError):
        return 0
    return min(MAX_LEVEL, int(math.isqrt(total // XP_PER_LEVEL_SQ)))


def progress(xp: int) -> tuple[int, int, int]:
    """`(level, span_to_next, earned_into_this_level)` — for the card."""
    level = level_from_xp(xp)
    if level >= MAX_LEVEL:
        return level, 0, 0
    floor_xp, next_xp = xp_for_level(level), xp_for_level(level + 1)
    return level, next_xp - floor_xp, max(0, int(xp) - floor_xp)


def _text_hash(text: str) -> str:
    return hashlib.sha1((text or "").strip().lower().encode()).hexdigest()[:16]


class XPManager:
    """Every community XP award goes through `grant`. Nothing else writes it.

    Every public method takes `guild_id` first and calls `require_main`. That
    is the service-layer half of the main-server lock, and the suite walks this
    class by introspection to prove no method skipped it.
    """

    def __init__(self, bot=None) -> None:
        self.bot = bot
        # In memory on purpose: "have I already paid for a reaction on THIS
        # message" only has to survive until the reaction stops being fresh,
        # and the daily cap on the profile is the durable backstop.
        self._reacted: set[tuple[str, str]] = set()

    # ── The one write path ───────────────────────────────────────────────────
    def grant(self, guild_id: Any, user_id: Any, amount: int, *,
              source: str = "message", now: Optional[float] = None,
              touch: bool = False) -> dict:
        """Add XP, honour the daily cap, return what happened.

        `{"awarded", "xp", "level", "levelled", "from_level", "capped"}`.
        `touch=False` because most of these arrive from a message rather than
        a command, and `last_seen` means "played", not "typed".
        """
        require_main(guild_id)
        from utils.database import mutate_user

        amount = max(0, int(amount))
        if amount == 0 or not C.get(C.K_XP_ENABLED, True):
            return {"awarded": 0, "xp": 0, "level": 0, "levelled": False,
                    "from_level": 0, "capped": False}

        def _apply(profile: dict) -> dict:
            today = CD.day_total(profile, K_DAY, "xp", now)
            room = max(0, DAILY_XP_CAP - today)
            give = min(amount, room)
            before = int(profile.get(K_XP) or 0)
            total = before + give
            from_level = level_from_xp(before)
            level = level_from_xp(total)
            profile[K_XP] = total
            profile[K_LEVEL] = level
            if give:
                CD.bump_day(profile, K_DAY, "xp", give, now)
                CD.bump_day(profile, K_DAY, source, 1, now)
            return {"awarded": give, "xp": total, "level": level,
                    "levelled": level > from_level, "from_level": from_level,
                    "capped": give < amount}

        return mutate_user(int(user_id), _apply, touch=touch)

    # ── Sources ──────────────────────────────────────────────────────────────
    def may_award_message(self, guild_id: Any, profile: dict, content: str,
                          now: Optional[float] = None) -> tuple[bool, str]:
        """The anti-farm gate, as a pure function of the profile and the text.

        Separate from `award_message` so the suite can interrogate each refusal
        reason without writing to a store.
        """
        require_main(guild_id)
        text = (content or "").strip()
        if len(text) < MIN_LENGTH:
            return False, "too short"
        ok, _left = CD.profile_gate(profile, K_LAST_MSG, MESSAGE_COOLDOWN, now)
        if not ok:
            return False, "cooling down"
        digest = _text_hash(text)
        if profile.get(K_LAST_HASH) == digest:
            last_at = float(profile.get(K_HASH_AT) or 0.0)
            import time as _t
            when = _t.time() if now is None else now
            if when - last_at < DUPLICATE_WINDOW:
                return False, "same message again"
        if CD.day_total(profile, K_DAY, "xp", now) >= DAILY_XP_CAP:
            return False, "daily cap"
        return True, ""

    def award_message(self, guild_id: Any, user_id: Any, content: str,
                      now: Optional[float] = None) -> dict:
        """Pay for one message, or refuse and say why."""
        require_main(guild_id)
        from utils.database import mutate_user

        text = (content or "").strip()
        amount = random.randint(XP_MESSAGE_MIN, XP_MESSAGE_MAX)

        def _apply(profile: dict) -> dict:
            ok, why = self.may_award_message(guild_id, profile, text, now)
            if not ok:
                return {"awarded": 0, "xp": int(profile.get(K_XP) or 0),
                        "level": int(profile.get(K_LEVEL) or 0),
                        "levelled": False,
                        "from_level": int(profile.get(K_LEVEL) or 0),
                        "capped": why == "daily cap", "refused": why}
            today = CD.day_total(profile, K_DAY, "xp", now)
            give = min(amount, max(0, DAILY_XP_CAP - today))
            before = int(profile.get(K_XP) or 0)
            total = before + give
            from_level = level_from_xp(before)
            level = level_from_xp(total)
            profile[K_XP] = total
            profile[K_LEVEL] = level
            CD.stamp(profile, K_LAST_MSG, now)
            profile[K_LAST_HASH] = _text_hash(text)
            CD.stamp(profile, K_HASH_AT, now)
            CD.bump_day(profile, K_DAY, "xp", give, now)
            CD.bump_day(profile, K_DAY, "message", 1, now)
            return {"awarded": give, "xp": total, "level": level,
                    "levelled": level > from_level, "from_level": from_level,
                    "capped": give < amount, "refused": ""}

        # touch=False: this is a message, not a command. See mutate_user.
        return mutate_user(int(user_id), _apply, touch=False)

    def award_reaction(self, guild_id: Any, user_id: Any, message_id: Any,
                       author_id: Any = None,
                       now: Optional[float] = None) -> dict:
        """Pay for reacting to somebody ELSE's message, once per message."""
        require_main(guild_id)
        nothing = {"awarded": 0, "xp": 0, "level": 0, "levelled": False,
                   "from_level": 0, "capped": False}
        if author_id is not None and str(author_id) == str(user_id):
            return dict(nothing, refused="own message")
        key = (str(user_id), str(message_id))
        if key in self._reacted:
            return dict(nothing, refused="already reacted")

        from utils.database import get_user
        profile = get_user(int(user_id))
        if CD.day_total(profile, K_DAY, "reaction", now) >= DAILY_REACT_CAP:
            return dict(nothing, refused="daily reaction cap")

        self._reacted.add(key)
        if len(self._reacted) > 50_000:
            self._reacted.clear()
        return self.grant(guild_id, user_id, XP_REACTION, source="reaction",
                          now=now)

    def award_poll_vote(self, guild_id: Any, user_id: Any,
                        now: Optional[float] = None) -> dict:
        """Duplicate-proof because the vote ROW is, not because of a set here."""
        require_main(guild_id)
        return self.grant(guild_id, user_id, XP_POLL_VOTE, source="poll",
                          now=now, touch=True)

    def award_giveaway_entry(self, guild_id: Any, user_id: Any,
                             now: Optional[float] = None) -> dict:
        require_main(guild_id)
        return self.grant(guild_id, user_id, XP_GIVEAWAY, source="giveaway",
                          now=now, touch=True)

    # ── Reads ────────────────────────────────────────────────────────────────
    def card(self, guild_id: Any, user_id: Any) -> dict:
        """What `/level` renders."""
        require_main(guild_id)
        from utils.database import get_user
        profile = get_user(int(user_id))
        xp = int(profile.get(K_XP) or 0)
        level, span, into = progress(xp)
        return {"xp": xp, "level": level, "span": span, "into": into,
                "today": CD.day_total(profile, K_DAY, "xp"),
                "cap": DAILY_XP_CAP,
                "next_at": xp_for_level(level + 1) if level < MAX_LEVEL else 0}

    def reset_memory(self) -> None:
        self._reacted.clear()


class LevelManager:
    """Level-ups: the announcement, and the role rewards.

    Role writes are the first in this codebase — nothing has ever called
    `add_roles`. So validation is explicit and happens twice: when the mapping
    is saved, and again at grant time, because a role can be moved above the
    bot between the two.
    """

    def __init__(self, bot=None) -> None:
        self.bot = bot

    # ── Mapping ──────────────────────────────────────────────────────────────
    def check_role(self, guild, role) -> tuple[bool, str]:
        """`(ok, why_not)` for using this role as a reward. Never raises."""
        if role is None:
            return False, "That role no longer exists."
        me = getattr(guild, "me", None)
        perms = getattr(me, "guild_permissions", None)
        if perms is not None and not getattr(perms, "manage_roles", False):
            return False, ("I don't have **Manage Roles** in this server, so I "
                           "can't hand out level roles.")
        if getattr(role, "is_default", None) and role.is_default():
            return False, "`@everyone` can't be a level reward."
        if getattr(role, "managed", False):
            return False, (f"**{role}** is managed by an integration, so "
                           f"Discord won't let me assign it.")
        top = getattr(me, "top_role", None)
        if top is not None and getattr(role, "position", 0) >= getattr(
                top, "position", 0):
            return False, (f"**{role}** sits above my own top role — move my "
                           f"role higher, or pick a lower one.")
        return True, ""

    def set_level_role(self, guild_id: Any, level: int, role) -> tuple[bool, str]:
        require_main(guild_id)
        guild = getattr(role, "guild", None)
        ok, why = self.check_role(guild, role)
        if not ok:
            return False, why
        level = max(1, min(MAX_LEVEL, int(level)))
        mapping = {str(k): v for k, v in C.level_roles().items()}
        mapping[str(level)] = int(role.id)
        C.put(C.K_LEVEL_ROLES, mapping)
        return True, f"Level **{level}** now grants **{role}**."

    def clear_level_role(self, guild_id: Any, level: int) -> bool:
        require_main(guild_id)
        mapping = {str(k): v for k, v in C.level_roles().items()}
        removed = mapping.pop(str(int(level)), None) is not None
        C.put(C.K_LEVEL_ROLES, mapping)
        return removed

    def roles_for(self, guild_id: Any, level: int) -> list[int]:
        """Every role a player at this level has earned, lowest first."""
        require_main(guild_id)
        return [rid for lvl, rid in sorted(C.level_roles().items())
                if lvl <= int(level)]

    # ── Granting ─────────────────────────────────────────────────────────────
    async def apply_roles(self, guild_id: Any, member, level: int) -> dict:
        """Give the member everything their level has earned. Never raises."""
        require_main(guild_id)
        out = {"added": [], "skipped": []}
        guild = getattr(member, "guild", None)
        if guild is None:
            return out
        have = {getattr(r, "id", None) for r in getattr(member, "roles", [])}
        for role_id in self.roles_for(guild_id, level):
            if role_id in have:
                continue
            role = guild.get_role(role_id)
            ok, why = self.check_role(guild, role)
            if not ok:
                out["skipped"].append((role_id, why))
                log.warning("[community] level role %s skipped: %s",
                            role_id, why)
                continue
            try:
                await member.add_roles(role, reason="Beycord community level")
                out["added"].append(role_id)
            except Exception as exc:                     # noqa: BLE001
                out["skipped"].append((role_id, str(exc)))
                log.exception("[community] could not add role %s", role_id)
        return out
