# Individual character files and component equipment

## Audit and content preservation

Baseline: `5267682163ced3dccd4dae6e3281ceed000d3855` (main / PR #206).
The committed `data/beyblades.json` contains **131** official definitions.
The existing startup migration adds **Unlock Unicorn**, so the running roster
contains **132**. All 132 are migrated. `avatar_data.json` contains **54** Avatars.
The Python parts catalogue contains **18 Disks, 17 Drivers and 16 Rings**.

`component_migration_baseline.json` records every original character ID, field
list, complete-record SHA-256, printed statline and the original part catalogue.
Tests reconstruct each original record from its original fields and compare
hashes, covering all abilities, special moves, forms, images, skills, shop and
restriction metadata. Registry ordering is preserved, including Avatar skill
order and shop catalogue order. No rarity/type/generation subfolders exist.

Old definitions came from two monolithic character files and the Python shop
catalogue. The authoritative layout is now:

```
beys/<one species>.json
avatars/<one avatar>.json
parts/disks/<one disk>.json
parts/drivers/<one driver>.json
```

The monolithic files are removed. Maintenance/simulation scripts use a
compatibility stream that reads/writes the individual files; it does not create
a second on-disk character registry. The former startup migration exports its
Unicorn constant from the individual file instead of keeping another authored
copy or writing definitions at startup.

The existing names, `BB...` IDs and `avatar_...` IDs remain unchanged. Parts had
name-based identities; new slug IDs are additive and names remain valid for
existing inventories and commands. Generation was not recorded in either
original catalogue; new `generation: null` properties mean **unknown**, not an
invented generation. Global type growth remains in `utils/bey_levels.py`.

## Balance decisions and unknown stock data

The existing `stats` are the game's **printed complete-Bey compatibility
statline**, not verified physical Main Frame contributions. Separate shop part
bonuses were added on top, including their penalties. No official character
record supplied a stock Disk/Driver assignment or a component decomposition.

Accordingly, each file uses `component_migration.strategy =
legacy_unsplit_frame`: the unchanged printed statline is carried as the
compatibility Main Frame. `default_parts.disk` and `.driver` are **null**.
An unassigned stock slot contributes an explicit zero delta; it is not a new
part, a shop item or a claim about the physical stock component's real stats.
We do not fabricate default part names, prices, HP bonuses or distributions.
All four stat keys exist in every shipped Disk/Driver definition. Existing
non-HP bonuses/penalties are converted exactly; **all current part HP deltas
are zero** because the existing catalogue supplies no HP modifiers.

The committed **Dranzer** is Balance, Epic, with HP 112 / ATK 105 / DEF 95 /
STM 100 (Special 125). The proposed HP 122 / ATK 59 / DEF 20 / STM 9 does not
match a repository variant; it has **not** replaced the real values.

Before assigning real stock components, a reviewer must supply verified stock
IDs and approved component contributions per Bey/form. Subtract those stock
contributions from the compatibility frame to preserve the old assembled
statline, update the compatibility `stats` adapter with the frame, and rerun
baseline/default-stat tests with an explicitly approved balance-data revision.
This PR enables customization with verified shop parts immediately without
silently decomposing or rebalancing the existing roster.

## Assembly and integration

`utils/character_registry.py` discovers and caches the four flat directories,
rejects duplicate IDs/names/aliases, malformed records, missing stats,
unresolved defaults and nested character folders. Lookup retains existing
name/ID interfaces. A bad reload leaves the previous validated cache intact.
No JSON is read on a battle turn. Content updates take effect after restart;
Avatar's existing reload entry point explicitly reloads its files.

`utils/bey_components.py` assembles each stat afresh from the mounted frame,
the selected copy's Disk and its Driver. It never modifies a frame or adds to
a saved total. Existing Rings remain legacy **frame modifiers**, retaining
ownership, prices, equip commands and their exact bonuses/tradeoffs. They are
not an additional assembled base-Bey bonus. Special remains the existing
intrinsic Special stat and follows the existing Special/Avatar rules.

The existing level growth/cap is applied to the intrinsic frame and component
deltas remain outside that cap, exactly as equipment worked before migration.
Mastery, Avatar bonuses, type passives, HP-pool conversion and combat formulas
retain their existing ordering. Mounted dual-spin forms retain their own
printed frame statline. No action, damage formula or ability engine is rewritten.

- PvP/Tournament/Story share `BattleSession.create` and snapshot the selected
  part IDs and stat deltas before constructing the session.
- Boss initialization and cards use the existing `effective_blade` resolver.
- NPCs receive no player equipment. Boss copies retain their fixed-roll rules.
- `;info` and `;ainfo` keep their existing renderers; card layouts are untouched.
- Custom Bey records stay in player persistence, outside `beys/`, and retain
  their previous global-equipment behavior. Creation/approval/pricing are unchanged.

## Ownership, UI and database compatibility

The existing `inventory: [name, name, ...]`, `parts: [part name, ...]` and
`bey_progress` structures remain intact. Additive JSON fields in each existing
SQLite/MySQL profile provide:

- `bey_instances`: ordered `{instance_id, name, parts: {disk?, driver?}}` records;
- `active_bey_instance`: the selected owned-copy UUID;
- `component_equipment_version`: an idempotent migration marker.

Old global Disk/Driver selections migrate once to the active official copy.
Other copies begin with the unchanged stock configuration. Coins, progress,
abilities, Custom Bey records and inventory names are not reset. The existing
SQLite/MySQL profile storage supports these extra JSON fields without a SQL
schema replacement. Snapshot Bey-section restore includes these fields.

Existing `;buy`, `;myparts`, `;equippart`, `;unequippart`, shop and inventory
buttons are reused. Part purchases retain the existing atomic profile mutation,
exact prices and one-owned-copy-per-part-name rule. Equipping/unequipping a
Disk/Driver now uses that same atomic profile mutation and validates current
ownership, compatibility and exclusive assignment to a single official copy.
Replacement removes only the same slot's reference; the other slot survives.

Players can select duplicates using the existing inventory dropdown, whose
items carry stable copy IDs and display inventory slot numbers. `;equip #12`
selects inventory slot 12. `;equip <name>` retains the name-based interface.
Disk/Driver configurations persist when switching copies and after restart.

Existing name-based sale/trade/listing APIs do not identify an individual
UUID. Reconciliation follows their `list.remove` semantics: removing the first
matching copy releases its parts back to their owner. A transferred/marketplace
Bey arrives with a stock build; equipped parts are **not** transferred or sold
with it. This avoids silently duplicating a uniquely owned part and leaves
existing trade, sell, listing, currency and progression behavior intact.
Per-copy level/IV progression is not introduced: the existing name-keyed
progress system remains unchanged. Custom Beys retain the legacy equipment
exception rather than receiving an unapproved player-record redesign.

## Validation and known baseline failures

All tests use isolated SQLite files or existing in-memory harnesses. No Discord
login, production database, real player or live purchase was used.

Passing focused suites:

| Suite | Result |
| --- | --- |
| `python -m unittest tools.test_character_components` | 16 tests |
| `python tools/test_component_battles.py` | 7 tests |
| `python tools/test_avatar_battle_connection.py` | 8 tests |
| `python tools/test_original_generation.py` | 24 tests |
| `python tools/test_draciel.py` | 19 tests |
| `python -m unittest tools.test_boss_flow` | 6 tests |
| `python tools/test_beycbot_info_card.py` | 10 tests |
| `python tools/sim_avatar_info_card.py` | 17 tests |
| `python tools/sim_stats_pipeline.py` | 24 checks |
| `python tools/sim_updater_paths.py` | 37 checks |
| `python tools/sim_snapshots.py` | 62 checks |
| `python -m unittest tools.test_character_startup` | all 51 extensions loaded; info/ainfo/part commands registered |
| Compileall / diff whitespace checks | passed |

The component suites cover exact migration, defaults, four-stat summation,
negative tradeoffs, replacement, idempotence, exclusive ownership, compatibility,
concurrent purchase charging, real SQLite restart, duplicate builds, inventory
controls, selected-build battle stats, HP-pool contribution and battle snapshots.
Existing suites exercise real Avatar skill/ability execution and info cards.

Broader legacy simulations were also run against the untouched baseline in a
separate worktree. These failures occur before and after this change and were
left outside scope:

| Legacy simulation | Same baseline failure |
| --- | --- |
| `sim_inventory_ui.py` | rarity-order fixture does not recognize `Limited` |
| `sim_shop_ui.py` | rarity presentation lacks `Boss Fighter`/`Limited` entries |
| `sim_levels.py` | 2 damaging Specials do not grow at level 100 |
| `sim_booster_beys.py` | one legacy opponent-stamina-drain assertion fails |
| `sim_avatar.py` | HP-band fixture lacks `Original Generation` |
| `sim_avatar_skills.py` | price-weight fixture lacks `stability_percent` |
| `sim_tournament.py` | draft rarity weights lack `Boss Fighter` and `Limited` |

The optional full `sim_story.py` statistical survey did not finish within the
verification window; it is not counted as a passing suite. Dedicated Story
build/real-session component tests and existing Draciel Story projections passed.

Live Discord gateway interactions and live MySQL restart were not exercised.
SQLite restart and the unchanged JSON profile API were tested. Browser profile
rendering dependency bootstrap was mocked for startup; actual card regression
suites were run separately. Existing harnesses mutate module globals, so the
listed battle suites must run in separate Python processes.

## Deployment and rollback

Review/merge through a PR only. Deploy the complete checkout/ZIP and restart;
include **all four new definition directories** with the Python adapters.
Existing hosted auto-update accepts their `.json` files while keeping live
`data/` player state protected. Removed monolithic files left behind by an old
ZIP updater are ignored by the new runtime; they are not a fallback registry.

Before deployment, use the existing snapshot command. For rollback:

1. Stop the bot and restore the previous code checkout/release, including the
   two previous monolithic character files; do not replace player databases.
2. Restart. Old code ignores additive `bey_instances` metadata and uses the
   retained active `equipped_parts` name mirror, keeping the active build's
   verified bonuses. Per-copy builds remain saved for a later redeploy.
3. If intentionally reverting player equipment as well, restore only the Bey
   section from the predeployment snapshot through the existing restore flow.
   Do not reset inventory or overwrite the whole database to roll back code.
