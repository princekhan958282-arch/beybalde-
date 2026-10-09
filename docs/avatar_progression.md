# Avatar progression

Open `;ainfo` for the equipped avatar, or `;ainfo <name or id>` for another owned avatar. The owned-card view has Level Up, Skill Level Up, Star Up/Try 6★/Try 7★, Feed Stage, and a skill selector. Details and Skills remain available. Views belong to the invoking player and expire; they deliberately do not register persistent views containing an old player balance.

## Balance and effects

All progression numbers live in `cogs/avatar/avatar_config.py`. Existing coin prices and card growth are retained: card maximum 5; skill maximum 10, gated by card level. Level-one effects do not change. Each star above 1 adds the configured flat ATK/DEF/STM gains. Numeric bonus/DSL skill magnitudes scale with skill level; timers, energy costs, counter thresholds and boolean mechanics remain unchanged. Selected noninteractive skills also gain configured flat stats. Original Generation skills gain a temporary ATK/DEF boost on activation, including skills with fixed control mechanics. Their costs, cooldowns and once-per-battle ultimate restrictions remain intact.

Safe star targets 2–5 require 1/2/3/4 spare copies and never roll. At 5★, feed five stages, one spare copy each. Then 6★ needs five copies and has a 50% chance; 7★ needs six copies and has a 25% chance. Failure consumes the required feeding copies and permanently removes the owned avatar, its level/skill progress and selection, and its equipped pointer. Leftover feeding copies remain resources; they are not an owned avatar. A future pack pull can grant a new level-one avatar.

## Storage and migration

Avatar ownership, feeding copies and progression share the existing SQLite/MySQL JSON profile row. No new SQL columns are needed. On first profile access/mutation, legacy `avatar_inventory.json` ownership is copied into `avatar_inventory`; repeated legacy IDs become extra feeding copies. The legacy file remains a backup. Presence of the new inventory key, including an empty list after loss, prevents reimport/resurrection. Existing card levels, skill levels and recorded spending are retained. Missing values read as level 1, skill level 1, 1★ and zero stages.

Pack duplicates now grant a feeding copy as well as their existing rarity refund. Original Generation still gives no duplicate coin refund and keeps its purchase cooldown. The single owned card is distinct from the spare-copy counter, so it and its equipped pointer are never used as feeding copies.

Risk confirmations expire after 30 seconds. Opening, cancelling or timing out never rolls or spends. Confirming closes/disables controls before awaiting and revalidates ownership, target tier, stages and copies under a per-user async lock and the database mutation lock. All changes are persisted in one profile upsert; a validation/roll exception abandons the mutation. These locks follow the bot's existing single-process storage model. Generic stale profile saves preserve authoritative ownership/progression to prevent an old battle snapshot from reviving a lost card. Administrative snapshot restore explicitly handles both new profiles and legacy ownership snapshots.

## Manual checks

1. Open `;ainfo` with an owned avatar. Verify level/max, progress bars, star rating, spare copies, cost and each skill's next-level preview. Try another user's buttons: the reply must be ephemeral and deny access.
2. Click Level Up with enough coins. Check the exact coin deduction, new stats and raised skill cap. With insufficient coins or maximum level, the button is disabled; server validation also rejects stale actions.
3. Select each skill and click Skill Level Up. Only that skill should change. Verify a new battle uses its scaled effect. For Original Generation, activate the skill and verify its temporary ATK/DEF boost, then expiry.
4. Pull an already-owned avatar. Confirm one feeding copy is added and the existing refund rules still apply. Original Generation refunds zero coins.
5. Star Up through 5★, confirming each attempt. Verify copy costs and guaranteed success; the owned/equipped card remains.
6. At 5★, feed five stages. Verify one copy per stage, progress 0/5 to 5/5, and Try 6★ stays disabled until full progress and sufficient copies.
7. Open a risky warning. Verify the avatar name, target, exact chance, required/owned copies, permanent-loss warning, red confirmation button, and extra 7★ warning. Cancel or wait 30 seconds: inventory, copies and stars must remain unchanged.
8. On a disposable test account, confirm a risky attempt. A success raises the tier; a failure removes the avatar and unequips it. Double-click or open two confirmations: the same tier attempt must never consume copies or roll twice. Restart and verify the result persists.
9. Verify inventory displays stars, level and spare copies. Verify snapshot collection/restore retains new progression fields and can import a legacy ownership snapshot.

## Automated checks

`python -m unittest tools.test_avatar_progression` covers migration, safe tiers, stage gates, both risky tiers, permanent loss, restart, stale saves, transaction rollback, concurrent attempts, ownership, costs, buttons, warnings, timeout/cancel, combat bonuses and snapshots.

`python tools/test_original_generation.py` covers all 27 interactive skills, including skill-level empowerment and expiry. Existing avatar roster, boss, component battle, startup, card and snapshot regression checks also run in CI. No live Discord login is required.

## Changed files

- `.github/workflows/character-components.yml` — runs progression tests in CI.
- `cogs/avatar/avatar_config.py` — progression balance.
- `cogs/avatar/avatar_collection.py` — ownership migration, feeding copies, stars and stages.
- `cogs/avatar/avatar_scaling.py` — independent battle skill snapshots and scaling.
- `cogs/avatar/avatar_progression_ui.py` — progress embeds, selectors, owner-only controls and confirmations.
- `cogs/avatar/avatar_levels.py` — existing arithmetic reads centralized config.
- `cogs/avatar/avatar_progress.py` — ownership validation and Original Generation skill upgrades.
- `cogs/avatar/avatar_engine.py` — applies star and selected skill bonuses.
- `cogs/avatar/avatar_shop.py` — duplicate copies/refunds and owned `;ainfo` controls.
- `cogs/avatar/avatar_upgrade.py` — prevents repeated legacy confirmation callbacks.
- `cogs/battle/session.py` — PvP/Story leveled skill snapshots.
- `cogs/battle/boss/boss_battle.py` — boss leveled skill snapshots.
- `cogs/battle/original_generation.py` — activation empowerment and expiry.
- `cogs/abilities/ability_engine.py` — cache includes skill levels.
- `cogs/ui/inventory_ui.py` — collection progression display.
- `utils/database.py` — profile ownership migration, copy grants and stale-save protection.
- `utils/snapshot.py` — progression backup/restore and authoritative ownership counts.
- `tools/test_avatar_progression.py` — new transactional and UI tests.
- `tools/test_original_generation.py` — upgraded skill/pack regression expectations.
- `tools/sim_avatar_info_card.py` — card plus progression embed regression expectations.
- `docs/avatar_progression.md` — this guide and complete file list.
