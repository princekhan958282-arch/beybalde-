"""
avatar_progress.py — where a player's per-card avatar progression is stored.

Shape, under `profile["avatar"]`:

    {
      "cards": {
        "avatar_x002": {
          "level": 3,
          "skills": {"titans-might": 4, "piercing-gaze": 2},
          "spent":  {"card": 63000, "skills": {"titans-might": 28000}}
        }
      }
    }

Design notes that are load-bearing:

**Per card, not per account.** Players collect avatars and equip one; levelling
the account instead would mean the 29 cards a player owns all share one level,
which makes the collection meaningless the moment the level is bought.

**Absence is a valid state.** Nothing migrates 3,356 existing profiles. Every
read defaults, so a profile that has never bought anything simply has no
`"avatar"` key and behaves exactly as it did before this module existed. The
block is created on the first purchase and never before. A batch migration
would be 3,356 writes to make no behavioural difference.

**Skills are keyed by name-slug, not array index.** An index is stable only
while `avatar_data.json`'s `skills` array is append-only, and the first time
somebody reorders one, every player's skill levels silently move to a different
skill. A renamed skill loses its level, which is rare, visible, and recoverable.

**`spent` records real coins paid.** Refunds are computed from it, never from
the cost table — see `avatar_levels.refund_for`.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Optional

from . import avatar_levels as AL

PROFILE_KEY = "avatar"


class PurchaseError(Exception):
    """Raised inside a transaction to abandon it with a player-facing reason.

    Raising is the abort: `database.mutate_user` writes nothing if the callback
    raises, so there is no path where coins leave without the level arriving.
    """


def slugify(name: str) -> str:
    """'Titan's Might' -> 'titans-might', 'Ōkami Strike' -> 'okami-strike'.

    Three details, each of which is a collision waiting to happen if skipped:

    * Apostrophes are DELETED, not treated as separators. Otherwise "Titan's
      Might" becomes `titan-s-might`, which is ugly and, worse, matches nothing
      a human would guess when reading the JSON.
    * Accents are folded through NFKD rather than dropped. A bare regex turns
      "Ōkami" into "kami", so "Ōkami Strike" and "Kami Strike" would become the
      same key and share a level.
    * A name with no latin characters at all (pure CJK, pure emoji) falls back
      to a hash of the original instead of the constant "skill" — three such
      names on one card would otherwise be one shared level.
    """
    raw = str(name or "")
    folded = unicodedata.normalize("NFKD", raw)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = re.sub(r"['’ʼ]", "", folded.lower())
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    if slug:
        return slug
    if not raw.strip():
        return "skill"
    return "skill-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def skill_slugs(avatar: dict) -> list[str]:
    return [slugify(sk.get("name", "")) for sk in (avatar.get("skills") or [])]


# ── Reads (never write, always default) ──────────────────────────────────────

def _block(profile: dict) -> dict:
    blk = profile.get(PROFILE_KEY)
    return blk if isinstance(blk, dict) else {}


def card_entry(profile: dict, avatar_id: str) -> dict:
    """One card's progression, defaulted. The returned dict is a COPY for
    absent cards, so a caller cannot accidentally persist a default."""
    cards = _block(profile).get("cards")
    entry = cards.get(str(avatar_id)) if isinstance(cards, dict) else None
    if not isinstance(entry, dict):
        return {"level": 1, "skills": {}, "spent": {"card": 0, "skills": {}}}
    return entry


def card_level(profile: dict, avatar_id: str) -> int:
    return AL.clamp_level(card_entry(profile, avatar_id).get("level", 1))


def skill_level(profile: dict, avatar_id: str, slug: str) -> int:
    skills = card_entry(profile, avatar_id).get("skills")
    raw = skills.get(slug, 1) if isinstance(skills, dict) else 1
    try:
        lvl = int(raw)
    except (TypeError, ValueError):
        lvl = 1
    # Clamped on READ as well as on write. A card that is refunded down to Lv1
    # must not leave a Lv8 skill behind it, and clamping here means that holds
    # even for data written by an older build or edited by hand.
    return max(1, min(AL.max_skill_level_for(card_level(profile, avatar_id)), lvl))


def spent_on_card(profile: dict, avatar_id: str) -> int:
    spent = card_entry(profile, avatar_id).get("spent") or {}
    try:
        return int(spent.get("card", 0) or 0)
    except (TypeError, ValueError):
        return 0


def spent_on_skill(profile: dict, avatar_id: str, slug: str) -> int:
    spent = (card_entry(profile, avatar_id).get("spent") or {}).get("skills") or {}
    try:
        return int(spent.get(slug, 0) or 0)
    except (TypeError, ValueError):
        return 0


def total_spent(profile: dict, avatar_id: str) -> int:
    entry = card_entry(profile, avatar_id)
    spent = entry.get("spent") or {}
    skills = spent.get("skills") or {}
    out = spent_on_card(profile, avatar_id)
    for slug in skills:
        out += spent_on_skill(profile, avatar_id, slug)
    return out


# ── Writes (mutate a profile dict in place; caller persists) ─────────────────

def _ensure(profile: dict, avatar_id: str) -> dict:
    blk = profile.get(PROFILE_KEY)
    if not isinstance(blk, dict):
        blk = {}
        profile[PROFILE_KEY] = blk
    cards = blk.get("cards")
    if not isinstance(cards, dict):
        cards = {}
        blk["cards"] = cards
    entry = cards.get(str(avatar_id))
    if not isinstance(entry, dict):
        entry = {"level": 1, "skills": {}, "spent": {"card": 0, "skills": {}}}
        cards[str(avatar_id)] = entry
    entry.setdefault("level", 1)
    entry.setdefault("skills", {})
    spent = entry.setdefault("spent", {})
    spent.setdefault("card", 0)
    spent.setdefault("skills", {})
    return entry


def quote_card(profile: dict, avatar_id: str, levels: int = 1) -> dict:
    """What a card upgrade would cost and produce. Pure — writes nothing.

    Returned so the confirm card and the transaction can be built from the same
    numbers instead of computing them twice and disagreeing.
    """
    now = card_level(profile, avatar_id)
    want = AL.clamp_level(now + max(1, int(levels or 1)))
    return {
        "from": now,
        "to": want,
        "levels": want - now,
        "cost": AL.card_level_cost(now, want),
        "maxed": now >= AL.MAX_CARD_LEVEL,
        "coins": int(profile.get("coins", 0) or 0),
        "skill_cap_now": AL.max_skill_level_for(now),
        "skill_cap_after": AL.max_skill_level_for(want),
    }


def quote_skill(profile: dict, avatar_id: str, slug: str,
                levels: int = 1) -> dict:
    """What a skill upgrade would cost, including why it is blocked."""
    now = skill_level(profile, avatar_id, slug)
    cap = AL.max_skill_level_for(card_level(profile, avatar_id))
    want = max(1, min(cap, now + max(1, int(levels or 1))))
    blocked = ""
    if now >= AL.MAX_SKILL_LEVEL:
        blocked = f"Already at the maximum, Lv{AL.MAX_SKILL_LEVEL}."
    elif now >= cap:
        # Named, not silently rejected — the spec's §5.5 rule, and the
        # difference between a button that teaches and one that frustrates.
        blocked = (f"Skill capped at Lv{cap} — raise the avatar to "
                   f"Lv{min(AL.MAX_CARD_LEVEL, (now // 2) + 1)} first.")
    return {
        "from": now, "to": want, "levels": max(0, want - now),
        "cost": AL.skill_level_cost(now, want),
        "cap": cap, "blocked": blocked,
        "coins": int(profile.get("coins", 0) or 0),
    }


def apply_card_purchase(profile: dict, avatar_id: str, levels: int = 1) -> dict:
    """Deduct coins and raise the card level, in one mutation.

    Call this INSIDE `database.mutate_user` so the balance is re-read under the
    lock rather than trusted from whatever a confirm card showed 60 seconds ago.
    """
    q = quote_card(profile, avatar_id, levels)
    if q["maxed"]:
        raise PurchaseError(f"Already at the maximum, Lv{AL.MAX_CARD_LEVEL}.")
    if q["levels"] <= 0:
        raise PurchaseError("Nothing to buy.")
    coins = int(profile.get("coins", 0) or 0)
    if coins < q["cost"]:
        raise PurchaseError(
            f"Costs {q['cost']:,} coins — you have {coins:,}, "
            f"{q['cost'] - coins:,} short.")

    entry = _ensure(profile, avatar_id)
    profile["coins"] = coins - q["cost"]
    entry["level"] = q["to"]
    entry["spent"]["card"] = int(entry["spent"].get("card", 0) or 0) + q["cost"]
    return q


def apply_skill_purchase(profile: dict, avatar_id: str, slug: str,
                         levels: int = 1) -> dict:
    q = quote_skill(profile, avatar_id, slug, levels)
    if q["blocked"]:
        raise PurchaseError(q["blocked"])
    if q["levels"] <= 0:
        raise PurchaseError("Nothing to buy.")
    coins = int(profile.get("coins", 0) or 0)
    if coins < q["cost"]:
        raise PurchaseError(
            f"Costs {q['cost']:,} coins — you have {coins:,}, "
            f"{q['cost'] - coins:,} short.")

    entry = _ensure(profile, avatar_id)
    profile["coins"] = coins - q["cost"]
    entry["skills"][slug] = q["to"]
    skills_spent = entry["spent"]["skills"]
    skills_spent[slug] = int(skills_spent.get(slug, 0) or 0) + q["cost"]
    return q


def apply_reset(profile: dict, avatar_id: str) -> dict:
    """Drop a card to Lv1, all skills to Lv1, refund 70% of the real spend."""
    spent = total_spent(profile, avatar_id)
    refund = AL.refund_for(spent)
    entry = _ensure(profile, avatar_id)
    entry["level"] = 1
    entry["skills"] = {}
    entry["spent"] = {"card": 0, "skills": {}}
    profile["coins"] = int(profile.get("coins", 0) or 0) + refund
    return {"spent": spent, "refund": refund}


# ── Convenience for the battle path ──────────────────────────────────────────

def equipped_card_level(user_id: int, profile: Optional[dict] = None) -> tuple:
    """(avatar_id, level) for whatever this player has equipped.

    Never raises and never writes: a broken or absent avatar block must not
    stop a fight starting, so anything unexpected reads as (None, 1).
    """
    try:
        if profile is None:
            from utils.database import get_user
            profile = get_user(user_id)
        avatar_id = profile.get("equipped_avatar")
        if not avatar_id:
            return None, 1
        return avatar_id, card_level(profile, avatar_id)
    except Exception:                                    # noqa: BLE001
        return None, 1
