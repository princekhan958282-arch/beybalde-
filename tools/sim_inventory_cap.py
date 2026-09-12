#!/usr/bin/env python3
"""
tools/sim_inventory_cap.py — the 200-bey cap, and buying room past it.

The assertion that matters most is that the cap holds at **every** door.
Nine places in this codebase append to `profile["inventory"]`, and only three
of them go through `add_beyblade_to_inventory`. A cap enforced in three of
nine is not a cap — it is a cap on spawns.

The second thing tested here is that a refusal is *free*. Several of those
doors take money several lines before the bey is granted: the booster loop
deducts the whole cost up front, `;buybey` pays the seller before the buyer
gets anything. A capacity check placed at the append would have charged the
player and then had nowhere to put the item — so each check is asserted to sit
*above* the line that moves the coins, in the source, not just in behaviour.

Also here: listings occupy slots (so `;cancellisting` can never fail and
listing is not free unlimited storage), trade is net-neutral by construction,
slots cost exactly 10,000 and stop at 2,000, a player one coin short keeps
both their coins and their capacity, and an admin `;givebey` bypasses the cap
on purpose.

Run:  python3 tools/sim_inventory_cap.py
"""
import asyncio
import copy
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {detail}")


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


import utils.database as DB                                        # noqa: E402
import utils.inventory as INV                                      # noqa: E402


def profile(n_inv=0, n_listed=0, coins=0, bought=0):
    return {
        "inventory": [f"Bey {i}" for i in range(n_inv)],
        "marketplace_listings": [{"bey_name": f"Listed {i}", "price": 1}
                                 for i in range(n_listed)],
        "coins": coins,
        INV.K_EXTRA_SLOTS: bought,
    }


print("\n── 1. the numbers themselves ────────────────────────────────────")
check("everyone starts with 200 slots", INV.BASE_INVENTORY_SLOTS == 200)
check("a slot costs exactly 10,000", INV.EXTRA_SLOT_PRICE == 10_000)
check("the ceiling is 2,000", INV.MAX_INVENTORY_SLOTS == 2_000)
check("a fresh profile has 200 capacity", INV.capacity({}) == 200)
check("...and 200 free", INV.free({}) == 200)

check("duplicates each cost a slot",
      INV.used({"inventory": ["Valkyrie"] * 5}) == 5)
check("an over-cap profile reads 0 free, never negative",
      INV.free(profile(n_inv=250)) == 0, INV.free(profile(n_inv=250)))
check("a corrupt slot count degrades to 0 rather than raising",
      INV.capacity({INV.K_EXTRA_SLOTS: "twelve"}) == 200)
check("a negative slot count cannot shrink the base cap",
      INV.capacity({INV.K_EXTRA_SLOTS: -50}) == 200)

print("\n── 2. listings occupy slots ─────────────────────────────────────")
p = profile(n_inv=150, n_listed=50)
check("150 held + 50 listed == 200 used", INV.used(p) == 200)
check("...which means the profile is full", not INV.can_add(p))

# ;sellbey moves a bey from inventory to listings. If that freed a slot,
# listing would be unlimited free storage.
before = INV.used(p)
p["inventory"].pop()
p["marketplace_listings"].append({"bey_name": "Bey 149", "price": 999_999})
check("listing a bey does not free a slot", INV.used(p) == before)

# ;cancellisting is the only add site that must NEVER refuse. It can't,
# because the slot was never released — proven by round-tripping from full.
p["marketplace_listings"].pop()
p["inventory"].append("Bey 149")
check("cancelling always fits (round trip from full is still 200)",
      INV.used(p) == 200 and len(p["inventory"]) == 150)

check("list-then-cancel cannot be used to exceed the cap",
      not INV.can_add(p))

print("\n── 3. every door checks the cap ─────────────────────────────────")

_fake = {}
real_get, real_put = DB.USER_STORE.get_one, DB.USER_STORE.put_one
DB.USER_STORE.get_one = lambda u, **kw: copy.deepcopy(_fake.get(str(u)))
DB.USER_STORE.put_one = (lambda u, prof, **kw:
                         _fake.__setitem__(str(u), copy.deepcopy(prof)))

