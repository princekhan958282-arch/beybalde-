# Dranzer evolution kits

Seven independent definitions live directly in `beys/`: `dranzer_g.json`,
`black_dranzer.json`, `dranzer_f.json`, `dranzer_v.json`, `dranzer_v2.json`,
`dranzer_gt.json`, and `dranzer_ms.json` (BB132–BB138).

The repository originally contained only Dranzer (BB043). The new variants copy
its stock stats, Epic combat tier, frame, generation, rotation and component
stats as explicitly requested. Each has its own default Disk/Driver definition
and references, two abilities, one Special, and the supplied image URL.
The original Dranzer and every other existing character/component record are
unchanged; original migration hashes remain authoritative.

Black Dranzer has `booster_exclusive: true`. This repository represents booster
exclusivity with an acquisition flag rather than a combat-rarity string: it
keeps the Epic tier and appears in booster pools, while ordinary spawn and
tournament reward pools exclude it. Black mechanics require both its kit marker
and its own identity; copying its ability descriptions cannot grant them to
another Beyblade.

## Battle behavior

`AbilityEngine.dranzer` owns the shared Phoenix runtime. Live PvP and Story use
the same BattleSession hooks. Boss fights and AI search project their HP,
stamina, stats and independent cloned runtime state into the same implementation.

- Cooldowns block the next N rounds, matching the existing Draciel convention.
  Two-round stat grants persist through the next two rounds; burn stacks tick
  at the application round end and the following round end, independently.
- Specials retain the existing gauge/stamina checks. Stacks and Dark Energy are
  snapshotted at cast start before consumption, including GT's Special discount.
- Normal Attacks use `calc_damage`/`base_damage`. Authored Special damage is
  base + ATK scaling, with inverse-DEF normalization (100 / effective DEF), as
  used by existing authored stat damage. DEF pierce changes that denominator;
  normal avatar, type, shield and damage-reduction stages still apply.
- Black heals 20% of actual, capped direct HP damage and transfers 4% of the
  opponent's current ATK/DEF once per damaging move. Transfers are equal and
  opposite, expire individually after three rounds, and cap at 16% of the
  opponent's effective stat before active theft. Its Special separately heals
  30%. No healing or theft is dispatched by burn, reflection, or extra hits.
- Forbidden Phoenix stores only damage it actually prevents, up to 35% max HP;
  at capacity further incoming damage is not absorbed. New damage received
  during a Special remains banked for the next Special.
- V2's second Special hit reads the first hit's DEF reduction. Resolved hits
  commit separately, so later hits can damage or defeat a reborn defender.
  Rebirth removes removable burn/silence/stat debuffs and Phoenix reductions,
  preserving permanent or explicitly nonremovable stat penalties.
- Secondary disabling and silence suppress the appropriate passive effects.
  Black's Special healing and F's Special burn remain Special effects.
- The fallback battle panel now displays cooldown rounds without dividing by
  a zero resource requirement. Phoenix stacks, energy and burns have status tags.

## Validation

Python 3.14.7, isolated local environment; no Discord login or deployed bot used.

- `python -m unittest tools.test_dranzer`: 31 passing tests, including all seven
  Specials in live rounds and boss exchanges, ability restrictions, stacks,
  durations, costs, actual-damage healing, rebirth, burn/reflection and Story
  search isolation.
- Migration, equipment and physical stock checks: 19 tests pass, including all
  frozen original character hashes and new component totals.
- Draciel, component battles, Original Generation, boss flow and startup suites
  pass. Avatar Special amplification passes over the full 139-character roster.
- Extended effects, tactical effects, battle-duration simulations and syntax
  compilation pass.

Pre-existing wider-suite issues reproduced against unchanged commit
`58ece803cce5c979c38fdd00c966e13b44cdc793`:

- Windows persistence-test cleanup attempts to delete SQLite files while
  connections remain open (WinError 32). Assertions in migration/equipment and
  physical stock tests pass; persistence cleanup needs separate repair.
- `test_combat_structure.LiveBoundary.test_authored_chance_and_enemy_suppression_before_roll`
  expects 283 HP damage, whereas the current shared rounding produces 282.

The GitHub workflow includes Phoenix regressions. Live Discord operation,
production deployment, and remote image availability still require deployment
validation; provided Discord CDN URLs retain their original expiry parameters.
