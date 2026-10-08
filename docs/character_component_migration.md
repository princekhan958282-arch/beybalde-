# Individual characters and physical component equipment

## Audited content

This follow-up starts from `dc33f6529b025f68cf00a37a0ee57c5b41c92486`
(current main, including PRs #207 and #208). The original content manifest in
`component_migration_baseline.json` remains unchanged: 132 runtime official
Beyblades (131 formerly authored in the monolith plus startup's Unlock Unicorn),
54 Avatars, 18 purchasable Disks, 17 purchasable Drivers and 16 legacy Rings.
Original record hashes verify IDs, total base stats, abilities, Specials, forms,
images, rarity, Avatar skills and shop metadata. No Avatar definition changes.

The former null-default/unsplit-frame compatibility strategy is superseded by
the user's authorized distribution. All 132 Beys now reference unique default
Disks and Drivers. There are **264 new defaults**, **150 Disk definitions** and
**149 Driver definitions** in total. Characters remain flat in root `beys/`
and `avatars/`; parts remain individual files in `parts/disks/` and
`parts/drivers/`. No character monolith or nested character folders return.

## Distribution and balance

For each integer HP/ATK/DEF/STM total, take integer floors with weights
18/40, 11/40 and 11/40. Assign the remaining points to the greatest fractional
remainders; ties use Frame, Disk, Driver order. This largest-remainder algorithm
uses integer arithmetic and exactly preserves each original total. Special is
intrinsic and unchanged. Dranzer retains its real HP 112 / ATK 105 / DEF 95 /
STM 100; its frame is 50 / 47 / 43 / 45. The illustrative 122/59/20/9 values
still do not match this repository's Dranzer.

Names combine the full original Bey identity with type themes: Impact/Rush
(Attack), Bastion/Anchor (Defense), Orbit/Glide (Stamina), Harmony/Pivot
(Balance), and Core/Spin where no standard type is recorded. Unknown generation
remains null rather than invented. Defaults have unrestricted compatibility
rules, following the existing empty-rules convention; purchased compatibility
rules are unchanged. No new compatibility restriction is introduced.

`stats` retains the complete stock statline for existing level/cap and display
interfaces. `main_frame` contains only the 45% component. Runtime assembly
sums this frame and the two equipped definitions afresh. The shared level
adapter applies **current parts minus stock parts** to the existing levelled
stock calculation, which is algebraically frame + current parts + existing
level growth. It does not add another full-Bey stat bonus. Mounted form deltas
remain frame modifiers; intrinsic abilities/Specials and form stats are unchanged.
Rings remain existing frame accessories. Avatar/mastery/type modifiers, stamina
rules, damage formulas and level caps retain their existing ordering.

**Purchased equipment values and prices are unchanged.** Their old definitions
are small bonus/penalty statlines with zero HP. With the newly authorized real
stock split, replacing stock with one of these removes the stock contribution.
Therefore previously upgraded effective totals can decrease, including HP.
The migration preserves those equipped selections rather than inventing
purchase-part balance, adding hidden stock bonuses or changing their stats.
This is a consequence of the requested replacement model, not a base-roster
rebalance. Reviewers should inspect purchased builds before deployment; no
additional balance numbers have been invented.

Existing HP pools use type-band conversion (PvP/Story) or a flat baseline (Boss).
Those conversions stay in place. Negative equipment HP replacement deltas now
reach those pools, alongside the existing positive growth; they are not dropped
by the previous positive-growth clamp. No damage formula changes.

## Physical ownership and migration

Existing inventory names, purchased `parts` names, per-species progression,
Custom Bey records and SQLite/MySQL architecture remain intact. Additive JSON:

- `bey_instances`: stable copy UUID, name, exactly one disk and driver UUID,
  `components_granted`, and original `bundled_parts` UUIDs.
- `part_instances`: stable physical UUID and authored `definition_id`.
- `active_bey_instance`: selected copy UUID.
- `component_equipment_version: 2`: profile adapter version.

Reconciliation creates two distinct defaults per owned official copy **once**.
Older name-only profiles and version-one definition-ID loadouts migrate under
existing profile locks. Purchased ownership and equipment selections survive;
unused bundled defaults remain available. Repeated reads, updates, restart,
and reconciliation preserve item IDs and counts. Missing/duplicate physical
records raise errors instead of silently minting replacements. Migration is
lazy on access and also runs before shared inventory persistence: spawn,
reward/code, shop/booster, admin and direct acquisition writes receive bundles.
No production profile was used for testing.

Parts have no independent equipped flag that could become stale. Assignment is
derived from Bey slots; available inventory is the physical ledger minus those
assignments. Switching replaces one UUID, returns the old UUID to availability,
keeps the other slot and recalculates from definitions. Physical IDs can be used
with `;equippart` to distinguish identical default definitions. Equipping an
already equipped part is idempotent. A physical UUID cannot occupy two Bey slots.

`;unequippart` restores that copy's original bundled part when it is still owned
and available. If it was transferred or equipped elsewhere, the command asks
for a replacement. It never leaves an empty slot or manufactures another part.
Purchased-part sales are atomic and restore available stock when needed; if
that stock is unavailable, select a replacement before selling.

Default definitions have `source: beyblade_default`, `shop_available: false`,
`tradable: false`, and **no price**. The shop catalogue contains only the original
51 purchasable entries. The purchase backend rejects defaults by ID or name,
even if called directly. Default parts cannot be sold/traded independently.
`myparts` and inventory display physical IDs and availability; existing panels
paginate larger collections rather than silently truncating them.

## Transfers and escrow

Trade moves the first named copy (the existing picker/name semantics), its UUID,
and its **currently attached two physical components** in one multi-profile
transaction. Available components stay with the sender. No recipient defaults
are granted again: the copy's grant marker travels with it. Default `tradable`
false prevents standalone part trading; attached components travel with their
Bey bundle. Purchased attached parts move their legacy ownership name as well.
The existing one-owned-copy-per-purchased-part-name rule remains: a trade/sale
to a recipient already owning that purchased definition is rejected atomically.
Choose another attached part before making that trade. Existing progression
remains per species/player, as it was before this equipment migration.

Marketplace listing escrows the same copy and two components outside available
inventory. Cancellation restores that bundle; purchase transfers it. Listings
and cancellation now run through locked mutations, and purchase retains the
existing atomic two-profile coin transfer and fee. Old pre-component listings
receive a recipient stock grant on acquisition. Ordinary consumption/removal
(quicksell, duplicate cleanup, admin removal) releases components to their
current owner; no part is silently destroyed with a removed frame.

Snapshots include the ledger and marketplace escrow in the Bey section. Restoring
an old Bey snapshot clears newer component fields absent from that backup before
running its one-time migration, avoiding a mixture of unrelated ledgers.

## Battle and Custom Bey compatibility

PvP, ranked/Tournament and Story share the existing session initialization;
Boss and effective-stat/card callers use the shared loadout adapter. Sessions
snapshot physical equipment/stat contributions, so later switching cannot
change a running battle. NPCs use stock authored totals; fixed boss copies keep
their previous rules. Avatar skill and ability engines are unchanged and tested
through execution. `;info`, `;ainfo` and all startup extensions still register.

Custom Beys remain outside the official directories and retain their existing
global purchased-equipment path, stat allocation, abilities, approval, pricing
and ownership. They are not given generated official default parts.
Maintenance streams preserve split metadata; creating new characters or changing
base stats requires explicit component JSON updates rather than resetting frames.

## Verification

**123 focused tests and 186 simulation checks pass**, in separate processes
because legacy harnesses patch global stores/modules:

| Suite | Result |
| --- | --- |
| Character migration, equipment and persistence | 16 tests |
| Physical conservation, all-132 totals, purchase blocking, migration, trade/escrow | 14 tests |
| Real PvP/ranked/Boss/Story and inventory/commands | 8 tests |
| Startup and command registration | 1 test, all 51 extensions |
| Avatar battle connection / Original Generation / Draciel / Boss flow | 8 / 24 / 19 / 6 tests |
| Bey and Avatar info cards | 10 / 17 tests |
| Stats / updater / snapshots / gift codes | 24 / 37 / 62 / 63 checks |

All 132 stock builds and duplicate bundles are checked; all 264 default
definitions are blocked from backend purchase by both ID and name. Repeated
switches, bought-part sale, SQLite restart, concurrent acquisition, transaction
rollback, actual trade callbacks, marketplace escrow/cancel/purchase and old
snapshot restore are exercised using isolated databases and mocks.
Compilation and whitespace checks pass. The PR workflow runs focused regressions.
Live Discord gateway behavior and live MySQL restart are not exercised.

Seven broader simulations fail **identically on untouched current main** and this
branch: inventory/shop rarity fixtures lack Limited/Boss Fighter; the level
suite has one check covering non-growing damaging Specials; booster suite has
one opponent stamina-drain assertion; Avatar fixtures lack Original Generation
HP bands/stability_percent weights; tournament draft weights lack Boss Fighter/
Limited. These pre-existing issues remain outside this focused migration.
Full Story statistical surveys are not used as a bounded regression test;
actual Story session initialization/execution tests pass.

## Deployment and rollback

Take a full player snapshot before deployment, deploy the entire checkout with
all JSON directories, and restart. Migration runs on the first profile access
or acquisition and saves under existing locks. Keep the snapshot for rollback.

To undo this release, stop the bot and restore the prior code **and the
predeployment player snapshot**, including Bey/parts/marketplace fields. Physical
UUID references differ from version-one definition IDs; do not run the old
component code against version-two profiles or try a code-only downgrade.
A full predeployment snapshot also restores escrow/currency consistently.
Never reset inventories or progression to perform a rollback. If trades or
purchases occurred after deployment, reconcile those transactions before
restoring the snapshot; review the snapshot before using existing restore tools.