try:
    # -- door 1-3: spawn claim, ;start and redeem, all via the helper --------
    _fake["1"] = profile(n_inv=199, coins=0)
    check("at 199/200 the helper accepts",
          DB.add_beyblade_to_inventory(1, "Storm Pegasus") is True)
    check("...and at 200/200 it refuses",
          DB.add_beyblade_to_inventory(1, "Storm Pegasus") is False)
    check("a refused add does not grow the inventory",
          len(_fake["1"]["inventory"]) == 200,
          len(_fake["1"]["inventory"]))

    # A refusal must not equip either — the blade never existed.
    _fake["2"] = profile(n_inv=200)
    _fake["2"]["active_beyblade"] = None
    DB.add_beyblade_to_inventory(2, "Storm Pegasus")
    check("a refused add does not equip the bey that never arrived",
          _fake["2"].get("active_beyblade") is None)

    # Listings count here too, so a player with 200 parked is full.
    _fake["3"] = profile(n_inv=0, n_listed=200)
    check("200 listed and 0 held is still full",
          DB.add_beyblade_to_inventory(3, "Storm Pegasus") is False)

    # -- bought slots actually raise the ceiling ----------------------------
    _fake["4"] = profile(n_inv=200, bought=1)
    check("a bought slot makes room for exactly one more",
          DB.add_beyblade_to_inventory(4, "Storm Pegasus") is True)
    check("...and only one",
          DB.add_beyblade_to_inventory(4, "Storm Pegasus") is False)

    print("\n── 4. buying slots ──────────────────────────────────────────────")
    _fake["5"] = profile(n_inv=200, coins=25_000)
    res = asyncio.run(INV.buy_slots_for(5, 2))
    check("two slots cost 20,000", res["spent"] == 20_000, res)
    check("...leaving 5,000", _fake["5"]["coins"] == 5_000)
    check("...and a capacity of 202", res["capacity"] == 202)
    check("the purchase persisted", _fake["5"][INV.K_EXTRA_SLOTS] == 2)

    # One coin short keeps BOTH the coins and the capacity. This is the whole
    # reason buy_slots runs inside mutate_user: raising abandons the write.
    _fake["6"] = profile(n_inv=0, coins=9_999)
    try:
        asyncio.run(INV.buy_slots_for(6, 1))
        check("one coin short is refused", False, "it went through")
    except INV.SlotError as exc:
        check("one coin short is refused", True)
        check("...with the shortfall named", "1" in str(exc), str(exc))
    check("...the coins are untouched", _fake["6"]["coins"] == 9_999)
    check("...and so is the capacity", INV.capacity(_fake["6"]) == 200)

    # The ceiling refuses outright rather than trimming — quietly selling
    # somebody 500 slots and handing them 40 is worse than saying no.
    _fake["7"] = profile(coins=10_000_000, bought=1_760)
    check("at 1,960 capacity, buying 40 more works",
          asyncio.run(INV.buy_slots_for(7, 40))["capacity"] == 2_000)
    try:
        asyncio.run(INV.buy_slots_for(7, 1))
        check("at the ceiling, buying is refused", False, "it went through")
    except INV.SlotError:
        check("at the ceiling, buying is refused", True)
    check("...without spending a coin",
          _fake["7"]["coins"] == 10_000_000 - 40 * 10_000,
          _fake["7"]["coins"])

    _fake["8"] = profile(coins=10_000_000, bought=1_790)
    try:
        asyncio.run(INV.buy_slots_for(8, 50))          # only 10 would fit
        check("an order that overshoots the ceiling is refused whole",
              False, "it went through")
    except INV.SlotError as exc:
        check("an order that overshoots the ceiling is refused whole", True)
        check("...and says how many WOULD fit", "10" in str(exc), str(exc))
    check("...with nothing partially granted",
          _fake["8"][INV.K_EXTRA_SLOTS] == 1_790)

    check("2,000 slots cost 18,000,000 in total",
          (INV.MAX_INVENTORY_SLOTS - INV.BASE_INVENTORY_SLOTS)
          * INV.EXTRA_SLOT_PRICE == 18_000_000)

    print("\n── 5. an admin grant bypasses on purpose ────────────────────────")
    # The admin give writes the inventory directly and is meant to. An admin
    # handing out a bey is an explicit act by someone who can also raise the
    # cap. v1.13 moved it from `cogs/admin/admin.py` to the action registry.
    admin = src("cogs/admin/actions.py")
    give = admin[admin.index("async def _givebey"):][:2500]
    check("the givebey action does not import the cap helper",
          "utils.inventory" not in give)
    _fake["9"] = profile(n_inv=200)
    _fake["9"].setdefault("inventory", []).append("Admin Gift")
    DB.USER_STORE.put_one("9", _fake["9"])
    check("...so an admin CAN push a player past 200",
          len(_fake["9"]["inventory"]) == 201)

