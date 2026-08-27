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
import time
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
K_HASHES    = "com_recent_hashes"      # [[digest, when], ...] newest last
# The highest community level this player has already been ANNOUNCED at.
# Separate from K_LEVEL, which is what they currently are: the two differ for
# exactly as long as it takes to say so, and that gap is what stops a level
# being announced twice. See `claim_level` below.
K_LEVEL_SAID = "com_level_said"

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
DUPLICATE_MEMORY = 5         # how many recent messages are remembered
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


def recent_digests(profile: dict, now: Optional[float] = None) -> set:
    """Digests still inside the duplicate window.

    A single remembered digest — which is what this was — is defeated by
    alternating two sentences, because each message differs from the one
    immediately before it. Remembering the last few closes that.
    """
    when = time.time() if now is None else now
    out = set()
    for entry in (profile.get(K_HASHES) or []):
        try:
            digest, at = entry[0], float(entry[1])
        except (TypeError, ValueError, IndexError):
            continue
        if when - at < DUPLICATE_WINDOW:
            out.add(digest)
    return out


def remember_digest(profile: dict, digest: str,
                    now: Optional[float] = None) -> None:
    when = time.time() if now is None else now
    ring = [e for e in (profile.get(K_HASHES) or [])
            if isinstance(e, (list, tuple)) and len(e) == 2]
    ring.append([digest, when])
    profile[K_HASHES] = ring[-DUPLICATE_MEMORY:]


