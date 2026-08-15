"""
utils/inventory.py — how many beys you may hold, and buying room for more.

Everyone starts with 200 slots. Extra slots are 10,000 coins each, bought one
at a time, permanent, up to 2,000.

Three rules, each of which was a decision rather than an accident:

**A slot is a bey, duplicates included.** The cap counts entries, not distinct
names — there are only 91 blades, so a distinct-name cap would never bind and
the slot shop would never sell anything. 200 is roughly two of everything.

**A listed bey still occupies its slot.** `;sellbey` takes a bey out of
`inventory` and holds it in `marketplace_listings`; `;cancellisting` puts it
back. If listings were free, listing was unlimited storage — park five hundred
beys at a price nobody will pay and your inventory reads empty. Counting them
also makes cancelling always safe: the slot was never released, so there is
always somewhere to put it back.

**A full inventory REFUSES, it never silently trims.** `boss_copy.add_copy`
does the opposite — it trims to `MAX_COPIES` and destroys the item with no
message — which is tolerable for a rolled copy nobody paid for and is not
tolerable here, where a bey may have been bought with coins thirty seconds
earlier. Every refusal names the cap and points at `;beyslots`.

Deliberately discord-free so the sim harnesses and `utils.database` can both
import it.
"""
from __future__ import annotations

BASE_INVENTORY_SLOTS = 200
EXTRA_SLOT_PRICE     = 10_000      # flat, per slot, permanent
MAX_INVENTORY_SLOTS  = 2_000

K_EXTRA_SLOTS = "inventory_slots_bought"


class SlotError(Exception):
    """A refused slot purchase, carrying the player-facing reason."""


class InventoryFull(Exception):
    """No room for another bey. The message names the cap and `;beyslots`."""


def extra_slots(profile: dict) -> int:
    try:
        return max(0, int((profile or {}).get(K_EXTRA_SLOTS, 0) or 0))
    except (TypeError, ValueError):
        return 0


def capacity(profile: dict) -> int:
    """Total slots this player has, base plus bought, clamped to the ceiling."""
    return min(MAX_INVENTORY_SLOTS, BASE_INVENTORY_SLOTS + extra_slots(profile))


def used(profile: dict) -> int:
    """Slots in use: beys held PLUS beys parked on the marketplace."""
    p = profile or {}
    inv = p.get("inventory")
    listings = p.get("marketplace_listings")
    return (len(inv) if isinstance(inv, list) else 0) + \
           (len(listings) if isinstance(listings, list) else 0)


def free(profile: dict) -> int:
    """Room left. Never negative — an over-cap profile reads 0, not -3."""
    return max(0, capacity(profile) - used(profile))


def can_add(profile: dict, n: int = 1) -> bool:
    return free(profile) >= max(0, int(n))


def full_message(profile: dict, what: str = "that") -> str:
    """The one refusal wording, so nine call sites cannot phrase it nine ways."""
    # `.capitalize()` would lowercase the rest — "Storm Pegasus" came out as
    # "Storm pegasus". Raise the first letter only, and leave the name alone.
    what = str(what or "that")
    what = what[:1].upper() + what[1:]
    return (f"🎒 Your inventory is **full** — {used(profile):,}/"
            f"{capacity(profile):,} slots. {what} could not be "
            f"added.\nSell something with `;quicksell`, or buy a permanent "
            f"slot for 🪙 **{EXTRA_SLOT_PRICE:,}** with `;beyslots buy`.")


def require_room(profile: dict, n: int = 1, what: str = "that") -> None:
    """Raise `InventoryFull` unless there is room for `n` more."""
    if not can_add(profile, n):
        raise InventoryFull(full_message(profile, what))


def quote_slots(profile: dict, n: int = 1) -> dict:
    """What buying `n` slots would cost and leave, without buying them."""
    n = max(1, int(n))
    have = capacity(profile)
    room = MAX_INVENTORY_SLOTS - have
    grant = min(n, max(0, room))
    return {"want": n, "grant": grant, "cost": grant * EXTRA_SLOT_PRICE,
            "capacity": have, "after": have + grant,
            "at_ceiling": room <= 0}


def buy_slots(profile: dict, n: int = 1) -> dict:
    """Charge for `n` permanent slots and grant them. Mutates `profile`.

    Call this INSIDE `database.mutate_user`: the balance is re-read under the
    lock, and raising abandons the whole write, so a refusal can never take
    the coins without granting the slots.

    Buying more than the ceiling allows is refused outright rather than
    silently trimmed — quietly selling somebody 500 slots and handing them 40
    is worse than saying no.
    """
    q = quote_slots(profile, n)
    if q["at_ceiling"]:
        raise SlotError(
            f"You are already at the maximum of "
            f"**{MAX_INVENTORY_SLOTS:,}** slots.")
    if q["grant"] < q["want"]:
        raise SlotError(
            f"That would pass the **{MAX_INVENTORY_SLOTS:,}**-slot maximum — "
            f"you can buy **{q['grant']:,}** more, not {q['want']:,}.")

    coins = int((profile or {}).get("coins", 0) or 0)
    if coins < q["cost"]:
        raise SlotError(
            f"**{q['grant']:,}** slot(s) cost 🪙 **{q['cost']:,}** — you have "
            f"**{coins:,}**, short by **{q['cost'] - coins:,}**.")

    profile["coins"] = coins - q["cost"]
    profile[K_EXTRA_SLOTS] = extra_slots(profile) + q["grant"]
    return {"bought": q["grant"], "spent": q["cost"],
            "coins": profile["coins"], "capacity": capacity(profile)}


def buy_slots_for(player_id: int, n: int = 1) -> dict:
    """`buy_slots` under the user lock."""
    from utils.database import mutate_user
    return mutate_user(int(player_id), lambda prof: buy_slots(prof, n))