finally:
    DB.USER_STORE.get_one, DB.USER_STORE.put_one = real_get, real_put

print("\n── 6. the check sits ABOVE the money, in the source ─────────────")
shop = src("cogs/economy/shop.py")


def line_of(hay, needle, start=0):
    i = hay.index(needle, start)
    return hay[:i].count("\n") + 1


# -- booster packs: coins are deducted in one go before the roll loop -------
b0 = shop.index("async def booster")
b_end = shop.index("async def ", b0 + 20)
booster = shop[b0:b_end]
CHARGE = 'user["coins"] = coins -'
check("booster checks capacity before it charges",
      booster.index("_inv_free(user)") < booster.index(CHARGE),
      f"{line_of(shop, '_inv_free(user)')} vs {line_of(shop, CHARGE)}")
check("...and trims the order to what fits rather than refusing it",
      "amount = room" in booster)
check("...naming ;beyslots when it trims", ";beyslots" in booster)

# The trim is the whole refund story: coins are never taken for a pack that
# cannot be granted, because `amount` shrinks before `total_cost` is computed.
check("the trim happens before the cost is computed",
      booster.index("amount = room")
      < booster.index("total_cost = BOOSTER_PACK_PRICE * amount"))

# -- ;buybey: both profiles move in one database transaction ----------------
m0 = shop.index("async def buybey")
m_end = shop.index("async def ", m0 + 20)
buy = shop[m0:m_end]
check("buybey uses the atomic multi-user transaction",
      "mutate_users(" in buy and "update_user(" not in buy)
check("seller payout and buyer grant are inside the same callback",
      buy.index('seller_profile["coins"] =')
      < buy.index("return {\"listing\"")
      and buy.index('buyer_profile.setdefault("inventory"')
      < buy.index("return {\"listing\""))
check("buybey checks capacity before the seller is paid",
      buy.index("_inv_can_add(buyer_profile)")
      < buy.index('seller_profile["coins"] ='))
check("...and before the buyer is charged",
      buy.index("_inv_can_add(buyer_profile)")
      < buy.index('buyer_profile["coins"] = buyer_coins - price'))
check("...and before the listing is removed",
      buy.index("_inv_can_add(buyer_profile)") < buy.index("listings.remove"))

# -- ;cancellisting deliberately has NO check ------------------------------
c0 = shop.index("async def cancellisting")
cancel = shop[c0:shop.index("async def ", c0 + 20)]
check("cancellisting has no capacity check, by design",
      "utils.inventory" not in cancel and "can_add" not in cancel)

# -- tournament prize -------------------------------------------------------
# The v1.12 rewrite pays COINS, not an item, so there is no inventory cap to
# check any more — an item prize can be refused for a full bag after the player
# has already won, which is a bad thing to discover at the trophy ceremony.
# What still matters is that the payout is atomic: get_user/update_user is a
# race whose lost write is exactly how redeem.grant erased blades.
tour = src("cogs/tournament/tournament.py")
# `"update_user" not in tour` was the first attempt and it failed on the
# module's own DOCSTRING, which explains why update_user is not used. Match the
# CALL — with its paren — so prose about a mistake cannot be read as the mistake.
check("the tournament pays through mutate_user, not a get/update race",
      "mutate_user(" in tour and "update_user(" not in tour)