def outcome(*, awarded: int = 0, xp: int = 0, level: int = 0,
            from_level: int = 0, capped: bool = False,
            refused: str = "") -> dict:
    """Every award path returns THIS shape.

    `grant()` used to omit `refused` while `award_message()` included it, so
    `award["refused"]` raised a KeyError or not depending on which branch fired.
    One constructor means one shape.
    """
    return {"awarded": int(awarded), "xp": int(xp), "level": int(level),
            "levelled": int(level) > int(from_level),
            "from_level": int(from_level), "capped": bool(capped),
            "refused": refused}


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
        # and the daily cap on the profile is the durable backstop. Keyed by
        # UTC day so it empties itself instead of being cleared wholesale —
        # a blanket clear let everybody re-earn every message at once.
        self._reacted: set[tuple[str, str]] = set()
        self._reacted_day: str = CD.utc_day()

    # ── The one write path ───────────────────────────────────────────────────
    async def grant(self, guild_id: Any, user_id: Any, amount: int, *,
              source: str = "message", now: Optional[float] = None,
              touch: bool = False) -> dict:
        """Add XP, honour the daily cap, return what happened.

        `{"awarded", "xp", "level", "levelled", "from_level", "capped"}`.
        `touch=False` because most of these arrive from a message rather than
        a command, and `last_seen` means "played", not "typed".
        """
        # Checked HERE and not inside the mutate callback. `mutate_user` holds
        # `_users_lock` — the process-wide lock in front of every profile read
        # and write in the bot — and `require_main` can miss its 5s config
        # cache and go to the store, which on MySQL is a network round-trip
        # under that lock, with every battle and purchase queued behind it.
        require_main(guild_id)
        from utils.database import mutate_user

        amount = max(0, int(amount))
        if amount == 0 or not C.get(C.K_XP_ENABLED, True):
            return outcome(refused="xp is off" if amount else "nothing to give")

        def _apply(profile: dict) -> dict:
            today = CD.day_total(profile, K_DAY, "xp", now)
            give = min(amount, max(0, DAILY_XP_CAP - today))
            before = int(profile.get(K_XP) or 0)
            total = before + give
            from_level = level_from_xp(before)
            profile[K_XP] = total
            profile[K_LEVEL] = level_from_xp(total)
            if give:
                CD.bump_day(profile, K_DAY, "xp", give, now)
                CD.bump_day(profile, K_DAY, source, 1, now)
            return outcome(awarded=give, xp=total, level=profile[K_LEVEL],
                           from_level=from_level, capped=give < amount,
                           refused="" if give else "daily cap")

        return await mutate_user(int(user_id), _apply, touch=touch)

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
        # Duplicate BEFORE cooldown, so repeating yourself is reported as
        # repeating yourself. The other order labelled every duplicate inside
        # the window "cooling down", which is a misleading reason to hand a
        # caller that is deciding what to tell the player.
        if _text_hash(text) in recent_digests(profile, now):
            return False, "same message again"
        ok, _left = CD.profile_gate(profile, K_LAST_MSG, MESSAGE_COOLDOWN, now)
        if not ok:
            return False, "cooling down"
        if CD.day_total(profile, K_DAY, "xp", now) >= DAILY_XP_CAP:
            return False, "daily cap"
        return True, ""

    async def award_message(self, guild_id: Any, user_id: Any, content: str,
                      now: Optional[float] = None) -> dict:
        """Pay for one message, or refuse and say why."""
        require_main(guild_id)
        from utils.database import get_user, mutate_user

        text = (content or "").strip()
        if len(text) < MIN_LENGTH:
            # Cheapest refusal first: no store access at all for the shortest
            # messages, which are most of them.
            return outcome(refused="too short")

        # A read before the write. This runs on EVERY message in the main
        # server, and with a 60s cooldown ~98% of them award nothing — the old
        # version still took `_users_lock`, re-serialised the whole profile
        # JSON and upserted it for each one, and created a profile row for
        # anyone who merely talked. A read is cheap and takes no lock.
        peek = await get_user(int(user_id))
        ok, why = self.may_award_message(guild_id, peek, text, now)
        if not ok:
            return outcome(xp=int(peek.get(K_XP) or 0),
                           level=int(peek.get(K_LEVEL) or 0),
                           from_level=int(peek.get(K_LEVEL) or 0),
                           capped=(why == "daily cap"), refused=why)

        amount = random.randint(XP_MESSAGE_MIN, XP_MESSAGE_MAX)

        def _apply(profile: dict) -> dict:
            # Re-checked under the lock: the read above is a fast path, not the
            # decision. Two messages a millisecond apart must not both pay.
            fresh_ok, fresh_why = self.may_award_message(
                guild_id, profile, text, now)
            if not fresh_ok:
                return outcome(xp=int(profile.get(K_XP) or 0),
                               level=int(profile.get(K_LEVEL) or 0),
                               from_level=int(profile.get(K_LEVEL) or 0),
                               capped=(fresh_why == "daily cap"),
                               refused=fresh_why)
            today = CD.day_total(profile, K_DAY, "xp", now)
            give = min(amount, max(0, DAILY_XP_CAP - today))
            before = int(profile.get(K_XP) or 0)
            total = before + give
            from_level = level_from_xp(before)
            profile[K_XP] = total
            profile[K_LEVEL] = level_from_xp(total)
            CD.stamp(profile, K_LAST_MSG, now)
            remember_digest(profile, _text_hash(text), now)
            CD.bump_day(profile, K_DAY, "xp", give, now)
            CD.bump_day(profile, K_DAY, "message", 1, now)
            return outcome(awarded=give, xp=total, level=profile[K_LEVEL],
                           from_level=from_level, capped=give < amount)

        # touch=False: this is a message, not a command. See mutate_user.
        return await mutate_user(int(user_id), _apply, touch=False)

    async def award_reaction(self, guild_id: Any, user_id: Any, message_id: Any,
                       author_id: Any = None,
                       now: Optional[float] = None) -> dict:
        """Pay for reacting to somebody ELSE's message, once per message."""
        require_main(guild_id)
        # An unknown author is treated as your own message, not somebody
        # else's. The old default ran the other way, so when the author lookup
        # failed, reacting to your own messages paid.
        if author_id is None or str(author_id) == str(user_id):
            return outcome(refused="own message")

        day = CD.utc_day(now)
        if day != self._reacted_day:
            self._reacted_day, self._reacted = day, set()
        key = (str(user_id), str(message_id))
        if key in self._reacted:
            return outcome(refused="already reacted")

        from utils.database import get_user
        profile = await get_user(int(user_id))
        if CD.day_total(profile, K_DAY, "reaction", now) >= DAILY_REACT_CAP:
            return outcome(refused="daily reaction cap")

        result = await self.grant(guild_id, user_id, XP_REACTION, source="reaction",
                            now=now)
        # Marked consumed only if it actually paid. Burning the key first meant
        # a capped-out day permanently ate the message: the cap lifts at
        # midnight, but the "already reacted" memory did not.
        if result["awarded"]:
            self._reacted.add(key)
            if len(self._reacted) > 100_000:
                self._reacted = set(list(self._reacted)[-50_000:])
        return result

    async def award_poll_vote(self, guild_id: Any, user_id: Any,
                        now: Optional[float] = None) -> dict:
        """Duplicate-proof because the vote ROW is, not because of a set here."""
        require_main(guild_id)
        return await self.grant(guild_id, user_id, XP_POLL_VOTE, source="poll",
                          now=now, touch=True)

    async def award_giveaway_entry(self, guild_id: Any, user_id: Any,
                             now: Optional[float] = None) -> dict:
        require_main(guild_id)
        return await self.grant(guild_id, user_id, XP_GIVEAWAY, source="giveaway",
                          now=now, touch=True)

    # ── Reads ────────────────────────────────────────────────────────────────
    async def card(self, guild_id: Any, user_id: Any) -> dict:
        """What `/level` renders."""
        require_main(guild_id)
        from utils.database import get_user
        profile = await get_user(int(user_id))
        xp = int(profile.get(K_XP) or 0)
        level, span, into = progress(xp)
        return {"xp": xp, "level": level, "span": span, "into": into,
                "today": CD.day_total(profile, K_DAY, "xp"),
                "cap": DAILY_XP_CAP,
                "next_at": xp_for_level(level + 1) if level < MAX_LEVEL else 0}

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

    def clear_level_roles(self, guild_id: Any) -> int:
        """Forget every level -> role mapping. Returns how many went.

        Replaces a single-level version that nothing could reach; the panel was
        writing the config key directly, which put a second writer next to the
        manager that owns it.
        """
        require_main(guild_id)
        had = len(C.level_roles())
        C.put(C.K_LEVEL_ROLES, {})
        return had

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