check("...and a failed payout cannot kill the trophy message",
      "except Exception" in tour[tour.index("def _award"):][:900])

# -- spawn claim reports the refusal --------------------------------------
spawn = src("cogs/spawn/spawn.py")
check("the spawn claim reports a full inventory",
      "full_message" in spawn)
check("...and returns without granting",
      re.search(r"full_message[\s\S]{0,600}?\n\s+return\b", spawn) is not None)

# -- redeem ----------------------------------------------------------------
check("redeem names ;beyslots when it refuses",
      ";beyslots" in src("cogs/codes/redeem.py"))

print("\n── 7. trade is net-neutral, so it cannot cross the cap ──────────")
trade = src("cogs/extras/trade.py")
seg = trade[trade.index('a_prof["inventory"].append') - 900:]
seg = seg[:1400]
check("each side removes exactly one bey", seg.count(".remove(") == 2, seg.count(".remove("))
check("...and appends exactly one", seg.count('["inventory"].append') == 2)

# Prove it rather than assume it: a full profile that gives one and takes one
# is still exactly full.
a = profile(n_inv=200)
b = profile(n_inv=200)
a["inventory"].remove("Bey 0"); b["inventory"].append("Bey 0")
b["inventory"].remove("Bey 5"); a["inventory"].append("Bey 5")
check("a 1-for-1 trade between two FULL players stays at 200/200",
      INV.used(a) == 200 and INV.used(b) == 200, (INV.used(a), INV.used(b)))

print("\n── 8. the refusal wording ───────────────────────────────────────")
msg = INV.full_message(profile(n_inv=200), "Storm Pegasus")
check("names the usage", "200/200" in msg.replace(",", ""), msg)
check("names the price", "10,000" in msg, msg)
check("points at ;beyslots", ";beyslots" in msg, msg)
check("names what was lost", "Storm Pegasus" in msg, msg)
check("a bey name keeps its capitals",
      "Storm Pegasus" in msg and "Storm pegasus" not in msg, msg)
check("every site that sends a standalone refusal uses full_message",
      all("full_message" in src(f) for f in
          ("cogs/spawn/spawn.py", "cogs/economy/shop.py")))
# Redeem is the deliberate exception: its refusal is one bullet inside a list
# of "here is what your code gave you", where a three-line message with its
# own heading would read as a separate event. It still names `;beyslots`.
check("redeem's compact refusal still points at the same command",
      ";beyslots" in src("cogs/codes/redeem.py"))

print("\n── 8b. redeeming coins and a blade together keeps both ──────────")
# `grant` used to snapshot the profile, then call add_beyblade_to_inventory
# (which writes the inventory under the user lock), then write the STALE
# snapshot back — erasing the blade on any code granting both.
redeem = src("cogs/codes/redeem.py")
check("grant no longer writes a stale profile snapshot",
      "update_user(user_id, profile)" not in redeem)
check("...it applies the coin delta under the lock instead",
      "coin_delta" in redeem and "mutate_user" in redeem)

try:
    INV.require_room(profile(n_inv=200), 1, "Storm Pegasus")
    check("require_room raises when full", False, "it returned")
except INV.InventoryFull as exc:
    check("require_room raises when full", True)
    check("...with that same wording", str(exc) == msg, str(exc))
check("require_room is silent when there is room",
      INV.require_room(profile(n_inv=199), 1) is None)

print("\n── 9. the casino keeps ;slots ───────────────────────────────────")
check("the inventory command is ;beyslots",
      'name="beyslots"' in shop)
check("...and does not claim the casino's ;slots",
      'name="slots"' not in shop)
check("the casino still owns ;slots",
      'name="slots"' in src("cogs/casino/slots.py"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
